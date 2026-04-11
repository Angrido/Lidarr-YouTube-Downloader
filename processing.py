"""Download processing: album downloads, queue management, progress tracking.

Manages the active download process state, orchestrates per-album
downloads (search, download, tag, import to Lidarr), and processes
the download queue.
"""

import copy
import logging
import os
import shutil
import threading
import time
import uuid

import models
from config import load_config
from models import CandidateOutcome
from downloader import (
    download_youtube_candidate,
    search_youtube_candidates,
)
from fingerprint import fingerprint_track, verify_fingerprint
from lidarr import get_valid_release_id, lidarr_request
from metadata import (
    create_xml_metadata,
    tag_audio_file,
    get_itunes_artwork,
    get_itunes_tracks,
)
from notifications import (
    build_musicbrainz_link,
    md2_link,
    md2_escape,
    send_notifications,
)
from utils import sanitize_filename, set_permissions

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

download_process = {
    "active": False,
    "stop": False,
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "cover_url": "",
    "tracks": [],
    "current_track_index": -1,
}

queue_lock = threading.Lock()


def _new_verify_stats():
    """Initial mutable container for per-album AcoustID telemetry.

    Tracked under ``_results_lock`` while ``_download_tracks`` runs and
    surfaced in success/failure notifications. ``best_rejected_score``
    is the highest AcoustID confidence among *rejected* candidates,
    which lets a user tell "no fingerprint hit at all" apart from
    "the closest match was 0.62 — almost the right track".
    """
    return {
        "verified_count": 0,
        "accepted_acoustid_scores": [],
        "mismatch_count": 0,
        "best_rejected_score": 0.0,
    }


def _send_album_notification(
    *, log_type, title, color, artist_name, album_title,
    album_mbid="", cover_url="", fields=None, extra_md2_lines=None,
    disable_notification=False,
):
    """Send a structured album notification to all channels.

    Builds parallel Telegram (MarkdownV2) and Discord (embed) payloads
    from the same set of fields so the two channels stay in sync. The
    Telegram body uses MarkdownV2 with inline links to Lidarr and
    MusicBrainz when ``album_mbid`` is available; Discord receives the
    cover URL as the embed thumbnail and the same deep link as the
    embed ``url``.

    Args:
        log_type: Notification filter key matching ``*_log_types`` in
            config.
        title: Headline shown as the Discord embed title and the
            bold first line of the Telegram message.
        color: Discord embed color (24-bit int).
        artist_name: Album artist (will be MD2-escaped).
        album_title: Album title (will be MD2-escaped).
        album_mbid: MusicBrainz release-group MBID; used to build
            Lidarr / MusicBrainz deep links when present.
        cover_url: Optional cover artwork URL.
        fields: Optional list of Discord embed field dicts.
        extra_md2_lines: Optional pre-escaped MarkdownV2 lines appended
            to the Telegram body. Callers are responsible for escaping
            any literal MD2 specials.
        disable_notification: If true, deliver Telegram silently.
    """
    plain_lines = [
        title,
        f"Album: {album_title}",
        f"Artist: {artist_name}",
    ]
    md2_lines = [
        f"*{md2_escape(title)}*",
        f"*Album:* {md2_escape(album_title)}",
        f"*Artist:* {md2_escape(artist_name)}",
    ]
    if extra_md2_lines:
        md2_lines.extend(extra_md2_lines)

    mb_link = build_musicbrainz_link(album_mbid)
    if mb_link:
        md2_lines.append(mb_link)

    embed_data = {
        "title": title,
        "description": f"{artist_name} — {album_title}",
        "color": color,
    }
    if cover_url:
        embed_data["thumbnail"] = cover_url
    if fields:
        embed_data["fields"] = fields
    if album_mbid:
        embed_data["url"] = (
            f"https://musicbrainz.org/release-group/{album_mbid}"
        )

    send_notifications(
        "\n".join(plain_lines),
        log_type=log_type,
        embed_data=embed_data,
        telegram_message="\n".join(md2_lines),
        telegram_parse_mode="MarkdownV2",
        photo_url=cover_url or None,
        disable_notification=disable_notification,
    )


class TrackSkippedException(Exception):
    """Raised from yt-dlp progress hook when track skip is requested."""


def _make_progress_hook(idx):
    """Create a yt-dlp progress hook bound to a specific track index."""

    def hook(d):
        if d["status"] == "downloading":
            if 0 <= idx < len(download_process["tracks"]):
                track = download_process["tracks"][idx]
                track["status"] = "downloading"
                track["progress_percent"] = (
                    d.get("_percent_str", "0%").strip()
                )
                track["progress_speed"] = (
                    d.get("_speed_str", "N/A").strip()
                )
                if track.get("skip"):
                    logger.debug(
                        "Skip flag detected for track %d: %s",
                        idx, track.get("track_title", ""),
                    )
                    raise TrackSkippedException()

    return hook


def get_download_status():
    """Return a snapshot of the current download process state."""
    with queue_lock:
        snapshot = dict(download_process)
        snapshot["tracks"] = copy.deepcopy(download_process["tracks"])
        return snapshot


def stop_download():
    """Signal the active download to stop and clear the queue."""
    with queue_lock:
        download_process["stop"] = True
        for track in download_process.get("tracks", []):
            if track.get("status") in (
                "pending", "searching", "downloading", "verifying",
            ):
                track["skip"] = True
        models.clear_queue()


