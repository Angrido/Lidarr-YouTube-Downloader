"""Manual download: one user-picked YouTube video for one album track.

``POST /api/download/manual`` retries a failed track (Lidarr album or a
negative-id playlist import); ``POST /api/album/<id>/track/manual-download``
downloads a track from the Library tracks dialog with live progress. Both
download, tag, optionally fingerprint, record and notify through
``_manual_dl_core``.
"""

import logging
import os
import shutil
import threading
import time
import uuid

from flask import Blueprint, jsonify, request

import models
from config import load_config
from downloader import download_youtube_candidate
from fingerprint import fingerprint_track
from lidarr import lidarr_request, tag_artist
from metadata import apply_file_extras, create_xml_metadata, tag_audio_file
from notifications import send_notifications
import vpn
from processing import download_process, inline_download, queue_lock
from utils import (
    BaseNotMountedError,
    _cleanup_temp_files,
    _json_object,
    _makedirs_bases_for,
    _resolve_write_base,
    _str_field,
    _synthetic_album_data,
    _validate_target_path,
    _validate_youtube_url,
    check_rate_limit,
    makedirs_safe,
    rate_limit_store,
    sanitize_filename,
    set_permissions,
)

logger = logging.getLogger(__name__)

bp = Blueprint("manual_download", __name__)

album_cache = {}
ALBUM_CACHE_TTL = 300


def _parse_track_number(raw):
    try:
        return int(raw)
    except (TypeError, ValueError, OverflowError):
        return 0


def _vpn_rotating_response():
    return jsonify({
        "success": False,
        "message": "The VPN is changing its IP; try again in a moment",
    }), 409


@bp.route("/api/download/manual", methods=["POST"])
def api_download_manual():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"manual_dl:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    if vpn.rotating():
        return _vpn_rotating_response()

    data = _json_object()
    youtube_url = _str_field(data, "youtube_url")
    track_title = _str_field(data, "track_title")
    track_num = _parse_track_number(data.get("track_num", 0))
    album_id_from_request = data.get("album_id")

    if not youtube_url or not track_title:
        return jsonify({"success": False, "message": "Missing required fields"}), 400

    youtube_url = _validate_youtube_url(youtube_url)
    if youtube_url is None:
        return jsonify({"success": False, "message": "Invalid YouTube URL"}), 400

    album_id_ctx = album_id_from_request or models.get_latest_download_album_id()
    if not album_id_ctx:
        return jsonify(
            {
                "success": False,
                "message": "No album context available. Please re-download the album first.",
            }
        ), 400
    try:
        album_id_ctx = int(album_id_ctx)
    except (TypeError, ValueError):
        return jsonify({"success": False, "message": "Invalid album id"}), 400

    # Playlist imports use a negative album_id and have no Lidarr album, so
    # their retry context comes from the stored track_downloads row instead
    # of Lidarr, and the track is re-downloaded into the same folder the
    # playlist used (issue #83).
    is_playlist_ctx = album_id_ctx < 0
    failed_ctx = models.get_failed_tracks_for_retry(album_id_ctx)
    config = load_config()

    if is_playlist_ctx:
        if not failed_ctx.get("album_title") and not failed_ctx.get(
            "artist_name"
        ):
            return jsonify(
                {
                    "success": False,
                    "message": "No album context available. Please re-download the album first.",
                }
            ), 400
        album_data = _synthetic_album_data(
            failed_ctx.get("album_title", ""),
            failed_ctx.get("artist_name", ""),
        )
        target_path = failed_ctx.get("album_path") or ""
        makedirs_bases = (
            _makedirs_bases_for(target_path, config) if target_path else []
        )
        lidarr_import_path = target_path
    else:
        album_data = lidarr_request(f"album/{album_id_ctx}")
        if "error" in album_data:
            return jsonify(
                {
                    "success": False,
                    "message": f"Failed to fetch album from Lidarr: {album_data['error']}",
                }
            ), 500

        if _resolve_write_base(config):
            target_path, makedirs_bases, lidarr_import_path = _build_album_paths(
                album_data, config,
            )
        else:
            lidarr_album_path_val = failed_ctx.get("lidarr_album_path", "")
            dl_album_path = failed_ctx.get("album_path", "")
            target_path = (
                lidarr_album_path_val
                if lidarr_album_path_val and os.path.isdir(lidarr_album_path_val)
                else dl_album_path
            )
            makedirs_bases = _makedirs_bases_for(target_path, config) if target_path else []
            lidarr_import_path = lidarr_album_path_val or target_path or ""

    if not target_path:
        return jsonify({"success": False, "message": "No album path available"}), 400

    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    try:
        makedirs_safe(target_path, makedirs_bases)
    except BaseNotMountedError as exc:
        return jsonify({
            "success": False,
            "message": (
                f"Configured path '{exc.base_dir}' is not mounted inside the container. "
                "Mount it as a volume or correct the path in settings."
            ),
        }), 400
    except PermissionError as exc:
        return jsonify({
            "success": False,
            "message": f"Permission denied creating {target_path}: {exc}",
        }), 500

    # This download runs in the request and holds no slot, so count it as
    # active (a VPN rotation or a restart waits for it), and only then look at
    # the rotation again: the check at the top may be stale by now.
    with inline_download():
        if vpn.rotating():
            return _vpn_rotating_response()
        return _execute_manual_download(
            youtube_url,
            track_title,
            track_num,
            target_path,
            album_data,
            album_id_ctx,
            failed_ctx,
            config,
            lidarr_album_path=lidarr_import_path,
        )


