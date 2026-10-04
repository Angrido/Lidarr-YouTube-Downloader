import json
import os

import pytest

import config


@pytest.fixture(autouse=True)
def temp_config(tmp_path, monkeypatch):
    """Redirect CONFIG_FILE to a temp path and clear env vars."""
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", config_file)
    env_vars = [
        "LIDARR_URL", "LIDARR_API_KEY", "LIDARR_PATH", "DOWNLOAD_PATH",
        "SCHEDULER_ENABLED", "SCHEDULER_AUTO_DOWNLOAD", "SCHEDULER_INTERVAL",
        "TELEGRAM_ENABLED", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
        "XML_METADATA_ENABLED", "DURATION_TOLERANCE",
        "YT_COOKIES_FILE", "YT_FORCE_IPV4", "YT_PLAYER_CLIENT",
        "YT_RETRIES", "YT_FRAGMENT_RETRIES", "YT_SLEEP_REQUESTS",
        "YT_SLEEP_INTERVAL", "YT_MAX_SLEEP_INTERVAL",
        "DISCORD_ENABLED", "DISCORD_WEBHOOK_URL",
    ]
    for var in env_vars:
        monkeypatch.delenv(var, raising=False)
    yield config_file


def test_load_config_defaults(temp_config):
    """Default config when no file exists and no env vars set."""
    cfg = config.load_config()
    assert cfg["lidarr_url"] == ""
    assert cfg["lidarr_api_key"] == ""
    assert cfg["lidarr_path"] == ""
    assert cfg["download_path"] == ""
    assert cfg["scheduler_enabled"] is False
    assert cfg["scheduler_auto_download"] is True
    assert cfg["scheduler_interval"] == 60
    assert cfg["telegram_enabled"] is False
    assert cfg["telegram_bot_token"] == ""
    assert cfg["telegram_chat_id"] == ""
    assert isinstance(cfg["telegram_log_types"], list)
    assert cfg["xml_metadata_enabled"] is True
    assert isinstance(cfg["forbidden_words"], list)
    assert "remix" in cfg["forbidden_words"]
    assert cfg["duration_tolerance"] == 10
    assert cfg["yt_force_ipv4"] is True
    assert cfg["yt_player_client"] == "android"
    assert cfg["yt_retries"] == 10
    assert cfg["discord_enabled"] is False
    assert cfg["discord_webhook_url"] == ""
    assert isinstance(cfg["discord_log_types"], list)
    assert cfg["path_conflict"] is False