def process_album_download(album_id, force=False):
    """Download all tracks for an album and import into Lidarr.

    Args:
        album_id: Lidarr album ID to download.
        force: If True, re-download tracks that already exist.

    Returns:
        Dict with "success", "error", or "stopped" key.
    """
    with queue_lock:
        if download_process["active"]:
            return {"error": "Busy"}
        download_process["active"] = True
        download_process["stop"] = False
        download_process["result_success"] = True
        download_process["result_partial"] = False
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = album_id
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["cover_url"] = ""

    failed_tracks = []
    album = {}
    album_title = ""
    artist_name = ""
    album_path = ""
    cover_data = None
    lidarr_album_path = ""
    total_downloaded_size = 0
    album_mbid = ""

    try:
        album = lidarr_request(f"album/{album_id}")
        if "error" in album:
            logger.error(
                f"Error fetching album {album_id}: {album['error']}"
            )
            return album

        logger.info(
            f"Starting download for album:"
            f" {album.get('title', 'Unknown')}"
            f" - {album.get('artist', {}).get('artistName', 'Unknown')}"
        )

        tracks = album.get("tracks", [])
        if not tracks:
            tracks_res = lidarr_request(f"track?albumId={album_id}")
            if isinstance(tracks_res, dict) and "error" in tracks_res:
                logger.warning(
                    "Lidarr track fetch for album %s failed: %s",
                    album_id, tracks_res["error"],
                )
            elif isinstance(tracks_res, list) and len(tracks_res) > 0:
                tracks = tracks_res

        if not tracks:
            logger.info(
                "No tracks from Lidarr for album %s, using iTunes fallback",
                album_id,
            )
            tracks = get_itunes_tracks(
                album["artist"]["artistName"], album["title"]
            )

        album["tracks"] = tracks

        artist_name = album["artist"]["artistName"]
        artist_id = album["artist"]["id"]
        artist_mbid = album["artist"].get("foreignArtistId", "")
        album_title = album["title"]
        release_year = str(album.get("releaseDate", ""))[:4]
        album_type = album.get("albumType", "Album")

        download_process["album_title"] = album_title
        download_process["artist_name"] = artist_name
        download_process["cover_url"] = next(
            (
                img["remoteUrl"]
                for img in album.get("images", [])
                if img.get("coverType") == "cover"
            ),
            "",
        )

        release_id = get_valid_release_id(album)
        if release_id == 0:
            return {"error": "No valid releases found for this album."}

        album_mbid = album.get("foreignAlbumId", "")

        sanitized_artist = sanitize_filename(artist_name)
        sanitized_album = sanitize_filename(album_title)

        artist_path = os.path.join(DOWNLOAD_DIR, sanitized_artist)
        if release_year:
            album_folder_name = (
                f"{sanitized_album} ({release_year}) [{album_type}]"
            )
        else:
            album_folder_name = f"{sanitized_album} [{album_type}]"
        album_path = os.path.join(artist_path, album_folder_name)
        os.makedirs(album_path, exist_ok=True)

        # Fetch cover art before sending the download_started
        # notification so the artwork can render in Telegram (sendPhoto)
        # and Discord (embed thumbnail).
        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(cover_data)
        cover_url = download_process.get("cover_url", "")

        models.add_log(
            log_type="download_started",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Starting download of {len(tracks)} track(s)",
        )
        _send_album_notification(
            log_type="download_started",
            title="Download Started",
            color=0x3498DB,
            artist_name=artist_name,
            album_title=album_title,
            album_mbid=album_mbid,
            cover_url=cover_url,
            fields=[
                {"name": "Tracks", "value": str(len(tracks)),
                 "inline": True},
            ],
            extra_md2_lines=[
                f"*Tracks:* {md2_escape(len(tracks))}",
            ],
            disable_notification=True,
        )

        tracks_to_download = _filter_tracks(
            tracks, force, album_path,
        )

        if len(tracks_to_download) == 0:
            lidarr_request(
                "command",
                method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )
            return {"success": True, "message": "Skipped"}

        logger.info(f"Total tracks to download: {len(tracks_to_download)}")

        album_ctx = {
            "artist_name": artist_name,
            "album_title": album_title,
            "album_id": album_id,
            "album_mbid": album_mbid,
            "artist_mbid": artist_mbid,
            "cover_data": cover_data,
            "cover_url": download_process.get("cover_url", ""),
            "lidarr_album_path": lidarr_album_path,
        }
        download_process["tracks"] = [
            {
                "track_title": t["title"],
                "track_number": int(t.get("trackNumber", i + 1)),
                "status": "pending",
                "youtube_url": "",
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            }
            for i, t in enumerate(tracks_to_download)
        ]
        (
            failed_tracks, succeeded_tracks, total_downloaded_size,
            verify_stats,
        ) = _download_tracks(
            tracks_to_download, album_path, album, album_ctx,
        )

        set_permissions(artist_path)

        result = _handle_post_download(
            failed_tracks, succeeded_tracks,
            tracks_to_download, album_id,
            album_title, artist_name, total_downloaded_size,
            verify_stats=verify_stats,
            album_mbid=album_mbid,
            cover_url=download_process.get("cover_url", ""),
        )
        if result is not None:
            return result

        config = load_config()
        lidarr_path = config.get("lidarr_path", "")
        import_path, lidarr_album_path = _copy_to_lidarr(
            lidarr_path, album_path, sanitized_artist,
            album_folder_name,
        )

        logger.info(
            f"Album downloaded successfully:"
            f" {artist_name} - {album_title}"
        )

        _log_import_result(
            failed_tracks, album_id, album_title, artist_name,
            total_downloaded_size,
            album_mbid=album_mbid,
            cover_url=download_process.get("cover_url", ""),
        )

        lidarr_request(
            "command",
            method="POST",
            data={"name": "RefreshArtist", "artistId": artist_id},
        )

        if lidarr_path and os.path.exists(artist_path):
            try:
                logger.info(
                    f"Cleaning up download folder: {artist_path}"
                )
                shutil.rmtree(artist_path)
                logger.info("Download folder cleaned up successfully")
            except Exception as e:
                logger.warning(
                    f"Failed to cleanup download folder: {e}"
                )

        return {"success": True}

    except Exception as e:
        logger.error("Error during album download: %s", e, exc_info=True)
        _artist = download_process.get("artist_name", "Unknown")
        _album = download_process.get("album_title", "Unknown")
        _send_album_notification(
            log_type="album_error",
            title="Download Failed",
            color=0xE74C3C,
            artist_name=_artist,
            album_title=_album,
            album_mbid=album_mbid,
            cover_url=download_process.get("cover_url", ""),
            extra_md2_lines=[
                f"_Error:_ {md2_escape(str(e))}",
            ],
        )
        models.add_log(
            log_type="album_error",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Error: {e}",
        )
        download_process["result_success"] = False
        return {"error": str(e)}
    finally:
        with queue_lock:
            download_process["active"] = False
            download_process["tracks"] = []
            download_process["current_track_index"] = -1
            download_process["album_id"] = None
            download_process["album_title"] = ""
            download_process["artist_name"] = ""
            download_process["cover_url"] = ""


