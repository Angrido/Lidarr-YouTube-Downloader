"""AcoustID fingerprinting for downloaded tracks.

Runs fpcalc (chromaprint) on a downloaded MP3, looks up the fingerprint
against the AcoustID API, and returns the best matching MusicBrainz
recording metadata.
"""

import json
import logging
import shutil
import subprocess
import threading
import time

import requests

logger = logging.getLogger(__name__)

ACOUSTID_API_URL = "https://api.acoustid.org/v2/lookup"
RATE_LIMIT_INTERVAL = 0.34  # ~3 requests per second

_last_request_time = 0.0
_fpcalc_warned = False
_api_key_invalid = False
_throttle_lock = threading.Lock()


def is_fpcalc_available():
    """Check whether the fpcalc binary is on PATH."""
    return shutil.which("fpcalc") is not None


def _run_fpcalc(filepath):
    """Run fpcalc and return (duration, fingerprint) or None on failure."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            logger.warning(
                "fpcalc failed for %s: %s",
                filepath, result.stderr.strip()[:200],
            )
            return None
        data = json.loads(result.stdout)
        duration = data.get("duration")
        fingerprint = data.get("fingerprint")
        if not duration or not fingerprint:
            return None
        return duration, fingerprint
    except subprocess.TimeoutExpired:
        logger.warning("fpcalc timed out for %s", filepath)
        return None
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("fpcalc error for %s: %s", filepath, e)
        return None


def _throttle():
    """Rate limit to ~3 requests per second for AcoustID API."""
    global _last_request_time
    with _throttle_lock:
        elapsed = time.monotonic() - _last_request_time
        if elapsed < RATE_LIMIT_INTERVAL:
            time.sleep(RATE_LIMIT_INTERVAL - elapsed)
        _last_request_time = time.monotonic()


def _lookup_acoustid(api_key, duration, fingerprint):
    global _api_key_invalid
    if _api_key_invalid:
        return None
    params = {
        "client": api_key,
        "duration": int(duration),
        "fingerprint": fingerprint,
        "meta": "recordings recordingmeta releasegroups",
    }
    try:
        _throttle()
        r = requests.post(ACOUSTID_API_URL, data=params, timeout=15)
        if not r.ok:
            logger.warning(
                "AcoustID API returned %d: %s",
                r.status_code, r.text[:300],
            )
            r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            error = data.get("error", {})
            if error.get("code") == 4:
                _api_key_invalid = True
                logger.error(
                    "AcoustID API key is invalid. "
                    "Register a key at https://acoustid.org/new-application "
                    "and set it in Settings > AcoustID API Key. "
                    "Fingerprinting disabled for this session."
                )
            else:
                logger.warning("AcoustID API error: %s", error.get("message", "unknown"))
            return None
        return data.get("results", [])
    except requests.exceptions.RequestException as e:
        logger.warning("AcoustID lookup failed: %s", e)
        return None


def _extract_best_match(results):
    """Extract the best recording from AcoustID results.

    Returns dict with acoustid_fingerprint_id, acoustid_score,
    acoustid_recording_id, acoustid_recording_title, or None.
    """
    if not results:
        return None

    best_score = 0.0
    best_recording_id = None
    best_recording_title = None
    best_fingerprint_id = None

    for result in results:
        score = result.get("score", 0.0)
        fingerprint_id = result.get("id")
        for recording in result.get("recordings", []):
            if score > best_score:
                best_score = score
                best_recording_id = recording.get("id")
                best_recording_title = recording.get("title")
                best_fingerprint_id = fingerprint_id

    if not best_recording_id:
        return None

    return {
        "acoustid_fingerprint_id": best_fingerprint_id,
        "acoustid_score": round(best_score, 4),
        "acoustid_recording_id": best_recording_id,
        "acoustid_recording_title": best_recording_title or "",
    }


def verify_fingerprint(
    filepath, expected_recording_id, acoustid_api_key, threshold=0.85,
    expected_release_group_id=None, accept_score_threshold=0.98,
):
    """Verify a downloaded file matches the expected MusicBrainz recording.

    Args:
        filepath: Path to the MP3 file.
        expected_recording_id: Expected MusicBrainz recording ID.
        acoustid_api_key: AcoustID API key string.
        threshold: Minimum score for a match (default 0.85).
        expected_release_group_id: Optional MusicBrainz release-group MBID
            (Lidarr's album id). When the exact recording id isn't matched,
            a high-score AcoustID recording belonging to this release group
            is accepted — the same song often has several recording ids
            across releases, which would otherwise be rejected as a mismatch.
        accept_score_threshold: A near-perfect acoustic score is accepted
            even when neither the recording id nor the release group match
            (same track on a different release/edition). Kept strict so
            genuinely wrong tracks, which fingerprint far lower, still fail.

    Returns:
        Dict with "status" ("verified", "mismatch", "unverified"),
        "fp_data" (dict or empty), and "matched_id" (str or None).
        None if fingerprinting is unavailable or fails to run.
    """
    global _fpcalc_warned

    if not acoustid_api_key:
        return None

    if not is_fpcalc_available():
        if not _fpcalc_warned:
            logger.warning(
                "fpcalc not found — AcoustID fingerprinting disabled."
                " Install chromaprint to enable."
            )
            _fpcalc_warned = True
        return None

    fp_result = _run_fpcalc(filepath)
    if fp_result is None:
        return None

    duration, fingerprint = fp_result
    results = _lookup_acoustid(acoustid_api_key, duration, fingerprint)

    if not results:
        return {
            "status": "unverified",
            "fp_data": {},
            "matched_id": None,
        }

    # Check if expected ID appears in any high-score result
    for result in results:
        score = result.get("score", 0.0)
        if score < threshold:
            continue
        for recording in result.get("recordings", []):
            if recording.get("id") == expected_recording_id:
                fp_data = {
                    "acoustid_fingerprint_id": result.get("id"),
                    "acoustid_score": round(score, 4),
                    "acoustid_recording_id": expected_recording_id,
                    "acoustid_recording_title": (
                        recording.get("title") or ""
                    ),
                }
                return {
                    "status": "verified",
                    "fp_data": fp_data,
                    "matched_id": expected_recording_id,
                }

    # Exact recording id not matched: fall back to release-group match.
    # AcoustID often maps the audio to a different recording id than the
    # one Lidarr holds, even though it's the same song on the same album.
    if expected_release_group_id:
        for result in results:
            score = result.get("score", 0.0)
            if score < threshold:
                continue
            for recording in result.get("recordings", []):
                rgs = recording.get("releasegroups") or []
                if any(
                    rg.get("id") == expected_release_group_id for rg in rgs
                ):
                    fp_data = {
                        "acoustid_fingerprint_id": result.get("id"),
                        "acoustid_score": round(score, 4),
                        "acoustid_recording_id": recording.get("id") or "",
                        "acoustid_recording_title": (
                            recording.get("title") or ""
                        ),
                    }
                    return {
                        "status": "verified",
                        "fp_data": fp_data,
                        "matched_id": recording.get("id"),
                    }

    # Neither the recording id nor the release group matched. A near-perfect
    # acoustic score is still almost always the same track on another
    # release, so accept it above a strict threshold rather than discarding
    # good audio over an MBID mismatch (issue #58).
    best = _extract_best_match(results)
    if best and best.get("acoustid_score", 0.0) >= accept_score_threshold:
        logger.info(
            "AcoustID accepted on score %.2f despite MBID mismatch (%s)",
            best.get("acoustid_score", 0.0),
            best.get("acoustid_recording_id", ""),
        )
        return {
            "status": "verified",
            "fp_data": best,
            "matched_id": best.get("acoustid_recording_id"),
        }

    matched_id = best["acoustid_recording_id"] if best else None
    return {
        "status": "mismatch",
        "fp_data": best or {},
        "matched_id": matched_id,
    }


def fingerprint_track(filepath, acoustid_api_key):
    """Fingerprint an audio file and look up its AcoustID metadata.

    Args:
        filepath: Path to the MP3 file.
        acoustid_api_key: AcoustID API key string.

    Returns:
        Dict with acoustid_fingerprint_id, acoustid_score,
        acoustid_recording_id, acoustid_recording_title on success.
        None if fingerprinting is unavailable, fails, or no match found.
    """
    global _fpcalc_warned

    if not acoustid_api_key:
        return None

    if not is_fpcalc_available():
        if not _fpcalc_warned:
            logger.warning(
                "fpcalc not found — AcoustID fingerprinting disabled."
                " Install chromaprint to enable."
            )
            _fpcalc_warned = True
        return None

    fp_result = _run_fpcalc(filepath)
    if fp_result is None:
        return None

    duration, fingerprint = fp_result
    results = _lookup_acoustid(acoustid_api_key, duration, fingerprint)
    if results is None:
        return None

    match = _extract_best_match(results)
    if match:
        logger.info(
            "AcoustID match for %s: recording=%s score=%.2f title='%s'",
            filepath, match["acoustid_recording_id"],
            match["acoustid_score"], match["acoustid_recording_title"],
        )
    else:
        logger.info("AcoustID: no recordings matched for %s", filepath)

    return match
