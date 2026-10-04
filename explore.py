import logging
import re
import threading
import time
import urllib.parse
from difflib import SequenceMatcher

import library
import models
from config import load_config
from downloader import (
    _coverage_text,
    _fold,
    _parse_ytmusicapi_duration,
    _ytmusicapi_client,
)

logger = logging.getLogger(__name__)

FEED_TTL = 1800
PAGE_TTL = 86400
MATCH_TTL = 600
FAILURE_TTL = 60
SUGGEST_TTL = 3600
THUMB_MAX = 544
CACHE_MAX_ITEMS = 600
SHELF_LIMIT = 20
TRACK_LIMIT = 200
QUERY_MAX_LENGTH = 100

ALBUM_ID_RE = re.compile(r"^MPREb_[A-Za-z0-9_-]{4,40}$")
ARTIST_ID_RE = re.compile(r"^(?:UC|MPLA)[A-Za-z0-9_-]{10,60}$")
PLAYLIST_ID_RE = re.compile(
    r"^(?:VL)?(?:PL|OLAK5uy_|RDCLAK5uy_|RDAMPL|RDTMAK5uy_|RD|UU|OL)[A-Za-z0-9_-]{6,80}$"
)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")
MOOD_PARAMS_RE = re.compile(r"^[A-Za-z0-9_%=-]{8,160}$")
COUNTRY_RE = re.compile(r"^[A-Z]{2}$")
YEAR_RE = re.compile(r"^(?:19|20)\d{2}$")
_SIZE_RE = re.compile(r"=w\d+-h\d+|=s\d+(?=$|-)")
_THUMB_HOSTS = (".googleusercontent.com", ".ytimg.com", ".ggpht.com")

SKIP_RESULT_TYPES = frozenset({"podcast", "episode", "profile", "station", "upload"})
ALBUM_RESULT_TYPES = frozenset({"album", "single", "ep"})
SONG_VIDEO_TYPES = frozenset({"MUSIC_VIDEO_TYPE_ATV"})
VIDEO_VIDEO_TYPES = frozenset({
    "MUSIC_VIDEO_TYPE_OMV", "MUSIC_VIDEO_TYPE_UGC",
    "MUSIC_VIDEO_TYPE_OFFICIAL_SOURCE_MUSIC",
})
PODCAST_VIDEO_TYPES = frozenset({"MUSIC_VIDEO_TYPE_PODCAST_EPISODE"})
RELEASE_TYPES = {"album": "Album", "single": "Single", "ep": "EP"}
VARIOUS_ARTISTS = frozenset({"various artists", "various", "artisti vari", "verschiedene interpreten", "varios artistas", "artistes divers"})

_EDITION_WORDS = (
    "remaster", "remastered", "deluxe", "expanded", "anniversary", "edition",
    "version", "bonus", "explicit", "clean", "mono", "stereo", "reissue",
    "special", "collector", "super", "legacy", "edizione", "rimasterizzato",
)
_BRACKET_RE = re.compile(r"[\(\[]([^\)\]]*)[\)\]]")
_DASH_EDITION_RE = re.compile(
    r"\s+-\s+(?:[^-]*\b(?:" + "|".join(_EDITION_WORDS) + r")\b[^-]*)$",
    re.IGNORECASE,
)

MATCH_ACCEPT = 0.85
MATCH_MARGIN = 0.08
MATCH_MIN_TITLE = 0.7
MATCH_MIN_ARTIST = 0.6
ARTIST_ACCEPT = 0.95

STATUS_COMPLETE = "complete"
STATUS_MISSING = "missing"
STATUS_UNMONITORED = "unmonitored"
STATUS_NOT_IN_LIBRARY = "not_in_library"
STATUS_NOT_ON_MB = "not_on_musicbrainz"
STATUS_AMBIGUOUS = "ambiguous"
STATUS_UNAVAILABLE = "unavailable"


class ExploreError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


class TTLCache:
    def __init__(self, max_items=CACHE_MAX_ITEMS, clock=time.monotonic):
        self._data = {}
        self._lock = threading.Lock()
        self._max = max_items
        self.clock = clock

    def get(self, key):
        with self._lock:
            entry = self._data.get(key)
            if entry is None:
                return None
            expires, value = entry
            if expires <= self.clock():
                del self._data[key]
                return None
            return value

    def set(self, key, value, ttl):
        with self._lock:
            now = self.clock()
            if len(self._data) >= self._max:
                for k in [k for k, (exp, _) in self._data.items() if exp <= now]:
                    del self._data[k]
                while len(self._data) >= self._max:
                    del self._data[min(self._data, key=lambda k: self._data[k][0])]
            self._data[key] = (now + ttl, value)

    def invalidate(self, prefix=None):
        with self._lock:
            if prefix is None:
                count = len(self._data)
                self._data.clear()
                return count
            keys = [k for k in self._data if str(k[0] if isinstance(k, tuple) else k).startswith(prefix)]
            for k in keys:
                del self._data[k]
            return len(keys)

    def __len__(self):
        with self._lock:
            return len(self._data)


cache = TTLCache()
_FAILED = object()
_clients = {}
_clients_lock = threading.Lock()


class _Client:
    def __init__(self, yt):
        self.yt = yt
        self.lock = threading.Lock()


def locale():
    cfg = load_config()
    return cfg.get("explore_language") or "en", cfg.get("explore_country") or "IT"


def _client():
    language, country = locale()
    key = (language, country)
    with _clients_lock:
        client = _clients.get(key)
        if client is None:
            yt = _ytmusicapi_client(language=language, location=country)
            if yt is None:
                return None
            client = _Client(yt)
            _clients[key] = client
        return client


def reset_clients():
    with _clients_lock:
        _clients.clear()


def call(method, *args, **kwargs):
    client = _client()
    if client is None:
        raise ExploreError("YouTube Music is not available.", 503)
    with client.lock:
        return getattr(client.yt, method)(*args, **kwargs)


def raw_browse(browse_id, params=None):
    body = {"browseId": browse_id}
    if params:
        body["params"] = params
    return call("_send_request", "browse", body)


def cached(key, ttl, loader):
    full_key = (key[0], locale()) + tuple(key[1:])
    value = cache.get(full_key)
    if value is _FAILED:
        return None
    if value is not None:
        return value
    try:
        value = loader()
    except Exception as e:
        logger.info("Explore: %s unavailable (%s)", key[0], str(e)[:160])
        cache.set(full_key, _FAILED, FAILURE_TTL)
        return None
    if value is None:
        cache.set(full_key, _FAILED, FAILURE_TTL)
        return None
    cache.set(full_key, value, ttl)
    return value


def invalidate(prefix=None):
    return cache.invalidate(prefix)


def _str(value):
    return value.strip() if isinstance(value, str) else ""


