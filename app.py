"""Flask application with thin route handlers.

All business logic lives in extracted modules. This file defines
routes, request parsing, and response formatting.
"""

import json
import logging
import os
import re
import shutil
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

import db
import models
from config import ALLOWED_CONFIG_KEYS, load_config, save_config
from downloader import get_ytdlp_version
from lidarr import get_missing_albums, lidarr_request
from metadata import create_xml_metadata, get_itunes_tracks, tag_mp3
from processing import (
    download_process,
    get_download_status,
    process_album_download,
    process_download_queue,
    queue_lock,
    stop_download,
)
from scheduler import run_scheduler, setup_scheduler
from utils import check_rate_limit, format_bytes, sanitize_filename, set_permissions

logging.basicConfig(
    level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)

VERSION = "1.5.2"
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


# --- Template routes ---


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


# --- Config routes ---


@app.route("/api/config", methods=["GET", "POST"])
def api_config():
    if request.method == "GET":
        return jsonify(load_config())
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"config:{client_ip}", rate_limit_store, window=5, max_requests=3):
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
        return jsonify({"success": False, "message": "Config must be a JSON object"}), 400
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
    return jsonify({
        "success": True,
        "applied": len(applied_keys),
        "skipped": len(skipped_keys),
        "message": (
            f"Imported {len(applied_keys)} settings."
            f" {len(skipped_keys)} keys skipped."
        ),
    })


# --- Lidarr / album routes ---


@app.route("/api/test-connection")
def api_test_connection():
    system = lidarr_request("system/status")
    if "error" in system:
        return jsonify({"status": "error", "message": system["error"]})
    return jsonify({
        "status": "success" if "version" in system else "error",
        "lidarr_version": system.get("version", "Unknown"),
    })


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


# --- yt-dlp routes ---


@app.route("/api/ytdlp/version")
def api_ytdlp_version():
    return jsonify({"version": get_ytdlp_version()})


