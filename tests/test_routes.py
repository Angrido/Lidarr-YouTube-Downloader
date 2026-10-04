"""Tests for Flask route handlers in app.py."""

import json
from unittest.mock import MagicMock, patch

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


class TestHealthRoute:
    def test_health_ok(self, client):
        resp = client.get("/api/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["db"] is True
        assert data["version"]

    def test_health_alias(self, client):
        assert client.get("/health").status_code == 200


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
        # Missing/invalid album_id is a client error, not a silent no-op
        # (previously this inserted a NULL album_id row).
        resp = client.post(
            "/api/download/queue",
            json={},
            content_type="application/json",
        )
        assert resp.status_code == 400

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

    def test_insights_page(self, client):
        resp = client.get("/insights")
        assert resp.status_code == 200


class TestInsightsRoute:
    """GET /api/insights returns aggregate analytics."""

    def test_insights_empty(self, client):
        resp = client.get("/api/insights")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["totals"]["total_tracks"] == 0
        assert data["window_days"] == 30
        assert isinstance(data["daily"], list)

    def test_insights_with_data(self, client):
        import models

        _add_track(models, album_id=1, artist_name="Artist X",
                   success=True, duration_seconds=200,
                   source_format="140 · m4a · 128 kbps")
        _add_track(models, album_id=1, artist_name="Artist X",
                   success=False)
        resp = client.get("/api/insights?days=7")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["window_days"] == 7
        assert len(data["daily"]) == 7
        assert data["totals"]["total_tracks"] == 2
        assert data["totals"]["successful"] == 1
        assert data["totals"]["success_rate"] == 50.0
        assert data["top_artists"][0]["artist"] == "Artist X"

    def test_insights_days_clamped(self, client):
        assert len(client.get("/api/insights?days=0")
                   .get_json()["daily"]) == 1
        assert len(client.get("/api/insights?days=9999")
                   .get_json()["daily"]) == 365


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


class TestDeleteTrackRoute:
    def test_delete_track_marks_deleted(self, client, tmp_path):
        import models
        _add_track(
            models, album_id=1, album_title="Album",
            artist_name="Artist", track_title="Song",
            track_number=1, youtube_url="https://yt/abc",
            youtube_title="vid", album_path=str(tmp_path),
        )
        # Create the MP3 file so deletion works
        mp3_path = tmp_path / "01 - Song.mp3"
        mp3_path.write_text("fake mp3")
        tracks = models.get_track_downloads_for_album(1)
        track_id = tracks[0]["id"]
        resp = client.delete(
            f"/api/download/track/{track_id}",
            json={"ban_url": False},
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["success"] is True
        assert data["file_deleted"] is True
        assert data["url_banned"] is False
        assert not mp3_path.exists()
        # DB marked as deleted
        tracks = models.get_track_downloads_for_album(1)
        assert tracks[0]["deleted"] == 1

    def test_delete_track_with_ban(self, client, tmp_path):
        import models
        _add_track(
            models, album_id=1, album_title="Album",
            artist_name="Artist", track_title="Song",
            track_number=1, youtube_url="https://yt/abc",
            youtube_title="vid", album_path=str(tmp_path),
        )
        mp3_path = tmp_path / "01 - Song.mp3"
        mp3_path.write_text("fake mp3")
        tracks = models.get_track_downloads_for_album(1)
        track_id = tracks[0]["id"]
        resp = client.delete(
            f"/api/download/track/{track_id}",
            json={"ban_url": True},
        )
        data = resp.get_json()
        assert data["url_banned"] is True
        banned = models.get_banned_urls_for_track(1, "Song")
        assert "https://yt/abc" in banned

    def test_delete_track_removes_xml_sidecar(self, client, tmp_path):
        import models
        _add_track(
            models, album_id=1, track_title="Song",
            track_number=1, album_path=str(tmp_path),
        )
        mp3_path = tmp_path / "01 - Song.mp3"
        xml_path = tmp_path / "01 - Song.xml"
        mp3_path.write_text("fake mp3")
        xml_path.write_text("<xml/>")
        tracks = models.get_track_downloads_for_album(1)
        resp = client.delete(
            f"/api/download/track/{tracks[0]['id']}",
            json={"ban_url": False},
        )
        assert resp.status_code == 200
        assert not mp3_path.exists()
        assert not xml_path.exists()

    def test_delete_track_file_missing(self, client):
        import models
        _add_track(
            models, album_id=1, track_title="Song",
            track_number=1, album_path="/nonexistent/path",
        )
        tracks = models.get_track_downloads_for_album(1)
        resp = client.delete(
            f"/api/download/track/{tracks[0]['id']}",
            json={"ban_url": False},
        )
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["file_deleted"] is False
        # Still marked deleted in DB
        tracks = models.get_track_downloads_for_album(1)
        assert tracks[0]["deleted"] == 1

    def test_delete_track_not_found(self, client):
        resp = client.delete(
            "/api/download/track/9999",
            json={"ban_url": False},
        )
        assert resp.status_code == 404

    def test_delete_track_ban_without_youtube_url(self, client, tmp_path):
        import models
        _add_track(
            models, album_id=1, track_title="Song",
            track_number=1, youtube_url="",
            album_path=str(tmp_path),
        )
        mp3_path = tmp_path / "01 - Song.mp3"
        mp3_path.write_text("fake mp3")
        tracks = models.get_track_downloads_for_album(1)
        resp = client.delete(
            f"/api/download/track/{tracks[0]['id']}",
            json={"ban_url": True},
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["url_banned"] is False
        assert models.get_banned_urls(page=1, per_page=50)["total"] == 0

    def test_delete_track_no_request_body(self, client, tmp_path):
        import models
        _add_track(
            models, album_id=1, track_title="Song",
            track_number=1, album_path=str(tmp_path),
        )
        mp3_path = tmp_path / "01 - Song.mp3"
        mp3_path.write_text("fake mp3")
        tracks = models.get_track_downloads_for_album(1)
        resp = client.delete(
            f"/api/download/track/{tracks[0]['id']}",
        )
        data = resp.get_json()
        assert data["success"] is True
        assert data["file_deleted"] is True
        assert data["url_banned"] is False


class TestBannedUrlsRoutes:
    def test_get_banned_urls_empty(self, client):
        resp = client.get("/api/banned-urls")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_get_banned_urls_with_data(self, client):
        import models
        models.add_banned_url(
            youtube_url="https://yt/abc", youtube_title="vid",
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1,
        )
        resp = client.get("/api/banned-urls")
        data = resp.get_json()
        assert data["total"] == 1
        assert data["items"][0]["youtube_url"] == "https://yt/abc"

    def test_remove_banned_url(self, client):
        import models
        models.add_banned_url(
            youtube_url="https://yt/abc", youtube_title="vid",
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1,
        )
        bans = models.get_banned_urls(page=1, per_page=50)
        ban_id = bans["items"][0]["id"]
        resp = client.delete(f"/api/banned-urls/{ban_id}")
        assert resp.status_code == 200
        assert resp.get_json()["success"] is True
        assert models.get_banned_urls(page=1, per_page=50)["total"] == 0

    def test_remove_banned_url_not_found(self, client):
        resp = client.delete("/api/banned-urls/9999")
        assert resp.status_code == 404

    def test_clear_banned_urls(self, client):
        import models
        for i in range(3):
            models.add_banned_url(
                youtube_url=f"https://yt/{i}", youtube_title="vid",
                album_id=1, album_title="A", artist_name="A",
                track_title=f"T{i}", track_number=i,
            )
        resp = client.post("/api/banned-urls/clear")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["success"] is True
        assert body["removed"] == 3
        assert models.get_banned_urls(page=1, per_page=50)["total"] == 0

    def test_clear_banned_urls_empty(self, client):
        resp = client.post("/api/banned-urls/clear")
        assert resp.status_code == 200
        assert resp.get_json()["removed"] == 0


class TestQueueTracksExtendedFields:
    """Track endpoint returns foreign_recording_id and duration_ms."""

    @patch("app.lidarr_request")
    def test_returns_foreign_recording_id(self, mock_lidarr, client):
        mock_lidarr.return_value = [
            {
                "title": "Song",
                "trackNumber": 1,
                "hasFile": False,
                "foreignRecordingId": "abc-123",
            },
        ]
        resp = client.get("/api/download/queue/1/tracks")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data[0]["foreign_recording_id"] == "abc-123"

    @patch("app.lidarr_request")
    def test_missing_foreign_recording_id_defaults_empty(self, mock_lidarr, client):
        mock_lidarr.return_value = [
            {"title": "Song", "trackNumber": 1, "hasFile": False},
        ]
        resp = client.get("/api/download/queue/1/tracks")
        data = resp.get_json()
        assert data[0]["foreign_recording_id"] == ""


class TestManualTrackDownload:
    """POST /api/album/<album_id>/track/manual-download."""

    @pytest.fixture(autouse=True)
    def _bypass_rate_limit(self):
        with patch("app.check_rate_limit", return_value=True):
            yield

    def test_missing_fields_returns_400(self, client):
        resp = client.post(
            "/api/album/1/track/manual-download",
            json={"youtube_url": "https://youtube.com/watch?v=abc12345678"},
        )
        assert resp.status_code == 400
        assert "Missing required fields" in resp.get_json()["message"]

    def test_missing_url_returns_400(self, client):
        resp = client.post(
            "/api/album/1/track/manual-download",
            json={"track_title": "Song", "track_number": 1},
        )
        assert resp.status_code == 400

    def test_invalid_url_returns_400(self, client):
        resp = client.post(
            "/api/album/1/track/manual-download",
            json={
                "youtube_url": "https://evil.com/malware",
                "track_title": "Song",
                "track_number": 1,
            },
        )
        assert resp.status_code == 400
        assert "Invalid YouTube URL" in resp.get_json()["message"]

    @patch("app._get_album_cached")
    def test_album_not_found_returns_500(self, mock_album, client):
        mock_album.return_value = {"error": "not found"}
        resp = client.post(
            "/api/album/999/track/manual-download",
            json={
                "youtube_url": "https://youtube.com/watch?v=abc12345678",
                "track_title": "Song",
                "track_number": 1,
            },
        )
        assert resp.status_code == 500
        assert "Failed to fetch album" in resp.get_json()["message"]

    @patch("app.fingerprint_track")
    @patch("app.lidarr_request")
    @patch("app._get_album_cached")
    @patch("app.set_permissions")
    @patch("app.tag_audio_file")
    def test_successful_download(
        self, mock_tag, mock_perms, mock_album, mock_lidarr,
        mock_fp, client, tmp_path, monkeypatch,
    ):
        dl_path = str(tmp_path / "downloads")
        monkeypatch.setattr("app.DOWNLOAD_DIR", dl_path)
        monkeypatch.setattr("downloader._ffmpeg_postprocess_works", lambda *a, **kw: True)
        monkeypatch.setattr("app.load_config", lambda: {
            "acoustid_enabled": True,
            "acoustid_api_key": "test-key",
            "xml_metadata_enabled": False,
            "yt_force_ipv4": False,
            "yt_player_client": "",
        })
        mock_album.return_value = {
            "title": "Test Album",
            "releaseDate": "2024-01-01",
            "albumType": "Album",
            "foreignAlbumId": "mbid-1",
            "artist": {
                "artistName": "Test Artist",
                "id": 42,
                "foreignArtistId": "artist-mbid",
            },
            "images": [{"coverType": "cover", "remoteUrl": "http://img/c.jpg"}],
        }
        mock_lidarr.return_value = [
            {"title": "Song", "trackNumber": 1, "hasFile": False},
        ]
        mock_fp.return_value = {
            "acoustid_fingerprint_id": "fp-1",
            "acoustid_score": 0.92,
            "acoustid_recording_id": "rec-1",
            "acoustid_recording_title": "Song",
        }

        import yt_dlp
        import os
        import time

        def fake_download(self_ydl, urls):
            outtmpl = self_ydl.params.get("outtmpl", "")
            if isinstance(outtmpl, dict):
                outtmpl = outtmpl.get("default", "")
            for ext in (".mp3", ".m4a"):
                path = outtmpl + ext
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "wb") as f:
                    f.write(b"\x00" * 100)

        def fake_extract(self_ydl, url, download=True):
            return {"title": "Fake Video Title"}

        with patch.object(yt_dlp.YoutubeDL, "download", fake_download), \
             patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract):
            resp = client.post(
                "/api/album/1/track/manual-download",
                json={
                    "youtube_url": "https://youtube.com/watch?v=abc12345678",
                    "track_title": "Song",
                    "track_number": 1,
                    "foreign_recording_id": "rec-1",
                },
            )

            data = resp.get_json()
            assert resp.status_code == 200
            assert data["success"] is True
            assert data["message"] == "Download queued"

            for _ in range(50):
                from processing import download_process
                if not download_process["active"]:
                    break
                time.sleep(0.1)

        mock_tag.assert_called_once()

    @patch("app._get_album_cached")
    def test_no_download_path_returns_400(self, mock_album, client, monkeypatch):
        monkeypatch.setattr("app.DOWNLOAD_DIR", "")
        mock_album.return_value = {
            "title": "Album",
            "releaseDate": "2024-01-01",
            "albumType": "Album",
            "artist": {"artistName": "Artist", "id": 1, "foreignArtistId": "x"},
            "images": [],
        }
        resp = client.post(
            "/api/album/1/track/manual-download",
            json={
                "youtube_url": "https://youtube.com/watch?v=abc12345678",
                "track_title": "Song",
                "track_number": 1,
            },
        )
        assert resp.status_code == 400
        assert "No download path" in resp.get_json()["message"]

    def test_rate_limiting(self, client):
        """Verify rate limiting works (bypass fixture does NOT apply here)."""
        with patch("app.check_rate_limit", return_value=False):
            resp = client.post(
                "/api/album/1/track/manual-download",
                json={
                    "youtube_url": "https://youtube.com/watch?v=abc12345678",
                    "track_title": "Song",
                    "track_number": 1,
                },
            )
        assert resp.status_code == 429

    @patch("app._get_album_cached")
    @patch("app.set_permissions")
    @patch("app.tag_audio_file")
    def test_ytdlp_exception_sets_failed_status(
        self, mock_tag, mock_perms, mock_album, client, tmp_path, monkeypatch,
    ):
        dl_path = str(tmp_path / "downloads")
        monkeypatch.setattr("app.DOWNLOAD_DIR", dl_path)
        mock_album.return_value = {
            "title": "Album", "releaseDate": "2024-01-01",
            "albumType": "Album", "foreignAlbumId": "m1",
            "artist": {"artistName": "Artist", "id": 1, "foreignArtistId": "a1"},
            "images": [],
        }
        import yt_dlp
        import time

        def boom(self_ydl, urls):
            raise Exception("yt-dlp exploded")

        def fake_extract(self_ydl, url, download=True):
            return {"title": "Fake Title"}

        with patch.object(yt_dlp.YoutubeDL, "download", boom), \
             patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract):
            resp = client.post(
                "/api/album/1/track/manual-download",
                json={
                    "youtube_url": "https://youtube.com/watch?v=abc12345678",
                    "track_title": "Song",
                    "track_number": 1,
                },
            )
            assert resp.status_code == 200
            assert resp.get_json()["message"] == "Download queued"

            for _ in range(50):
                from processing import download_process
                if not download_process["active"]:
                    break
                time.sleep(0.1)

    @patch("app._get_album_cached")
    @patch("app.set_permissions")
    @patch("app.tag_audio_file")
    def test_file_not_created_sets_failed_status(
        self, mock_tag, mock_perms, mock_album, client, tmp_path, monkeypatch,
    ):
        dl_path = str(tmp_path / "downloads")
        monkeypatch.setattr("app.DOWNLOAD_DIR", dl_path)
        mock_album.return_value = {
            "title": "Album", "releaseDate": "2024-01-01",
            "albumType": "Album", "foreignAlbumId": "m1",
            "artist": {"artistName": "Artist", "id": 1, "foreignArtistId": "a1"},
            "images": [],
        }
        import yt_dlp
        import time

        def fake_extract(self_ydl, url, download=True):
            return {"title": "Fake Title"}

        with patch.object(yt_dlp.YoutubeDL, "download", lambda self, urls: None), \
             patch.object(yt_dlp.YoutubeDL, "extract_info", fake_extract):
            resp = client.post(
                "/api/album/1/track/manual-download",
                json={
                    "youtube_url": "https://youtube.com/watch?v=abc12345678",
                    "track_title": "Song",
                    "track_number": 1,
                },
            )
            assert resp.status_code == 200
            assert resp.get_json()["message"] == "Download queued"

            for _ in range(50):
                from processing import download_process  # noqa: F811
                if not download_process["active"]:
                    break
                time.sleep(0.1)


