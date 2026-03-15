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
