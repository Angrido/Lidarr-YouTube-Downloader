"""YouTube search, scoring, and download via yt-dlp.

Provides download_track_youtube() which searches YouTube for a track,
scores candidates by title similarity, duration, channel, and view count,
then downloads the best match as MP3.

Public API:
    search_youtube_candidates() -- search and score; returns ranked list
    download_youtube_candidate() -- download a single candidate dict
    download_track_youtube()    -- thin wrapper combining both
"""

import logging
import math
import os
import re
from difflib import SequenceMatcher

import yt_dlp

from config import DEFAULT_FORBIDDEN_WORDS, load_config

logger = logging.getLogger(__name__)


def get_ytdlp_version():
    """Return the installed yt-dlp version string."""
    try:
        import importlib.metadata
        return importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        pass
    except Exception as e:
        logger.debug("importlib.metadata version lookup failed: %s", e)
    try:
        return yt_dlp.version.__version__
    except AttributeError:
        logger.warning("Could not determine yt-dlp version")
        return "unknown"


_NOISE_PATTERN = re.compile(
    r"\s*[\(\[\|]\s*(?:"
    r"clip\s*officiel?|paroles?|lyrics?\s*(?:vid[eé]o)?|official\s*(?:music\s*)?(?:video|audio|clip)?"
    r"|audio\s*officiel?|vid[eé]o\s*officielle?|hd|4k|vevo|remastered|visualizer"
    r"|feat\.?|ft\.?\s*\w[\w\s]*"
    r")\s*[\)\]\|]",
    re.IGNORECASE,
)
_FEAT_PATTERN = re.compile(r"\s+(?:feat\.?|ft\.?)\s+[\w\s&,]+", re.IGNORECASE)


_DASH_PATTERN = re.compile(r"[–—‐‑‒―]")


def _normalize_dashes(text):
    return _DASH_PATTERN.sub("-", text)


def _normalize_yt_title(title):
    t = _normalize_dashes(title)
    t = _NOISE_PATTERN.sub("", t)
    t = _FEAT_PATTERN.sub("", t)
    return re.sub(r"\s+", " ", t).strip().lower()


def _title_similarity(yt_title, track_title, artist_name):
    yt_lower = _normalize_dashes(yt_title).lower()
    track_lower = _normalize_dashes(track_title).lower()
    artist_lower = artist_name.lower()

    has_track = track_lower in yt_lower
    has_artist = artist_lower in yt_lower

    if has_track and has_artist:
        return 1.0

    yt_norm = _normalize_yt_title(yt_title)
    track_norm = _normalize_yt_title(track_title)
    artist_norm = _normalize_yt_title(artist_name)

    if track_norm in yt_norm and artist_norm in yt_norm:
        return 1.0

    score = SequenceMatcher(None, yt_norm, f"{artist_norm} {track_norm}").ratio()
    if has_track:
        score += 0.3
    if has_artist:
        score += 0.2
    elif artist_norm in yt_norm:
        score += 0.15
    return min(score, 1.0)


def _is_official_channel(channel_name, artist_name):
    """Check if a YouTube channel looks official for the artist.

    Returns True if the channel name contains the artist name or
    common official suffixes like VEVO, Topic, or Official.
    """
    if not channel_name:
        return False
    ch = channel_name.lower()
    ar = artist_name.lower()
    if ar in ch:
        return True
    for suffix in [" - topic", "vevo", " official"]:
        if suffix in ch:
            return True
    return False


_MUSIC_VIDEO_HINT = re.compile(
    r"\b(?:music\s*video|official\s*video|video\s*oficial"
    r"|clip\s*officiel|m/?v)\b",
    re.IGNORECASE,
)


def _looks_like_music_video(title):
    """Heuristic: title advertises a music video rather than the audio."""
    return bool(title) and bool(_MUSIC_VIDEO_HINT.search(title))


def _is_topic_channel(channel_name, artist_name):
    """True for YouTube's auto-generated "<Artist> - Topic" channels.

    Topic channels host the same masters Lidarr already pulls metadata
    for, so they're the highest-quality, most canonical source.
    """
    if not channel_name:
        return False
    ch = channel_name.lower().strip()
    ar = artist_name.lower().strip()
    if not ar:
        return False
    return ch.endswith("- topic") and ar in ch


def get_effective_forbidden_words(config):
    """Build the normalized forbidden-word list (built-in + custom).

    Both the built-in selection (``forbidden_words``) and the user's
    additions (``forbidden_words_custom``) are stripped, lower-cased and
    de-duplicated, so matching is case-insensitive and a word configured in
    either list — even via the API/env with stray casing or whitespace — is
    honored. Falls back to ``DEFAULT_FORBIDDEN_WORDS`` only when the
    built-in key is missing or not a list.
    """
    builtin = config.get("forbidden_words")
    if not isinstance(builtin, (list, tuple)):
        builtin = DEFAULT_FORBIDDEN_WORDS
    custom = config.get("forbidden_words_custom")
    if not isinstance(custom, (list, tuple)):
        custom = []
    merged = []
    seen = set()
    for raw in list(builtin) + list(custom):
        word = raw.strip().lower() if isinstance(raw, str) else ""
        if word and word not in seen:
            seen.add(word)
            merged.append(word)
    return merged


