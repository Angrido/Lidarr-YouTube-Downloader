import json

import pytest

from fingerprint import (
    _extract_best_match,
    _run_fpcalc,
    fingerprint_track,
    is_fpcalc_available,
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