def _filter_tracks(tracks, force, album_path):
    """Filter tracks that need downloading."""
    audio_ext = load_config().get("audio_format", "mp3")
    tracks_to_download = []
    for t in tracks:
        if not force:
            if t.get("hasFile", False):
                continue
            try:
                track_num = int(t.get("trackNumber", 0))
            except (ValueError, TypeError):
                track_num = 0
            track_title = t["title"]
            sanitized_track = sanitize_filename(track_title)
            final_file = os.path.join(
                album_path, f"{track_num:02d} - {sanitized_track}.{audio_ext}"
            )
            if os.path.exists(final_file):
                continue
        tracks_to_download.append(t)
    return tracks_to_download


def _cleanup_temp_files(temp_file):
    """Remove temp download files for all common extensions."""
    for ext in [".mp3", ".opus", ".flac", ".aac", ".ogg", ".webm", ".m4a", ".part", ""]:
        tmp = temp_file + ext
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as rm_err:
                logger.debug(
                    "Failed to remove temp file %s: %s", tmp, rm_err,
                )


def _download_candidate_threaded(
    candidate, attempt_temp, progress_hook, skip_check, track_state,
):
    """Download a candidate in a background thread with skip support.

    Returns:
        (dl_result, actual_file) on success, or None on skip/failure.
    """
    dl_result_box = [None]
    dl_error_box = [None]

    def _run_dl(cand=candidate, tmp=attempt_temp):
        try:
            dl_result_box[0] = download_youtube_candidate(
                cand, tmp,
                progress_hook=progress_hook,
                skip_check=skip_check,
            )
        except TrackSkippedException:
            dl_error_box[0] = "skipped"
        except Exception as exc:
            dl_error_box[0] = exc

    dl_thread = threading.Thread(target=_run_dl, daemon=True)
    dl_thread.start()

    while dl_thread.is_alive():
        dl_thread.join(timeout=0.5)
        if dl_thread.is_alive() and skip_check():
            _cleanup_temp_files(attempt_temp)
            track_state["status"] = "skipped"
            return None

    if dl_error_box[0] == "skipped":
        _cleanup_temp_files(attempt_temp)
        track_state["status"] = "skipped"
        return None

    if isinstance(dl_error_box[0], Exception):
        logger.warning(
            "Download exception for '%s': %s",
            candidate.get("title", ""), dl_error_box[0],
        )
        _cleanup_temp_files(attempt_temp)
        return None

    dl_result = dl_result_box[0]
    if dl_result is None:
        logger.warning(
            "Download returned None for '%s'",
            candidate.get("title", ""),
        )
        _cleanup_temp_files(attempt_temp)
        return None

    if dl_result.get("skipped"):
        _cleanup_temp_files(attempt_temp)
        track_state["status"] = "skipped"
        return None

    if not dl_result.get("success"):
        _cleanup_temp_files(attempt_temp)
        return None

    audio_ext = load_config().get("audio_format", "mp3")
    actual_file = attempt_temp + f".{audio_ext}"
    if not os.path.exists(actual_file):
        logger.warning(
            "Download reported success but file not found: %s",
            actual_file,
        )
        return None

    return dl_result, actual_file


def _build_candidate_attempt(
    candidate, outcome, expected_recording_id,
    fp_data=None, error_message="",
):
    """Build a candidate attempt dict for buffering."""
    fp = fp_data or {}
    return {
        "youtube_url": candidate.get("url", ""),
        "youtube_title": candidate.get("title", ""),
        "match_score": candidate.get("score", 0.0),
        "duration_seconds": candidate.get("duration", 0),
        "outcome": outcome,
        "acoustid_matched_id": fp.get(
            "acoustid_recording_id", "",
        ),
        "acoustid_matched_title": fp.get(
            "acoustid_recording_title", "",
        ),
        "acoustid_score": fp.get("acoustid_score", 0.0),
        "expected_recording_id": expected_recording_id or "",
        "error_message": error_message,
        "timestamp": time.time(),
    }


def _accept_track_file(
    src_file, track_num, sanitized_track, dl_result, fp_data,
    *, track_state, track_title, album_path, album_ctx,
    candidate_attempts=None,
):
    """Accept a downloaded file: XML metadata, move, record in DB.

    Returns:
        Tuple of (file_size_bytes, track_download_id or None).
    """
    cfg = load_config()
    if cfg.get("xml_metadata_enabled", True):
        logger.info("Creating XML metadata file...")
        create_xml_metadata(
            album_path, album_ctx["artist_name"],
            album_ctx["album_title"], track_num, track_title,
            album_ctx["album_mbid"], album_ctx["artist_mbid"],
        )

    audio_ext = cfg.get("audio_format", "mp3")
    final_file = os.path.join(
        album_path,
        f"{track_num:02d} - {sanitized_track}.{audio_ext}",
    )
    try:
        file_size = os.path.getsize(src_file)
    except OSError:
        file_size = 0
    shutil.move(src_file, final_file)
    track_state["status"] = "done"

    try:
        track_download_id = models.add_track_download(
            album_id=album_ctx["album_id"],
            album_title=album_ctx["album_title"],
            artist_name=album_ctx["artist_name"],
            track_title=track_title,
            track_number=track_num, success=True,
            error_message="",
            youtube_url=dl_result.get("youtube_url", ""),
            youtube_title=dl_result.get("youtube_title", ""),
            match_score=dl_result.get("match_score", 0.0),
            duration_seconds=dl_result.get(
                "duration_seconds", 0,
            ),
            album_path=album_path,
            lidarr_album_path=album_ctx["lidarr_album_path"],
            cover_url=album_ctx["cover_url"],
            acoustid_fingerprint_id=fp_data.get(
                "acoustid_fingerprint_id", "",
            ),
            acoustid_score=fp_data.get("acoustid_score", 0.0),
            acoustid_recording_id=fp_data.get(
                "acoustid_recording_id", "",
            ),
            acoustid_recording_title=fp_data.get(
                "acoustid_recording_title", "",
            ),
        )
    except Exception:
        logger.error(
            "Failed to record track download for '%s' (album %d)",
            track_title, album_ctx["album_id"], exc_info=True,
        )
        return file_size, None

    if candidate_attempts and track_download_id is not None:
        try:
            models.flush_candidate_attempts(
                track_download_id, candidate_attempts,
            )
        except Exception:
            logger.error(
                "Failed to flush candidate attempts for '%s'"
                " (track_download_id %d)",
                track_title, track_download_id, exc_info=True,
            )

    return file_size, track_download_id


