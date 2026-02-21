import os
import json
import time
import threading
import shutil
import re
import logging
import uuid
import math
import urllib.parse
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from difflib import SequenceMatcher
from flask import Flask, render_template, request, jsonify, send_from_directory, Response
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

VERSION = "1.5.0"


@app.context_processor
def inject_version():
    return {"APP_VERSION": VERSION}


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
    "cover_url": "",
}

download_queue = []
download_history = []
download_logs = []
queue_lock = threading.Lock()

last_failed_result = {
    "failed_tracks": [],
    "album_id": None,
    "album_title": "",
    "artist_name": "",
    "cover_url": "",
    "album_path": "",
    "album_data": None,
    "cover_data": None,
    "lidarr_album_path": "",
}

rate_limit_store = {}
RATE_LIMIT_WINDOW = 2
RATE_LIMIT_MAX = 5

ALLOWED_CONFIG_KEYS = {
    "scheduler_interval", "telegram_bot_token", "telegram_chat_id",
    "telegram_enabled", "telegram_log_types", "download_path",
    "lidarr_path", "forbidden_words", "duration_tolerance",
    "scheduler_enabled", "scheduler_auto_download",
    "xml_metadata_enabled", "yt_cookies_file", "yt_force_ipv4",
    "yt_player_client", "yt_retries", "yt_fragment_retries",
    "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
    "discord_enabled", "discord_webhook_url", "discord_log_types",
}


def check_rate_limit(key, window=RATE_LIMIT_WINDOW, max_requests=RATE_LIMIT_MAX):
    now = time.time()
    if key not in rate_limit_store:
        rate_limit_store[key] = []
    rate_limit_store[key] = [t for t in rate_limit_store[key] if now - t < window]
    if len(rate_limit_store[key]) >= max_requests:
        return False
    rate_limit_store[key].append(now)
    return True

HISTORY_FILE = "/config/download_history.json"
LOGS_FILE = "/config/download_logs.json"
album_cache = {}
ALBUM_CACHE_TTL = 300


def load_persistent_data():
    global download_history, download_logs
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r") as f:
                download_history = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Failed to load history file: {e}")
            download_history = []
    if os.path.exists(LOGS_FILE):
        try:
            with open(LOGS_FILE, "r") as f:
                download_logs = json.load(f)
        except Exception as e:
            logger.warning(f"⚠️ Failed to load logs file: {e}")
            download_logs = []


_file_write_lock = threading.Lock()


def save_history():
    try:
        os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
        with _file_write_lock:
            with open(HISTORY_FILE, "w") as f:
                json.dump(download_history[-25:], f)
    except Exception as e:
        logger.warning(f"⚠️ Failed to save history: {e}")


def save_logs():
    try:
        os.makedirs(os.path.dirname(LOGS_FILE), exist_ok=True)
        with _file_write_lock:
            with open(LOGS_FILE, "w") as f:
                json.dump(download_logs[-100:], f)
    except Exception as e:
        logger.warning(f"⚠️ Failed to save logs: {e}")


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
            "album_error",
        ],                        
        "xml_metadata_enabled": os.getenv("XML_METADATA_ENABLED", "true").lower()
        == "true",
        "forbidden_words": ["remix", "cover", "mashup", "bootleg", "live", "dj mix", "karaoke", "slowed", "reverb", "nightcore", "sped up", "instrumental", "acapella", "tribute"],
        "duration_tolerance": int(os.getenv("DURATION_TOLERANCE", "10")),
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": os.getenv("YT_FORCE_IPV4", "true").lower() == "true",
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_retries": int(os.getenv("YT_RETRIES", "10")),
        "yt_fragment_retries": int(os.getenv("YT_FRAGMENT_RETRIES", "10")),
        "yt_sleep_requests": int(os.getenv("YT_SLEEP_REQUESTS", "1")),
        "yt_sleep_interval": int(os.getenv("YT_SLEEP_INTERVAL", "1")),
        "yt_max_sleep_interval": int(os.getenv("YT_MAX_SLEEP_INTERVAL", "5")),
        "discord_enabled": os.getenv("DISCORD_ENABLED", "false").lower() == "true",
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "discord_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
        ],
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
            if "duration_tolerance" in config:
                config["duration_tolerance"] = int(config["duration_tolerance"])
        except Exception as e:
            logger.warning(f"⚠️ Failed to load config file: {e}")

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
    if "duration_tolerance" in config:
        config["duration_tolerance"] = int(config["duration_tolerance"])
    with _file_write_lock:
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
        except Exception as e:
            logger.warning(f"⚠️ Telegram notification failed: {e}")


