"""YouTube import: manual URL / playlist imports and watched playlists.

The ``/youtube`` page and its API, the playlist download runner shared with
Explore's album and artist imports, and the hourly playlist watch loop.
"""

import logging
import os
import re
import shutil
import threading
import time
import urllib.parse
import uuid

from flask import Blueprint, jsonify, render_template, request

import db
import explore
import external_playlists
import models
from config import load_config
from downloader import (
    cookiefile_for_ytdlp,
    download_track_youtube,
    download_youtube_candidate,
)
from lidarr import lidarr_request
from metadata import apply_file_extras, ensure_artist_image, tag_audio_file
from notifications import send_notifications
from processing import (
    _dir_has_audio,
    automatic_downloads_held,
    download_process,
    queue_lock,
)
from utils import (
    BaseNotMountedError,
    _cleanup_temp_files,
    _is_safe_stream_url,
    _json_object,
    _makedirs_bases_for,
    _resolve_write_base,
    _str_field,
    _synthetic_album_data,
    _validate_target_path,
    _validate_youtube_url,
    _YtdlpSilentLogger,
    check_rate_limit,
    makedirs_safe,
    rate_limit_store,
    sanitize_filename,
    set_permissions,
)

logger = logging.getLogger(__name__)

bp = Blueprint("youtube_import", __name__)


@bp.route("/youtube")
def youtube_import():
    return render_template("youtube.html")


def _flat_youtube_info(url, config):
    """yt-dlp info for a video or playlist, entries listed without download."""
    import yt_dlp

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "logger": _YtdlpSilentLogger(),
        "extract_flat": True,
        "noplaylist": False,
    }
    cookiefile = cookiefile_for_ytdlp(
        (config.get("yt_cookies_file") or "").strip(),
    )
    if cookiefile:
        ydl_opts["cookiefile"] = cookiefile
    if config.get("yt_force_ipv4", True):
        ydl_opts["source_address"] = "0.0.0.0"
    pc = config.get("yt_player_client", "android")
    if pc:
        ydl_opts["extractor_args"] = {"youtube": {"player_client": [pc]}}
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)


@bp.route("/api/youtube/playlist/info", methods=["POST"])
def api_youtube_playlist_info():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"playlist_info:{client_ip}", rate_limit_store, window=5, max_requests=3
    ):
        return jsonify({"error": "Too many requests"}), 429

    url = _str_field(_json_object(), "url")
    if not url:
        return jsonify({"error": "Missing URL"}), 400

    if external_playlists.is_external_url(url):
        try:
            result = external_playlists.fetch(url)
        except external_playlists.ExternalPlaylistError as e:
            return jsonify({"error": e.message}), e.status
        except Exception as e:
            logger.warning("External playlist read failed: %s", e)
            return jsonify({"error": "Could not read that link"}), 502
        return jsonify(external_playlists.info_response(result))

    if not _validate_youtube_url_for_playlist(url):
        return jsonify({"error": "Invalid YouTube URL"}), 400

    try:
        info = _flat_youtube_info(url, load_config())

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
            if "yt3.googleusercontent.com" not in (playlist_thumb or ""):
                ytm_thumb = _fetch_ytmusic_video_art(info.get("id", ""))
                if ytm_thumb:
                    logger.info(
                        "Replaced yt-dlp thumbnail with YT Music track art: %s",
                        ytm_thumb[:120],
                    )
                    playlist_thumb = _resize_yt3_url(ytm_thumb)
            logger.info(
                "Video thumbnail selected: %s (yt3=%s)",
                playlist_thumb[:120] if playlist_thumb else "(none)",
                "yt3.googleusercontent.com" in (playlist_thumb or ""),
            )

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