def _get_album_cached(album_id):
    now = time.time()
    if album_id in album_cache:
        cached, ts = album_cache[album_id]
        if now - ts < ALBUM_CACHE_TTL:
            return cached
    album = lidarr_request(f"album/{album_id}")
    # Errors are remembered for 30 s so a down Lidarr isn't re-asked on
    # every one-second queue refresh.
    album_cache[album_id] = (
        album, now if "error" not in album else now - ALBUM_CACHE_TTL + 30,
    )
    return album


def _artist_folder_from_album(album_data):
    """Match processing.py: use Lidarr's artist.path basename when available."""
    artist_name = album_data.get("artist", {}).get("artistName", "") or "Unknown"
    lidarr_artist_path_field = album_data.get("artist", {}).get("path", "") or ""
    lidarr_artist_folder = os.path.basename(
        lidarr_artist_path_field.rstrip("/\\")
    )
    return lidarr_artist_folder or sanitize_filename(artist_name)


def _album_folder_from_album(album_data):
    album_title = album_data.get("title", "Unknown")
    release_year = str(album_data.get("releaseDate", ""))[:4]
    sanitized_album = sanitize_filename(album_title)
    if release_year:
        return f"{sanitized_album} ({release_year})"
    return sanitized_album


def _build_album_paths(album_data, config):
    """Return (target_path, makedirs_bases, lidarr_import_path)."""
    write_base = _resolve_write_base(config)
    if not write_base:
        return None, [], ""
    artist_folder = _artist_folder_from_album(album_data)
    album_folder = _album_folder_from_album(album_data)
    target_path = os.path.join(write_base, artist_folder, album_folder)
    makedirs_bases = _makedirs_bases_for(target_path, config)
    lidarr_path = config.get("lidarr_path", "")
    lidarr_import_path = target_path
    if lidarr_path:
        lidarr_import_path = os.path.join(
            lidarr_path, artist_folder, album_folder,
        )
    return target_path, makedirs_bases, lidarr_import_path


def _execute_manual_download(
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id_ctx,
    failed_ctx,
    config,
    lidarr_album_path="",
):
    return _execute_manual_dl(
        youtube_url=youtube_url,
        track_title=track_title,
        track_num=track_num,
        target_path=target_path,
        album_data=album_data,
        album_id=album_id_ctx,
        album_title=failed_ctx.get("album_title", "") or album_data.get("title", ""),
        artist_name=(
            failed_ctx.get("artist_name", "")
            or album_data.get("artist", {}).get("artistName", "")
        ),
        config=config,
        album_path=target_path,
        lidarr_album_path=lidarr_album_path,
        cover_url=failed_ctx.get("cover_url", ""),
    )


