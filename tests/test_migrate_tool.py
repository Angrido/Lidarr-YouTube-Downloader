"""Tests for the JSON-to-SQLite migration tool.

Note: The migration tool targets V1 schema tables (download_history,
failed_tracks). Since the DB now initializes to V2 schema (which drops
those tables), most migration operations will fail. These tests verify
the tool's behavior against V2 schema -- it gracefully reports errors
but does not crash.
"""

import json
import os
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


def test_migration_errors_on_v2_schema(config_dir):
    """V2 schema drops old tables, so migration reports errors."""
    result = run_migrate(config_dir)
    assert result.returncode == 0
    assert "ERROR" in result.stdout
    assert "no such table: download_history" in result.stdout


def test_migration_logs_fail_on_v2_schema(config_dir):
    """V2 download_logs has no failed_tracks column."""
    result = run_migrate(config_dir)
    assert "failed_tracks" in result.stdout


def test_migration_does_not_rename_on_error(config_dir):
    """JSON files are NOT renamed when migration fails."""
    run_migrate(config_dir)
    assert (config_dir / "download_history.json").exists()
    assert not (config_dir / "download_history.json.migrated").exists()


def test_migration_no_files(tmp_path):
    result = run_migrate(tmp_path)
    assert result.returncode == 0
    assert "No JSON state files found" in result.stdout


def test_migration_corrupt_json(tmp_path):
    """Corrupt JSON files are skipped with a warning."""
    (tmp_path / "download_history.json").write_text("{invalid json!!!")
    (tmp_path / "download_logs.json").write_text("not json at all")
    result = run_migrate(tmp_path)
    assert result.returncode == 0
    assert "WARNING" in result.stdout
    assert not (tmp_path / "download_history.json.migrated").exists()
    assert not (tmp_path / "download_logs.json.migrated").exists()
