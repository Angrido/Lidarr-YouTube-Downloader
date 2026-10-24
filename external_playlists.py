import html
import json
import logging
import re
import threading
import time
import urllib.parse

import requests

from config import load_config
from version import USER_AGENT

logger = logging.getLogger(__name__)

MAX_TRACKS = 500
TIMEOUT = 15
SERVICE_LABELS = {
    "spotify": "Spotify",
    "deezer": "Deezer",
    "apple": "Apple Music",
    "listenbrainz": "ListenBrainz",
}
KIND_TYPES = {"playlist": "playlist", "album": "album", "track": "video"}

_SPOTIFY_RE = re.compile(
    r"^https?://open\.spotify\.com/(?:intl-[a-z]{2}(?:-[a-z]{2})?/)?(?:embed/)?"
    r"(playlist|album|track)/([A-Za-z0-9]{22})(?:[/?#].*)?$",
)
_SPOTIFY_URI_RE = re.compile(r"^spotify:(playlist|album|track):([A-Za-z0-9]{22})$")
_DEEZER_RE = re.compile(
    r"^https?://(?:www\.)?deezer\.com/(?:[a-z]{2}(?:-[a-z]{2})?/)?"
    r"(playlist|album|track)/(\d{1,20})(?:[/?#].*)?$",
)
_APPLE_RE = re.compile(
    r"^https?://(?:embed\.)?music\.apple\.com/([a-z]{2})/(playlist|album|song)/"
    r"(?:[^/?#]*/)?(pl\.[A-Za-z0-9.-]{8,64}|\d{1,20})(?:[/?#].*)?$",
)
_LISTENBRAINZ_RE = re.compile(
    r"^https?://(?:www\.|beta\.)?listenbrainz\.org/playlist/"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})/?(?:[?#].*)?$",
)
_SHORT_LINK_HOSTS = frozenset({
    "link.deezer.com", "deezer.page.link", "spotify.link", "spoti.fi",
})
_COVER_HOST_SUFFIXES = (
    ".scdn.co", ".spotifycdn.com", ".dzcdn.net", ".mzstatic.com",
    "coverartarchive.org", ".archive.org",
)
_NEXT_DATA_RE = re.compile(
    r'<script[^>]+id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.S,
)
_APPLE_DATA_RE = re.compile(
    r'<script[^>]+id="serialized-server-data"[^>]*>(.*?)</script>', re.S,
)
_OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"')
_OG_IMAGE_RE = re.compile(r'<meta[^>]+property="og:image"[^>]+content="([^"]*)"')

_spotify_token = {"value": "", "expires": 0.0, "key": ""}
_spotify_lock = threading.Lock()


class ExternalPlaylistError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _host(url):
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except ValueError:
        return ""


def is_cover_url(url):
    if not isinstance(url, str) or not url.startswith("https://"):
        return False
    host = _host(url)
    return any(
        host.endswith(s) or host == s.lstrip(".") for s in _COVER_HOST_SUFFIXES
    )


def _match(url):
    url = (url or "").strip()
    m = _SPOTIFY_RE.match(url) or _SPOTIFY_URI_RE.match(url)
    if m:
        return "spotify", m.group(1), m.group(2), ""
    m = _DEEZER_RE.match(url)
    if m:
        return "deezer", m.group(1), m.group(2), ""
    m = _APPLE_RE.match(url)
    if m:
        kind = {"song": "track"}.get(m.group(2), m.group(2))
        song = urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("i")
        if kind == "album" and song and song[0].isdigit():
            return "apple", "track", song[0], m.group(1)
        return "apple", kind, m.group(3), m.group(1)
    m = _LISTENBRAINZ_RE.match(url)
    if m:
        return "listenbrainz", "playlist", m.group(1), ""
    return None


def is_external_url(url):
    url = (url or "").strip()
    return bool(_match(url)) or _host(url) in _SHORT_LINK_HOSTS


def _get(url, **kwargs):
    headers = {"User-Agent": USER_AGENT, "Accept-Language": "en"}
    headers.update(kwargs.pop("headers", {}))
    try:
        return requests.get(url, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as e:
        raise ExternalPlaylistError(
            f"Could not reach {_host(url)}: {type(e).__name__}", 502,
        ) from None


def _json(url, **kwargs):
    resp = _get(url, **kwargs)
    if resp.status_code == 404:
        raise ExternalPlaylistError("Not found. Is the playlist public?", 404)
    if resp.status_code != 200:
        raise ExternalPlaylistError(
            f"{_host(url)} answered HTTP {resp.status_code}", 502,
        )
    try:
        return resp.json()
    except ValueError:
        raise ExternalPlaylistError(f"{_host(url)} sent an unreadable answer", 502) from None


def _resolve_short_link(url):
    try:
        resp = requests.head(
            url, allow_redirects=True, timeout=TIMEOUT,
            headers={"User-Agent": USER_AGENT},
        )
    except requests.RequestException:
        raise ExternalPlaylistError("Could not open the short link", 502) from None
    final = resp.url or ""
    if not _match(final):
        raise ExternalPlaylistError("The short link does not lead to a playlist")
    return final


def _track(title, artist, album="", duration=0, thumbnail=""):
    title = " ".join(str(title or "").split())[:300]
    artist = " ".join(str(artist or "").split())[:300]
    if not title or not artist:
        return None
    try:
        duration = max(0, int(round(float(duration or 0))))
    except (TypeError, ValueError):
        duration = 0
    return {
        "title": title,
        "artist": artist,
        "album": " ".join(str(album or "").split())[:300],
        "duration": duration,
        "thumbnail": thumbnail if is_cover_url(thumbnail) else "",
    }


def _result(service, kind, title, owner, thumbnail, tracks):
    tracks = [t for t in tracks if t][:MAX_TRACKS]
    if not tracks:
        raise ExternalPlaylistError("No tracks found at that link.", 404)
    return {
        "service": service,
        "kind": kind,
        "title": " ".join(str(title or "").split())[:300] or SERVICE_LABELS[service],
        "owner": " ".join(str(owner or "").split())[:300],
        "thumbnail": thumbnail if is_cover_url(thumbnail) else "",
        "tracks": tracks,
    }


def _largest_image(images):
    best = ""
    best_size = -1
    for img in images or []:
        if not isinstance(img, dict) or not img.get("url"):
            continue
        size = img.get("width") or img.get("maxWidth") or 0
        try:
            size = int(size)
        except (TypeError, ValueError):
            size = 0
        if size > best_size:
            best, best_size = img["url"], size
    return best


def _spotify_api_token(cfg):
    client_id = (cfg.get("spotify_client_id") or "").strip()
    secret = (cfg.get("spotify_client_secret") or "").strip()
    if not client_id or not secret:
        return ""
    key = client_id + ":" + secret
    with _spotify_lock:
        if _spotify_token["key"] == key and _spotify_token["expires"] > time.time() + 30:
            return _spotify_token["value"]
        try:
            resp = requests.post(
                "https://accounts.spotify.com/api/token",
                data={"grant_type": "client_credentials"},
                auth=(client_id, secret), timeout=TIMEOUT,
            )
            data = resp.json() if resp.status_code == 200 else {}
        except (requests.RequestException, ValueError):
            data = {}
        token = data.get("access_token") or ""
        if not token:
            logger.warning("Spotify login with the client ID failed; using the public page")
            return ""
        _spotify_token.update(
            value=token, key=key,
            expires=time.time() + float(data.get("expires_in") or 3600),
        )
        return token


def _spotify_paged(first_url, token, pick):
    out = []
    url = first_url
    while url and len(out) < MAX_TRACKS:
        if not url.startswith("https://api.spotify.com/"):
            break
        page = _json(url, headers={"Authorization": f"Bearer {token}"})
        for item in page.get("items") or []:
            track = pick(item)
            if track:
                out.append(track)
        url = page.get("next")
    return out


def _spotify_api(kind, item_id, token):
    auth = {"Authorization": f"Bearer {token}"}
    base = "https://api.spotify.com/v1"

    def artists_of(obj):
        return ", ".join(
            a.get("name") for a in obj.get("artists") or [] if a.get("name")
        )

    if kind == "track":
        t = _json(f"{base}/tracks/{item_id}", headers=auth)
        album = t.get("album") or {}
        cover = _largest_image(album.get("images"))
        return _result(
            "spotify", kind, t.get("name"), artists_of(t), cover,
            [_track(t.get("name"), artists_of(t), album.get("name"),
                    (t.get("duration_ms") or 0) / 1000, cover)],
        )
    if kind == "album":
        a = _json(f"{base}/albums/{item_id}", headers=auth)
        cover = _largest_image(a.get("images"))
        tracks = _spotify_paged(
            f"{base}/albums/{item_id}/tracks?limit=50", token,
            lambda t: _track(t.get("name"), artists_of(t), a.get("name"),
                             (t.get("duration_ms") or 0) / 1000, cover),
        )
        return _result("spotify", kind, a.get("name"), artists_of(a), cover, tracks)
    p = _json(
        f"{base}/playlists/{item_id}?fields=name,owner(display_name),images",
        headers=auth,
    )

    def pick(item):
        t = item.get("track") or {}
        if t.get("type", "track") != "track" or t.get("is_local"):
            return None
        album = t.get("album") or {}
        return _track(
            t.get("name"), artists_of(t), album.get("name"),
            (t.get("duration_ms") or 0) / 1000,
            _largest_image(album.get("images")),
        )

    tracks = _spotify_paged(
        f"{base}/playlists/{item_id}/tracks?limit=100", token, pick,
    )
    return _result(
        "spotify", kind, p.get("name"), (p.get("owner") or {}).get("display_name"),
        _largest_image(p.get("images")), tracks,
    )


def _find_entity(node, depth=0):
    if depth > 12:
        return None
    if isinstance(node, dict):
        if "trackList" in node or (
            node.get("type") == "track" and node.get("name") and node.get("artists")
        ):
            return node
        for value in node.values():
            found = _find_entity(value, depth + 1)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_entity(value, depth + 1)
            if found is not None:
                return found
    return None


def parse_spotify_embed(page_html, kind):
    m = _NEXT_DATA_RE.search(page_html or "")
    if not m:
        raise ExternalPlaylistError("Spotify changed its page; add a Spotify app key in Settings", 502)
    try:
        data = json.loads(m.group(1))
    except ValueError:
        raise ExternalPlaylistError("Spotify sent an unreadable page", 502) from None
    entity = _find_entity(data)
    if not entity:
        raise ExternalPlaylistError("No tracks found at that link.", 404)
    cover = _largest_image(
        (entity.get("coverArt") or {}).get("sources")
        or (entity.get("visualIdentity") or {}).get("image")
    )
    title = entity.get("name") or entity.get("title") or ""
    owner = entity.get("subtitle") or ""
    if "trackList" not in entity:
        artist = ", ".join(
            a.get("name") for a in entity.get("artists") or [] if a.get("name")
        )
        return _result(
            "spotify", kind, title, artist, cover,
            [_track(title, artist, "", (entity.get("duration") or 0) / 1000, cover)],
        )
    album_title = title if kind == "album" else ""
    tracks = [
        _track(
            t.get("title"), t.get("subtitle"), album_title,
            (t.get("duration") or 0) / 1000,
        )
        for t in entity.get("trackList") or []
        if isinstance(t, dict)
    ]
    return _result("spotify", kind, title, owner, cover, tracks)


def _spotify(kind, item_id, cfg):
    token = _spotify_api_token(cfg)
    if token:
        try:
            return _spotify_api(kind, item_id, token)
        except ExternalPlaylistError as e:
            logger.info("Spotify API refused %s %s (%s); trying the public page", kind, item_id, e.message)
    resp = _get(f"https://open.spotify.com/embed/{kind}/{item_id}")
    if resp.status_code == 404:
        raise ExternalPlaylistError("Not found. Is the playlist public?", 404)
    if resp.status_code != 200:
        raise ExternalPlaylistError(f"Spotify answered HTTP {resp.status_code}", 502)
    return parse_spotify_embed(resp.text, kind)


def _deezer_error(data):
    if isinstance(data, dict) and data.get("error"):
        raise ExternalPlaylistError("Deezer: not found or not public.", 404)
    return data


def _deezer_track(t, album_title="", cover=""):
    album = t.get("album") or {}
    return _track(
        t.get("title"), (t.get("artist") or {}).get("name"),
        album.get("title") or album_title, t.get("duration"),
        album.get("cover_xl") or album.get("cover_big") or cover,
    )


def _deezer(kind, item_id):
    base = "https://api.deezer.com"
    data = _deezer_error(_json(f"{base}/{kind}/{item_id}"))
    if kind == "track":
        cover = (data.get("album") or {}).get("cover_xl") or ""
        return _result(
            "deezer", kind, data.get("title"), (data.get("artist") or {}).get("name"),
            cover, [_deezer_track(data, cover=cover)],
        )
    cover = data.get("cover_xl") or data.get("picture_xl") or ""
    owner = (
        (data.get("artist") or {}).get("name") if kind == "album"
        else (data.get("creator") or {}).get("name")
    )
    album_title = data.get("title") if kind == "album" else ""
    tracks = []
    url = f"{base}/{kind}/{item_id}/tracks?limit=100"
    while url and len(tracks) < MAX_TRACKS:
        if not url.startswith(base + "/"):
            break
        page = _deezer_error(_json(url))
        for t in page.get("data") or []:
            if isinstance(t, dict) and t.get("readable", True) is not False:
                tracks.append(_deezer_track(t, album_title, cover))
        url = page.get("next")
    return _result("deezer", kind, data.get("title"), owner, cover, tracks)


def _apple_art(url, size=600):
    url = str(url or "")
    url = url.replace("{w}", str(size)).replace("{h}", str(size)).replace("{f}", "jpg")
    return re.sub(r"/\d+x\d+(bb)?\.(jpg|png|webp)$", f"/{size}x{size}bb.jpg", url)


def _apple_lookup(kind, item_id, country):
    data = _json(
        "https://itunes.apple.com/lookup",
        params={"id": item_id, "entity": "song", "country": country or "us", "limit": 200},
    )
    results = [r for r in data.get("results") or [] if isinstance(r, dict)]
    songs = [r for r in results if r.get("wrapperType") == "track"]
    if not songs:
        raise ExternalPlaylistError("Apple Music: not found in this country.", 404)
    songs.sort(key=lambda r: (r.get("discNumber") or 0, r.get("trackNumber") or 0))
    collection = next(
        (r for r in results if r.get("wrapperType") == "collection"), songs[0],
    )
    cover = _apple_art(collection.get("artworkUrl100"))
    tracks = [
        _track(
            s.get("trackName"), s.get("artistName"), s.get("collectionName"),
            (s.get("trackTimeMillis") or 0) / 1000, cover,
        )
        for s in songs
    ]
    if kind == "track":
        song = next((s for s in songs if str(s.get("trackId")) == str(item_id)), songs[0])
        return _result(
            "apple", kind, song.get("trackName"), song.get("artistName"), cover,
            [_track(song.get("trackName"), song.get("artistName"),
                    song.get("collectionName"),
                    (song.get("trackTimeMillis") or 0) / 1000, cover)],
        )
    return _result(
        "apple", kind, collection.get("collectionName"),
        collection.get("artistName"), cover, tracks,
    )


def _collect_apple_songs(node, out, depth=0):
    if depth > 30 or len(out) >= MAX_TRACKS:
        return
    if isinstance(node, dict):
        title = node.get("title") or node.get("name")
        artist = node.get("artistName")
        if isinstance(title, str) and isinstance(artist, str) and (
            node.get("duration") or node.get("durationInMillis")
        ):
            out.append(node)
            return
        for value in node.values():
            _collect_apple_songs(value, out, depth + 1)
    elif isinstance(node, list):
        for value in node:
            _collect_apple_songs(value, out, depth + 1)


def parse_apple_playlist(page_html):
    m = _APPLE_DATA_RE.search(page_html or "")
    songs = []
    if m:
        try:
            _collect_apple_songs(json.loads(m.group(1)), songs)
        except ValueError:
            songs = []
    seen = set()
    tracks = []
    for s in songs:
        key = ((s.get("title") or s.get("name")).casefold(), s["artistName"].casefold())
        if key in seen:
            continue
        seen.add(key)
        art = (s.get("artwork") or {}).get("dictionary") or s.get("artwork") or {}
        duration = s.get("duration") or s.get("durationInMillis") or 0
        tracks.append(_track(
            s.get("title") or s.get("name"), s["artistName"],
            s.get("albumName") or "", duration / 1000,
            _apple_art(art.get("url") if isinstance(art, dict) else ""),
        ))
    title = _OG_TITLE_RE.search(page_html or "")
    image = _OG_IMAGE_RE.search(page_html or "")
    name = html.unescape(title.group(1)) if title else ""
    name = re.sub(r"\s+(?:on|su|auf|sur|en)\s+Apple\s+Music$", "", name)
    return _result(
        "apple", "playlist", name, "", html.unescape(image.group(1)) if image else "",
        tracks,
    )


def _apple(kind, item_id, country):
    if kind != "playlist":
        return _apple_lookup(kind, item_id, country)
    resp = _get(f"https://music.apple.com/{country or 'us'}/playlist/{item_id}")
    if resp.status_code != 200:
        raise ExternalPlaylistError(f"Apple Music answered HTTP {resp.status_code}", 502)
    return parse_apple_playlist(resp.text)


def _listenbrainz(item_id):
    data = _json(f"https://api.listenbrainz.org/1/playlist/{item_id}")
    playlist = data.get("playlist") or {}
    tracks = [
        _track(
            t.get("title"), t.get("creator"), t.get("album"),
            (t.get("duration") or 0) / 1000,
        )
        for t in playlist.get("track") or []
        if isinstance(t, dict)
    ]
    return _result(
        "listenbrainz", "playlist", playlist.get("title"), playlist.get("creator"),
        "", tracks,
    )


def fetch(url, cfg=None):
    url = (url or "").strip()
    if _host(url) in _SHORT_LINK_HOSTS:
        url = _resolve_short_link(url)
    matched = _match(url)
    if not matched:
        raise ExternalPlaylistError(
            "Use a Spotify, Deezer, Apple Music or ListenBrainz link.",
        )
    service, kind, item_id, country = matched
    cfg = cfg or load_config()
    if service == "spotify":
        result = _spotify(kind, item_id, cfg)
    elif service == "deezer":
        result = _deezer(kind, item_id)
    elif service == "apple":
        result = _apple(kind, item_id, country)
    else:
        result = _listenbrainz(item_id)
    logger.info(
        "Read %s %s '%s': %d tracks",
        SERVICE_LABELS[service], kind, result["title"], len(result["tracks"]),
    )
    return result


def info_response(result):
    entries = [
        {
            "index": i,
            "title": t["title"],
            "artist": t["artist"],
            "album": t["album"],
            "duration": t["duration"],
            "thumbnail": t["thumbnail"] or result["thumbnail"],
            "channel": t["artist"],
            "search": True,
        }
        for i, t in enumerate(result["tracks"])
    ]
    return {
        "type": KIND_TYPES[result["kind"]],
        "kind": result["kind"],
        "source": SERVICE_LABELS[result["service"]],
        "title": result["title"],
        "channel": result["owner"],
        "thumbnail": result["thumbnail"],
        "entries": entries,
    }


MAX_LIDARR_ALBUMS = 60
_lidarr_job = {"running": False, "albums": [], "done": 0, "total": 0}
_lidarr_lock = threading.Lock()


def unique_albums(entries):
    seen = set()
    albums = []
    for e in entries or []:
        if not isinstance(e, dict):
            continue
        artist = " ".join(str(e.get("artist") or "").split())[:300]
        album = " ".join(str(e.get("album") or "").split())[:300]
        if not artist or not album:
            continue
        artist = artist.split(", ")[0]
        key = (artist.casefold(), album.casefold())
        if key in seen:
            continue
        seen.add(key)
        albums.append({"artist": artist, "album": album})
    return albums[:MAX_LIDARR_ALBUMS]


def _match_lidarr_album(artist, album):
    import explore
    import library

    ytm = {"title": album, "artists": [{"name": artist}]}
    candidates = []
    seen = set()
    decision = {"status": explore.STATUS_NOT_ON_MB, "match": None}
    for term in explore._album_lookup_terms(ytm):
        for c in library.search("album", term):
            if c["foreignAlbumId"] not in seen:
                seen.add(c["foreignAlbumId"])
                candidates.append(c)
        decision = explore.decide_album(ytm, candidates)
        if decision["match"] is not None:
            return decision
    exact = [
        c for c in explore.rank_album_candidates(ytm, candidates)
        if (c.get("title") or "").casefold() == album.casefold()
        and (c.get("artistName") or "").casefold() == artist.casefold()
    ]
    if len(exact) > 1:
        exact = [c for c in exact if (c.get("type") or "").lower() == "album"] or exact
    if len(exact) == 1:
        return {"status": explore.album_status(exact[0]), "match": exact[0]}
    return decision


def _add_one_album(item):
    import explore
    import library

    try:
        decision = _match_lidarr_album(item["artist"], item["album"])
    except library.LibraryError as e:
        return {"state": "error", "message": e.message}
    match = decision["match"]
    if match is None:
        if decision["status"] == explore.STATUS_AMBIGUOUS:
            return {"state": "ambiguous", "message": "Several MusicBrainz releases match"}
        return {"state": "not_found", "message": "Not on MusicBrainz"}
    if decision["status"] == explore.STATUS_COMPLETE:
        return {"state": "complete", "message": "Already complete in Lidarr"}
    try:
        added = library.add_album(
            {"foreignAlbumId": match["foreignAlbumId"], "download": True},
        )
    except library.LibraryError as e:
        return {"state": "error", "message": e.message}
    return {"state": "queued", "message": f"Added and queued ({added.get('status')})"}


def _run_lidarr_job(albums):
    import db

    try:
        for item in albums:
            try:
                outcome = _add_one_album(item)
            except Exception as e:
                logger.warning("Adding %s — %s failed: %s", item["artist"], item["album"], e)
                outcome = {"state": "error", "message": "Unexpected error"}
            with _lidarr_lock:
                item.update(outcome)
                _lidarr_job["done"] += 1
        queued = sum(1 for a in albums if a.get("state") == "queued")
        logger.info("Playlist albums to Lidarr: %d of %d queued", queued, len(albums))
    finally:
        with _lidarr_lock:
            _lidarr_job["running"] = False
        try:
            db.close_db()
        except Exception:
            pass


def start_lidarr_job(entries):
    albums = unique_albums(entries)
    if not albums:
        raise ExternalPlaylistError("No album names to look up in Lidarr.")
    with _lidarr_lock:
        if _lidarr_job["running"]:
            raise ExternalPlaylistError("Albums are already being added; wait for them.", 409)
        for a in albums:
            a.update(state="pending", message="")
        _lidarr_job.update(running=True, albums=albums, done=0, total=len(albums))
    threading.Thread(
        target=_run_lidarr_job, args=(albums,), daemon=True, name="playlist-to-lidarr",
    ).start()
    return lidarr_job_status()


def lidarr_job_status():
    with _lidarr_lock:
        return {
            "running": _lidarr_job["running"],
            "done": _lidarr_job["done"],
            "total": _lidarr_job["total"],
            "albums": [dict(a) for a in _lidarr_job["albums"]],
        }
