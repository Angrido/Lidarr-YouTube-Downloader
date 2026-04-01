import time

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


# --- Track Downloads ---


def test_add_track_download():
    models.add_track_download(
        album_id=1, album_title="Album1", artist_name="Artist1",
        track_title="Track1", track_number=1, success=True,
        error_message="", youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="Artist1 - Track1", match_score=0.92,
        duration_seconds=240, album_path="/downloads/a",
        lidarr_album_path="/music/a", cover_url="http://cover.jpg",
    )
    tracks = models.get_track_downloads_for_album(1)
    assert len(tracks) == 1
    assert tracks[0]["track_title"] == "Track1"
    assert tracks[0]["youtube_url"] == "https://youtube.com/watch?v=abc"
    assert tracks[0]["success"] == 1


def test_add_track_download_with_acoustid():
    models.add_track_download(
        album_id=1, album_title="Album1", artist_name="Artist1",
        track_title="Track1", track_number=1, success=True,
        error_message="", youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="Artist1 - Track1", match_score=0.92,
        duration_seconds=240, album_path="/downloads/a",
        lidarr_album_path="/music/a", cover_url="http://cover.jpg",
        acoustid_fingerprint_id="fp-123",
        acoustid_score=0.95,
        acoustid_recording_id="rec-456",
        acoustid_recording_title="Track One",
    )
    tracks = models.get_track_downloads_for_album(1)
    assert len(tracks) == 1
    assert tracks[0]["acoustid_fingerprint_id"] == "fp-123"
    assert tracks[0]["acoustid_score"] == 0.95
    assert tracks[0]["acoustid_recording_id"] == "rec-456"
    assert tracks[0]["acoustid_recording_title"] == "Track One"


def test_add_track_download_acoustid_defaults():
    models.add_track_download(
        album_id=2, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    tracks = models.get_track_downloads_for_album(2)
    assert tracks[0]["acoustid_fingerprint_id"] == ""
    assert tracks[0]["acoustid_score"] == 0.0
    assert tracks[0]["acoustid_recording_id"] == ""
    assert tracks[0]["acoustid_recording_title"] == ""


def test_get_track_downloads_for_album_ordered_newest_first():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    time.sleep(0.01)
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T2", track_number=2, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    tracks = models.get_track_downloads_for_album(1)
    assert tracks[0]["track_title"] == "T2"


def test_get_album_history_grouped():
    models.add_track_download(
        album_id=1, album_title="Album1", artist_name="Artist1",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="",
        cover_url="http://cover1.jpg",
    )
    models.add_track_download(
        album_id=1, album_title="Album1", artist_name="Artist1",
        track_title="T2", track_number=2, success=False,
        error_message="no match", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="",
        cover_url="http://cover1.jpg",
    )
    result = models.get_album_history(page=1, per_page=50)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["album_id"] == 1
    assert item["success_count"] == 1
    assert item["fail_count"] == 1
    assert item["total_count"] == 2


def test_get_album_history_pagination():
    for i in range(3):
        models.add_track_download(
            album_id=i, album_title=f"Album{i}", artist_name="A",
            track_title="T1", track_number=1, success=True,
            error_message="", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
    result = models.get_album_history(page=1, per_page=2)
    assert result["total"] == 3
    assert result["pages"] == 2
    assert len(result["items"]) == 2


def test_get_failed_tracks_for_retry():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="Ar",
        track_title="T1", track_number=1, success=False,
        error_message="no match", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="/dl/a", lidarr_album_path="/music/a",
        cover_url="http://cover.jpg",
    )
    models.add_track_download(
        album_id=1, album_title="A", artist_name="Ar",
        track_title="T2", track_number=2, success=True,
        error_message="", youtube_url="http://yt/1", youtube_title="vid",
        match_score=0.9, duration_seconds=200,
        album_path="/dl/a", lidarr_album_path="/music/a",
        cover_url="http://cover.jpg",
    )
    result = models.get_failed_tracks_for_retry(1)
    assert result["album_id"] == 1
    assert result["album_path"] == "/dl/a"
    assert len(result["failed_tracks"]) == 1
    assert result["failed_tracks"][0]["title"] == "T1"


def test_get_failed_tracks_for_retry_latest_success_hides_old_failure():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="Ar",
        track_title="T1", track_number=1, success=False,
        error_message="no match", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="/dl/a", lidarr_album_path="/music/a",
        cover_url="",
    )
    time.sleep(0.01)
    models.add_track_download(
        album_id=1, album_title="A", artist_name="Ar",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="http://yt/1", youtube_title="vid",
        match_score=0.9, duration_seconds=200,
        album_path="/dl/a", lidarr_album_path="/music/a",
        cover_url="",
    )
    result = models.get_failed_tracks_for_retry(1)
    assert len(result["failed_tracks"]) == 0


