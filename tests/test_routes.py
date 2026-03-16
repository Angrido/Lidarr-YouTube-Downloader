"""Tests for Flask route handlers in app.py."""

import json
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

    app.config["TESTING"] = True  # nosemgrep
    with app.test_client() as c:
        yield c


def _add_track(models, **overrides):
    """Add a track download with sensible defaults, overriding any keys."""
    defaults = {
        "album_id": 1, "album_title": "A", "artist_name": "A",
        "track_title": "T1", "track_number": 1, "success": True,
        "error_message": "", "youtube_url": "", "youtube_title": "",
        "match_score": 0.0, "duration_seconds": 0, "album_path": "",
        "lidarr_album_path": "", "cover_url": "",
    }
    defaults.update(overrides)
    models.add_track_download(**defaults)


class TestHistoryRoutes:
    def test_get_history_empty(self, client):
        resp = client.get("/api/download/history")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []
        assert data["total"] == 0
        assert data["page"] == 1

    def test_get_history_grouped(self, client):
        import models

        _add_track(
            models, album_id=1, album_title="Album A",
            artist_name="Artist A", track_title="T1",
            youtube_url="http://yt/1", youtube_title="vid1",
            match_score=0.9, duration_seconds=200,
        )
        _add_track(
            models, album_id=1, album_title="Album A",
            artist_name="Artist A", track_title="T2",
            track_number=2, success=False, error_message="fail",
        )
        resp = client.get("/api/download/history")
        data = resp.get_json()
        assert data["total"] == 1
        item = data["items"][0]
        assert item["success_count"] == 1
        assert item["fail_count"] == 1

    def test_get_history_pagination(self, client):
        import models

        for i in range(5):
            _add_track(
                models, album_id=i, album_title=f"Album {i}",
                artist_name="Artist",
            )
        resp = client.get("/api/download/history?page=1&per_page=2")
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["items"]) == 2
        assert data["pages"] == 3

    def test_clear_history(self, client):
        import models

        _add_track(models)
        resp = client.post("/api/download/history/clear")
        assert resp.status_code == 200
        resp2 = client.get("/api/download/history")
        assert resp2.get_json()["total"] == 0


