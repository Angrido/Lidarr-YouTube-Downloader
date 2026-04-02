"""Tests for processing module."""

import os
from unittest.mock import patch

import pytest

import db
import models
from models import CandidateOutcome


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

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_success_records_track_download(
        self, mock_config, mock_tag, mock_dl_candidate, mock_search,
        tmp_path,
    ):
        from processing import _download_tracks, download_process

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Test Track",
            "trackNumber": 1,
            "duration": 240000,
        }
        album = {"tracks": [track]}

        mock_search.return_value = [
            {"url": "https://youtube.com/watch?v=abc",
             "title": "Artist - Test Track", "duration": 240,
             "score": 0.92, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        download_process["stop"] = False
        download_process["tracks"] = [
            {"track_title": track["title"],
             "track_number": int(track["trackNumber"]),
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1

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
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_failure_records_track_download(
        self, mock_config, mock_dl_candidate, mock_search, tmp_path,
    ):
        from processing import _download_tracks, download_process

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Failed Track",
            "trackNumber": 1,
            "duration": 240000,
        }

        mock_search.return_value = [
            {"url": "url_1", "title": "Failed Track",
             "duration": 240, "score": 0.7, "channel": "Ch"},
        ]
        mock_dl_candidate.return_value = {
            "success": False,
            "error_message": "No suitable match",
        }

        download_process["stop"] = False
        download_process["tracks"] = [
            {"track_title": track["title"],
             "track_number": int(track["trackNumber"]),
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1

        failed, size = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 0
        download_process["tracks"] = []
        download_process["current_track_index"] = -1


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

    def test_progress_hook_sets_track_fields(self):
        from processing import _make_progress_hook, download_process
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = 0
        hook = _make_progress_hook(0)
        hook({
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

    def test_progress_hook_sets_downloading_status(self):
        from processing import _make_progress_hook, download_process
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "searching",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = 0
        hook = _make_progress_hook(0)
        hook({
            "status": "downloading",
            "_percent_str": "10%",
            "_speed_str": "1MiB/s",
        })
        assert download_process["tracks"][0]["status"] == "downloading"
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    def test_progress_hook_raises_on_skip_flag(self):
        from processing import (
            TrackSkippedException, _make_progress_hook, download_process,
        )
        download_process["tracks"] = [
            {"track_title": "T1", "track_number": 1, "status": "downloading",
             "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": True},
        ]
        download_process["current_track_index"] = 0
        hook = _make_progress_hook(0)
        with pytest.raises(TrackSkippedException):
            hook({"status": "downloading",
                  "_percent_str": "10%", "_speed_str": "1MiB/s"})
        # cleanup
        download_process["tracks"] = []
        download_process["current_track_index"] = -1


class TestTrackStateTransitions:
    """_download_tracks populates tracks list and handles skip."""

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_tracks_state_transitions(
        self, mock_config, mock_tag, mock_dl_candidate, mock_search,
        tmp_path,
    ):
        from processing import _download_tracks, download_process

        mock_search.return_value = [
            {"url": "https://youtube.com/watch?v=abc",
             "title": "Title", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
            {"title": "Track 2", "trackNumber": 2, "duration": 180000},
        ]
        download_process["tracks"] = [
            {"track_title": t["title"], "track_number": int(t["trackNumber"]),
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False}
            for t in tracks
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        assert len(failed) == 0
        assert download_process["tracks"][0]["status"] == "done"
        assert download_process["tracks"][1]["status"] == "done"
        assert download_process["tracks"][0]["youtube_url"] == (
            "https://youtube.com/watch?v=abc"
        )
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.search_youtube_candidates")
    def test_pre_skipped_track_never_downloads(self, mock_search, tmp_path):
        from processing import _download_tracks, download_process
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        # skip=True is set before search runs, so search returns []
        mock_search.return_value = []
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": True},
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        assert download_process["tracks"][0]["status"] == "skipped"
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.search_youtube_candidates")
    def test_stop_all_still_stops(self, mock_search, tmp_path):
        from processing import _download_tracks, download_process
        download_process["stop"] = True
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        mock_search.return_value = []
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        assert download_process["tracks"][0]["status"] == "skipped"
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["stop"] = False

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    def test_skip_during_download_sets_skipped(
        self, mock_dl_candidate, mock_search, tmp_path,
    ):
        from processing import (
            TrackSkippedException, _download_tracks, download_process,
        )
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        mock_search.return_value = [
            {"url": "url_1", "title": "Track 1", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]
        mock_dl_candidate.side_effect = TrackSkippedException()
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        assert len(failed) == 0
        assert download_process["tracks"][0]["status"] == "skipped"
        download_process["tracks"] = []
        download_process["current_track_index"] = -1

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    def test_skipped_result_sets_skipped(
        self, mock_dl_candidate, mock_search, tmp_path,
    ):
        from processing import _download_tracks, download_process
        album_path = str(tmp_path / "album")
        os.makedirs(album_path)
        tracks = [
            {"title": "Track 1", "trackNumber": 1, "duration": 200000},
        ]
        mock_search.return_value = [
            {"url": "url_1", "title": "Track 1", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]
        mock_dl_candidate.return_value = {"skipped": True}
        download_process["tracks"] = [
            {"track_title": "Track 1", "track_number": 1,
             "status": "pending", "youtube_url": "", "youtube_title": "",
             "progress_percent": "", "progress_speed": "",
             "error_message": "", "skip": False},
        ]
        download_process["current_track_index"] = -1
        download_process["stop"] = False
        failed, size = _download_tracks(
            tracks, album_path, {}, _make_album_ctx(),
        )
        assert len(failed) == 0
        assert download_process["tracks"][0]["status"] == "skipped"
        download_process["tracks"] = []
        download_process["current_track_index"] = -1


class TestVerifyRetryLoop:
    """_download_tracks with AcoustID verification retry loop."""

    def _setup_download_process(self, track):
        from processing import download_process
        download_process["stop"] = False
        download_process["album_id"] = 42
        download_process["tracks"] = [
            {
                "track_title": track["title"],
                "track_number": int(track.get("trackNumber", 1)),
                "status": "pending",
                "youtube_url": "",
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            },
        ]
        download_process["current_track_index"] = -1

    def _teardown_download_process(self):
        from processing import download_process
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = None

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_mismatch_then_verified_accepts_second(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """First candidate mismatches, second verifies."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_a", "title": "Wrong", "duration": 200,
             "score": 0.9, "channel": "Ch"},
            {"url": "url_b", "title": "Right", "duration": 200,
             "score": 0.8, "channel": "Ch"},
        ]

        call_count = [0]

        def fake_download(candidate, output_path, **kwargs):
            call_count[0] += 1
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        mock_verify.side_effect = [
            {
                "status": "mismatch",
                "fp_data": {"acoustid_recording_id": "wrong-rec",
                            "acoustid_score": 0.9},
                "matched_id": "wrong-rec",
            },
            {
                "status": "verified",
                "fp_data": {
                    "acoustid_fingerprint_id": "fp-1",
                    "acoustid_score": 0.92,
                    "acoustid_recording_id": "expected-rec",
                    "acoustid_recording_title": "Song",
                },
                "matched_id": "expected-rec",
            },
        ]

        album = {"tracks": [track]}
        failed, size = _download_tracks(
            [track], album_path, album, _make_album_ctx(),
        )

        assert len(failed) == 0
        assert call_count[0] == 2
        # First URL should be banned
        banned = models.get_banned_urls_for_track(42, "Song")
        assert "url_a" in banned
        # Track download recorded with second URL
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["youtube_url"] == "url_b"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_all_mismatch_fails(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """All candidates mismatch -> track fails, all banned."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": f"url_{i}", "title": f"Wrong {i}",
             "duration": 200, "score": 0.9 - i * 0.1, "channel": "Ch"}
            for i in range(3)
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        mock_verify.return_value = {
            "status": "mismatch",
            "fp_data": {},
            "matched_id": "other-rec",
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        banned = models.get_banned_urls_for_track(42, "Song")
        assert len(banned) == 3

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_all_unverified_accepts_best(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """All candidates unverified -> fallback accepts best-scored."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Obscure Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_best", "title": "Best",
             "duration": 200, "score": 0.95, "channel": "Ch"},
            {"url": "url_other", "title": "Other",
             "duration": 200, "score": 0.8, "channel": "Ch"},
        ]

        dl_count = [0]

        def fake_download(candidate, output_path, **kwargs):
            dl_count[0] += 1
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        mock_verify.return_value = {
            "status": "unverified",
            "fp_data": {},
            "matched_id": None,
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        # 2 initial downloads + 1 re-download of best candidate
        assert dl_count[0] == 3
        # No URLs should be banned (unverified != mismatch)
        banned = models.get_banned_urls_for_track(42, "Obscure Song")
        assert len(banned) == 0
        # Track recorded with best-scored URL (re-downloaded)
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["youtube_url"] == "url_best"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_no_foreign_recording_id_skips_verification(
        self, mock_config, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """Track without foreignRecordingId skips verification."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "No ID Track",
            "trackNumber": 1,
            "duration": 200000,
            # No foreignRecordingId
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_1", "title": "Track",
             "duration": 200, "score": 0.9, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 1

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_acoustid_disabled_skips_verification(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """AcoustID disabled -> no verification."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "rec-123",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_1", "title": "Song",
             "duration": 200, "score": 0.9, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        mock_verify.assert_not_called()

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_verify_returns_none_accepts_without_verification(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """verify_fingerprint returns None (fpcalc unavailable) -> accept."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_1", "title": "Song",
             "duration": 200, "score": 0.9, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        mock_verify.return_value = None

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        tracks = models.get_track_downloads_for_album(42)
        assert len(tracks) == 1
        assert tracks[0]["success"] == 1
        assert tracks[0]["youtube_url"] == "url_1"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_mix_mismatch_and_unverified_fails(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """Mix of mismatch + unverified -> fails (not all_unverified)."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_a", "title": "A",
             "duration": 200, "score": 0.9, "channel": "Ch"},
            {"url": "url_b", "title": "B",
             "duration": 200, "score": 0.8, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download

        mock_verify.side_effect = [
            {"status": "mismatch", "fp_data": {},
             "matched_id": "wrong"},
            {"status": "unverified", "fp_data": {},
             "matched_id": None},
        ]

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1

        self._teardown_download_process()


class TestVerifyRetryIntegration:
    """End-to-end: search -> download -> reject -> retry -> accept."""

    def _setup_download_process(self, track):
        from processing import download_process
        download_process["stop"] = False
        download_process["album_id"] = 42
        download_process["tracks"] = [
            {
                "track_title": track["title"],
                "track_number": int(track.get("trackNumber", 1)),
                "status": "pending",
                "youtube_url": "",
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            },
        ]
        download_process["current_track_index"] = -1

    def _teardown_download_process(self):
        from processing import download_process
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = None

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_banned_urls_persist_across_downloads(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """URLs banned in first download attempt are filtered in second."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song",
            "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }

        # First download: url_a mismatches, url_b verifies
        self._setup_download_process(track)
        mock_search.return_value = [
            {"url": "url_a", "title": "Wrong",
             "duration": 200, "score": 0.9, "channel": "Ch"},
            {"url": "url_b", "title": "Right",
             "duration": 200, "score": 0.8, "channel": "Ch"},
        ]

        def fake_download(candidate, output_path, **kwargs):
            open(output_path + ".mp3", "w").close()
            return {
                "success": True,
                "youtube_url": candidate["url"],
                "youtube_title": candidate["title"],
                "match_score": candidate["score"],
                "duration_seconds": candidate["duration"],
            }
        mock_dl_candidate.side_effect = fake_download
        mock_verify.side_effect = [
            {"status": "mismatch", "fp_data": {},
             "matched_id": "wrong"},
            {"status": "verified",
             "fp_data": {
                 "acoustid_fingerprint_id": "fp",
                 "acoustid_score": 0.9,
                 "acoustid_recording_id": "expected-rec",
                 "acoustid_recording_title": "Song",
             },
             "matched_id": "expected-rec"},
        ]

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )
        assert len(failed) == 0

        # url_a should now be in banned_urls
        banned = models.get_banned_urls_for_track(42, "Song")
        assert "url_a" in banned

        self._teardown_download_process()


class TestCandidateAttemptCapture:
    """Verify candidate_attempts are flushed to DB during downloads."""

    def _setup_download_process(self, tracks_list):
        from processing import download_process
        download_process["stop"] = False
        download_process["album_id"] = 42
        download_process["tracks"] = [
            {
                "track_title": t["title"],
                "track_number": int(t.get("trackNumber", 1)),
                "status": "pending",
                "youtube_url": "",
                "youtube_title": "",
                "progress_percent": "",
                "progress_speed": "",
                "error_message": "",
                "skip": False,
            }
            for t in tracks_list
        ]
        download_process["current_track_index"] = -1

    def _teardown_download_process(self):
        from processing import download_process
        download_process["tracks"] = []
        download_process["current_track_index"] = -1
        download_process["album_id"] = None

    def _fake_download(self, candidate, output_path, **kwargs):
        open(output_path + ".mp3", "w").close()
        return {
            "success": True,
            "youtube_url": candidate["url"],
            "youtube_title": candidate["title"],
            "match_score": candidate["score"],
            "duration_seconds": candidate["duration"],
        }

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_accepted_no_verify_captures_attempt(
        self, mock_config, mock_tag, mock_dl_candidate,
        mock_search, tmp_path,
    ):
        """AcoustID disabled -> ACCEPTED_NO_VERIFY attempt recorded."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Track", "trackNumber": 1,
            "duration": 200000,
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": "url_1", "title": "Track", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]
        mock_dl_candidate.side_effect = self._fake_download

        _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        rows = models.get_track_downloads_for_album(42)
        assert len(rows) == 1
        td_id = rows[0]["id"]
        attempts = models.get_candidate_attempts(td_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == CandidateOutcome.ACCEPTED_NO_VERIFY
        assert attempts[0]["youtube_url"] == "url_1"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_mismatch_then_verified_captures_both(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """Mismatch + verified -> 2 attempts with correct outcomes."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Song", "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": "url_a", "title": "Wrong", "duration": 200,
             "score": 0.9, "channel": "Ch"},
            {"url": "url_b", "title": "Right", "duration": 200,
             "score": 0.8, "channel": "Ch"},
        ]
        mock_dl_candidate.side_effect = self._fake_download
        mock_verify.side_effect = [
            {
                "status": "mismatch",
                "fp_data": {
                    "acoustid_recording_id": "wrong-rec",
                    "acoustid_recording_title": "Other",
                    "acoustid_score": 0.88,
                },
                "matched_id": "wrong-rec",
            },
            {
                "status": "verified",
                "fp_data": {
                    "acoustid_fingerprint_id": "fp",
                    "acoustid_score": 0.92,
                    "acoustid_recording_id": "expected-rec",
                    "acoustid_recording_title": "Song",
                },
                "matched_id": "expected-rec",
            },
        ]

        _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        rows = models.get_track_downloads_for_album(42)
        td_id = rows[0]["id"]
        attempts = models.get_candidate_attempts(td_id)
        assert len(attempts) == 2
        assert attempts[0]["outcome"] == CandidateOutcome.MISMATCH
        assert attempts[0]["youtube_url"] == "url_a"
        assert attempts[0]["acoustid_matched_id"] == "wrong-rec"
        assert attempts[0]["acoustid_score"] == pytest.approx(0.88)
        assert attempts[1]["outcome"] == CandidateOutcome.VERIFIED
        assert attempts[1]["youtube_url"] == "url_b"
        assert attempts[1]["expected_recording_id"] == "expected-rec"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_all_mismatch_captures_attempts_on_failure(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """All mismatches -> track fails, attempts still flushed."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Song", "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": f"url_{i}", "title": f"Wrong {i}",
             "duration": 200, "score": 0.9 - i * 0.1, "channel": "Ch"}
            for i in range(3)
        ]
        mock_dl_candidate.side_effect = self._fake_download
        mock_verify.return_value = {
            "status": "mismatch",
            "fp_data": {},
            "matched_id": "other-rec",
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        rows = models.get_track_downloads_for_album(42)
        td_id = rows[0]["id"]
        attempts = models.get_candidate_attempts(td_id)
        assert len(attempts) == 3
        assert all(
            a["outcome"] == CandidateOutcome.MISMATCH
            for a in attempts
        )

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
    })
    def test_download_failure_captures_attempt(
        self, mock_config, mock_dl_candidate,
        mock_search, tmp_path,
    ):
        """Download failure -> DOWNLOAD_FAILED attempt recorded."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Track", "trackNumber": 1,
            "duration": 200000,
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": "url_1", "title": "Track", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]
        mock_dl_candidate.return_value = {
            "success": False,
            "error_message": "403 Forbidden",
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        rows = models.get_track_downloads_for_album(42)
        td_id = rows[0]["id"]
        attempts = models.get_candidate_attempts(td_id)
        assert len(attempts) == 1
        assert attempts[0]["outcome"] == CandidateOutcome.DOWNLOAD_FAILED
        assert attempts[0]["youtube_url"] == "url_1"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_unverified_fallback_captures_all_attempts(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """All unverified -> fallback accepted with all attempts."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Song", "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": "url_best", "title": "Best", "duration": 200,
             "score": 0.95, "channel": "Ch"},
            {"url": "url_other", "title": "Other", "duration": 200,
             "score": 0.8, "channel": "Ch"},
        ]
        mock_dl_candidate.side_effect = self._fake_download
        mock_verify.return_value = {
            "status": "unverified",
            "fp_data": {},
            "matched_id": None,
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        rows = models.get_track_downloads_for_album(42)
        td_id = rows[0]["id"]
        attempts = models.get_candidate_attempts(td_id)
        # 2 unverified + 1 fallback re-download accepted
        assert len(attempts) == 3
        outcomes = [a["outcome"] for a in attempts]
        assert outcomes.count(CandidateOutcome.UNVERIFIED) == 2
        assert outcomes.count(
            CandidateOutcome.ACCEPTED_UNVERIFIED_FALLBACK,
        ) == 1

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
    })
    def test_failed_track_includes_track_download_id(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """failed_tracks entries include track_download_id."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path)

        track = {
            "title": "Song", "trackNumber": 1,
            "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process([track])
        mock_search.return_value = [
            {"url": "url_1", "title": "Wrong", "duration": 200,
             "score": 0.9, "channel": "Ch"},
        ]
        mock_dl_candidate.side_effect = self._fake_download
        mock_verify.return_value = {
            "status": "mismatch",
            "fp_data": {},
            "matched_id": "other-rec",
        }

        failed, _ = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        assert failed[0]["track_download_id"] is not None
        assert isinstance(failed[0]["track_download_id"], int)

        self._teardown_download_process()


def test_handle_post_download_creates_track_failure_logs(monkeypatch):
    """After a partial download, each failed track gets its own log entry."""
    import processing

    monkeypatch.setattr(processing, "download_process", {
        "tracks": [
            {"status": "done"},
            {"status": "failed"},
            {"status": "failed"},
        ],
        "stop": False,
    })
    monkeypatch.setattr(processing, "send_notifications", lambda *a, **kw: None)

    failed_tracks = [
        {"title": "Track2", "reason": "AcoustID failed", "track_num": 2, "track_download_id": 10},
        {"title": "Track3", "reason": "Download failed", "track_num": 3, "track_download_id": 11},
    ]
    processing._handle_post_download(
        failed_tracks, [None, None, None], 1, "Album", "Artist", 5000000,
    )
    logs = models.get_logs(log_type="track_failure")
    assert logs["total"] == 2
    titles = {item["track_title"] for item in logs["items"]}
    assert titles == {"Track2", "Track3"}