def test_get_history_count_today():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T2", track_number=2, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    models.add_track_download(
        album_id=2, album_title="B", artist_name="A",
        track_title="T1", track_number=1, success=False,
        error_message="fail", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    assert models.get_history_count_today() == 1


def test_get_history_album_ids_since():
    now = time.time()
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    models.add_track_download(
        album_id=2, album_title="B", artist_name="A",
        track_title="T1", track_number=1, success=False,
        error_message="fail", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    result = models.get_history_album_ids_since(now - 10)
    assert result == {1}


def test_clear_history():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    models.clear_history()
    result = models.get_album_history(page=1, per_page=50)
    assert result["total"] == 0


# --- Logs (updated -- no failed_tracks) ---


def test_add_log_no_failed_tracks():
    log_id = models.add_log(
        "download_success", 1, "Album", "Artist", details="ok"
    )
    assert log_id is not None
    result = models.get_logs(page=1, per_page=50)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["type"] == "download_success"
    assert "failed_tracks" not in item


def test_add_log_track_level_id():
    log_id = models.add_log(
        "track_success", 1, "Album", "Artist",
        details="ok", track_number=3,
    )
    assert "_1_3" in log_id


def test_get_logs_filter_by_type():
    models.add_log("download_success", 1, "A", "A", details="ok")
    models.add_log("album_error", 2, "B", "B", details="fail")
    result = models.get_logs(page=1, per_page=50, log_type="album_error")
    assert result["total"] == 1
    assert result["items"][0]["type"] == "album_error"


def test_delete_log():
    models.add_log("download_success", 1, "A", "A")
    log_id = models.get_logs()["items"][0]["id"]
    assert models.delete_log(log_id) is True
    assert models.get_logs()["total"] == 0


def test_delete_log_not_found():
    assert models.delete_log("nonexistent") is False


def test_clear_logs():
    models.add_log("download_success", 1, "A", "A")
    models.clear_logs()
    assert models.get_logs()["total"] == 0


def test_get_logs_db_size():
    models.add_log("download_success", 1, "A", "A", details="some text")
    size = models.get_logs_db_size()
    assert size > 0


def test_get_logs_pagination():
    for i in range(3):
        models.add_log("album_error", i, f"A{i}", "Artist")
    page1 = models.get_logs(page=1, per_page=2)
    assert len(page1["items"]) == 2
    assert page1["total"] == 3
    assert page1["pages"] == 2
    page2 = models.get_logs(page=2, per_page=2)
    assert len(page2["items"]) == 1


def test_get_logs_filtered_pagination():
    for i in range(3):
        models.add_log("album_error", i, f"A{i}", "Artist")
    models.add_log("download_success", 99, "B", "Artist")
    page1 = models.get_logs(page=1, per_page=2, log_type="album_error")
    assert len(page1["items"]) == 2
    assert page1["total"] == 3
    page2 = models.get_logs(page=2, per_page=2, log_type="album_error")
    assert len(page2["items"]) == 1


def test_get_banned_urls_pagination():
    for i in range(3):
        models.add_banned_url(
            youtube_url=f"https://youtube.com/watch?v={i:011d}",
            youtube_title=f"V{i}", album_id=i, album_title=f"A{i}",
            artist_name="A", track_title="T", track_number=1,
        )
    page1 = models.get_banned_urls(page=1, per_page=2)
    assert len(page1["items"]) == 2
    assert page1["total"] == 3
    assert page1["pages"] == 2
    page2 = models.get_banned_urls(page=2, per_page=2)
    assert len(page2["items"]) == 1


# --- Queue (unchanged) ---


def test_enqueue_and_get_queue():
    models.enqueue_album(10)
    models.enqueue_album(20)
    queue = models.get_queue()
    assert len(queue) == 2
    assert queue[0]["album_id"] == 10
    assert queue[1]["album_id"] == 20


def test_enqueue_duplicate_ignored():
    models.enqueue_album(10)
    models.enqueue_album(10)
    assert len(models.get_queue()) == 1


def test_dequeue_album():
    models.enqueue_album(10)
    models.enqueue_album(20)
    models.dequeue_album(10)
    queue = models.get_queue()
    assert len(queue) == 1
    assert queue[0]["album_id"] == 20


def test_set_queue_status():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    queue = models.get_queue()
    assert queue[0]["status"] == "downloading"


def test_set_queue_status_invalid():
    models.enqueue_album(10)
    with pytest.raises(ValueError):
        models.set_queue_status(10, "invalid_status")


def test_reset_downloading_to_queued():
    models.enqueue_album(10)
    models.set_queue_status(10, models.QUEUE_STATUS_DOWNLOADING)
    models.reset_downloading_to_queued()
    queue = models.get_queue()
    assert queue[0]["status"] == "queued"


def test_clear_queue():
    models.enqueue_album(10)
    models.clear_queue()
    assert len(models.get_queue()) == 0


def test_get_latest_download_album_id_empty():
    assert models.get_latest_download_album_id() is None


def test_get_latest_download_album_id_returns_most_recent():
    models.add_track_download(
        album_id=10, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    time.sleep(0.01)
    models.add_track_download(
        album_id=20, album_title="B", artist_name="B",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    assert models.get_latest_download_album_id() == 20


def test_get_history_album_ids_since_empty():
    result = models.get_history_album_ids_since(time.time() - 10)
    assert result == set()


def test_get_history_album_ids_since_future_timestamp():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    result = models.get_history_album_ids_since(time.time() + 3600)
    assert result == set()


# --- Banned URLs ---


def test_add_banned_url():
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="Wrong Video",
        album_id=1,
        album_title="Album1",
        artist_name="Artist1",
        track_title="Track1",
        track_number=1,
    )
    result = models.get_banned_urls(page=1, per_page=50)
    assert result["total"] == 1
    item = result["items"][0]
    assert item["youtube_url"] == "https://youtube.com/watch?v=abc"
    assert item["track_title"] == "Track1"
    assert item["album_id"] == 1


def test_add_banned_url_duplicate_ignored():
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="Wrong Video",
        album_id=1, album_title="A", artist_name="A",
        track_title="Track1", track_number=1,
    )
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="Wrong Video",
        album_id=1, album_title="A", artist_name="A",
        track_title="Track1", track_number=1,
    )
    result = models.get_banned_urls(page=1, per_page=50)
    assert result["total"] == 1