def _record_track_failure(
    fail_reason, track_state, track_title, track_num,
    *, album_path, album_ctx, failed_tracks, _results_lock,
    candidate_attempts=None,
):
    """Record a track failure in state, failed_tracks list, and DB."""
    track_state["status"] = "failed"
    track_state["error_message"] = fail_reason
    track_download_id = None
    with _results_lock:
        failed_tracks.append({
            "title": track_title,
            "reason": fail_reason,
            "track_num": track_num,
            "track_download_id": track_download_id,
        })
    try:
        track_download_id = models.add_track_download(
            album_id=album_ctx["album_id"],
            album_title=album_ctx["album_title"],
            artist_name=album_ctx["artist_name"],
            track_title=track_title,
            track_number=track_num, success=False,
            error_message=fail_reason,
            youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path=album_path,
            lidarr_album_path=album_ctx["lidarr_album_path"],
            cover_url=album_ctx["cover_url"],
        )
    except Exception:
        logger.error(
            "Failed to record track download for '%s' (album %d)",
            track_title, album_ctx["album_id"], exc_info=True,
        )
        return

    if candidate_attempts and track_download_id is not None:
        try:
            models.flush_candidate_attempts(
                track_download_id, candidate_attempts,
            )
        except Exception:
            logger.error(
                "Failed to flush candidate attempts for '%s'"
                " (track_download_id %d)",
                track_title, track_download_id, exc_info=True,
            )

    with _results_lock:
        for entry in failed_tracks:
            if (
                entry["track_num"] == track_num
                and entry["track_download_id"] is None
            ):
                entry["track_download_id"] = track_download_id
                break


