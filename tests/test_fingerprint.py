import json
import threading
import time

import pytest

from fingerprint import (
    _extract_best_match,
    _run_fpcalc,
    fingerprint_track,
    is_fpcalc_available,
    verify_fingerprint,
)


def test_is_fpcalc_available():
    result = is_fpcalc_available()
    assert isinstance(result, bool)


def test_extract_best_match_empty():
    assert _extract_best_match([]) is None
    assert _extract_best_match(None) is None


def test_extract_best_match_single_result():
    results = [{
        "id": "fp-id-123",
        "score": 0.95,
        "recordings": [{
            "id": "rec-id-456",
            "title": "Test Song",
        }],
    }]
    match = _extract_best_match(results)
    assert match is not None
    assert match["acoustid_fingerprint_id"] == "fp-id-123"
    assert match["acoustid_score"] == 0.95
    assert match["acoustid_recording_id"] == "rec-id-456"
    assert match["acoustid_recording_title"] == "Test Song"


def test_extract_best_match_picks_highest_score():
    results = [
        {
            "id": "fp-low",
            "score": 0.5,
            "recordings": [{"id": "rec-low", "title": "Low"}],
        },
        {
            "id": "fp-high",
            "score": 0.99,
            "recordings": [{"id": "rec-high", "title": "High"}],
        },
    ]
    match = _extract_best_match(results)
    assert match["acoustid_recording_id"] == "rec-high"
    assert match["acoustid_score"] == 0.99


def test_extract_best_match_no_recordings():
    results = [{"id": "fp-1", "score": 0.9, "recordings": []}]
    assert _extract_best_match(results) is None


def test_extract_best_match_missing_title():
    results = [{
        "id": "fp-1",
        "score": 0.8,
        "recordings": [{"id": "rec-1"}],
    }]
    match = _extract_best_match(results)
    assert match["acoustid_recording_title"] == ""


def test_run_fpcalc_nonexistent_file():
    result = _run_fpcalc("/nonexistent/file.mp3")
    assert result is None


def test_fingerprint_track_no_api_key():
    result = fingerprint_track("/some/file.mp3", "")
    assert result is None


def test_fingerprint_track_no_fpcalc(monkeypatch):
    monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: False)
    monkeypatch.setattr("fingerprint._fpcalc_warned", False)
    result = fingerprint_track("/some/file.mp3", "test-key")
    assert result is None


def test_fingerprint_track_fpcalc_fails(monkeypatch):
    monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
    monkeypatch.setattr("fingerprint._run_fpcalc", lambda f: None)
    result = fingerprint_track("/some/file.mp3", "test-key")
    assert result is None


def test_fingerprint_track_lookup_fails(monkeypatch):
    monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
    monkeypatch.setattr(
        "fingerprint._run_fpcalc", lambda f: (180, "AQAA...")
    )
    monkeypatch.setattr(
        "fingerprint._lookup_acoustid", lambda k, d, fp: None
    )
    result = fingerprint_track("/some/file.mp3", "test-key")
    assert result is None


def test_fingerprint_track_success(monkeypatch):
    monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
    monkeypatch.setattr(
        "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
    )
    monkeypatch.setattr(
        "fingerprint._lookup_acoustid",
        lambda k, d, fp: [{
            "id": "fp-abc",
            "score": 0.92,
            "recordings": [{
                "id": "rec-xyz",
                "title": "My Song",
            }],
        }],
    )
    result = fingerprint_track("/some/file.mp3", "test-key")
    assert result is not None
    assert result["acoustid_fingerprint_id"] == "fp-abc"
    assert result["acoustid_score"] == 0.92
    assert result["acoustid_recording_id"] == "rec-xyz"
    assert result["acoustid_recording_title"] == "My Song"


def test_throttle_is_thread_safe():
    """Concurrent _throttle() calls don't overlap within RATE_LIMIT_INTERVAL."""
    from fingerprint import _throttle, RATE_LIMIT_INTERVAL

    timestamps = []
    lock = threading.Lock()

    def record_time():
        _throttle()
        with lock:
            timestamps.append(time.monotonic())

    threads = [threading.Thread(target=record_time) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    timestamps.sort()
    for i in range(1, len(timestamps)):
        gap = timestamps[i] - timestamps[i - 1]
        assert gap >= RATE_LIMIT_INTERVAL * 0.9, (
            f"Gap {gap:.3f}s < {RATE_LIMIT_INTERVAL * 0.9:.3f}s"
        )


def test_fingerprint_track_no_match(monkeypatch):
    monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
    monkeypatch.setattr(
        "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
    )
    monkeypatch.setattr(
        "fingerprint._lookup_acoustid",
        lambda k, d, fp: [{"id": "fp-1", "score": 0.1, "recordings": []}],
    )
    result = fingerprint_track("/some/file.mp3", "test-key")
    assert result is None


class TestVerifyFingerprint:
    """verify_fingerprint compares AcoustID result to expected recording."""

    def test_verified_when_expected_id_matches(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: [{
                "id": "fp-1",
                "score": 0.95,
                "recordings": [{"id": "expected-rec", "title": "Song"}],
            }],
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result["status"] == "verified"
        assert result["fp_data"]["acoustid_recording_id"] == "expected-rec"
        assert result["fp_data"]["acoustid_score"] == 0.95
        assert result["matched_id"] == "expected-rec"

    def test_mismatch_when_different_id_returned(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: [{
                "id": "fp-1",
                "score": 0.95,
                "recordings": [{"id": "other-rec", "title": "Other"}],
            }],
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result["status"] == "mismatch"
        assert result["matched_id"] == "other-rec"

    def test_mismatch_when_score_below_threshold(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: [{
                "id": "fp-1",
                "score": 0.50,
                "recordings": [{"id": "expected-rec", "title": "Song"}],
            }],
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key", threshold=0.85,
        )
        assert result["status"] == "mismatch"

    def test_unverified_when_empty_results(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: [],
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result["status"] == "unverified"
        assert result["matched_id"] is None

    def test_unverified_when_api_error(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: None,
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result["status"] == "unverified"

    def test_returns_none_when_no_api_key(self):
        result = verify_fingerprint("/file.mp3", "expected-rec", "")
        assert result is None

    def test_returns_none_when_no_fpcalc(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: False)
        monkeypatch.setattr("fingerprint._fpcalc_warned", False)
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result is None

    def test_returns_none_when_fpcalc_fails(self, monkeypatch):
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr("fingerprint._run_fpcalc", lambda f: None)
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result is None

    def test_verified_checks_all_recordings(self, monkeypatch):
        """Expected ID in second recording of a high-score result."""
        monkeypatch.setattr("fingerprint.is_fpcalc_available", lambda: True)
        monkeypatch.setattr(
            "fingerprint._run_fpcalc", lambda f: (200, "AQAA...")
        )
        monkeypatch.setattr(
            "fingerprint._lookup_acoustid",
            lambda k, d, fp: [{
                "id": "fp-1",
                "score": 0.92,
                "recordings": [
                    {"id": "other-rec", "title": "Other"},
                    {"id": "expected-rec", "title": "Song"},
                ],
            }],
        )
        result = verify_fingerprint(
            "/file.mp3", "expected-rec", "test-key",
        )
        assert result["status"] == "verified"