def test_get_banned_urls_for_track():
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="V1", album_id=1, album_title="A",
        artist_name="A", track_title="Track1", track_number=1,
    )
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=def",
        youtube_title="V2", album_id=1, album_title="A",
        artist_name="A", track_title="Track1", track_number=1,
    )
    # Different track - should not appear
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="V1", album_id=1, album_title="A",
        artist_name="A", track_title="Track2", track_number=2,
    )
    banned = models.get_banned_urls_for_track(1, "Track1")
    assert banned == {
        "https://youtube.com/watch?v=abc",
        "https://youtube.com/watch?v=def",
    }


def test_remove_banned_url():
    models.add_banned_url(
        youtube_url="https://youtube.com/watch?v=abc",
        youtube_title="V1", album_id=1, album_title="A",
        artist_name="A", track_title="Track1", track_number=1,
    )
    result = models.get_banned_urls(page=1, per_page=50)
    ban_id = result["items"][0]["id"]
    assert models.remove_banned_url(ban_id) is True
    assert models.get_banned_urls(page=1, per_page=50)["total"] == 0


def test_remove_banned_url_nonexistent():
    assert models.remove_banned_url(9999) is False


def test_mark_track_deleted():
    models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="https://yt/abc",
        youtube_title="vid", match_score=0.9,
        duration_seconds=200, album_path="/dl/a",
        lidarr_album_path="/music/a", cover_url="",
    )
    tracks = models.get_track_downloads_for_album(1)
    track_id = tracks[0]["id"]
    track_data = models.mark_track_deleted(track_id)
    assert track_data is not None
    assert track_data["album_path"] == "/dl/a"
    assert track_data["track_title"] == "T1"
    # Verify it's now marked deleted
    tracks = models.get_track_downloads_for_album(1)
    assert tracks[0]["deleted"] == 1


def test_mark_track_deleted_nonexistent():
    assert models.mark_track_deleted(9999) is None


# --- CandidateOutcome Enum ---


class TestCandidateOutcome:

    def test_enum_values(self):
        assert CandidateOutcome.VERIFIED == "verified"
        assert CandidateOutcome.MISMATCH == "mismatch"
        assert CandidateOutcome.UNVERIFIED == "unverified"
        assert CandidateOutcome.DOWNLOAD_FAILED == "download_failed"
        assert CandidateOutcome.ACCEPTED_NO_VERIFY == "accepted_no_verify"
        assert CandidateOutcome.ACCEPTED_UNVERIFIED_FALLBACK == (
            "accepted_unverified_fallback"
        )

    def test_is_str_subclass(self):
        outcome = CandidateOutcome.VERIFIED
        assert isinstance(outcome, str)


