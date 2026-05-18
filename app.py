"""Flask application with thin route handlers.

All business logic lives in extracted modules. This file defines
routes, request parsing, and response formatting.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid

from flask import (
    Flask,
    Response,
    jsonify,
    render_template,
    request,
    send_from_directory,
)
from werkzeug.utils import secure_filename as werkzeug_secure_filename

import db
import models
from config import ALLOWED_CONFIG_KEYS, load_config, save_config
from downloader import get_ytdlp_version
from fingerprint import fingerprint_track
from lidarr import get_missing_albums, lidarr_request
from metadata import create_xml_metadata, get_itunes_tracks, tag_mp3, tag_audio_file
from notifications import send_notifications
from processing import (
    TrackSkippedException,
    download_process,
    get_download_status,
    process_album_download,
    process_download_queue,
    queue_lock,
    stop_download,
)
from scheduler import run_scheduler, setup_scheduler
from utils import check_rate_limit, format_bytes, sanitize_filename, set_permissions, makedirs_safe

logging.basicConfig(
    level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)

VERSION = "1.7.0"

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

rate_limit_store = {}
album_cache = {}
ALBUM_CACHE_TTL = 300


@app.context_processor
def inject_version():
    return {"APP_VERSION": VERSION}


@app.teardown_appcontext
def teardown_db(exception):
    db.close_db()


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/downloads")
def downloads():
    return render_template("downloads.html")


@app.route("/settings")
def settings():
    return render_template("settings.html")


@app.route("/logs")
def logs():
    return render_template("logs.html")


@app.route("/favicon.ico")
def favicon():
    return send_from_directory(
        os.path.join(app.root_path, "static"),
        "favicon.svg",
        mimetype="image/svg+xml",
    )


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(load_config())
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"config:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    current = load_config()
    incoming = request.json or {}
    for key, value in incoming.items():
        if key in ALLOWED_CONFIG_KEYS:
            current[key] = value
    save_config(current)
    return jsonify({"success": True})


@app.route("/api/config/export")
def api_config_export():
    config = load_config()
    config.pop("path_conflict", None)
    formatted = json.dumps(config, indent=2, ensure_ascii=False)
    response = Response(formatted, mimetype="application/json")
    response.headers["Content-Disposition"] = "attachment; filename=config.json"
    return response


@app.route("/api/config/import", methods=["POST"])
def api_config_import():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"config_import:{client_ip}", rate_limit_store, window=10, max_requests=2
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    if "file" in request.files:
        file = request.files["file"]
        try:
            content = file.read().decode("utf-8")
            incoming = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return jsonify({"success": False, "message": f"Invalid JSON: {e}"}), 400
    elif request.is_json:
        incoming = request.json
    else:
        return jsonify({"success": False, "message": "No config data provided"}), 400
    if not isinstance(incoming, dict):
        return jsonify(
            {"success": False, "message": "Config must be a JSON object"}
        ), 400
    current = load_config()
    applied_keys = []
    skipped_keys = []
    for key, value in incoming.items():
        if key in ALLOWED_CONFIG_KEYS:
            current[key] = value
            applied_keys.append(key)
        else:
            skipped_keys.append(key)
    save_config(current)
    return jsonify(
        {
            "success": True,
            "applied": len(applied_keys),
            "skipped": len(skipped_keys),
            "message": (
                f"Imported {len(applied_keys)} settings."
                f" {len(skipped_keys)} keys skipped."
            ),
        }
    )


@app.route("/api/test-connection")
def api_test_connection():
    system = lidarr_request("system/status")
    if "error" in system:
        return jsonify({"status": "error", "message": system["error"]})
    return jsonify(
        {
            "status": "success" if "version" in system else "error",
            "lidarr_version": system.get("version", "Unknown"),
        }
    )


@app.route("/api/missing-albums")
def api_missing_albums():
    return jsonify(get_missing_albums())


@app.route("/api/album/<int:album_id>")
def api_album_details(album_id):
    album = lidarr_request(f"album/{album_id}")
    if not album.get("tracks"):
        album["tracks"] = get_itunes_tracks(
            album["artist"]["artistName"], album["title"]
        )
    return jsonify(album)


@app.route("/api/ytdlp/version")
def api_ytdlp_version():
    return jsonify({"version": get_ytdlp_version()})


def _pip_update_ytdlp():
    old_version = get_ytdlp_version()
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            return None, None, result.stderr[-500:] if result.stderr else "pip failed"
        new_version = get_ytdlp_version()
        return old_version, new_version, None
    except subprocess.TimeoutExpired:
        return None, None, "Update timed out (120s)"
    except Exception as e:
        return None, None, str(e)


@app.route("/api/ytdlp/update", methods=["POST"])
def api_ytdlp_update():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"ytdlp_update:{client_ip}", rate_limit_store, window=60, max_requests=1
    ):
        return jsonify(
            {
                "success": False,
                "message": "Update already in progress or rate limited",
            }
        ), 429
    old_version, new_version, error = _pip_update_ytdlp()
    if error:
        return jsonify({"success": False, "message": error})
    updated = old_version != new_version
    return jsonify(
        {
            "success": True,
            "old_version": old_version,
            "new_version": new_version,
            "updated": updated,
            "restart_required": updated,
        }
    )


def _exec_restart():
    try:
        os.closerange(3, 65536)
    except Exception:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)


@app.route("/api/restart", methods=["POST"])
def api_restart():
    if download_process.get("active"):
        return jsonify(
            {
                "success": False,
                "message": "A download is in progress. Stop it before restarting.",
            }
        )

    def _do_restart():
        time.sleep(0.5)
        _exec_restart()

    threading.Thread(target=_do_restart, daemon=True).start()
    return jsonify({"success": True})


@app.route("/api/download/<int:album_id>", methods=["POST"])
def api_download(album_id):
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"download:{client_ip}", rate_limit_store):
        return jsonify(
            {
                "success": False,
                "message": "Too many requests, please slow down",
            }
        ), 429
    with queue_lock:
        current_id = download_process.get("album_id")
    if current_id == album_id:
        return jsonify({"success": False, "message": "Already in queue or downloading"})
    added = models.enqueue_album(album_id)
    if added:
        return jsonify({"success": True, "queued": True})
    return jsonify({"success": False, "message": "Already in queue or downloading"})


@app.route("/api/download/stop", methods=["POST"])
def api_download_stop():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"stop:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    stop_download()
    return jsonify({"success": True})


@app.route("/api/download/skip-track", methods=["POST"])
def api_skip_track():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"skip_track:{client_ip}",
        rate_limit_store,
        window=5,
        max_requests=10,
    ):
        return jsonify({"error": "Too many requests"}), 429
    data = request.json or {}
    track_index = data.get("track_index")
    if track_index is None:
        return jsonify({"error": "track_index required"}), 400
    if not isinstance(track_index, int):
        return jsonify({"error": "track_index must be an integer"}), 400
    with queue_lock:
        if not download_process["active"]:
            return jsonify({"error": "No active download"}), 409
        tracks = download_process.get("tracks", [])
        if track_index < 0 or track_index >= len(tracks):
            return jsonify({"error": "Invalid track_index"}), 400
        tracks[track_index]["skip"] = True
    return jsonify({"success": True})


@app.route("/api/download/status")
def api_download_status():
    return jsonify(get_download_status())


@app.route("/api/download/stream")
def api_download_stream():
    sse_timeout = 3600

    def generate():
        start_time = time.time()
        try:
            while True:
                if time.time() - start_time > sse_timeout:
                    break
                with queue_lock:
                    queue_rows = models.get_queue()
                    queue_data = []
                    for row in queue_rows:
                        album = _get_album_cached(row["album_id"])
                        if "error" not in album:
                            cover_url = ""
                            for img in album.get("images", []):
                                if img.get("coverType") == "cover":
                                    cover_url = img.get("remoteUrl", "")
                                    break
                            queue_data.append(
                                {
                                    "id": row["album_id"],
                                    "title": album.get("title", ""),
                                    "artist": album.get("artist", {}).get(
                                        "artistName", ""
                                    ),
                                    "cover_url": cover_url,
                                    "track_count": album.get("statistics", {}).get(
                                        "trackCount", 0
                                    ),
                                }
                            )
                    status = dict(download_process)
                    tracks = status.get("tracks", [])
                    total = len(tracks)
                    done_count = sum(
                        1 for t in tracks
                        if t.get("status") in ("done", "failed", "skipped")
                    )
                    downloading = [
                        t for t in tracks if t.get("status") == "downloading"
                    ]
                    active_track = downloading[0] if downloading else None
                    if active_track is None:
                        idx = status.get("current_track_index", -1)
                        if 0 <= idx < total:
                            active_track = tracks[idx]
                    status["current_track_title"] = (
                        active_track.get("track_title", "") if active_track else ""
                    )
                    overall_percent = (
                        round(done_count / total * 100) if total > 0 else 0
                    )
                    status["progress"] = {
                        "current": done_count + (1 if active_track else 0),
                        "total": total,
                        "overall_percent": overall_percent,
                        "percent": (
                            active_track.get("progress_percent", "")
                            if active_track else ""
                        ),
                        "speed": (
                            active_track.get("progress_speed", "")
                            if active_track else ""
                        ),
                    }
                data = {
                    "status": status,
                    "queue": queue_data,
                }
                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(1)
        except GeneratorExit:
            return

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/download/queue", methods=["GET"])
def api_get_queue():
    queue_rows = models.get_queue()
    queue_with_details = []
    for row in queue_rows:
        album = _get_album_cached(row["album_id"])
        if "error" not in album:
            queue_with_details.append(
                {
                    "id": row["album_id"],
                    "title": album.get("title", ""),
                    "artist": album.get("artist", {}).get("artistName", ""),
                    "cover": next(
                        (
                            img["remoteUrl"]
                            for img in album.get("images", [])
                            if img["coverType"] == "cover"
                        ),
                        "",
                    ),
                    "track_count": album.get("statistics", {}).get("trackCount", 0),
                }
            )
    return jsonify(queue_with_details)


@app.route("/api/download/queue/<int:album_id>/tracks")
def api_queue_tracks(album_id):
    tracks = lidarr_request(f"track?albumId={album_id}")
    if isinstance(tracks, dict) and "error" in tracks:
        tracks = []
    if not tracks:
        album = _get_album_cached(album_id)
        if "error" not in album:
            artist = album.get("artist", {}).get("artistName", "")
            title = album.get("title", "")
            if artist and title:
                logger.debug(
                    "Lidarr tracks unavailable for album %d,"
                    " falling back to iTunes: %s - %s",
                    album_id,
                    artist,
                    title,
                )
                tracks = get_itunes_tracks(artist, title)
    result = [
        {
            "title": t.get("title", ""),
            "track_number": t.get("trackNumber", 0),
            "has_file": t.get("hasFile", False),
            "foreign_recording_id": t.get("foreignRecordingId", ""),
        }
        for t in tracks
    ]
    return jsonify(result)


@app.route("/api/download/queue", methods=["POST"])
def api_add_to_queue():
    album_id = (request.json or {}).get("album_id")
    with queue_lock:
        current_id = download_process.get("album_id")
    if current_id != album_id:
        models.enqueue_album(album_id)
    return jsonify({"success": True, "queue_length": models.get_queue_length()})


@app.route("/api/download/queue/bulk", methods=["POST"])
def api_add_to_queue_bulk():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"bulk_queue:{client_ip}", rate_limit_store, window=10, max_requests=3
    ):
        return jsonify(
            {
                "success": False,
                "message": "Too many bulk requests, please slow down",
            }
        ), 429
    album_ids = (request.json or {}).get("album_ids", [])
    if not isinstance(album_ids, list):
        return jsonify({"success": False, "message": "album_ids must be a list"}), 400
    added = 0
    with queue_lock:
        current_id = download_process.get("album_id")
    for album_id in album_ids:
        if isinstance(album_id, int) and album_id != current_id:
            if models.enqueue_album(album_id):
                added += 1
    return jsonify(
        {
            "success": True,
            "added": added,
            "queue_length": models.get_queue_length(),
        }
    )


@app.route("/api/download/queue/<int:album_id>", methods=["DELETE"])
def api_remove_from_queue(album_id):
    models.dequeue_album(album_id)
    return jsonify({"success": True})


@app.route("/api/download/queue/clear", methods=["POST"])
def api_clear_queue():
    models.clear_queue()
    return jsonify({"success": True})


@app.route("/api/download/queue/reorder", methods=["PUT"])
def api_reorder_queue():
    new_order = request.json.get("queue", [])
    if not isinstance(new_order, list):
        return jsonify({"success": False, "message": "queue must be a list"}), 400
    models.reorder_queue(new_order)
    return jsonify({"success": True})


@app.route("/api/download/history")
def api_download_history():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    return jsonify(models.get_album_history(page, per_page))


@app.route("/api/download/history/clear", methods=["POST"])
def api_clear_history():
    models.clear_history()
    return jsonify({"success": True})


@app.route("/api/download/history/<int:album_id>/tracks")
def api_album_tracks(album_id):
    return jsonify(models.get_track_downloads_for_album(album_id))


@app.route("/api/download/track/<int:track_id>", methods=["DELETE"])
def api_delete_track(track_id):
    track_data = models.mark_track_deleted(track_id)
    if track_data is None:
        return jsonify({"success": False, "error": "Track not found"}), 404

    file_deleted = False
    sanitized_track = sanitize_filename(track_data["track_title"])
    track_num = track_data["track_number"] or 0
    album_path = track_data["album_path"]
    xml_name = f"{track_num:02d} - {sanitized_track}.xml"
    xml_path = os.path.join(album_path, xml_name)

    cfg_ext = load_config().get("audio_format", "mp3")
    audio_exts = [cfg_ext] + [e for e in ["mp3", "opus", "flac", "aac", "ogg", "m4a"] if e != cfg_ext]
    for ext in audio_exts:
        candidate = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.{ext}")
        try:
            os.remove(candidate)
            file_deleted = True
            break
        except FileNotFoundError:
            continue
        except OSError:
            logger.error("Failed to delete track file: %s", candidate, exc_info=True)
            break
    if not file_deleted:
        logger.warning("Track file not found for deletion: %s/%02d - %s.*", album_path, track_num, sanitized_track)
    try:
        os.remove(xml_path)
    except OSError:
        pass

    url_banned = False
    body = request.get_json(silent=True) or {}
    if body.get("ban_url") and track_data.get("youtube_url"):
        try:
            models.add_banned_url(
                youtube_url=track_data["youtube_url"],
                youtube_title=track_data.get("youtube_title", ""),
                album_id=track_data["album_id"],
                album_title=track_data.get("album_title", ""),
                artist_name=track_data.get("artist_name", ""),
                track_title=track_data["track_title"],
                track_number=track_num,
            )
            url_banned = True
        except Exception:
            logger.error(
                "Failed to ban URL %s for track %s",
                track_data["youtube_url"],
                track_data["track_title"],
                exc_info=True,
            )

    return jsonify(
        {
            "success": True,
            "file_deleted": file_deleted,
            "url_banned": url_banned,
        }
    )


@app.route("/api/banned-urls")
def api_get_banned_urls():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    return jsonify(models.get_banned_urls(page, per_page))


@app.route("/api/banned-urls/<int:ban_id>", methods=["DELETE"])
def api_remove_banned_url(ban_id):
    deleted = models.remove_banned_url(ban_id)
    if deleted:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Ban not found"}), 404


@app.route("/api/stats")
def api_stats():
    downloaded_today = models.get_history_count_today()
    in_queue = models.get_queue_length() + (1 if download_process["active"] else 0)
    return jsonify(
        {
            "in_queue": in_queue,
            "downloaded_today": downloaded_today,
        }
    )


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    log_type = request.args.get("type", None, type=str)
    result = models.get_logs(page, per_page, log_type=log_type)
    _enrich_track_logs(result["items"])
    return jsonify(result)


_TRACK_LOG_TYPES = {"track_failure", "track_download"}


def _enrich_track_logs(items):
    banned_cache = {}
    for item in items:
        if item.get("type") not in _TRACK_LOG_TYPES:
            continue
        td_id = item.get("track_download_id")
        if not td_id:
            item["candidates"] = []
            continue
        try:
            candidates = models.get_candidate_attempts(td_id)
        except Exception:
            logger.warning(
                "Failed to fetch candidates for track_download %s",
                td_id, exc_info=True,
            )
            item["candidates"] = []
            continue
        album_id = item.get("album_id")
        if album_id is None:
            banned_lookup = {}
        elif album_id not in banned_cache:
            try:
                banned = models.get_banned_urls_for_album(album_id)
                banned_cache[album_id] = {
                    b["youtube_url"]: b["id"] for b in banned
                }
            except Exception:
                logger.warning(
                    "Failed to fetch banned URLs for album %s",
                    album_id, exc_info=True,
                )
                banned_cache[album_id] = {}
        if album_id is not None:
            banned_lookup = banned_cache[album_id]
        for c in candidates:
            url = c.get("youtube_url", "")
            c["is_banned"] = url in banned_lookup
            c["ban_id"] = banned_lookup.get(url)
        item["candidates"] = candidates


@app.route("/api/logs/size", methods=["GET"])
def api_logs_size():
    size = models.get_logs_db_size()
    return jsonify({"size": size, "formatted": format_bytes(size)})


@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    models.clear_logs()
    return jsonify({"success": True})


@app.route("/api/logs/<log_id>/dismiss", methods=["DELETE"])
def api_dismiss_log(log_id):
    deleted = models.delete_log(log_id)
    if deleted:
        return jsonify({"success": True})
    return jsonify({"success": False, "error": "Log not found"}), 404


@app.route("/api/download/failed")
def api_download_failed():
    album_id = request.args.get("album_id", type=int)
    if album_id is None:
        album_id = models.get_latest_download_album_id()
    if album_id is None:
        return jsonify(
            {
                "failed_tracks": [],
                "album_id": None,
                "album_title": "",
                "artist_name": "",
                "cover_url": "",
                "album_path": "",
                "lidarr_album_path": "",
            }
        )
    return jsonify(models.get_failed_tracks_for_retry(album_id))


@app.route("/api/scheduler/toggle", methods=["POST"])
def api_scheduler_toggle():
    config = load_config()
    config["scheduler_enabled"] = not config.get("scheduler_enabled", False)
    save_config(config)
    setup_scheduler()
    return jsonify({"enabled": config["scheduler_enabled"]})


@app.route("/api/scheduler/autodownload/toggle", methods=["POST"])
def api_autodownload_toggle():
    config = load_config()
    config["scheduler_auto_download"] = not config.get("scheduler_auto_download", True)
    save_config(config)
    return jsonify({"enabled": config["scheduler_auto_download"]})


@app.route("/api/xmlmetadata/toggle", methods=["POST"])
def api_xmlmetadata_toggle():
    config = load_config()
    config["xml_metadata_enabled"] = not config.get("xml_metadata_enabled", True)
    save_config(config)
    return jsonify({"enabled": config["xml_metadata_enabled"]})


@app.route("/api/acoustid/toggle", methods=["POST"])
def api_acoustid_toggle():
    config = load_config()
    config["acoustid_enabled"] = not config.get("acoustid_enabled", True)
    save_config(config)
    return jsonify({"enabled": config["acoustid_enabled"]})


@app.route("/api/youtube/search", methods=["POST"])
def api_youtube_search():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"yt_search:{client_ip}", rate_limit_store, window=3, max_requests=5
    ):
        return jsonify({"results": [], "error": "Too many requests"}), 429
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"results": []})

    import yt_dlp

    config = load_config()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _YtdlpSilentLogger(),
        "extract_flat": True,
        "noplaylist": True,
    }
    cookies_path = (config.get("yt_cookies_file") or "").strip()
    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path
    if config.get("yt_force_ipv4", True):
        ydl_opts["source_address"] = "0.0.0.0"
    pc = config.get("yt_player_client", "android")
    if pc:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [pc]}}
    try:
        items = []
        seen_urls = set()

        def _entry_watch_url(entry):
            wp = entry.get("webpage_url", "")
            if wp:
                return wp
            vid = entry.get("id", "")
            if vid:
                return f"https://www.youtube.com/watch?v={vid}"
            return entry.get("url", "")

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            yt_results = ydl.extract_info(f"ytsearch10:{query}", download=False)
            for entry in (yt_results or {}).get("entries", []):
                vid = entry.get("id", "")
                if vid and (
                    vid.startswith("RD")
                    or vid.startswith("PL")
                    or vid.startswith("UU")
                    or len(vid) != 11
                ):
                    continue
                url = _entry_watch_url(entry)
                if url and url not in seen_urls:
                    seen_urls.add(url)
                    items.append(
                        {
                            "title": entry.get("title", ""),
                            "url": url,
                            "duration": entry.get("duration", 0),
                            "channel": (
                                entry.get("channel", "")
                                or entry.get("uploader", "")
                                or ""
                            ),
                            "thumbnail": entry.get("thumbnail", ""),
                        }
                    )
        return jsonify({"results": items})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)[:200]}), 500


_audio_stream_cache = {}


class _YtdlpSilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


@app.route("/api/youtube/stream", methods=["GET"])
def api_youtube_stream():
    import requests as http_requests

    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"yt_stream:{client_ip}", rate_limit_store, window=5, max_requests=6
    ):
        return "Too many requests", 429
    url = request.args.get("url", "").strip()
    if not url:
        return "Missing url", 400

    url = _validate_youtube_url(url)
    if url is None:
        return "Invalid YouTube URL", 400

    import yt_dlp

    now = time.time()
    cached = _audio_stream_cache.get(url)
    if cached and now - cached["ts"] < 300:
        audio_url = cached["audio_url"]
        http_headers = cached["http_headers"]
        if not _is_safe_stream_url(audio_url):
            logger.error("Cached stream URL failed safety check: %s", audio_url[:100])
            del _audio_stream_cache[url]
            return "Unsafe audio stream URL", 403
    else:
        config = load_config()
        ydl_opts = {
            "quiet": True,
            "no_warnings": True,
            "logger": _YtdlpSilentLogger(),
            "format": "bestaudio/best",
            "noplaylist": True,
        }
        cookies_path = (config.get("yt_cookies_file") or "").strip()
        if cookies_path and os.path.exists(cookies_path):
            ydl_opts["cookiefile"] = cookies_path
        if config.get("yt_force_ipv4", True):
            ydl_opts["source_address"] = "0.0.0.0"
        pc = config.get("yt_player_client", "android")
        if pc:
            ydl_opts["extractor_args"] = {"youtube": {"player_client": [pc]}}
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            if not info:
                return "Could not extract info", 404
            audio_url = ""
            http_headers = info.get("http_headers", {})
            requested = info.get("requested_formats") or []
            if requested:
                for fmt in requested:
                    if fmt.get("vcodec") == "none" or fmt.get("acodec") != "none":
                        audio_url = fmt.get("url", "")
                        if fmt.get("http_headers"):
                            http_headers = fmt["http_headers"]
                        break
            if not audio_url:
                audio_url = info.get("url", "")
            if not audio_url:
                return "No audio stream found", 404
            if not _is_safe_stream_url(audio_url):
                logger.warning("Blocked unsafe audio URL: %s", audio_url[:100])
                return "Unsafe audio stream URL", 403
            audio_url = _sanitize_stream_url(audio_url)
            _audio_stream_cache[url] = {
                "audio_url": audio_url,
                "http_headers": http_headers,
                "ts": now,
            }
            for k in list(_audio_stream_cache):
                if now - _audio_stream_cache[k]["ts"] > 600:
                    del _audio_stream_cache[k]
            if len(_audio_stream_cache) > 200:
                oldest = min(_audio_stream_cache, key=lambda k: _audio_stream_cache[k]["ts"])
                del _audio_stream_cache[oldest]
        except Exception as e:
            logger.warning("Stream extraction failed: %s", e)
            return str(e)[:200], 500

    range_header = request.headers.get("Range")
    return _proxy_audio_stream(audio_url, http_headers, range_header)


def _proxy_audio_stream(sanitized_url, http_headers, range_header):
    import requests as http_requests

    proxy_headers = {
        "User-Agent": http_headers.get("User-Agent", ""),
        "Referer": http_headers.get("Referer", ""),
        "Accept": "*/*",
    }
    if range_header:
        proxy_headers["Range"] = range_header

    try:
        upstream = http_requests.get(
            sanitized_url,  # nosemgrep
            headers=proxy_headers,
            stream=True,
            timeout=30,
        )
        resp_headers = {
            "Content-Type": upstream.headers.get("Content-Type", "audio/webm"),
            "Accept-Ranges": "bytes",
            "Cache-Control": "no-cache",
        }
        if "Content-Length" in upstream.headers:
            resp_headers["Content-Length"] = upstream.headers["Content-Length"]
        if "Content-Range" in upstream.headers:
            resp_headers["Content-Range"] = upstream.headers["Content-Range"]
        return Response(
            upstream.iter_content(chunk_size=16384),
            status=upstream.status_code,
            headers=resp_headers,
        )
    except Exception as e:
        logger.warning("Stream proxy failed: %s", e)
        return "Stream unavailable", 502


@app.route("/api/download/manual", methods=["POST"])
def api_download_manual():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"manual_dl:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429

    data = request.json or {}
    youtube_url = data.get("youtube_url", "").strip()
    track_title = data.get("track_title", "").strip()
    track_num = data.get("track_num", 0)
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

    album_data = lidarr_request(f"album/{album_id_ctx}")
    if "error" in album_data:
        return jsonify(
            {
                "success": False,
                "message": f"Failed to fetch album from Lidarr: {album_data['error']}",
            }
        ), 500

    failed_ctx = models.get_failed_tracks_for_retry(album_id_ctx)
    config = load_config()
    lidarr_path = config.get("lidarr_path", "")

    artist_name_meta = album_data.get("artist", {}).get("artistName", "")
    album_title_meta = album_data.get("title", "")
    release_year_meta = str(album_data.get("releaseDate", ""))[:4]
    san_artist = sanitize_filename(artist_name_meta)
    san_album = sanitize_filename(album_title_meta)
    if release_year_meta:
        album_folder_meta = f"{san_album} ({release_year_meta})"
    else:
        album_folder_meta = san_album

    if lidarr_path:
        target_path = os.path.join(lidarr_path, san_artist, album_folder_meta)
    elif DOWNLOAD_DIR:
        target_path = os.path.join(DOWNLOAD_DIR, san_artist, album_folder_meta)
    else:
        lidarr_album_path_val = failed_ctx.get("lidarr_album_path", "")
        dl_album_path = failed_ctx.get("album_path", "")
        target_path = (
            lidarr_album_path_val
            if lidarr_album_path_val and os.path.isdir(lidarr_album_path_val)
            else dl_album_path
        )

    if not target_path:
        return jsonify({"success": False, "message": "No album path available"}), 400

    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    makedirs_safe(target_path, [DOWNLOAD_DIR, lidarr_path])

    return _execute_manual_download(
        youtube_url,
        track_title,
        track_num,
        target_path,
        album_data,
        album_id_ctx,
        failed_ctx,
        config,
        lidarr_path=lidarr_path,
    )


def _get_album_cached(album_id):
    now = time.time()
    if album_id in album_cache:
        cached, ts = album_cache[album_id]
        if now - ts < ALBUM_CACHE_TTL:
            return cached
    album = lidarr_request(f"album/{album_id}")
    if "error" not in album:
        album_cache[album_id] = (album, now)
    return album


def _sanitize_stream_url(stream_url):
    parts = urllib.parse.urlparse(stream_url)
    return urllib.parse.urlunparse(
        (
            parts.scheme,
            parts.netloc,
            parts.path,
            parts.params,
            parts.query,
            parts.fragment,
        )
    )


def _is_safe_stream_url(stream_url):
    if not isinstance(stream_url, str) or not stream_url:
        return False
    parsed = urllib.parse.urlparse(stream_url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    safe_domains = (
        ".googlevideo.com",
        ".youtube.com",
        ".ytimg.com",
        ".googleusercontent.com",
        ".gvt1.com",
        ".ggpht.com",
    )
    return any(
        hostname.endswith(domain) or hostname == domain.lstrip(".")
        for domain in safe_domains
    )


def _validate_youtube_url(youtube_url):
    if not youtube_url.startswith("http"):
        if not re.match(r"^[a-zA-Z0-9_-]{11}$", youtube_url):
            return None
        return f"https://www.youtube.com/watch?v={youtube_url}"  # nosemgrep
    parsed = urllib.parse.urlparse(youtube_url)
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "music.youtube.com",
    }
    if parsed.hostname not in allowed_hosts:
        return None
    return youtube_url


def _validate_target_path(target_path, config):
    lidarr_path = config.get("lidarr_path", "")
    allowed_bases = [os.path.realpath(DOWNLOAD_DIR)] if DOWNLOAD_DIR else []
    if lidarr_path:
        allowed_bases.append(os.path.realpath(lidarr_path))
    real_target = os.path.realpath(target_path)
    return any(
        real_target.startswith(base + os.sep) or real_target == base
        for base in allowed_bases
    )


def _execute_manual_download(
    youtube_url,
    track_title,
    track_num,
    target_path,
    album_data,
    album_id_ctx,
    failed_ctx,
    config,
    lidarr_path="",
):
    lidarr_album_path_rec = target_path if lidarr_path else ""
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
        lidarr_album_path=lidarr_album_path_rec,
        cover_url=failed_ctx.get("cover_url", ""),
    )


@app.route("/api/album/<int:album_id>/track/manual-download", methods=["POST"])
def api_manual_track_download(album_id):
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"manual_track:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429

    data = request.json or {}
    youtube_url = data.get("youtube_url", "").strip()
    track_title = data.get("track_title", "").strip()
    track_num = data.get("track_number", 0)

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
    release_year = str(album_data.get("releaseDate", ""))[:4]
    album_type = album_data.get("albumType", "Album")

    sanitized_artist = sanitize_filename(artist_name)
    sanitized_album = sanitize_filename(album_title)
    if release_year:
        album_folder = f"{sanitized_album} ({release_year})"
    else:
        album_folder = sanitized_album

    config = load_config()
    lidarr_path = config.get("lidarr_path", "")

    if lidarr_path:
        target_path = os.path.join(lidarr_path, sanitized_artist, album_folder)
    elif DOWNLOAD_DIR:
        target_path = os.path.join(DOWNLOAD_DIR, sanitized_artist, album_folder)
    else:
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
            lidarr_album_path=target_path if lidarr_path else "",
            cover_url=cover_url,
        )

    threading.Thread(target=_run_manual_download, daemon=True).start()
    return jsonify({"success": True, "message": "Download queued"})


def _build_ydl_opts(config, temp_file):
    audio_format = config.get("audio_format", "mp3")
    pp = {
        "key": "FFmpegExtractAudio",
        "preferredcodec": audio_format,
    }
    if audio_format == "mp3":
        pp["preferredquality"] = "320"
    opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [pp],
        "outtmpl": temp_file,
        "noplaylist": True,
    }
    cookies_path = (config.get("yt_cookies_file") or "").strip()
    if cookies_path and os.path.exists(cookies_path):
        opts["cookiefile"] = cookies_path
    if config.get("yt_force_ipv4", True):
        opts["source_address"] = "0.0.0.0"
    pc = config.get("yt_player_client", "android")
    if pc:
        opts["extractor_args"] = {"youtube": {"player_client": [pc]}}
    return opts


def _cleanup_temp_files(temp_file):
    for ext in [".mp3", ".opus", ".flac", ".aac", ".ogg", ".webm", ".m4a", ".part"]:
        tmp = temp_file + ext
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as rm_err:
                logger.debug("Failed to remove temp file %s: %s", tmp, rm_err)


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
):
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

    with queue_lock:
        download_process["active"] = True
        download_process["stop"] = False
        download_process["album_id"] = album_id
        download_process["album_title"] = album_title
        download_process["artist_name"] = artist_name
        download_process["cover_url"] = cover_url
        download_process["current_track_index"] = 0
        download_process["tracks"] = [
            {
                "track_title": track_title,
                "track_number": int(track_num),
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
        makedirs_safe(target_path, [DOWNLOAD_DIR, config.get("lidarr_path", "")])
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
    import yt_dlp

    track_state = download_process["tracks"][0]

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
        track_state["status"] = "failed"
        track_state["error_message"] = "Invalid track filename"
        return

    def progress_hook(d):
        if d["status"] == "downloading":
            track_state["progress_percent"] = d.get("_percent_str", "0%").strip()
            track_state["progress_speed"] = d.get("_speed_str", "N/A").strip()

    ydl_opts = _build_ydl_opts(config, temp_file)
    ydl_opts["progress_hooks"] = [progress_hook]

    youtube_title = ""
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(youtube_url, download=False)
            if info:
                youtube_title = info.get("title", "")
                track_state["youtube_title"] = youtube_title
            ydl.download([youtube_url])
    except Exception as e:
        logger.error("yt-dlp download failed for '%s': %s", track_title, e)
        _cleanup_temp_files(temp_file)
        track_state["status"] = "failed"
        track_state["error_message"] = str(e)[:200]
        return

    actual_file = temp_file + f".{audio_ext}"
    if not os.path.exists(actual_file):
        _cleanup_temp_files(temp_file)
        track_state["status"] = "failed"
        track_state["error_message"] = "Download failed -- file not created"
        return

    track_state["status"] = "tagging"
    try:
        track_info = _resolve_track_info(
            track_title,
            track_num,
            album_data,
            album_id,
        )
        tag_mp3(actual_file, track_info, album_data, None)

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
        if config.get("acoustid_enabled") and config.get("acoustid_api_key"):
            track_state["status"] = "verifying"
            fp_data = _run_manual_acoustid(config, actual_file)

        file_size = os.path.getsize(actual_file)
        shutil.move(actual_file, final_file)
        set_permissions(final_file)
    except Exception as e:
        logger.error(
            "Post-download processing failed for '%s': %s",
            track_title,
            e,
            exc_info=True,
        )
        _cleanup_temp_files(temp_file)
        track_state["status"] = "failed"
        track_state["error_message"] = str(e)[:200]
        return

    track_state["status"] = "done"

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
    )

    _refresh_lidarr_artist(album_data, track_title)


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
    import yt_dlp

    sanitized_track = sanitize_filename(track_title)
    if not sanitized_track:
        sanitized_track = "untitled"
    audio_ext = config.get("audio_format", "mp3")
    temp_file = os.path.join(target_path, f"temp_manual_{uuid.uuid4().hex[:8]}")
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
        return jsonify(
            {
                "success": False,
                "message": "Invalid track filename",
            }
        ), 400

    ydl_opts = _build_ydl_opts(config, temp_file)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([youtube_url])
    except Exception as e:
        logger.error("yt-dlp download failed for '%s': %s", track_title, e)
        _cleanup_temp_files(temp_file)
        return jsonify({"success": False, "message": str(e)[:200]}), 500

    actual_file = temp_file + f".{audio_ext}"
    if not os.path.exists(actual_file):
        _cleanup_temp_files(temp_file)
        return jsonify(
            {
                "success": False,
                "message": "Download failed -- file not created",
            }
        ), 500

    try:
        track_info = _resolve_track_info(
            track_title,
            track_num,
            album_data,
            album_id,
        )
        tag_mp3(actual_file, track_info, album_data, None)

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
            fp_data = _run_manual_acoustid(config, actual_file)

        file_size = os.path.getsize(actual_file)
        shutil.move(actual_file, final_file)
        set_permissions(final_file)
    except Exception as e:
        logger.error(
            "Post-download processing failed for '%s': %s",
            track_title,
            e,
            exc_info=True,
        )
        _cleanup_temp_files(temp_file)
        return jsonify({"success": False, "message": str(e)[:200]}), 500

    _record_manual_download(
        album_id=album_id,
        album_title=album_title,
        artist_name=artist_name,
        track_title=track_title,
        track_num=track_num,
        youtube_url=youtube_url,
        album_path=album_path,
        lidarr_album_path=lidarr_album_path,
        cover_url=cover_url,
        fp_data=fp_data,
        file_size=file_size,
    )

    _refresh_lidarr_artist(album_data, track_title)

    response = {
        "success": True,
        "message": f"Track '{track_title}' downloaded successfully",
    }
    if fp_data:
        response["acoustid_score"] = fp_data.get("acoustid_score", 0.0)
        response["acoustid_recording_id"] = fp_data.get(
            "acoustid_recording_id",
            "",
        )
    return jsonify(response)


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
    if not tracks:
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


@app.route("/youtube")
def youtube_import():
    return render_template("youtube.html")


@app.route("/api/youtube/playlist/info", methods=["POST"])
def api_youtube_playlist_info():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"playlist_info:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"error": "Too many requests"}), 429

    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    if not _validate_youtube_url_for_playlist(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    import yt_dlp

    config = load_config()
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _YtdlpSilentLogger(),
        "extract_flat": True,
        "noplaylist": False,
    }
    cookies_path = (config.get("yt_cookies_file") or "").strip()
    if cookies_path and os.path.exists(cookies_path):
        ydl_opts["cookiefile"] = cookies_path
    if config.get("yt_force_ipv4", True):
        ydl_opts["source_address"] = "0.0.0.0"
    pc = config.get("yt_player_client", "android")
    if pc:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [pc]}}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Could not extract info from URL"}), 404

        def _thumb_from_entry(entry):
            thumb = entry.get("thumbnail", "")
            if not thumb:
                vid_id = entry.get("id", "")
                if vid_id:
                    thumb = f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg"
            return thumb

        entries = []
        if "entries" in info:
            for i, entry in enumerate((info.get("entries") or [])):
                if not entry:
                    continue
                vid_url = entry.get("webpage_url") or entry.get("url", "")
                if not vid_url and entry.get("id"):
                    vid_url = f"https://www.youtube.com/watch?v={entry['id']}"
                entries.append({
                    "index": i,
                    "title": entry.get("title", f"Track {i + 1}"),
                    "url": vid_url,
                    "duration": entry.get("duration", 0),
                    "thumbnail": _thumb_from_entry(entry),
                    "channel": (
                        entry.get("channel", "") or entry.get("uploader", "")
                    ),
                })
            result_type = "playlist"
            playlist_title = info.get("title", "YouTube Playlist")
            channel = info.get("channel", "") or info.get("uploader", "")
            playlist_thumb = _best_playlist_thumbnail(info, entries)
            if "yt3.googleusercontent.com" not in (playlist_thumb or ""):
                ytm_thumb = _fetch_ytmusic_album_art(info.get("id", ""))
                if ytm_thumb:
                    logger.info(
                        "Replaced yt-dlp thumbnail with YT Music album art: %s",
                        ytm_thumb[:120],
                    )
                    playlist_thumb = _resize_yt3_url(ytm_thumb)
            logger.info(
                "Playlist thumbnail selected: %s (yt3=%s, info.thumbnail=%s, thumbnails count=%d)",
                playlist_thumb[:120] if playlist_thumb else "(none)",
                "yt3.googleusercontent.com" in (playlist_thumb or ""),
                "yt3.googleusercontent.com" in (info.get("thumbnail") or ""),
                len(info.get("thumbnails") or []),
            )
        else:
            single_entry = {
                "index": 0,
                "title": info.get("title", ""),
                "url": url,
                "duration": info.get("duration", 0),
                "thumbnail": _thumb_from_entry(info),
                "channel": info.get("channel", "") or info.get("uploader", ""),
            }
            entries.append(single_entry)
            result_type = "video"
            playlist_title = info.get("title", "")
            channel = info.get("channel", "") or info.get("uploader", "")
            playlist_thumb = _best_playlist_thumbnail(info, entries)

        return jsonify({
            "type": result_type,
            "title": playlist_title,
            "channel": channel,
            "thumbnail": playlist_thumb,
            "entries": entries,
        })
    except Exception as e:
        logger.warning("Playlist info extraction failed: %s", e)
        return jsonify({"error": str(e)[:300]}), 500


def _fetch_ytmusic_album_art(playlist_id):
    """Fetch the square album art from YouTube Music for an OLAK5uy_ playlist.

    yt-dlp does not extract the square album cover for OLAK5uy_* playlists
    (it returns rectangular i.ytimg.com thumbnails). The YouTube Music page
    embeds the real album art URL in its initial data. We scrape it with a
    regex against the page HTML.

    Returns the largest yt3/lh3 thumbnail URL, or "" if not found.
    """
    if not playlist_id:
        return ""
    if not (playlist_id.startswith("OLAK5uy_") or playlist_id.startswith("RDCLAK5uy_")):
        logger.info(
            "Skipping YT Music album art fetch (not an OLAK5uy_ playlist): %s",
            playlist_id,
        )
        return ""
    import requests as http_requests
    url = f"https://music.youtube.com/playlist?list={playlist_id}&hl=en"
    logger.info("Fetching YT Music page: %s", url)
    try:
        resp = http_requests.get(
            url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.9",
                "Cookie": "SOCS=CAI; PREF=hl=en",
            },
            allow_redirects=True,
        )
        logger.info(
            "YT Music page returned HTTP %d, body length=%d",
            resp.status_code, len(resp.text or ""),
        )
        if resp.status_code != 200:
            return ""
        # Album art URLs appear as yt3.googleusercontent.com or lh3.googleusercontent.com
        matches = re.findall(
            r'"url":"(https://(?:yt3|lh3)\.googleusercontent\.com/[^"]+?=w(\d+)-h(\d+)[^"]*)"',
            resp.text,
        )
        logger.info(
            "Found %d googleusercontent thumbnails in YT Music page",
            len(matches),
        )
        if not matches:
            # Probe whether the page has anything from googleusercontent at all
            probe = re.findall(
                r'https://(?:yt3|lh3)\.googleusercontent\.com/[^\s"\'<>]+',
                resp.text,
            )
            if probe:
                logger.info(
                    "Sample googleusercontent URLs (no size match): %s",
                    probe[0][:200],
                )
            else:
                logger.info("No googleusercontent URLs at all in YT Music page")
            return ""
        best = max(matches, key=lambda m: int(m[1]) * int(m[2]))
        logger.info(
            "Selected best YT Music album art (%sx%s): %s",
            best[1], best[2], best[0][:160],
        )
        return best[0]
    except Exception as e:
        logger.warning("YT Music album art fetch failed: %s", e)
        return ""


def _resize_yt3_url(url, size=512):
    """Resize a yt3.googleusercontent.com thumbnail URL to the given square size."""
    resized = re.sub(r'(=w)\d+(-h)\d+', f'=w{size}-h{size}', url)
    if resized == url and "=" not in url.rsplit("/", 1)[-1]:
        resized = url + f"=w{size}-h{size}-l90-rj"
    return resized


def _yt3_thumb_size(t):
    w, h = t.get("width") or 0, t.get("height") or 0
    if w > 0 and h > 0:
        return w * h
    m = re.search(r"=w(\d+)-h(\d+)", t.get("url", ""))
    return int(m.group(1)) * int(m.group(2)) if m else 0


def _best_playlist_thumbnail(info, entries):
    """Return the best thumbnail URL for a playlist/album.

    Prefers yt3.googleusercontent.com URLs (YouTube Music album art, always
    square). Falls back to the largest square thumbnail, then largest overall,
    then the first entry thumbnail.
    """
    # 1. plain info["thumbnail"] from yt3 (most reliable source for YT Music)
    plain_url = info.get("thumbnail", "")
    if plain_url and "yt3.googleusercontent.com" in plain_url:
        return _resize_yt3_url(plain_url)

    thumbnails = info.get("thumbnails") or []

    # 2. any yt3 thumbnail in the thumbnails list
    yt3_thumbs = [
        t for t in thumbnails
        if "yt3.googleusercontent.com" in (t.get("url") or "")
    ]
    if yt3_thumbs:
        best = max(yt3_thumbs, key=_yt3_thumb_size)
        return _resize_yt3_url(best["url"])

    # 3. any other square thumbnail (e.g. channel art)
    def _is_square(t):
        w, h = t.get("width") or 0, t.get("height") or 0
        return w > 0 and h > 0 and abs(w - h) <= max(w, h) * 0.15

    square = [t for t in thumbnails if _is_square(t) and t.get("url")]
    if square:
        return max(square, key=lambda t: (t.get("width") or 0))["url"]

    # 4. largest thumbnail regardless of shape
    with_url = [t for t in thumbnails if t.get("url")]
    if with_url:
        return max(
            with_url,
            key=lambda t: (t.get("width") or 0) * (t.get("height") or 0),
        )["url"]

    # 5. plain thumbnail (non-yt3)
    if plain_url:
        return plain_url

    # 6. first entry thumbnail as last resort
    return entries[0]["thumbnail"] if entries else ""


def _validate_youtube_url_for_playlist(url):
    if not url.startswith("http"):
        if re.match(r"^[a-zA-Z0-9_-]{11}$", url):
            return True
        if re.match(r"^PL[a-zA-Z0-9_-]+$", url):
            return True
        return False
    parsed = urllib.parse.urlparse(url)
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "music.youtube.com",
    }
    return parsed.hostname in allowed_hosts


@app.route("/api/youtube/playlist/download", methods=["POST"])
def api_youtube_playlist_download():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"playlist_dl:{client_ip}", rate_limit_store, window=10, max_requests=2
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429

    with queue_lock:
        if download_process.get("active"):
            return jsonify(
                {"success": False, "message": "A download is already in progress"}
            ), 409

    data = request.json or {}
    artist_name = data.get("artist_name", "").strip()
    album_title = data.get("album_title", "").strip()
    entries = data.get("entries", [])
    thumbnail_url = data.get("thumbnail_url", "").strip()
    source_url = data.get("source_url", "").strip()

    if not artist_name and not album_title:
        return jsonify(
            {"success": False, "message": "At least one of artist_name or album_title is required"}
        ), 400

    if not entries:
        return jsonify({"success": False, "message": "No entries to download"}), 400

    validated_entries = []
    for entry in entries:
        v_url = _validate_youtube_url(entry.get("url", ""))
        if not v_url:
            return jsonify(
                {"success": False, "message": "Invalid YouTube URL in entries"}
            ), 400
        validated_entries.append({**entry, "url": v_url})

    config = load_config()
    lidarr_path = config.get("lidarr_path", "")
    base = lidarr_path if lidarr_path else DOWNLOAD_DIR
    if not base:
        return jsonify(
            {"success": False, "message": "No download path configured"}
        ), 400

    path_parts = [p for p in [sanitize_filename(artist_name), sanitize_filename(album_title)] if p]
    target_path = os.path.join(base, *path_parts)

    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    threading.Thread(
        target=_execute_playlist_download,
        args=(artist_name, album_title, validated_entries, target_path, config, thumbnail_url, source_url),
        daemon=True,
    ).start()
    return jsonify(
        {"success": True, "message": f"Downloading {len(validated_entries)} track(s)"}
    )


def _execute_playlist_download(
    artist_name, album_title, entries, target_path, config, thumbnail_url="", source_url=""
):
    import yt_dlp

    for _ in range(300):
        if not download_process["active"]:
            break
        time.sleep(1)
    else:
        logger.warning(
            "Playlist download timed out waiting for active download: %s",
            album_title,
        )
        return

    with queue_lock:
        download_process["active"] = True
        download_process["stop"] = False
        download_process["album_id"] = 0
        download_process["album_title"] = album_title
        download_process["artist_name"] = artist_name
        download_process["cover_url"] = ""
        download_process["current_track_index"] = 0
        download_process["tracks"] = [
            {
                "track_title": entry.get("title", f"Track {i + 1}"),
                "track_number": i + 1,
                "status": "pending",
                "youtube_url": entry.get("url", ""),
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            }
            for i, entry in enumerate(entries)
        ]

    display_album = album_title or artist_name
    display_artist = artist_name or album_title

    album_data = {
        "title": display_album,
        "artist": {
            "artistName": display_artist,
            "id": 0,
            "foreignArtistId": "",
        },
        "releaseDate": "",
        "trackCount": len(entries),
        "foreignAlbumId": "",
        "releases": [],
        "images": [],
    }

    total_size = 0
    success_count = 0
    try:
        makedirs_safe(target_path, [DOWNLOAD_DIR, config.get("lidarr_path", "")])

        if thumbnail_url and _is_safe_stream_url(thumbnail_url):
            try:
                import requests as req_lib
                resp = req_lib.get(
                    thumbnail_url,
                    timeout=15,
                    stream=True,
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) "
                            "Chrome/120.0.0.0 Safari/537.36"
                        ),
                        "Referer": "https://music.youtube.com/",
                    },
                )
                if resp.status_code == 200:
                    cover_path = os.path.join(target_path, "cover.jpg")
                    with open(cover_path, "wb") as cf:
                        for chunk in resp.iter_content(8192):
                            cf.write(chunk)
                    set_permissions(cover_path)
                    logger.info("Cover art saved: %s", cover_path)
                else:
                    logger.warning(
                        "Cover art fetch returned HTTP %d for %s",
                        resp.status_code, thumbnail_url[:120],
                    )
            except Exception as cover_err:
                logger.warning("Failed to download cover art: %s", cover_err)
        elif thumbnail_url:
            logger.warning(
                "Cover art URL rejected by safety check: %s",
                thumbnail_url[:120],
            )
        else:
            logger.info("No cover art URL provided for this import")

        logger.info(
            "YouTube import started: %s / %s (%d tracks)",
            artist_name, album_title, len(entries),
        )

        for i, entry in enumerate(entries):
            if download_process.get("stop"):
                break

            track_state = download_process["tracks"][i]
            if track_state.get("skip"):
                track_state["status"] = "skipped"
                continue

            download_process["current_track_index"] = i
            track_title = entry.get("title", f"Track {i + 1}")
            track_num = i + 1
            youtube_url = entry.get("url", "")

            logger.info(
                "YouTube import [%d/%d] downloading: %s",
                i + 1, len(entries), track_title,
            )
            track_state["status"] = "downloading"

            def _progress_hook(d, state=track_state):
                if d["status"] == "downloading":
                    state["progress_percent"] = d.get("_percent_str", "0%").strip()
                    state["progress_speed"] = d.get("_speed_str", "N/A").strip()
                    if state.get("skip"):
                        raise TrackSkippedException()

            temp_file = os.path.join(
                target_path, f"temp_playlist_{uuid.uuid4().hex[:8]}"
            )
            ydl_opts = _build_ydl_opts(config, temp_file)
            ydl_opts["progress_hooks"] = [_progress_hook]

            youtube_title = ""
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(youtube_url, download=False)
                    if info:
                        youtube_title = info.get("title", "")
                        track_state["youtube_title"] = youtube_title
                    ydl.download([youtube_url])
            except TrackSkippedException:
                _cleanup_temp_files(temp_file)
                track_state["status"] = "skipped"
                continue
            except Exception as e:
                logger.error(
                    "yt-dlp download failed for playlist track '%s': %s",
                    track_title, e,
                )
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = str(e)[:200]
                _record_playlist_track(
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, success=False,
                    error_message=str(e)[:200], file_size=0,
                    cover_url=thumbnail_url, source_url=source_url,
                )
                continue

            audio_ext = config.get("audio_format", "mp3")
            actual_file = temp_file + f".{audio_ext}"
            if not os.path.exists(actual_file):
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = "Download failed — file not created"
                _record_playlist_track(
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, success=False,
                    error_message="Download failed — file not created", file_size=0,
                    cover_url=thumbnail_url,
                )
                continue

            track_state["status"] = "tagging"
            sanitized_track = sanitize_filename(track_title) or f"track_{track_num:02d}"
            final_file = os.path.join(
                target_path, f"{track_num:02d} - {sanitized_track}.{audio_ext}"
            )

            real_final = os.path.realpath(final_file)
            real_target = os.path.realpath(target_path)
            if not (
                real_final.startswith(real_target + os.sep)
                or real_final == real_target
            ):
                logger.error(
                    "Path containment violation: '%s' escapes '%s'",
                    real_final, real_target,
                )
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = "Invalid track filename"
                continue

            track_info = {
                "title": track_title,
                "trackNumber": track_num,
                "foreignRecordingId": "",
            }

            try:
                tag_audio_file(actual_file, track_info, album_data, None)
                file_size = os.path.getsize(actual_file)
                shutil.move(actual_file, final_file)
                set_permissions(final_file)
                total_size += file_size
            except Exception as e:
                logger.error(
                    "Post-download processing failed for '%s': %s",
                    track_title, e, exc_info=True,
                )
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = str(e)[:200]
                _record_playlist_track(
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, success=False,
                    error_message=str(e)[:200], file_size=0,
                    cover_url=thumbnail_url, source_url=source_url,
                )
                continue

            track_state["status"] = "done"
            track_state["youtube_url"] = youtube_url
            track_state["youtube_title"] = youtube_title
            success_count += 1
            logger.info(
                "YouTube import [%d/%d] done: %s",
                i + 1, len(entries), track_title,
            )

            _record_playlist_track(
                album_title=album_title, artist_name=artist_name,
                track_title=track_title, track_num=track_num,
                youtube_url=youtube_url, youtube_title=youtube_title,
                target_path=target_path, success=True,
                error_message="", file_size=file_size,
                cover_url=thumbnail_url, source_url=source_url,
            )

        set_permissions(target_path)

    except Exception as e:
        logger.error("Playlist download error: %s", e, exc_info=True)
    finally:
        logger.info(
            "YouTube import finished: %s / %s — %d/%d tracks OK",
            display_artist, display_album, success_count, len(entries),
        )
        try:
            models.add_log(
                log_type="manual_download",
                album_id=0,
                album_title=display_album,
                artist_name=display_artist,
                details=(
                    f"YouTube import: {success_count}/{len(entries)} tracks downloaded"
                ),
                total_file_size=total_size,
            )
        except Exception as log_err:
            logger.error("Failed to add playlist summary log: %s", log_err)
        try:
            failed_count = len(entries) - success_count
            if success_count == len(entries):
                notif_log_type = "download_success"
                notif_title = "YouTube Import Complete"
                notif_color = 0x10B981
            elif success_count > 0:
                notif_log_type = "partial_success"
                notif_title = "YouTube Import Partial"
                notif_color = 0xF59E0B
            else:
                notif_log_type = "album_error"
                notif_title = "YouTube Import Failed"
                notif_color = 0xEF4444
            from notifications import md2_escape as _md2e
            plain = (
                f"{notif_title}\n"
                f"Album: {display_album}\n"
                f"Artist: {display_artist}\n"
                f"Downloaded: {success_count}/{len(entries)} tracks"
            )
            md2 = (
                f"*{_md2e(notif_title)}*\n"
                f"*Album:* {_md2e(display_album)}\n"
                f"*Artist:* {_md2e(display_artist)}\n"
                f"Downloaded: {success_count}/{len(entries)} tracks"
            )
            verified_thumb = ""
            if thumbnail_url:
                try:
                    import requests as req_check
                    head = req_check.head(
                        thumbnail_url, timeout=5, allow_redirects=True,
                        headers={"Referer": "https://music.youtube.com/"},
                    )
                    if head.status_code == 200:
                        verified_thumb = thumbnail_url
                    else:
                        logger.info(
                            "Skipping notification thumbnail (HTTP %d): %s",
                            head.status_code, thumbnail_url[:120],
                        )
                except Exception as head_err:
                    logger.debug("Thumbnail HEAD check failed: %s", head_err)
            embed = {
                "title": notif_title,
                "color": notif_color,
                "fields": [
                    {"name": "Album", "value": display_album, "inline": True},
                    {"name": "Artist", "value": display_artist, "inline": True},
                    {
                        "name": "Tracks",
                        "value": f"{success_count}/{len(entries)} downloaded"
                        + (f", {failed_count} failed" if failed_count else ""),
                        "inline": True,
                    },
                ],
            }
            if verified_thumb:
                embed["thumbnail"] = {"url": verified_thumb}
            send_notifications(
                plain,
                log_type=notif_log_type,
                embed_data=embed,
                telegram_message=md2,
                telegram_parse_mode="MarkdownV2",
                photo_url=verified_thumb or None,
            )
        except Exception as notif_err:
            logger.error("Failed to send YouTube import notification: %s", notif_err)
        with queue_lock:
            download_process["active"] = False
            download_process["current_track_index"] = -1
            download_process["album_id"] = None
            download_process["album_title"] = ""
            download_process["artist_name"] = ""
            download_process["cover_url"] = ""


def _record_playlist_track(
    *, album_title, artist_name, track_title, track_num,
    youtube_url, youtube_title, target_path,
    success, error_message, file_size, cover_url="", source_url="",
):
    track_download_id = None
    try:
        track_download_id = models.add_track_download(
            album_id=0,
            album_title=album_title,
            artist_name=artist_name,
            track_title=track_title,
            track_number=track_num,
            success=success,
            error_message=error_message,
            youtube_url=youtube_url,
            youtube_title=youtube_title or track_title,
            match_score=1.0,
            duration_seconds=0,
            album_path=target_path,
            lidarr_album_path=source_url,
            cover_url=cover_url,
        )
    except Exception as db_err:
        logger.error(
            "Failed to record playlist track '%s': %s", track_title, db_err,
        )
    try:
        if success:
            models.add_log(
                log_type="track_download",
                album_id=0,
                album_title=album_title,
                artist_name=artist_name,
                details="Track downloaded successfully",
                track_title=track_title,
                track_number=track_num,
                track_download_id=track_download_id,
                total_file_size=file_size,
            )
        else:
            models.add_log(
                log_type="track_failure",
                album_id=0,
                album_title=album_title,
                artist_name=artist_name,
                details=error_message or "Unknown error",
                track_title=track_title,
                track_number=track_num,
                track_download_id=track_download_id,
            )
    except Exception as log_err:
        logger.error(
            "Failed to add log for playlist track '%s': %s", track_title, log_err,
        )


@app.route("/api/thumbnail")
def api_thumbnail_proxy():
    """Proxy a thumbnail image through the server to avoid CORS/hotlink issues."""
    import requests as http_requests

    url = request.args.get("url", "").strip()
    if not url:
        return "Missing url", 400
    if not _is_safe_stream_url(url):
        return "Invalid thumbnail URL", 400
    try:
        resp = http_requests.get(
            url,
            timeout=10,
            stream=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://music.youtube.com/",
                "Accept": "image/webp,image/apng,image/*,*/*;q=0.8",
            },
        )
        if resp.status_code != 200:
            logger.debug(
                "Thumbnail proxy upstream %d for %s",
                resp.status_code, url[:120],
            )
            return "Failed to fetch thumbnail", 502
        content_type = resp.headers.get("Content-Type", "image/jpeg")
        return Response(
            resp.content,
            status=200,
            headers={
                "Content-Type": content_type,
                "Cache-Control": "public, max-age=86400",
            },
        )
    except Exception as e:
        logger.debug("Thumbnail proxy failed: %s", e)
        return "Thumbnail unavailable", 502


@app.route("/api/youtube/recent")
def api_youtube_recent():
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT album_title, artist_name,
               MAX(cover_url) as cover_url,
               MAX(lidarr_album_path) as source_url,
               MAX(youtube_url) as youtube_url,
               COUNT(*) as track_count,
               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as success_count,
               MAX(timestamp) as latest_timestamp
        FROM track_downloads
        WHERE album_id = 0
        GROUP BY album_title, artist_name
        ORDER BY latest_timestamp DESC
        LIMIT 6
        """
    ).fetchall()
    return jsonify([dict(r) for r in rows])