class TestTracksEndpoint:
    def test_get_tracks_for_album(self, client):
        import models

        _add_track(
            models, album_id=42, album_title="Album",
            artist_name="Artist", track_title="Track1",
            youtube_url="http://yt/1", youtube_title="vid1",
            match_score=0.92, duration_seconds=240,
            album_path="/dl", lidarr_album_path="/music",
        )
        _add_track(
            models, album_id=42, album_title="Album",
            artist_name="Artist", track_title="Track2",
            track_number=2, success=False, error_message="no match",
            album_path="/dl", lidarr_album_path="/music",
        )
        resp = client.get("/api/download/history/42/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2

    def test_get_tracks_empty(self, client):
        resp = client.get("/api/download/history/999/tracks")
        assert resp.status_code == 200
        assert resp.get_json() == []


class TestLogsRoutes:
    def test_get_logs_empty(self, client):
        resp = client.get("/api/logs")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_logs_no_failed_tracks_field(self, client):
        import models

        models.add_log("download_success", 1, "A", "A", "OK")
        resp = client.get("/api/logs")
        item = resp.get_json()["items"][0]
        assert "failed_tracks" not in item

    def test_get_logs_pagination(self, client):
        import models

        for i in range(5):
            models.add_log(
                "download_success", i, f"Album {i}", "Artist", "OK"
            )
        resp = client.get("/api/logs?page=1&per_page=2")
        data = resp.get_json()
        assert data["total"] == 5
        assert len(data["items"]) == 2

    def test_dismiss_log(self, client):
        import models

        log_id = models.add_log(
            "download_success", 1, "A", "A", "OK"
        )
        resp = client.delete(f"/api/logs/{log_id}/dismiss")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True

    def test_dismiss_nonexistent_log(self, client):
        resp = client.delete("/api/logs/nonexistent_123/dismiss")
        assert resp.status_code == 404

    def test_clear_logs(self, client):
        import models

        models.add_log("download_success", 1, "A", "A", "OK")
        resp = client.post("/api/logs/clear")
        assert resp.status_code == 200

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

    def test_get_failed_tracks_with_data(self, client):
        import models

        _add_track(
            models, album_id=42, album_title="Test Album",
            artist_name="Test Artist", track_title="Track 1",
            success=False, error_message="Not found",
            album_path="/tmp/downloads/test",
            lidarr_album_path="/tmp/music/test",
            cover_url="http://example.com/cover.jpg",
        )
        _add_track(
            models, album_id=42, album_title="Test Album",
            artist_name="Test Artist", track_title="Track 2",
            track_number=2, success=True,
            youtube_url="http://yt/1", youtube_title="vid",
            match_score=0.9, duration_seconds=200,
            album_path="/tmp/downloads/test",
            lidarr_album_path="/tmp/music/test",
            cover_url="http://example.com/cover.jpg",
        )
        resp = client.get("/api/download/failed")
        data = resp.get_json()
        assert len(data["failed_tracks"]) == 1
        assert data["album_id"] == 42


class TestStatsRoute:
    def test_stats_empty(self, client):
        resp = client.get("/api/stats")
        data = resp.get_json()
        assert data["downloaded_today"] == 0
        assert data["in_queue"] == 0

    def test_stats_with_downloads(self, client):
        import models

        _add_track(models)
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

    def test_add_to_queue_null_json(self, client):
        resp = client.post(
            "/api/download/queue",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 200

    def test_bulk_add_empty_json(self, client):
        resp = client.post(
            "/api/download/queue/bulk",
            json={},
            content_type="application/json",
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["added"] == 0


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


class TestSkipTrackRoute:
    """POST /api/download/skip-track sets skip flag."""

    def test_skip_no_active_download(self, client):
        resp = client.post("/api/download/skip-track",
                           json={"track_index": 0})
        assert resp.status_code == 409

    def test_skip_invalid_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": 5})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []

    def test_skip_valid_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": 0})
            assert resp.status_code == 200
            assert download_process["tracks"][0]["skip"] is True
        finally:
            download_process["active"] = False
            download_process["tracks"] = []

    def test_skip_missing_track_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track", json={})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []


    def test_skip_non_integer_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": "foo"})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []

    def test_skip_negative_index(self, client):
        from processing import download_process
        download_process["active"] = True
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "pending",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        try:
            resp = client.post("/api/download/skip-track",
                               json={"track_index": -1})
            assert resp.status_code == 400
        finally:
            download_process["active"] = False
            download_process["tracks"] = []


class TestQueueTracksRoute:
    """GET /api/download/queue/<album_id>/tracks returns track list."""

    @patch("app.lidarr_request")
    def test_returns_tracks_from_lidarr(self, mock_lidarr, client):
        mock_lidarr.return_value = [
            {"title": "Track 1", "trackNumber": 1, "hasFile": False},
            {"title": "Track 2", "trackNumber": 2, "hasFile": True},
        ]
        resp = client.get("/api/download/queue/123/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 2
        assert data[0]["title"] == "Track 1"
        assert data[0]["track_number"] == 1
        assert data[0]["has_file"] is False
        assert data[1]["has_file"] is True

    @patch("app.lidarr_request")
    @patch("app.get_itunes_tracks")
    def test_falls_back_to_itunes(self, mock_itunes, mock_lidarr, client):
        mock_lidarr.return_value = []
        mock_itunes.return_value = [
            {"title": "iTunes Track", "trackNumber": 1},
        ]
        from app import album_cache
        import time as time_mod
        album_cache[123] = (
            {"title": "Album", "artist": {"artistName": "Artist"}},
            time_mod.time(),
        )
        try:
            resp = client.get("/api/download/queue/123/tracks")
            assert resp.status_code == 200
            data = resp.get_json()
            assert len(data) == 1
            assert data[0]["title"] == "iTunes Track"
        finally:
            album_cache.pop(123, None)

    @patch("app.lidarr_request")
    def test_empty_when_no_tracks(self, mock_lidarr, client):
        mock_lidarr.side_effect = lambda path, **kw: (
            {"error": "not found"} if "album/" in path else []
        )
        resp = client.get("/api/download/queue/999/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data == []


class TestQueueTrackCount:
    """Queue endpoint includes track_count."""

    @patch("app.lidarr_request")
    @patch("app.models.get_queue")
    def test_queue_includes_track_count(self, mock_queue, mock_lidarr, client):
        mock_queue.return_value = [{"album_id": 123}]
        mock_lidarr.side_effect = lambda path, **kw: (
            {"title": "Album", "artist": {"artistName": "Art"},
             "images": [{"coverType": "cover", "remoteUrl": "http://img"}],
             "statistics": {"trackCount": 10}}
            if "album/" in path else
            [{"title": "T%d" % i, "trackNumber": i, "hasFile": False}
             for i in range(1, 11)]
        )
        resp = client.get("/api/download/queue")
        assert resp.status_code == 200
        data = resp.get_json()
        assert len(data) == 1
        assert data[0].get("track_count") == 10


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

    def test_test_connection_error(self, client):
        with patch(
            "app.lidarr_request",
            return_value={"error": "Connection refused"},
        ):
            resp = client.get("/api/test-connection")
            data = resp.get_json()
            assert data["status"] == "error"
            assert "Connection refused" in data["message"]

    def test_missing_albums(self, client):
        with patch("app.get_missing_albums", return_value=[]):
            resp = client.get("/api/missing-albums")
            assert resp.status_code == 200
            assert resp.get_json() == []

    def test_ytdlp_version(self, client):
        with patch("app.get_ytdlp_version", return_value="2024.01.01"):
            resp = client.get("/api/ytdlp/version")
            assert resp.get_json()["version"] == "2024.01.01"
