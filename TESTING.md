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

## Cleanup

```bash
docker compose down
```