def test_load_config_from_env(temp_config, monkeypatch):
    """Env vars provide default values before file overlay."""
    monkeypatch.setenv("LIDARR_URL", "http://env:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "env_key")
    monkeypatch.setenv("SCHEDULER_INTERVAL", "120")
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://env:8686"
    assert cfg["lidarr_api_key"] == "env_key"
    assert cfg["scheduler_interval"] == 120


def test_load_config_from_file(temp_config):
    """File config overlays env var defaults."""
    with open(temp_config, "w") as f:
        json.dump({"lidarr_url": "http://test:8686"}, f)
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://test:8686"


def test_file_overrides_env(temp_config, monkeypatch):
    """File values take precedence over env vars."""
    monkeypatch.setenv("SCHEDULER_INTERVAL", "120")
    with open(temp_config, "w") as f:
        json.dump({"scheduler_interval": 15}, f)
    cfg = config.load_config()
    assert cfg["scheduler_interval"] == 15


def test_save_config(temp_config):
    """Save and reload round-trips correctly."""
    cfg = config.load_config()
    cfg["lidarr_path"] = "/saved/music"
    config.save_config(cfg)
    reloaded = config.load_config()
    assert reloaded["lidarr_path"] == "/saved/music"


def test_save_config_coerces_ints(temp_config):
    """save_config coerces scheduler_interval and duration_tolerance to int."""
    cfg = config.load_config()
    cfg["scheduler_interval"] = "30"
    cfg["duration_tolerance"] = "5"
    config.save_config(cfg)
    with open(temp_config) as f:
        raw = json.load(f)
    assert raw["scheduler_interval"] == 30
    assert raw["duration_tolerance"] == 5


def test_load_config_coerces_ints_from_file(temp_config):
    """load_config coerces scheduler_interval and duration_tolerance from file."""
    with open(temp_config, "w") as f:
        json.dump({"scheduler_interval": "45", "duration_tolerance": "7"}, f)
    cfg = config.load_config()
    assert cfg["scheduler_interval"] == 45
    assert cfg["duration_tolerance"] == 7


def test_load_config_ignores_unknown_file_keys(temp_config):
    """Unknown keys in file are not loaded into config."""
    with open(temp_config, "w") as f:
        json.dump({"unknown_key": "value", "lidarr_url": "http://x"}, f)
    cfg = config.load_config()
    assert "unknown_key" not in cfg
    assert cfg["lidarr_url"] == "http://x"


def test_load_config_corrupt_file(temp_config):
    """Corrupt config file is handled gracefully, defaults returned."""
    with open(temp_config, "w") as f:
        f.write("not valid json{{{")
    cfg = config.load_config()
    assert cfg["lidarr_url"] == ""
    assert cfg["scheduler_interval"] == 60


def test_path_conflict_detection(temp_config):
    """path_conflict is True when lidarr_path and download_path match."""
    with open(temp_config, "w") as f:
        json.dump({
            "lidarr_path": "/data/downloads",
            "download_path": "/data/downloads",
        }, f)
    cfg = config.load_config()
    assert cfg["path_conflict"] is True


def test_no_path_conflict(temp_config):
    """path_conflict is False when paths differ."""
    with open(temp_config, "w") as f:
        json.dump({
            "lidarr_path": "/data/lidarr",
            "download_path": "/data/downloads",
        }, f)
    cfg = config.load_config()
    assert cfg["path_conflict"] is False


def test_allowed_config_keys():
    """ALLOWED_CONFIG_KEYS contains expected keys and excludes credentials."""
    assert "scheduler_interval" in config.ALLOWED_CONFIG_KEYS
    assert "telegram_bot_token" in config.ALLOWED_CONFIG_KEYS
    assert "discord_enabled" in config.ALLOWED_CONFIG_KEYS
    assert "forbidden_words" in config.ALLOWED_CONFIG_KEYS
    # Sensitive keys should not be in ALLOWED_CONFIG_KEYS
    assert "lidarr_url" not in config.ALLOWED_CONFIG_KEYS
    assert "lidarr_api_key" not in config.ALLOWED_CONFIG_KEYS


def test_min_match_score_default(temp_config):
    """min_match_score defaults to 0.8."""
    cfg = config.load_config()
    assert cfg["min_match_score"] == 0.8


def test_min_match_score_from_env(temp_config, monkeypatch):
    """MIN_MATCH_SCORE env var overrides default."""
    monkeypatch.setenv("MIN_MATCH_SCORE", "0.65")
    cfg = config.load_config()
    assert cfg["min_match_score"] == 0.65


def test_min_match_score_invalid_env_falls_back(
    temp_config, monkeypatch, caplog,
):
    """Malformed MIN_MATCH_SCORE env var falls back to default with warning."""
    monkeypatch.setenv("MIN_MATCH_SCORE", "not-a-number")
    with caplog.at_level("WARNING"):
        cfg = config.load_config()
    assert cfg["min_match_score"] == 0.8
    assert any("min_match_score" in r.message for r in caplog.records)


def test_min_match_score_out_of_range_falls_back(temp_config, monkeypatch):
    """Out-of-range MIN_MATCH_SCORE clamps to default."""
    monkeypatch.setenv("MIN_MATCH_SCORE", "1.5")
    cfg = config.load_config()
    assert cfg["min_match_score"] == 0.8


def test_min_match_score_invalid_in_file_falls_back(temp_config):
    """Malformed min_match_score in config.json falls back to default."""
    with open(temp_config, "w") as f:
        json.dump({"min_match_score": "garbage"}, f)
    cfg = config.load_config()
    assert cfg["min_match_score"] == 0.8


def test_save_config_creates_directory(tmp_path, monkeypatch):
    """save_config creates parent directories if they don't exist."""
    nested = str(tmp_path / "nested" / "dir" / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", nested)
    cfg = config.load_config()
    config.save_config(cfg)
    assert os.path.exists(nested)


def test_load_config_cache_invalidated_on_save(temp_config):
    """save_config must invalidate the cache so the next load sees changes."""
    cfg = config.load_config()
    assert cfg["lidarr_path"] == ""
    cfg["lidarr_path"] = "/changed"
    config.save_config(cfg)
    assert config.load_config()["lidarr_path"] == "/changed"


def test_load_config_returns_independent_copies(temp_config):
    """Mutating a returned config must not corrupt the cached value."""
    first = config.load_config()
    first["forbidden_words"].append("__mutation__")
    second = config.load_config()
    assert "__mutation__" not in second["forbidden_words"]


def test_env_only_mode_not_cached(temp_config, monkeypatch):
    """With no config.json, env changes are reflected on the next load."""
    monkeypatch.setenv("LIDARR_URL", "http://first:8686")
    assert config.load_config()["lidarr_url"] == "http://first:8686"
    monkeypatch.setenv("LIDARR_URL", "http://second:8686")
    assert config.load_config()["lidarr_url"] == "http://second:8686"


def test_acoustid_accept_score_default(temp_config):
    assert config.load_config()["acoustid_accept_score"] == 0.98


def test_acoustid_accept_score_from_file_clamped(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"acoustid_accept_score": "1.5"}, f)
    # Out-of-range value falls back to the default.
    assert config.load_config()["acoustid_accept_score"] == 0.98


def test_acoustid_accept_score_from_file_valid(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"acoustid_accept_score": 0.95}, f)
    assert config.load_config()["acoustid_accept_score"] == 0.95