def _check_forbidden(yt_title_lower, track_title_lower, forbidden_list):
    """Check if a YouTube title contains a forbidden word.

    Multi-word forbidden terms use substring matching. Single words
    use word-boundary regex. Terms present in the original track
    title are allowed (so a track called "Foo (Live)" still matches
    "Foo (Live)" on YouTube).

    Returns:
        The matched forbidden word, or None if clean.
    """
    for word in forbidden_list:
        if " " in word:
            if word in yt_title_lower and word not in track_title_lower:
                return word
        else:
            pattern = r'\b' + re.escape(word) + r'\b'
            if (
                re.search(pattern, yt_title_lower)
                and not re.search(pattern, track_title_lower)
            ):
                return word
    return None


class _SilentYDLLogger:
    # yt-dlp routes format errors through report_error, which writes to
    # stderr even with quiet=True. We catch them as exceptions and
    # walk the fallback chain, so they don't deserve ERROR-level noise.
    _SUPPRESS_SUBSTRINGS = (
        "requested format is not available",
        "no video formats found",
        # android client commonly returns this even with valid cookies;
        # music/web clients recover on the next attempt.
        "please sign in",
    )

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        logger.debug("yt-dlp warning: %s", msg)

    def error(self, msg):
        text = str(msg or "").lower()
        if any(s in text for s in self._SUPPRESS_SUBSTRINGS):
            logger.debug("yt-dlp (suppressed): %s", msg)
            return
        logger.warning("yt-dlp: %s", msg)


_SILENT_YDL_LOGGER = _SilentYDLLogger()


def _build_common_opts(player_client=None):
    cfg = load_config()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "logger": _SILENT_YDL_LOGGER,
        "retries": int(cfg.get("yt_retries", 10)),
        "fragment_retries": int(cfg.get("yt_fragment_retries", 10)),
        "sleep_interval_requests": int(cfg.get("yt_sleep_requests", 1)),
        "sleep_interval": int(cfg.get("yt_sleep_interval", 1)),
        "max_sleep_interval": int(cfg.get("yt_max_sleep_interval", 5)),
        "noplaylist": True,
        # Without this yt-dlp may pick a 128k stream over a 256k one.
        "format_sort": ["abr", "asr"],
    }
    cookies_path = (cfg.get("yt_cookies_file") or "").strip()
    if cookies_path and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    elif cookies_path and not os.path.exists(cookies_path):
        logger.warning(f"YT_COOKIES_FILE not found: {cookies_path}")
    if cfg.get("yt_force_ipv4", True):
        opts["source_address"] = "0.0.0.0"
    extractor_args = {}
    yt_args = {}
    if player_client:
        yt_args["player_client"] = [player_client]
    # Manual PO token(s) for users hitting "Sign in to confirm you're not a
    # bot" / format-unavailable.
    po_token = (cfg.get("yt_po_token") or "").strip()
    if po_token:
        yt_args["po_token"] = [
            t.strip() for t in po_token.split(",") if t.strip()
        ]
    if yt_args:
        extractor_args["youtube"] = yt_args
    # Automatic PO tokens via a bgutil provider server (run as a sidecar).
    pot_url = (cfg.get("yt_pot_provider_url") or "").strip()
    if pot_url:
        extractor_args["youtubepot-bgutilhttp"] = {"base_url": [pot_url]}
    if extractor_args:
        opts["extractor_args"] = extractor_args
    return opts


MAX_CANDIDATES = 10

# YouTube Music auto-generates an album-browse playlist for every release
# uploaded by a label; its id always starts with this prefix. Discovering
# this playlist is the most reliable way to identify the canonical track
# list before doing any per-song search.
_YTM_ALBUM_PLAYLIST_PREFIX = "OLAK5uy_"
_YTM_ALBUM_LIST_PATTERN = re.compile(r"list=(OLAK5uy_[A-Za-z0-9_-]+)")

# YouTube Music search-filter params (base64 protobuf, stable values
# also used by ytmusicapi). Forcing the "Albums" filter makes YT Music
# return album shelves only, which yt-dlp's search extractor surfaces as
# entries whose id/url contains the album playlist id (OLAK5uy_...).
_YTM_PARAMS_ALBUMS = "EgWKAQIYAWoMEA4QChADEAQQCRAF"


def _ytdl_for_album_browse(player_client, flat_mode=True):
    """YDL options for browsing YT Music albums / search shelves."""
    opts = _build_common_opts(player_client=player_client)
    opts.update({
        "extract_flat": flat_mode,
        "skip_download": True,
        # _build_common_opts sets noplaylist=True for per-video extraction;
        # album browse needs the playlist contents.
        "noplaylist": False,
    })
    return opts


def _ytm_search_url(query, params=None):
    import urllib.parse as _urlparse
    url = (
        "https://music.youtube.com/search?q="
        + _urlparse.quote(query)
    )
    if params:
        url += "&sp=" + _urlparse.quote(params)
    return url


def _scan_for_olak_id(obj, depth=0):
    """Recursively scan a yt-dlp result for an OLAK5uy_ album playlist id.

    YT Music search results can wrap album playlists in nested shelves /
    structured fields; a flat ``entries`` iteration misses them. This
    walks the whole structure (capped depth) and inspects id, playlist_id,
    url, webpage_url, original_url, and any string value that contains a
    ``list=OLAK5uy_...`` substring.
    """
    if depth > 6 or obj is None:
        return None
    if isinstance(obj, dict):
        for key in ("id", "playlist_id", "browse_id"):
            val = obj.get(key) or ""
            if isinstance(val, str) and val.startswith(
                _YTM_ALBUM_PLAYLIST_PREFIX
            ):
                return val
        for key in ("url", "webpage_url", "original_url"):
            val = obj.get(key) or ""
            if isinstance(val, str):
                m = _YTM_ALBUM_LIST_PATTERN.search(val)
                if m:
                    return m.group(1)
        for v in obj.values():
            if isinstance(v, (dict, list)):
                found = _scan_for_olak_id(v, depth + 1)
                if found:
                    return found
            elif isinstance(v, str):
                m = _YTM_ALBUM_LIST_PATTERN.search(v)
                if m:
                    return m.group(1)
    elif isinstance(obj, list):
        for item in obj:
            found = _scan_for_olak_id(item, depth + 1)
            if found:
                return found
    return None


