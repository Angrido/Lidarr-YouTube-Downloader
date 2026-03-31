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
    @patch("app.tag_mp3")
    def test_successful_download(
        self, mock_tag, mock_perms, mock_album, mock_lidarr,
        mock_fp, client, tmp_path, monkeypatch,
    ):
        dl_path = str(tmp_path / "downloads")
        monkeypatch.setattr("app.DOWNLOAD_DIR", dl_path)
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
            mp3_path = outtmpl + ".mp3"
            os.makedirs(os.path.dirname(mp3_path), exist_ok=True)
            with open(mp3_path, "wb") as f:
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
    @patch("app.tag_mp3")
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
    @patch("app.tag_mp3")
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

    def test_validate_target_path_blocks_escape(self, client, tmp_path):
        from app import _validate_target_path

        config = {"lidarr_path": str(tmp_path / "music")}
        assert not _validate_target_path("/etc/evil", config)