def send_discord(message, log_type=None, embed_data=None):
    config = load_config()
    if not config.get("discord_enabled"):
        return
    webhook_url = config.get("discord_webhook_url", "")
    if not webhook_url:
        return
    if log_type is not None:
        allowed_types = config.get("discord_log_types", [])
        if log_type not in allowed_types:
            return
    try:
        payload = {}
        if embed_data:
            embed = {
                "title": embed_data.get("title", ""),
                "description": embed_data.get("description", ""),
                "color": embed_data.get("color", 0x10b981),
            }
            if embed_data.get("thumbnail"):
                embed["thumbnail"] = {"url": embed_data["thumbnail"]}
            if embed_data.get("fields"):
                embed["fields"] = embed_data["fields"]
            payload["embeds"] = [embed]
        else:
            payload["content"] = message
        requests.post(webhook_url, json=payload, timeout=10)
    except Exception as e:
        logger.warning(f"⚠️ Discord notification failed: {e}")


def send_notifications(message, log_type=None, embed_data=None):
    send_telegram(message, log_type=log_type)
    send_discord(message, log_type=log_type, embed_data=embed_data)


def get_ytdlp_version():
    try:
        import importlib.metadata
        return importlib.metadata.version("yt-dlp")
    except Exception:
        try:
            return yt_dlp.version.__version__
        except Exception:
            return "unknown"