def _discover_ytm_album_playlist_id(search_url, player_client, flat_mode=True):
    """Scan a YouTube Music search URL for an OLAK5uy_ album playlist."""
    try:
        with yt_dlp.YoutubeDL(
            _ytdl_for_album_browse(player_client, flat_mode=flat_mode)
        ) as ydl:
            res = ydl.extract_info(search_url, download=False) or {}
    except Exception as exc:
        logger.debug(
            "YT Music album discovery failed for %s (flat=%s): %s",
            search_url, flat_mode, exc,
        )
        return None
    return _scan_for_olak_id(res)


def _extract_ytm_album_entries(playlist_url, player_client):
    """Extract the track list from a YT Music album playlist URL."""
    try:
        with yt_dlp.YoutubeDL(_ytdl_for_album_browse(player_client)) as ydl:
            res = ydl.extract_info(playlist_url, download=False) or {}
    except Exception as exc:
        logger.debug("YT Music playlist extraction failed for %s: %s", playlist_url, exc)
        return []
    raw_entries = res.get("entries") or []
    out = []
    for raw in raw_entries:
        if not isinstance(raw, dict):
            continue
        vid = raw.get("id") or ""
        if not vid:
            continue
        out.append({
            "url": f"https://music.youtube.com/watch?v={vid}",
            "title": (raw.get("title") or "").strip(),
            "duration": int(raw.get("duration") or 0),
            "channel": (
                raw.get("uploader")
                or raw.get("channel")
                or raw.get("artist")
                or ""
            ),
        })
    return out


def _ytmusicapi_client():
    """Lazy-import ytmusicapi; return YTMusic instance or None."""
    try:
        from ytmusicapi import YTMusic
    except Exception as exc:
        logger.debug("ytmusicapi not available: %s", exc)
        return None
    try:
        return YTMusic()
    except Exception as exc:
        logger.debug("ytmusicapi client init failed: %s", exc)
        return None


def _parse_ytmusicapi_duration(dur_str):
    """Parse 'M:SS' / 'H:MM:SS' duration strings from ytmusicapi."""
    if not dur_str:
        return 0
    parts = dur_str.strip().split(":")
    try:
        parts = [int(p) for p in parts]
    except ValueError:
        return 0
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    if len(parts) == 3:
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    return 0


def _ytmusicapi_pick_album(results, artist, album):
    """Pick the album result that best matches (artist, album).

    ytmusicapi may return multiple albums with similar titles; we require
    the artist field to match and use title similarity to break ties.
    """
    if not results:
        return None
    artist_lower = artist.lower()
    album_norm = _normalize_yt_title(album)
    best = None
    best_score = 0.0
    for r in results:
        if not isinstance(r, dict):
            continue
        if r.get("resultType") not in (None, "album"):
            continue
        r_artist_field = r.get("artist") or ""
        r_artists_list = r.get("artists") or []
        r_artist_names = " ".join(
            (a.get("name", "") if isinstance(a, dict) else str(a))
            for a in r_artists_list
        )
        r_artist_blob = (r_artist_field + " " + r_artist_names).lower()
        if artist_lower not in r_artist_blob:
            continue
        r_title = r.get("title", "") or ""
        r_norm = _normalize_yt_title(r_title)
        sim = SequenceMatcher(None, album_norm, r_norm).ratio()
        if album_norm and (album_norm in r_norm or r_norm in album_norm):
            sim = max(sim, 0.95)
        if r_norm == album_norm:
            sim = 1.0
        if sim > best_score:
            best_score = sim
            best = r
    if best and best_score >= 0.60:
        return best
    return None


def _find_album_via_ytmusicapi(artist, album, skip_check=None):
    """Use ytmusicapi (InnerTube) to resolve OLAK5uy_ + track list.

    yt-dlp can't reliably resolve YT Music album browse URLs (upstream
    issue yt-dlp/yt-dlp#16241), but ytmusicapi returns the playlistId
    and full track list directly from the InnerTube API.
    """
    yt = _ytmusicapi_client()
    if yt is None:
        return None
    queries = [f"{artist} {album}", album]
    chosen = None
    for q in queries:
        if skip_check and skip_check():
            return None
        try:
            logger.info(
                "   Album lookup [ytmusicapi albums-filter]: \"%s\"", q,
            )
            results = yt.search(q, filter="albums", limit=20)
        except Exception as exc:
            logger.debug("ytmusicapi search failed (%s): %s", q, exc)
            continue
        chosen = _ytmusicapi_pick_album(results, artist, album)
        if chosen:
            break
    if not chosen:
        return None
    browse_id = chosen.get("browseId") or ""
    playlist_id = chosen.get("playlistId") or ""
    if not browse_id and not playlist_id:
        return None
    if skip_check and skip_check():
        return None
    album_details = {}
    try:
        if browse_id:
            album_details = yt.get_album(browse_id) or {}
    except Exception as exc:
        logger.debug("ytmusicapi get_album(%s) failed: %s", browse_id, exc)
    if not playlist_id:
        playlist_id = album_details.get("audioPlaylistId", "") or ""
    if not playlist_id:
        return None
    raw_tracks = album_details.get("tracks", []) or []
    entries = []
    for t in raw_tracks:
        if not isinstance(t, dict):
            continue
        vid = t.get("videoId") or ""
        if not vid:
            continue
        t_artists = t.get("artists") or []
        channel_name = " / ".join(
            (a.get("name", "") if isinstance(a, dict) else str(a))
            for a in t_artists
        ) or artist
        entries.append({
            "url": f"https://music.youtube.com/watch?v={vid}",
            "title": (t.get("title") or "").strip(),
            "duration": _parse_ytmusicapi_duration(t.get("duration", "")),
            "channel": channel_name,
        })
    if not entries:
        cfg = load_config()
        player_client = cfg.get("yt_player_client", "android") or None
        playlist_url = (
            f"https://music.youtube.com/playlist?list={playlist_id}"
        )
        entries = _extract_ytm_album_entries(playlist_url, player_client)
    if not entries:
        return None
    playlist_url = (
        f"https://music.youtube.com/playlist?list={playlist_id}"
    )
    logger.info(
        "   Resolved official YT Music album via ytmusicapi: %s (%d tracks)",
        playlist_url, len(entries),
    )
    return {
        "playlist_url": playlist_url,
        "playlist_id": playlist_id,
        "entries": entries,
    }