def _download_tracks(
    tracks_to_download, album_path, album, album_ctx,
):
    """Download each track, tag, and create XML metadata.

    Uses a ThreadPoolExecutor with ``concurrent_tracks`` workers so
    multiple tracks download in parallel.

    Args:
        tracks_to_download: List of track dicts to download.
        album_path: Local directory for downloaded files.
        album: Full album data dict from Lidarr.
        album_ctx: Dict with keys: artist_name, album_title, album_id,
            album_mbid, artist_mbid, cover_data, cover_url,
            lidarr_album_path.

    Returns:
        Tuple of (failed_tracks list, total_downloaded_size int).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    artist_name = album_ctx["artist_name"]
    album_title = album_ctx["album_title"]
    album_id = album_ctx["album_id"]
    cover_data = album_ctx["cover_data"]

    failed_tracks = []
    succeeded_tracks = []
    total_downloaded_size = 0
    verify_stats = _new_verify_stats()
    _results_lock = threading.Lock()

    config = load_config()
    concurrent_tracks = max(
        1, min(5, config.get("concurrent_tracks", 2)),
    )

    def _process_single_track(idx, track):
        nonlocal total_downloaded_size

        track_state = download_process["tracks"][idx]
        download_process["current_track_index"] = idx
        track_title = track.get("title", f"Track {idx + 1}")
        try:
            track_num = int(track.get("trackNumber", idx + 1))
        except (ValueError, TypeError):
            track_num = idx + 1
        track_duration_ms = track.get("duration")
        expected_recording_id = track.get("foreignRecordingId")
        sanitized_track = sanitize_filename(track_title)

        def _skip_check():
            return (
                track_state.get("skip")
                or download_process.get("stop")
            )

        if _skip_check():
            track_state["status"] = "skipped"
            return

        progress_hook = _make_progress_hook(idx)
        track_state["status"] = "searching"

        try:
            banned_url_set = models.get_banned_urls_for_track(
                album_id, track_title,
            )
        except Exception:
            logger.warning(
                "Failed to fetch banned URLs for track '%s',"
                " proceeding without ban filter",
                track_title, exc_info=True,
            )
            banned_url_set = set()

        candidates = search_youtube_candidates(
            f"{artist_name} {track_title} official audio",
            track_title, track_duration_ms,
            skip_check=_skip_check, banned_urls=banned_url_set,
        )

        if not candidates:
            if _skip_check():
                track_state["status"] = "skipped"
                return
            _record_track_failure(
                "No suitable YouTube match found"
                " (filtered by duration/forbidden words)",
                track_state, track_title, track_num,
                album_path=album_path, album_ctx=album_ctx,
                failed_tracks=failed_tracks,
                _results_lock=_results_lock,
            )
            return

        cfg_loop = load_config()
        # load_config() already validates and clamps min_match_score; this
        # just guards against tests/callers that bypass it.
        try:
            min_match_score = float(cfg_loop.get("min_match_score", 0.8))
        except (TypeError, ValueError):
            min_match_score = 0.8
        will_verify = bool(
            cfg_loop.get("acoustid_enabled", True)
            and cfg_loop.get("acoustid_api_key", "")
            and expected_recording_id
        )
        # Reason verification is unavailable, used in rejection messages so
        # users can tell why the score gate is being enforced.
        if not will_verify:
            if not cfg_loop.get("acoustid_enabled", True):
                no_verify_reason = "AcoustID disabled"
            elif not cfg_loop.get("acoustid_api_key", ""):
                no_verify_reason = "AcoustID API key not set"
            elif not expected_recording_id:
                no_verify_reason = "no MusicBrainz recording id"
            else:
                no_verify_reason = "verification unavailable"
        else:
            no_verify_reason = ""

        best_unverified_candidate = None
        accepted = False
        any_downloaded = False
        any_low_score_skipped = False
        candidate_attempts_buf = []

        for ci, candidate in enumerate(candidates):
            if _skip_check():
                track_state["status"] = "skipped"
                return

            if (
                not will_verify
                and candidate.get("score", 0.0) < min_match_score
            ):
                candidate_attempts_buf.append(
                    _build_candidate_attempt(
                        candidate,
                        CandidateOutcome.REJECTED_LOW_SCORE,
                        expected_recording_id,
                        error_message=(
                            f"score {candidate.get('score', 0.0):.2f}"
                            f" < min_match_score {min_match_score:.2f}"
                            f" ({no_verify_reason})"
                        ),
                    )
                )
                logger.info(
                    "Rejected '%s' for '%s': score %.2f < min %.2f (%s)",
                    candidate.get("title", ""), track_title,
                    candidate.get("score", 0.0), min_match_score,
                    no_verify_reason,
                )
                any_low_score_skipped = True
                continue

            track_state["status"] = "downloading"
            attempt_temp = os.path.join(
                album_path,
                f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}",
            )

            dl_out = _download_candidate_threaded(
                candidate, attempt_temp, progress_hook,
                _skip_check, track_state,
            )
            if dl_out is None:
                candidate_attempts_buf.append(
                    _build_candidate_attempt(
                        candidate,
                        CandidateOutcome.DOWNLOAD_FAILED,
                        expected_recording_id,
                        error_message=track_state.get(
                            "error_message", "",
                        ),
                    )
                )
                if track_state["status"] == "skipped":
                    return
                continue
            dl_result, actual_file = dl_out

            any_downloaded = True
            track_state["status"] = "tagging"
            track_state["youtube_url"] = dl_result.get(
                "youtube_url", "",
            )
            track_state["youtube_title"] = dl_result.get(
                "youtube_title", "",
            )
            tag_audio_file(actual_file, track, album, cover_data)

            cfg = load_config()
            should_verify = (
                cfg.get("acoustid_enabled", True)
                and cfg.get("acoustid_api_key", "")
                and expected_recording_id
            )

            fp_data = {}

            if should_verify:
                track_state["status"] = "verifying"
                vresult = verify_fingerprint(
                    actual_file,
                    expected_recording_id,
                    cfg["acoustid_api_key"],
                )

                if vresult is None:
                    pass
                elif vresult["status"] == "verified":
                    fp_data = vresult["fp_data"]
                    candidate_attempts_buf.append(
                        _build_candidate_attempt(
                            candidate,
                            CandidateOutcome.VERIFIED,
                            expected_recording_id,
                            fp_data=fp_data,
                        )
                    )
                    with _results_lock:
                        verify_stats["verified_count"] += 1
                        verify_stats[
                            "accepted_acoustid_scores"
                        ].append(
                            float(fp_data.get("acoustid_score", 0.0))
                        )
                elif vresult["status"] == "mismatch":
                    mismatch_fp = vresult["fp_data"]
                    with _results_lock:
                        verify_stats["mismatch_count"] += 1
                        mismatch_score = float(
                            mismatch_fp.get("acoustid_score", 0.0)
                        )
                        if (
                            mismatch_score
                            > verify_stats["best_rejected_score"]
                        ):
                            verify_stats[
                                "best_rejected_score"
                            ] = mismatch_score
                    mismatch_attempt = _build_candidate_attempt(
                        candidate,
                        CandidateOutcome.MISMATCH,
                        expected_recording_id,
                        fp_data=mismatch_fp,
                    )
                    mismatch_attempt["acoustid_matched_id"] = (
                        vresult.get("matched_id", "")
                    )
                    candidate_attempts_buf.append(
                        mismatch_attempt,
                    )
                    remaining = len(candidates) - ci - 1
                    next_msg = (
                        f"Trying next candidate"
                        f" ({ci + 2}/{len(candidates)})."
                        if remaining > 0
                        else "No more candidates."
                    )
                    logger.info(
                        "AcoustID verification failed for"
                        " '%s': expected=%s, got=%s"
                        " (score=%.2f). %s",
                        track_title,
                        expected_recording_id,
                        vresult["matched_id"],
                        mismatch_fp.get(
                            "acoustid_score", 0,
                        ),
                        next_msg,
                    )
                    _cleanup_temp_files(attempt_temp)
                    try:
                        models.add_banned_url(
                            youtube_url=candidate["url"],
                            youtube_title=candidate["title"],
                            album_id=album_id,
                            album_title=album_title,
                            artist_name=artist_name,
                            track_title=track_title,
                            track_number=track_num,
                        )
                    except Exception:
                        logger.warning(
                            "Failed to ban URL %s",
                            candidate["url"],
                            exc_info=True,
                        )
                    track_state["youtube_url"] = ""
                    track_state["youtube_title"] = ""
                    continue
                elif vresult["status"] == "unverified":
                    candidate_attempts_buf.append(
                        _build_candidate_attempt(
                            candidate,
                            CandidateOutcome.UNVERIFIED,
                            expected_recording_id,
                        )
                    )
                    logger.debug(
                        "AcoustID returned no data for '%s'"
                        " candidate '%s'",
                        track_title, candidate["title"],
                    )
                    if best_unverified_candidate is None:
                        best_unverified_candidate = candidate
                    _cleanup_temp_files(attempt_temp)
                    continue
            else:
                if cfg.get("acoustid_enabled", True):
                    api_key = cfg.get("acoustid_api_key", "")
                    if api_key:
                        track_state["status"] = "fingerprinting"
                        fp_result = fingerprint_track(
                            actual_file, api_key,
                        )
                        if fp_result:
                            fp_data = fp_result
                candidate_attempts_buf.append(
                    _build_candidate_attempt(
                        candidate,
                        CandidateOutcome.ACCEPTED_NO_VERIFY,
                        expected_recording_id,
                        fp_data=fp_data,
                    )
                )

            file_size, td_id = _accept_track_file(
                actual_file, track_num, sanitized_track,
                dl_result, fp_data,
                track_state=track_state,
                track_title=track_title,
                album_path=album_path,
                album_ctx=album_ctx,
                candidate_attempts=candidate_attempts_buf,
            )
            with _results_lock:
                total_downloaded_size += file_size
                succeeded_tracks.append({
                    "title": track_title,
                    "track_num": track_num,
                    "track_download_id": td_id,
                    "youtube_url": dl_result.get("youtube_url", ""),
                    "youtube_title": dl_result.get(
                        "youtube_title", ""
                    ),
                })
            accepted = True
            break

        low_score_fallback = False
        # Note: best_unverified_candidate is only ever assigned in the
        # AcoustID "unverified" branch (where no fingerprint match exists).
        # Mismatched candidates are banned via add_banned_url and never
        # become the fallback, so removing the old all_unverified gate is
        # safe — a known-bad URL cannot reach this path.
        if not accepted:
            if (
                best_unverified_candidate is not None
                and best_unverified_candidate.get("score", 0.0)
                < min_match_score
            ):
                candidate_attempts_buf.append(
                    _build_candidate_attempt(
                        best_unverified_candidate,
                        CandidateOutcome.REJECTED_LOW_SCORE,
                        expected_recording_id,
                        error_message=(
                            f"fallback score"
                            f" {best_unverified_candidate.get('score', 0.0):.2f}"
                            f" < min_match_score {min_match_score:.2f}"
                        ),
                    )
                )
                logger.info(
                    "Skipping unverified fallback for '%s':"
                    " best score %.2f < min %.2f",
                    track_title,
                    best_unverified_candidate.get("score", 0.0),
                    min_match_score,
                )
                low_score_fallback = True
                best_unverified_candidate = None
            if best_unverified_candidate:
                track_state["status"] = "downloading"
                fallback_temp = os.path.join(
                    album_path,
                    f"temp_{track_num:02d}"
                    f"_{uuid.uuid4().hex[:8]}",
                )
                fb_result = download_youtube_candidate(
                    best_unverified_candidate, fallback_temp,
                    progress_hook=progress_hook,
                    skip_check=_skip_check,
                )
                if fb_result.get("skipped"):
                    _cleanup_temp_files(fallback_temp)
                    track_state["status"] = "skipped"
                    return
                fb_file = fallback_temp + f".{load_config().get('audio_format', 'mp3')}"
                if (
                    fb_result.get("success")
                    and os.path.exists(fb_file)
                ):
                    candidate_attempts_buf.append(
                        _build_candidate_attempt(
                            best_unverified_candidate,
                            CandidateOutcome
                            .ACCEPTED_UNVERIFIED_FALLBACK,
                            expected_recording_id,
                        )
                    )
                    track_state["status"] = "tagging"
                    track_state["youtube_url"] = fb_result.get(
                        "youtube_url", "",
                    )
                    track_state["youtube_title"] = fb_result.get(
                        "youtube_title", "",
                    )
                    tag_audio_file(fb_file, track, album, cover_data)
                    file_size, td_id = _accept_track_file(
                        fb_file, track_num, sanitized_track,
                        fb_result, {},
                        track_state=track_state,
                        track_title=track_title,
                        album_path=album_path,
                        album_ctx=album_ctx,
                        candidate_attempts=(
                            candidate_attempts_buf
                        ),
                    )
                    with _results_lock:
                        total_downloaded_size += file_size
                        succeeded_tracks.append({
                            "title": track_title,
                            "track_num": track_num,
                            "track_download_id": td_id,
                            "youtube_url": fb_result.get(
                                "youtube_url", ""
                            ),
                            "youtube_title": fb_result.get(
                                "youtube_title", ""
                            ),
                        })
                    return
                else:
                    candidate_attempts_buf.append(
                        _build_candidate_attempt(
                            best_unverified_candidate,
                            CandidateOutcome.DOWNLOAD_FAILED,
                            expected_recording_id,
                            error_message=fb_result.get(
                                "error_message",
                                "file not found",
                            ),
                        )
                    )
                    logger.warning(
                        "Fallback re-download of '%s' failed"
                        " for '%s': %s",
                        best_unverified_candidate["title"],
                        track_title,
                        fb_result.get(
                            "error_message", "file not found",
                        ),
                    )
                    _cleanup_temp_files(fallback_temp)

            if low_score_fallback:
                fail_reason = (
                    f"Unverified fallback below"
                    f" min_match_score={min_match_score:.2f}"
                    f" (tried {len(candidates)} candidates)"
                )
            elif any_low_score_skipped and not any_downloaded:
                fail_reason = (
                    f"No candidate met min_match_score={min_match_score:.2f}"
                    f" ({no_verify_reason};"
                    f" tried {len(candidates)} candidates)"
                )
            elif not any_downloaded:
                fail_reason = (
                    "All candidate downloads failed"
                    f" (tried {len(candidates)} candidates)"
                )
            else:
                fail_reason = (
                    "AcoustID verification failed: no"
                    " candidate matched expected recording"
                    f" {expected_recording_id}"
                    f" (tried {len(candidates)} candidates)"
                )
            _record_track_failure(
                fail_reason,
                track_state, track_title, track_num,
                album_path=album_path, album_ctx=album_ctx,
                failed_tracks=failed_tracks,
                _results_lock=_results_lock,
                candidate_attempts=candidate_attempts_buf,
            )

    with ThreadPoolExecutor(max_workers=concurrent_tracks) as executor:
        futures = {
            executor.submit(_process_single_track, idx, track): idx
            for idx, track in enumerate(tracks_to_download)
        }
        for future in as_completed(futures):
            if download_process["stop"]:
                executor.shutdown(wait=False, cancel_futures=True)
                logger.warning("Download stopped by user")
                break
            try:
                future.result()
            except Exception as e:
                logger.warning("Track worker exception: %s", e)

    return (
        failed_tracks, succeeded_tracks, total_downloaded_size,
        verify_stats,
    )


def _format_failed_tracks_field(failed_tracks, *, limit=1024):
    """Format failed tracks with their reasons for an embed field.

    Each entry shows ``• <title> — <reason>``. Truncates to ``limit``
    characters so we never exceed Discord's per-field cap.
    """
    lines = []
    for ft in failed_tracks:
        reason = ft.get("reason", "") or "unknown error"
        lines.append(f"• {ft['title']} — {reason}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _format_failed_tracks_md2(failed_tracks):
    """MarkdownV2-escaped failed-track lines for the Telegram body."""
    lines = []
    for ft in failed_tracks:
        reason = ft.get("reason", "") or "unknown error"
        lines.append(
            f"• *{md2_escape(ft['title'])}* — "
            f"_{md2_escape(reason)}_"
        )
    return lines


def _format_youtube_links_field(succeeded_tracks, *, limit=1024):
    """Plain-text YouTube links for a Discord embed field.

    Each line shows ``• <youtube title> — <url>``. Tracks without a URL
    are skipped. Truncates to ``limit`` characters.
    """
    lines = []
    for st in succeeded_tracks:
        url = st.get("youtube_url", "")
        if not url:
            continue
        label = (
            st.get("youtube_title")
            or st.get("title")
            or url
        )
        lines.append(f"• {label} — {url}")
    text = "\n".join(lines)
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text


def _format_youtube_links_md2(succeeded_tracks):
    """MarkdownV2 lines of clickable YouTube links for Telegram.

    Each line renders the YouTube video title as the link label, with
    the underlying href pointing at the actual YouTube URL. Tracks
    without a URL are skipped.
    """
    lines = []
    for st in succeeded_tracks:
        url = st.get("youtube_url", "")
        if not url:
            continue
        label = (
            st.get("youtube_title")
            or st.get("title")
            or url
        )
        lines.append(f"• {md2_link(label, url)}")
    return lines


def _verify_summary_lines(verify_stats, verified_total):
    """Build (plain_field_value, md2_lines) describing AcoustID stats.

    Returns ``(None, [])`` when there is nothing interesting to report
    (no AcoustID activity at all).
    """
    if not verify_stats:
        return None, []
    verified = verify_stats.get("verified_count", 0)
    mismatches = verify_stats.get("mismatch_count", 0)
    best_rejected = verify_stats.get("best_rejected_score", 0.0)
    scores = verify_stats.get("accepted_acoustid_scores", [])

    if verified == 0 and mismatches == 0:
        return None, []

    parts = []
    if verified_total > 0:
        parts.append(f"{verified}/{verified_total} verified")
    if scores:
        avg = sum(scores) / len(scores)
        parts.append(f"avg {avg:.2f}")
    if mismatches:
        parts.append(f"{mismatches} auto-banned")
    if best_rejected > 0:
        parts.append(f"best rejected {best_rejected:.2f}")
    field_value = ", ".join(parts) if parts else None
    md2_lines = (
        [f"*AcoustID:* {md2_escape(field_value)}"]
        if field_value else []
    )
    return field_value, md2_lines


def _handle_post_download(
    failed_tracks, succeeded_tracks, tracks_to_download,
    album_id, album_title, artist_name, total_downloaded_size,
    *, verify_stats=None, album_mbid="", cover_url="",
):
    """Log and notify about download results.

    Returns:
        A result dict if download should stop (all failed), else None.
    """
    skipped_count = sum(
        1 for t in download_process.get("tracks", [])
        if t.get("status") == "skipped"
    )
    attempted_count = len(tracks_to_download) - skipped_count
    verified_total = attempted_count - len(failed_tracks)
    verify_field, verify_md2_lines = _verify_summary_lines(
        verify_stats, verified_total,
    )
    yt_links_field = _format_youtube_links_field(succeeded_tracks)
    yt_links_md2 = _format_youtube_links_md2(succeeded_tracks)

    if failed_tracks:
        failed_field = _format_failed_tracks_field(failed_tracks)
        failed_md2 = _format_failed_tracks_md2(failed_tracks)
        best_rejected = (
            verify_stats.get("best_rejected_score", 0.0)
            if verify_stats else 0.0
        )

        if attempted_count > 0 and len(failed_tracks) == attempted_count:
            extra_md2 = []
            extra_md2.extend(verify_md2_lines)
            if best_rejected > 0:
                extra_md2.append(
                    f"*Best rejected score:*"
                    f" {md2_escape(f'{best_rejected:.2f}')}"
                )
            extra_md2.append("*Failed tracks:*")
            extra_md2.extend(failed_md2)
            embed_fields = []
            if verify_field:
                embed_fields.append({
                    "name": "AcoustID",
                    "value": verify_field, "inline": False,
                })
            if best_rejected > 0:
                embed_fields.append({
                    "name": "Best rejected score",
                    "value": f"{best_rejected:.2f}",
                    "inline": True,
                })
            embed_fields.append({
                "name": "Failed Tracks",
                "value": failed_field, "inline": False,
            })
            _send_album_notification(
                log_type="album_error",
                title="Download Failed",
                color=0xE74C3C,
                artist_name=artist_name,
                album_title=album_title,
                album_mbid=album_mbid,
                cover_url=cover_url,
                fields=embed_fields,
                extra_md2_lines=extra_md2,
            )
            logger.error(
                f"All {len(failed_tracks)} tracks failed to download."
                " Skipping import."
            )
            models.add_log(
                log_type="album_error",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=(
                    f"All {attempted_count} track(s)"
                    " failed to download"
                ),
            )
            for ft in failed_tracks:
                try:
                    models.add_log(
                        log_type="track_failure",
                        album_id=album_id,
                        album_title=album_title,
                        artist_name=artist_name,
                        details=ft["reason"],
                        track_title=ft["title"],
                        track_number=ft["track_num"],
                        track_download_id=ft.get(
                            "track_download_id",
                        ),
                    )
                except Exception:
                    logger.warning(
                        "Failed to log track_failure for '%s'",
                        ft["title"], exc_info=True,
                    )
            download_process["result_success"] = False
            return {"error": "All tracks failed to download"}

        download_process["result_partial"] = True
        partial_extra = list(verify_md2_lines)
        if best_rejected > 0:
            partial_extra.append(
                f"*Best rejected score:*"
                f" {md2_escape(f'{best_rejected:.2f}')}"
            )
        partial_extra.append("*Failed tracks:*")
        partial_extra.extend(failed_md2)
        if yt_links_md2:
            partial_extra.append("*Downloaded from YouTube:*")
            partial_extra.extend(yt_links_md2)
        partial_fields = []
        if verify_field:
            partial_fields.append({
                "name": "AcoustID",
                "value": verify_field, "inline": False,
            })
        partial_fields.append({
            "name": "Failed Tracks",
            "value": failed_field, "inline": False,
        })
        if yt_links_field:
            partial_fields.append({
                "name": "YouTube Sources",
                "value": yt_links_field, "inline": False,
            })
        _send_album_notification(
            log_type="partial_success",
            title="Partial Download",
            color=0xE67E22,
            artist_name=artist_name,
            album_title=album_title,
            album_mbid=album_mbid,
            cover_url=cover_url,
            fields=partial_fields,
            extra_md2_lines=partial_extra,
        )
        logger.warning(
            f"Download completed with {len(failed_tracks)} failed"
            " tracks. Proceeding with import."
        )
        models.add_log(
            log_type="partial_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"{len(failed_tracks)} track(s) failed to download"
                f" out of {attempted_count}"
            ),
            total_file_size=total_downloaded_size,
        )
        for ft in failed_tracks:
            try:
                models.add_log(
                    log_type="track_failure",
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    details=ft["reason"],
                    track_title=ft["title"],
                    track_number=ft["track_num"],
                    track_download_id=ft.get(
                        "track_download_id",
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to log track_failure for '%s'",
                    ft["title"], exc_info=True,
                )
    else:
        models.add_log(
            log_type="download_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"Successfully downloaded"
                f" {attempted_count} track(s)"
            ),
            total_file_size=total_downloaded_size,
        )
        success_fields = [{
            "name": "Tracks",
            "value": f"{attempted_count}/{attempted_count}",
            "inline": True,
        }]
        if verify_field:
            success_fields.append({
                "name": "AcoustID",
                "value": verify_field, "inline": False,
            })
        success_extra = [
            f"*Tracks:* {md2_escape(f'{attempted_count}/{attempted_count}')}",
        ]
        success_extra.extend(verify_md2_lines)
        if yt_links_md2:
            success_extra.append("*Downloaded from YouTube:*")
            success_extra.extend(yt_links_md2)
        if yt_links_field:
            success_fields.append({
                "name": "YouTube Sources",
                "value": yt_links_field, "inline": False,
            })
        _send_album_notification(
            log_type="download_success",
            title="Download Successful",
            color=0x2ECC71,
            artist_name=artist_name,
            album_title=album_title,
            album_mbid=album_mbid,
            cover_url=cover_url,
            fields=success_fields,
            extra_md2_lines=success_extra,
        )
        logger.info("All tracks downloaded successfully")

    for st in succeeded_tracks:
        try:
            models.add_log(
                log_type="track_download",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details="Track downloaded successfully",
                track_title=st["title"],
                track_number=st["track_num"],
                track_download_id=st.get(
                    "track_download_id",
                ),
            )
        except Exception:
            logger.warning(
                "Failed to log track_download for '%s'",
                st["title"], exc_info=True,
            )

    return None


def _copy_to_lidarr(
    lidarr_path, album_path, sanitized_artist, album_folder_name,
):
    """Copy downloaded files to Lidarr music folder if configured.

    Returns:
        Tuple of (import_path, lidarr_album_path).
    """
    lidarr_album_path = ""
    if lidarr_path:
        abs_lidarr = os.path.abspath(lidarr_path)
        abs_download = os.path.abspath(DOWNLOAD_DIR)

        if abs_lidarr == abs_download:
            logger.warning(
                "LIDARR_PATH matches DOWNLOAD_PATH."
                " Skipping move to prevent data loss."
            )
            lidarr_path = ""
        else:
            logger.info(
                f"Moving files to Lidarr music folder: {lidarr_path}"
            )
        lidarr_artist_path = os.path.join(
            lidarr_path, sanitized_artist
        )
        lidarr_album_path = os.path.join(
            lidarr_artist_path, album_folder_name
        )

        try:
            os.makedirs(lidarr_album_path, exist_ok=True)
            for item in os.listdir(album_path):
                src = os.path.join(album_path, item)
                dst = os.path.join(lidarr_album_path, item)
                if os.path.isfile(src):
                    shutil.copy2(src, dst)
                    logger.info(f"  Copied: {item}")
            set_permissions(lidarr_artist_path)
            logger.info("Files copied to Lidarr folder successfully")
            return lidarr_album_path, lidarr_album_path
        except Exception as e:
            logger.error(
                "Error copying files to Lidarr folder: %s",
                e, exc_info=True,
            )
            send_notifications(
                f"Copy to Lidarr failed: {e}",
                log_type="album_error",
            )
            return album_path, lidarr_album_path
    return album_path, lidarr_album_path


def _log_import_result(
    failed_tracks, album_id, album_title, artist_name,
    total_downloaded_size, *, album_mbid="", cover_url="",
):
    """Log and notify about the Lidarr import result."""
    if failed_tracks:
        models.add_log(
            log_type="import_partial",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"Album imported with {len(failed_tracks)}"
                " failed tracks"
            ),
            total_file_size=total_downloaded_size,
        )
        _send_album_notification(
            log_type="import_partial",
            title="Import Partial",
            color=0xE67E22,
            artist_name=artist_name,
            album_title=album_title,
            album_mbid=album_mbid,
            cover_url=cover_url,
            fields=[{
                "name": "Missing Tracks",
                "value": str(len(failed_tracks)),
                "inline": True,
            }],
            extra_md2_lines=[
                f"_Refreshing in Lidarr_",
                f"*Missing tracks:* {md2_escape(len(failed_tracks))}",
            ],
        )
    else:
        models.add_log(
            log_type="import_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details="Album downloaded and refreshing in Lidarr",
            total_file_size=total_downloaded_size,
        )
        _send_album_notification(
            log_type="import_success",
            title="Import Successful",
            color=0x2ECC71,
            artist_name=artist_name,
            album_title=album_title,
            album_mbid=album_mbid,
            cover_url=cover_url,
            extra_md2_lines=[
                "_Refreshing in Lidarr_",
            ],
        )


def process_download_queue():
    """Continuously process the download queue in a loop.

    Pops the next album from the queue and starts a download thread.
    Sleeps 2 seconds between checks.
    """
    while True:
        try:
            if not download_process["active"]:
                next_album_id = models.pop_next_from_queue()
                if next_album_id is not None:
                    threading.Thread(
                        target=process_album_download,
                        args=(next_album_id, False),
                        daemon=True,
                    ).start()
        except Exception as e:
            logger.warning(f"Queue processor error: {e}")
        time.sleep(2)