def _scrape_ytmusic_art(ytmusic_url, label=""):
    """Fetch a YouTube Music page and extract the largest yt3/lh3 album art URL.

    yt-dlp does not extract the square album cover from YouTube Music; this
    function scrapes the page HTML, which embeds the real cover URL in the
    initial data. Returns the largest yt3/lh3 thumbnail URL, or "".
    """
    import requests as http_requests
    logger.info("Fetching YT Music page (%s): %s", label or "art", ytmusic_url)
    try:
        resp = http_requests.get(
            ytmusic_url,
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
        matches = re.findall(
            r'(https://(?:yt3|lh3)\.googleusercontent\.com/[^"\s<>\\]*=w(\d+)-h(\d+)[^"\s<>\\]*)',
            resp.text,
        )
        logger.info(
            "Found %d googleusercontent thumbnails in YT Music page",
            len(matches),
        )
        if not matches:
            probe = re.findall(
                r'https://(?:yt3|lh3)\.googleusercontent\.com/[^\s"\'<>]+',
                resp.text,
            )
            if probe:
                logger.info(
                    "Sample googleusercontent URLs (no size match): %s",
                    probe[0][:200],
                )
            return ""
        best = max(matches, key=lambda m: int(m[1]) * int(m[2]))
        logger.info(
            "Selected best YT Music art (%sx%s): %s",
            best[1], best[2], best[0][:160],
        )
        return best[0]
    except Exception as e:
        logger.warning("YT Music art fetch failed: %s", e)
        return ""


def _fetch_ytmusic_album_art(playlist_id):
    """Scrape the square album art for an OLAK5uy_* playlist."""
    if not playlist_id:
        return ""
    if not (playlist_id.startswith("OLAK5uy_") or playlist_id.startswith("RDCLAK5uy_")):
        logger.info(
            "Skipping YT Music album art fetch (not an OLAK5uy_ playlist): %s",
            playlist_id,
        )
        return ""
    return _scrape_ytmusic_art(
        f"https://music.youtube.com/playlist?list={playlist_id}&hl=en",
        label="album",
    )


def _fetch_ytmusic_video_art(video_id):
    """Scrape the square album art for a single video/track from its YT Music page.

    Works for tracks that have an associated album on YouTube Music. For
    non-music videos the page may not contain a square cover, in which case
    "" is returned and the caller should keep its existing thumbnail.
    """
    if not video_id or not re.match(r"^[a-zA-Z0-9_-]{11}$", video_id):
        return ""
    return _scrape_ytmusic_art(
        f"https://music.youtube.com/watch?v={video_id}&hl=en",
        label="video",
    )


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


@bp.route("/api/youtube/playlist/download", methods=["POST"])
def api_youtube_playlist_download():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"playlist_dl:{client_ip}", rate_limit_store, window=10, max_requests=2
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429

    data = _json_object()
    artist_name = _str_field(data, "artist_name")
    album_title = _str_field(data, "album_title")
    entries = data.get("entries", [])
    thumbnail_url = _str_field(data, "thumbnail_url")
    source_url = _str_field(data, "source_url")
    if not isinstance(entries, list):
        return jsonify(
            {"success": False, "message": "entries must be a list"}
        ), 400

    if not artist_name and not album_title:
        return jsonify(
            {"success": False, "message": "At least one of artist_name or album_title is required"}
        ), 400

    if not entries:
        return jsonify({"success": False, "message": "No entries to download"}), 400

    validated_entries = []
    for entry in entries:
        if not isinstance(entry, dict):
            return jsonify(
                {"success": False, "message": "Each entry must be an object"}
            ), 400
        _str_field(entry, "url")
        _str_field(entry, "title", nullable=True)
        if not entry.get("url") and entry.get("search"):
            searched = _search_entry(entry)
            if searched is None:
                return jsonify(
                    {"success": False, "message": "Each track needs a title and an artist"}
                ), 400
            validated_entries.append(searched)
            continue
        v_url = _validate_youtube_url(entry.get("url") or "")
        if not v_url:
            return jsonify(
                {"success": False, "message": "Invalid YouTube URL in entries"}
            ), 400
        validated_entries.append({**entry, "url": v_url})

    return _start_playlist_import(
        artist_name, album_title, validated_entries, thumbnail_url, source_url,
    )


def _search_entry(entry):
    title = _str_field(entry, "title")[:300]
    artist = _str_field(entry, "artist")[:300]
    album = _str_field(entry, "album", nullable=True)[:300]
    if not title or not artist:
        return None
    try:
        duration = max(0, int(entry.get("duration") or 0))
    except (TypeError, ValueError):
        duration = 0
    return {"title": title, "artist": artist, "album": album, "duration": duration}


def _start_playlist_import(
    artist_name, album_title, validated_entries, thumbnail_url, source_url,
    release_year="", target_path=None,
):
    with queue_lock:
        if download_process.get("active"):
            return jsonify(
                {"success": False, "message": "A download is already in progress"}
            ), 409

    config = load_config()
    if target_path:
        return _launch_playlist_import(
            artist_name, album_title, validated_entries, target_path, config,
            thumbnail_url, source_url, release_year,
        )
    write_base = _playlist_write_base(config)
    if not write_base:
        return jsonify(
            {"success": False, "message": "No download path configured"}
        ), 400

    path_parts = [sanitize_filename(p) for p in (artist_name, album_title) if p]
    target_path = os.path.join(write_base, *path_parts)

    return _launch_playlist_import(
        artist_name, album_title, validated_entries, target_path, config,
        thumbnail_url, source_url, release_year,
    )


def _playlist_write_base(config):
    # When "save playlist imports to the music library" is on, write under
    # LIDARR_PATH so the files land where Jellyfin/Lidarr look (issue #79);
    # otherwise keep the legacy download-folder location.
    if config.get("playlist_to_library") and (
        config.get("lidarr_path") or ""
    ).strip():
        return config["lidarr_path"].strip()
    return _resolve_write_base(config)


# Watched playlists (issue #96): each video is one full-album upload (vinyl
# rips and the like), imported as Artist/Album (Year)/01 - Album.<ext>.
WATCH_INTERVAL_SECONDS = 3600


_watch_wakeup = threading.Event()


def _watch_playlists_once(config):
    """Import every video of the watched playlists not downloaded yet.

    A video counts as done once it has any track_downloads row (success or
    failure); failed ones are retried from the failed-tracks view, so a
    video that isn't downloadable doesn't cost a full attempt every cycle.
    """
    urls = config.get("yt_watch_playlists") or []
    if not urls:
        return
    seen = models.get_track_download_youtube_urls()
    base = _playlist_write_base(config)
    if not base:
        logger.warning("Playlist watch: no download path configured")
        return
    for playlist_url in urls:
        try:
            info = _flat_youtube_info(playlist_url, config) or {}
        except Exception as e:
            logger.warning("Playlist watch: cannot read %s: %s", playlist_url, e)
            continue
        for entry in info.get("entries") or []:
            video_id = (entry or {}).get("id") or ""
            if not re.match(r"^[\w-]{11}$", video_id):
                continue
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            if video_url in seen:
                continue
            artist, album, year = explore.album_from_video_title(
                entry.get("title") or "",
                entry.get("channel") or entry.get("uploader") or "",
            )
            artist = artist or "Unknown artist"
            target = os.path.join(
                base, sanitize_filename(artist) or "Unknown artist",
                _release_folder(album, year),
            )
            if not _validate_target_path(target, config):
                continue
            # Clear history / backup restore wipe track_downloads; audio in
            # the target folder still says this video was imported.
            if _dir_has_audio(target):
                logger.debug("Playlist watch: %s already on disk", target)
                seen.add(video_url)
                continue
            logger.info("Playlist watch: new upload %s — %s", artist, album)
            # Wait quietly for the download slot (the Lidarr queue can keep it
            # busy for a long time) and start nothing while the queue is paused
            # or a restart is pending. False = lost a race for the slot.
            logged = False
            while True:
                held = automatic_downloads_held()
                if held or download_process.get("active"):
                    if held and not logged:
                        logger.info(
                            "Playlist watch: queue paused or restart pending, "
                            "waiting to import %s — %s", artist, album,
                        )
                        logged = True
                    time.sleep(10)
                    continue
                if _execute_playlist_download(
                    artist, album, [{"title": album, "url": video_url}], target,
                    config, f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
                    playlist_url, year,
                ) is not False:
                    break
            seen.add(video_url)


def _playlist_watch_loop():
    timeout = 60  # first check shortly after startup
    while True:
        _watch_wakeup.wait(timeout)
        timeout = WATCH_INTERVAL_SECONDS
        _watch_wakeup.clear()
        try:
            _watch_playlists_once(load_config())
        except Exception:
            logger.exception("Playlist watch failed; will retry next cycle")
        finally:
            db.close_db()


def _launch_playlist_import(
    artist_name, album_title, validated_entries, target_path, config,
    thumbnail_url, source_url, release_year="",
):
    if not _validate_target_path(target_path, config):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    threading.Thread(
        target=_execute_playlist_download,
        args=(artist_name, album_title, validated_entries, target_path, config, thumbnail_url, source_url, release_year),
        daemon=True,
    ).start()
    return jsonify(
        {"success": True, "message": f"Downloading {len(validated_entries)} track(s)"}
    )


def _ensure_import_artist_image(target_path, artist_name, album_title):
    if not artist_name or not album_title:
        return
    artist_dir = os.path.dirname(os.path.normpath(target_path))
    if os.path.basename(artist_dir) != sanitize_filename(artist_name):
        return
    _ensure_artist_dir_image(artist_dir, artist_name)


def _ensure_artist_dir_image(artist_dir, artist_name, image_urls=()):
    try:
        ensure_artist_image(
            artist_dir, artist_name, image_urls,
            fallbacks=(lambda: explore.artist_image_for_name(artist_name),),
        )
    except Exception as e:
        logger.debug("Artist image step failed for %s: %s", artist_name, e)


def _maybe_scan_playlist_into_library(config, target_path, success_count):
    """Ask Lidarr to scan a finished playlist import into the library.

    Only when "save playlist imports to the music library" is on, Lidarr is
    configured and at least one track downloaded. Uses the path-based
    DownloadedAlbumsScan command (the playlist has no Lidarr artist/album to
    RefreshArtist against); Lidarr imports whatever it can match and the
    files are in the library path for Jellyfin to pick up on its next scan.
    """
    if not config.get("playlist_to_library") or success_count <= 0:
        return
    if not (config.get("lidarr_url") or "").strip():
        return
    try:
        result = lidarr_request(
            "command",
            method="POST",
            data={"name": "DownloadedAlbumsScan", "path": target_path},
        )
        if isinstance(result, dict) and "error" in result:
            logger.warning(
                "Lidarr DownloadedAlbumsScan failed for %s: %s",
                target_path, result["error"],
            )
        else:
            logger.info(
                "Requested Lidarr library scan of %s", target_path,
            )
    except Exception as exc:
        logger.warning(
            "Lidarr library scan request failed for %s: %s",
            target_path, exc,
        )


def _execute_playlist_download(
    artist_name, album_title, entries, target_path, config, thumbnail_url="",
    source_url="", release_year="",
):
    has_search = any(not e.get("url") for e in entries)
    written_files = []
    for _ in range(300):
        if not download_process["active"]:
            break
        time.sleep(1)
    else:
        logger.warning(
            "Playlist download timed out waiting for active download: %s",
            album_title,
        )
        return False

    with queue_lock:
        if download_process["active"]:
            logger.warning(
                "Playlist download aborted: another download became active: %s",
                album_title,
            )
            return False
        # Allocate the unique negative album_id under the lock, while no
        # other download is active, so two imports can't read the same MIN
        # and collide. It keeps this import's tracks distinct from other
        # playlists (and real Lidarr albums) in the history / retry views
        # (issue #83).
        playlist_album_id = models.next_playlist_album_id()
        download_process["active"] = True
        download_process["stop"] = False
        download_process["album_id"] = playlist_album_id
        download_process["album_title"] = album_title or artist_name
        download_process["artist_name"] = artist_name or album_title
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

    album_data = _synthetic_album_data(
        display_album, display_artist, track_count=len(entries),
    )
    if re.match(r"^\d{4}$", str(release_year or "")):
        album_data["releaseDate"] = f"{release_year}-01-01"
    cover_bytes = None

    total_size = 0
    success_count = 0
    try:
        try:
            makedirs_safe(target_path, _makedirs_bases_for(target_path, config))
        except (BaseNotMountedError, PermissionError) as exc:
            logger.error("Playlist target unusable: %s", exc)
            for ts in download_process.get("tracks", []):
                ts["status"] = "failed"
                ts["error_message"] = str(exc)
            return

        if thumbnail_url and (
            _is_safe_stream_url(thumbnail_url)
            or external_playlists.is_cover_url(thumbnail_url)
        ):
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
                    try:
                        with open(cover_path, "rb") as cf:
                            cover_bytes = cf.read() or None
                    except OSError:
                        cover_bytes = None
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
                    state["status"] = "downloading"
                    state["progress_percent"] = d.get("_percent_str", "0%").strip()
                    state["progress_speed"] = d.get("_speed_str", "N/A").strip()

            temp_file = os.path.join(
                target_path, f"temp_playlist_{uuid.uuid4().hex[:8]}"
            )
            # Same robust multi-client / multi-selector fallback as the album
            # and manual paths, so a playlist track doesn't fail on a single
            # "Requested format is not available" (issue #80).
            candidate = {
                "url": youtube_url, "title": track_title,
                "duration": 0, "score": 1.0, "source": "manual",
            }
            youtube_title = ""
            track_artist = entry.get("artist") or ""

            def _skip_check(state=track_state):
                return state.get("skip") or download_process.get("stop")

            try:
                if youtube_url:
                    dl_result = download_youtube_candidate(
                        candidate, temp_file,
                        progress_hook=_progress_hook,
                        skip_check=_skip_check,
                    )
                else:
                    track_state["status"] = "searching"
                    dl_result = download_track_youtube(
                        f"{track_artist} {track_title} official audio",
                        temp_file, track_title,
                        expected_duration_ms=(entry.get("duration") or 0) * 1000 or None,
                        progress_hook=_progress_hook,
                        skip_check=_skip_check,
                    )
                    if dl_result.get("youtube_url"):
                        youtube_url = dl_result["youtube_url"]
                        track_state["youtube_url"] = youtube_url
            except Exception as e:
                dl_result = {"success": False, "error_message": str(e)}
            if dl_result.get("skipped"):
                _cleanup_temp_files(temp_file)
                track_state["status"] = "skipped"
                continue
            if not dl_result.get("success"):
                msg = (dl_result.get("error_message") or "Download failed")[:200]
                logger.error(
                    "yt-dlp download failed for playlist track '%s': %s",
                    track_title, msg,
                )
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = msg
                _record_playlist_track(
                    album_id=playlist_album_id,
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, track_artist=track_artist, success=False,
                    error_message=msg, file_size=0,
                    cover_url=thumbnail_url, source_url=source_url,
                )
                continue
            youtube_title = dl_result.get("youtube_title", "") or track_title
            track_state["youtube_title"] = youtube_title

            audio_ext = config.get("audio_format", "mp3")
            actual_file = temp_file + f".{audio_ext}"
            if not os.path.exists(actual_file):
                _cleanup_temp_files(temp_file)
                track_state["status"] = "failed"
                track_state["error_message"] = "Download failed — file not created"
                _record_playlist_track(
                    album_id=playlist_album_id,
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, track_artist=track_artist, success=False,
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
                "trackArtist": track_artist,
            }

            try:
                tag_audio_file(actual_file, track_info, album_data, cover_bytes)
                file_size = os.path.getsize(actual_file)
                shutil.move(actual_file, final_file)
                apply_file_extras(
                    final_file, config, artist=track_artist or artist_name,
                    title=track_title, album=entry.get("album") or album_title,
                    duration=dl_result.get("duration_seconds", 0),
                )
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
                    album_id=playlist_album_id,
                    album_title=display_album, artist_name=display_artist,
                    track_title=track_title, track_num=track_num,
                    youtube_url=youtube_url, youtube_title=youtube_title,
                    target_path=target_path, track_artist=track_artist, success=False,
                    error_message=str(e)[:200], file_size=0,
                    cover_url=thumbnail_url, source_url=source_url,
                )
                continue

            track_state["status"] = "done"
            track_state["youtube_url"] = youtube_url
            track_state["youtube_title"] = youtube_title
            success_count += 1
            written_files.append((final_file, track_title, track_artist, dl_result))
            logger.info(
                "YouTube import [%d/%d] done: %s",
                i + 1, len(entries), track_title,
            )

            _record_playlist_track(
                album_id=playlist_album_id,
                album_title=album_title, artist_name=artist_name,
                track_title=track_title, track_num=track_num,
                youtube_url=youtube_url, youtube_title=youtube_title,
                target_path=target_path, track_artist=track_artist, success=True,
                error_message="", file_size=file_size,
                cover_url=thumbnail_url, source_url=source_url,
                source_format=dl_result.get("source_format", ""),
            )

        if has_search and written_files:
            _write_m3u(target_path, display_album, written_files)
        set_permissions(target_path)
        if success_count > 0:
            _ensure_import_artist_image(target_path, artist_name, album_title)
        _maybe_scan_playlist_into_library(config, target_path, success_count)

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
                album_id=playlist_album_id,
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
    return True


