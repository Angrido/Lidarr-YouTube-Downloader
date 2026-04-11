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
    monkeypatch.setenv("LIDARR_URL", "http://env:8686")
    with open(temp_config, "w") as f:
        json.dump({"lidarr_url": "http://file:8686"}, f)
    cfg = config.load_config()
    assert cfg["lidarr_url"] == "http://file:8686"


def test_save_config(temp_config):
    """Save and reload round-trips correctly."""
    cfg = config.load_config()
    cfg["lidarr_url"] = "http://saved:8686"
    config.save_config(cfg)
    reloaded = config.load_config()
    assert reloaded["lidarr_url"] == "http://saved:8686"


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
