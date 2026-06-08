"""Configuration management for Lidarr YouTube Downloader.

Loads defaults from environment variables, overlays with config.json.
"""

import copy
import json
import logging
import os
import threading

logger = logging.getLogger(__name__)

CONFIG_FILE = "/config/config.json"

_file_write_lock = threading.Lock()

# Cache the parsed config so the download-client polling path doesn't
# re-read config.json on every call; rebuilt when the file changes.
_config_cache = None
_config_cache_key = None


def _config_file_key():
    # Return None when there is no file to cache against: in env-only mode
    # (no config.json) we rebuild from os.environ every call so runtime env
    # changes are picked up, and there's no disk read to amortise anyway.
    try:
        st = os.stat(CONFIG_FILE)
    except OSError:
        return None
    return (CONFIG_FILE, st.st_mtime_ns, st.st_size)


def invalidate_config_cache():
    """Drop the cached config (call after writing config.json)."""
    global _config_cache, _config_cache_key
    _config_cache = None
    _config_cache_key = None


ALLOWED_CONFIG_KEYS = {
    "scheduler_interval", "telegram_bot_token", "telegram_chat_id",
    "telegram_enabled", "telegram_log_types", "download_path",
    "lidarr_path", "forbidden_words", "forbidden_words_custom",
    "duration_tolerance",
    "scheduler_enabled", "scheduler_auto_download", "scheduler_max_albums",
    "xml_metadata_enabled", "concurrent_tracks", "yt_cookies_file", "yt_force_ipv4",
    "yt_player_client", "yt_retries", "yt_fragment_retries",
    "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
    "discord_enabled", "discord_webhook_url", "discord_log_types",
    "acoustid_enabled", "acoustid_api_key", "acoustid_accept_score",
    "min_match_score", "audio_format", "audio_quality",
    "lidarr_rename_after_import", "save_cover_art_file",
    "scheduler_retry_after_hours",
    "download_client_enabled", "download_client_api_key",
    "download_client_category",
    "yt_po_token", "audio_normalize", "yt_pot_provider_url",
}

MIN_MATCH_SCORE_DEFAULT = 0.8


def _parse_unit_float(value, name, default):
    """Coerce a value to a float in [0.0, 1.0], falling back with a warning.

    Accepts strings, numbers, or anything float-coercible; out-of-range or
    invalid input logs a warning and returns ``default``.
    """
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        logger.warning("Invalid %s=%r; using default %.2f", name, value, default)
        return default
    if not 0.0 <= parsed <= 1.0:
        logger.warning(
            "%s=%.2f out of range [0.0, 1.0]; using default %.2f",
            name, parsed, default,
        )
        return default
    return parsed


def _parse_min_match_score(value):
    """Parse min_match_score to a float in [0.0, 1.0] (default 0.8)."""
    return _parse_unit_float(value, "min_match_score", MIN_MATCH_SCORE_DEFAULT)


