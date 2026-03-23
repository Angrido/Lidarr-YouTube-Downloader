# AcoustID Post-Download Verification

**Date:** 2026-03-22
**Status:** Approved
**Branch:** feature/acoustid-verification

## Summary

After downloading a track from YouTube, verify it against the expected MusicBrainz recording ID using AcoustID fingerprinting. If the fingerprint doesn't match, reject the file, ban the URL, and try the next candidate from the search results. Keep up to 10 candidates from the initial search to avoid re-searching.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Config toggle | None — active whenever AcoustID is enabled | Simpler; fingerprinting without verification is less useful |
| Unverified (no AcoustID data) | Reject and try next candidate | Strict by default |
| All candidates unverified | Accept best-scored candidate | Prevents obscure tracks from being un-downloadable |
| Architecture | Split search/download in downloader | Clean separation; processing.py drives verify-retry loop |
| UI status | Show "verifying" status | Users see why a track takes longer |
| DB recording | Only record final outcome | Rejected candidates visible via banned_urls table |
| Threshold | 0.85 (same as verify_fingerprints.py) | Proven default from existing tool |

## Architecture

### Modified Modules

**`downloader.py`** — split into search + download:

- `search_youtube_candidates(query, track_title, expected_duration_ms, skip_check, banned_urls) -> list[dict]`
  Returns ranked candidates (up to 10, capped after filtering). The existing search uses `ytsearch15:`, filters by forbidden words/duration/banned URLs, then truncates to 10. Extracts existing search + scoring logic.

- `download_youtube_candidate(candidate, output_path, progress_hook, skip_check) -> dict`
  Downloads a single candidate with client fallback. Extracts existing download logic.

- `download_track_youtube()` — kept as thin wrapper calling both functions. Backward compatible.

**`fingerprint.py`** — add verification function:

- `verify_fingerprint(filepath, expected_recording_id, acoustid_api_key, threshold=0.85) -> dict`
  Runs fpcalc, looks up AcoustID, compares against expected ID.
  Returns `{"status": "verified"|"mismatch"|"unverified", "fp_data": {...}, "matched_id": "..."}`.
  Reuses existing `_run_fpcalc` and `_lookup_acoustid` internals.

**`processing.py`** — verify-retry loop in `_process_single_track`:

The existing single-download flow is replaced with a candidate iteration loop.

### Data Flow

```
_process_single_track(idx, track):
    expected_id = track.get("foreignRecordingId")
        # Lidarr's /api/v1/track returns this field for MusicBrainz recording ID.
        # iTunes fallback tracks won't have it — verification is skipped.

    candidates = search_youtube_candidates(...)
    all_unverified = True
    best_unverified_candidate = None  # track best-scored unverified for fallback

    for candidate in candidates:
        temp_file = fresh unique temp path per attempt
        download_youtube_candidate(candidate) -> temp_file

        if acoustid_enabled and expected_id:
            tag_mp3(temp_file, track, album, cover_data)
                # Tagging before fingerprinting is necessary: fpcalc reads
                # raw audio so tags don't affect it, but we need the file
                # ready to move on success. The cost is acceptable — tag_mp3
                # only writes ID3 tags to the local file (no network calls).
                # XML metadata and artwork are NOT created here — only after
                # verification succeeds (see "accept" path below).

            track_state["status"] = "verifying"
            result = verify_fingerprint(temp_file, expected_id, api_key)

            if result.status == "verified":
                -> create XML metadata, accept file, record fp_data,
                   move to final, done
            elif result.status == "mismatch":
                all_unverified = False
                -> delete temp, ban URL, log rejection, try next
            elif result.status == "unverified":
                -> delete temp, try next (don't ban — no AcoustID data)
                if best_unverified_candidate is None:
                    best_unverified_candidate = candidate
        else:
            -> tag, create XML metadata, accept file, done
              (no verification possible)

    if exhausted all candidates:
        if all_unverified and best_unverified_candidate is not None:
            -> re-download best_unverified_candidate to a fresh temp file,
               tag, create XML metadata, accept without verification
               (fp_data fields set to empty/zero in track_downloads record)
        else:
            -> mark track as failed
            error_message = "AcoustID verification failed: no candidate
                matched expected recording <expected_id> (tried N candidates)"
```

