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

from config import load_config

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
    if player_client:
        opts["extractor_args"] = {
            "youtube": {"player_client": [player_client]}
        }
    return opts


MAX_CANDIDATES = 10


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

    forbidden_words = list(config.get("forbidden_words", [
        "remix", "cover", "mashup", "bootleg", "live", "dj mix",
        "karaoke", "slowed", "reverb", "nightcore", "sped up",
        "instrumental", "acapella", "tribute", "8d audio",
    ]))
    for extra in config.get("forbidden_words_custom", []) or []:
        word = (extra or "").strip().lower()
        if word and word not in forbidden_words:
            forbidden_words.append(word)
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

                    # Topic channels host the canonical master, so the user's
                    # chosen album wins over any forbidden-word filter.
                    is_topic = _is_topic_channel(channel, base_artist)
                    if not is_topic:
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
                    if expected_duration_sec and duration_known:
                        min_dur = max(
                            15, expected_duration_sec - duration_tolerance
                        )
                        max_dur = expected_duration_sec + duration_tolerance
                        if duration < min_dur or duration > max_dur:
                            logger.debug(
                                f"   Rejected '{entry.get('title', '')}'"
                                f" - duration {int(duration)}s outside"
                                f" [{int(min_dur)}s - {int(max_dur)}s]"
                            )
                            continue
                        dur_diff = abs(duration - expected_duration_sec)
                        duration_score = max(
                            0, 1.0 - (dur_diff / max(duration_tolerance, 1))
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

                    title_score = _title_similarity(
                        entry.get("title", ""),
                        track_title_original, base_artist,
                    )
                    # Hard floor on title match. Without this the ytmusic
                    # "Topic-equivalent" bonus could push completely
                    # unrelated tracks (same artist, different song) past
                    # the score gate.
                    if title_score < 0.5:
                        logger.debug(
                            f"   Rejected '{entry.get('title', '')}'"
                            f" - title score {title_score:.2f} below 0.5"
                        )
                        continue
                    if kind == "ytmusic" or _is_topic_channel(channel, base_artist):
                        official_bonus = 0.45
                    elif _is_official_channel(channel, base_artist):
                        official_bonus = 0.30
                    else:
                        official_bonus = 0.0
                    if view_count > 0:
                        view_score = min(
                            0.1, math.log10(max(view_count, 1)) / 100
                        )
                    else:
                        view_score = 0.0
                    certainty_bonus = 0.15 if title_score >= 1.0 else 0.0
                    total_score = (
                        (duration_score * 0.25)
                        + (title_score * 0.50)
                        + official_bonus
                        + view_score
                        + certainty_bonus
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

    first_client = config.get("yt_player_client", "android")
    is_music = candidate.get("source") == "ytmusic"
    clients_to_try = []
    # Music clients hit the YouTube Music InnerTube endpoint, honor
    # cookies more reliably than bare android, and expose higher-tier
    # audio. For ytmusic-sourced candidates they take priority over
    # the user-configured default.
    if is_music:
        clients_to_try.extend(["web_music", "android_music", "ios_music"])
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
    for sel_idx, selector in enumerate(format_selectors):
        for pc in clients_to_try:
            if skip_check and skip_check():
                return {"skipped": True}
            ydl_opts_download = {
                **_build_common_opts(player_client=pc),
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": audio_format,
                        "preferredquality": audio_quality,
                    }
                ],
                "outtmpl": output_path,
            }
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
        logger.debug(
            f"   Failed to download '{candidate['title']}'"
            " after trying multiple client profiles."
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
    if format_unavailable_errors and "requested format" in last_error_msg.lower():
        return {
            "success": False,
            "error_message": (
                "No downloadable audio format available for this video."
                " YouTube may require cookies — set yt_cookies_file in settings."
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