def _record_playlist_track(
    *, album_id, album_title, artist_name, track_title, track_num,
    youtube_url, youtube_title, target_path,
    success, error_message, file_size, cover_url="", source_url="",
    source_format="", track_artist="",
):
    track_download_id = None
    try:
        track_download_id = models.add_track_download(
            album_id=album_id,
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
            source_format=source_format,
            track_artist=track_artist,
        )
    except Exception as db_err:
        logger.error(
            "Failed to record playlist track '%s': %s", track_title, db_err,
        )
    try:
        if success:
            models.add_log(
                log_type="track_download",
                album_id=album_id,
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
                album_id=album_id,
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


@bp.route("/api/youtube/external/lidarr", methods=["POST"])
def api_external_to_lidarr():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(
        f"external_lidarr:{client_ip}", rate_limit_store, window=10, max_requests=2
    ):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    entries = _json_object().get("entries")
    if not isinstance(entries, list):
        return jsonify({"success": False, "message": "entries must be a list"}), 400
    try:
        status = external_playlists.start_lidarr_job(entries)
    except external_playlists.ExternalPlaylistError as e:
        return jsonify({"success": False, "message": e.message}), e.status
    return jsonify({"success": True, **status})


@bp.route("/api/youtube/external/lidarr/status")
def api_external_to_lidarr_status():
    return jsonify(external_playlists.lidarr_job_status())


@bp.route("/api/youtube/recent")
def api_youtube_recent():
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT album_id, MAX(album_title) as album_title,
               MAX(artist_name) as artist_name,
               MAX(cover_url) as cover_url,
               MAX(lidarr_album_path) as source_url,
               MAX(youtube_url) as youtube_url,
               COUNT(*) as track_count,
               SUM(CASE WHEN success=1 THEN 1 ELSE 0 END) as success_count,
               MAX(timestamp) as latest_timestamp
        FROM track_downloads
        WHERE album_id < 0
        GROUP BY album_id
        ORDER BY latest_timestamp DESC
        LIMIT 6
        """
    ).fetchall()
    return jsonify([dict(r) for r in rows])


def _write_m3u(target_path, name, written_files):
    lines = ["#EXTM3U"]
    for path, title, artist, dl_result in written_files:
        seconds = int(dl_result.get("duration_seconds") or -1)
        label = f"{artist} - {title}" if artist else title
        lines.append(f"#EXTINF:{seconds},{label}")
        lines.append(os.path.basename(path))
    m3u_path = os.path.join(
        target_path, (sanitize_filename(name) or "playlist") + ".m3u8",
    )
    try:
        with open(m3u_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
        set_permissions(m3u_path)
        logger.info("Playlist file written: %s", m3u_path)
    except OSError as e:
        logger.warning("Cannot write the playlist file %s: %s", m3u_path, e)


def _release_folder(title, year):
    name = sanitize_filename(title) or "Untitled"
    return f"{name} ({year})" if re.match(r"^\d{4}$", str(year or "")) else name