def format_bytes(size_bytes):
    if size_bytes <= 0:
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def add_download_log(
    log_type, album_id, album_title, artist_name, details=None, failed_tracks=None,
    total_file_size=0,
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
            "total_file_size": total_file_size,
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
    except Exception as e:
        logger.warning(f"⚠️ Failed to get missing albums: {e}")
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
    except Exception as e:
        logger.debug(f"iTunes tracks lookup failed: {e}")
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
    except Exception as e:
        logger.debug(f"iTunes artwork lookup failed: {e}")
    return None


def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.replace("..", "").replace("~", "")
    name = name.strip(". ")
    if not name:
        name = "untitled"
    return name


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
    forbidden_words = config.get("forbidden_words", ["remix", "cover", "mashup", "bootleg", "live", "dj mix", "karaoke", "slowed", "reverb", "nightcore", "sped up", "instrumental", "acapella", "tribute"])
    duration_tolerance = config.get("duration_tolerance", 10)

    expected_duration_sec = None
    if expected_duration_ms:
        expected_duration_sec = expected_duration_ms / 1000.0
        logger.info(f"📏 Expected track duration: {int(expected_duration_sec // 60)}:{int(expected_duration_sec % 60):02d} ({int(expected_duration_sec)}s)")

    def _title_similarity(yt_title, track_title, artist_name):
        yt_lower = yt_title.lower()
        expected_lower = f"{artist_name} {track_title}".lower()
        score = SequenceMatcher(None, yt_lower, expected_lower).ratio()
        track_lower = track_title.lower()
        if track_lower in yt_lower:
            score += 0.3
        if artist_name.lower() in yt_lower:
            score += 0.2
        return min(score, 1.0)

    def _is_official_channel(channel_name, artist_name):
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
        for word in forbidden_list:
            if " " in word:
                if word in yt_title_lower and word not in track_title_lower:
                    return word
            else:
                pattern = r'\b' + re.escape(word) + r'\b'
                if re.search(pattern, yt_title_lower) and not re.search(pattern, track_title_lower):
                    return word
        return None

    artist_part = query.split(" ")[0] if " " in query else query
    search_queries = [query]
    base_track = track_title_original
    base_artist = query.replace(f" {track_title_original} official audio", "").replace(f" {track_title_original}", "").strip()
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
        if candidates:
            break
        if qi > 0:
            logger.info(f"   🔄 Fallback search ({qi+1}/{len(search_queries)}): \"{sq}\"")
        try:
            with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
                search_results = ydl.extract_info(f"ytsearch15:{sq}", download=False)

                for entry in search_results.get("entries", []):
                    title = entry.get("title", "").lower()
                    url = entry.get("url")
                    duration = entry.get("duration", 0)
                    channel = entry.get("channel", "") or entry.get("uploader", "") or ""
                    view_count = entry.get("view_count", 0) or 0

                    blocked_word = _check_forbidden(title, track_title_original.lower(), forbidden_words)
                    if blocked_word:
                        logger.debug(f"   ⊗ Rejected '{entry.get('title', '')}' - forbidden word '{blocked_word}'")
                        continue

                    if expected_duration_sec:
                        min_duration = max(15, expected_duration_sec - duration_tolerance)
                        max_duration = expected_duration_sec + duration_tolerance

                        if duration < min_duration or duration > max_duration:
                            logger.debug(f"   ⊗ Rejected '{entry.get('title', '')}' - duration {int(duration)}s outside [{int(min_duration)}s - {int(max_duration)}s]")
                            continue

                        duration_diff = abs(duration - expected_duration_sec)
                        duration_score = max(0, 1.0 - (duration_diff / max(duration_tolerance, 1)))
                    else:
                        if duration < 15 or duration > 7200:
                            continue
                        duration_score = 0.5

                    title_score = _title_similarity(entry.get("title", ""), track_title_original, base_artist)

                    official_bonus = 0.15 if _is_official_channel(channel, base_artist) else 0.0

                    view_score = 0.0
                    if view_count > 0:
                        view_score = min(0.1, math.log10(max(view_count, 1)) / 100)

                    total_score = (duration_score * 0.35) + (title_score * 0.40) + official_bonus + view_score

                    if url:
                        candidates.append({
                            "url": url,
                            "title": entry.get("title", ""),
                            "duration": duration,
                            "channel": channel,
                            "score": total_score
                        })
                        logger.debug(f"   ✓ Candidate '{entry.get('title', '')}' — score={total_score:.2f} (dur={duration_score:.2f} title={title_score:.2f} official={official_bonus:.2f} views={view_score:.3f})")
        except Exception as e:
            logger.error(f"   ❌ Search failed for \"{sq}\": {str(e)}")
            if qi == len(search_queries) - 1 and not candidates:
                return f"Search failed: {str(e)[:120]}"

    if not candidates:
        logger.warning(f"   ⚠️  No suitable candidates found after all search attempts")
        return "No suitable YouTube match found (filtered by duration/forbidden words)"

    candidates.sort(key=lambda x: x["score"], reverse=True)

    best = candidates[0]
    logger.info(f"   🎯 Best match: '{best['title']}' (score={best['score']:.2f}, duration={int(best['duration'])}s, channel='{best.get('channel', '')}')")

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

    last_error_msg = str(last_err)[:120] if last_err else "Unknown error"
    if last_err and "403" in str(last_err):
        return f"HTTP 403 Forbidden - try providing/refreshing YouTube cookies"
    return f"Download failed after all attempts: {last_error_msg}"


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
    except Exception as e:
        logger.debug(f"Failed to set permissions on {path}: {e}")


def tag_mp3(file_path, track_info, album_info, cover_data):
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except Exception:
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
        except (ValueError, KeyError):
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
    except Exception as e:
        logger.warning(f"⚠️ Failed to tag MP3 {file_path}: {e}")
        return False


def create_xml_metadata(
    output_dir, artist, album, track_num, title, album_id=None, artist_id=None
):
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        safe_title = xml_escape(title)
        safe_artist = xml_escape(artist)
        safe_album = xml_escape(album)
        mb_album = (
            f"  <musicbrainzalbumid>{xml_escape(str(album_id))}</musicbrainzalbumid>\n"
            if album_id
            else ""
        )
        mb_artist = (
            f"  <musicbrainzartistid>{xml_escape(str(artist_id))}</musicbrainzartistid>\n"
            if artist_id
            else ""
        )
        content = f"""<song>
  <title>{safe_title}</title>
  <artist>{safe_artist}</artist>
  <performingartist>{safe_artist}</performingartist>
  <albumartist>{safe_artist}</albumartist>
  <album>{safe_album}</album>
{mb_album}{mb_artist}</song>"""
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"⚠️ Failed to create XML metadata: {e}")
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
            except Exception as e:
                logger.debug(f"Failed to fetch tracks from Lidarr: {e}")

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
        download_process["cover_url"] = next(
            (img["remoteUrl"] for img in album.get("images", []) if img.get("coverType") == "cover"),
            ""
        )

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
        send_notifications(
            f"🎵 Download Started\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📦 Tracks: {len(tracks)}",
            log_type="download_started",
            embed_data={"title": "Download Started", "description": f"{artist_name} — {album_title}", "color": 0x3498db, "fields": [{"name": "Tracks", "value": str(len(tracks)), "inline": True}]},
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

        if len(tracks_to_download) == 0:
            lidarr_request(
                "command",
                method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )
            return {"success": True, "message": "Skipped"}

        logger.info(f"📦 Total tracks to download: {len(tracks_to_download)}")

        for idx, track in enumerate(tracks_to_download, 1):
            if download_process["stop"]:
                logger.warning(f"⏹️  Download stopped by user")
                return {"stopped": True}

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
                f"⬇️  Downloading track {idx}/{len(tracks_to_download)}: {track_title}"
            )

            sanitized_track = sanitize_filename(track_title)

            temp_file = os.path.join(album_path, f"temp_{track_num:02d}_{uuid.uuid4().hex[:8]}")
            final_file = os.path.join(
                album_path, f"{track_num:02d} - {sanitized_track}.mp3"
            )

            track_duration_ms = track.get("duration")

            download_result = download_track_youtube(
                f"{artist_name} {track_title} official audio",
                temp_file,
                track_title,
                track_duration_ms,
            )
            actual_file = temp_file + ".mp3"

            if download_result is True and os.path.exists(actual_file):
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
                try:
                    total_downloaded_size += os.path.getsize(actual_file)
                except OSError:
                    pass
                shutil.move(actual_file, final_file)
            else:
                fail_reason = download_result if isinstance(download_result, str) else "Download failed or file not found"
                logger.warning(f"⚠️  Failed to download track: {track_title} — {fail_reason}")
                # Clean up temp files from failed download
                for ext in [".mp3", ".webm", ".m4a", ".part", ""]:
                    tmp = temp_file + ext
                    if os.path.exists(tmp):
                        try:
                            os.remove(tmp)
                        except Exception:
                            pass
                failed_tracks.append({"title": track_title, "reason": fail_reason, "track_num": track_num})

                                                    
            download_process["progress"]["current"] = idx
            download_process["progress"]["total"] = len(tracks_to_download)
            download_process["progress"]["overall_percent"] = int(
                (idx / len(tracks_to_download)) * 100
            )

        set_permissions(artist_path)

        if failed_tracks:
            failed_list = "\n".join([f"• {t['title']}" for t in failed_tracks])

            if len(failed_tracks) == len(tracks_to_download):
                send_notifications(
                    f"❌ Download Failed (All Tracks)\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="album_error",
                    embed_data={"title": "Download Failed", "description": f"{artist_name} — {album_title}", "color": 0xe74c3c, "fields": [{"name": "Failed Tracks", "value": failed_list[:1024], "inline": False}]},
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
                download_process["result_partial"] = True
                send_notifications(
                    f"⚠️ Partial Download Completed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n\nFailed tracks:\n{failed_list}",
                    log_type="partial_success",
                    embed_data={"title": "Partial Download", "description": f"{artist_name} — {album_title}", "color": 0xe67e22, "fields": [{"name": "Failed Tracks", "value": failed_list[:1024], "inline": False}]},
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
                    total_file_size=total_downloaded_size,
                )
        else:
            add_download_log(
                log_type="download_success",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details=f"Successfully downloaded {len(tracks_to_download)} track(s)",
                failed_tracks=[],
                total_file_size=total_downloaded_size,
            )
            send_notifications(
                f"✅ Download successful\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📦 Tracks: {len(tracks_to_download)}/{len(tracks_to_download)}",
                log_type="download_success",
                embed_data={"title": "Download Successful", "description": f"{artist_name} — {album_title}", "color": 0x2ecc71, "fields": [{"name": "Tracks", "value": f"{len(tracks_to_download)}/{len(tracks_to_download)}", "inline": True}]},
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
                total_file_size=total_downloaded_size,
            )

            send_notifications(
                f"⚠️ Import Partial\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📚 Refreshing in Lidarr (Missing {len(failed_tracks)} tracks)",
                log_type="import_partial",
                embed_data={"title": "Import Partial", "description": f"{artist_name} — {album_title}", "color": 0xe67e22, "fields": [{"name": "Missing Tracks", "value": str(len(failed_tracks)), "inline": True}]},
            )
        else:
            add_download_log(
                log_type="import_success",
                album_id=album_id,
                album_title=album_title,
                artist_name=artist_name,
                details="Album downloaded and refreshing in Lidarr",
                failed_tracks=[],
                total_file_size=total_downloaded_size,
            )

            send_notifications(
                f"✅ Import Success\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📚 Refreshing in Lidarr",
                log_type="import_success",
                embed_data={"title": "Import Successful", "description": f"{artist_name} — {album_title}", "color": 0x2ecc71},
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
        send_notifications(
            f"❌ Download failed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}",
            log_type="album_error",
            embed_data={"title": "Download Failed", "description": f"{artist_name} — {album_title}", "color": 0xe74c3c},
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
        _cover_url = download_process.get("cover_url", "")
        if failed_tracks:
            last_failed_result.update({
                "failed_tracks": [
                    {"title": t["title"], "reason": t["reason"], "track_num": t.get("track_num", 0)}
                    for t in failed_tracks
                ],
                "album_id": album_id,
                "album_title": download_process.get("album_title", "") or album_title,
                "artist_name": download_process.get("artist_name", "") or artist_name,
                "cover_url": _cover_url,
                "album_path": album_path,
                "album_data": album if album else None,
                "cover_data": cover_data,
                "lidarr_album_path": lidarr_album_path,
            })
        else:
            last_failed_result.update({
                "failed_tracks": [], "album_id": None, "album_title": "",
                "artist_name": "", "cover_url": "", "album_path": "",
                "album_data": None, "cover_data": None, "lidarr_album_path": "",
            })

        with queue_lock:
            download_history.append(
                {
                    "album_id": download_process.get("album_id"),
                    "album_title": download_process.get("album_title", ""),
                    "artist_name": download_process.get("artist_name", ""),
                    "success": download_process.get("result_success", True),
                    "partial": download_process.get("result_partial", False),
                    "timestamp": time.time(),
                }
            )
            download_history[:] = download_history[-25:]
            save_history()
        download_process["active"] = False
        download_process["progress"] = {}
        download_process["album_id"] = None
        download_process["album_title"] = ""
        download_process["artist_name"] = ""
        download_process["current_track_title"] = ""
        download_process["cover_url"] = ""


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
    except Exception:
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
        client_ip = request.remote_addr or "unknown"
        if not check_rate_limit(f"config:{client_ip}", window=5, max_requests=3):
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
    if not check_rate_limit(f"config_import:{client_ip}", window=10, max_requests=2):
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
        "message": f"Imported {len(applied_keys)} settings. {len(skipped_keys)} keys skipped.",
    })


@app.route("/api/ytdlp/version")
def api_ytdlp_version():
    return jsonify({"version": get_ytdlp_version()})


@app.route("/api/ytdlp/update", methods=["POST"])
def api_ytdlp_update():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"ytdlp_update:{client_ip}", window=60, max_requests=1):
        return jsonify({"success": False, "message": "Update already in progress or rate limited"}), 429
    old_version = get_ytdlp_version()
    try:
        import subprocess
        result = subprocess.run(
            ["pip", "install", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=120
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
        else:
            return jsonify({
                "success": False,
                "message": result.stderr[-500:] if result.stderr else "Update failed",
            })
    except subprocess.TimeoutExpired:
        return jsonify({"success": False, "message": "Update timed out (120s)"})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)})


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
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"download:{client_ip}"):
        return jsonify({"success": False, "message": "Too many requests, please slow down"}), 429
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
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"stop:{client_ip}", window=5, max_requests=3):
        return jsonify({"success": False, "message": "Too many requests"}), 429
    with queue_lock:
        download_process["stop"] = True
        download_queue.clear()
    return jsonify({"success": True})


