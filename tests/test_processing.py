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


def _make_album_ctx(**overrides):
    """Build a default album_ctx dict, overriding any keys."""
    ctx = {
        "artist_name": "Artist",
        "album_title": "Album",
        "album_id": 42,
        "album_mbid": "mbid",
        "artist_mbid": "artist_mbid",
        "cover_data": None,
        "cover_url": "",
        "lidarr_album_path": "",
    }
    ctx.update(overrides)
    return ctx


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

        album_ctx = _make_album_ctx(
            cover_url="http://cover.jpg",
            lidarr_album_path="/music/a",
        )
        failed, size = _download_tracks(
            [track], album_path, album, album_ctx,
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

        failed, size = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 0
        assert tracks[0]["error_message"] == "No suitable match"


class TestTrackStateModel:
    """download_process tracks list and TrackSkippedException."""

    def test_download_process_has_tracks_list(self):
        from processing import download_process
        assert "tracks" in download_process
        assert isinstance(download_process["tracks"], list)
        assert download_process["current_track_index"] == -1

    def test_download_process_no_legacy_fields(self):
        from processing import download_process
        assert "progress" not in download_process
        assert "current_track_title" not in download_process

    def test_track_skipped_exception_exists(self):
        from processing import TrackSkippedException
        assert issubclass(TrackSkippedException, Exception)

    def test_update_progress_sets_track_fields(self):
        from processing import download_process, update_progress
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = 0
        update_progress({
            "status": "downloading",
            "_percent_str": " 45.2% ",
            "_speed_str": " 2.4MiB/s ",
        })
        track = download_process["tracks"][0]
        assert track["progress_percent"] == "45.2%"
        assert track["progress_speed"] == "2.4MiB/s"
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    def test_update_progress_raises_on_skip_flag(self):
        from processing import (
            TrackSkippedException, download_process, update_progress,
        )
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": True},
        ]
        download_process["current_track_index"] = 0
        with pytest.raises(TrackSkippedException):
            update_progress({"status": "downloading",
                             "_percent_str": "10%", "_speed_str": "1MiB/s"})
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
