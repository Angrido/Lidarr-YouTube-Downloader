"""Tests for processing module."""

import os
from unittest.mock import patch

import pytest

import db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    yield db_path
    db.close_db()


class TestDownloadTracks:
    """_download_tracks calls add_track_download per track."""

    @patch("processing.download_track_youtube")
    @patch("processing.tag_mp3")
    @patch("processing.create_xml_metadata")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
    })
    def test_success_records_track_download(
        self, mock_config, mock_xml, mock_tag, mock_dl, tmp_path,
    ):
        import models
        from processing import _download_tracks, download_process

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Test Track",
            "trackNumber": 1,
            "duration": 240000,
        }
        album = {"tracks": [track]}

        def create_mp3(*args, **kwargs):
            temp_path = args[1]
            open(temp_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": "https://youtube.com/watch?v=abc",
                "youtube_title": "Artist - Test Track",
                "match_score": 0.92,
                "duration_seconds": 240,
            }
        mock_dl.side_effect = create_mp3

        download_process["stop"] = False
        download_process["progress"] = {
            "current": 0, "total": 0, "overall_percent": 0,
        }
        download_process["current_track_title"] = ""

        failed, size = _download_tracks(
            [track], album_path, "Artist", "Album",
            album, "mbid", "artist_mbid", None,
            album_id=42, cover_url="http://cover.jpg",
            lidarr_album_path="/music/a",
        )

        assert len(failed) == 0
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 1
        assert tracks[0]["youtube_url"] == (
            "https://youtube.com/watch?v=abc"
        )

    @patch("processing.download_track_youtube")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
    })
    def test_failure_records_track_download(
        self, mock_config, mock_dl, tmp_path,
    ):
        import models
        from processing import _download_tracks, download_process

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Failed Track",
            "trackNumber": 1,
            "duration": 240000,
        }

        mock_dl.return_value = {
            "success": False,
            "error_message": "No suitable match",
        }

        download_process["stop"] = False
        download_process["progress"] = {
            "current": 0, "total": 0, "overall_percent": 0,
        }
        download_process["current_track_title"] = ""

        failed, size = _download_tracks(
            [track], album_path, "Artist", "Album",
            {"tracks": [track]}, "mbid", "artist_mbid", None,
            album_id=42, cover_url="",
            lidarr_album_path="",
        )

        assert len(failed) == 1
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 0
        assert tracks[0]["error_message"] == "No suitable match"
