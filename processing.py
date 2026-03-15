"""Download processing: album downloads, queue management, progress tracking.

Manages the active download process state, orchestrates per-album
downloads (search, download, tag, import to Lidarr), and processes
the download queue.
"""

import logging
import os
import shutil
import threading
import time
import uuid

import models
from config import load_config
from downloader import download_track_youtube
from lidarr import get_valid_release_id, lidarr_request
from metadata import (
    create_xml_metadata,
    get_itunes_artwork,
    get_itunes_tracks,
    tag_mp3,
)
from notifications import send_notifications
from utils import sanitize_filename, set_permissions

logger = logging.getLogger(__name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

download_process = {
    "active": False,
    "stop": False,
    "progress": {},
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "current_track_title": "",
    "cover_url": "",
}

queue_lock = threading.Lock()


def update_progress(d):
    """yt-dlp progress hook that updates download_process state."""
    if d["status"] == "downloading":
        download_process["progress"].update({
            "percent": d.get("_percent_str", "0%").strip(),
            "speed": d.get("_speed_str", "N/A").strip(),
        })


def get_download_status():
    """Return a snapshot of the current download process state."""
    with queue_lock:
        return dict(download_process)


def stop_download():
    """Signal the active download to stop and clear the queue."""
    with queue_lock:
        download_process["stop"] = True
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
        download_process["progress"] = {
            "current": 0,
            "total": 0,
            "percent": "0%",
            "speed": "N/A",
            "overall_percent": 0,
        }
        download_process["album_id"] = album_id
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["current_track_title"] = ""
        download_process["cover_url"] = ""

    failed_tracks = []
    album = {}
    album_title = ""
    artist_name = ""
    album_path = ""
    cover_data = None
    lidarr_album_path = ""
    total_downloaded_size = 0

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

        models.add_log(
            log_type="download_started",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Starting download of {len(tracks)} track(s)",
        )
        send_notifications(
            f"Download Started\n"
            f"Album: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Tracks: {len(tracks)}",
            log_type="download_started",
            embed_data={
                "title": "Download Started",
                "description": f"{artist_name} — {album_title}",
                "color": 0x3498DB,
                "fields": [
                    {"name": "Tracks", "value": str(len(tracks)),
                     "inline": True},
                ],
            },
        )

        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(cover_data)

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
        failed_tracks, total_downloaded_size = _download_tracks(
            tracks_to_download, album_path, album, album_ctx,
        )

        set_permissions(artist_path)

        result = _handle_post_download(
            failed_tracks, tracks_to_download, album_id,
            album_title, artist_name, total_downloaded_size,
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
        send_notifications(
            f"Download failed\nAlbum: {_album}\nArtist: {_artist}",
            log_type="album_error",
            embed_data={
                "title": "Download Failed",
                "description": f"{_artist} — {_album}",
                "color": 0xE74C3C,
            },
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
            download_process["progress"] = {}
            download_process["album_id"] = None
            download_process["album_title"] = ""
            download_process["artist_name"] = ""
            download_process["current_track_title"] = ""
            download_process["cover_url"] = ""


def _filter_tracks(tracks, force, album_path):
    """Filter tracks that need downloading."""
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
                album_path, f"{track_num:02d} - {sanitized_track}.mp3"
            )
            if os.path.exists(final_file):
                continue
        tracks_to_download.append(t)
    return tracks_to_download


def _download_tracks(
    tracks_to_download, album_path, album, album_ctx,
):
    """Download each track, tag, and create XML metadata.

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
    artist_name = album_ctx["artist_name"]
    album_title = album_ctx["album_title"]
    album_id = album_ctx["album_id"]
    album_mbid = album_ctx["album_mbid"]
    artist_mbid = album_ctx["artist_mbid"]
    cover_data = album_ctx["cover_data"]
    cover_url = album_ctx["cover_url"]
    lidarr_album_path = album_ctx["lidarr_album_path"]

    failed_tracks = []
    total_downloaded_size = 0

    for idx, track in enumerate(tracks_to_download, 1):
        if download_process["stop"]:
            logger.warning("Download stopped by user")
            break

        track_title = track["title"]
        try:
            track_num = int(track.get("trackNumber", idx))
        except (ValueError, TypeError):
            track_num = idx

        download_process["current_track_title"] = track_title
        download_process["progress"]["current"] = idx
        download_process["progress"]["total"] = len(tracks_to_download)
        download_process["progress"]["overall_percent"] = int(
            (idx / len(tracks_to_download)) * 100
        )

        logger.info(
            f"Downloading track {idx}/{len(tracks_to_download)}:"
            f" {track_title}"
        )

        sanitized_track = sanitize_filename(track_title)
        temp_file = os.path.join(
            album_path,
            f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}",
        )
        final_file = os.path.join(
            album_path, f"{track_num:02d} - {sanitized_track}.mp3"
        )

        track_duration_ms = track.get("duration")
        download_result = download_track_youtube(
            f"{artist_name} {track_title} official audio",
            temp_file,
            track_title,
            track_duration_ms,
            progress_hook=update_progress,
        )
        actual_file = temp_file + ".mp3"

        if download_result.get("success") and os.path.exists(actual_file):
            logger.info(
                f"Track downloaded successfully: {track_title}"
            )
            time.sleep(0.5)
            logger.info("Adding metadata tags...")
            tag_mp3(actual_file, track, album, cover_data)
            config = load_config()
            if config.get("xml_metadata_enabled", True):
                logger.info("Creating XML metadata file...")
                create_xml_metadata(
                    album_path, artist_name, album_title,
                    track_num, track_title, album_mbid, artist_mbid,
                )
            try:
                total_downloaded_size += os.path.getsize(actual_file)
            except OSError:
                pass
            shutil.move(actual_file, final_file)
            try:
                models.add_track_download(
                    album_id=album_id, album_title=album_title,
                    artist_name=artist_name, track_title=track_title,
                    track_number=track_num, success=True,
                    error_message="",
                    youtube_url=download_result.get("youtube_url", ""),
                    youtube_title=download_result.get(
                        "youtube_title", ""
                    ),
                    match_score=download_result.get("match_score", 0.0),
                    duration_seconds=download_result.get(
                        "duration_seconds", 0
                    ),
                    album_path=album_path,
                    lidarr_album_path=lidarr_album_path,
                    cover_url=cover_url,
                )
            except Exception:
                logger.error(
                    "Failed to record track download for '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )
        else:
            fail_reason = download_result.get(
                "error_message", "Download failed or file not found"
            )
            logger.warning(
                f"Failed to download track: {track_title}"
                f" -- {fail_reason}"
            )
            for ext in [".mp3", ".webm", ".m4a", ".part", ""]:
                tmp = temp_file + ext
                if os.path.exists(tmp):
                    try:
                        os.remove(tmp)
                    except OSError as rm_err:
                        logger.debug("Failed to remove temp file %s: %s", tmp, rm_err)
            failed_tracks.append({
                "title": track_title,
                "reason": fail_reason,
                "track_num": track_num,
            })
            try:
                models.add_track_download(
                    album_id=album_id, album_title=album_title,
                    artist_name=artist_name, track_title=track_title,
                    track_number=track_num, success=False,
                    error_message=fail_reason,
                    youtube_url="", youtube_title="",
                    match_score=0.0, duration_seconds=0,
                    album_path=album_path,
                    lidarr_album_path=lidarr_album_path,
                    cover_url=cover_url,
                )
            except Exception:
                logger.error(
                    "Failed to record track download for '%s' (album %d)",
                    track_title, album_id, exc_info=True,
                )

        download_process["progress"]["current"] = idx
        download_process["progress"]["total"] = len(tracks_to_download)
        download_process["progress"]["overall_percent"] = int(
            (idx / len(tracks_to_download)) * 100
        )

    return failed_tracks, total_downloaded_size


def _handle_post_download(
    failed_tracks, tracks_to_download, album_id,
    album_title, artist_name, total_downloaded_size,
):
    """Log and notify about download results.

    Returns:
        A result dict if download should stop (all failed), else None.
    """
    if failed_tracks:
        failed_list = "\n".join(
            [f"* {t['title']}" for t in failed_tracks]
        )

        if len(failed_tracks) == len(tracks_to_download):
            send_notifications(
                f"Download Failed (All Tracks)\n"
                f"Album: {album_title}\nArtist: {artist_name}\n\n"
                f"Failed tracks:\n{failed_list}",
                log_type="album_error",
                embed_data={
                    "title": "Download Failed",
                    "description": f"{artist_name} — {album_title}",
                    "color": 0xE74C3C,
                    "fields": [{
                        "name": "Failed Tracks",
                        "value": failed_list[:1024],
                        "inline": False,
                    }],
                },
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
                    f"All {len(tracks_to_download)} track(s)"
                    " failed to download"
                ),
            )
            download_process["result_success"] = False
            return {"error": "All tracks failed to download"}

        download_process["result_partial"] = True
        send_notifications(
            f"Partial Download Completed\n"
            f"Album: {album_title}\nArtist: {artist_name}\n\n"
            f"Failed tracks:\n{failed_list}",
            log_type="partial_success",
            embed_data={
                "title": "Partial Download",
                "description": f"{artist_name} — {album_title}",
                "color": 0xE67E22,
                "fields": [{
                    "name": "Failed Tracks",
                    "value": failed_list[:1024],
                    "inline": False,
                }],
            },
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
                f" out of {len(tracks_to_download)}"
            ),
            total_file_size=total_downloaded_size,
        )
    else:
        models.add_log(
            log_type="download_success",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=(
                f"Successfully downloaded"
                f" {len(tracks_to_download)} track(s)"
            ),
            total_file_size=total_downloaded_size,
        )
        send_notifications(
            f"Download successful\n"
            f"Album: {album_title}\nArtist: {artist_name}\n"
            f"Tracks: {len(tracks_to_download)}"
            f"/{len(tracks_to_download)}",
            log_type="download_success",
            embed_data={
                "title": "Download Successful",
                "description": f"{artist_name} — {album_title}",
                "color": 0x2ECC71,
                "fields": [{
                    "name": "Tracks",
                    "value": (
                        f"{len(tracks_to_download)}"
                        f"/{len(tracks_to_download)}"
                    ),
                    "inline": True,
                }],
            },
        )
        logger.info("All tracks downloaded successfully")

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
    total_downloaded_size,
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
        send_notifications(
            f"Import Partial\nAlbum: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Refreshing in Lidarr"
            f" (Missing {len(failed_tracks)} tracks)",
            log_type="import_partial",
            embed_data={
                "title": "Import Partial",
                "description": f"{artist_name} — {album_title}",
                "color": 0xE67E22,
                "fields": [{
                    "name": "Missing Tracks",
                    "value": str(len(failed_tracks)),
                    "inline": True,
                }],
            },
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
        send_notifications(
            f"Import Success\nAlbum: {album_title}\n"
            f"Artist: {artist_name}\n"
            f"Refreshing in Lidarr",
            log_type="import_success",
            embed_data={
                "title": "Import Successful",
                "description": f"{artist_name} — {album_title}",
                "color": 0x2ECC71,
            },
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
