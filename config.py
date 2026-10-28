"""Configuration management for Lidarr YouTube Downloader.

Loads defaults from environment variables, overlays with config.json.
"""

import copy
import errno
import json
import logging
import math
import os
import re
import threading
import urllib.parse
import uuid

logger = logging.getLogger(__name__)

CONFIG_FILE = "/config/config.json"

_file_write_lock = threading.Lock()
_config_update_lock = threading.RLock()

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
    "ntfy_enabled", "ntfy_url", "ntfy_topic", "ntfy_token", "ntfy_priority",
    "ntfy_log_types",
    "acoustid_enabled", "acoustid_api_key", "acoustid_accept_score",
    "min_match_score", "audio_format", "audio_quality", "ytdlp_format",
    "lidarr_rename_after_import", "save_cover_art_file",
    "scheduler_retry_after_hours",
    "track_retry_backoff", "max_track_retries",
    "download_client_enabled", "download_client_api_key",
    "download_client_category", "download_client_concurrent_albums",
    "yt_po_token", "audio_normalize", "yt_pot_provider_url",
    "playlist_to_library", "queue_paused",
    "vpn_proxy_url", "gluetun_control_url", "gluetun_api_key",
    "vpn_rotate_every_albums", "vpn_rotate_on_block",
    "search_artist_source",
    "save_lyrics", "apply_replaygain",
    "explore_country", "explore_language",
    "yt_watch_playlists",
    "ui_accent", "ui_aura_mode",
    "lidarr_tag",
    "apprise_enabled", "apprise_urls", "apprise_log_types",
    "spotify_client_id", "spotify_client_secret",
}

AUTH_CONFIG_KEYS = (
    "auth_enabled", "auth_username", "auth_password_hash", "auth_api_key",
)

_LIDARR_TAG_RE = re.compile(r"^[a-z0-9-]{0,50}$")

# Appearance (Settings > Appearance). The accent is written into every
# page's <head>, so only a plain #rrggbb ever gets through.
UI_ACCENT_DEFAULT = "#7c5cff"
UI_AURA_MODES = ("cover", "accent", "off")
_HEX_COLOR_RE = re.compile(r"^#[0-9a-f]{6}$")

EXPLORE_LANGUAGES = frozenset({
    "ar", "cs", "de", "en", "es", "fr", "hi", "it", "ja", "ko", "nl", "pt",
    "ru", "tr", "ur", "zh_CN", "zh_TW",
})
EXPLORE_COUNTRY_DEFAULT = "IT"
EXPLORE_LANGUAGE_DEFAULT = "en"
_COUNTRY_RE = re.compile(r"^[A-Z]{2}$")

# Valid values for search_artist_source: which artist to use when building
# the YouTube search query for a track. "album" (default) is Lidarr's
# album-level artist (cheap, but wrong for compilations tagged "Various
# Artists"); the others resolve a real per-track artist via MusicBrainz
# and/or iTunes before falling back to "album".
SEARCH_ARTIST_SOURCES = {"album", "mb_itunes", "itunes_mb", "mb", "itunes"}

INT_CONFIG_KEYS = frozenset({
    "scheduler_interval", "duration_tolerance", "scheduler_max_albums",
    "concurrent_tracks", "yt_retries", "yt_fragment_retries",
    "yt_sleep_requests", "yt_sleep_interval", "yt_max_sleep_interval",
    "download_client_concurrent_albums", "max_track_retries",
    "vpn_rotate_every_albums",
})

FLOAT_CONFIG_KEYS = frozenset({
    "scheduler_retry_after_hours", "min_match_score", "acoustid_accept_score",
})

BOOL_CONFIG_KEYS = frozenset({
    "scheduler_enabled", "scheduler_auto_download", "track_retry_backoff",
    "telegram_enabled", "xml_metadata_enabled", "yt_force_ipv4",
    "audio_normalize", "save_lyrics", "apply_replaygain", "discord_enabled",
    "ntfy_enabled", "acoustid_enabled", "lidarr_rename_after_import",
    "save_cover_art_file", "download_client_enabled", "playlist_to_library",
    "queue_paused", "apprise_enabled", "vpn_rotate_on_block",
})