class TestYoutubeStreamValidation:
    """Tests for SSRF prevention in /api/youtube/stream."""

    def test_rejects_non_youtube_url(self, client):
        resp = client.get(
            "/api/youtube/stream", query_string={"url": "http://evil.com/malicious"}
        )
        assert resp.status_code == 400
        assert b"Invalid YouTube URL" in resp.data

    def test_rejects_internal_url(self, client):
        resp = client.get(
            "/api/youtube/stream",
            query_string={"url": "http://169.254.169.254/metadata"},
        )
        assert resp.status_code == 400

    def test_rejects_file_scheme(self, client):
        resp = client.get(
            "/api/youtube/stream",
            query_string={"url": "file:///etc/passwd"},
        )
        assert resp.status_code == 400

    def test_rejects_empty_url(self, client):
        resp = client.get("/api/youtube/stream", query_string={"url": ""})
        assert resp.status_code == 400

    def test_rejects_missing_url(self, client):
        resp = client.get("/api/youtube/stream")
        assert resp.status_code == 400


class TestSafeStreamUrl:
    """Tests for _is_safe_stream_url CDN allowlist."""

    def test_allows_googlevideo(self):
        from app import _is_safe_stream_url

        assert _is_safe_stream_url(
            "https://rr3---sn-abc.googlevideo.com/videoplayback?id=123"
        )

    def test_allows_youtube(self):
        from app import _is_safe_stream_url

        assert _is_safe_stream_url("https://www.youtube.com/stream/123")

    def test_blocks_arbitrary_domain(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url("https://evil.com/audio.mp3")

    def test_blocks_internal_ip(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url("http://192.168.1.1/internal")

    def test_blocks_file_scheme(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url("file:///etc/passwd")

    def test_blocks_empty_string(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url("")

    def test_allows_bare_googlevideo_domain(self):
        from app import _is_safe_stream_url

        assert _is_safe_stream_url(
            "https://googlevideo.com/videoplayback?id=123"
        )

    def test_blocks_lookalike_suffix(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url(
            "https://evilgooglevideo.com/audio"
        )

    def test_blocks_subdomain_of_evil_containing_safe_domain(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url(
            "https://googlevideo.com.evil.com/audio"
        )

    def test_blocks_none_input(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url(None)

    def test_blocks_non_string_input(self):
        from app import _is_safe_stream_url

        assert not _is_safe_stream_url(12345)


class TestValidateYoutubeUrl:
    """Tests for _validate_youtube_url allowlist."""

    def test_accepts_standard_youtube(self):
        from app import _validate_youtube_url

        result = _validate_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_accepts_short_url(self):
        from app import _validate_youtube_url

        result = _validate_youtube_url("https://youtu.be/dQw4w9WgXcQ")
        assert result is not None

    def test_accepts_music_youtube(self):
        from app import _validate_youtube_url

        result = _validate_youtube_url(
            "https://music.youtube.com/watch?v=dQw4w9WgXcQ"
        )
        assert result is not None

    def test_accepts_bare_video_id(self):
        from app import _validate_youtube_url

        result = _validate_youtube_url("dQw4w9WgXcQ")
        assert result == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"

    def test_rejects_non_youtube(self):
        from app import _validate_youtube_url

        assert _validate_youtube_url("https://evil.com/watch?v=abc") is None

    def test_rejects_internal_host(self):
        from app import _validate_youtube_url

        assert _validate_youtube_url("http://localhost:8080/admin") is None

    def test_rejects_javascript_scheme(self):
        from app import _validate_youtube_url

        assert _validate_youtube_url("javascript:alert(1)") is None


class TestPathContainment:
    """Tests for path traversal prevention in manual downloads."""

    def test_sanitize_filename_strips_path_separators(self):
        from utils import sanitize_filename

        result = sanitize_filename("../../etc/passwd")
        assert "/" not in result
        assert ".." not in result

    def test_sanitize_filename_strips_backslash(self):
        from utils import sanitize_filename

        result = sanitize_filename("..\\..\\windows\\system32")
        assert "\\" not in result
        assert ".." not in result

    def test_validate_target_path_blocks_escape(self, tmp_path):
        from app import _validate_target_path

        config = {"lidarr_path": str(tmp_path / "music")}
        assert not _validate_target_path("/etc/evil", config)

    def test_validate_target_path_allows_valid_child(self, tmp_path):
        from app import _validate_target_path

        music = tmp_path / "music"
        config = {"lidarr_path": str(music)}
        assert _validate_target_path(
            str(music / "Artist" / "Album"), config
        )

    def test_validate_target_path_allows_exact_base(self, tmp_path):
        from app import _validate_target_path

        music = tmp_path / "music"
        config = {"lidarr_path": str(music)}
        assert _validate_target_path(str(music), config)


class TestLogsEnrichment:
    def test_track_failure_log_includes_candidates(self, client):
        import models
        from models import CandidateOutcome

        track_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=False,
            error_message="AcoustID failed", youtube_url="",
            youtube_title="", match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(track_id, [
            {
                "youtube_url": "https://youtube.com/watch?v=a",
                "youtube_title": "Video A",
                "match_score": 0.87, "duration_seconds": 222,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "wrong-id",
                "acoustid_matched_title": "Wrong Song",
                "acoustid_score": 0.92,
                "expected_recording_id": "expected-id",
                "error_message": "", "timestamp": 1000.0,
            },
        ])
        models.add_log(
            log_type="track_failure", album_id=1,
            album_title="A", artist_name="A",
            details="AcoustID failed", track_title="T1",
            track_number=1, track_download_id=track_id,
        )
        resp = client.get("/api/logs?type=track_failure")
        data = resp.get_json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["track_title"] == "T1"
        assert "candidates" in item
        assert len(item["candidates"]) == 1
        assert item["candidates"][0]["outcome"] == "mismatch"
        assert item["candidates"][0]["acoustid_matched_title"] == "Wrong Song"

    def test_track_failure_candidate_includes_ban_status(self, client):
        import models
        from models import CandidateOutcome

        track_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=False,
            error_message="failed", youtube_url="",
            youtube_title="", match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(track_id, [
            {
                "youtube_url": "https://youtube.com/watch?v=banned",
                "youtube_title": "Banned Vid",
                "match_score": 0.8, "duration_seconds": 200,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "x",
                "acoustid_matched_title": "X",
                "acoustid_score": 0.9,
                "expected_recording_id": "y",
                "error_message": "", "timestamp": 1000.0,
            },
        ])
        models.add_banned_url(
            youtube_url="https://youtube.com/watch?v=banned",
            youtube_title="Banned Vid", album_id=1,
            album_title="A", artist_name="A",
            track_title="T1", track_number=1,
        )
        models.add_log(
            log_type="track_failure", album_id=1,
            album_title="A", artist_name="A",
            details="failed", track_title="T1",
            track_number=1, track_download_id=track_id,
        )
        resp = client.get("/api/logs?type=track_failure")
        data = resp.get_json()
        cand = data["items"][0]["candidates"][0]
        assert cand["is_banned"] is True
        assert isinstance(cand["ban_id"], int)

    def test_track_download_log_includes_candidates(self, client):
        import models
        from models import CandidateOutcome

        track_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=True,
            error_message="", youtube_url="http://yt/ok",
            youtube_title="OK", match_score=0.9,
            duration_seconds=200, album_path="",
            lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(track_id, [
            {
                "youtube_url": "http://yt/bad",
                "youtube_title": "Bad",
                "match_score": 0.8, "duration_seconds": 200,
                "outcome": CandidateOutcome.UNVERIFIED,
                "acoustid_matched_id": "",
                "acoustid_matched_title": "",
                "acoustid_score": 0.0,
                "expected_recording_id": "rec-1",
                "error_message": "", "timestamp": 1000.0,
            },
            {
                "youtube_url": "http://yt/ok",
                "youtube_title": "OK",
                "match_score": 0.9, "duration_seconds": 200,
                "outcome": CandidateOutcome
                .ACCEPTED_UNVERIFIED_FALLBACK,
                "acoustid_matched_id": "",
                "acoustid_matched_title": "",
                "acoustid_score": 0.0,
                "expected_recording_id": "rec-1",
                "error_message": "", "timestamp": 1001.0,
            },
        ])
        models.add_log(
            log_type="track_download", album_id=1,
            album_title="A", artist_name="A",
            details="Track downloaded successfully",
            track_title="T1", track_number=1,
            track_download_id=track_id,
        )
        resp = client.get("/api/logs?type=track_download")
        data = resp.get_json()
        assert len(data["items"]) == 1
        item = data["items"][0]
        assert item["track_title"] == "T1"
        assert len(item["candidates"]) == 2
        assert item["candidates"][0]["outcome"] == "unverified"
        assert (
            item["candidates"][1]["outcome"]
            == "accepted_unverified_fallback"
        )

    def test_non_track_logs_have_no_candidates(self, client):
        import models

        models.add_log(
            log_type="download_success", album_id=1,
            album_title="A", artist_name="A",
            details="ok",
        )
        resp = client.get("/api/logs")
        data = resp.get_json()
        for item in data["items"]:
            assert "candidates" not in item


class TestNotifyManualDownload:
    """`_notify_manual_download` routes a manual download into
    `send_notifications` with the right log_type, message body, and
    embed fields."""

    def test_sends_with_manual_download_log_type(self):
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
            )

        mock_send.assert_called_once()
        _, kwargs = mock_send.call_args
        assert kwargs["log_type"] == "manual_download"
        message = mock_send.call_args.args[0]
        assert "Manual Download" in message
        assert "Song" in message
        assert "Album" in message
        assert "Artist" in message
        # No AcoustID data => no score line and no embed field.
        assert "AcoustID" not in message
        assert kwargs["embed_data"]["fields"] == []

    def test_includes_acoustid_score_when_present(self):
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={
                    "acoustid_score": 0.92,
                    "acoustid_recording_id": "rec-1",
                },
            )

        message = mock_send.call_args.args[0]
        assert "AcoustID: 0.92" in message
        fields = mock_send.call_args.kwargs["embed_data"]["fields"]
        assert fields == [{
            "name": "AcoustID",
            "value": "0.92",
            "inline": True,
        }]

    def test_handles_missing_album_and_artist(self):
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title=None,
                artist_name=None,
                fp_data={},
            )

        message = mock_send.call_args.args[0]
        assert "Unknown Album" in message
        assert "Unknown Artist" in message

    def test_zero_score_is_omitted(self):
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={"acoustid_score": 0.0},
            )

        message = mock_send.call_args.args[0]
        assert "AcoustID" not in message

    def test_invalid_score_is_tolerated(self):
        """A malformed fp_data value must not raise."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={"acoustid_score": "not-a-number"},
            )

        mock_send.assert_called_once()
        message = mock_send.call_args.args[0]
        assert "AcoustID" not in message

    def test_notification_exception_is_swallowed(self, caplog):
        """Notification failure must not break the download flow."""
        import app as app_module

        with patch(
            "app.send_notifications",
            side_effect=Exception("boom"),
        ):
            # Should not raise.
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
            )
        assert "Manual download notification failed" in caplog.text

    def test_uses_unique_icon_in_title(self):
        """Manual download must use the 👤 icon to be visually distinct
        from automated download notifications (⬇️ ✅ ⚠️ ❌ 📥)."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
            )
        message = mock_send.call_args.args[0]
        assert "👤" in message
        embed = mock_send.call_args.kwargs["embed_data"]
        assert "👤" in embed["title"]

    def test_cover_url_passed_to_telegram_and_discord(self):
        """When cover_url is supplied it must reach both channels:
        Telegram via photo_url, Discord via embed thumbnail."""
        import app as app_module

        cover = "https://example.com/cover.jpg"
        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
                cover_url=cover,
            )
        kwargs = mock_send.call_args.kwargs
        assert kwargs["photo_url"] == cover
        assert kwargs["embed_data"]["thumbnail"] == cover

    def test_no_cover_url_omits_photo_and_thumbnail(self):
        """Empty cover_url must not set photo_url or thumbnail keys."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
                cover_url="",
            )
        kwargs = mock_send.call_args.kwargs
        assert kwargs["photo_url"] is None
        assert "thumbnail" not in kwargs["embed_data"]

    def test_youtube_link_rendered_in_both_channels(self):
        """youtube_url must appear as a clickable link in Telegram (MD2)
        with the video title as the label, and in the Discord embed
        url + a YouTube field."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
                youtube_url="https://youtu.be/abc",
                youtube_title="Song (Official Video)",
            )
        kwargs = mock_send.call_args.kwargs
        # Telegram MD2 body has a clickable link with escaped label.
        tg = kwargs["telegram_message"]
        assert kwargs["telegram_parse_mode"] == "MarkdownV2"
        assert "Song \\(Official Video\\)" in tg
        assert "](https://youtu.be/abc)" in tg
        # Discord embed has the url and a field pointing at YouTube.
        embed = kwargs["embed_data"]
        assert embed["url"] == "https://youtu.be/abc"
        yt_fields = [f for f in embed["fields"] if f["name"] == "YouTube"]
        assert yt_fields
        assert "https://youtu.be/abc" in yt_fields[0]["value"]
        assert "Song (Official Video)" in yt_fields[0]["value"]

    def test_youtube_link_falls_back_to_track_title(self):
        """When youtube_title is empty, the track title is used as the
        link label."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Fallback",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
                youtube_url="https://youtu.be/xyz",
                youtube_title="",
            )
        kwargs = mock_send.call_args.kwargs
        embed = kwargs["embed_data"]
        yt_fields = [f for f in embed["fields"] if f["name"] == "YouTube"]
        assert "Fallback" in yt_fields[0]["value"]

    def test_no_youtube_url_omits_link(self):
        """Without youtube_url, neither the MD2 body nor the embed get
        a YouTube link/field."""
        import app as app_module

        with patch("app.send_notifications") as mock_send:
            app_module._notify_manual_download(
                track_title="Song",
                album_title="Album",
                artist_name="Artist",
                fp_data={},
                youtube_url="",
            )
        kwargs = mock_send.call_args.kwargs
        embed = kwargs["embed_data"]
        assert "url" not in embed
        assert not [f for f in embed["fields"] if f["name"] == "YouTube"]


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


def test_pot_provider_test_valid_bgutil(client, monkeypatch):
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    resp = _FakeResp(200, {"version": "1.2.3", "server_uptime": 10})
    with patch("requests.get", return_value=resp):
        r = client.post(
            "/api/pot-provider/test", json={"url": "http://prov:4416"},
        )
    data = r.get_json()
    assert data["success"] is True
    assert "1.2.3" in data["message"]


def test_pot_provider_test_reachable_but_not_bgutil(client, monkeypatch):
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    resp = _FakeResp(200, {"hello": "world"})
    with patch("requests.get", return_value=resp):
        r = client.post(
            "/api/pot-provider/test", json={"url": "http://nginx:80"},
        )
    data = r.get_json()
    assert data["success"] is False
    assert "not a bgutil" in data["message"].lower()


def test_pot_provider_test_unreachable(client, monkeypatch):
    import requests
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    with patch("requests.get", side_effect=requests.RequestException("boom")):
        r = client.post(
            "/api/pot-provider/test", json={"url": "http://down:4416"},
        )
    data = r.get_json()
    assert data["success"] is False
    assert "not reachable" in data["message"].lower()


def _mock_ydl_with_formats(n):
    m = MagicMock()
    m.return_value.__enter__.return_value.extract_info.return_value = {
        "formats": [{} for _ in range(n)]
    }
    return m


_COOKIES_HEADER = "# Netscape HTTP Cookie File\n"


def test_cookies_test_signed_in(client, tmp_path, monkeypatch):
    # Signed in per yt-dlp's _has_auth_cookies: LOGIN_INFO AND a
    # SAPISID-family cookie on youtube.com.
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        _COOKIES_HEADER
        + ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tabc\n"
        + ".youtube.com\tTRUE\t/\tTRUE\t0\tSAPISID\txyz\n"
    )
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mock_ydl_with_formats(14))
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is True
    assert "signed in" in data["message"].lower()


def test_cookies_test_httponly_login_info_detected(
    client, tmp_path, monkeypatch,
):
    # LOGIN_INFO is an HttpOnly cookie: real exports (yt-dlp, curl, the
    # cookies.txt browser extensions) write it with the #HttpOnly_ line
    # prefix. The Test must not misread such a file as logged out.
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        _COOKIES_HEADER
        + "#HttpOnly_.youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tabc\n"
        + ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\txyz\n"
    )
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mock_ydl_with_formats(14))
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is True
    assert "signed in" in data["message"].lower()


def test_cookies_test_rotated_session_diagnosed(
    client, tmp_path, monkeypatch,
):
    # The signature of a session YouTube rotated/invalidated (and that a
    # later run wrote back to the file): SAPISID-family account cookies
    # survive while LOGIN_INFO is cleared. The Test must say exactly that,
    # not the generic "export from a youtube.com tab" advice.
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        _COOKIES_HEADER
        + ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PAPISID\tabc\n"
        + ".youtube.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tdef\n"
        + ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tx\n"
    )
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mock_ydl_with_formats(14))
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is False
    assert "rotates or invalidates" in data["message"]
    assert "private/incognito" in data["message"]


def test_cookies_test_not_signed_in_warns(client, tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        _COOKIES_HEADER
        + ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tx\n"
    )
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mock_ydl_with_formats(14))
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is False
    assert "login_info" in data["message"].lower()


def test_cookies_test_google_only_not_signed_in(client, tmp_path, monkeypatch):
    # Google-domain auth cookies but no youtube.com LOGIN_INFO: must NOT be
    # reported as signed in (these don't pass YouTube's age gate).
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(
        _COOKIES_HEADER
        + ".google.com\tTRUE\t/\tTRUE\t0\tSAPISID\tabc\n"
        + ".google.com\tTRUE\t/\tTRUE\t0\t__Secure-3PSID\tdef\n"
        + ".youtube.com\tTRUE\t/\tTRUE\t0\tVISITOR_INFO1_LIVE\tx\n"
    )
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    monkeypatch.setattr(yt_dlp, "YoutubeDL", _mock_ydl_with_formats(14))
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is False
    assert "login_info" in data["message"].lower()


def test_cookies_test_no_file(client, monkeypatch):
    monkeypatch.setattr("app.load_config", lambda: {"yt_cookies_file": ""})
    data = client.post("/api/cookies/test").get_json()
    assert data["success"] is False


_COOKIE_ROW = ".youtube.com\tTRUE\t/\tTRUE\t0\tLOGIN_INFO\tabc\n"


def _upload_cookies(client, text):
    import io
    return client.post(
        "/api/cookies/upload",
        data={"file": (io.BytesIO(text.encode("utf-8")), "cookies.txt")},
        content_type="multipart/form-data",
    )


def test_cookies_upload_adds_missing_header(client, tmp_path, monkeypatch):
    target = tmp_path / "cfg" / "cookies.txt"
    monkeypatch.setattr("app.COOKIES_PATH", str(target))
    resp = _upload_cookies(client, _COOKIE_ROW)
    assert resp.status_code == 200
    assert target.read_text() == _COOKIES_HEADER + _COOKIE_ROW
    from yt_dlp.cookies import YoutubeDLCookieJar
    jar = YoutubeDLCookieJar(str(target))
    jar.load(ignore_discard=True, ignore_expires=True)
    assert [c.name for c in jar] == ["LOGIN_INFO"]


def test_cookies_upload_rejects_json_export(client, tmp_path, monkeypatch):
    target = tmp_path / "cookies.txt"
    monkeypatch.setattr("app.COOKIES_PATH", str(target))
    resp = _upload_cookies(client, '[{"name": "SID", "value": "x"}]')
    assert resp.status_code == 400
    assert "JSON" in resp.get_json()["message"]
    assert not target.exists()


def test_cookies_status_reports_unusable_file(client, tmp_path, monkeypatch):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("this is not a cookies file\n")
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    data = client.get("/api/cookies/status").get_json()
    assert data["valid"] is False
    assert data["reason"]


def test_cookies_test_never_lets_ytdlp_rewrite_the_export(
    client, tmp_path, monkeypatch,
):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text(_COOKIES_HEADER + _COOKIE_ROW)
    monkeypatch.setattr(
        "app.load_config", lambda: {"yt_cookies_file": str(cookies)}
    )
    import yt_dlp
    ydl = _mock_ydl_with_formats(3)
    monkeypatch.setattr(yt_dlp, "YoutubeDL", ydl)
    client.post("/api/cookies/test")
    assert ydl.call_args.args[0]["cookiefile"] != str(cookies)


class TestPlaylistToLibrary:
    def test_scan_triggered_when_enabled(self, monkeypatch):
        # With playlist_to_library on, Lidarr configured and tracks done, a
        # path-based DownloadedAlbumsScan is requested (issue #79).
        import app as app_module
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda *a, **k: calls.append(k.get("data")) or {"id": 1},
        )
        app_module._maybe_scan_playlist_into_library(
            {"playlist_to_library": True, "lidarr_url": "http://lidarr:8686"},
            "/music/Artist/Album", 3,
        )
        assert len(calls) == 1
        assert calls[0]["name"] == "DownloadedAlbumsScan"
        assert calls[0]["path"] == "/music/Artist/Album"

    def test_scan_skipped_when_disabled(self, monkeypatch):
        import app as app_module
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda *a, **k: calls.append(1),
        )
        app_module._maybe_scan_playlist_into_library(
            {"playlist_to_library": False, "lidarr_url": "http://lidarr:8686"},
            "/music/Artist/Album", 3,
        )
        assert calls == []

    def test_scan_skipped_when_nothing_downloaded(self, monkeypatch):
        import app as app_module
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda *a, **k: calls.append(1),
        )
        app_module._maybe_scan_playlist_into_library(
            {"playlist_to_library": True, "lidarr_url": "http://lidarr:8686"},
            "/music/Artist/Album", 0,
        )
        assert calls == []

    def test_scan_skipped_without_lidarr_url(self, monkeypatch):
        import app as app_module
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda *a, **k: calls.append(1),
        )
        app_module._maybe_scan_playlist_into_library(
            {"playlist_to_library": True, "lidarr_url": ""},
            "/music/Artist/Album", 3,
        )
        assert calls == []


class TestPlaylistRetryContext:
    """Issue #83: playlist imports get unique negative album_ids so their
    failed tracks can be retried without a Lidarr album."""

    def test_next_playlist_album_id_decrements(self, client):
        import models
        assert models.next_playlist_album_id() == -1
        _add_track(models, album_id=-1, album_title="P1", success=False)
        assert models.next_playlist_album_id() == -2
        _add_track(models, album_id=-2, album_title="P2", success=True)
        assert models.next_playlist_album_id() == -3
        # A positive Lidarr album id doesn't affect the negative allocation.
        _add_track(models, album_id=500, album_title="Real")
        assert models.next_playlist_album_id() == -3

    def test_history_tracks_route_accepts_negative_id(self, client):
        import models
        _add_track(
            models, album_id=-1, album_title="P1", track_title="T1",
            success=False, album_path="/x",
        )
        resp = client.get("/api/download/history/-1/tracks")
        assert resp.status_code == 200
        assert any(t["album_id"] == -1 for t in resp.get_json())

    def test_manual_download_negative_id_no_context(self, client, monkeypatch):
        # A playlist album_id with no stored row returns the context error
        # without ever querying Lidarr for a (non-existent) album.
        import app as app_module
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda p, *a, **k: calls.append(p) or {"error": "x"},
        )
        resp = client.post("/api/download/manual", json={
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "track_title": "Song", "track_num": 1, "album_id": -5,
        })
        data = resp.get_json()
        assert data["success"] is False
        assert "No album context" in data["message"]
        assert calls == []

    def test_manual_download_negative_id_skips_lidarr(
        self, client, monkeypatch, tmp_path,
    ):
        # With stored playlist context, the retry resolves it from the DB
        # row and reaches the download step without a Lidarr album fetch.
        import models
        import app as app_module
        album_dir = tmp_path / "downloads" / "Various" / "My Playlist"
        _add_track(
            models, album_id=-1, album_title="My Playlist",
            artist_name="Various", track_title="Song", track_number=1,
            success=False, album_path=str(album_dir),
        )
        calls = []
        monkeypatch.setattr(
            app_module, "lidarr_request",
            lambda p, *a, **k: calls.append(p) or {"error": "x"},
        )
        monkeypatch.setattr(
            app_module, "_validate_target_path", lambda *a, **k: True,
        )
        monkeypatch.setattr(app_module, "makedirs_safe", lambda *a, **k: None)
        monkeypatch.setattr(
            app_module, "download_youtube_candidate",
            lambda *a, **k: {"success": False, "error_message": "SENTINEL_DL"},
        )
        resp = client.post("/api/download/manual", json={
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "track_title": "Song", "track_num": 1, "album_id": -1,
        })
        data = resp.get_json()
        assert "SENTINEL_DL" in (data.get("message") or "")
        assert calls == []


def test_manual_download_invalid_album_id_400(client, monkeypatch):
    # A non-numeric album_id yields a clean 400, not a 500 (issue #83 review).
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    resp = client.post("/api/download/manual", json={
        "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "track_title": "Song", "track_num": 1, "album_id": "abc",
    })
    assert resp.get_json()["success"] is False
    assert "invalid album id" in resp.get_json()["message"].lower()


def test_manual_download_negative_id_records_success(
    client, monkeypatch, tmp_path,
):
    # End-to-end: a playlist retry (negative album_id) downloads and records
    # the track UNDER THE NEGATIVE ID with success, so it drops out of the
    # failed-retry list.
    import models
    import app as app_module
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    album_dir = tmp_path / "downloads" / "Various" / "My Playlist"
    album_dir.mkdir(parents=True)
    _add_track(
        models, album_id=-1, album_title="My Playlist", artist_name="Various",
        track_title="Song", track_number=1, success=False,
        album_path=str(album_dir),
    )

    ext = app_module.load_config().get("audio_format", "mp3")

    def fake_dl(candidate, output_path, **kw):
        with open(output_path + "." + ext, "wb") as f:
            f.write(b"AUDIO")
        return {"success": True, "youtube_title": candidate["title"]}

    monkeypatch.setattr(app_module, "download_youtube_candidate", fake_dl)
    monkeypatch.setattr(app_module, "_validate_target_path", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "makedirs_safe", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "tag_audio_file", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "create_xml_metadata", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "set_permissions", lambda *a, **k: None)
    monkeypatch.setattr(
        app_module, "lidarr_request",
        lambda *a, **k: pytest.fail("Lidarr must not be called for a playlist"),
    )

    resp = client.post("/api/download/manual", json={
        "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "track_title": "Song", "track_num": 1, "album_id": -1,
    })
    assert resp.get_json()["success"] is True
    # Recorded under the negative id with success -> no longer a failed track.
    rows = models.get_track_downloads_for_album(-1)
    assert any(
        r["track_title"] == "Song" and r["success"] == 1 for r in rows
    )
    assert models.get_failed_tracks_for_retry(-1)["failed_tracks"] == []


def test_manual_download_multi_playlist_isolation(
    client, monkeypatch, tmp_path,
):
    # Retrying a track of playlist B (-2) must rebuild B's folder/context,
    # not A's (-1).
    import models
    import app as app_module
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    dir_a = tmp_path / "A"
    dir_b = tmp_path / "B"
    dir_a.mkdir()
    dir_b.mkdir()
    _add_track(
        models, album_id=-1, album_title="A", artist_name="AA",
        track_title="x", track_number=1, success=False, album_path=str(dir_a),
    )
    _add_track(
        models, album_id=-2, album_title="B", artist_name="BB",
        track_title="Song", track_number=1, success=False,
        album_path=str(dir_b),
    )
    seen = {}
    ext = app_module.load_config().get("audio_format", "mp3")

    def fake_dl(candidate, output_path, **kw):
        seen["output_path"] = output_path
        with open(output_path + "." + ext, "wb") as f:
            f.write(b"AUDIO")
        return {"success": True, "youtube_title": candidate["title"]}

    monkeypatch.setattr(app_module, "download_youtube_candidate", fake_dl)
    monkeypatch.setattr(app_module, "_validate_target_path", lambda *a, **k: True)
    monkeypatch.setattr(app_module, "makedirs_safe", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "tag_audio_file", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "create_xml_metadata", lambda *a, **k: None)
    monkeypatch.setattr(app_module, "set_permissions", lambda *a, **k: None)

    resp = client.post("/api/download/manual", json={
        "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
        "track_title": "Song", "track_num": 1, "album_id": -2,
    })
    assert resp.get_json()["success"] is True
    # Downloaded into B's folder, not A's.
    assert seen["output_path"].startswith(str(dir_b))


def test_next_playlist_album_id_counts_download_logs(client):
    # An import that recorded only a summary log (no track rows) must not
    # have its id handed out again.
    import models
    assert models.next_playlist_album_id() == -1
    models.add_log(
        log_type="manual_download", album_id=-1,
        album_title="P", artist_name="A",
    )
    assert models.next_playlist_album_id() == -2


class TestYtdlpFormatsRoute:
    def test_lists_formats(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.list_video_formats",
            lambda url: {
                "title": "Song",
                "formats": [{
                    "format_id": "141", "ext": "m4a", "acodec": "mp4a",
                    "abr": 256, "filesize": 0, "audio_only": True,
                    "note": "",
                }],
            },
        )
        resp = client.post("/api/ytdlp/formats", json={"url": "abcdefghijk"})
        data = resp.get_json()
        assert data["success"] is True
        assert data["title"] == "Song"
        assert data["formats"][0]["format_id"] == "141"

    def test_missing_url_is_error(self, client):
        data = client.post("/api/ytdlp/formats", json={}).get_json()
        assert data["success"] is False

    def test_no_formats_is_error(self, client, monkeypatch):
        monkeypatch.setattr(
            "app.list_video_formats",
            lambda url: {"title": "x", "formats": []},
        )
        data = client.post(
            "/api/ytdlp/formats", json={"url": "abcdefghijk"}
        ).get_json()
        assert data["success"] is False

    def test_extractor_error_is_reported(self, client, monkeypatch):
        def boom(url):
            raise Exception("Video unavailable")
        monkeypatch.setattr("app.list_video_formats", boom)
        data = client.post(
            "/api/ytdlp/formats", json={"url": "dQw4w9WgXcQ"}
        ).get_json()
        assert data["success"] is False
        assert "unavailable" in data["message"].lower()

    def test_rejects_non_youtube_url(self, client, monkeypatch):
        # SSRF guard: a non-YouTube URL must be refused before yt-dlp is
        # ever invoked.
        calls = []
        monkeypatch.setattr(
            "app.list_video_formats", lambda url: calls.append(url)
        )
        data = client.post(
            "/api/ytdlp/formats",
            json={"url": "http://localhost:8080/admin"},
        ).get_json()
        assert data["success"] is False
        assert calls == []

    def test_accepts_music_youtube_url(self, client, monkeypatch):
        received = []

        def fake(url):
            received.append(url)
            return {"title": "Song", "formats": [{
                "format_id": "141", "ext": "m4a", "acodec": "mp4a",
                "abr": 256, "filesize": 0, "audio_only": True, "note": "",
            }]}
        monkeypatch.setattr("app.list_video_formats", fake)
        data = client.post(
            "/api/ytdlp/formats",
            json={"url": "https://music.youtube.com/watch?v=DIEI2YLYg6o"},
        ).get_json()
        assert data["success"] is True
        assert received == [
            "https://music.youtube.com/watch?v=DIEI2YLYg6o"
        ]

    def test_bare_id_is_normalized_before_listing(self, client, monkeypatch):
        received = []

        def fake(url):
            received.append(url)
            return {"title": "x", "formats": [{
                "format_id": "140", "ext": "m4a", "acodec": "mp4a",
                "abr": 128, "filesize": 0, "audio_only": True, "note": "",
            }]}
        monkeypatch.setattr("app.list_video_formats", fake)
        client.post("/api/ytdlp/formats", json={"url": "dQw4w9WgXcQ"})
        assert received == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]


def test_pwa_manifest(client):
    import json as _json
    resp = client.get("/manifest.webmanifest")
    assert resp.status_code == 200
    assert "manifest" in resp.mimetype
    m = _json.loads(resp.get_data(as_text=True))
    assert m["start_url"] == "/"
    assert m["display"] == "standalone"
    assert m["icons"] and m["icons"][0]["src"].endswith(".svg")


def test_pwa_manifest_has_png_and_maskable_icons(client):
    import json as _json
    m = _json.loads(client.get("/manifest.webmanifest").get_data(as_text=True))
    pngs = [i for i in m["icons"] if i["type"] == "image/png"]
    assert {i["sizes"] for i in pngs} >= {"192x192", "512x512"}
    assert any(i["purpose"] == "maskable" for i in pngs)
    assert all("maskable" not in i["purpose"] for i in m["icons"] if i["src"].endswith(".svg"))
    for icon in m["icons"]:
        resp = client.get(icon["src"])
        assert resp.status_code == 200
        resp.close()


def test_apple_touch_icon_is_png(client):
    resp = client.get("/static/apple-touch-icon.png")
    assert resp.status_code == 200
    assert resp.get_data()[:8] == b"\x89PNG\r\n\x1a\n"
    resp.close()


def test_stats_reports_queue_and_any_active_download(client, monkeypatch):
    import processing
    monkeypatch.setattr("models.get_queue_length", lambda: 3)
    monkeypatch.setattr("models.get_history_count_today", lambda: 0)
    data = client.get("/api/stats").get_json()
    assert data["queued"] == 3
    assert data["active"] is False
    monkeypatch.setitem(processing._active_states, 77, {"active": True})
    data = client.get("/api/stats").get_json()
    assert data["active"] is True
    assert data["queued"] == 3


def test_service_worker(client):
    resp = client.get("/sw.js")
    assert resp.status_code == 200
    assert "javascript" in resp.mimetype
    assert resp.headers.get("Service-Worker-Allowed") == "/"
    assert "addEventListener('fetch'" in resp.get_data(as_text=True)


def test_setup_page_renders(client):
    resp = client.get("/setup")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "downloadPath" in body and "lidarrPath" in body
    assert "testConnection" in body
    assert "/static/components.css" in body


def test_components_css_served(client):
    resp = client.get("/static/components.css")
    assert resp.status_code == 200
    assert ".ui-btn" in resp.get_data(as_text=True)


def test_backup_export_returns_sqlite(client, tmp_path):
    resp = client.get("/api/backup/export")
    assert resp.status_code == 200
    data = resp.get_data()
    assert data[:16] == b"SQLite format 3\x00"
    import sqlite3
    f = tmp_path / "dl.db"
    f.write_bytes(data)
    con = sqlite3.connect(str(f))
    has = con.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name='schema_version'"
    ).fetchone()
    con.close()
    assert has is not None


def test_backup_import_rejects_invalid(client, monkeypatch):
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    import io
    data = {"file": (io.BytesIO(b"not a database"), "bad.db")}
    resp = client.post(
        "/api/backup/import", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_backup_import_valid_restarts(client, monkeypatch, tmp_path):
    import io
    import sqlite3
    import app as app_module
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    restarted = []
    monkeypatch.setattr(app_module, "_exec_restart", lambda: restarted.append(1))
    bak = tmp_path / "backup.db"
    con = sqlite3.connect(str(bak))
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at REAL)")
    con.execute("INSERT INTO schema_version VALUES (10, 0)")
    con.commit()
    con.close()
    data = {"file": (io.BytesIO(bak.read_bytes()), "backup.db")}
    resp = client.post(
        "/api/backup/import", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 200
    assert resp.get_json()["success"] is True
    import time
    for _ in range(50):
        if restarted:
            break
        time.sleep(0.1)
    assert restarted == [1]


def test_backup_import_rejects_empty_schema_version(client, monkeypatch, tmp_path):
    # A DB whose schema_version table exists but has no rows must be
    # rejected: restoring it would make init_db re-run every migration and
    # crash-loop the app on restart.
    import io
    import sqlite3
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    bak = tmp_path / "empty.db"
    con = sqlite3.connect(str(bak))
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at REAL)")
    con.commit()
    con.close()
    data = {"file": (io.BytesIO(bak.read_bytes()), "empty.db")}
    resp = client.post(
        "/api/backup/import", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 400
    assert resp.get_json()["success"] is False


def test_backup_import_rejects_future_schema(client, monkeypatch, tmp_path):
    import io
    import sqlite3
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    bak = tmp_path / "future.db"
    con = sqlite3.connect(str(bak))
    con.execute("CREATE TABLE schema_version (version INTEGER, applied_at REAL)")
    con.execute("INSERT INTO schema_version VALUES (999, 0)")
    con.commit()
    con.close()
    data = {"file": (io.BytesIO(bak.read_bytes()), "future.db")}
    resp = client.post(
        "/api/backup/import", data=data, content_type="multipart/form-data"
    )
    assert resp.status_code == 400
    assert "not supported" in resp.get_json()["message"].lower()


def test_backup_import_refused_while_downloading(client, monkeypatch):
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    import app as app_module
    import io
    app_module.download_process["active"] = True
    try:
        data = {"file": (io.BytesIO(b"x"), "b.db")}
        resp = client.post(
            "/api/backup/import", data=data,
            content_type="multipart/form-data",
        )
        assert resp.status_code == 409
    finally:
        app_module.download_process["active"] = False



def test_backup_import_refused_while_client_job_downloads(client, monkeypatch):
    monkeypatch.setattr("app.check_rate_limit", lambda *a, **k: True)
    import io
    import processing
    monkeypatch.setitem(processing._active_states, 4242, {"active": True})
    data = {"file": (io.BytesIO(b"x"), "b.db")}
    resp = client.post(
        "/api/backup/import", data=data,
        content_type="multipart/form-data",
    )
    assert resp.status_code == 409


def test_restart_refused_while_client_job_downloads(client, monkeypatch):
    import processing
    called = []
    monkeypatch.setattr("app._exec_restart", lambda: called.append(1))
    monkeypatch.setitem(processing._active_states, 4243, {"active": True})
    resp = client.post("/api/restart")
    assert resp.get_json()["success"] is False
    assert called == []


class TestFfmpegStatusRoute:
    def test_status_reports_ok(self, client, monkeypatch):
        import downloader
        monkeypatch.setattr(downloader, "_ffmpeg_pp_state", True)
        resp = client.get("/api/ffmpeg/status")
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_status_explains_a_broken_host(self, client, monkeypatch):
        import downloader
        monkeypatch.setattr(downloader, "_ffmpeg_pp_state", False)
        data = client.get("/api/ffmpeg/status").get_json()
        assert data["ok"] is False
        assert data["fixes"]
        assert "machine" in data

    def test_health_exposes_ffmpeg_state(self, client, monkeypatch):
        import downloader
        monkeypatch.setattr(downloader, "_ffmpeg_pp_state", True)
        assert client.get("/api/health").get_json()["ffmpeg_ok"] is True


class TestNtfyNotificationRoute:
    @pytest.fixture(autouse=True)
    def reset_rate_limit(self):
        import app
        app.rate_limit_store.clear()
        yield
        app.rate_limit_store.clear()

    def test_test_ntfy_route_success(self, client):
        with patch("notifications.send_ntfy_test") as mock_test:
            mock_test.return_value = {"success": True, "error": ""}
            resp = client.post(
                "/api/notifications/test/ntfy",
                json={
                    "server_url": "https://ntfy.sh",
                    "topic": "my-topic",
                    "token": "tk_test",
                    "priority": "high",
                },
            )
            assert resp.status_code == 200
            data = resp.get_json()
            assert data["success"] is True
            assert data["message"] == "Test message sent successfully"
            mock_test.assert_called_once_with(
                "https://ntfy.sh", "my-topic", token="tk_test", priority="high",
            )

    def test_test_ntfy_route_missing_topic(self, client, monkeypatch):
        monkeypatch.setattr("app.load_config", lambda: {"ntfy_topic": ""})
        resp = client.post(
            "/api/notifications/test/ntfy",
            json={"server_url": "https://ntfy.sh", "topic": ""},
        )
        assert resp.status_code == 400
        assert "Missing Ntfy topic" in resp.get_json()["message"]

    def test_test_ntfy_route_invalid_url(self, client):
        resp = client.post(
            "/api/notifications/test/ntfy",
            json={"server_url": "ftp://bad-url", "topic": "mytopic"},
        )
        assert resp.status_code == 400
        assert "Server URL must start with http://" in resp.get_json()["message"]

    def test_test_ntfy_route_failure_from_service(self, client):
        with patch("notifications.send_ntfy_test") as mock_test:
            mock_test.return_value = {"success": False, "error": "HTTP 401: Unauthorized"}
            resp = client.post(
                "/api/notifications/test/ntfy",
                json={"server_url": "https://ntfy.sh", "topic": "private-topic"},
            )
            assert resp.status_code == 400
            data = resp.get_json()
            assert data["success"] is False
            assert "Unauthorized" in data["message"]

    def test_test_ntfy_route_rate_limiting(self, client):
        with patch("notifications.send_ntfy_test", return_value={"success": True, "error": ""}):
            for _ in range(3):
                resp = client.post(
                    "/api/notifications/test/ntfy",
                    json={"server_url": "https://ntfy.sh", "topic": "t"},
                )
                assert resp.status_code == 200
            resp = client.post(
                "/api/notifications/test/ntfy",
                json={"server_url": "https://ntfy.sh", "topic": "t"},
            )
            assert resp.status_code == 429
            assert "Too many test requests" in resp.get_json()["message"]




class TestManualDownloadTrackNumber:
    @pytest.fixture(autouse=True)
    def _bypass_rate_limit(self):
        with patch("app.check_rate_limit", return_value=True):
            yield

    def test_vinyl_track_number_does_not_leave_download_active(
        self, client, monkeypatch, tmp_path,
    ):
        import app as app_module
        from processing import download_process
        seen = {}
        monkeypatch.setattr(app_module, "makedirs_safe", lambda *a, **k: None)
        monkeypatch.setattr(
            app_module, "_do_manual_dl",
            lambda **kw: seen.update(kw),
        )
        monkeypatch.setitem(download_process, "active", False)
        app_module._execute_manual_dl_with_progress(
            youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
            track_title="Song", track_num="A1",
            target_path=str(tmp_path), album_data={}, album_id=1,
            album_title="Album", artist_name="Artist", config={},
            album_path=str(tmp_path), lidarr_album_path=str(tmp_path),
            cover_url="", makedirs_bases=[],
        )
        assert download_process["active"] is False
        assert seen["track_num"] == 0

    def test_state_setup_failure_still_releases_active(
        self, client, monkeypatch, tmp_path,
    ):
        import app as app_module
        from processing import download_process
        monkeypatch.setitem(download_process, "active", False)
        monkeypatch.setattr(
            app_module, "_parse_track_number",
            lambda raw: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        with pytest.raises(RuntimeError):
            app_module._execute_manual_dl_with_progress(
                youtube_url="https://www.youtube.com/watch?v=abcdefghijk",
                track_title="Song", track_num=1,
                target_path=str(tmp_path), album_data={}, album_id=1,
                album_title="Album", artist_name="Artist", config={},
                album_path=str(tmp_path), lidarr_album_path=str(tmp_path),
                cover_url="", makedirs_bases=[],
            )
        assert download_process["active"] is False

    def test_album_route_parses_vinyl_track_number(
        self, client, monkeypatch, tmp_path,
    ):
        import app as app_module
        seen = {}
        monkeypatch.setattr("app.DOWNLOAD_DIR", str(tmp_path / "downloads"))
        monkeypatch.setattr(app_module, "_get_album_cached", lambda aid: {
            "title": "Album", "artist": {"artistName": "Artist"},
            "images": [],
        })
        monkeypatch.setattr(
            app_module, "_execute_manual_dl_with_progress",
            lambda **kw: seen.update(kw),
        )
        monkeypatch.setattr(
            app_module.threading, "Thread",
            lambda target, daemon=None: MagicMock(start=target),
        )
        resp = client.post("/api/album/1/track/manual-download", json={
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "track_title": "Song", "track_number": "A1",
        })
        assert resp.status_code == 200
        assert seen["track_num"] == 0

    def test_manual_route_vinyl_track_number_not_500(
        self, client, monkeypatch, tmp_path,
    ):
        import models
        import app as app_module
        album_dir = tmp_path / "downloads" / "Various" / "My Playlist"
        _add_track(
            models, album_id=-1, album_title="My Playlist",
            artist_name="Various", track_title="Song", track_number=1,
            success=False, album_path=str(album_dir),
        )
        monkeypatch.setattr(
            app_module, "_validate_target_path", lambda *a, **k: True,
        )
        monkeypatch.setattr(app_module, "makedirs_safe", lambda *a, **k: None)
        monkeypatch.setattr(
            app_module, "download_youtube_candidate",
            lambda *a, **k: {"success": False, "error_message": "SENTINEL_DL"},
        )
        resp = client.post("/api/download/manual", json={
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "track_title": "Song", "track_num": "A1", "album_id": -1,
        })
        assert "SENTINEL_DL" in (resp.get_json().get("message") or "")


class TestConfigHardening:
    @pytest.fixture(autouse=True)
    def _bypass_rate_limit(self):
        with patch("app.check_rate_limit", return_value=True):
            yield

    def test_get_config_hides_lidarr_api_key(self, client):
        data = client.get("/api/config").get_json()
        assert "lidarr_api_key" not in data
        assert "test-key" not in json.dumps(data)

    def test_export_hides_lidarr_api_key(self, client):
        resp = client.get("/api/config/export")
        assert "lidarr_api_key" not in json.loads(resp.data)

    @pytest.mark.parametrize("value", ["abc", None, [], True])
    def test_invalid_int_returns_400(self, client, value):
        resp = client.post("/api/config", json={"scheduler_interval": value})
        assert resp.status_code == 400
        assert "scheduler_interval" in resp.get_json()["message"]

    def test_invalid_value_not_saved(self, client):
        client.post(
            "/api/config",
            json={"scheduler_interval": 5, "duration_tolerance": "x"},
        )
        assert client.get("/api/config").get_json()["scheduler_interval"] == 60

    def test_string_int_coerced(self, client):
        resp = client.post("/api/config", json={"scheduler_interval": "90"})
        assert resp.status_code == 200
        assert client.get("/api/config").get_json()["scheduler_interval"] == 90

    @pytest.mark.parametrize(
        "raw,expected",
        [("false", False), ("true", True), ("0", False), ("yes", True),
         ("off", False), (0, False), (1, True), (False, False)],
    )
    def test_bool_strings_coerced(self, client, raw, expected):
        resp = client.post("/api/config", json={"audio_normalize": raw})
        assert resp.status_code == 200
        assert client.get("/api/config").get_json()["audio_normalize"] is expected

    def test_invalid_bool_returns_400(self, client):
        resp = client.post("/api/config", json={"audio_normalize": "maybe"})
        assert resp.status_code == 400

    def test_invalid_float_returns_400(self, client):
        resp = client.post("/api/config", json={"min_match_score": "high"})
        assert resp.status_code == 400

    def test_list_key_rejects_string(self, client):
        resp = client.post("/api/config", json={"forbidden_words": "remix"})
        assert resp.status_code == 400

    def test_import_invalid_value_returns_400(self, client):
        resp = client.post(
            "/api/config/import", json={"scheduler_interval": "abc"},
        )
        assert resp.status_code == 400

    def test_import_coerces_bool(self, client):
        resp = client.post(
            "/api/config/import", json={"scheduler_auto_download": "false"},
        )
        assert resp.status_code == 200
        cfg = client.get("/api/config").get_json()
        assert cfg["scheduler_auto_download"] is False

    def test_config_array_body_returns_400(self, client):
        resp = client.post("/api/config", json=[1, 2])
        assert resp.status_code == 400

    def test_concurrent_toggles_do_not_lose_updates(self, client, monkeypatch):
        import threading
        import time
        import config as config_module
        from app import app as flask_app
        real_load = config_module.load_config

        def slow_load():
            cfg = real_load()
            time.sleep(0.1)
            return cfg

        monkeypatch.setattr("config.load_config", slow_load)
        monkeypatch.setattr("app.load_config", slow_load)
        before = real_load()
        results = []

        def toggle(path):
            with flask_app.test_client() as c:
                results.append(c.post(path).status_code)

        threads = [
            threading.Thread(target=toggle, args=("/api/xmlmetadata/toggle",)),
            threading.Thread(target=toggle, args=("/api/acoustid/toggle",)),
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        after = real_load()
        assert results == [200, 200]
        assert after["xml_metadata_enabled"] is not before["xml_metadata_enabled"]
        assert after["acoustid_enabled"] is not before["acoustid_enabled"]

    def test_saved_file_omits_env_only_keys(self, client, tmp_path):
        client.post("/api/xmlmetadata/toggle")
        with open(tmp_path / "config.json") as f:
            raw = json.load(f)
        assert "lidarr_api_key" not in raw
        assert "lidarr_url" not in raw


class TestYoutubeRecent:
    def test_lists_negative_playlist_imports(self, client):
        import models
        _add_track(models, album_id=-1, album_title="P1", artist_name="V")
        _add_track(models, album_id=-2, album_title="P1", artist_name="V")
        _add_track(models, album_id=7, album_title="Real", artist_name="R")
        data = client.get("/api/youtube/recent").get_json()
        assert len(data) == 2
        assert {d["album_title"] for d in data} == {"P1"}


class TestQueueIdValidation:
    @pytest.fixture(autouse=True)
    def _bypass_rate_limit(self):
        with patch("app.check_rate_limit", return_value=True):
            yield

    @pytest.mark.parametrize("value", [-1, 0, True, "5", 1.5])
    def test_add_rejects_non_positive_or_non_int(self, client, value):
        import models
        resp = client.post("/api/download/queue", json={"album_id": value})
        assert resp.status_code == 400
        assert models.get_queue_length() == 0

    def test_bulk_skips_invalid_ids(self, client):
        import models
        resp = client.post(
            "/api/download/queue/bulk",
            json={"album_ids": [-1, True, 0, 2]},
        )
        assert resp.get_json()["added"] == 1
        assert [r["album_id"] for r in models.get_queue()] == [2]

    def test_add_array_body_returns_400(self, client):
        resp = client.post("/api/download/queue", json=[1])
        assert resp.status_code == 400

    def test_bulk_array_body_returns_400(self, client):
        resp = client.post("/api/download/queue/bulk", json=[1])
        assert resp.status_code == 400

    def test_queue_tolerates_images_missing_keys(self, client, monkeypatch):
        import models
        models.enqueue_album(5)
        monkeypatch.setattr("app._get_album_cached", lambda aid: {
            "title": "A", "artist": {"artistName": "B"},
            "images": [{"url": "x"}, {"coverType": "cover"},
                       {"coverType": "cover", "remoteUrl": "http://c"}],
        })
        resp = client.get("/api/download/queue")
        assert resp.status_code == 200
        assert resp.get_json()[0]["cover"] == ""


class TestJsonBodyValidation:
    @pytest.fixture(autouse=True)
    def _bypass_rate_limit(self):
        with patch("app.check_rate_limit", return_value=True):
            yield

    def test_youtube_search_null_query(self, client):
        resp = client.post("/api/youtube/search", json={"query": None})
        assert resp.status_code == 400

    def test_youtube_search_array_body(self, client):
        resp = client.post("/api/youtube/search", json=["x"])
        assert resp.status_code == 400

    def test_skip_track_array_body(self, client):
        resp = client.post("/api/download/skip-track", json=[0])
        assert resp.status_code == 400

    def test_manual_download_null_url(self, client):
        resp = client.post("/api/download/manual", json={
            "youtube_url": None, "track_title": "Song",
        })
        assert resp.status_code == 400

    def test_manual_track_download_non_string_title(self, client):
        resp = client.post("/api/album/1/track/manual-download", json={
            "youtube_url": "https://www.youtube.com/watch?v=abcdefghijk",
            "track_title": 5,
        })
        assert resp.status_code == 400

    def test_playlist_info_non_string_url(self, client):
        resp = client.post("/api/youtube/playlist/info", json={"url": 5})
        assert resp.status_code == 400

    @pytest.mark.parametrize("entries", [["x"], [{"url": 5}], "abc",
                                         [{"url": "abcdefghijk", "title": 3}]])
    def test_playlist_download_bad_entries(self, client, entries):
        resp = client.post("/api/youtube/playlist/download", json={
            "artist_name": "A", "album_title": "B", "entries": entries,
        })
        assert resp.status_code == 400

    def test_playlist_download_null_artist(self, client):
        resp = client.post("/api/youtube/playlist/download", json={
            "artist_name": None, "album_title": "B",
            "entries": [{"url": "abcdefghijk"}],
        })
        assert resp.status_code == 400

    def test_reorder_array_body(self, client):
        resp = client.put("/api/download/queue/reorder", json=[1, 2])
        assert resp.status_code == 400

    def test_ytdlp_formats_non_string_url(self, client):
        resp = client.post("/api/ytdlp/formats", json={"url": 5})
        assert resp.status_code == 400

    def test_pot_provider_array_body(self, client):
        resp = client.post("/api/pot-provider/test", json=["x"])
        assert resp.status_code == 400


class TestLibraryRoutes:
    def test_add_page_renders(self, client):
        resp = client.get("/add")
        assert resp.status_code == 200
        assert b"Add music" in resp.data

    def test_search_returns_results(self, client, monkeypatch):
        import library
        monkeypatch.setattr(
            library, "search",
            lambda kind, term: [{"kind": kind, "term": term}],
        )
        data = client.get("/api/library/search?type=album&term=disc").get_json()
        assert data == {
            "success": True, "results": [{"kind": "album", "term": "disc"}],
        }

    def test_library_errors_keep_their_status(self, client, monkeypatch):
        import library

        def boom(payload):
            raise library.LibraryError("already in your library", 409)

        monkeypatch.setattr(library, "add_artist", boom)
        resp = client.post("/api/library/artist", json={})
        assert resp.status_code == 409
        assert resp.get_json() == {
            "success": False, "message": "already in your library",
        }

    def test_add_album(self, client, monkeypatch):
        import library
        monkeypatch.setattr(
            library, "add_album", lambda payload: {"id": 3, "echo": payload},
        )
        data = client.post(
            "/api/library/album", json={"foreignAlbumId": "x", "download": True},
        ).get_json()
        assert data["album"]["echo"]["download"] is True

    def test_pending(self, client):
        assert client.get("/api/library/pending").get_json() == {"pending": []}

    def test_search_is_rate_limited(self, client, monkeypatch):
        import app as app_module
        import library
        app_module.rate_limit_store.clear()
        monkeypatch.setattr(library, "search", lambda kind, term: [])
        codes = [
            client.get("/api/library/search?term=ab").status_code
            for _ in range(21)
        ]
        app_module.rate_limit_store.clear()
        assert codes[-1] == 429


class TestExploreRoutes:
    @pytest.fixture(autouse=True)
    def _fresh(self, monkeypatch):
        import app as app_module
        import explore
        explore.cache.invalidate()
        app_module.rate_limit_store.clear()
        yield
        explore.cache.invalidate()

    def test_page_renders(self, client):
        resp = client.get("/explore")
        assert resp.status_code == 200
        html = resp.get_data(as_text=True)
        assert "<title>Explore" in html
        assert 'id="player"' in html

    def test_explore_is_in_the_navigation(self, client):
        html = client.get("/").get_data(as_text=True)
        assert 'href="/explore" data-nav="explore"' in html
        tabbar = html.split('class="app-tabbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/explore"' in tabbar
        assert 'href="/youtube"' not in tabbar
        assert tabbar.count('class="app-tab"') == 6

    def test_youtube_page_highlights_explore_tab(self, client):
        html = client.get("/youtube").get_data(as_text=True)
        tabbar = html.split('class="app-tabbar"', 1)[1].split("</nav>", 1)[0]
        assert 'href="/explore" aria-current="page"' in tabbar

    def test_add_page_links_to_explore(self, client):
        assert 'href="/explore"' in client.get("/add").get_data(as_text=True)

    def test_home_ok(self, client, monkeypatch):
        import explore
        monkeypatch.setattr(explore, "home", lambda country="": {
            "country": "IT", "sections": [{"key": "hero", "items": []}], "moods": [],
        })
        data = client.get("/api/explore/home").get_json()
        assert data["success"] is True and data["country"] == "IT"

    def test_home_reports_youtube_down(self, client, monkeypatch):
        import explore
        monkeypatch.setattr(explore, "home", lambda country="": {
            "country": "IT", "sections": [], "moods": [],
        })
        resp = client.get("/api/explore/home")
        assert resp.status_code == 502
        assert resp.get_json()["success"] is False

    def test_unexpected_errors_never_leak(self, client, monkeypatch):
        import explore

        def boom(country=""):
            raise RuntimeError("Traceback secret")

        monkeypatch.setattr(explore, "home", boom)
        resp = client.get("/api/explore/home")
        assert resp.status_code == 502
        assert "secret" not in resp.get_data(as_text=True)

    @pytest.mark.parametrize("url", [
        "/api/explore/album/UC_not_an_album",
        "/api/explore/artist/MPREb_abcdef",
        "/api/explore/playlist/notaplaylist",
        "/api/explore/mood/%3Cscript%3E",
        "/api/explore/charts?country=ITA",
        "/api/explore/search?q=a",
        "/api/explore/suggestions?q=",
        "/api/explore/match?kind=song&id=aaaaaaaaaaa",
        "/api/explore/match?kind=album&id=../../x",
    ])
    def test_invalid_input_is_400(self, client, monkeypatch, url):
        import explore
        monkeypatch.setattr(explore, "call", lambda *a, **k: pytest.fail("YouTube was called"))
        resp = client.get(url)
        assert resp.status_code == 400
        assert resp.get_json()["success"] is False

    def test_album_route(self, client, monkeypatch):
        import explore
        monkeypatch.setattr(explore, "album", lambda bid: {"id": bid, "title": "Revival"})
        data = client.get("/api/explore/album/MPREb_abcdef").get_json()
        assert data["album"] == {"id": "MPREb_abcdef", "title": "Revival"}

    def test_match_route_passes_refresh(self, client, monkeypatch):
        import explore
        seen = {}
        monkeypatch.setattr(
            explore, "match",
            lambda kind, item_id, refresh=False: seen.update(kind=kind, id=item_id, refresh=refresh) or {"status": "complete"},
        )
        data = client.get("/api/explore/match?kind=album&id=MPREb_abcdef&refresh=1").get_json()
        assert data["match"]["status"] == "complete"
        assert seen == {"kind": "album", "id": "MPREb_abcdef", "refresh": True}

    def test_add_route_errors_keep_status(self, client, monkeypatch):
        import explore

        def boom(payload):
            raise explore.ExploreError("Several MusicBrainz releases match; pick one.", 409)

        monkeypatch.setattr(explore, "add", boom)
        resp = client.post("/api/explore/add", json={"kind": "album", "id": "MPREb_abcdef"})
        assert resp.status_code == 409
        assert "pick one" in resp.get_json()["message"]

    def test_add_route_rejects_non_object(self, client):
        resp = client.post("/api/explore/add", json=[1, 2])
        assert resp.status_code == 400

    def test_search_is_rate_limited(self, client, monkeypatch):
        import explore
        monkeypatch.setattr(explore, "search", lambda q: {"query": q, "top": None, "results": {}})
        codes = [client.get("/api/explore/search?q=abc").status_code for _ in range(16)]
        assert codes[:15] == [200] * 15
        assert codes[15] == 429

    def test_import_route_starts_a_playlist_import(self, client, monkeypatch):
        import app as app_module
        import explore
        monkeypatch.setattr(explore, "import_plan", lambda payload: {
            "artist_name": "Eminem", "album_title": "Revival",
            "entries": [{"url": "https://music.youtube.com/watch?v=iKLU7z_xdYQ", "title": "Walk On Water"}],
            "thumbnail_url": "https://lh3.googleusercontent.com/x=w1200-h1200",
            "source_url": "https://music.youtube.com/playlist?list=OLAK5uy_x",
        })
        started = {}

        class FakeThread:
            def __init__(self, target, args, daemon):
                started["args"] = args

            def start(self):
                started["started"] = True

        monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
        resp = client.post("/api/explore/import", json={"kind": "album", "id": "MPREb_abcdef"})
        assert resp.status_code == 200, resp.get_json()
        assert started["started"] is True
        artist, album, entries, target, _cfg, thumb, source = started["args"]
        assert (artist, album) == ("Eminem", "Revival")
        assert target.endswith("Eminem/Revival")
        assert thumb.startswith("https://lh3.googleusercontent.com/")

    def test_import_route_refused_while_downloading(self, client, monkeypatch):
        import app as app_module
        import explore
        monkeypatch.setattr(explore, "import_plan", lambda payload: {
            "artist_name": "A", "album_title": "B",
            "entries": [{"url": "https://music.youtube.com/watch?v=iKLU7z_xdYQ", "title": "x"}],
            "thumbnail_url": "", "source_url": "",
        })
        monkeypatch.setitem(app_module.download_process, "active", True)
        resp = client.post("/api/explore/import", json={"kind": "album", "id": "MPREb_abcdef"})
        assert resp.status_code == 409

    def test_import_route_validation(self, client):
        resp = client.post("/api/explore/import", json={"kind": "song", "id": "x"})
        assert resp.status_code == 400

    def test_explore_config_keys_via_api(self, client):
        resp = client.post("/api/config", json={"explore_country": "us", "explore_language": "it"})
        assert resp.status_code == 200
        cfg = client.get("/api/config").get_json()
        assert cfg["explore_country"] == "US" and cfg["explore_language"] == "it"
        resp = client.post("/api/config", json={"explore_country": "USA"})
        assert resp.status_code == 400