def find_album_on_ytmusic(artist, album, skip_check=None):
    """Discover the official YouTube Music album playlist for (artist, album).

    Returns dict {"playlist_url", "playlist_id", "entries"} or None.
    Each entry is {"url", "title", "duration", "channel"}.

    Primary: ytmusicapi (InnerTube; returns OLAK5uy_ playlistId and the
    full track list directly).
    Fallback: yt-dlp scan of music.youtube.com/search results (works only
    when YT Music exposes the OLAK5uy_ id in the page payload).
    """
    if skip_check and skip_check():
        return None
    artist = (artist or "").strip()
    album = (album or "").strip()
    if not artist or not album:
        return None

    via_api = _find_album_via_ytmusicapi(artist, album, skip_check=skip_check)
    if via_api:
        return via_api

    cfg = load_config()
    player_client = cfg.get("yt_player_client", "android") or None
    strategies = [
        (f"{artist} {album}", _YTM_PARAMS_ALBUMS, True),
        (f"{artist} {album} album", _YTM_PARAMS_ALBUMS, True),
        (f"{artist} {album}", _YTM_PARAMS_ALBUMS, "in_playlist"),
        (f"{artist} {album}", None, True),
        (f"{artist} {album} album", None, True),
        (f"{artist} {album}", None, "in_playlist"),
    ]
    playlist_id = None
    for q, sp_filter, flat_mode in strategies:
        if skip_check and skip_check():
            return None
        url = _ytm_search_url(q, params=sp_filter)
        label = "albums-filter" if sp_filter else "plain"
        logger.info(
            "   Album lookup [ytdlp %s flat=%s]: \"%s\"",
            label, flat_mode, url,
        )
        playlist_id = _discover_ytm_album_playlist_id(
            url, player_client, flat_mode=flat_mode,
        )
        if playlist_id:
            break
    if not playlist_id:
        logger.info(
            "   No official YT Music album playlist found for %s - %s",
            artist, album,
        )
        return None

    playlist_url = (
        f"https://music.youtube.com/playlist?list={playlist_id}"
    )
    entries = _extract_ytm_album_entries(playlist_url, player_client)
    if not entries:
        logger.info(
            "   YT Music album playlist %s yielded no tracks", playlist_url,
        )
        return None
    logger.info(
        "   Official YT Music album (ytdlp fallback): %s (%d tracks)",
        playlist_url, len(entries),
    )
    return {
        "playlist_url": playlist_url,
        "playlist_id": playlist_id,
        "entries": entries,
    }


def match_album_track(album_entries, track_title, expected_duration_ms=None):
    """Find the YT Music album entry that best matches a track.

    Returns a candidate dict ready for download_youtube_candidate(), or
    None if no entry passes the similarity gate.
    """
    if not album_entries:
        return None
    expected_sec = None
    if expected_duration_ms:
        try:
            expected_sec = float(expected_duration_ms) / 1000.0
        except (TypeError, ValueError):
            expected_sec = None

    track_norm = _normalize_yt_title(track_title)
    track_lower = _normalize_dashes(track_title).lower().strip()
    best = None
    best_score = 0.0

    for e in album_entries:
        title = e.get("title", "") or ""
        e_norm = _normalize_yt_title(title)
        if not e_norm:
            continue
        e_lower = _normalize_dashes(title).lower().strip()
        if track_lower == e_lower or track_norm == e_norm:
            sim = 1.0
        elif track_norm and (
            track_norm in e_norm or e_norm in track_norm
        ):
            sim = 0.95
        else:
            sim = SequenceMatcher(None, track_norm, e_norm).ratio()

        dur_ok = True
        if expected_sec and e.get("duration"):
            ddiff = abs(e["duration"] - expected_sec)
            # Album versions can differ from singles; 30s is a generous gate.
            if ddiff > 30:
                dur_ok = False

        if sim > best_score and dur_ok:
            best_score = sim
            best = e

    if best and best_score >= 0.80:
        return {
            "url": best["url"],
            "title": best["title"],
            "duration": int(best.get("duration") or 0),
            "channel": best.get("channel", ""),
            "score": 1.0,
            "source": "ytmusic",
            "from_album_playlist": True,
        }
    return None


