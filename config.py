"""Configuration management for Lidarr YouTube Downloader.

Loads defaults from environment variables, overlays with config.json.
"""

import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

CONFIG_FILE = "/config/config.json"

_file_write_lock = threading.Lock()

ALLOWED_CONFIG_KEYS = {
    "scheduler_interval", "telegram_bot_token", "telegram_chat_id",
    "telegram_enabled", "telegram_log_types", "download_path",
    "lidarr_path", "forbidden_words", "duration_tolerance",
    "scheduler_enabled", "scheduler_auto_download",
    "xml_metadata_enabled", "concurrent_tracks", "yt_cookies_file", "yt_force_ipv4",
    "yt_player_client", "yt_retries", "yt_fragment_retries",
    "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
    "discord_enabled", "discord_webhook_url", "discord_log_types",
    "acoustid_enabled", "acoustid_api_key",
    "min_match_score",
}

MIN_MATCH_SCORE_DEFAULT = 0.8


def _parse_min_match_score(value):
    """Parse min_match_score from any input, falling back to default with a warning.

    Accepts strings, numbers, or anything float-coercible. Logs a warning if
    the value is invalid or out of the [0.0, 1.0] range.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid min_match_score=%r; using default %.2f",
            value, MIN_MATCH_SCORE_DEFAULT,
        )
        return MIN_MATCH_SCORE_DEFAULT
    if not 0.0 <= parsed <= 1.0:
        logger.warning(
            "min_match_score=%.2f out of range [0.0, 1.0];"
            " using default %.2f",
            parsed, MIN_MATCH_SCORE_DEFAULT,
        )
        return MIN_MATCH_SCORE_DEFAULT
    return parsed


def load_config():
    """Load config with env var defaults, overlaid by config.json."""
    config = {
        "lidarr_url": os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": os.getenv("LIDARR_API_KEY", ""),
        "lidarr_path": os.getenv("LIDARR_PATH", ""),
        "download_path": os.getenv("DOWNLOAD_PATH", ""),
        "scheduler_enabled": (
            os.getenv("SCHEDULER_ENABLED", "false").lower() == "true"
        ),
        "scheduler_auto_download": (
            os.getenv("SCHEDULER_AUTO_DOWNLOAD", "true").lower() == "true"
        ),
        "scheduler_interval": int(os.getenv("SCHEDULER_INTERVAL", "60")),
        "telegram_enabled": (
            os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
        ),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
        ],
        "xml_metadata_enabled": (
            os.getenv("XML_METADATA_ENABLED", "true").lower() == "true"
        ),
        "forbidden_words": [
            "remix", "cover", "mashup", "bootleg", "live", "dj mix",
            "karaoke", "slowed", "reverb", "nightcore", "sped up",
            "instrumental", "acapella", "tribute",
        ],
        "duration_tolerance": int(os.getenv("DURATION_TOLERANCE", "10")),
        "concurrent_tracks": int(os.getenv("CONCURRENT_TRACKS", "2")),
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": (
            os.getenv("YT_FORCE_IPV4", "true").lower() == "true"
        ),
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_retries": int(os.getenv("YT_RETRIES", "10")),
        "yt_fragment_retries": int(os.getenv("YT_FRAGMENT_RETRIES", "10")),
        "yt_sleep_requests": int(os.getenv("YT_SLEEP_REQUESTS", "1")),
        "yt_sleep_interval": int(os.getenv("YT_SLEEP_INTERVAL", "1")),
        "yt_max_sleep_interval": int(
            os.getenv("YT_MAX_SLEEP_INTERVAL", "5")
        ),
        "discord_enabled": (
            os.getenv("DISCORD_ENABLED", "false").lower() == "true"
        ),
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "discord_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
        ],
        "acoustid_enabled": (
            os.getenv("ACOUSTID_ENABLED", "true").lower() == "true"
        ),
        "acoustid_api_key": os.getenv("ACOUSTID_API_KEY", ""),
        "min_match_score": _parse_min_match_score(
            os.getenv("MIN_MATCH_SCORE", "0.8"),
        ),
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
                config["scheduler_interval"] = int(
                    config["scheduler_interval"]
                )
            if "duration_tolerance" in config:
                config["duration_tolerance"] = int(
                    config["duration_tolerance"]
                )
            if "min_match_score" in config:
                config["min_match_score"] = _parse_min_match_score(
                    config["min_match_score"]
                )
        except (json.JSONDecodeError, OSError, ValueError) as e:
            logger.warning("Failed to load config file %s: %s", CONFIG_FILE, e)

    def norm(p):
        return (
            os.path.normcase(os.path.abspath(str(p))).rstrip("\\/")
            if p
            else ""
        )

    l_path = norm(config.get("lidarr_path"))
    d_path = norm(config.get("download_path"))

    config["path_conflict"] = bool(l_path and l_path == d_path)

    if config["path_conflict"]:
        logger.warning(f"Path Conflict Detected: {l_path}")

    return config


def save_config(config):
    """Write config dict to CONFIG_FILE as JSON."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if "scheduler_interval" in config:
        config["scheduler_interval"] = int(config["scheduler_interval"])
    if "duration_tolerance" in config:
        config["duration_tolerance"] = int(config["duration_tolerance"])
    try:
        with _file_write_lock:
            with open(CONFIG_FILE, "w") as f:
                json.dump(config, f, indent=2)
    except OSError as e:
        logger.error("Failed to save config to %s: %s", CONFIG_FILE, e)
        raise
