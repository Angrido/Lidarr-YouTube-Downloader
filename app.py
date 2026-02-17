import os
import json
import time
import threading
import shutil
import re
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC, TXXX, UFID
import yt_dlp
import schedule

logging.basicConfig(
    level=logging.INFO, format="%(message)s", handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

log = logging.getLogger("werkzeug")
log.setLevel(logging.ERROR)

app = Flask(__name__)

VERSION = "1.3.0"

CONFIG_FILE = "/config/config.json"
DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")
download_process = {
    "active": False,
    "stop": False,
    "progress": {},
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "current_track_title": "",
}

download_queue = []
download_history = []
download_logs = []
queue_lock = threading.Lock()

HISTORY_FILE = "/config/download_history.json"
LOGS_FILE = "/config/download_logs.json"


def load_persistent_data():
    global download_history, download_logs
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                download_history = json.load(f)
        except:
            download_history = []
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r") as f:
                download_logs = json.load(f)
        except:
            download_logs = []


def save_history():
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with open(HISTORY_FILE, "w") as f:
            json.dump(download_history[-200:], f)
    except:
        pass


def save_logs():
    try:
        os.makedirs(os.path.dirname(LOGS_FILE), exist_ok=True)
        with open(LOGS_FILE, "w") as f:
            json.dump(download_logs[-100:], f)
    except:
        pass


def load_config():
    config = {
        "lidarr_url": os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": os.getenv("LIDARR_API_KEY", ""),
        "lidarr_path": os.getenv("LIDARR_PATH", ""),                               
        "download_path": os.getenv("DOWNLOAD_PATH", ""),                             
        "scheduler_enabled": os.getenv("SCHEDULER_ENABLED", "false").lower() == "true",
        "scheduler_auto_download": os.getenv("SCHEDULER_AUTO_DOWNLOAD", "true").lower()
        == "true",
        "scheduler_interval": int(os.getenv("SCHEDULER_INTERVAL", "60")),
        "telegram_enabled": os.getenv("TELEGRAM_ENABLED", "false").lower() == "true",
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_log_types": [
            "partial_success",
            "import_partial",
            "import_failed",
            "album_error",
        ],                        
        "xml_metadata_enabled": os.getenv("XML_METADATA_ENABLED", "true").lower()
        == "true",
        "forbidden_words": ["remix", "cover", "mashup", "bootleg", "live", "dj mix"],
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": os.getenv("YT_FORCE_IPV4", "true").lower() == "true",
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_retries": int(os.getenv("YT_RETRIES", "10")),
        "yt_fragment_retries": int(os.getenv("YT_FRAGMENT_RETRIES", "10")),
        "yt_sleep_requests": int(os.getenv("YT_SLEEP_REQUESTS", "1")),
        "yt_sleep_interval": int(os.getenv("YT_SLEEP_INTERVAL", "1")),
        "yt_max_sleep_interval": int(os.getenv("YT_MAX_SLEEP_INTERVAL", "5")),
        "path_conflict": False,
    }
    
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
                for key in config.keys():
                    if key in file_config:
                        config[key] = file_config[key]
            if "scheduler_interval" in config:
                config["scheduler_interval"] = int(config["scheduler_interval"])
        except:
            pass

    def norm(p):
        return os.path.normcase(os.path.abspath(str(p))).rstrip("\\/") if p else ""

    l_path = norm(config.get("lidarr_path"))
    d_path = norm(config.get("download_path"))
    
    config["path_conflict"] = bool(l_path and l_path == d_path)
    
    if config["path_conflict"]:
        logger.warning(f"⚠️ Path Conflict Detected: {l_path}")

    return config


def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if "scheduler_interval" in config:
        config["scheduler_interval"] = int(config["scheduler_interval"])
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def send_telegram(message, log_type=None):
    config = load_config()
    if (
        config.get("telegram_enabled")
        and config.get("telegram_bot_token")
        and config.get("telegram_chat_id")
    ):
                                                                      
        if log_type is not None:
            allowed_types = config.get("telegram_log_types", [])
            if log_type not in allowed_types:
                return                                          

        try:
            url = f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage"
            requests.post(
                url,
                json={"chat_id": config["telegram_chat_id"], "text": message},
                timeout=10,
            )
        except:
            pass


