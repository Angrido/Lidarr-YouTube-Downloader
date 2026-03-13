"""Tests for the JSON-to-SQLite migration tool."""

import json
import os
import sqlite3
import subprocess
import sys

import pytest


@pytest.fixture
def config_dir(tmp_path):
    history = [
        {
            "album_id": 1,
            "album_title": "A",
            "artist_name": "X",
            "success": True,
            "partial": False,
            "timestamp": 1700000000,
        },
    ]
    logs = [
        {
            "id": "123_1",
            "type": "download_success",
            "album_id": 1,
            "album_title": "A",
            "artist_name": "X",
            "timestamp": 1700000000,
            "details": "ok",
            "failed_tracks": [],
            "dismissed": False,
            "total_file_size": 1024,
        },
    ]
    failed = {
        "failed_tracks": [
            {"title": "T1", "reason": "fail", "track_num": 1}
        ],
        "album_id": 1,
        "album_title": "A",
        "artist_name": "X",
        "cover_url": "http://img",
        "album_path": "/path",
        "album_data": None,
        "cover_data": None,
        "cover_data_b64": None,
        "lidarr_album_path": "/lidarr",
    }
    (tmp_path / "download_history.json").write_text(json.dumps(history))
    (tmp_path / "download_logs.json").write_text(json.dumps(logs))
    (tmp_path / "last_failed_result.json").write_text(json.dumps(failed))
    return tmp_path


def run_migrate(config_dir):
    """Run the migration tool as a subprocess."""
    tool_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools",
        "migrate_json_to_db.py",
    )
    return subprocess.run(
        [sys.executable, tool_path, "--config-dir", str(config_dir)],
        capture_output=True,
        text=True,
    )


def test_migration_creates_db(config_dir):
    result = run_migrate(config_dir)
    assert result.returncode == 0, result.stderr
    assert (config_dir / "lidarr-downloader.db").exists()


def test_migration_imports_history(config_dir):
    run_migrate(config_dir)
    conn = sqlite3.connect(str(config_dir / "lidarr-downloader.db"))
    count = conn.execute(
        "SELECT COUNT(*) FROM download_history"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_migration_imports_logs(config_dir):
    run_migrate(config_dir)
    conn = sqlite3.connect(str(config_dir / "lidarr-downloader.db"))
    count = conn.execute(
        "SELECT COUNT(*) FROM download_logs"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_migration_imports_failed_tracks(config_dir):
    run_migrate(config_dir)
    conn = sqlite3.connect(str(config_dir / "lidarr-downloader.db"))
    count = conn.execute(
        "SELECT COUNT(*) FROM failed_tracks"
    ).fetchone()[0]
    conn.close()
    assert count == 1


def test_migration_renames_json_files(config_dir):
    run_migrate(config_dir)
    assert (config_dir / "download_history.json.migrated").exists()
    assert not (config_dir / "download_history.json").exists()
    assert (config_dir / "download_logs.json.migrated").exists()
    assert not (config_dir / "download_logs.json").exists()
    assert (config_dir / "last_failed_result.json.migrated").exists()
    assert not (config_dir / "last_failed_result.json").exists()


def test_migration_idempotent(config_dir):
    run_migrate(config_dir)
    result = run_migrate(config_dir)
    assert result.returncode == 0
    assert "No JSON state files found" in result.stdout


def test_migration_no_files(tmp_path):
    result = run_migrate(tmp_path)
    assert result.returncode == 0
    assert "No JSON state files found" in result.stdout


def test_migration_partial_files(tmp_path):
    """Only history file present -- other missing files are skipped."""
    history = [
        {
            "album_id": 2,
            "album_title": "B",
            "artist_name": "Y",
            "success": True,
            "partial": False,
            "timestamp": 1700000001,
        },
    ]
    (tmp_path / "download_history.json").write_text(json.dumps(history))
    result = run_migrate(tmp_path)
    assert result.returncode == 0
    conn = sqlite3.connect(str(tmp_path / "lidarr-downloader.db"))
    count = conn.execute(
        "SELECT COUNT(*) FROM download_history"
    ).fetchone()[0]
    conn.close()
    assert count == 1
    assert (tmp_path / "download_history.json.migrated").exists()