LIST_CONFIG_KEYS = frozenset({
    "telegram_log_types", "discord_log_types", "ntfy_log_types",
    "apprise_log_types",
    "forbidden_words", "forbidden_words_custom", "yt_watch_playlists",
})

# A watched playlist must be a YouTube / YT Music URL carrying a list id.
_WATCH_PLAYLIST_RE = re.compile(
    r"^https://(?:www\.|m\.|music\.)?youtube\.com/\S*[?&]list=[\w-]+",
)

# Masked in GET /api/config, dropped from the export. download_client_api_key
# is left out on purpose: /api/download-client/info shows it for Lidarr.
SECRET_CONFIG_KEYS = (
    "telegram_bot_token", "discord_webhook_url", "ntfy_token",
    "acoustid_api_key", "yt_po_token", "apprise_urls",
    "spotify_client_secret", "vpn_proxy_url", "gluetun_api_key",
)
SECRET_MASK = "********"

ENV_PREFERRED_KEYS = frozenset({
    "lidarr_url", "lidarr_api_key", "download_path",
})

_TRUE_STRINGS = frozenset({"true", "1", "yes", "on"})
_FALSE_STRINGS = frozenset({"false", "0", "no", "off"})

MIN_MATCH_SCORE_DEFAULT = 0.8

# Default forbidden words filtered out of YouTube search results. Single
# source of truth shared by the config defaults and the downloader's
# effective-list builder, and mirrored by the Settings UI checkboxes.
DEFAULT_FORBIDDEN_WORDS = [
    "remix", "cover", "mashup", "bootleg", "live", "dj mix",
    "karaoke", "slowed", "reverb", "nightcore", "sped up",
    "instrumental", "acapella", "tribute", "reaction", "8d audio",
]


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


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in _TRUE_STRINGS:
            return True
        if lowered in _FALSE_STRINGS:
            return False
    raise ValueError(f"expected a boolean, got {value!r}")