def add_download_log(
    log_type, album_id, album_title, artist_name, details=None, failed_tracks=None
):
    with queue_lock:
        log_entry = {
            "id": f"{int(time.time() * 1000)}_{album_id}",
            "type": log_type,
            "album_id": album_id,
            "album_title": album_title,
            "artist_name": artist_name,
            "timestamp": time.time(),
            "details": details or "",
            "failed_tracks": failed_tracks or [],
            "dismissed": False,
        }
        download_logs.append(log_entry)
        if len(download_logs) > 100:
            download_logs.pop(0)
        save_logs()


def lidarr_request(endpoint, method="GET", data=None, params=None):
    config = load_config()
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, params=params, timeout=30)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def get_missing_albums():
    try:
        wanted = lidarr_request(
            "wanted/missing?pageSize=2000&sortKey=releaseDate&sortDirection=descending&includeArtist=true"
        )
        if isinstance(wanted, dict) and "records" in wanted:
            records = wanted.get("records", [])
            for album in records:
                stats = album.get("statistics", {})
                total = stats.get("trackCount", 0)
                files = stats.get("trackFileCount", 0)
                album["missingTrackCount"] = total - files
            return records
        return []
    except:
        return []


def get_itunes_tracks(artist, album_name):
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{artist} {album_name}", "entity": "album", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            collection_id = data["results"][0]["collectionId"]
            lookup_url = "https://itunes.apple.com/lookup"
            lookup_params = {"id": collection_id, "entity": "song"}
            lookup_r = requests.get(lookup_url, params=lookup_params, timeout=10)
            lookup_data = lookup_r.json()
            tracks = []
            for item in lookup_data.get("results", [])[1:]:
                tracks.append(
                    {
                        "trackNumber": item.get("trackNumber"),
                        "title": item.get("trackName"),
                        "previewUrl": item.get("previewUrl"),
                        "hasFile": False,
                    }
                )
            return tracks
    except:
        pass
    return []