@bp.route("/api/album/<int:album_id>/track/manual-download", methods=["POST"])
def api_manual_track_download(album_id):
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"manual_track:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    if vpn.rotating():
        return _vpn_rotating_response()

    data = _json_object()
    youtube_url = _str_field(data, "youtube_url")
    track_title = _str_field(data, "track_title")
    track_num = _parse_track_number(data.get("track_number", 0))

    if not youtube_url or not track_title:
        return jsonify({"success": False, "message": "Missing required fields"}), 400

    youtube_url = _validate_youtube_url(youtube_url)
    if youtube_url is None:
        return jsonify({"success": False, "message": "Invalid YouTube URL"}), 400

    album_data = _get_album_cached(album_id)
    if "error" in album_data:
        return jsonify(
            {
                "success": False,
                "message": f"Failed to fetch album: {album_data['error']}",
            }
        ), 500

    artist_name = album_data.get("artist", {}).get("artistName", "Unknown")
    album_title = album_data.get("title", "Unknown")

    config = load_config()
    target_path, makedirs_bases, lidarr_import_path = _build_album_paths(
        album_data, config,
    )
    if not target_path:
        return jsonify(
            {"success": False, "message": "No download path configured"}
        ), 400

    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    cover_url = ""
    images = album_data.get("images", [])
    if images:
        cover_url = images[0].get("remoteUrl", "")

    def _run_manual_download():
        _execute_manual_dl_with_progress(
            youtube_url=youtube_url,
            track_title=track_title,
            track_num=track_num,
            target_path=target_path,
            album_data=album_data,
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            config=config,
            album_path=target_path,
            lidarr_album_path=lidarr_import_path,
            makedirs_bases=makedirs_bases,
            cover_url=cover_url,
        )

    threading.Thread(target=_run_manual_download, daemon=True).start()
    return jsonify({"success": True, "message": "Download queued"})


def _execute_manual_dl_with_progress(
    *,
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id,
    album_title,
    artist_name,
    config,
    album_path,
    lidarr_album_path,
    cover_url,
    makedirs_bases=None,
):
    if makedirs_bases is None:
        makedirs_bases = _makedirs_bases_for(target_path, config)
    for _ in range(300):
        if not download_process["active"]:
            break
        time.sleep(1)
    else:
        logger.warning(
            "Manual download timed out waiting for active download: %s",
            track_title,
        )
        return

    claimed = False
    try:
        with queue_lock:
            if download_process["active"]:
                logger.warning(
                    "Manual download aborted: another download became active: %s",
                    track_title,
                )
                return
            download_process["active"] = True
            claimed = True
            track_num = _parse_track_number(track_num)
            download_process["stop"] = False
            download_process["album_id"] = album_id
            download_process["album_title"] = album_title
            download_process["artist_name"] = artist_name
            download_process["cover_url"] = cover_url
            download_process["current_track_index"] = 0
            download_process["tracks"] = [
                {
                    "track_title": track_title,
                    "track_number": track_num,
                    "status": "downloading",
                    "youtube_url": youtube_url,
                    "youtube_title": "",
                    "progress_percent": "",
                    "progress_speed": "",
                    "error_message": "",
                    "skip": False,
                }
            ]

        try:
            makedirs_safe(target_path, makedirs_bases)
        except (BaseNotMountedError, PermissionError) as exc:
            logger.error("Manual download target unusable: %s", exc)
            track_state = download_process["tracks"][0]
            track_state["status"] = "failed"
            track_state["error_message"] = str(exc)
            return
        _do_manual_dl(
            youtube_url=youtube_url,
            track_title=track_title,
            track_num=track_num,
            target_path=target_path,
            album_data=album_data,
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            config=config,
            album_path=album_path,
            lidarr_album_path=lidarr_album_path,
            cover_url=cover_url,
        )
    finally:
        if claimed:
            with queue_lock:
                download_process["active"] = False
                download_process["tracks"] = []
                download_process["current_track_index"] = -1
                download_process["album_id"] = None
                download_process["album_title"] = ""
                download_process["artist_name"] = ""
                download_process["cover_url"] = ""