def parse_http_url(value, allow_userinfo=True):
    """Validate an ``http(s)://host[:port]`` URL; "" is allowed (= unset).

    Raises ValueError otherwise. Only http(s) is accepted because one value
    serves both yt-dlp and ``requests`` (vpn_proxy_url, gluetun_control_url).
    ``allow_userinfo=False`` also refuses ``user:pass@``: a value that is not
    a masked secret must not carry credentials.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected a URL")
    value = value.strip()
    if not value:
        return ""
    if any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise ValueError("a URL cannot contain spaces")
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("expected an http:// or https:// URL")
    try:
        parsed.port
    except ValueError:
        raise ValueError("invalid port") from None
    if parsed.query or parsed.fragment:
        raise ValueError("a URL cannot have a query or fragment")
    if not allow_userinfo and (parsed.username or parsed.password or "@" in parsed.netloc):
        raise ValueError("put credentials in the API key field, not in the URL")
    return value


def _parse_control_url(value):
    return parse_http_url(value, allow_userinfo=False)


def _parse_gluetun_api_key(value):
    """The key travels in an HTTP header: plain printable ASCII, no spaces."""
    if value is None:
        return ""
    if not isinstance(value, str):
        raise ValueError("expected a string")
    value = value.strip()
    if value and (len(value) > 256 or not all(33 <= ord(ch) <= 126 for ch in value)):
        raise ValueError("the API key must be plain ASCII without spaces")
    return value


VPN_ROTATE_EVERY_MAX = 1000
_warned_invalid = set()


def _parse_vpn_rotate_every(value):
    number = _parse_int(value)
    if not 0 <= number <= VPN_ROTATE_EVERY_MAX:
        raise ValueError(f"expected 0 to {VPN_ROTATE_EVERY_MAX}")
    return number


def _parse_int(value):
    if isinstance(value, bool):
        raise ValueError(f"expected an integer, got {value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return int(value.strip())
    raise ValueError(f"expected an integer, got {value!r}")


def _parse_float(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ValueError(f"expected a number, got {value!r}")
    parsed = float(value.strip() if isinstance(value, str) else value)
    if not math.isfinite(parsed):
        raise ValueError(f"expected a finite number, got {value!r}")
    return parsed


def _parse_explore_country(value):
    if not isinstance(value, str):
        raise ValueError("expected a country code")
    code = value.strip().upper()
    if not _COUNTRY_RE.match(code):
        raise ValueError("expected a two-letter country code")
    return code


def _parse_explore_language(value):
    if not isinstance(value, str) or value.strip() not in EXPLORE_LANGUAGES:
        raise ValueError("unsupported language")
    return value.strip()


def _parse_ui_accent(value):
    color = value.strip().lower() if isinstance(value, str) else ""
    if not _HEX_COLOR_RE.match(color):
        raise ValueError("expected a #rrggbb colour")
    return color


def _parse_ui_aura_mode(value):
    if value not in UI_AURA_MODES:
        raise ValueError(f"expected one of {', '.join(UI_AURA_MODES)}")
    return value


def _parse_lidarr_tag(value):
    tag = value.strip().lower() if isinstance(value, str) else None
    if tag is None or not _LIDARR_TAG_RE.match(tag):
        raise ValueError("use lowercase letters, digits and hyphens")
    return tag


def coerce_config_value(key, value):
    try:
        if key == "lidarr_tag":
            return _parse_lidarr_tag(value)
        if key == "vpn_proxy_url":
            return parse_http_url(value)
        if key == "gluetun_control_url":
            return _parse_control_url(value)
        if key == "gluetun_api_key":
            return _parse_gluetun_api_key(value)
        if key == "vpn_rotate_every_albums":
            return _parse_vpn_rotate_every(value)
        if key == "ui_accent":
            return _parse_ui_accent(value)
        if key == "ui_aura_mode":
            return _parse_ui_aura_mode(value)
        if key == "explore_country":
            return _parse_explore_country(value)
        if key == "explore_language":
            return _parse_explore_language(value)
        if key in INT_CONFIG_KEYS:
            return _parse_int(value)
        if key in FLOAT_CONFIG_KEYS:
            return _parse_float(value)
        if key in BOOL_CONFIG_KEYS:
            return _parse_bool(value)
        if key in LIST_CONFIG_KEYS:
            if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value
            ):
                raise ValueError("expected a list of strings")
            if key == "yt_watch_playlists":
                value = [url.strip() for url in value if url.strip()]
                if not all(_WATCH_PLAYLIST_RE.match(url) for url in value):
                    raise ValueError("expected YouTube playlist URLs")
            return value
        if isinstance(value, str):
            return value
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return str(value)
        raise ValueError("expected a string")
    except ValueError:
        raise ValueError(f"Invalid value for {key}: {value!r}") from None


def _env_value(name, default, parser):
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return parser(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r in environment; using default %r",
            name, raw, default,
        )
        return default


def _env_int(name, default):
    return _env_value(name, default, _parse_int)


def _env_float(name, default):
    return _env_value(name, default, _parse_float)


def _env_bool(name, default):
    return _env_value(name, default, _parse_bool)


def retry_cooldown_seconds(cfg=None):
    """Seconds before a tried album may be retried (0 = disabled).

    Single source of the ``scheduler_retry_after_hours`` window, shared by
    the scheduler and the download-client bridge (indexer feed exclusion,
    grab refusal and release-guid bucketing), so the layers can't drift.
    """
    if cfg is None:
        cfg = load_config()
    try:
        hours = float(cfg.get("scheduler_retry_after_hours", 24))
    except (TypeError, ValueError):
        hours = 24.0
    return hours * 3600 if hours > 0 else 0


def _env_config():
    return {
        "lidarr_url": os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": os.getenv("LIDARR_API_KEY", ""),
        "lidarr_path": os.getenv("LIDARR_PATH", ""),
        "download_path": os.getenv("DOWNLOAD_PATH", ""),
        "scheduler_enabled": _env_bool("SCHEDULER_ENABLED", False),
        "scheduler_auto_download": _env_bool("SCHEDULER_AUTO_DOWNLOAD", True),
        "scheduler_interval": _env_int("SCHEDULER_INTERVAL", 60),
        "scheduler_max_albums": _env_int("SCHEDULER_MAX_ALBUMS", 0),
        "scheduler_retry_after_hours": _env_float(
            "SCHEDULER_RETRY_AFTER_HOURS", 24.0
        ),
        "track_retry_backoff": _env_bool("TRACK_RETRY_BACKOFF", True),
        "max_track_retries": _env_int("MAX_TRACK_RETRIES", 0),
        "telegram_enabled": _env_bool("TELEGRAM_ENABLED", False),
        "telegram_bot_token": os.getenv("TELEGRAM_BOT_TOKEN", ""),
        "telegram_chat_id": os.getenv("TELEGRAM_CHAT_ID", ""),
        "telegram_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
            "manual_download",
        ],
        "xml_metadata_enabled": _env_bool("XML_METADATA_ENABLED", True),
        "forbidden_words": list(DEFAULT_FORBIDDEN_WORDS),
        "forbidden_words_custom": [],
        "duration_tolerance": _env_int("DURATION_TOLERANCE", 10),
        "concurrent_tracks": _env_int("CONCURRENT_TRACKS", 2),
        "yt_cookies_file": os.getenv("YT_COOKIES_FILE", ""),
        "yt_force_ipv4": _env_bool("YT_FORCE_IPV4", True),
        "yt_player_client": os.getenv("YT_PLAYER_CLIENT", "android"),
        "yt_po_token": os.getenv("YT_PO_TOKEN", ""),
        "yt_pot_provider_url": os.getenv("YT_POT_PROVIDER_URL", ""),
        "vpn_proxy_url": os.getenv("VPN_PROXY_URL", ""),
        "gluetun_control_url": os.getenv("GLUETUN_CONTROL_URL", ""),
        "gluetun_api_key": os.getenv("GLUETUN_API_KEY", ""),
        "vpn_rotate_every_albums": 0,
        "vpn_rotate_on_block": True,
        "audio_normalize": _env_bool("AUDIO_NORMALIZE", False),
        # Write a synced .lrc lyrics sidecar (fetched from LRCLIB) next to
        # each downloaded track.
        "save_lyrics": _env_bool("SAVE_LYRICS", False),
        "apply_replaygain": _env_bool("APPLY_REPLAYGAIN", False),
        "yt_retries": _env_int("YT_RETRIES", 10),
        "yt_fragment_retries": _env_int("YT_FRAGMENT_RETRIES", 10),
        "yt_sleep_requests": _env_int("YT_SLEEP_REQUESTS", 1),
        "yt_sleep_interval": _env_int("YT_SLEEP_INTERVAL", 1),
        "yt_max_sleep_interval": _env_int("YT_MAX_SLEEP_INTERVAL", 5),
        "discord_enabled": _env_bool("DISCORD_ENABLED", False),
        "discord_webhook_url": os.getenv("DISCORD_WEBHOOK_URL", ""),
        "discord_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
            "manual_download",
        ],
        "ntfy_enabled": _env_bool("NTFY_ENABLED", False),
        "ntfy_url": os.getenv("NTFY_URL", "https://ntfy.sh").rstrip("/"),
        "ntfy_topic": os.getenv("NTFY_TOPIC", ""),
        "ntfy_token": os.getenv("NTFY_TOKEN", ""),
        "ntfy_priority": os.getenv("NTFY_PRIORITY", "default"),
        "ntfy_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
            "manual_download",
        ],
        "acoustid_enabled": _env_bool("ACOUSTID_ENABLED", True),
        "acoustid_api_key": os.getenv("ACOUSTID_API_KEY", ""),
        "acoustid_accept_score": _parse_unit_float(
            os.getenv("ACOUSTID_ACCEPT_SCORE", "0.98"),
            "acoustid_accept_score", 0.98,
        ),
        "min_match_score": _parse_min_match_score(
            os.getenv("MIN_MATCH_SCORE", "0.8"),
        ),
        "search_artist_source": os.getenv("SEARCH_ARTIST_SOURCE", "album"),
        "ui_accent": UI_ACCENT_DEFAULT,
        "ui_aura_mode": "cover",
        "explore_country": os.getenv("EXPLORE_COUNTRY", EXPLORE_COUNTRY_DEFAULT),
        "explore_language": os.getenv(
            "EXPLORE_LANGUAGE", EXPLORE_LANGUAGE_DEFAULT,
        ),
        "audio_format": os.getenv("AUDIO_FORMAT", "mp3"),
        "audio_quality": os.getenv("AUDIO_QUALITY", "320"),
        # Optional yt-dlp format selector override (e.g. "141" for 256 kbps
        # AAC on Premium accounts). Empty = use the built-in smart selectors.
        "ytdlp_format": os.getenv("YTDLP_FORMAT", ""),
        "lidarr_rename_after_import": _env_bool(
            "LIDARR_RENAME_AFTER_IMPORT", False
        ),
        "save_cover_art_file": _env_bool("SAVE_COVER_ART_FILE", True),
        "download_client_enabled": _env_bool(
            "DOWNLOAD_CLIENT_ENABLED", False
        ),
        "download_client_api_key": os.getenv("DOWNLOAD_CLIENT_API_KEY", ""),
        "download_client_category": os.getenv(
            "DOWNLOAD_CLIENT_CATEGORY", "music"
        ),
        "download_client_concurrent_albums": _env_int(
            "DOWNLOAD_CLIENT_CONCURRENT_ALBUMS", 1
        ),
        # When true, a YouTube playlist import is written into the Lidarr
        # music library (LIDARR_PATH) and a library scan is requested, so the
        # files land where Jellyfin/Lidarr look instead of only the download
        # folder. Default false keeps the legacy download-folder-only flow.
        "playlist_to_library": _env_bool("PLAYLIST_TO_LIBRARY", False),
        "queue_paused": False,
        # YouTube playlists checked periodically: every video not yet
        # downloaded is imported as one album (issue #96).
        "yt_watch_playlists": [],
        "lidarr_tag": os.getenv("LIDARR_TAG", ""),
        "apprise_enabled": _env_bool("APPRISE_ENABLED", False),
        "apprise_urls": os.getenv("APPRISE_URLS", ""),
        "apprise_log_types": [
            "partial_success",
            "import_partial",
            "album_error",
            "manual_download",
        ],
        "spotify_client_id": os.getenv("SPOTIFY_CLIENT_ID", ""),
        "spotify_client_secret": os.getenv("SPOTIFY_CLIENT_SECRET", ""),
        "auth_enabled": False,
        "auth_username": "",
        "auth_password_hash": "",
        "auth_api_key": "",
        "path_conflict": False,
    }


def load_config():
    """Load config with env var defaults, overlaid by config.json."""
    global _config_cache, _config_cache_key
    cache_key = _config_file_key()
    if cache_key is not None and _config_cache is not None and (
        cache_key == _config_cache_key
    ):
        # Deep copy so callers mutating the result can't corrupt the cache.
        return copy.deepcopy(_config_cache)
    config = _env_config()

    if os.path.exists(CONFIG_FILE):
        # Keep the env-derived defaults so a malformed value in config.json
        # falls back instead of propagating a string into code that does
        # int(...) on it (e.g. the scheduler / yt-dlp options).
        env_defaults = dict(config)
        try:
            with open(CONFIG_FILE, "r") as f:
                file_config = json.load(f)
            if not isinstance(file_config, dict):
                raise ValueError("top-level value is not an object")
            for key in config.keys():
                if key in ENV_PREFERRED_KEYS and env_defaults[key]:
                    continue
                if key in file_config:
                    config[key] = file_config[key]
        except (ValueError, OSError) as e:
            logger.warning("Failed to load config file %s: %s", CONFIG_FILE, e)

        for _k in INT_CONFIG_KEYS:
            if _k in config:
                try:
                    config[_k] = int(config[_k])
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid %s=%r in config.json; using %r",
                        _k, config[_k], env_defaults.get(_k),
                    )
                    config[_k] = env_defaults.get(_k)
        for _k in BOOL_CONFIG_KEYS:
            try:
                config[_k] = _parse_bool(config[_k])
            except ValueError:
                logger.warning(
                    "Invalid %s=%r in config.json; using %r",
                    _k, config[_k], env_defaults[_k],
                )
                config[_k] = env_defaults[_k]
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

    # Clamp to the 1-5 range the UI offers and the engine enforces, so an
    # out-of-range env/file value can't render the Settings select blank
    # (which would then silently save 1 over the configured value).
    try:
        _cca = int(config.get("download_client_concurrent_albums", 1))
    except (TypeError, ValueError):
        _cca = 1
    config["download_client_concurrent_albums"] = max(1, min(5, _cca))

    for vpn_key, parser in (
        ("vpn_proxy_url", parse_http_url),
        ("gluetun_control_url", _parse_control_url),
        ("gluetun_api_key", _parse_gluetun_api_key),
    ):
        try:
            config[vpn_key] = parser(config.get(vpn_key))
        except ValueError:
            if vpn_key not in _warned_invalid:  # without a config file this runs every call
                _warned_invalid.add(vpn_key)
                logger.warning("Ignoring invalid %s in the configuration", vpn_key)
            config[vpn_key] = ""
    try:
        config["vpn_rotate_every_albums"] = _parse_vpn_rotate_every(
            config.get("vpn_rotate_every_albums", 0),
        )
    except (TypeError, ValueError):
        config["vpn_rotate_every_albums"] = 0

    if config.get("search_artist_source") not in SEARCH_ARTIST_SOURCES:
        config["search_artist_source"] = "album"

    try:
        config["explore_country"] = _parse_explore_country(
            config.get("explore_country"),
        )
    except ValueError:
        config["explore_country"] = EXPLORE_COUNTRY_DEFAULT
    if config.get("explore_language") not in EXPLORE_LANGUAGES:
        config["explore_language"] = EXPLORE_LANGUAGE_DEFAULT
    try:
        config["ui_accent"] = _parse_ui_accent(config.get("ui_accent"))
    except ValueError:
        config["ui_accent"] = UI_ACCENT_DEFAULT
    if config.get("ui_aura_mode") not in UI_AURA_MODES:
        config["ui_aura_mode"] = "cover"
    try:
        config["lidarr_tag"] = _parse_lidarr_tag(config.get("lidarr_tag") or "")
    except ValueError:
        config["lidarr_tag"] = ""

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


def _persistable(config):
    env_config = _env_config()
    data = {}
    for key, value in config.items():
        if key in ALLOWED_CONFIG_KEYS:
            if key in INT_CONFIG_KEYS | FLOAT_CONFIG_KEYS | BOOL_CONFIG_KEYS:
                value = coerce_config_value(key, value)
            data[key] = value
        elif (
            key in env_config
            and key != "path_conflict"
            and value != env_config[key]
        ):
            data[key] = value
    return data


def _write_atomic(data):
    tmp_path = f"{CONFIG_FILE}.{uuid.uuid4().hex}.tmp"
    try:
        mode = os.stat(CONFIG_FILE).st_mode & 0o777
    except OSError:
        mode = None
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        if mode is not None:
            os.chmod(tmp_path, mode)
        try:
            os.replace(tmp_path, CONFIG_FILE)
        except OSError as e:
            if e.errno not in (errno.EBUSY, errno.EXDEV):
                raise
            with open(CONFIG_FILE, "w") as f:
                json.dump(data, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
    finally:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)


def save_config(config):
    """Write config dict to CONFIG_FILE as JSON."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    data = _persistable(config)
    try:
        with _file_write_lock:
            _write_atomic(data)
    except OSError as e:
        logger.error("Failed to save config to %s: %s", CONFIG_FILE, e)
        raise
    invalidate_config_cache()


def update_config(mutator):
    with _config_update_lock:
        cfg = load_config()
        mutator(cfg)
        save_config(cfg)
    return cfg