def get_itunes_artwork(artist, album):
    try:
        url = "https://itunes.apple.com/search"
        params = {"term": f"{artist} {album}", "entity": "album", "limit": 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            artwork_url = (
                data["results"][0]
                .get("artworkUrl100", "")
                .replace("100x100", "3000x3000")
            )
            return requests.get(artwork_url, timeout=15).content
    except:
        pass
    return None


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip()


def download_track_youtube(query, output_path, track_title_original, expected_duration_ms=None):
    def build_common_opts(player_client=None):
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
            logger.warning(f"⚠️ YT_COOKIES_FILE not found: {cookies_path}")
        if cfg.get("yt_force_ipv4", True):
            opts["source_address"] = "0.0.0.0"
        if player_client:
            opts["extractor_args"] = {"youtube": {"player_client": [player_client]}}
        return opts

    config = load_config()
    ydl_opts_search = {
        **build_common_opts(player_client=config.get("yt_player_client", "android") or None),
        "format": "bestaudio/best",
        "extract_flat": True,
    }

    candidates = []
    forbidden_words = config.get("forbidden_words", ["remix", "cover", "mashup", "bootleg", "live", "dj mix"])
    duration_tolerance = config.get("duration_tolerance", 10)
    
    expected_duration_sec = None
    if expected_duration_ms:
        expected_duration_sec = expected_duration_ms / 1000.0
        logger.info(f"📏 Expected track duration: {int(expected_duration_sec // 60)}:{int(expected_duration_sec % 60):02d} ({int(expected_duration_sec)}s)")

    try:
        with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
            search_results = ydl.extract_info(f"ytsearch10:{query}", download=False)

            for entry in search_results.get("entries", []):
                title = entry.get("title", "").lower()
                url = entry.get("url")
                duration = entry.get("duration", 0)

                is_clean = True
                for word in forbidden_words:
                    if word in title and word not in track_title_original.lower():
                        logger.debug(f"   ⊗ Rejected '{entry.get('title', '')}' - contains forbidden word '{word}'")
                        is_clean = False
                        break

                if not is_clean:
                    continue

                if expected_duration_sec:
                    min_duration = max(15, expected_duration_sec - duration_tolerance)
                    max_duration = expected_duration_sec + duration_tolerance
                    
                    if duration < min_duration or duration > max_duration:
                        logger.debug(f"   ⊗ Rejected '{entry.get('title', '')}' - duration {int(duration)}s outside range [{int(min_duration)}s - {int(max_duration)}s]")
                        continue
                    
                    duration_diff = abs(duration - expected_duration_sec)
                    duration_score = 1000 - duration_diff
                else:
                    if duration < 15 or duration > 7200:
                        logger.debug(f"   ⊗ Rejected '{entry.get('title', '')}' - duration {int(duration)}s outside permissive range [15s - 7200s]")
                        continue
                    duration_score = 500

                if url:
                    candidates.append({
                        "url": url,
                        "title": entry.get("title", ""),
                        "duration": duration,
                        "score": duration_score
                    })
                    logger.debug(f"   ✓ Added candidate '{entry.get('title', '')}' - duration {int(duration)}s, score {int(duration_score)}")
    except Exception as e:
        logger.error(f"   ❌ Search failed: {str(e)}")
        pass

    if not candidates:
        logger.warning(f"   ⚠️  No suitable candidates found after filtering")
        return False

    candidates.sort(key=lambda x: x["score"], reverse=True)
    
    if expected_duration_sec:
        best_candidate = candidates[0]
        duration_diff = abs(best_candidate["duration"] - expected_duration_sec)
        logger.info(f"   🎯 Best match: '{best_candidate['title']}' (duration: {int(best_candidate['duration'])}s, diff: {int(duration_diff)}s)")
    else:
        logger.info(f"   🎯 Selected: '{candidates[0]['title']}' (duration: {int(candidates[0]['duration'])}s)")

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
                **build_common_opts(player_client=pc),
                "format": "bestaudio/best",
                "postprocessors": [
                    {
                        "key": "FFmpegExtractAudio",
                        "preferredcodec": "mp3",
                        "preferredquality": "320",
                    }
                ],
                "outtmpl": output_path,
                "progress_hooks": [lambda d: update_progress(d)],
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_dl:
                    ydl_dl.download([candidate["url"]])
                return True
            except Exception as e:
                last_err = e
                msg = str(e)
                if "403" in msg:
                    logger.debug(
                        f"   ⊗ 403 with player_client={pc or 'default'}; ensure cookies are provided (YT_COOKIES_FILE) and try again"
                    )
                else:
                    logger.debug(
                        f"   ⊗ Failed with player_client={pc or 'default'}; {msg[:180]}"
                    )
                continue
        if last_err:
            logger.debug(
                f"   ⚠️  Failed to download '{candidate['title']}' after trying multiple client profiles."
            )
        continue

    return False


def update_progress(d):
    if d["status"] == "downloading":
        download_process["progress"].update(
            {
                "percent": d.get("_percent_str", "0%").strip(),
                "speed": d.get("_speed_str", "N/A").strip(),
            }
        )


def set_permissions(path):
    try:
        if os.path.isdir(path):
            os.chmod(path, 0o777)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o777)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o666)
        else:
            os.chmod(path, 0o666)
    except:
        pass


def tag_mp3(file_path, track_info, album_info, cover_data):
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except:
            audio = MP3(file_path)
            audio.add_tags()
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info["title"]))
        audio.tags.add(TPE1(encoding=3, text=album_info["artist"]["artistName"]))
        audio.tags.add(TPE2(encoding=3, text=album_info["artist"]["artistName"]))
        audio.tags.add(TALB(encoding=3, text=album_info["title"]))
        audio.tags.add(
            TDRC(encoding=3, text=str(album_info.get("releaseDate", "")[:4]))
        )

        try:
            t_num = int(track_info["trackNumber"])
            audio.tags.add(
                TRCK(encoding=3, text=f"{t_num}/{album_info.get('trackCount', 0)}")
            )
        except:
            pass

        if album_info.get("releases"):
            release = album_info["releases"][0]
            if track_info.get("foreignRecordingId"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="MusicBrainz Release Track Id",
                        text=track_info["foreignRecordingId"],
                    )
                )
            if release.get("foreignReleaseId"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="MusicBrainz Album Id",
                        text=release["foreignReleaseId"],
                    )
                )
            if album_info["artist"].get("foreignArtistId"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="MusicBrainz Artist Id",
                        text=album_info["artist"]["foreignArtistId"],
                    )
                )
            if album_info.get("foreignAlbumId"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="MusicBrainz Album Release Group Id",
                        text=album_info["foreignAlbumId"],
                    )
                )
            if release.get("country"):
                audio.tags.add(
                    TXXX(
                        encoding=3,
                        desc="MusicBrainz Release Country",
                        text=release["country"],
                    )
                )

        if track_info.get("foreignRecordingId"):
            audio.tags.add(
                UFID(
                    owner="http://musicbrainz.org",
                    data=track_info["foreignRecordingId"].encode(),
                )
            )
        if cover_data:
            audio.tags.add(
                APIC(
                    encoding=3, mime="image/jpeg", type=3, desc="Cover", data=cover_data
                )
            )

        audio.save(v2_version=3)
        return True
    except:
        return False