@app.route("/api/download/status")
def api_download_status():
    with queue_lock:
        return jsonify(dict(download_process))


@app.route("/api/download/stream")
def api_download_stream():
    SSE_TIMEOUT = 3600

    def generate():
        start_time = time.time()
        try:
            while True:
                if time.time() - start_time > SSE_TIMEOUT:
                    break
                with queue_lock:
                    queue_data = []
                    for album_id in download_queue:
                        album = get_album_cached(album_id)
                        if "error" not in album:
                            cover_url = ""
                            for img in album.get("images", []):
                                if img.get("coverType") == "cover":
                                    cover_url = img.get("remoteUrl", "")
                                    break
                            queue_data.append({
                                "id": album_id,
                                "title": album.get("title", ""),
                                "artist": album.get("artist", {}).get("artistName", ""),
                                "cover_url": cover_url,
                            })
                data = {
                    "status": dict(download_process),
                    "queue": queue_data
                }
                yield f"data: {json.dumps(data)}\n\n"
                time.sleep(1)
        except GeneratorExit:
            return

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/api/stats")
def api_stats():
    today_start = time.time() - (time.time() % 86400)
    with queue_lock:
        downloaded_today = sum(
            1 for h in download_history
            if h.get("success") and h.get("timestamp", 0) >= today_start
        )
        in_queue = len(download_queue) + (1 if download_process["active"] else 0)
    return jsonify({
        "in_queue": in_queue,
        "downloaded_today": downloaded_today
    })