def search_youtube_candidates(
    query, track_title_original,
    expected_duration_ms=None, skip_check=None, banned_urls=None,
):
    """Search YouTube and return scored, ranked candidates (up to MAX_CANDIDATES).

    Args:
        query: Search query string (typically "Artist Track official audio").
        track_title_original: Original track title for scoring and filtering.
        expected_duration_ms: Expected duration in milliseconds, or None.
        skip_check: Optional callable; if it returns True, abort early and
            return an empty list.
        banned_urls: Optional set of YouTube URLs to exclude.

    Returns:
        List of candidate dicts sorted by score descending, each with keys:
        url, title, duration, channel, score. Empty list on no match or skip.
    """
    if skip_check and skip_check():
        return []

    config = load_config()
    first_client = config.get("yt_player_client", "android") or None
    ydl_opts_search = {
        **_build_common_opts(player_client=first_client),
        "format": "bestaudio/best",
        "extract_flat": True,
    }

    forbidden_words = get_effective_forbidden_words(config)
    duration_tolerance = config.get("duration_tolerance", 15)

    expected_duration_sec = None
    if expected_duration_ms:
        expected_duration_sec = expected_duration_ms / 1000.0
        mins = int(expected_duration_sec // 60)
        secs = int(expected_duration_sec % 60)
        logger.info(
            f"Expected track duration: {mins}:{secs:02d}"
            f" ({int(expected_duration_sec)}s)"
        )

    artist_part = query.split(" ")[0] if " " in query else query
    base_track = track_title_original
    base_artist = query.replace(
        f" {track_title_original} official audio", ""
    ).replace(f" {track_title_original}", "").strip()
    if not base_artist:
        base_artist = artist_part

    added = {query}
    import urllib.parse as _urlparse

    def _ytmusic_url(q):
        return (
            "https://music.youtube.com/search?q=" + _urlparse.quote(q)
        )

    search_queries = [
        ("ytmusic", _ytmusic_url(f"{base_artist} {base_track}")),
        ("ytmusic", _ytmusic_url(f"{base_artist} {base_track} official audio")),
        ("ytsearch", f"{base_artist} - Topic {base_track}"),
        ("ytsearch", f'"{base_artist}" {base_track} official audio'),
        ("ytsearch", query),
    ]
    added.update(sq for kind, sq in search_queries if kind == "ytsearch")

    for candidate_q in [
        f"{base_artist} - {base_track}",
        f"{base_artist} {base_track}",
        f"{base_artist} {base_track} audio officiel",
        f"{base_track} {base_artist}",
        f"{base_track} audio",
    ]:
        if candidate_q not in added:
            added.add(candidate_q)
            search_queries.append(("ytsearch", candidate_q))

    seen_ids = {}
    candidates = []
    GOOD_SCORE = 0.80

    # Phase 1: only accept entries from the artist's official/Topic channel
    # on YouTube Music or YouTube. Phase 2 (any source) runs only if Phase 1
    # fails to surface a candidate at or above GOOD_SCORE.
    search_phases = [
        ("artist-channel", True),
        ("any-source", False),
    ]
    for phase_name, official_only in search_phases:
        if any(c["score"] >= GOOD_SCORE for c in candidates):
            break
        logger.info(
            "   Search phase: %s",
            (
                "artist channel only (music.youtube + Topic)"
                if official_only
                else "any source (fallback)"
            ),
        )
        for qi, (kind, sq) in enumerate(search_queries):
            if skip_check and skip_check():
                return []

            has_good = any(c["score"] >= GOOD_SCORE for c in candidates)
            if has_good:
                break

            logger.info(
                f"   Search ({qi+1}/{len(search_queries)}) [{kind}]:"
                f' "{sq}"'
            )
            if kind == "ytmusic":
                search_target = sq
                search_limit = 10
            else:
                search_target = f"ytsearch15:{sq}"
                search_limit = None
            try:
                with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                    search_results = ydl.extract_info(
                        search_target, download=False,
                    )
                    if (
                        search_limit is not None
                        and isinstance(search_results, dict)
                        and search_results.get("entries")
                    ):
                        search_results = {
                            "entries": list(search_results["entries"])[:search_limit]
                        }
                    entries_total = (
                        len(search_results.get("entries", []) or [])
                        if isinstance(search_results, dict) else 0
                    )
                    accepted_before = len(candidates)
                    for entry in search_results.get("entries", []):
                        title = entry.get("title", "").lower()
                        url = entry.get("url")
                        duration = entry.get("duration", 0)
                        channel = (
                            entry.get("channel", "")
                            or entry.get("uploader", "")
                            or ""
                        )
                        view_count = entry.get("view_count", 0) or 0

                        # ytmusic credits the artist via ``artists``; absent
                        # means unverifiable (lenient), present-and-wrong
                        # means reject.
                        entry_artists = entry.get("artists") or []
                        if isinstance(entry_artists, str):
                            entry_artists = [entry_artists]
                        artists_blob = " ".join(
                            a if isinstance(a, str) else (a.get("name", "") if isinstance(a, dict) else "")
                            for a in entry_artists
                        ).lower()
                        artist_in_artists = bool(
                            base_artist and artists_blob
                            and base_artist.lower() in artists_blob
                        )
                        uploader_field = entry.get("uploader", "") or ""

                        ytmusic_source = kind == "ytmusic"
                        is_topic = _is_topic_channel(channel, base_artist)
                        is_official = _is_official_channel(channel, base_artist)
                        channel_artist_match = is_topic or is_official
                        uploader_artist_match = (
                            _is_official_channel(uploader_field, base_artist)
                            or _is_topic_channel(uploader_field, base_artist)
                        )
                        ytmusic_artist_proven = ytmusic_source and (
                            artist_in_artists or uploader_artist_match
                        )

                        # Reject explicit-mismatch ytmusic entries: homonym
                        # songs / same-title covers by other artists are
                        # the main failure mode.
                        has_explicit_mismatch = False
                        if base_artist:
                            ba_lower = base_artist.lower()
                            if artists_blob and ba_lower not in artists_blob:
                                has_explicit_mismatch = True
                            elif (
                                channel
                                and ba_lower not in channel.lower()
                                and not is_topic
                                and not is_official
                            ):
                                # Non-matching channel counts as mismatch
                                # only if no other artist field rescues it.
                                if not artist_in_artists and not uploader_artist_match:
                                    has_explicit_mismatch = ytmusic_source

                        if official_only and has_explicit_mismatch:
                            logger.debug(
                                "   Rejected '%s' (channel '%s', artists=%s)"
                                " - phase 1 explicit artist mismatch",
                                entry.get("title", ""), channel, entry_artists,
                            )
                            continue

                        # Lenient phase 1: positive proof OR ytmusic source
                        # (YT Music's catalogue is artist-curated).
                        artist_official = (
                            channel_artist_match
                            or ytmusic_artist_proven
                            or ytmusic_source
                        )
                        if official_only and not artist_official:
                            logger.debug(
                                "   Rejected '%s' (channel '%s', artists=%s)"
                                " - phase 1 accepts artist's official"
                                " or Topic channel or ytmusic source",
                                entry.get("title", ""), channel, entry_artists,
                            )
                            continue
                        if not channel_artist_match:
                            blocked = _check_forbidden(
                                title, track_title_original.lower(),
                                forbidden_words,
                            )
                            if blocked:
                                logger.debug(
                                    f"   Rejected '{entry.get('title', '')}'"
                                    f" - forbidden word '{blocked}'"
                                )
                                continue

                        duration_known = bool(duration) and duration > 0
                        effective_tolerance = duration_tolerance
                        if artist_official:
                            effective_tolerance = max(
                                duration_tolerance, duration_tolerance * 2, 30
                            )
                        if expected_duration_sec and duration_known:
                            min_dur = max(
                                15, expected_duration_sec - effective_tolerance
                            )
                            max_dur = expected_duration_sec + effective_tolerance
                            if duration < min_dur or duration > max_dur:
                                logger.debug(
                                    f"   Rejected '{entry.get('title', '')}'"
                                    f" - duration {int(duration)}s outside"
                                    f" [{int(min_dur)}s - {int(max_dur)}s]"
                                )
                                continue
                            dur_diff = abs(duration - expected_duration_sec)
                            duration_score = max(
                                0, 1.0 - (dur_diff / max(effective_tolerance, 1))
                            )
                        elif expected_duration_sec and not duration_known:
                            duration_score = 0.5
                        else:
                            if duration_known and (duration < 15 or duration > 7200):
                                continue
                            duration_score = 0.5

                        if banned_urls and url in banned_urls:
                            logger.debug(
                                "   Rejected '%s' - URL banned by user",
                                entry.get("title", ""),
                            )
                            continue

                        # Hard requirement: the track title must actually
                        # appear in the YouTube title (or cover ≥85% of it
                        # by longest common substring, to allow tiny
                        # punctuation/normalisation differences). Just
                        # matching the artist name is the same-artist
                        # wrong-song trap.
                        yt_title_raw = entry.get("title", "")
                        yt_norm = _normalize_dashes(yt_title_raw).lower()
                        track_norm = _normalize_dashes(track_title_original).lower()
                        if track_norm and track_norm not in yt_norm:
                            match = SequenceMatcher(
                                None, track_norm, yt_norm,
                            ).find_longest_match(
                                0, len(track_norm), 0, len(yt_norm),
                            )
                            coverage = match.size / max(len(track_norm), 1)
                            if coverage < 0.85:
                                logger.debug(
                                    f"   Rejected '{yt_title_raw}'"
                                    f" - track title not present"
                                    f" (coverage {coverage:.0%})"
                                )
                                continue

                        title_score = _title_similarity(
                            yt_title_raw,
                            track_title_original, base_artist,
                        )
                        if has_explicit_mismatch:
                            # Penalty large enough to outweigh any
                            # title/duration/view boost in phase 2.
                            official_bonus = 0.0
                            artist_mismatch_penalty = 0.55
                        elif channel_artist_match or ytmusic_artist_proven:
                            official_bonus = 0.45
                            artist_mismatch_penalty = 0.0
                        elif is_official:
                            official_bonus = 0.40
                            artist_mismatch_penalty = 0.0
                        elif ytmusic_source:
                            official_bonus = 0.20
                            artist_mismatch_penalty = 0.0
                        else:
                            official_bonus = 0.0
                            artist_mismatch_penalty = 0.0
                        if view_count > 0:
                            view_score = min(
                                0.1, math.log10(max(view_count, 1)) / 100
                            )
                        else:
                            view_score = 0.0
                        certainty_bonus = 0.15 if title_score >= 1.0 else 0.0
                        # Nudge toward the audio version when a title looks
                        # like a music video (videos are often louder/longer
                        # and lower audio quality than the Topic/audio upload).
                        video_penalty = (
                            0.12 if _looks_like_music_video(yt_title_raw)
                            else 0.0
                        )
                        total_score = (
                            (duration_score * 0.25)
                            + (title_score * 0.50)
                            + official_bonus
                            + view_score
                            + certainty_bonus
                            - artist_mismatch_penalty
                            - video_penalty
                        )

                        if not url:
                            continue
                        video_id = _extract_video_id(url) or url
                        if video_id in seen_ids:
                            existing = candidates[seen_ids[video_id]]
                            # Same video found by both ytmusic and ytsearch
                            # passes — keep the ytmusic source so the UI
                            # link reflects YouTube Music.
                            if kind == "ytmusic" and existing.get("source") != "ytmusic":
                                existing["source"] = "ytmusic"
                                existing["url"] = url
                            continue
                        seen_ids[video_id] = len(candidates)
                        candidates.append({
                            "url": url,
                            "title": entry.get("title", ""),
                            "duration": duration,
                            "channel": channel,
                            "score": total_score,
                            "source": kind,
                        })
                        logger.debug(
                            f"   Candidate '{entry.get('title', '')}'"
                            f" -- score={total_score:.2f}"
                            f" (dur={duration_score:.2f}"
                            f" title={title_score:.2f}"
                            f" official={official_bonus:.2f}"
                            f" certainty={certainty_bonus:.2f}"
                            f" views={view_score:.3f})"
                        )
                    logger.info(
                        f"   [{kind}] {entries_total} entries"
                        f" -> {len(candidates) - accepted_before} accepted"
                    )
            except Exception as e:
                logger.error(f'   Search failed for "{sq}": {e}')

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:MAX_CANDIDATES]