def create_xml_metadata(
    output_dir, artist, album, track_num, title, album_id=None, artist_id=None
):
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        mb_album = (
            f"  <musicbrainzalbumid>{album_id}</musicbrainzalbumid>\n"
            if album_id
            else ""
        )
        mb_artist = (
            f"  <musicbrainzartistid>{artist_id}</musicbrainzartistid>\n"
            if artist_id
            else ""
        )
        content = f"""<song>
  <title>{title}</title>
  <artist>{artist}</artist>
  <performingartist>{artist}</performingartist>
  <albumartist>{artist}</albumartist>
  <album>{album}</album>
{mb_album}{mb_artist}</song>"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except:
        return False


def get_valid_release_id(album):
    releases = album.get("releases", [])
    if not releases:
        return 0
    for rel in releases:
        if rel.get("monitored", False) and rel.get("id", 0) > 0:
            return rel["id"]
    for rel in releases:
        if rel.get("id", 0) > 0:
            return rel["id"]
    return 0


def process_album_download(album_id, force=False):
    if download_process["active"]:
        return {"error": "Busy"}
    download_process["active"] = True
    download_process["stop"] = False
    download_process["result_success"] = True
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

    try:
        album = lidarr_request(f"album/{album_id}")
        if "error" in album:
            logger.error(f"❌ Error fetching album {album_id}: {album['error']}")
            return album

        logger.info(
            f"🎵 Starting download for album: {album.get('title', 'Unknown')} - {album.get('artist', {}).get('artistName', 'Unknown')}"
        )

        tracks = album.get("tracks", [])
        if not tracks:
            try:
                tracks_res = lidarr_request(f"track?albumId={album_id}")
                if isinstance(tracks_res, list) and len(tracks_res) > 0:
                    tracks = tracks_res
            except:
                pass

        if not tracks:
            tracks = get_itunes_tracks(album["artist"]["artistName"], album["title"])

        album["tracks"] = tracks

        artist_name = album["artist"]["artistName"]
        artist_id = album["artist"]["id"]
        artist_mbid = album["artist"].get("foreignArtistId", "")
        album_title = album["title"]
        release_year = str(album.get("releaseDate", ""))[:4]

                                                 
        download_process["album_title"] = album_title
        download_process["artist_name"] = artist_name

        release_id = get_valid_release_id(album)
        if release_id == 0:
            return {"error": "No valid releases found for this album."}

        album_mbid = album.get("foreignAlbumId", "")

        sanitized_artist = sanitize_filename(artist_name)
        sanitized_album = sanitize_filename(album_title)

        artist_path = os.path.join(DOWNLOAD_DIR, sanitized_artist)
        album_folder_name = (
            f"{sanitized_album} ({release_year})" if release_year else sanitized_album
        )
        album_path = os.path.join(artist_path, album_folder_name)
        os.makedirs(album_path, exist_ok=True)

                              
        add_download_log(
            log_type="download_started",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Starting download of {len(tracks)} track(s)",
            failed_tracks=[],
        )
        send_telegram(
            f"🎵 Download Started\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📦 Tracks: {len(tracks)}",
            log_type="download_started",
        )

        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, "cover.jpg"), "wb") as f:
                f.write(cover_data)

        tracks_to_download = []
        for t in tracks:
            if not force:
                if t.get("hasFile", False):
                    continue
                try:
                    track_num = int(t.get("trackNumber", 0))
                except:
                    track_num = 0

                track_title = t["title"]
                sanitized_track = sanitize_filename(track_title)
                final_file = os.path.join(
                    album_path, f"{track_num:02d} - {sanitized_track}.mp3"
                )
                if os.path.exists(final_file):
                    continue

            tracks_to_download.append(t)

        if len(tracks_to_download) == 0:
            lidarr_request(
                "command",
                method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )
            return {"success": True, "message": "Skipped"}

        logger.info(f"📦 Total tracks to download: {len(tracks_to_download)}")

        failed_tracks = []

        for idx, track in enumerate(tracks_to_download, 1):
            if download_process["stop"]:
                logger.warning(f"⏹️  Download stopped by user")
                return {"stopped": True}

            track_title = track["title"]
            try:
                track_num = int(track.get("trackNumber", idx))
            except:
                track_num = idx

                                       
            download_process["current_track_title"] = track_title
            download_process["progress"]["current"] = idx
            download_process["progress"]["total"] = len(tracks_to_download)
            download_process["progress"]["overall_percent"] = int(
                (idx / len(tracks_to_download)) * 100
            )

            logger.info(
                f"⬇️  Downloading track {idx}/{len(tracks_to_download)}: {track_title}"
            )

            sanitized_track = sanitize_filename(track_title)

            temp_file = os.path.join(album_path, f"temp_{track_num:02d}")
            final_file = os.path.join(
                album_path, f"{track_num:02d} - {sanitized_track}.mp3"
            )

            track_duration_ms = track.get("duration")
            
            download_success = False
            queries_to_try = []
            
            queries_to_try.append(f"{artist_name} {track_title} official audio")
            
            if "/" in track_title or " - " in track_title:
                simplified_title = track_title.split("/")[0].split(" - ")[0].strip()
                if simplified_title != track_title:
                    queries_to_try.append(f"{artist_name} {simplified_title} official audio")
                    logger.info(f"   💡 Will try simplified query: '{simplified_title}'")
            
            for idx, query in enumerate(queries_to_try):
                if idx > 0:
                    logger.info(f"   🔄 Trying alternative search query ({idx+1}/{len(queries_to_try)})...")
                download_success = download_track_youtube(query, temp_file, track_title, track_duration_ms)
                if download_success:
                    break
            actual_file = temp_file + ".mp3"

            if download_success and os.path.exists(actual_file):
                logger.info(f"✅ Track downloaded successfully: {track_title}")
                time.sleep(0.5)
                logger.info(f"🏷️  Adding metadata tags...")
                tag_mp3(actual_file, track, album, cover_data)
                config = load_config()
                if config.get("xml_metadata_enabled", True):
                    logger.info(f"📄 Creating XML metadata file...")
                    create_xml_metadata(
                        album_path,
                        artist_name,
                        album_title,
                        track_num,
                        track_title,
                        album_mbid,
                        artist_mbid,
                    )
                shutil.move(actual_file, final_file)
            else:
                logger.warning(f"⚠️  Failed to download track: {track_title}")
                failed_tracks.append(track_title)

                                                    
            download_process["progress"]["current"] = idx
            download_process["progress"]["total"] = len(tracks_to_download)
            download_process["progress"]["overall_percent"] = int(
                (idx / len(tracks_to_download)) * 100
            )

        set_permissions(artist_path)

        if failed_tracks:
            failed_list = "\n".join([f"• {t}" for t in failed_tracks])

            if len(failed_tracks) == len(tracks_to_download):
                send_telegram(
                    f"❌ Download Failed (All Tracks)\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="album_error",
                )
                logger.error(
                    f"❌ All {len(failed_tracks)} tracks failed to download. Skipping import."
                )
                add_download_log(
                    log_type="album_error",
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    details=f"All {len(tracks_to_download)} track(s) failed to download",
                    failed_tracks=failed_tracks,
                )
                download_process["result_success"] = False
                return {"error": "All tracks failed to download"}

            else:
                send_telegram(
                    f"⚠️ Partial Download Completed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="partial_success",
                )
                logger.warning(
                    f"⚠️  Download completed with {len(failed_tracks)} failed tracks. Proceeding with import."
                )
                add_download_log(
                    log_type="partial_success",
                    album_id=album_id,
                    album_title=album_title,
                    artist_name=artist_name,
                    details=f"{len(failed_tracks)} track(s) failed to download out of {len(tracks_to_download)}",
                    failed_tracks=failed_tracks,
                )
        else:
            add_download_log(
                log_type="download_success",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=f"Successfully downloaded {len(tracks_to_download)} track(s)",
                failed_tracks=[],
            )
            send_telegram(
                f"✅ Download successful\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📦 Tracks: {len(tracks_to_download)}/{len(tracks_to_download)}",
                log_type="download_success",
            )
            logger.info(f"✅ All tracks downloaded successfully")

        logger.info(f"📥 Importing album to Lidarr...")

                               
        config = load_config()
        lidarr_path = config.get("lidarr_path", "")

                                                       
        if lidarr_path:
            abs_lidarr = os.path.abspath(lidarr_path)
            abs_download = os.path.abspath(DOWNLOAD_DIR)
            
            if abs_lidarr == abs_download:
                logger.warning("⚠️ LIDARR_PATH matches DOWNLOAD_PATH. Skipping move and cleanup to prevent data loss.")
                lidarr_path = ""
            else:
                logger.info(f"📂 Moving files to Lidarr music folder: {lidarr_path}")
            lidarr_artist_path = os.path.join(lidarr_path, sanitized_artist)
            lidarr_album_path = os.path.join(lidarr_artist_path, album_folder_name)

            try:
                                                          
                os.makedirs(lidarr_album_path, exist_ok=True)

                                                                      
                for item in os.listdir(album_path):
                    src = os.path.join(album_path, item)
                    dst = os.path.join(lidarr_album_path, item)
                    if os.path.isfile(src):
                        shutil.copy2(src, dst)
                        logger.info(f"  ✓ Copied: {item}")

                set_permissions(lidarr_artist_path)
                logger.info(f"✅ Files copied to Lidarr folder successfully")

                                            
                import_path = lidarr_album_path
            except Exception as e:
                logger.error(f"❌ Error copying files to Lidarr folder: {str(e)}")
                                                         
                import_path = album_path
        else:
            import_path = album_path

        logger.info(f"✅ Album downloaded successfully: {artist_name} - {album_title}")

        if failed_tracks:
            add_download_log(
                log_type="import_partial",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=f"Album imported with {len(failed_tracks)} failed tracks",
                failed_tracks=failed_tracks,
            )

            send_telegram(
                f"⚠️ Import Partial\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📚 Refreshing in Lidarr (Missing {len(failed_tracks)} tracks)",
                log_type="import_partial",
            )
        else:
            add_download_log(
                log_type="import_success",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details="Album downloaded and refreshing in Lidarr",
                failed_tracks=[],
            )

            send_telegram(
                f"✅ Import Success\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📚 Refreshing in Lidarr",
                log_type="import_success",
            )

        lidarr_request(
            "command",
            method="POST",
            data={"name": "RefreshArtist", "artistId": artist_id},
        )

        if lidarr_path and os.path.exists(artist_path):
            try:
                logger.info(f"🧹 Cleaning up download folder: {artist_path}")
                shutil.rmtree(artist_path)
                logger.info(f"✅ Download folder cleaned up successfully")
            except Exception as e:
                logger.warning(f"⚠️  Failed to cleanup download folder: {str(e)}")

        return {"success": True}

    except Exception as e:
        logger.error(f"❌ Error during album download: {str(e)}")
        artist_name = download_process.get("artist_name", "Unknown")
        album_title = download_process.get("album_title", "Unknown")
        send_telegram(
            f"❌ Download failed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}",
            log_type="album_error",
        )
        add_download_log(
            log_type="album_error",
            album_id=album_id,
            album_title=album_title,
            artist_name=artist_name,
            details=f"Error: {str(e)}",
            failed_tracks=[],
        )
        download_process["result_success"] = False
        return {"error": str(e)}
    finally:
        with queue_lock:
            download_history.append(
                {
                    "album_id": download_process.get("album_id"),
                    "album_title": download_process.get("album_title", ""),
                    "artist_name": download_process.get("artist_name", ""),
                    "success": download_process.get("result_success", True),
                    "timestamp": time.time(),
                }
            )
            save_history()
        download_process["active"] = False
        download_process["progress"] = {}
        download_process["album_id"] = None
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["current_track_title"] = ""


@app.route("/api/test-connection")
def api_test_connection():
    try:
        system = lidarr_request("system/status")
        return jsonify(
            {
                "status": "success" if "version" in system else "error",
                "lidarr_version": system.get("version", "Unknown"),
            }
        )
    except:
        return jsonify({"status": "error"})


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
    else:
        current = load_config()
        current.update(request.json)
        save_config(current)
        return jsonify({"success": True})


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


@app.route("/api/download/<int:album_id>", methods=["POST"])
def api_download(album_id):
    with queue_lock:
        if (
            album_id not in download_queue
            and download_process.get("album_id") != album_id
        ):
            download_queue.append(album_id)
            return jsonify({"success": True, "queued": True})
        else:
            return jsonify(
                {"success": False, "message": "Already in queue or downloading"}
            )


@app.route("/api/download/stop", methods=["POST"])
def api_download_stop():
    download_process["stop"] = True
    with queue_lock:
        download_queue.clear()
    return jsonify({"success": True})


@app.route("/api/download/status")
def api_download_status():
    return jsonify(download_process)


@app.route("/api/version")
def api_version():
    return jsonify({"version": VERSION})


@app.route("/api/download/queue", methods=["GET"])
def api_get_queue():
    with queue_lock:
        queue_with_details = []
        for album_id in download_queue:
            album = lidarr_request(f"album/{album_id}")
            if "error" not in album:
                queue_with_details.append(
                    {
                        "id": album_id,
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
                    }
                )
        return jsonify(queue_with_details)


@app.route("/api/download/queue", methods=["POST"])
def api_add_to_queue():
    album_id = request.json.get("album_id")
    with queue_lock:
        if (
            album_id not in download_queue
            and download_process.get("album_id") != album_id
        ):
            download_queue.append(album_id)
    return jsonify({"success": True, "queue_length": len(download_queue)})


@app.route("/api/download/queue/<int:album_id>", methods=["DELETE"])
def api_remove_from_queue(album_id):
    with queue_lock:
        if album_id in download_queue:
            download_queue.remove(album_id)
    return jsonify({"success": True})


@app.route("/api/download/queue/clear", methods=["POST"])
def api_clear_queue():
    with queue_lock:
        download_queue.clear()
    return jsonify({"success": True})


@app.route("/api/download/history")
def api_download_history():
    return jsonify(download_history[-20:])


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


@app.route("/api/logs", methods=["GET"])
def api_get_logs():
    with queue_lock:
                                                                   
        return jsonify(
            sorted(download_logs, key=lambda x: x["timestamp"], reverse=True)
        )


@app.route("/api/logs/clear", methods=["POST"])
def api_clear_logs():
    with queue_lock:
        download_logs.clear()
        save_logs()
    return jsonify({"success": True})


@app.route("/api/logs/<log_id>/dismiss", methods=["DELETE"])
def api_dismiss_log(log_id):
    with queue_lock:
        for i, log in enumerate(download_logs):
            if log["id"] == log_id:
                download_logs.pop(i)
                save_logs()
                return jsonify({"success": True})
    return jsonify({"success": False, "error": "Log not found"}), 404


def scheduled_check():
    if download_process["active"]:
        return
    config = load_config()
    albums = get_missing_albums()

    if not albums:
        return

    with queue_lock:
        recent_history_ids = [
            h["album_id"] for h in download_history[-50:] if h.get("success")
        ]
        current_download_id = download_process.get("album_id")

        new_albums = [
            album
            for album in albums
            if album["id"] not in download_queue
            and album["id"] not in recent_history_ids
            and album["id"] != current_download_id
            and album.get("missingTrackCount", 0) > 0
        ]

    if new_albums:
        if config.get("scheduler_auto_download", True):
            logger.info(
                f"🤖 Scheduler: Found {len(new_albums)} new missing albums, adding to queue..."
            )
            send_telegram(
                f"🚀 Scheduler: Adding {len(new_albums)} new missing albums to queue..."
            )
            with queue_lock:
                for album in new_albums:
                    download_queue.append(album["id"])
        else:
            logger.info(
                f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-Download disabled)"
            )
            send_telegram(
                f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-DL Disabled)"
            )


def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(10)


def setup_scheduler():
    config = load_config()
    schedule.clear()
    if config.get("scheduler_enabled"):
        interval = int(config.get("scheduler_interval", 60))
        schedule.every(interval).minutes.do(scheduled_check)


def process_download_queue():
    while True:
        try:
            if not download_process["active"] and download_queue:
                with queue_lock:
                    if download_queue:
                        next_album_id = download_queue.pop(0)
                        threading.Thread(
                            target=process_album_download, args=(next_album_id, False)
                        ).start()
        except:
            pass
        time.sleep(2)


if __name__ == "__main__":
    load_persistent_data()
    logger.info("🚀 Starting Lidarr YouTube Downloader...")
    logger.info(f"📌 Version: {VERSION}")
    logger.info(
        f"📂 Download directory: {DOWNLOAD_DIR if DOWNLOAD_DIR else 'Not set (check DOWNLOAD_PATH env)'}"
    )
    setup_scheduler()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=process_download_queue, daemon=True).start()
    logger.info("✅ Application started successfully on http://0.0.0.0:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)