def get_album_cached(album_id):
    now = time.time()
    if album_id in album_cache:
        cached, ts = album_cache[album_id]
        if now - ts < ALBUM_CACHE_TTL:
            return cached
    album = lidarr_request(f"album/{album_id}")
    if "error" not in album:
        album_cache[album_id] = (album, now)
    return album


@app.route("/api/download/queue", methods=["GET"])
def api_get_queue():
    with queue_lock:
        queue_with_details = []
        for album_id in download_queue:
            album = get_album_cached(album_id)
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
    return jsonify(download_history)


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


@app.route("/api/download/history/clear", methods=["POST"])
def api_clear_history():
    with queue_lock:
        download_history.clear()
        save_history()
    return jsonify({"success": True})


@app.route("/api/logs/size", methods=["GET"])
def api_logs_size():
    try:
        size = os.path.getsize(LOGS_FILE)
    except OSError:
        size = 0
    return jsonify({"size": size, "formatted": format_bytes(size)})


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


@app.route("/api/download/failed")
def api_download_failed():
    return jsonify({
        "failed_tracks": last_failed_result.get("failed_tracks", []),
        "album_id": last_failed_result.get("album_id"),
        "album_title": last_failed_result.get("album_title", ""),
        "artist_name": last_failed_result.get("artist_name", ""),
        "cover_url": last_failed_result.get("cover_url", ""),
    })