def _do_manual_dl(
    *,
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id,
    album_title,
    artist_name,
    config,
    album_path,
    lidarr_album_path,
    cover_url,
):
    track_state = download_process["tracks"][0]

    def on_status(status, **fields):
        track_state.update(fields)
        track_state["status"] = status

    def progress_hook(d):
        if d["status"] == "downloading":
            track_state["progress_percent"] = d.get("_percent_str", "0%").strip()
            track_state["progress_speed"] = d.get("_speed_str", "N/A").strip()

    ok, message, _http_status, _fp_data = _manual_dl_core(
        youtube_url=youtube_url,
        track_title=track_title,
        track_num=track_num,
        target_path=target_path,
        album_data=album_data,
        album_id=album_id,
        album_title=album_title,
        artist_name=artist_name,
        config=config,
        album_path=album_path,
        lidarr_album_path=lidarr_album_path,
        cover_url=cover_url,
        run_acoustid=bool(
            config.get("acoustid_enabled") and config.get("acoustid_api_key")
        ),
        on_status=on_status,
        progress_hook=progress_hook,
    )
    if not ok:
        track_state["status"] = "failed"
        track_state["error_message"] = message


def _execute_manual_dl(
    *,
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id,
    album_title,
    artist_name,
    config,
    album_path,
    lidarr_album_path,
    cover_url,
    run_acoustid=False,
):
    ok, message, http_status, fp_data = _manual_dl_core(
        youtube_url=youtube_url,
        track_title=track_title,
        track_num=track_num,
        target_path=target_path,
        album_data=album_data,
        album_id=album_id,
        album_title=album_title,
        artist_name=artist_name,
        config=config,
        album_path=album_path,
        lidarr_album_path=lidarr_album_path,
        cover_url=cover_url,
        run_acoustid=run_acoustid,
    )
    if not ok:
        return jsonify({"success": False, "message": message}), http_status
    response = {"success": True, "message": message}
    if fp_data:
        response["acoustid_score"] = fp_data.get("acoustid_score", 0.0)
        response["acoustid_recording_id"] = fp_data.get(
            "acoustid_recording_id",
            "",
        )
    return jsonify(response)


def _manual_dl_core(
    *,
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id,
    album_title,
    artist_name,
    config,
    album_path,
    lidarr_album_path,
    cover_url,
    run_acoustid,
    on_status=None,
    progress_hook=None,
):
    """Download, tag, verify and record one manually picked track.

    Returns ``(ok, message, http_status, fp_data)``. ``on_status(status,
    **fields)`` reports 'tagging', 'verifying' and 'done' to the progress UI.
    """
    def _report(status, **fields):
        if on_status:
            on_status(status, **fields)

    sanitized_track = sanitize_filename(track_title)
    if not sanitized_track:
        sanitized_track = "untitled"
    temp_file = os.path.join(target_path, f"temp_manual_{uuid.uuid4().hex[:8]}")
    audio_ext = config.get("audio_format", "mp3")
    final_file = os.path.join(
        target_path, f"{int(track_num):02d} - {sanitized_track}.{audio_ext}"
    )

    real_final = os.path.realpath(final_file)
    real_target = os.path.realpath(target_path)
    if not (real_final.startswith(real_target + os.sep) or real_final == real_target):
        logger.error(
            "Path containment violation: '%s' escapes target '%s'",
            real_final, real_target,
        )
        return False, "Invalid track filename", 400, {}

    # Route through the shared multi-client / multi-selector fallback
    # (cookies, PO tokens and the format override included) so a manual
    # retry succeeds where a single bestaudio/android attempt hits
    # "Requested format is not available" (issue #80).
    candidate = {
        "url": youtube_url,
        "title": track_title,
        "duration": 0,
        "score": 1.0,
        "source": "manual",
    }
    dl_result = download_youtube_candidate(
        candidate, temp_file, progress_hook=progress_hook,
    )
    if not dl_result.get("success"):
        msg = (dl_result.get("error_message") or "Download failed")[:200]
        logger.error("yt-dlp download failed for '%s': %s", track_title, msg)
        _cleanup_temp_files(temp_file)
        return False, msg, 500, {}
    youtube_title = dl_result.get("youtube_title", "") or track_title

    actual_file = temp_file + f".{audio_ext}"
    if not os.path.exists(actual_file):
        _cleanup_temp_files(temp_file)
        return False, "Download failed -- file not created", 500, {}

    _report("tagging", youtube_title=youtube_title)
    try:
        track_info = _resolve_track_info(
            track_title,
            track_num,
            album_data,
            album_id,
        )
        tag_audio_file(actual_file, track_info, album_data, None)

        if config.get("xml_metadata_enabled", True):
            create_xml_metadata(
                target_path,
                artist_name,
                album_title,
                int(track_num),
                track_title,
                album_data.get("foreignAlbumId", ""),
                album_data.get("artist", {}).get("foreignArtistId", ""),
            )

        fp_data = {}
        if run_acoustid:
            _report("verifying")
            fp_data = _run_manual_acoustid(config, actual_file)

        file_size = os.path.getsize(actual_file)
        shutil.move(actual_file, final_file)
        apply_file_extras(
            final_file, config, artist=artist_name, title=track_title,
            album=album_title, duration=dl_result.get("duration_seconds", 0),
        )
        set_permissions(final_file)
    except Exception as e:
        logger.error(
            "Post-download processing failed for '%s': %s",
            track_title,
            e,
            exc_info=True,
        )
        _cleanup_temp_files(temp_file)
        return False, str(e)[:200], 500, {}

    _report("done")

    _record_manual_download(
        album_id=album_id,
        album_title=album_title,
        artist_name=artist_name,
        track_title=track_title,
        track_num=track_num,
        youtube_url=youtube_url,
        youtube_title=youtube_title,
        album_path=album_path,
        lidarr_album_path=lidarr_album_path,
        cover_url=cover_url,
        fp_data=fp_data,
        file_size=file_size,
        source_format=dl_result.get("source_format", ""),
    )

    _refresh_lidarr_artist(album_data, track_title)

    return True, f"Track '{track_title}' downloaded successfully", 200, fp_data


