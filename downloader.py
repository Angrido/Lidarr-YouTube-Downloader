"""YouTube search, scoring, and download via yt-dlp.

Provides download_track_youtube() which searches YouTube for a track,
scores candidates by title similarity, duration, channel, and view count,
then downloads the best match as MP3.
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


def _title_similarity(yt_title, track_title, artist_name):
    """Score how well a YouTube title matches the expected track.

    Combines SequenceMatcher ratio with bonuses for containing
    the track title and artist name.

    Returns:
        Float between 0.0 and 1.0.
    """
    yt_lower = yt_title.lower()
    expected_lower = f"{artist_name} {track_title}".lower()
    score = SequenceMatcher(None, yt_lower, expected_lower).ratio()
    if track_title.lower() in yt_lower:
        score += 0.3
    if artist_name.lower() in yt_lower:
        score += 0.2
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


def _check_forbidden(yt_title_lower, track_title_lower, forbidden_list):
    """Check if a YouTube title contains a forbidden word.

    Multi-word forbidden terms use substring matching. Single words
    use word-boundary regex. Terms present in the original track
    title are allowed.

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


def _build_common_opts(player_client=None):
    """Build yt-dlp options dict from current config.

    Args:
        player_client: YouTube player client override (e.g. "android").

    Returns:
        Dict of yt-dlp options.
    """
    cfg = load_config()
    opts = {
        "quiet": True,
        "no_warnings": True,
        "retries": int(cfg.get("yt_retries", 10)),
        "fragment_retries": int(cfg.get("yt_fragment_retries", 10)),
        "sleep_interval_requests": int(cfg.get("yt_sleep_requests", 1)),
        "sleep_interval": int(cfg.get("yt_sleep_interval", 1)),
        "max_sleep_interval": int(cfg.get("yt_max_sleep_interval", 5)),
        "noplaylist": True,
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

    Returns:
        True on success, or an error message string on failure.
    """
    if skip_check and skip_check():
        return {"skipped": True}
    config = load_config()
    first_client = config.get("yt_player_client", "android") or None
    ydl_opts_search = {
        **_build_common_opts(player_client=first_client),
        "format": "bestaudio/best",
        "extract_flat": True,
    }

    candidates = []
    forbidden_words = config.get("forbidden_words", [
        "remix", "cover", "mashup", "bootleg", "live", "dj mix",
        "karaoke", "slowed", "reverb", "nightcore", "sped up",
        "instrumental", "acapella", "tribute",
    ])
    duration_tolerance = config.get("duration_tolerance", 10)

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
    search_queries = [query]
    base_track = track_title_original
    base_artist = query.replace(
        f" {track_title_original} official audio", ""
    ).replace(f" {track_title_original}", "").strip()
    if not base_artist:
        base_artist = artist_part

    alt_q = f"{base_artist} {base_track}"
    if alt_q != query and alt_q not in search_queries:
        search_queries.append(alt_q)

    alt_q2 = f"{base_track} {base_artist}"
    if alt_q2 not in search_queries:
        search_queries.append(alt_q2)

    alt_q3 = f"{base_track} audio"
    if alt_q3 not in search_queries:
        search_queries.append(alt_q3)

    for qi, sq in enumerate(search_queries):
        if skip_check and skip_check():
            return {"skipped": True}
        if candidates:
            break
        if qi > 0:
            logger.info(
                f"   Fallback search ({qi+1}/{len(search_queries)}):"
                f' "{sq}"'
            )
        try:
            with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                search_results = ydl.extract_info(
                    f"ytsearch15:{sq}", download=False
                )
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

                    if expected_duration_sec:
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
                    else:
                        if duration < 15 or duration > 7200:
                            continue
                        duration_score = 0.5

                    if banned_urls and url in banned_urls:
                        logger.debug(
                            "   Rejected '%s'"
                            " - URL banned by user",
                            entry.get("title", ""),
                        )
                        continue

                    title_score = _title_similarity(
                        entry.get("title", ""),
                        track_title_original, base_artist,
                    )
                    official_bonus = (
                        0.15 if _is_official_channel(channel, base_artist)
                        else 0.0
                    )
                    view_score = 0.0
                    if view_count > 0:
                        view_score = min(
                            0.1, math.log10(max(view_count, 1)) / 100
                        )
                    total_score = (
                        (duration_score * 0.35)
                        + (title_score * 0.40)
                        + official_bonus
                        + view_score
                    )

                    if url:
                        candidates.append({
                            "url": url,
                            "title": entry.get("title", ""),
                            "duration": duration,
                            "channel": channel,
                            "score": total_score,
                        })
                        logger.debug(
                            f"   Candidate '{entry.get('title', '')}'"
                            f" -- score={total_score:.2f}"
                            f" (dur={duration_score:.2f}"
                            f" title={title_score:.2f}"
                            f" official={official_bonus:.2f}"
                            f" views={view_score:.3f})"
                        )
        except Exception as e:
            logger.error(f'   Search failed for "{sq}": {e}')
            if qi == len(search_queries) - 1 and not candidates:
                return {
                    "success": False,
                    "error_message": f"Search failed: {str(e)[:120]}",
                }

    if not candidates:
        logger.warning(
            "   No suitable candidates found after all search attempts"
        )
        return {
            "success": False,
            "error_message": (
                "No suitable YouTube match found"
                " (filtered by duration/forbidden words)"
            ),
        }

    candidates.sort(key=lambda x: x["score"], reverse=True)
    if skip_check and skip_check():
        return {"skipped": True}
    best = candidates[0]
    logger.info(
        f"   Best match: '{best['title']}'"
        f" (score={best['score']:.2f},"
        f" duration={int(best['duration'])}s,"
        f" channel='{best.get('channel', '')}')"
    )

    last_err = None
    for candidate in candidates:
        clients_to_try = []
        first_client = config.get("yt_player_client", "android")
        if first_client:
            clients_to_try.append(first_client)
        for alt in ["web", "ios"]:
            if alt != first_client:
                clients_to_try.append(alt)
        clients_to_try.append(None)

        last_err = None
        for pc in clients_to_try:
            ydl_opts_download = {
                **_build_common_opts(player_client=pc),
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
                "outtmpl": output_path,
            }
            if progress_hook:
                ydl_opts_download["progress_hooks"] = [progress_hook]
            try:
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_dl:
                    ydl_dl.download([candidate["url"]])
                return {
                    "success": True,
                    "youtube_url": candidate["url"],
                    "youtube_title": candidate["title"],
                    "match_score": round(candidate["score"], 4),
                    "duration_seconds": int(candidate["duration"]),
                }
            except Exception as e:
                last_err = e
                msg = str(e)
                if "403" in msg:
                    logger.debug(
                        f"   403 with player_client={pc or 'default'};"
                        " ensure cookies are provided"
                        " (YT_COOKIES_FILE) and try again"
                    )
                else:
                    logger.debug(
                        f"   Failed with"
                        f" player_client={pc or 'default'};"
                        f" {msg[:180]}"
                    )
                continue
        if last_err:
            logger.debug(
                f"   Failed to download '{candidate['title']}'"
                " after trying multiple client profiles."
            )
        continue

    last_error_msg = str(last_err)[:120] if last_err else "Unknown error"
    if last_err and "403" in str(last_err):
        return {
            "success": False,
            "error_message": (
                "HTTP 403 Forbidden"
                " - try providing/refreshing YouTube cookies"
            ),
        }
    return {
        "success": False,
        "error_message": (
            f"Download failed after all attempts: {last_error_msg}"
        ),
    }