@app.route("/api/youtube/search", methods=["POST"])
def api_youtube_search():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"yt_search:{client_ip}", window=3, max_requests=5):
        return jsonify({"results": [], "error": "Too many requests"}), 429
    query = (request.json or {}).get("query", "").strip()
    if not query:
        return jsonify({"results": []})
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
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            results = ydl.extract_info(f"ytsearch10:{query}", download=False)
            items = []
            for entry in results.get("entries", []):
                items.append({
                    "title": entry.get("title", ""),
                    "url": entry.get("url", ""),
                    "duration": entry.get("duration", 0),
                    "channel": entry.get("channel", "") or entry.get("uploader", "") or "",
                    "thumbnail": entry.get("thumbnail", ""),
                })
            return jsonify({"results": items})
    except Exception as e:
        return jsonify({"results": [], "error": str(e)[:200]}), 500


@app.route("/api/download/manual", methods=["POST"])
def api_download_manual():
    client_ip = request.remote_addr or "unknown"
    if not check_rate_limit(f"manual_dl:{client_ip}", window=5, max_requests=3):
        return jsonify({"success": False, "message": "Too many requests"}), 429

    data = request.json or {}
    youtube_url = data.get("youtube_url", "").strip()
    track_title = data.get("track_title", "").strip()
    track_num = data.get("track_num", 0)

    if not youtube_url or not track_title:
        return jsonify({"success": False, "message": "Missing required fields"}), 400

    if not youtube_url.startswith("http"):
        if not re.match(r'^[a-zA-Z0-9_-]{11}$', youtube_url):
            return jsonify({"success": False, "message": "Invalid YouTube video ID"}), 400
        youtube_url = f"https://www.youtube.com/watch?v={youtube_url}"
    else:
        try:
            parsed = urllib.parse.urlparse(youtube_url)
            allowed_hosts = {
                "youtube.com", "www.youtube.com", "m.youtube.com",
                "youtu.be", "www.youtu.be",
                "music.youtube.com",
            }
            if parsed.hostname not in allowed_hosts:
                return jsonify({"success": False, "message": "Only YouTube URLs are allowed"}), 400
        except Exception:
            return jsonify({"success": False, "message": "Invalid URL"}), 400

    album_data = last_failed_result.get("album_data")
    if not album_data:
        return jsonify({"success": False, "message": "No album context available. Please re-download the album first."}), 400

    dl_album_path = last_failed_result.get("album_path", "")
    lidarr_album_path_val = last_failed_result.get("lidarr_album_path", "")
    target_path = lidarr_album_path_val if lidarr_album_path_val and os.path.isdir(lidarr_album_path_val) else dl_album_path

    if not target_path:
        return jsonify({"success": False, "message": "No album path available"}), 400

    # Path traversal protection: ensure target_path is within DOWNLOAD_DIR or lidarr_path
    config = load_config()
    lidarr_path = config.get("lidarr_path", "")
    allowed_bases = [os.path.realpath(DOWNLOAD_DIR)] if DOWNLOAD_DIR else []
    if lidarr_path:
        allowed_bases.append(os.path.realpath(lidarr_path))
    real_target = os.path.realpath(target_path)
    if not any(real_target.startswith(base + os.sep) or real_target == base for base in allowed_bases):
        return jsonify({"success": False, "message": "Invalid target path"}), 400

    os.makedirs(target_path, exist_ok=True)

    cover_data_stored = last_failed_result.get("cover_data")
    sanitized_track = sanitize_filename(track_title)
    temp_file = os.path.join(target_path, f"temp_manual_{uuid.uuid4().hex[:8]}")
    final_file = os.path.join(target_path, f"{int(track_num):02d} - {sanitized_track}.mp3")

    config = load_config()
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
            return jsonify({"success": False, "message": "Download failed — file not created"}), 500

        track_info = {"title": track_title, "trackNumber": track_num}
        for t in album_data.get("tracks", []):
            if t.get("title", "").lower() == track_title.lower():
                track_info = t
                break

        tag_mp3(actual_file, track_info, album_data, cover_data_stored)

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

        last_failed_result["failed_tracks"] = [
            t for t in last_failed_result.get("failed_tracks", [])
            if t.get("title", "").lower() != track_title.lower()
        ]

        artist_id = album_data.get("artist", {}).get("id")
        if artist_id:
            lidarr_request(
                "command", method="POST",
                data={"name": "RefreshArtist", "artistId": artist_id},
            )

        logger.info(f"✅ Manual download successful: {track_title}")

        album_title = last_failed_result.get("album_title", "")
        artist_name = last_failed_result.get("artist_name", "")
        album_id = last_failed_result.get("album_id")

        add_download_log(
            log_type="manual_download",
            album_id=album_id or 0,
            album_title=album_title or "Unknown Album",
            artist_name=artist_name or "Unknown Artist",
            details=f"Manually downloaded track: {track_title} (from YouTube)",
            failed_tracks=[],
            total_file_size=manual_file_size,
        )

        with queue_lock:
            download_history.append(
                {
                    "album_id": album_id,
                    "album_title": album_title or "Unknown Album",
                    "artist_name": artist_name or "Unknown Artist",
                    "success": True,
                    "partial": False,
                    "manual": True,
                    "track_title": track_title,
                    "timestamp": time.time(),
                }
            )
            download_history[:] = download_history[-25:]
            save_history()

        return jsonify({"success": True, "message": f"Track '{track_title}' downloaded successfully"})

    except Exception as e:
        logger.warning(f"⚠️ Manual download failed for '{track_title}': {e}")
        for ext in [".mp3", ".webm", ".m4a", ".part"]:
            tmp = temp_file + ext
            if os.path.exists(tmp):
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return jsonify({"success": False, "message": str(e)[:200]}), 500


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
            send_notifications(
                f"🚀 Scheduler: Adding {len(new_albums)} new missing albums to queue...",
                log_type="download_started",
                embed_data={"title": "Scheduler", "description": f"Adding {len(new_albums)} new missing albums to queue", "color": 0x3498db},
            )
            with queue_lock:
                for album in new_albums:
                    download_queue.append(album["id"])
        else:
            logger.info(
                f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-Download disabled)"
            )
            send_notifications(
                f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-DL Disabled)",
                log_type="download_started",
                embed_data={"title": "Scheduler", "description": f"Found {len(new_albums)} missing albums (Auto-DL Disabled)", "color": 0xe67e22},
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
                            target=process_album_download, args=(next_album_id, False), daemon=True
                        ).start()
        except Exception as e:
            logger.warning(f"⚠️ Queue processor error: {e}")
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