def test_concurrent_albums_clamped_to_range(temp_config):
    # Out-of-range / invalid values are clamped to 1-5 so the Settings
    # select always matches an option (and can't silently save back 1).
    for raw, expected in ((10, 5), (0, 1), (-3, 1), ("bad", 1), (3, 3)):
        with open(temp_config, "w") as f:
            json.dump({"download_client_concurrent_albums": raw}, f)
        config.invalidate_config_cache()
        assert (
            config.load_config()["download_client_concurrent_albums"]
            == expected
        )


def test_playlist_to_library_default_false(temp_config):
    assert config.load_config()["playlist_to_library"] is False


def test_playlist_to_library_from_file(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"playlist_to_library": True}, f)
    assert config.load_config()["playlist_to_library"] is True


def test_playlist_to_library_in_allowed_keys():
    assert "playlist_to_library" in config.ALLOWED_CONFIG_KEYS


def test_save_lyrics_default_false(temp_config):
    assert config.load_config()["save_lyrics"] is False


def test_apply_replaygain_default_false(temp_config):
    assert config.load_config()["apply_replaygain"] is False


def test_lyrics_and_replaygain_from_file(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"save_lyrics": True, "apply_replaygain": True}, f)
    cfg = config.load_config()
    assert cfg["save_lyrics"] is True
    assert cfg["apply_replaygain"] is True


def test_lyrics_replaygain_in_allowed_keys():
    assert "save_lyrics" in config.ALLOWED_CONFIG_KEYS
    assert "apply_replaygain" in config.ALLOWED_CONFIG_KEYS


def test_save_config_crash_mid_write_keeps_old_file(temp_config, monkeypatch):
    cfg = config.load_config()
    cfg["scheduler_interval"] = 77
    config.save_config(cfg)

    def broken_dump(obj, fp, **kwargs):
        fp.write('{"scheduler_interval": ')
        raise OSError("disk full")

    monkeypatch.setattr(config.json, "dump", broken_dump)
    cfg["scheduler_interval"] = 88
    with pytest.raises(OSError):
        config.save_config(cfg)
    monkeypatch.undo()
    with open(temp_config) as f:
        assert json.load(f)["scheduler_interval"] == 77
    leftovers = [
        n for n in os.listdir(os.path.dirname(temp_config))
        if n != os.path.basename(temp_config)
    ]
    assert leftovers == []