_VIDEO_ID_RE = re.compile(r"(?:v=|/)([0-9A-Za-z_-]{11})(?:[?&#]|$)")


def _extract_video_id(url):
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    if match:
        return match.group(1)
    if re.fullmatch(r"[0-9A-Za-z_-]{11}", url):
        return url
    return None


def _candidate_display_url(candidate):
    raw = candidate.get("url", "")
    video_id = _extract_video_id(raw)
    if not video_id:
        return raw
    if candidate.get("source") == "ytmusic":
        return f"https://music.youtube.com/watch?v={video_id}"
    return f"https://www.youtube.com/watch?v={video_id}"


def download_youtube_candidate(
    candidate, output_path, progress_hook=None, skip_check=None,
):
    if skip_check and skip_check():
        return {"skipped": True}

    config = load_config()
    audio_format = config.get("audio_format", "mp3")
    audio_quality = str(config.get("audio_quality", "320"))
    normalize_audio = bool(config.get("audio_normalize", False))
    download_url = candidate["url"]
    display_url = _candidate_display_url(candidate)

    # Ordered from strictest to broadest. Trailing "" lets yt-dlp pick
    # its default, covering HLS-only / live / unusual streams.
    if audio_format == "m4a":
        format_selectors = [
            "bestaudio[ext=m4a]/bestaudio[acodec^=mp4a]",
            "bestaudio",
            "bestaudio/best",
            "bestaudio*[acodec!=none]/best*[acodec!=none]",
            "best",
            "",
        ]
    elif audio_format == "opus":
        format_selectors = [
            "bestaudio[acodec=opus]/bestaudio[ext=webm]",
            "bestaudio",
            "bestaudio/best",
            "bestaudio*[acodec!=none]/best*[acodec!=none]",
            "best",
            "",
        ]
    else:
        format_selectors = [
            "bestaudio/best",
            "bestaudio",
            "bestaudio*[acodec!=none]/best*[acodec!=none]",
            "best",
            "",
        ]

    # Optional user override (e.g. "141" for the 256 kbps AAC stream on
    # Premium accounts — issue #58). Tried first, then the built-in
    # selectors as a fallback so a download still succeeds when the
    # requested format isn't available for a given video.
    custom_format = (config.get("ytdlp_format") or "").strip()
    if custom_format and custom_format not in format_selectors:
        format_selectors = [custom_format] + format_selectors

    first_client = config.get("yt_player_client", "android")
    is_music = candidate.get("source") == "ytmusic"
    has_po_token = bool(
        (config.get("yt_po_token") or "").strip()
        or (config.get("yt_pot_provider_url") or "").strip()
    )
    clients_to_try = []
    # Music clients hit the YouTube Music InnerTube endpoint, honor
    # cookies more reliably than bare android, and expose higher-tier
    # audio. For ytmusic-sourced candidates they take priority over
    # the user-configured default.
    if is_music:
        clients_to_try.extend(["web_music", "android_music", "ios_music"])
    # PO tokens (manual or via the bgutil provider) are only honored by the
    # web-family clients, so when one is configured try web before the
    # default (e.g. android) — otherwise the first attempt, on a client that
    # ignores the token, just wastes it and reports "format not available".
    if has_po_token:
        for c in ("web", "web_music"):
            if c not in clients_to_try:
                clients_to_try.append(c)
    if first_client and first_client not in clients_to_try:
        clients_to_try.append(first_client)
    for alt in ["web", "ios", "web_creator", "tv_embedded"]:
        if alt not in clients_to_try:
            clients_to_try.append(alt)
    clients_to_try.append(None)

    # Selector-outer / client-inner: ``android`` often only sees the
    # combined 360p mp4 (22k audio) while ``web`` exposes the 130k DASH
    # m4a. The obvious "exhaust selectors per client" order would
    # download the 22k stream from android and never try web.
    last_err = None
    any_403 = False
    format_unavailable_errors = 0
    extract_pp = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_format,
            "preferredquality": audio_quality,
        }
    ]
    for sel_idx, selector in enumerate(format_selectors):
        pp_variants = [extract_pp]
        if sel_idx >= len(format_selectors) - 2:
            pp_variants.append(None)
        for postprocessors in pp_variants:
            for pc in clients_to_try:
                if skip_check and skip_check():
                    return {"skipped": True}
                ydl_opts_download = {
                    **_build_common_opts(player_client=pc),
                    "outtmpl": output_path,
                }
                if postprocessors:
                    ydl_opts_download["postprocessors"] = postprocessors
                    if normalize_audio:
                        # EBU R128 loudness normalization on the ffmpeg
                        # extract step (forces a re-encode; only when the
                        # user opts in). Targets the streaming-loudness
                        # standard of -14 LUFS.
                        ydl_opts_download["postprocessor_args"] = [
                            "-af",
                            "loudnorm=I=-14:TP=-1.5:LRA=11",
                        ]
                if selector:
                    ydl_opts_download["format"] = selector
                # Streams without abr/asr metadata get rejected by
                # format_sort, so drop it on the last two fallbacks.
                if sel_idx >= len(format_selectors) - 2:
                    ydl_opts_download.pop("format_sort", None)
                if progress_hook:
                    ydl_opts_download["progress_hooks"] = [progress_hook]
                try:
                    with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_dl:
                        ydl_dl.download([download_url])
                    logger.info(
                        "Downloaded '%s' via player_client=%s",
                        candidate["title"], pc or "default",
                    )
                    return {
                        "success": True,
                        "youtube_url": display_url,
                        "youtube_title": candidate["title"],
                        "match_score": round(candidate["score"], 4),
                        "duration_seconds": int(candidate["duration"]),
                    }
                except Exception as e:
                    last_err = e
                    msg = str(e)
                    msg_low = msg.lower()
                    if "403" in msg:
                        any_403 = True
                        logger.debug(
                            f"   403 with player_client={pc or 'default'};"
                            " ensure cookies are provided"
                            " (YT_COOKIES_FILE) and try again"
                        )
                        continue
                    if (
                        "requested format is not available" in msg_low
                        or "no video formats" in msg_low
                    ):
                        format_unavailable_errors += 1
                        logger.debug(
                            "   Format '%s' not available with player_client=%s;"
                            " trying next client/selector",
                            selector, pc or "default",
                        )
                        continue
                    logger.debug(
                        f"   Failed with player_client={pc or 'default'}"
                        f" selector='{selector}'; {msg[:180]}"
                    )
                    continue

    if last_err:
        # Surface PO-token state on failure so users can tell whether a
        # configured token/provider was actually in play (issue #64).
        logger.info(
            "Failed to download '%s' after trying clients %s"
            " (po_token=%s, %d format-unavailable, %s403)",
            candidate["title"],
            [c or "default" for c in clients_to_try],
            "yes" if has_po_token else "no",
            format_unavailable_errors,
            "" if any_403 else "no ",
        )

    last_error_msg = str(last_err)[:120] if last_err else "Unknown error"
    if any_403:
        return {
            "success": False,
            "error_message": (
                "HTTP 403 Forbidden"
                " - try providing/refreshing YouTube cookies"
            ),
        }
    # Only blame format gating when the *final* attempt was a format error,
    # so an earlier hiccup doesn't mis-report an unrelated last failure.
    if format_unavailable_errors and (
        "requested format is not available" in last_error_msg.lower()
        or "no video formats" in last_error_msg.lower()
    ):
        return {
            "success": False,
            "error_message": (
                "No downloadable audio format available for this video."
                " YouTube is likely gating formats behind sign-in — upload a"
                " cookies.txt (Settings → YouTube cookies) and keep yt-dlp"
                " updated."
            ),
        }
    return {
        "success": False,
        "error_message": f"Download failed after all attempts: {last_error_msg}",
    }