def _record_manual_download(
    *,
    album_id,
    album_title,
    artist_name,
    track_title,
    track_num,
    youtube_url,
    youtube_title="",
    album_path,
    lidarr_album_path,
    cover_url,
    fp_data,
    file_size,
    source_format="",
):
    try:
        models.add_track_download(
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            track_title=track_title,
            track_number=int(track_num),
            success=True,
            error_message="",
            youtube_url=youtube_url,
            youtube_title=youtube_title or track_title,
            match_score=1.0,
            duration_seconds=0,
            album_path=album_path,
            lidarr_album_path=lidarr_album_path,
            cover_url=cover_url,
            acoustid_fingerprint_id=fp_data.get("acoustid_fingerprint_id", ""),
            acoustid_score=fp_data.get("acoustid_score", 0.0),
            acoustid_recording_id=fp_data.get("acoustid_recording_id", ""),
            acoustid_recording_title=fp_data.get("acoustid_recording_title", ""),
            source_format=source_format,
        )
    except Exception as db_err:
        logger.error(
            "Track downloaded but DB record failed for '%s': %s",
            track_title,
            db_err,
            exc_info=True,
        )
    try:
        models.add_log(
            log_type="manual_download",
            album_id=album_id or 0,
            album_title=album_title or "Unknown Album",
            artist_name=artist_name or "Unknown Artist",
            details=f"Manually downloaded track: {track_title} (from YouTube)",
            total_file_size=file_size,
        )
    except Exception as log_err:
        logger.error("Failed to add log for '%s': %s", track_title, log_err)

    _notify_manual_download(
        track_title=track_title,
        album_title=album_title,
        artist_name=artist_name,
        fp_data=fp_data,
        cover_url=cover_url,
        youtube_url=youtube_url,
        youtube_title=youtube_title,
    )

    logger.info("Manual download successful: %s", track_title)