### Track Status Flow

```
searching -> downloading -> tagging -> verifying -> [retry: downloading -> tagging -> verifying ->] ... -> done/failed
```

The "verifying" status appears while `verify_fingerprint` runs. On rejection, status cycles back to "downloading" for the next candidate.

## Ban URL Integration

### Automatic banning on fingerprint mismatch

When a candidate is rejected due to fingerprint mismatch, `processing.py` calls the existing `models.add_banned_url()` with all track context. This means:
- The ban shows up in the Logs page under "URL Banned" filter
- The ban shows up in the Downloads page track grid
- Unban buttons work as they do today

Unverified rejections (no AcoustID data) are NOT banned — the URL isn't necessarily wrong, AcoustID just doesn't have data for it.

### UI changes

No template changes needed for ban display. The existing banned URL UI handles everything.

The "verifying" track status needs a display label in the SSE rendering code, using the same style as "fingerprinting".

## Error Handling & Edge Cases

1. **Track has no `foreignRecordingId`** — Skip verification, accept download. Lidarr's `/api/v1/track` endpoint returns `foreignRecordingId` for tracks with MusicBrainz data. iTunes fallback tracks (from `get_itunes_tracks()`) won't have this field. The verification condition is `track.get("foreignRecordingId")` — missing or empty means skip.

2. **`fpcalc` not installed / AcoustID disabled** — Verification skipped entirely. Downloads work as today.

3. **AcoustID API errors (timeout, rate limit, 5xx)** — Treat as "unverified". Don't ban the URL, try next candidate. If all candidates hit API errors, all-unverified fallback accepts best-scored one.

4. **All candidates are mismatches** — Track marked as failed. Error message indicates fingerprint verification failed for all candidates.

5. **All candidates are unverified** — Re-download best-scored candidate and accept without verification.

6. **Skip/stop during verification** — Honored immediately. Clean up temp file, mark as skipped. The `"verifying"` status must be added to `stop_download()`'s skip logic alongside `"pending"`, `"searching"`, `"downloading"`.

7. **Concurrent downloads** — Verify-retry loop runs within `_process_single_track` thread. AcoustID rate limiting (`_throttle()`) currently has a TOCTOU race on the global `_last_request_time` float. As part of this feature, add a `threading.Lock` to `_throttle()` to make it thread-safe under concurrent track downloads.

8. **Re-download of previously downloaded album** — Banned URLs from verification rejections are filtered by `get_banned_urls_for_track()` at the start of each track download.

## Logging

Each verification rejection logs at INFO level:

```
AcoustID verification failed for 'Track Title':
expected=<foreignRecordingId>, got=<acoustid_recording_id>
(score=0.92). Trying next candidate (2/10).
```

## Testing

### Unit tests

**`test_fingerprint.py`** — `verify_fingerprint()`:
- Expected recording ID found with score >= 0.85 -> "verified"
- Expected recording ID not in results -> "mismatch"
- AcoustID returns empty results -> "unverified"
- AcoustID API error -> "unverified"
- Expected recording ID found but score < threshold -> "mismatch"
- No API key -> returns None (skipped)
- fpcalc not available -> returns None

**`test_downloader.py`** — split functions:
- `search_youtube_candidates()` returns ranked list, respects banned URLs, forbidden words, caps at 10
- `download_youtube_candidate()` downloads single candidate, tries client fallbacks
- `download_track_youtube()` wrapper backward compatibility

**`test_processing.py`** — verify-retry loop:
- Candidate #1 mismatches, #2 verifies -> accepts #2, bans #1
- All candidates mismatch -> track fails, all get banned
- All candidates unverified -> accepts best-scored
- Mix of mismatches and unverified -> track fails (at least one mismatch means `all_unverified=False`, so no fallback)
- Track has no foreignRecordingId -> skips verification
- AcoustID disabled -> skips verification
- Skip requested during verification -> skipped, temp cleaned

### Integration tests
- Full download flow with mocked yt-dlp and AcoustID: search -> download -> reject -> retry -> accept
- Banned URLs persist across download attempts