def _get_ytdlp_pypi_version():
    import requests as http_requests

    try:
        resp = http_requests.get(
            "https://pypi.org/pypi/yt-dlp/json",
            headers={"User-Agent": "lidarr-yt-downloader"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()["info"]["version"]
    except Exception as e:
        logger.debug("Failed to fetch yt-dlp version from PyPI: %s", e)
        return None


def _startup_ytdlp_update():
    current = get_ytdlp_version()
    logger.info("Checking for yt-dlp updates (installed: %s)...", current)
    latest = _get_ytdlp_pypi_version()
    if not latest:
        logger.warning("Could not reach PyPI to check yt-dlp version")
        return
    if current == latest:
        logger.info("yt-dlp %s is up to date", current)
        return
    logger.info("Updating yt-dlp %s -> %s...", current, latest)
    _, new_version, error = _pip_update_ytdlp()
    if error:
        logger.warning("yt-dlp update failed: %s", error)
        return
    logger.info("yt-dlp updated %s -> %s, restarting...", current, new_version)
    _exec_restart()


if __name__ == "__main__":
    db.init_db()
    models.reset_downloading_to_queued()
    logger.info("Starting Lidarr YouTube Downloader...")
    logger.info("Version: %s", VERSION)
    logger.info(
        "Download directory: %s",
        DOWNLOAD_DIR if DOWNLOAD_DIR else "Not set (check DOWNLOAD_PATH env)",
    )
    setup_scheduler()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=process_download_queue, daemon=True).start()
    threading.Thread(target=_startup_ytdlp_update, daemon=True).start()
    flask_host = os.environ.get("FLASK_HOST", "0.0.0.0")
    flask_port = int(os.environ.get("FLASK_PORT", "5000"))
    logger.info(
        "Application started successfully on http://%s:%d", flask_host, flask_port
    )
    app.run(host=flask_host, port=flask_port, debug=False, use_reloader=False)