def _thumb_size(t):
    w = t.get("width") or 0
    h = t.get("height") or 0
    if not (w and h):
        m = re.search(r"=w(\d+)-h(\d+)", t.get("url") or "")
        if m:
            w, h = int(m.group(1)), int(m.group(2))
        else:
            m = re.search(r"=s(\d+)(?:$|-)", t.get("url") or "")
            if m:
                w = h = int(m.group(1))
    return int(w or 0), int(h or 0)


def _allowed_image(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = urllib.parse.urlparse(url).hostname or ""
    return any(host.endswith(h) or host == h.lstrip(".") for h in _THUMB_HOSTS)


def best_thumbnail_url(thumbnails, max_size=THUMB_MAX):
    usable = [
        t for t in (thumbnails or [])
        if isinstance(t, dict) and _allowed_image(t.get("url"))
    ]
    if not usable:
        return ""
    sized = [(t, _thumb_size(t)) for t in usable]
    within = [(t, s) for t, s in sized if max(s) <= max_size]
    if within:
        t, s = max(within, key=lambda x: x[1][0] * x[1][1])
    else:
        t, s = min(sized, key=lambda x: x[1][0] * x[1][1])
    url = t["url"]
    if max(s) != max_size and "googleusercontent.com" in url and _SIZE_RE.search(url):
        url = _resize(url, max_size)
    return url


def _resize(url, size):
    return _SIZE_RE.sub(
        lambda m: f"=s{size}" if m.group(0).startswith("=s") else f"=w{size}-h{size}",
        url, count=1,
    )


def proxied(url):
    if not url or not _allowed_image(url):
        return ""
    return "/api/thumbnail?url=" + urllib.parse.quote(url, safe="")


def cover_url(thumbnails, size=1200):
    url = best_thumbnail_url(thumbnails, max_size=10000)
    if url and "googleusercontent.com" in url and _SIZE_RE.search(url):
        url = _resize(url, size)
    return url


def _artists(raw):
    out = []
    for a in raw.get("artists") or []:
        if isinstance(a, dict) and _str(a.get("name")):
            aid = a.get("id") if isinstance(a.get("id"), str) else ""
            out.append({
                "name": _str(a["name"]),
                "id": aid if ARTIST_ID_RE.match(aid or "") else "",
            })
        elif isinstance(a, str) and a.strip():
            out.append({"name": a.strip(), "id": ""})
    if not out:
        artist = raw.get("artist")
        if isinstance(artist, str) and artist.strip() and raw.get("resultType") != "artist":
            out.append({"name": artist.strip(), "id": ""})
        author = raw.get("author")
        if isinstance(author, list):
            for a in author:
                if isinstance(a, dict) and _str(a.get("name")):
                    out.append({"name": _str(a["name"]), "id": ""})
        elif isinstance(author, dict) and _str(author.get("name")):
            out.append({"name": _str(author["name"]), "id": ""})
        elif isinstance(author, str) and author.strip():
            out.append({"name": author.strip(), "id": ""})
    return out


def _duration(raw):
    seconds = raw.get("duration_seconds")
    if isinstance(seconds, int) and seconds > 0:
        return seconds
    text = raw.get("duration") or raw.get("length") or ""
    if isinstance(text, str) and ":" in text:
        return _parse_ytmusicapi_duration(text)
    return 0


def _year(raw):
    year = raw.get("year")
    if isinstance(year, int):
        year = str(year)
    return year if isinstance(year, str) and YEAR_RE.match(year.strip()) else ""


def _album_ref(raw):
    album = raw.get("album")
    if isinstance(album, dict):
        aid = album.get("id") if isinstance(album.get("id"), str) else ""
        return {
            "name": _str(album.get("name")),
            "id": aid if ALBUM_ID_RE.match(aid or "") else "",
        }
    if isinstance(album, str) and album.strip():
        return {"name": album.strip(), "id": ""}
    return None


def item_kind(raw):
    result_type = (raw.get("resultType") or "").lower()
    if result_type in SKIP_RESULT_TYPES:
        return None
    if raw.get("podcast") or raw.get("podcastId"):
        return None
    video_type = raw.get("videoType") or ""
    if video_type in PODCAST_VIDEO_TYPES:
        return None
    browse_id = raw.get("browseId") if isinstance(raw.get("browseId"), str) else ""
    if result_type in ALBUM_RESULT_TYPES or browse_id.startswith("MPREb_"):
        return "album"
    if result_type == "artist" or browse_id.startswith(("UC", "MPLA")):
        return "artist"
    if result_type == "playlist":
        return "playlist"
    if browse_id.startswith(("MPSP", "MPED")):
        return None
    if raw.get("videoId"):
        if result_type == "song" or video_type in SONG_VIDEO_TYPES:
            return "song"
        if result_type == "video" or video_type in VIDEO_VIDEO_TYPES:
            return "video"
        return "song" if _album_ref(raw) else "video"
    if browse_id.startswith("VL") or raw.get("playlistId"):
        return "playlist"
    return None


def _item_id(raw, kind):
    if kind == "album":
        value = raw.get("browseId") or ""
        return value if ALBUM_ID_RE.match(value) else ""
    if kind == "artist":
        value = raw.get("browseId") or ""
        if not value:
            for a in raw.get("artists") or []:
                if isinstance(a, dict) and isinstance(a.get("id"), str):
                    value = a["id"]
                    break
        return value if ARTIST_ID_RE.match(value or "") else ""
    if kind == "playlist":
        value = raw.get("playlistId") or raw.get("browseId") or ""
        if isinstance(value, str) and value.startswith("VL"):
            value = value[2:]
        return value if PLAYLIST_ID_RE.match(value or "") else ""
    value = raw.get("videoId") or ""
    return value if VIDEO_ID_RE.match(value) else ""


def _release_type(raw, kind):
    if kind != "album":
        return ""
    value = _str(raw.get("type"))
    if value:
        return value
    return RELEASE_TYPES.get((raw.get("resultType") or "").lower(), "Album")


def _subtitle(kind, item, raw):
    names = ", ".join(a["name"] for a in item["artists"])
    if kind == "album":
        return " · ".join(p for p in (item["type"], names, item["year"]) if p)
    if kind == "artist":
        subs = _str(raw.get("subscribers"))
        return f"{subs} subscribers" if subs else "Artist"
    if kind == "playlist":
        description = _str(raw.get("description"))
        if description:
            return description[:120]
        count = raw.get("count") or raw.get("itemCount")
        parts = [names, f"{count} songs" if count else ""]
        return " · ".join(p for p in parts if p) or "Playlist"
    album = item.get("album") or {}
    parts = [names, album.get("name") if kind == "song" else _str(raw.get("views"))]
    return " · ".join(p for p in parts if p)


def normalize_item(raw):
    if not isinstance(raw, dict):
        return None
    kind = item_kind(raw)
    if kind is None:
        return None
    item_id = _item_id(raw, kind)
    if not item_id:
        return None
    artists = _artists(raw)
    title = _str(raw.get("title"))
    if not title and kind == "artist":
        title = _str(raw.get("artist")) or (artists[0]["name"] if artists else "")
    if not title:
        return None
    item = {
        "kind": kind,
        "id": item_id,
        "title": title,
        "subtitle": "",
        "artists": [] if kind == "artist" else artists,
        "year": _year(raw),
        "type": _release_type(raw, kind),
        "thumbnail": proxied(best_thumbnail_url(raw.get("thumbnails") or raw.get("thumbnail"))),
        "explicit": bool(raw.get("isExplicit")),
        "duration": _duration(raw) if kind in ("song", "video") else 0,
    }
    if kind in ("song", "video"):
        item["album"] = _album_ref(raw)
        item["available"] = raw.get("isAvailable") is not False
        if raw.get("rank"):
            item["rank"] = _str(str(raw.get("rank")))
    if kind == "album":
        playlist_id = raw.get("audioPlaylistId") or raw.get("playlistId") or ""
        item["playlistId"] = playlist_id if PLAYLIST_ID_RE.match(playlist_id or "") else ""
    item["subtitle"] = _subtitle(kind, item, raw)
    return item


def normalize_items(raws, limit=SHELF_LIMIT, kinds=None):
    out = []
    seen = set()
    for raw in raws or []:
        try:
            item = normalize_item(raw)
        except Exception as e:
            logger.debug("Explore: skipped an item (%s)", e)
            continue
        if item is None or (kinds and item["kind"] not in kinds):
            continue
        key = (item["kind"], item["id"])
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _runs_text(node):
    if not isinstance(node, dict):
        return ""
    if "simpleText" in node:
        return _str(node.get("simpleText"))
    return "".join(r.get("text", "") for r in node.get("runs") or [] if isinstance(r, dict)).strip()


def _thumbs_of(renderer):
    for path in (
        ("thumbnailRenderer", "musicThumbnailRenderer", "thumbnail", "thumbnails"),
        ("thumbnail", "musicThumbnailRenderer", "thumbnail", "thumbnails"),
    ):
        node = renderer
        for key in path:
            node = node.get(key) if isinstance(node, dict) else None
        if isinstance(node, list):
            return node
    return []


def _endpoint_info(endpoint):
    if not isinstance(endpoint, dict):
        return {}
    browse = endpoint.get("browseEndpoint")
    if isinstance(browse, dict):
        page = (
            browse.get("browseEndpointContextSupportedConfigs", {})
            .get("browseEndpointContextMusicConfig", {})
            .get("pageType", "")
        )
        return {"browseId": browse.get("browseId") or "", "pageType": page}
    watch = endpoint.get("watchEndpoint")
    if isinstance(watch, dict):
        video_type = (
            watch.get("watchEndpointMusicSupportedConfigs", {})
            .get("watchEndpointMusicConfig", {})
            .get("musicVideoType", "")
        )
        return {
            "videoId": watch.get("videoId") or "",
            "playlistId": watch.get("playlistId") or "",
            "videoType": video_type,
        }
    return {}


def _runs_meta(runs):
    artists = []
    year = ""
    release_type = ""
    album = None
    texts = []
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        text = _str(run.get("text"))
        if not text or text in ("•", "·", "&", ","):
            continue
        info = _endpoint_info(run.get("navigationEndpoint"))
        page = info.get("pageType", "")
        if page in ("MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_USER_CHANNEL"):
            artists.append({"name": text, "id": info.get("browseId", "")})
        elif page == "MUSIC_PAGE_TYPE_ALBUM":
            album = {"name": text, "id": info.get("browseId", "")}
        elif YEAR_RE.match(text):
            year = text
        elif text.lower() in RELEASE_TYPES and not release_type:
            release_type = RELEASE_TYPES[text.lower()]
        texts.append(text)
    return artists, year, release_type, album, texts


def _explicit_badge(renderer):
    for badge in renderer.get("subtitleBadges") or renderer.get("badges") or []:
        icon = (badge.get("musicInlineBadgeRenderer") or {}).get("icon", {})
        if icon.get("iconType") == "MUSIC_EXPLICIT_BADGE":
            return True
    return False


def _raw_two_row(r):
    info = _endpoint_info(r.get("navigationEndpoint"))
    artists, year, release_type, album, texts = _runs_meta((r.get("subtitle") or {}).get("runs"))
    raw = {
        "title": _runs_text(r.get("title")),
        "thumbnails": _thumbs_of(r),
        "artists": artists,
        "year": year,
        "isExplicit": _explicit_badge(r),
    }
    page = info.get("pageType", "")
    if info.get("browseId"):
        raw["browseId"] = info["browseId"]
        if page == "MUSIC_PAGE_TYPE_ALBUM":
            raw["resultType"] = "album"
            raw["type"] = release_type or "Album"
        elif page in ("MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_USER_CHANNEL"):
            raw["resultType"] = "artist"
            raw["subscribers"] = texts[-1].split(" ")[0] if texts else ""
        elif page == "MUSIC_PAGE_TYPE_PLAYLIST":
            raw["resultType"] = "playlist"
            raw["description"] = " · ".join(texts)
        else:
            return None
    elif info.get("videoId"):
        raw["videoId"] = info["videoId"]
        raw["videoType"] = info.get("videoType", "")
        raw["album"] = album
        if texts:
            raw["views"] = texts[-1] if not artists or texts[-1] != artists[-1]["name"] else ""
    else:
        return None
    return raw


def _flex_runs(r, index):
    columns = r.get("flexColumns") or []
    if index >= len(columns):
        return []
    col = columns[index].get("musicResponsiveListItemFlexColumnRenderer") or {}
    return (col.get("text") or {}).get("runs") or []


def _raw_responsive(r):
    title_runs = _flex_runs(r, 0)
    title = "".join(run.get("text", "") for run in title_runs).strip()
    raw = {"title": title, "thumbnails": _thumbs_of(r), "isExplicit": _explicit_badge(r)}
    second = _flex_runs(r, 1)
    artists, year, release_type, album, texts = _runs_meta(second)
    third_artists, _, _, third_album, third_texts = _runs_meta(_flex_runs(r, 2))
    if album is None:
        album = third_album
    if album is None and third_texts:
        album = {"name": third_texts[0], "id": ""}
    raw["artists"] = artists or third_artists
    if not raw["artists"] and texts:
        raw["artists"] = [{"name": texts[0], "id": ""}]
    raw["year"] = year
    video_id = (r.get("playlistItemData") or {}).get("videoId") or ""
    info = _endpoint_info(r.get("navigationEndpoint"))
    if not video_id and title_runs:
        info = info or _endpoint_info(title_runs[0].get("navigationEndpoint"))
    if video_id or info.get("videoId"):
        raw["videoId"] = video_id or info.get("videoId")
        raw["videoType"] = info.get("videoType", "")
        raw["album"] = album
        rank = (r.get("customIndexColumn") or {}).get("musicCustomIndexColumnRenderer")
        if rank:
            raw["rank"] = _runs_text(rank.get("text"))
        return raw
    page = info.get("pageType", "")
    if info.get("browseId"):
        raw["browseId"] = info["browseId"]
        if page in ("MUSIC_PAGE_TYPE_ARTIST", "MUSIC_PAGE_TYPE_USER_CHANNEL"):
            raw["resultType"] = "artist"
            raw["artists"] = []
            raw["subscribers"] = (texts[0].split(" ")[0] if texts else "")
        elif page == "MUSIC_PAGE_TYPE_ALBUM":
            raw["resultType"] = "album"
            raw["type"] = release_type or "Album"
        elif page == "MUSIC_PAGE_TYPE_PLAYLIST":
            raw["resultType"] = "playlist"
        else:
            return None
        return raw
    return None


def _raw_item(entry):
    if not isinstance(entry, dict):
        return None
    if "musicTwoRowItemRenderer" in entry:
        return _raw_two_row(entry["musicTwoRowItemRenderer"])
    if "musicResponsiveListItemRenderer" in entry:
        return _raw_responsive(entry["musicResponsiveListItemRenderer"])
    return None


def _raw_mood(entry):
    button = entry.get("musicNavigationButtonRenderer") if isinstance(entry, dict) else None
    if not isinstance(button, dict):
        return None
    browse = (button.get("clickCommand") or {}).get("browseEndpoint") or {}
    params = browse.get("params") or ""
    if browse.get("browseId") != "FEmusic_moods_and_genres_category" or not params:
        return None
    return {"title": _runs_text(button.get("buttonText")), "params": params}


def _section_contents(data):
    try:
        tabs = data["contents"]["singleColumnBrowseResultsRenderer"]["tabs"]
        return tabs[0]["tabRenderer"]["content"]["sectionListRenderer"]["contents"]
    except (KeyError, IndexError, TypeError):
        return []


def raw_sections(data):
    sections = []
    for section in _section_contents(data):
        if not isinstance(section, dict) or not section:
            continue
        name, shelf = next(iter(section.items()))
        if not isinstance(shelf, dict):
            continue
        header = shelf.get("header") or {}
        title = ""
        for key in (
            "musicCarouselShelfBasicHeaderRenderer",
            "musicImmersiveCarouselShelfBasicHeaderRenderer",
            "gridHeaderRenderer",
        ):
            if key in header:
                title = _runs_text(header[key].get("title"))
                break
        if not title:
            title = _runs_text(shelf.get("title"))
        entries = shelf.get("contents") or shelf.get("items") or []
        moods = [m for m in (_raw_mood(e) for e in entries) if m]
        raws = [r for r in (_raw_item(e) for e in entries) if r]
        sections.append({"title": title, "raws": raws, "moods": moods, "renderer": name})
    return sections


def _shelves_from_raw(data):
    shelves = []
    for section in raw_sections(data):
        items = normalize_items(section["raws"])
        if items:
            shelves.append({"title": section["title"], "items": items})
    return shelves


def _home_loader():
    try:
        rows = call("get_home", limit=6)
        shelves = []
        for row in rows or []:
            items = normalize_items((row or {}).get("contents"))
            if items:
                shelves.append({"title": _str(row.get("title")), "items": items})
        return shelves
    except ExploreError:
        raise
    except Exception as e:
        logger.debug("Explore: get_home failed, parsing the raw page (%s)", e)
    return _shelves_from_raw(raw_browse("FEmusic_home"))


def home_shelves():
    return cached(("home",), FEED_TTL, _home_loader) or []


def _explore_from_parsed(data):
    out = {
        "new_releases": normalize_items(data.get("new_releases"), kinds=("album",)),
        "trending": normalize_items((data.get("trending") or {}).get("items")),
        "top_songs": normalize_items((data.get("top_songs") or {}).get("items")),
        "new_videos": normalize_items(data.get("new_videos")),
        "moods": [
            {"title": _str(m.get("title")), "params": m.get("params")}
            for m in data.get("moods_and_genres") or []
            if isinstance(m, dict) and MOOD_PARAMS_RE.match(m.get("params") or "")
            and _str(m.get("title"))
        ],
        "trending_playlist": "",
    }
    playlist = (data.get("trending") or {}).get("playlist") or ""
    if playlist.startswith("VL"):
        playlist = playlist[2:]
    if PLAYLIST_ID_RE.match(playlist):
        out["trending_playlist"] = playlist
    return out


def _explore_from_raw(data):
    out = {
        "new_releases": [], "trending": [], "top_songs": [], "new_videos": [],
        "moods": [], "trending_playlist": "", "other": [],
    }
    for section in raw_sections(data):
        items = normalize_items(section["raws"])
        out["moods"].extend(
            m for m in section["moods"] if MOOD_PARAMS_RE.match(m["params"]) and m["title"]
        )
        if not items:
            continue
        kinds = {i["kind"] for i in items}
        if kinds == {"album"} and not out["new_releases"]:
            out["new_releases"] = items
        elif kinds <= {"song", "video"} and any(i.get("album") for i in items) and not out["trending"]:
            out["trending"] = items
        elif kinds <= {"video", "song"} and not out["new_videos"]:
            out["new_videos"] = items
        else:
            out["other"].append({"title": section["title"], "items": items})
    return out


def _explore_loader():
    try:
        return _explore_from_parsed(call("get_explore"))
    except ExploreError:
        raise
    except Exception as e:
        logger.debug("Explore: get_explore failed, parsing the raw page (%s)", e)
    return _explore_from_raw(raw_browse("FEmusic_explore"))


def explore_feed():
    return cached(("explore",), FEED_TTL, _explore_loader) or {}


def _mood_loader():
    try:
        data = call("get_mood_categories")
    except ExploreError:
        raise
    except Exception as e:
        logger.debug("Explore: get_mood_categories failed (%s)", e)
        data = {}
    categories = []
    for title, items in (data or {}).items():
        moods = [
            {"title": _str(m.get("title")), "params": m.get("params")}
            for m in items or []
            if isinstance(m, dict) and MOOD_PARAMS_RE.match(m.get("params") or "")
            and _str(m.get("title"))
        ]
        if moods:
            categories.append({"title": _str(title), "items": moods})
    if not categories:
        feed = explore_feed()
        if feed.get("moods"):
            categories.append({"title": "Moods & genres", "items": feed["moods"]})
    return categories


def mood_categories():
    return cached(("moods",), FEED_TTL, _mood_loader) or []


def mood_playlists(params):
    if not MOOD_PARAMS_RE.match(params or ""):
        raise ExploreError("Invalid mood.")

    def loader():
        return normalize_items(call("get_mood_playlists", params), limit=40, kinds=("playlist",))

    return cached(("mood", params), FEED_TTL, loader) or []


def _country(country):
    country = (country or "").strip().upper()
    if not country:
        return locale()[1]
    if not COUNTRY_RE.match(country):
        raise ExploreError("Invalid country.")
    return country


def charts(country=None):
    country = _country(country)

    def loader():
        data = call("get_charts", country) or {}
        playlists = normalize_items(
            list(data.get("videos") or []) + list(data.get("genres") or []),
            kinds=("playlist",),
        )
        artists = normalize_items(data.get("artists"), kinds=("artist",))
        for raw, item in zip(data.get("artists") or [], artists):
            if isinstance(raw, dict) and raw.get("rank"):
                item["rank"] = _str(str(raw["rank"]))
        songs = normalize_items(
            data.get("songs", {}).get("items") if isinstance(data.get("songs"), dict) else data.get("songs"),
            limit=50,
            kinds=("song", "video"),
        )
        chart_playlist = playlists[0]["id"] if playlists else ""
        if not songs and chart_playlist:
            try:
                page = playlist(chart_playlist)
                songs = [t for t in page["tracks"] if t["available"]][:50]
            except ExploreError:
                songs = []
        options = (data.get("countries") or {}).get("options") or []
        return {
            "country": country,
            "countries": [c for c in options if isinstance(c, str) and COUNTRY_RE.match(c)],
            "songs": songs,
            "artists": artists,
            "playlists": playlists,
            "chartPlaylist": chart_playlist,
        }

    return cached(("charts", country), FEED_TTL, loader)


def _track(raw, index, album=None):
    video_id = raw.get("videoId") if isinstance(raw.get("videoId"), str) else ""
    artists = _artists(raw)
    track_number = raw.get("trackNumber")
    if not isinstance(track_number, int) or track_number <= 0:
        track_number = index + 1
    return {
        "kind": "song",
        "id": video_id if VIDEO_ID_RE.match(video_id or "") else "",
        "title": _str(raw.get("title")) or f"Track {index + 1}",
        "artists": artists,
        "duration": _duration(raw),
        "explicit": bool(raw.get("isExplicit")),
        "trackNumber": track_number,
        "available": bool(video_id) and raw.get("isAvailable") is not False,
        "album": album or _album_ref(raw),
        "thumbnail": proxied(best_thumbnail_url(raw.get("thumbnails"))),
    }


def _validate(kind, value):
    pattern = {
        "album": ALBUM_ID_RE, "artist": ARTIST_ID_RE,
        "playlist": PLAYLIST_ID_RE, "video": VIDEO_ID_RE,
    }[kind]
    value = (value or "").strip()
    if not pattern.match(value):
        raise ExploreError(f"Invalid {kind} id.")
    return value


def normalize_album(raw, browse_id):
    if not isinstance(raw, dict) or not _str(raw.get("title")):
        return None
    artists = _artists(raw)
    album_ref = {"name": _str(raw.get("title")), "id": browse_id}
    tracks = [
        _track(t, i, album_ref)
        for i, t in enumerate((raw.get("tracks") or [])[:TRACK_LIMIT])
        if isinstance(t, dict)
    ]
    playlist_id = raw.get("audioPlaylistId") or ""
    track_count = raw.get("trackCount")
    return {
        "kind": "album",
        "id": browse_id,
        "title": _str(raw.get("title")),
        "type": _str(raw.get("type")) or "Album",
        "artists": artists,
        "year": _year(raw),
        "explicit": bool(raw.get("isExplicit")) or any(t["explicit"] for t in tracks),
        "thumbnail": proxied(best_thumbnail_url(raw.get("thumbnails"))),
        "coverUrl": cover_url(raw.get("thumbnails")),
        "description": _str(raw.get("description"))[:1200],
        "trackCount": track_count if isinstance(track_count, int) and track_count > 0 else len(tracks),
        "duration": raw.get("duration_seconds") if isinstance(raw.get("duration_seconds"), int) else sum(t["duration"] for t in tracks),
        "playlistId": playlist_id if PLAYLIST_ID_RE.match(playlist_id or "") else "",
        "tracks": tracks,
        "otherVersions": normalize_items(raw.get("other_versions"), kinds=("album",)),
    }


def album(browse_id):
    browse_id = _validate("album", browse_id)
    page = cached(
        ("album", browse_id), PAGE_TTL,
        lambda: normalize_album(call("get_album", browse_id), browse_id),
    )
    if page is None:
        raise ExploreError("This album could not be loaded from YouTube Music.", 502)
    return page


def _section_items(raw, key, kinds=None, limit=SHELF_LIMIT):
    block = raw.get(key)
    if not isinstance(block, dict):
        return []
    return normalize_items(block.get("results"), limit=limit, kinds=kinds)


def normalize_artist(raw, channel_id):
    if not isinstance(raw, dict) or not _str(raw.get("name")):
        return None
    songs = []
    for i, t in enumerate(((raw.get("songs") or {}).get("results") or [])[:10]):
        if isinstance(t, dict):
            songs.append(_track(t, i))
    songs = [s for s in songs if s["id"]]
    songs_playlist = (raw.get("songs") or {}).get("browseId") or ""
    if songs_playlist.startswith("VL"):
        songs_playlist = songs_playlist[2:]
    videos_playlist = (raw.get("videos") or {}).get("browseId") or ""
    if videos_playlist.startswith("VL"):
        videos_playlist = videos_playlist[2:]
    for block in ("albums", "singles"):
        for entry in (raw.get(block) or {}).get("results") or []:
            if isinstance(entry, dict) and "resultType" not in entry:
                entry["resultType"] = "album" if block == "albums" else "single"
    return {
        "kind": "artist",
        "id": channel_id,
        "name": _str(raw.get("name")),
        "description": _str(raw.get("description"))[:1500],
        "subscribers": _str(raw.get("subscribers")),
        "monthlyListeners": _str(raw.get("monthlyListeners")),
        "thumbnail": proxied(best_thumbnail_url(raw.get("thumbnails"))),
        "banner": proxied(cover_url(raw.get("thumbnails"), size=1200)),
        "songs": songs,
        "songsPlaylistId": songs_playlist if PLAYLIST_ID_RE.match(songs_playlist) else "",
        "videosPlaylistId": videos_playlist if PLAYLIST_ID_RE.match(videos_playlist) else "",
        "imageUrl": cover_url(raw.get("thumbnails"), size=1200),
        "albums": _section_items(raw, "albums", kinds=("album",)),
        "singles": _section_items(raw, "singles", kinds=("album",)),
        "videos": _section_items(raw, "videos", kinds=("video", "song")),
        "related": _section_items(raw, "related", kinds=("artist",)),
    }


def artist(channel_id):
    channel_id = _validate("artist", channel_id)
    page = cached(
        ("artist", channel_id), PAGE_TTL,
        lambda: normalize_artist(call("get_artist", channel_id), channel_id),
    )
    if page is None:
        raise ExploreError("This artist could not be loaded from YouTube Music.", 502)
    return page


def normalize_playlist(raw, playlist_id):
    if not isinstance(raw, dict):
        return None
    tracks = [
        _track(t, i)
        for i, t in enumerate((raw.get("tracks") or [])[:TRACK_LIMIT])
        if isinstance(t, dict)
    ]
    for i, t in enumerate(tracks):
        t["trackNumber"] = i + 1
    author = _artists({"author": raw.get("author")})
    count = raw.get("trackCount")
    return {
        "kind": "playlist",
        "id": playlist_id,
        "title": _str(raw.get("title")) or "Playlist",
        "author": author[0]["name"] if author else "",
        "description": _str(raw.get("description"))[:1200],
        "year": _year(raw),
        "thumbnail": proxied(best_thumbnail_url(raw.get("thumbnails"))),
        "coverUrl": cover_url(raw.get("thumbnails")),
        "trackCount": count if isinstance(count, int) and count > 0 else len(tracks),
        "duration": raw.get("duration_seconds") if isinstance(raw.get("duration_seconds"), int) else sum(t["duration"] for t in tracks),
        "tracks": tracks,
    }


def playlist(playlist_id):
    playlist_id = _validate("playlist", playlist_id)
    if playlist_id.startswith("VL"):
        playlist_id = playlist_id[2:]
    page = cached(
        ("playlist", playlist_id), PAGE_TTL,
        lambda: normalize_playlist(call("get_playlist", playlist_id, limit=TRACK_LIMIT), playlist_id),
    )
    if page is None:
        raise ExploreError("This playlist could not be loaded from YouTube Music.", 502)
    return page


def _query(q):
    q = " ".join(str(q or "").split())[:QUERY_MAX_LENGTH]
    if len(q) < 2:
        raise ExploreError("Type at least 2 characters to search.")
    return q


def search(q):
    q = _query(q)

    def loader():
        raws = list(call("search", q, limit=20) or [])
        for flt in ("albums", "artists", "songs"):
            try:
                raws.extend(call("search", q, filter=flt, limit=12) or [])
            except ExploreError:
                raise
            except Exception as e:
                logger.debug("Explore: %s search failed (%s)", flt, e)
        items = normalize_items(raws, limit=200)
        groups = {"artists": [], "albums": [], "songs": [], "videos": [], "playlists": []}
        plural = {"artist": "artists", "album": "albums", "song": "songs", "video": "videos", "playlist": "playlists"}
        for item in items:
            bucket = groups[plural[item["kind"]]]
            if len(bucket) < 20:
                bucket.append(item)
        top = items[0] if items else None
        return {"query": q, "top": top, "results": groups}

    result = cached(("search", q.casefold()), FEED_TTL, loader)
    if result is None:
        raise ExploreError("YouTube Music search is not responding.", 502)
    return result


def suggestions(q):
    q = _query(q)

    def loader():
        out = []
        for s in call("get_search_suggestions", q) or []:
            text = s.get("text") if isinstance(s, dict) else s
            if isinstance(text, str) and text.strip() and text.strip() not in out:
                out.append(text.strip())
        return out[:8]

    return cached(("suggest", q.casefold()), SUGGEST_TTL, loader) or []


def home(country=None):
    country = _country(country)
    feed = explore_feed()
    chart = charts(country) or {}
    sections = []

    def add(key, title, items, layout="cards", **extra):
        if items:
            sections.append({"key": key, "title": title, "layout": layout, "items": items, **extra})

    new_releases = feed.get("new_releases") or []
    add("hero", "New releases", new_releases[:6], layout="hero")
    add("top_songs", "Top songs", (chart.get("songs") or feed.get("top_songs") or [])[:20], layout="tracks", country=country, countries=chart.get("countries") or [])
    add("trending", "Trending", feed.get("trending") or [], layout="tracks", playlistId=feed.get("trending_playlist") or "")
    add("new_albums", "New albums", new_releases, layout="cards")
    add("top_artists", "Top artists", chart.get("artists") or [], layout="artists")
    add("charts", "Charts", chart.get("playlists") or [], layout="cards")
    add("new_videos", "New music videos", feed.get("new_videos") or [], layout="cards")
    for i, shelf in enumerate(feed.get("other") or []):
        add(f"explore_{i}", shelf["title"] or "More", shelf["items"])
    for i, shelf in enumerate(home_shelves()):
        add(f"home_{i}", shelf["title"] or "For you", shelf["items"])
    moods = mood_categories()
    return {"country": country, "sections": sections, "moods": moods}


_VARIOUS_RE = re.compile(r"^(?:" + "|".join(re.escape(v) for v in VARIOUS_ARTISTS) + r")$")


def _edition_free(title):
    def keep(match):
        inner = _fold(match.group(1))
        if any(w in inner for w in _EDITION_WORDS) or re.search(r"\b(?:19|20)\d{2}\b", inner):
            return " "
        return match.group(0)

    title = _BRACKET_RE.sub(keep, title or "")
    title = _DASH_EDITION_RE.sub("", title)
    return title


def norm_title(title):
    return _coverage_text(_edition_free(title)).replace("&", "and")


def norm_artist(name):
    text = _coverage_text(name or "").replace("&", "and")
    if text.startswith("the "):
        text = text[4:]
    return text


def is_various(name):
    return bool(_VARIOUS_RE.match(_fold(name or "").strip()))


def _ratio(a, b):
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    score = SequenceMatcher(None, a, b).ratio()
    if len(a) >= 4 and len(b) >= 4 and (a in b or b in a):
        score = max(score, 0.9)
    return score


def title_similarity(a, b):
    exact = _coverage_text(a) == _coverage_text(b)
    base = _ratio(norm_title(a), norm_title(b))
    return min(1.0, base + (0.02 if exact else 0.0)), exact


def artist_similarity(yt_artists, candidate_artist):
    names = [a for a in yt_artists if a]
    if not names or all(is_various(n) for n in names):
        return 1.0 if is_various(candidate_artist) else (0.7 if not names else 0.0)
    if is_various(candidate_artist):
        return 0.65 if len(names) >= 3 else 0.0
    cand = norm_artist(candidate_artist)
    joined = norm_artist(" ".join(names))
    best = max([_ratio(norm_artist(n), cand) for n in names] + [_ratio(joined, cand)])
    return best


def score_album_candidate(ytm, candidate):
    title_score, exact = title_similarity(ytm["title"], candidate.get("title") or "")
    artist_score = artist_similarity(
        [a["name"] for a in ytm.get("artists") or []], candidate.get("artistName") or "",
    )
    if title_score < MATCH_MIN_TITLE or artist_score < MATCH_MIN_ARTIST:
        return 0.0
    score = 0.6 * title_score + 0.3 * artist_score + 0.1
    try:
        delta = abs(int(ytm.get("year") or 0) - int(candidate.get("year") or 0))
    except ValueError:
        delta = 99
    if ytm.get("year") and candidate.get("year"):
        score += 0.06 if delta == 0 else 0.03 if delta <= 1 else 0.0
    yt_count = ytm.get("trackCount") or 0
    cand_count = candidate.get("trackCount") or 0
    if yt_count and cand_count:
        diff = abs(yt_count - cand_count)
        score += 0.06 if diff == 0 else 0.03 if diff <= 2 else -0.03 if diff > 6 else 0.0
    yt_type = (ytm.get("type") or "").lower()
    cand_type = (candidate.get("type") or "").lower()
    if yt_type and cand_type:
        if yt_type == cand_type:
            score += 0.03
        elif {yt_type, cand_type} & {"single", "ep"} and "album" in {yt_type, cand_type}:
            score -= 0.05
    if exact:
        score += 0.02
    if candidate.get("inLibrary"):
        score += 0.015
    return round(score, 4)


def album_status(candidate):
    if not candidate.get("inLibrary"):
        return STATUS_NOT_IN_LIBRARY
    if not candidate.get("monitored"):
        return STATUS_UNMONITORED
    missing = candidate.get("missingTracks")
    if missing is None or missing > 0:
        return STATUS_MISSING
    return STATUS_COMPLETE


def rank_album_candidates(ytm, candidates):
    scored = []
    for c in candidates or []:
        score = score_album_candidate(ytm, c)
        if score > 0:
            scored.append(dict(c, score=score))
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored


def decide_album(ytm, candidates):
    ranked = rank_album_candidates(ytm, candidates)
    if not ranked:
        return {"status": STATUS_NOT_ON_MB, "match": None, "candidates": []}
    best = ranked[0]
    runner = ranked[1]["score"] if len(ranked) > 1 else 0.0
    if best["score"] >= MATCH_ACCEPT and best["score"] - runner >= MATCH_MARGIN:
        return {"status": album_status(best), "match": best, "candidates": ranked[:5]}
    return {"status": STATUS_AMBIGUOUS, "match": None, "candidates": ranked[:5]}


def _album_lookup_terms(ytm):
    names = [a["name"] for a in ytm.get("artists") or [] if not is_various(a["name"])]
    clean = " ".join(_edition_free(ytm["title"]).split())
    terms = []
    if names:
        terms.append(f"{names[0]} {clean}")
    terms.append(clean)
    return [t for i, t in enumerate(terms) if t and t not in terms[:i]]


def match_album(browse_id, refresh=False):
    browse_id = _validate("album", browse_id)
    key = ("match", "album", browse_id, locale())
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    ytm = album(browse_id)
    candidates = []
    seen = set()
    decision = None
    for term in _album_lookup_terms(ytm):
        try:
            found = library.search("album", term)
        except library.LibraryError as e:
            raise ExploreError(e.message, e.status)
        for c in found:
            if c["foreignAlbumId"] not in seen:
                seen.add(c["foreignAlbumId"])
                candidates.append(c)
        decision = decide_album(ytm, candidates)
        if decision["match"] is not None:
            break
    result = {
        "kind": "album",
        "id": browse_id,
        "status": decision["status"],
        "match": decision["match"],
        "candidates": decision["candidates"],
        "playlistId": ytm.get("playlistId") or "",
    }
    cache.set(key, result, MATCH_TTL)
    return result


def decide_artist(name, candidates):
    target = norm_artist(name)
    scored = []
    for c in candidates or []:
        score = _ratio(target, norm_artist(c.get("name") or ""))
        if score >= 0.75:
            scored.append(dict(c, score=round(score, 4)))
    scored.sort(key=lambda c: (c["score"], c.get("inLibrary", False)), reverse=True)
    if not scored:
        return {"status": STATUS_NOT_ON_MB, "match": None, "candidates": []}
    exact = [c for c in scored if c["score"] >= ARTIST_ACCEPT]
    in_library = [c for c in exact if c.get("inLibrary")]
    chosen = None
    if len(exact) == 1:
        chosen = exact[0]
    elif len(in_library) == 1:
        chosen = in_library[0]
    if chosen is None:
        return {"status": STATUS_AMBIGUOUS, "match": None, "candidates": scored[:6]}
    status = "in_library" if chosen.get("inLibrary") else STATUS_NOT_IN_LIBRARY
    return {"status": status, "match": chosen, "candidates": scored[:6]}


def match_artist(channel_id, refresh=False):
    channel_id = _validate("artist", channel_id)
    key = ("match", "artist", channel_id, locale())
    if not refresh:
        hit = cache.get(key)
        if hit is not None:
            return hit
    page = artist(channel_id)
    try:
        found = library.search("artist", page["name"])
    except library.LibraryError as e:
        raise ExploreError(e.message, e.status)
    decision = decide_artist(page["name"], found)
    result = {"kind": "artist", "id": channel_id, **decision}
    cache.set(key, result, MATCH_TTL)
    return result


def match(kind, item_id, refresh=False):
    if kind == "album":
        return match_album(item_id, refresh=refresh)
    if kind == "artist":
        return match_artist(item_id, refresh=refresh)
    raise ExploreError("kind must be 'album' or 'artist'")


def _chosen_mbid(payload, result, field):
    chosen = payload.get(field)
    if chosen:
        mbid = str(chosen).strip().lower()
        if not library.MBID_RE.match(mbid):
            raise ExploreError(f"{field} must be a MusicBrainz id.")
        return mbid
    if result["match"] is None:
        if result["status"] == STATUS_AMBIGUOUS:
            raise ExploreError("Several MusicBrainz releases match; pick one.", 409)
        raise ExploreError("This release is not on MusicBrainz; import it from YouTube instead.", 409)
    return result["match"]["foreignAlbumId" if field == "foreignAlbumId" else "foreignArtistId"]


def add_album(payload):
    browse_id = _validate("album", payload.get("id"))
    result = match_album(browse_id)
    mbid = _chosen_mbid(payload, result, "foreignAlbumId")
    download = payload.get("download", True) is not False
    body = {
        k: payload[k]
        for k in ("rootFolderPath", "qualityProfileId", "metadataProfileId")
        if k in payload
    }
    body.update({"foreignAlbumId": mbid, "download": False})
    try:
        added = library.add_album(body)
    except library.LibraryError as e:
        raise ExploreError(e.message, e.status)
    album_id = added["id"]
    hinted = False
    playlist_id = result.get("playlistId") or ""
    if playlist_id:
        hinted = models.set_album_source_hint(album_id, playlist_id, browse_id)
    if download:
        added["queued"] = library.queue_when_ready(
            album_id, added.get("title") or "", added.get("artistName") or "",
        )
    cache.invalidate("match")
    logger.info(
        "Explore: %s — %s %s%s",
        added.get("artistName") or "", added.get("title") or "", added.get("status"),
        " (YT Music album remembered)" if hinted else "",
    )
    return {**added, "sourceHint": hinted}


def add_artist(payload):
    channel_id = _validate("artist", payload.get("id"))
    result = match_artist(channel_id)
    mbid = payload.get("foreignArtistId")
    if mbid:
        mbid = str(mbid).strip().lower()
        if not library.MBID_RE.match(mbid):
            raise ExploreError("foreignArtistId must be a MusicBrainz id.")
    elif result["match"] is not None:
        mbid = result["match"]["foreignArtistId"]
    elif result["status"] == STATUS_AMBIGUOUS:
        raise ExploreError("Several MusicBrainz artists match; pick one.", 409)
    else:
        raise ExploreError("This artist is not on MusicBrainz.", 409)
    body = {
        k: payload[k]
        for k in (
            "rootFolderPath", "qualityProfileId", "metadataProfileId",
            "monitor", "monitorNewItems",
        )
        if k in payload
    }
    body["foreignArtistId"] = mbid
    try:
        added = library.add_artist(body)
    except library.LibraryError as e:
        raise ExploreError(e.message, e.status)
    cache.invalidate("match")
    return added


def add(payload):
    kind = payload.get("kind")
    if kind == "album":
        return add_album(payload)
    if kind == "artist":
        return add_artist(payload)
    raise ExploreError("kind must be 'album' or 'artist'")


def import_plan(payload):
    kind = payload.get("kind")
    selected = payload.get("videoIds")
    if selected is not None:
        if not isinstance(selected, list) or not all(
            isinstance(v, str) and VIDEO_ID_RE.match(v) for v in selected
        ):
            raise ExploreError("videoIds must be a list of video ids.")
        selected = set(selected)
    if kind == "album":
        page = album(payload.get("id"))
        artist_name = ", ".join(a["name"] for a in page["artists"][:2]) or "Unknown artist"
        title = page["title"]
        source = f"https://music.youtube.com/playlist?list={page['playlistId']}" if page["playlistId"] else ""
    elif kind == "playlist":
        page = playlist(payload.get("id"))
        artist_name = page["author"] or "YouTube Music"
        title = page["title"]
        source = f"https://music.youtube.com/playlist?list={page['id']}"
    else:
        raise ExploreError("kind must be 'album' or 'playlist'")
    entries = [
        {"url": f"https://music.youtube.com/watch?v={t['id']}", "title": t["title"]}
        for t in page["tracks"]
        if t["available"] and t["id"] and (selected is None or t["id"] in selected)
    ]
    if not entries:
        raise ExploreError("No playable tracks to import.")
    return {
        "artist_name": artist_name[:200],
        "album_title": title[:200],
        "entries": entries,
        "thumbnail_url": page.get("coverUrl") or "",
        "source_url": source,
        "year": page.get("year") or "",
    }


_VIDEO_NOISE_WORDS = (
    "official", "officiel", "officielle", "ufficiale", "oficial", "offiziell",
    "clip", "video", "vidéo", "audio", "lyric", "lyrics", "visualizer",
    "visualiser", "directed", "realise", "réalisé", "prod", "hd", "4k", "mv",
    "music video", "live session", "paroles", "testo", "letra",
)
_VIDEO_BRACKET_RE = re.compile(r"[\(\[\{]([^\)\]\}]*)[\)\]\}]")
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")
_SEPARATORS = ("-", "–", "—", ":", "|", "_")


def clean_video_title(title, artist_name=""):
    text = _INVISIBLE_RE.sub("", title or "").strip()

    def drop(match):
        inner = _fold(match.group(1))
        if any(re.search(r"(?:^|\W)" + re.escape(w) + r"(?:$|\W)", inner) for w in _VIDEO_NOISE_WORDS) or "@" in inner:
            return " "
        return match.group(0)

    text = _VIDEO_BRACKET_RE.sub(drop, text)
    artist_fold = _fold(artist_name or "").strip()
    if artist_fold:
        folded = _fold(text)
        if folded.startswith(artist_fold):
            rest = text[len(artist_fold):].lstrip()
            if rest[:1] in _SEPARATORS:
                text = rest[1:]
            elif rest[:1] in ("x", "X", "&", ",") or _fold(rest).startswith(("feat", "ft.")):
                head, sep, tail = rest.partition(" - ")
                guests = re.sub(r"^(?:x|&|,|feat\.?|ft\.?)\s*", "", head.strip(), flags=re.I)
                if sep and guests:
                    text = f"{tail.strip()} (feat. {guests.strip()})"
                elif sep:
                    text = tail
    text = " ".join(text.split()).strip(" -–—|:_")
    return text or (title or "").strip()


def _unproxy(path):
    if not path or not path.startswith("/api/thumbnail?url="):
        return ""
    return urllib.parse.unquote(path[len("/api/thumbnail?url="):])


def _track_releases(page):
    tracks = []
    for pid in (page.get("videosPlaylistId"), page.get("songsPlaylistId")):
        if not pid:
            continue
        try:
            tracks = [t for t in playlist(pid)["tracks"] if t["available"] and t["id"]]
        except ExploreError:
            tracks = []
        if tracks:
            break
    if not tracks:
        tracks = [t for t in (page.get("songs") or []) + (page.get("videos") or []) if t.get("id")]
    releases = []
    seen_ids = set()
    seen_titles = set()
    for t in tracks:
        title = clean_video_title(t["title"], page["name"])
        key = _fold(title)
        if t["id"] in seen_ids or key in seen_titles:
            continue
        seen_ids.add(t["id"])
        seen_titles.add(key)
        releases.append({
            "kind": "track",
            "videoId": t["id"],
            "title": title[:200],
            "year": "",
            "type": "Single",
            "thumbnail_url": _unproxy(t.get("thumbnail") or ""),
        })
    return releases


def artist_import_plan(channel_id, include_singles=True, already_imported=None):
    page = artist(channel_id)
    already = {_fold(t) for t in (already_imported or ())}
    releases = []
    seen = set()
    blocks = page["albums"] + (page["singles"] if include_singles else [])
    for item in blocks:
        if item["id"] in seen:
            continue
        seen.add(item["id"])
        releases.append({
            "kind": "album",
            "id": item["id"],
            "title": item["title"][:200],
            "year": item.get("year") or "",
            "type": item.get("type") or "Album",
            "thumbnail_url": _unproxy(item.get("thumbnail") or ""),
        })
    mode = "releases"
    if not releases:
        releases = _track_releases(page)
        mode = "tracks"
    todo = [r for r in releases if _fold(r["title"]) not in already]
    skipped = [r["title"] for r in releases if _fold(r["title"]) in already]
    return {
        "artist_id": channel_id,
        "artist_name": page["name"][:200],
        "artist_image": page.get("imageUrl") or "",
        "mode": mode,
        "releases": todo,
        "skipped": skipped,
    }


def release_import(release, artist_name):
    if release["kind"] == "album":
        plan = import_plan({"kind": "album", "id": release["id"]})
        plan["artist_name"] = artist_name
        plan["year"] = plan.get("year") or release.get("year") or ""
        return plan
    url = f"https://music.youtube.com/watch?v={release['videoId']}"
    return {
        "artist_name": artist_name,
        "album_title": release["title"],
        "entries": [{"url": url, "title": release["title"]}],
        "thumbnail_url": release.get("thumbnail_url") or "",
        "source_url": url,
        "year": release.get("year") or "",
    }