def load_config():
    """Load config with env var defaults, overlaid by config.json."""
    global _config_cache, _config_cache_key
    cache_key = _config_file_key()
    if cache_key is not None and _config_cache is not None and (
        cache_key == _config_cache_key
    ):
        # Deep copy so callers mutating the result can't corrupt the cache.
        return copy.deepcopy(_config_cache)
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
        "scheduler_max_albums": int(os.getenv("SCHEDULER_MAX_ALBUMS", "0")),
        "scheduler_retry_after_hours": float(
            os.getenv("SCHEDULER_RETRY_AFTER_HOURS", "24")
        ),
        "telegram_enabled": (
            os.getenv("TELEGRAM_ENABLED", "false").lower() == "true"
        ),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
            "manual_download",
        ],
        "xml_metadata_enabled": (
            os.getenv("XML_METADATA_ENABLED", "true").lower() == "true"
        ),
        "forbidden_words": [
            "remix", "cover", "mashup", "bootleg", "live", "dj mix",
            "karaoke", "slowed", "reverb", "nightcore", "sped up",
            "instrumental", "acapella", "tribute", "8d audio",
        ],
        "forbidden_words_custom": [],
        "duration_tolerance": int(os.getenv("DURATION_TOLERANCE", "10")),
        "concurrent_tracks": int(os.getenv("CONCURRENT_TRACKS", "2")),
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": (
            os.getenv("YT_FORCE_IPV4", "true").lower() == "true"
        ),
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_po_token": os.getenv("YT_PO_TOKEN", ""),
        "yt_pot_provider_url": os.getenv("YT_POT_PROVIDER_URL", ""),
        "audio_normalize": (
            os.getenv("AUDIO_NORMALIZE", "false").lower() == "true"
        ),
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
            "manual_download",
        ],
        "acoustid_enabled": (
            os.getenv("ACOUSTID_ENABLED", "true").lower() == "true"
        ),
        "acoustid_api_key": os.getenv("ACOUSTID_API_KEY", ""),
        "acoustid_accept_score": _parse_unit_float(
            os.getenv("ACOUSTID_ACCEPT_SCORE", "0.98"),
            "acoustid_accept_score", 0.98,
        ),
        "min_match_score": _parse_min_match_score(
            os.getenv("MIN_MATCH_SCORE", "0.8"),
        ),
        "audio_format": os.getenv("AUDIO_FORMAT", "mp3"),
        "audio_quality": os.getenv("AUDIO_QUALITY", "320"),
        "lidarr_rename_after_import": (
            os.getenv("LIDARR_RENAME_AFTER_IMPORT", "false").lower() == "true"
        ),
        "save_cover_art_file": (
            os.getenv("SAVE_COVER_ART_FILE", "true").lower() == "true"
        ),
        "download_client_enabled": (
            os.getenv("DOWNLOAD_CLIENT_ENABLED", "false").lower() == "true"
        ),
        "download_client_api_key": os.getenv("DOWNLOAD_CLIENT_API_KEY", ""),
        "download_client_category": os.getenv(
            "DOWNLOAD_CLIENT_CATEGORY", "music"
        ),
        "path_conflict": False,
    }

    if os.path.exists(CONFIG_FILE):
        # Keep the env-derived defaults so a malformed value in config.json
        # falls back instead of propagating a string into code that does
        # int(...) on it (e.g. the scheduler / yt-dlp options).
        env_defaults = dict(config)
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
            for key in config.keys():
                if key in file_config:
                    config[key] = file_config[key]
        except (json.JSONDecodeError, OSError) as e:
            logger.warning("Failed to load config file %s: %s", CONFIG_FILE, e)

        _int_keys = (
            "scheduler_interval", "duration_tolerance", "scheduler_max_albums",
            "concurrent_tracks", "yt_retries", "yt_fragment_retries",
            "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
        )
        for _k in _int_keys:
            if _k in config:
                try:
                    config[_k] = int(config[_k])
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid %s=%r in config.json; using %r",
                        _k, config[_k], env_defaults.get(_k),
                    )
                    config[_k] = env_defaults.get(_k)
        if "scheduler_retry_after_hours" in config:
            try:
                config["scheduler_retry_after_hours"] = float(
                    config["scheduler_retry_after_hours"]
                )
            except (TypeError, ValueError):
                config["scheduler_retry_after_hours"] = env_defaults.get(
                    "scheduler_retry_after_hours", 24.0
                )
        if "min_match_score" in config:
            config["min_match_score"] = _parse_min_match_score(
                config["min_match_score"]
            )
        if "acoustid_accept_score" in config:
            config["acoustid_accept_score"] = _parse_unit_float(
                config["acoustid_accept_score"], "acoustid_accept_score",
                env_defaults.get("acoustid_accept_score", 0.98),
            )

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

    if cache_key is not None:
        _config_cache = copy.deepcopy(config)
        _config_cache_key = cache_key
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
    invalidate_config_cache()