def _notify_manual_download(
    *, track_title, album_title, artist_name, fp_data, cover_url="",
    youtube_url="", youtube_title="",
):
    """Send a notification summarizing a successful manual download.

    Manual downloads bypass the automated verify-retry loop. The 👤
    icon distinguishes them at a glance from automated downloads
    (⬇️ ✅ ⚠️ ❌ 📥). When ``cover_url`` is supplied, Telegram renders
    it via ``sendPhoto`` and Discord embeds it as a thumbnail. When
    ``youtube_url`` is supplied, a clickable link (using the video
    title as the label) is added to both channels.
    """
    from notifications import md2_escape, md2_link
    title_line = "👤 Manual Download"
    album_disp = album_title or "Unknown Album"
    artist_disp = artist_name or "Unknown Artist"
    score = 0.0
    if fp_data:
        try:
            score = float(fp_data.get("acoustid_score", 0.0) or 0.0)
        except (TypeError, ValueError):
            score = 0.0

    lines = [
        title_line,
        f"Track: {track_title}",
        f"Album: {album_disp}",
        f"Artist: {artist_disp}",
    ]
    md2_lines = [
        f"*{md2_escape(title_line)}*",
        f"*Track:* {md2_escape(track_title)}",
        f"*Album:* {md2_escape(album_disp)}",
        f"*Artist:* {md2_escape(artist_disp)}",
    ]
    fields = []
    if score > 0:
        score_str = f"{score:.2f}"
        lines.append(f"AcoustID: {score_str}")
        md2_lines.append(f"*AcoustID:* {md2_escape(score_str)}")
        fields.append({
            "name": "AcoustID",
            "value": score_str,
            "inline": True,
        })
    if youtube_url:
        yt_label = youtube_title or track_title or youtube_url
        lines.append(f"Source: {yt_label} — {youtube_url}")
        md2_lines.append(
            f"*Source:* {md2_link(yt_label, youtube_url)}"
        )
        fields.append({
            "name": "YouTube",
            "value": f"[{yt_label}]({youtube_url})",
            "inline": False,
        })
    embed = {
        "title": title_line,
        "description": (
            f"{artist_disp} — {album_disp} — {track_title}"
        ),
        "color": 0x9B59B6,
        "fields": fields,
    }
    if cover_url:
        embed["thumbnail"] = cover_url
    if youtube_url:
        embed["url"] = youtube_url
    try:
        send_notifications(
            "\n".join(lines),
            log_type="manual_download",
            embed_data=embed,
            telegram_message="\n".join(md2_lines),
            telegram_parse_mode="MarkdownV2",
            photo_url=cover_url or None,
        )
    except Exception as exc:
        # Notifications must never break the manual-download flow;
        # logging here is intentional and the only side effect.
        logger.warning(
            "Manual download notification failed for '%s': %s",
            track_title, exc,
        )


def _resolve_track_info(track_title, track_num, album_data, album_id):
    track_info = {"title": track_title, "trackNumber": track_num}
    tracks = album_data.get("tracks", [])
    # A playlist import (album_id <= 0) has no Lidarr album to query.
    if not tracks and int(album_id or 0) > 0:
        tracks_res = lidarr_request(f"track?albumId={album_id}")
        if isinstance(tracks_res, list):
            tracks = tracks_res
        else:
            logger.warning(
                "Could not fetch tracks from Lidarr for album %s",
                album_id,
            )
    for t in tracks:
        if t.get("title", "").lower() == track_title.lower():
            track_info = t
            break
    return track_info


def _run_manual_acoustid(config, filepath):
    acoustid_api_key = config.get("acoustid_api_key", "")
    if not config.get("acoustid_enabled") or not acoustid_api_key:
        return {}
    fp_result = fingerprint_track(filepath, acoustid_api_key)
    return fp_result or {}


def _refresh_lidarr_artist(album_data, track_title):
    artist_id = album_data.get("artist", {}).get("id")
    if not artist_id:
        logger.warning(
            "No artist_id for album -- skipping Lidarr refresh after '%s'",
            track_title,
        )
        return
    result = lidarr_request(
        "command",
        method="POST",
        data={"name": "RefreshArtist", "artistId": artist_id},
    )
    if isinstance(result, dict) and "error" in result:
        logger.warning(
            "Lidarr RefreshArtist failed after manual download of '%s': %s",
            track_title,
            result["error"],
        )
    try:
        tag_artist(artist_id)
    except Exception as e:
        logger.warning("Lidarr tag step failed: %s", e)