# --- add_track_download returns ID ---


def test_add_track_download_returns_id():
    row_id = models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=True,
        error_message="", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    assert isinstance(row_id, int)
    assert row_id > 0


# --- add_log with track fields ---


def test_add_log_with_track_fields():
    td_id = models.add_track_download(
        album_id=1, album_title="A", artist_name="A",
        track_title="T1", track_number=1, success=False,
        error_message="fail", youtube_url="", youtube_title="",
        match_score=0.0, duration_seconds=0,
        album_path="", lidarr_album_path="", cover_url="",
    )
    log_id = models.add_log(
        "track_failure", 1, "A", "A",
        details="no match", track_number=1,
        track_title="T1", track_download_id=td_id,
    )
    result = models.get_logs(page=1, per_page=50)
    item = result["items"][0]
    assert item["id"] == log_id
    assert item["track_title"] == "T1"
    assert item["track_number"] == 1
    assert item["track_download_id"] == td_id


# --- Candidate Attempts ---


class TestCandidateAttempts:

    def test_flush_candidate_attempts_inserts_rows(self):
        td_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=True,
            error_message="", youtube_url="https://yt/final",
            youtube_title="Final", match_score=0.95,
            duration_seconds=240, album_path="/dl",
            lidarr_album_path="/music", cover_url="",
        )
        attempts = [
            {
                "youtube_url": "https://yt/1",
                "youtube_title": "Candidate 1",
                "match_score": 0.8,
                "duration_seconds": 230,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "rec-wrong",
                "acoustid_matched_title": "Wrong Song",
                "acoustid_score": 0.4,
                "expected_recording_id": "rec-expected",
                "error_message": "AcoustID mismatch",
                "timestamp": 1000.0,
            },
            {
                "youtube_url": "https://yt/2",
                "youtube_title": "Candidate 2",
                "match_score": 0.95,
                "duration_seconds": 240,
                "outcome": CandidateOutcome.VERIFIED,
                "acoustid_matched_id": "rec-expected",
                "acoustid_matched_title": "Correct Song",
                "acoustid_score": 0.92,
                "expected_recording_id": "rec-expected",
                "error_message": "",
                "timestamp": 2000.0,
            },
        ]
        models.flush_candidate_attempts(td_id, attempts)
        rows = models.get_candidate_attempts(td_id)
        assert len(rows) == 2
        assert rows[0]["youtube_url"] == "https://yt/1"
        assert rows[0]["outcome"] == "mismatch"
        assert rows[1]["youtube_url"] == "https://yt/2"
        assert rows[1]["outcome"] == "verified"
        assert rows[1]["acoustid_score"] == 0.92

    def test_flush_empty_list_is_noop(self):
        td_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=True,
            error_message="", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        models.flush_candidate_attempts(td_id, [])
        rows = models.get_candidate_attempts(td_id)
        assert len(rows) == 0

    def test_get_candidate_attempts_ordered_by_timestamp(self):
        td_id = models.add_track_download(
            album_id=1, album_title="A", artist_name="A",
            track_title="T1", track_number=1, success=True,
            error_message="", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0,
            album_path="", lidarr_album_path="", cover_url="",
        )
        attempts = [
            {
                "youtube_url": "https://yt/later",
                "youtube_title": "Later",
                "match_score": 0.9,
                "duration_seconds": 200,
                "outcome": CandidateOutcome.VERIFIED,
                "acoustid_matched_id": "",
                "acoustid_matched_title": "",
                "acoustid_score": 0.0,
                "expected_recording_id": "",
                "error_message": "",
                "timestamp": 3000.0,
            },
            {
                "youtube_url": "https://yt/earlier",
                "youtube_title": "Earlier",
                "match_score": 0.7,
                "duration_seconds": 180,
                "outcome": CandidateOutcome.MISMATCH,
                "acoustid_matched_id": "",
                "acoustid_matched_title": "",
                "acoustid_score": 0.0,
                "expected_recording_id": "",
                "error_message": "",
                "timestamp": 1000.0,
            },
        ]
        models.flush_candidate_attempts(td_id, attempts)
        rows = models.get_candidate_attempts(td_id)
        assert rows[0]["youtube_title"] == "Earlier"
        assert rows[1]["youtube_title"] == "Later"