def download_track_youtube(
    query, output_path, track_title_original,
    expected_duration_ms=None, progress_hook=None, skip_check=None,
    banned_urls=None,
):
    """Search YouTube and download the best matching track as MP3.

    Args:
        query: Search query string (typically "Artist Track official audio").
        output_path: Output file path template (without .mp3 extension).
        track_title_original: Original track title for scoring.
        expected_duration_ms: Expected duration in milliseconds, or None.
        progress_hook: Optional callback for yt-dlp progress events.
        skip_check: Optional callable; if it returns True, abort and return
            {"skipped": True}.
        banned_urls: Optional set of YouTube URLs to exclude from candidates.

    Returns:
        Dict with result info on success/failure, or {"skipped": True}.
    """
    candidates = search_youtube_candidates(
        query, track_title_original, expected_duration_ms, skip_check,
        banned_urls,
    )
    if not candidates:
        if skip_check and skip_check():
            return {"skipped": True}
        return {
            "success": False,
            "error_message": (
                "No suitable YouTube match found"
                " (filtered by duration/forbidden words)"
            ),
        }

    if skip_check and skip_check():
        return {"skipped": True}
    best = candidates[0]
    logger.info(
        f"   Best match: '{best['title']}'"
        f" (score={best['score']:.2f},"
        f" duration={int(best['duration'])}s,"
        f" channel='{best.get('channel', '')}')"
    )

    last_error = "Download failed after all candidates"
    for candidate in candidates:
        result = download_youtube_candidate(
            candidate, output_path, progress_hook, skip_check,
        )
        if result.get("skipped"):
            return result
        if result.get("success"):
            return result
        last_error = result.get("error_message", "unknown")
        logger.debug(
            "   Failed to download '%s': %s", candidate["title"], last_error
        )

    return {"success": False, "error_message": last_error}