@app.route("/api/ytdlp/update", methods=["POST"])
def api_ytdlp_update():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"ytdlp_update:{client_ip}", rate_limit_store, window=60, max_requests=1
    ):
        return jsonify({
            "success": False,
            "message": "Update already in progress or rate limited",
        }), 429
    old_version = get_ytdlp_version()
    try:
        import subprocess

        import yt_dlp

        result = subprocess.run(
            ["pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            import importlib

            importlib.reload(yt_dlp)
            new_version = get_ytdlp_version()
            return jsonify({
                "success": True,
                "old_version": old_version,
                "new_version": new_version,
            })
        return jsonify({
            "success": False,
            "message": result.stderr[-500:] if result.stderr else "Update failed",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "message": "Update timed out (120s)"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


# --- Download routes ---


@app.route("/api/download/<int:album_id>", methods=["POST"])
def api_download(album_id):
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"download:{client_ip}", rate_limit_store):
        return jsonify({
            "success": False,
            "message": "Too many requests, please slow down",
        }), 429
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
                            queue_data.append({
                                "id": row["album_id"],
                                "title": album.get("title", ""),
                                "artist": album.get("artist", {}).get("artistName", ""),
                                "cover_url": cover_url,
                            })
                data = {
                    "status": dict(download_process),
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


# --- Queue routes ---


@app.route("/api/download/queue", methods=["GET"])
def api_get_queue():
    queue_rows = models.get_queue()
    queue_with_details = []
    for row in queue_rows:
        album = _get_album_cached(row["album_id"])
        if "error" not in album:
            queue_with_details.append({
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
            })
    return jsonify(queue_with_details)


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
        return jsonify({
            "success": False,
            "message": "Too many bulk requests, please slow down",
        }), 429
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
    return jsonify({
        "success": True,
        "added": added,
        "queue_length": models.get_queue_length(),
    })


@app.route("/api/download/queue/<int:album_id>", methods=["DELETE"])
def api_remove_from_queue(album_id):
    models.dequeue_album(album_id)
    return jsonify({"success": True})


@app.route("/api/download/queue/clear", methods=["POST"])
def api_clear_queue():
    models.clear_queue()
    return jsonify({"success": True})


# --- History routes ---


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


# --- Stats ---


@app.route("/api/stats")
def api_stats():
    downloaded_today = models.get_history_count_today()
    in_queue = models.get_queue_length() + (1 if download_process["active"] else 0)
    return jsonify({
        "in_queue": in_queue,
        "downloaded_today": downloaded_today,
    })


# --- Logs routes ---


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    page = request.args.get("page", 1, type=int)
    per_page = request.args.get("per_page", 50, type=int)
    log_type = request.args.get("type", None, type=str)
    return jsonify(models.get_logs(page, per_page, log_type=log_type))


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


# --- Failed tracks / retry ---


@app.route("/api/download/failed")
def api_download_failed():
    album_id = models.get_latest_download_album_id()
    if album_id is None:
        return jsonify({
            "failed_tracks": [],
            "album_id": None,
            "album_title": "",
            "artist_name": "",
            "cover_url": "",
            "album_path": "",
            "lidarr_album_path": "",
        })
    return jsonify(models.get_failed_tracks_for_retry(album_id))


# --- Scheduler routes ---


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


# --- YouTube search ---


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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            try:
                music_results = ydl.extract_info(
                    f"ytmsearch10:{query}", download=False
                )
                for entry in (music_results or {}).get("entries", []):
                    url = entry.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        items.append({
                            "title": entry.get("title", ""),
                            "url": url,
                            "duration": entry.get("duration", 0),
                            "channel": (
                                entry.get("channel", "")
                                or entry.get("uploader", "")
                                or ""
                            ),
                            "thumbnail": entry.get("thumbnail", ""),
                            "source": "youtube_music",
                        })
            except Exception as e:
                logger.warning("YouTube Music search failed: %s", e)
            try:
                yt_results = ydl.extract_info(
                    f"ytsearch5:{query}", download=False
                )
                for entry in (yt_results or {}).get("entries", []):
                    url = entry.get("url", "")
                    if url and url not in seen_urls:
                        seen_urls.add(url)
                        items.append({
                            "title": entry.get("title", ""),
                            "url": url,
                            "duration": entry.get("duration", 0),
                            "channel": (
                                entry.get("channel", "")
                                or entry.get("uploader", "")
                                or ""
                            ),
                            "thumbnail": entry.get("thumbnail", ""),
                            "source": "youtube",
                        })
            except Exception as e:
                logger.warning("YouTube search failed: %s", e)
        return jsonify({"results": items})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)[:200]}), 500


# --- Manual download ---


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

    if not youtube_url or not track_title:
        return jsonify({"success": False, "message": "Missing required fields"}), 400

    youtube_url = _validate_youtube_url(youtube_url)
    if youtube_url is None:
        return jsonify({"success": False, "message": "Invalid YouTube URL"}), 400

    album_id_ctx = models.get_latest_download_album_id()
    if not album_id_ctx:
        return jsonify({
            "success": False,
            "message": "No album context available. Please re-download the album first.",
        }), 400

    album_data = lidarr_request(f"album/{album_id_ctx}")
    if "error" in album_data:
        return jsonify({
            "success": False,
            "message": f"Failed to fetch album from Lidarr: {album_data['error']}",
        }), 500

    failed_ctx = models.get_failed_tracks_for_retry(album_id_ctx)
    dl_album_path = failed_ctx.get("album_path", "")
    lidarr_album_path_val = failed_ctx.get("lidarr_album_path", "")
    target_path = (
        lidarr_album_path_val
        if lidarr_album_path_val and os.path.isdir(lidarr_album_path_val)
        else dl_album_path
    )

    if not target_path:
        return jsonify({"success": False, "message": "No album path available"}), 400

    config = load_config()
    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    os.makedirs(target_path, exist_ok=True)

    return _execute_manual_download(
        youtube_url, track_title, track_num, target_path,
        album_data, album_id_ctx, failed_ctx, config,
    )


# --- Helpers ---


def _get_album_cached(album_id):
    """Fetch album from Lidarr with a short TTL cache."""
    now = time.time()
    if album_id in album_cache:
        cached, ts = album_cache[album_id]
        if now - ts < ALBUM_CACHE_TTL:
            return cached
    album = lidarr_request(f"album/{album_id}")
    if "error" not in album:
        album_cache[album_id] = (album, now)
    return album


def _validate_youtube_url(youtube_url):
    """Validate and normalize a YouTube URL. Returns URL or None."""
    if not youtube_url.startswith("http"):
        if not re.match(r'^[a-zA-Z0-9_-]{11}$', youtube_url):
            return None
        return f"https://www.youtube.com/watch?v={youtube_url}"
    try:
        parsed = urllib.parse.urlparse(youtube_url)
        allowed_hosts = {
            "youtube.com", "www.youtube.com", "m.youtube.com",
            "youtu.be", "www.youtu.be", "music.youtube.com",
        }
        if parsed.hostname not in allowed_hosts:
            return None
    except Exception:
        return None
    return youtube_url


def _validate_target_path(target_path, config):
    """Ensure target_path is within DOWNLOAD_DIR or lidarr_path."""
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
    youtube_url, track_title, track_num, target_path,
    album_data, album_id_ctx, failed_ctx, config,
):
    """Download a single track manually and update state."""
    import yt_dlp

    sanitized_track = sanitize_filename(track_title)
    temp_file = os.path.join(target_path, f"temp_manual_{uuid.uuid4().hex[:8]}")
    final_file = os.path.join(
        target_path, f"{int(track_num):02d} - {sanitized_track}.mp3"
    )

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "bestaudio/best",
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "320",
        }],
        "outtmpl": temp_file,
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
            ydl.download([youtube_url])

        actual_file = temp_file + ".mp3"
        if not os.path.exists(actual_file):
            return jsonify({
                "success": False,
                "message": "Download failed -- file not created",
            }), 500

        track_info = {"title": track_title, "trackNumber": track_num}
        for t in album_data.get("tracks", []):
            if t.get("title", "").lower() == track_title.lower():
                track_info = t
                break

        tag_mp3(actual_file, track_info, album_data, None)

        if config.get("xml_metadata_enabled", True):
            create_xml_metadata(
                target_path,
                album_data["artist"]["artistName"],
                album_data["title"],
                int(track_num),
                track_title,
                album_data.get("foreignAlbumId", ""),
                album_data["artist"].get("foreignArtistId", ""),
            )

        try:
            manual_file_size = os.path.getsize(actual_file)
        except OSError:
            manual_file_size = 0
        shutil.move(actual_file, final_file)
        set_permissions(final_file)

        album_title = failed_ctx.get("album_title", "")
        artist_name = failed_ctx.get("artist_name", "")

        models.add_track_download(
            album_id=album_id_ctx, album_title=album_title,
            artist_name=artist_name, track_title=track_title,
            track_number=int(track_num), success=True,
            error_message="",
            youtube_url=youtube_url,
            youtube_title="Manual download",
            match_score=1.0,
            duration_seconds=0,
            album_path=failed_ctx.get("album_path", ""),
            lidarr_album_path=failed_ctx.get("lidarr_album_path", ""),
            cover_url=failed_ctx.get("cover_url", ""),
        )

        artist_id = album_data.get("artist", {}).get("id")
        if artist_id:
            lidarr_request(
                "command", method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )

        logger.info("Manual download successful: %s", track_title)

        models.add_log(
            log_type="manual_download",
            album_id=album_id_ctx or 0,
            album_title=album_title or "Unknown Album",
            artist_name=artist_name or "Unknown Artist",
            details=f"Manually downloaded track: {track_title} (from YouTube)",
            total_file_size=manual_file_size,
        )

        return jsonify({
            "success": True,
            "message": f"Track '{track_title}' downloaded successfully",
        })

    except Exception as e:
        logger.warning(
            "Manual download failed for '%s': %s",
            track_title, e, exc_info=True,
        )
        for ext in [".mp3", ".webm", ".m4a", ".part"]:
            tmp = temp_file + ext
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except OSError as rm_err:
                    logger.debug("Failed to remove temp file %s: %s", tmp, rm_err)
        return jsonify({"success": False, "message": str(e)[:200]}), 500


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
    logger.info("Application started successfully on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
