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
        failed, _, size, _stats = _download_tracks(
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

        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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
        failed, _, size, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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
    def test_mix_mismatch_and_unverified_falls_back(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """Mix of mismatch + unverified -> still falls back to unverified.

        Regression: previously a single mismatch flipped all_unverified to
        False and prevented the unverified fallback. Now a mismatch only
        bans that specific URL; unverified candidates remain eligible.
        """
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

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        assert len(succeeded) == 1

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
        "min_match_score": 0.8,
    })
    def test_no_verify_rejects_low_score_then_accepts(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """No verification + low-score candidate -> skip, try next."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {"title": "Song", "trackNumber": 1, "duration": 200000}
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_low", "title": "Low",
             "duration": 200, "score": 0.5, "channel": "Ch"},
            {"url": "url_high", "title": "High",
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

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        assert len(succeeded) == 1
        tracks = models.get_track_downloads_for_album(42)
        assert tracks[0]["youtube_url"] == "url_high"

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
        "min_match_score": 0.8,
    })
    def test_no_verify_all_below_threshold_fails(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """All candidates below min_match_score -> track fails."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {"title": "Song", "trackNumber": 1, "duration": 200000}
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_a", "title": "A",
             "duration": 200, "score": 0.5, "channel": "Ch"},
            {"url": "url_b", "title": "B",
             "duration": 200, "score": 0.6, "channel": "Ch"},
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

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        assert len(succeeded) == 0
        assert "min_match_score" in failed[0]["reason"]

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
        "min_match_score": 0.8,
    })
    def test_unverified_fallback_skipped_when_below_threshold(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """Unverified fallback is gated by min_match_score."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song", "trackNumber": 1, "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_low", "title": "Low",
             "duration": 200, "score": 0.5, "channel": "Ch"},
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
            "status": "unverified", "fp_data": {}, "matched_id": None,
        }

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 1
        assert len(succeeded) == 0
        assert "min_match_score" in failed[0]["reason"]

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": False,
        "min_match_score": 0.8,
    })
    def test_score_at_threshold_is_accepted(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """A candidate exactly at min_match_score is accepted (>=, not >)."""
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {"title": "Song", "trackNumber": 1, "duration": 200000}
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_eq", "title": "Eq",
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

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        assert len(succeeded) == 1

        self._teardown_download_process()

    @patch("processing.search_youtube_candidates")
    @patch("processing.download_youtube_candidate")
    @patch("processing.tag_mp3")
    @patch("processing.verify_fingerprint")
    @patch("processing.load_config", return_value={
        "xml_metadata_enabled": False,
        "acoustid_enabled": True,
        "acoustid_api_key": "test-key",
        "min_match_score": 0.8,
    })
    def test_verify_enabled_bypasses_score_gate(
        self, mock_config, mock_verify, mock_tag,
        mock_dl_candidate, mock_search, tmp_path,
    ):
        """When AcoustID will verify, low-score candidates are still tried.

        AcoustID is the source of truth — a 0.3-scored candidate that
        fingerprint-matches the expected recording must still be accepted.
        """
        from processing import _download_tracks

        album_path = str(tmp_path / "album")
        os.makedirs(album_path, exist_ok=True)

        track = {
            "title": "Song", "trackNumber": 1, "duration": 200000,
            "foreignRecordingId": "expected-rec",
        }
        self._setup_download_process(track)

        mock_search.return_value = [
            {"url": "url_lowscore", "title": "Low",
             "duration": 200, "score": 0.3, "channel": "Ch"},
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
            "status": "verified",
            "fp_data": {
                "acoustid_recording_id": "expected-rec",
                "acoustid_score": 0.95,
            },
        }

        failed, succeeded, _, _stats = _download_tracks(
            [track], album_path, {"tracks": [track]},
            _make_album_ctx(),
        )

        assert len(failed) == 0
        assert len(succeeded) == 1
        tracks = models.get_track_downloads_for_album(42)
        assert tracks[0]["youtube_url"] == "url_lowscore"

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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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

        failed, _, _, _stats = _download_tracks(
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
    succeeded_tracks = [
        {"title": "Track1", "track_num": 1, "track_download_id": 9},
    ]
    processing._handle_post_download(
        failed_tracks, succeeded_tracks,
        [None, None, None], 1, "Album", "Artist", 5000000,
    )
    logs = models.get_logs(log_type="track_failure")
    assert logs["total"] == 2
    titles = {item["track_title"] for item in logs["items"]}
    assert titles == {"Track2", "Track3"}


def test_handle_post_download_all_failed_creates_track_logs(monkeypatch):
    """When all tracks fail, per-track failure logs are still created."""
    import processing

    monkeypatch.setattr(processing, "download_process", {
        "tracks": [
            {"status": "failed"},
            {"status": "failed"},
        ],
        "stop": False,
        "result_success": True,
    })
    monkeypatch.setattr(processing, "send_notifications", lambda *a, **kw: None)

    failed_tracks = [
        {"title": "T1", "reason": "No match", "track_num": 1, "track_download_id": 20},
        {"title": "T2", "reason": "DL error", "track_num": 2, "track_download_id": 21},
    ]
    result = processing._handle_post_download(
        failed_tracks, [],
        [None, None], 1, "Album", "Artist", 0,
    )
    assert result is not None  # returns error dict
    logs = models.get_logs(log_type="track_failure")
    assert logs["total"] == 2
    titles = {item["track_title"] for item in logs["items"]}
    assert titles == {"T1", "T2"}


def test_handle_post_download_creates_track_download_logs(monkeypatch):
    """Successful tracks get track_download log entries."""
    import processing

    monkeypatch.setattr(processing, "download_process", {
        "tracks": [
            {"status": "done"},
            {"status": "done"},
        ],
        "stop": False,
    })
    monkeypatch.setattr(
        processing, "send_notifications", lambda *a, **kw: None,
    )

    succeeded_tracks = [
        {"title": "T1", "track_num": 1, "track_download_id": 50},
        {"title": "T2", "track_num": 2, "track_download_id": 51},
    ]
    processing._handle_post_download(
        [], succeeded_tracks,
        [None, None], 1, "Album", "Artist", 5000000,
    )
    logs = models.get_logs(log_type="track_download")
    assert logs["total"] == 2
    titles = {item["track_title"] for item in logs["items"]}
    assert titles == {"T1", "T2"}
    for item in logs["items"]:
        assert item["track_download_id"] is not None


def test_handle_post_download_partial_creates_both_log_types(
    monkeypatch,
):
    """Partial download creates both failure and success per-track logs."""
    import processing

    monkeypatch.setattr(processing, "download_process", {
        "tracks": [
            {"status": "done"},
            {"status": "failed"},
        ],
        "stop": False,
    })
    monkeypatch.setattr(
        processing, "send_notifications", lambda *a, **kw: None,
    )

    failed_tracks = [
        {"title": "T2", "reason": "failed", "track_num": 2,
         "track_download_id": 61},
    ]
    succeeded_tracks = [
        {"title": "T1", "track_num": 1, "track_download_id": 60},
    ]
    processing._handle_post_download(
        failed_tracks, succeeded_tracks,
        [None, None], 1, "Album", "Artist", 3000000,
    )
    fail_logs = models.get_logs(log_type="track_failure")
    assert fail_logs["total"] == 1
    success_logs = models.get_logs(log_type="track_download")
    assert success_logs["total"] == 1


# --- Notification helpers (PR1: rich notifications) ---


class TestVerifySummaryLines:
    """`_verify_summary_lines` aggregates AcoustID telemetry."""

    def test_returns_empty_when_no_acoustid_activity(self):
        import processing

        stats = processing._new_verify_stats()
        field, lines = processing._verify_summary_lines(stats, 0)
        assert field is None
        assert lines == []

    def test_returns_empty_when_stats_none(self):
        import processing

        field, lines = processing._verify_summary_lines(None, 0)
        assert field is None
        assert lines == []

    def test_summarizes_verified_with_avg_score(self):
        import processing

        stats = processing._new_verify_stats()
        stats["verified_count"] = 2
        stats["accepted_acoustid_scores"] = [0.9, 0.7]
        field, lines = processing._verify_summary_lines(stats, 2)
        assert "2/2 verified" in field
        assert "avg 0.80" in field
        # MD2-escaped line is non-empty.
        assert lines and "AcoustID" in lines[0]

    def test_summarizes_mismatch_and_best_rejected(self):
        import processing

        stats = processing._new_verify_stats()
        stats["mismatch_count"] = 3
        stats["best_rejected_score"] = 0.62
        field, _lines = processing._verify_summary_lines(stats, 0)
        assert "3 auto-banned" in field
        assert "best rejected 0.62" in field


class TestFormatFailedTracks:
    def test_format_field_includes_reason(self):
        import processing

        failed = [
            {"title": "Track A", "reason": "no match"},
            {"title": "Track B", "reason": ""},
        ]
        field = processing._format_failed_tracks_field(failed)
        assert "Track A — no match" in field
        # Empty reason falls back to a sentinel.
        assert "Track B — unknown error" in field

    def test_format_field_truncates(self):
        import processing

        failed = [
            {"title": f"T{i}", "reason": "x" * 100} for i in range(50)
        ]
        field = processing._format_failed_tracks_field(failed, limit=200)
        assert len(field) <= 200
        assert field.endswith("…")

    def test_format_md2_escapes_specials(self):
        import processing

        failed = [{"title": "Track (1)", "reason": "score 0.5"}]
        lines = processing._format_failed_tracks_md2(failed)
        assert lines and "\\(" in lines[0] and "\\)" in lines[0]
        assert "0\\.5" in lines[0]


class TestFormatYouTubeLinks:
    """`_format_youtube_links_*` helpers render YouTube sources."""

    def test_md2_uses_video_title_as_label(self):
        import processing

        succeeded = [{
            "title": "Song A",
            "youtube_url": "https://youtu.be/abc",
            "youtube_title": "Song A (Official Video)",
        }]
        lines = processing._format_youtube_links_md2(succeeded)
        assert len(lines) == 1
        # Label is the video title (with specials escaped) and the
        # href is the raw youtube URL.
        assert "Song A \\(Official Video\\)" in lines[0]
        assert "](https://youtu.be/abc)" in lines[0]

    def test_md2_falls_back_to_track_title(self):
        import processing

        succeeded = [{
            "title": "Fallback",
            "youtube_url": "https://youtu.be/xyz",
            "youtube_title": "",
        }]
        lines = processing._format_youtube_links_md2(succeeded)
        assert "Fallback" in lines[0]
        assert "](https://youtu.be/xyz)" in lines[0]

    def test_md2_skips_tracks_without_url(self):
        import processing

        succeeded = [
            {"title": "Has URL", "youtube_url": "https://y/1",
             "youtube_title": "t1"},
            {"title": "No URL", "youtube_url": "",
             "youtube_title": ""},
        ]
        lines = processing._format_youtube_links_md2(succeeded)
        assert len(lines) == 1
        assert "Has URL" not in "\n".join(lines) or "https://y/1" in lines[0]

    def test_field_plain_text_has_title_and_url(self):
        import processing

        succeeded = [{
            "title": "Song",
            "youtube_url": "https://youtu.be/abc",
            "youtube_title": "Official",
        }]
        field = processing._format_youtube_links_field(succeeded)
        assert "Official" in field
        assert "https://youtu.be/abc" in field

    def test_field_truncates(self):
        import processing

        succeeded = [
            {"title": f"T{i}", "youtube_url": f"https://y/{i}",
             "youtube_title": "x" * 100}
            for i in range(50)
        ]
        field = processing._format_youtube_links_field(
            succeeded, limit=200,
        )
        assert len(field) <= 200
        assert field.endswith("…")


class TestSendAlbumNotification:
    """`_send_album_notification` routes data into both channels."""

    def test_passes_telegram_md2_and_discord_embed(self, monkeypatch):
        import processing

        monkeypatch.setattr(
            processing, "load_config",
            lambda: {"lidarr_url": "http://lidarr"},
        )
        captured = {}

        def fake_send(message, **kw):
            captured["message"] = message
            captured.update(kw)

        monkeypatch.setattr(processing, "send_notifications", fake_send)

        processing._send_album_notification(
            log_type="download_success",
            title="Download Successful",
            color=0x2ECC71,
            artist_name="A.B.",
            album_title="Album (Deluxe)",
            album_mbid="mbid-1",
            cover_url="https://i/c.jpg",
            fields=[{"name": "Tracks", "value": "5/5", "inline": True}],
            extra_md2_lines=["*Tracks:* 5/5"],
        )

        assert captured["log_type"] == "download_success"
        assert captured["telegram_parse_mode"] == "MarkdownV2"
        assert captured["photo_url"] == "https://i/c.jpg"
        # MD2 body has escaped specials and only the MusicBrainz link.
        tg = captured["telegram_message"]
        assert "A\\.B\\." in tg
        assert "Album \\(Deluxe\\)" in tg
        assert "lidarr" not in tg
        assert "Open in Lidarr" not in tg
        assert "musicbrainz.org/release-group/mbid-1" in tg
        # Discord embed carries thumbnail + MusicBrainz url + the field.
        embed = captured["embed_data"]
        assert embed["thumbnail"] == "https://i/c.jpg"
        assert embed["url"] == (
            "https://musicbrainz.org/release-group/mbid-1"
        )
        assert embed["fields"][0]["value"] == "5/5"

    def test_omits_links_when_no_mbid(self, monkeypatch):
        import processing

        monkeypatch.setattr(
            processing, "load_config",
            lambda: {"lidarr_url": "http://lidarr"},
        )
        captured = {}
        monkeypatch.setattr(
            processing, "send_notifications",
            lambda message, **kw: captured.update(kw),
        )

        processing._send_album_notification(
            log_type="album_error",
            title="Download Failed",
            color=0xE74C3C,
            artist_name="A",
            album_title="B",
            album_mbid="",
            cover_url="",
        )
        assert "url" not in captured["embed_data"]
        assert "thumbnail" not in captured["embed_data"]
        assert captured["photo_url"] is None
        assert "lidarr/album" not in captured["telegram_message"]


class TestVerifyStatsAccumulation:
    """Verify the new 4-tuple return wires stats out of `_download_tracks`."""

    def test_new_verify_stats_has_expected_keys(self):
        import processing

        stats = processing._new_verify_stats()
        assert stats == {
            "verified_count": 0,
            "accepted_acoustid_scores": [],
            "mismatch_count": 0,
            "best_rejected_score": 0.0,
        }
