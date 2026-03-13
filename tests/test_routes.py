"""Tests for Flask route handlers in app.py."""

import json
import time
from unittest.mock import patch

import pytest

from db import close_db, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    """Set up a temporary SQLite database for each test."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    init_db()
    yield db_path
    close_db()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """Create a Flask test client with mocked config paths."""
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", config_file)
    monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setenv("LIDARR_URL", "http://localhost:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")

    from app import app

    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestHistoryRoutes:
    def test_get_history_empty(self, client):
        resp = client.get("/api/download/history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1

    def test_get_history_with_entries(self, client):
        import models

        models.add_history_entry(1, "Album A", "Artist A", True, False)
        models.add_history_entry(2, "Album B", "Artist B", False, True)
        resp = client.get("/api/download/history")
        data = resp.get_json()
        assert data["total"] == 2
        assert len(data["items"]) == 2

    def test_get_history_pagination(self, client):
        import models

        for i in range(5):
            models.add_history_entry(i, f"Album {i}", "Artist", True, False)
        resp = client.get("/api/download/history?page=1&per_page=2")
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["pages"] == 3
        assert data["page"] == 1

    def test_get_history_page_2(self, client):
        import models

        for i in range(5):
            models.add_history_entry(i, f"Album {i}", "Artist", True, False)
        resp = client.get("/api/download/history?page=2&per_page=2")
        data = resp.get_json()
        assert data["page"] == 2
        assert len(data["items"]) == 2

    def test_clear_history(self, client):
        import models

        models.add_history_entry(1, "Album", "Artist", True, False)
        resp = client.post("/api/download/history/clear")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        resp2 = client.get("/api/download/history")
        assert resp2.get_json()["total"] == 0


class TestLogsRoutes:
    def test_get_logs_empty(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_logs_with_entries(self, client):
        import models

        models.add_log("download_success", 1, "Album A", "Artist A", "OK")
        models.add_log("album_error", 2, "Album B", "Artist B", "Failed")
        resp = client.get("/api/logs")
        data = resp.get_json()
        assert data["total"] == 2

    def test_get_logs_pagination(self, client):
        import models

        for i in range(5):
            models.add_log("download_success", i, f"Album {i}", "Artist", "OK")
        resp = client.get("/api/logs?page=1&per_page=2")
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["pages"] == 3

    def test_dismiss_log(self, client):
        import models

        log_id = models.add_log(
            "download_success", 1, "Album", "Artist", "OK"
        )
        resp = client.delete(f"/api/logs/{log_id}/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        resp2 = client.get("/api/logs")
        assert resp2.get_json()["total"] == 0

    def test_dismiss_nonexistent_log(self, client):
        resp = client.delete("/api/logs/nonexistent_123/dismiss")
        assert resp.status_code == 404

    def test_clear_logs(self, client):
        import models

        models.add_log("download_success", 1, "Album", "Artist", "OK")
        resp = client.post("/api/logs/clear")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        resp2 = client.get("/api/logs")
        assert resp2.get_json()["total"] == 0

    def test_logs_size(self, client):
        resp = client.get("/api/logs/size")
        data = resp.get_json()
        assert "size" in data
        assert "formatted" in data


class TestFailedTracksRoute:
    def test_get_failed_tracks_empty(self, client):
        resp = client.get("/api/download/failed")
        data = resp.get_json()
        assert data["failed_tracks"] == []
        assert data["album_id"] is None

    def test_get_failed_tracks_with_data(self, client):
        import models

        models.save_failed_tracks(
            album_id=42,
            album_title="Test Album",
            artist_name="Test Artist",
            cover_url="http://example.com/cover.jpg",
            album_path="/tmp/downloads/test",
            lidarr_album_path="/tmp/music/test",
            tracks=[
                {"title": "Track 1", "reason": "Not found", "track_num": 1},
                {"title": "Track 2", "reason": "403 error", "track_num": 2},
            ],
        )
        resp = client.get("/api/download/failed")
        data = resp.get_json()
        assert len(data["failed_tracks"]) == 2
        assert data["album_id"] == 42
        assert data["album_title"] == "Test Album"


class TestStatsRoute:
    def test_stats_empty(self, client):
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["downloaded_today"] == 0
        assert data["in_queue"] == 0

    def test_stats_with_history(self, client):
        import models

        models.add_history_entry(1, "Album", "Artist", True, False)
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["downloaded_today"] == 1

    def test_stats_with_queue(self, client):
        import models

        models.enqueue_album(100)
        models.enqueue_album(200)
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["in_queue"] == 2


class TestQueueRoutes:
    def test_get_empty_queue(self, client):
        with patch("app.lidarr_request", return_value={"error": "not found"}):
            resp = client.get("/api/download/queue")
        assert resp.status_code == 200
        assert resp.get_json() == []

    def test_add_to_queue(self, client):
        resp = client.post(
            "/api/download/queue",
            json={"album_id": 42},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["queue_length"] == 1

    def test_add_duplicate_to_queue(self, client):
        client.post(
            "/api/download/queue",
            json={"album_id": 42},
            content_type="application/json",
        )
        resp = client.post(
            "/api/download/queue",
            json={"album_id": 42},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["queue_length"] == 1

    def test_remove_from_queue(self, client):
        import models

        models.enqueue_album(42)
        resp = client.delete("/api/download/queue/42")
        assert resp.status_code == 200
        assert models.get_queue_length() == 0

    def test_clear_queue(self, client):
        import models

        models.enqueue_album(1)
        models.enqueue_album(2)
        resp = client.post("/api/download/queue/clear")
        assert resp.status_code == 200
        assert models.get_queue_length() == 0

    def test_bulk_add_to_queue(self, client):
        resp = client.post(
            "/api/download/queue/bulk",
            json={"album_ids": [1, 2, 3]},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["added"] == 3
        assert data["queue_length"] == 3

    def test_bulk_add_invalid_input(self, client):
        resp = client.post(
            "/api/download/queue/bulk",
            json={"album_ids": "not a list"},
            content_type="application/json",
        )
        assert resp.status_code == 400


class TestDownloadRoute:
    def test_download_enqueues(self, client):
        import models

        resp = client.post("/api/download/42")
        data = resp.get_json()
        assert data["success"] is True
        assert data["queued"] is True
        assert models.get_queue_length() == 1

    def test_download_duplicate_rejected(self, client):
        client.post("/api/download/42")
        resp = client.post("/api/download/42")
        data = resp.get_json()
        assert data["success"] is False

    def test_download_stop(self, client):
        with patch("app.stop_download") as mock_stop:
            resp = client.post("/api/download/stop")
            assert resp.status_code == 200
            mock_stop.assert_called_once()

    def test_download_status(self, client):
        with patch("app.get_download_status", return_value={"active": False}):
            resp = client.get("/api/download/status")
            assert resp.status_code == 200
            assert resp.get_json()["active"] is False


class TestConfigRoutes:
    def test_get_config(self, client):
        resp = client.get("/api/config")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "lidarr_url" in data
        assert "scheduler_enabled" in data

    def test_set_config(self, client):
        resp = client.post(
            "/api/config",
            json={"scheduler_interval": 120},
            content_type="application/json",
        )
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        resp2 = client.get("/api/config")
        assert resp2.get_json()["scheduler_interval"] == 120

    def test_set_config_rejects_unknown_keys(self, client):
        resp = client.post(
            "/api/config",
            json={"lidarr_url": "http://evil.com"},
            content_type="application/json",
        )
        assert resp.get_json()["success"] is True
        resp2 = client.get("/api/config")
        assert resp2.get_json()["lidarr_url"] != "http://evil.com"

    def test_config_export(self, client):
        resp = client.get("/api/config/export")
        assert resp.status_code == 200
        assert "Content-Disposition" in resp.headers
        data = json.loads(resp.data)
        assert "path_conflict" not in data

    def test_config_import(self, client):
        resp = client.post(
            "/api/config/import",
            json={"scheduler_interval": 30, "lidarr_url": "ignored"},
            content_type="application/json",
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["applied"] == 1
        assert data["skipped"] == 1


class TestTemplateRoutes:
    def test_index(self, client):
        resp = client.get("/")
        assert resp.status_code == 200

    def test_downloads(self, client):
        resp = client.get("/downloads")
        assert resp.status_code == 200

    def test_settings(self, client):
        resp = client.get("/settings")
        assert resp.status_code == 200

    def test_logs_page(self, client):
        resp = client.get("/logs")
        assert resp.status_code == 200


class TestMiscRoutes:
    def test_test_connection(self, client):
        with patch(
            "app.lidarr_request",
            return_value={"version": "1.0.0"},
        ):
            resp = client.get("/api/test-connection")
            data = resp.get_json()
            assert data["status"] == "success"
            assert data["lidarr_version"] == "1.0.0"

    def test_missing_albums(self, client):
        with patch("app.get_missing_albums", return_value=[]):
            resp = client.get("/api/missing-albums")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_ytdlp_version(self, client):
        with patch("app.get_ytdlp_version", return_value="2024.01.01"):
            resp = client.get("/api/ytdlp/version")
            assert resp.get_json()["version"] == "2024.01.01"
