# Manual Testing Checklist

Tests that cannot be fully automated and should be verified after changes to the webapp UI or API.

## Prerequisites

```bash
docker compose up -d --build
```

App runs at `http://localhost:5000`. Requires a reachable Lidarr instance configured via `.env`.

## API Smoke Tests

Run after any changes to route handlers or error handling:

- [ ] `GET /api/stats` — returns `{"downloaded_today": N, "in_queue": N}`
- [ ] `GET /api/ytdlp/version` — returns `{"version": "..."}`
- [ ] `GET /api/test-connection` — returns `{"status": "success", "lidarr_version": "..."}` when Lidarr is reachable
- [ ] `GET /api/test-connection` — returns `{"status": "error", "message": "..."}` when Lidarr is unreachable
- [ ] `GET /api/missing-albums` — returns JSON array (may be empty)
- [ ] `GET /api/download/history` — returns paginated response with grouped albums (each item has `success_count`, `fail_count`, `total_count`)
- [ ] `GET /api/download/history/<album_id>/tracks` — returns track-level download records
- [ ] `GET /api/download/failed` — returns failed tracks inferred from most recent download batch
- [ ] `GET /api/logs` — returns paginated response
- [ ] `GET /api/config` — returns config dict with all expected keys
- [ ] `POST /api/download/queue` with `{"album_id": N}` — returns `{"success": true, "queue_length": N}`
- [ ] `POST /api/download/queue` with empty JSON `{}` — does not crash (returns 200)
- [ ] `POST /api/download/queue/bulk` with `{"album_ids": [1,2,3]}` — returns added count
- [ ] `POST /api/download/queue/bulk` with `{"album_ids": "not a list"}` — returns 400
- [ ] `POST /api/download/skip-track` with `{"track_index": 0}` when no active download — returns 409
- [ ] `POST /api/download/skip-track` with `{"track_index": 0}` during active download — returns 200 and sets skip flag
- [ ] `GET /api/download/queue/<album_id>/tracks` — returns track list with title, track_number, has_file

## UI Page Load Tests

Run after any changes to templates or static assets:

- [ ] `GET /` (index) — loads without errors, shows missing albums section
- [ ] `GET /downloads` — loads download queue and history sections
- [ ] `GET /settings` — loads configuration form with current values
- [ ] `GET /logs` — loads log entries table

## UI Interaction Tests

Run with `agent-browser` after changes to frontend JavaScript or template logic:

- [ ] **Settings page**: Change scheduler interval, save, reload — value persists
- [ ] **Settings page**: Toggle scheduler enabled, save, reload — toggle state persists
- [ ] **Settings page**: Add/remove forbidden words — list updates correctly
- [ ] **Downloads page**: Queue an album from missing list — appears in queue
- [ ] **Downloads page**: Remove album from queue — disappears from queue
- [ ] **Downloads page**: Clear queue — all items removed
- [ ] **Downloads page**: Pagination controls work for history and queue
- [ ] **Downloads page**: History shows album rows with color-coded track count badges
- [ ] **Downloads page**: Click album row to expand — shows track detail grid
- [ ] **Downloads page**: Expanded tracks show YouTube links, match scores, durations
- [ ] **Downloads page**: Failed tracks shown with red background and error message
- [ ] **Downloads page**: Multiple attempt indicator shows "(N attempts)" on re-downloaded tracks
- [ ] **Logs page**: Dismiss a log entry — entry removed
- [ ] **Logs page**: Clear all logs — all entries removed
- [ ] **Logs page**: Filter by log type — only matching entries shown
- [ ] **Logs page**: Pagination controls work
- [ ] **Logs page**: All log types render as compact single-line rows (not cards)
- [ ] **Logs page**: "Track Failures" filter shows per-track failure rows with candidate sub-rows
- [ ] **Logs page**: Track failure row shows track title, album, artist, track number, timestamp
- [ ] **Logs page**: Each candidate sub-row shows outcome badge, YouTube title, match score, duration
- [ ] **Logs page**: Mismatch candidates show AcoustID matched title and score
- [ ] **Logs page**: Unverified candidates show "AcoustID: no results"
- [ ] **Logs page**: Download failed candidates show error message
- [ ] **Logs page**: Banned mismatch candidates show inline Unban button
- [ ] **Logs page**: Clicking Unban on candidate refreshes logs (ban removed)
- [ ] **Index page**: Click download on a missing album — album queues and download starts
- [ ] **Index page**: Click stop download — active download stops

## Track-Level Downloads UI Tests

Run after changes to Current Download or Download Queue sections:

- [ ] **Current Download**: Shows all tracks in album with per-track status during active download
- [ ] **Current Download**: Active track has highlighted background, progress bar, and speed
- [ ] **Current Download**: Done tracks show green checkmark and YouTube link
- [ ] **Current Download**: Pending tracks are dimmed (lower opacity)
- [ ] **Current Download**: Failed tracks show red background and error message on hover
- [ ] **Current Download**: Overall progress bar reflects (done + failed + skipped) / total
- [ ] **Skip active track**: Click Skip on downloading track — stops within a few seconds, next track starts
- [ ] **Skip pending track**: Click Skip on pending track — marked as skipped, never downloads
- [ ] **Skip searching track**: Click Skip while searching YouTube — search aborts, moves to next
- [ ] **Stop All**: Stops current track and clears entire queue (existing behavior preserved)
- [ ] **Download Queue**: Position 1 auto-expanded showing track list
- [ ] **Download Queue**: Other positions collapsed with expand chevron
- [ ] **Download Queue**: Click chevron to expand/collapse track list
- [ ] **Download Queue**: Track count badge shown on each queue item
- [ ] **Download Queue**: Tracks with existing files shown with strikethrough
- [ ] **Download Queue**: Expansion state preserved across SSE updates (1-second rebuilds)
- [ ] **Download Queue**: Remove button still works (doesn't toggle expansion)

## Delete Track + Ban URL Tests

Run after changes to track deletion, URL banning, or related UI:

- [ ] **Downloads page**: Expand album in history — successful tracks show trash icon
- [ ] **Downloads page**: Failed tracks and deleted tracks do NOT show trash icon
- [ ] **Downloads page**: Click trash icon — confirmation dialog appears with correct file path
- [ ] **Downloads page**: Dialog shows XML sidecar note
- [ ] **Downloads page**: Ban checkbox is checked by default
- [ ] **Downloads page**: Confirm delete (with ban) — file removed, track shows strikethrough + "deleted" badge
- [ ] **Downloads page**: Confirm delete (without ban) — file removed, track deleted but no ban created
- [ ] **Downloads page**: Deleted track has dimmed YouTube link, no trash icon
- [ ] **Downloads page**: Cancel button closes dialog without action
- [ ] **Downloads page**: Click outside modal closes dialog
- [ ] **Logs page**: "URL Banned" option appears in filter dropdown
- [ ] **Logs page**: Select "URL Banned" filter — shows banned URL cards with orange accent
- [ ] **Logs page**: Banned URL card shows YouTube link, track context, and Unban button
- [ ] **Logs page**: Click Unban — card slides out and disappears
- [ ] **Logs page**: After unban, switching away and back to "URL Banned" filter confirms it's gone
- [ ] **Re-download**: Queue previously downloaded album — deleted track re-downloads with different URL (banned one skipped)
- [ ] **Re-download**: Unban a URL, re-download — previously banned URL is now a candidate again

## Manual Track URL Download Tests

Run after changes to track expansion, manual URL download, or related UI on the Home page:

- [ ] **Index page**: Each album card/list/table row shows a "Tracks" expand button
- [ ] **Index page**: Click "Tracks" button — expands to show track list with loading spinner
- [ ] **Index page**: Track list shows track numbers, titles, and status icons (green check / red x)
- [ ] **Index page**: Tracks with files show green check icon and dimmed title
- [ ] **Index page**: Missing tracks show red x icon and YouTube URL input field
- [ ] **Index page**: Click "Tracks" again — collapses the track panel
- [ ] **Index page**: Re-expand — cached tracks load instantly (no spinner)
- [ ] **Index page**: All three view modes (card, list, table) show track expansion
- [ ] **Index page**: Enter valid YouTube URL for missing track, click Download — shows spinner
- [ ] **Index page**: Successful download — shows green "Downloaded successfully" with AcoustID score if available
- [ ] **Index page**: Download button changes to "Done" and input is disabled after success
- [ ] **Index page**: Enter invalid URL — shows "Invalid YouTube URL" error
- [ ] **Index page**: Empty URL field — shows "Please enter a YouTube URL"
- [ ] **Index page**: Press Enter in URL field — triggers download (keyboard shortcut)
- [ ] **Index page**: Download failure — shows error message and "Retry" button
- [ ] **Index page**: Download always accepts regardless of AcoustID score (no rejection)
- [ ] **API**: `GET /api/download/queue/<album_id>/tracks` returns `foreign_recording_id`
- [ ] **API**: `POST /api/album/<album_id>/track/manual-download` with valid data — returns success + AcoustID data
- [ ] **API**: `POST /api/album/<album_id>/track/manual-download` with invalid URL — returns 400
- [ ] **API**: `POST /api/album/<album_id>/track/manual-download` with missing fields — returns 400
- [ ] **API**: Manual download triggers Lidarr RefreshArtist after file is placed
- [ ] **API**: Manual download creates log entry with type "manual_download"

## Scheduler Tests

Requires scheduler to be enabled in settings:

- [ ] Scheduler polls at configured interval (check container logs)
- [ ] New missing albums are auto-queued when `scheduler_auto_download` is true
- [ ] Albums already in history (within lookback window) are not re-queued
- [ ] Albums currently downloading are not re-queued

## Notification Tests

Requires Telegram or Discord configured:

- [ ] Successful download sends notification (if log type enabled)
- [ ] Partial success sends notification
- [ ] Album error sends notification
- [ ] Copy-to-Lidarr failure sends notification with `album_error` type

## AcoustID Post-Download Verification Tests

Run after changes to fingerprint verification, verify-retry loop, or related UI:

- [ ] **Settings page**: AcoustID enabled + API key set — verification active during downloads
- [ ] **Current Download**: Track shows "Verifying..." status after "Tagging..."
- [ ] **Current Download**: If verification fails, track cycles back to "Downloading" for next candidate
- [ ] **Current Download**: Skip button works during "Verifying..." status
- [ ] **Logs page**: "URL Banned" filter shows auto-banned URLs from verification mismatches
- [ ] **Logs page**: Unban button works on auto-banned URLs
- [ ] **Downloads page**: History shows final accepted track (not rejected candidates)
- [ ] **Re-download**: Queue same album — previously auto-banned URLs are skipped during search

## Cleanup

```bash
docker compose down
```