def test_save_config_never_exposes_partial_file(temp_config, monkeypatch):
    cfg = config.load_config()
    config.save_config(cfg)
    seen = []
    real_dump = json.dump

    def spying_dump(obj, fp, **kwargs):
        config.invalidate_config_cache()
        with open(temp_config) as f:
            seen.append(f.read())
        real_dump(obj, fp, **kwargs)

    monkeypatch.setattr(config.json, "dump", spying_dump)
    config.save_config(cfg)
    assert seen and json.loads(seen[0])


def test_save_config_preserves_file_mode(temp_config):
    config.save_config(config.load_config())
    os.chmod(temp_config, 0o640)
    config.save_config(config.load_config())
    assert os.stat(temp_config).st_mode & 0o777 == 0o640


def test_save_config_does_not_freeze_env_values(temp_config, monkeypatch):
    monkeypatch.setenv("LIDARR_URL", "http://old:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "old-key")
    monkeypatch.setenv("SCHEDULER_INTERVAL", "30")
    config.save_config(config.load_config())
    with open(temp_config) as f:
        raw = json.load(f)
    assert "lidarr_url" not in raw
    assert "lidarr_api_key" not in raw
    assert "path_conflict" not in raw
    monkeypatch.setenv("LIDARR_URL", "http://new:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "new-key")
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://new:8686"
    assert cfg["lidarr_api_key"] == "new-key"


def test_save_config_keeps_explicit_non_env_value(temp_config):
    cfg = config.load_config()
    cfg["lidarr_url"] = "http://explicit:8686"
    config.save_config(cfg)
    with open(temp_config) as f:
        assert json.load(f)["lidarr_url"] == "http://explicit:8686"


def test_env_wins_over_frozen_file_values(temp_config, monkeypatch):
    with open(temp_config, "w") as f:
        json.dump({
            "lidarr_url": "http://frozen:8686",
            "lidarr_api_key": "frozen-key",
            "download_path": "/frozen",
        }, f)
    monkeypatch.setenv("LIDARR_URL", "http://env:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "env-key")
    monkeypatch.setenv("DOWNLOAD_PATH", "/env")
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://env:8686"
    assert cfg["lidarr_api_key"] == "env-key"
    assert cfg["download_path"] == "/env"


def test_file_used_when_env_empty_for_env_preferred_keys(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"lidarr_api_key": "file-key", "download_path": "/f"}, f)
    cfg = config.load_config()
    assert cfg["lidarr_api_key"] == "file-key"
    assert cfg["download_path"] == "/f"


def test_update_config_serializes_concurrent_updates(temp_config, monkeypatch):
    import threading
    import time
    real_load = config.load_config

    def slow_load():
        cfg = real_load()
        time.sleep(0.05)
        return cfg

    monkeypatch.setattr(config, "load_config", slow_load)
    keys = ["save_lyrics", "apply_replaygain", "audio_normalize",
            "playlist_to_library"]

    def set_key(k):
        config.update_config(lambda c: c.__setitem__(k, True))

    threads = [threading.Thread(target=set_key, args=(k,)) for k in keys]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    final = real_load()
    assert all(final[k] is True for k in keys)


def test_update_config_returns_saved_config(temp_config):
    result = config.update_config(
        lambda c: c.__setitem__("scheduler_interval", 15),
    )
    assert result["scheduler_interval"] == 15
    assert config.load_config()["scheduler_interval"] == 15


def test_update_config_mutator_error_saves_nothing(temp_config):
    def bad(c):
        c["scheduler_interval"] = 99
        raise RuntimeError("nope")

    with pytest.raises(RuntimeError):
        config.update_config(bad)
    assert not os.path.exists(temp_config)


@pytest.mark.parametrize("var,key,default", [
    ("SCHEDULER_INTERVAL", "scheduler_interval", 60),
    ("DURATION_TOLERANCE", "duration_tolerance", 10),
    ("YT_RETRIES", "yt_retries", 10),
    ("SCHEDULER_RETRY_AFTER_HOURS", "scheduler_retry_after_hours", 24.0),
    ("DOWNLOAD_CLIENT_CONCURRENT_ALBUMS",
     "download_client_concurrent_albums", 1),
])
def test_bad_env_number_falls_back(temp_config, monkeypatch, caplog,
                                   var, key, default):
    monkeypatch.setenv(var, "1h")
    with caplog.at_level("WARNING"):
        cfg = config.load_config()
    assert cfg[key] == default
    assert any(var in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("raw,expected", [
    ("1", True), ("yes", True), ("on", True), ("TRUE", True),
    ("0", False), ("no", False), ("off", False), ("false", False),
])
def test_env_bool_variants(temp_config, monkeypatch, raw, expected):
    monkeypatch.setenv("SCHEDULER_ENABLED", raw)
    monkeypatch.setenv("SCHEDULER_AUTO_DOWNLOAD", raw)
    cfg = config.load_config()
    assert cfg["scheduler_enabled"] is expected
    assert cfg["scheduler_auto_download"] is expected


def test_env_bool_garbage_uses_default(temp_config, monkeypatch):
    monkeypatch.setenv("SCHEDULER_AUTO_DOWNLOAD", "maybe")
    assert config.load_config()["scheduler_auto_download"] is True


def test_file_bool_strings_coerced(temp_config):
    with open(temp_config, "w") as f:
        json.dump({"scheduler_auto_download": "false",
                   "save_lyrics": "true", "audio_normalize": "junk"}, f)
    cfg = config.load_config()
    assert cfg["scheduler_auto_download"] is False
    assert cfg["save_lyrics"] is True
    assert cfg["audio_normalize"] is False


@pytest.mark.parametrize("key,raw,expected", [
    ("scheduler_interval", "45", 45),
    ("scheduler_interval", 45.0, 45),
    ("scheduler_retry_after_hours", "1.5", 1.5),
    ("scheduler_retry_after_hours", 2, 2.0),
    ("telegram_enabled", "off", False),
    ("telegram_enabled", 1, True),
    ("telegram_chat_id", 12345, "12345"),
    ("forbidden_words", ["a"], ["a"]),
])
def test_coerce_config_value_valid(key, raw, expected):
    assert config.coerce_config_value(key, raw) == expected


@pytest.mark.parametrize("key,raw", [
    ("scheduler_interval", "abc"),
    ("scheduler_interval", None),
    ("scheduler_interval", True),
    ("scheduler_interval", 1.5),
    ("scheduler_retry_after_hours", "nan"),
    ("scheduler_retry_after_hours", None),
    ("telegram_enabled", "maybe"),
    ("telegram_enabled", None),
    ("telegram_enabled", 2),
    ("telegram_chat_id", None),
    ("telegram_chat_id", {"a": 1}),
    ("forbidden_words", "remix"),
    ("forbidden_words", [1]),
])
def test_coerce_config_value_invalid(key, raw):
    with pytest.raises(ValueError):
        config.coerce_config_value(key, raw)


def test_explore_keys_are_allowed_and_coerced():
    from config import ALLOWED_CONFIG_KEYS, coerce_config_value
    assert {"explore_country", "explore_language"} <= ALLOWED_CONFIG_KEYS
    assert coerce_config_value("explore_country", " us ") == "US"
    assert coerce_config_value("explore_language", "it") == "it"
    for bad in ("USA", "1T", "", 5, None):
        with pytest.raises(ValueError):
            coerce_config_value("explore_country", bad)
    for bad in ("xx", "", None, "EN"):
        with pytest.raises(ValueError):
            coerce_config_value("explore_language", bad)
