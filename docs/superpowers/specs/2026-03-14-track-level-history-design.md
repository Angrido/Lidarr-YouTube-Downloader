# Track-Level Download History

Tracks individual song downloads instead of just album-level records. Captures YouTube source metadata per track and maintains a full audit trail across re-downloads.

## Problem

Everything is tracked at the album level. YouTube URLs used for downloads are discarded after use. If a song is deleted and re-downloaded, there's no record of which YouTube video was previously used. No per-track metadata is visible in the UI.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Data model | Replace `download_history` + `failed_tracks` with `track_downloads` | Single source of truth, album views derived via GROUP BY |
| Per-track metadata | YouTube URL, video title, match score, duration, timestamp | Core metadata without bloat (no channel, file size, bitrate) |
| Logs | Keep album-level events, add track-level entries, drop `failed_tracks` JSON column | Full granularity in Logs page |
| UI layout | Album rows with expandable track detail | Preserves current UX, one click to drill in |
| `failed_tracks` table | Drop entirely | Redundant once `track_downloads` exists |
| Multiple attempts | Keep all rows per track | Full audit trail of YouTube URLs used |
| Migration | Drop old data, clean slate | Old data has no track-level info to preserve |
| `manual` flag | Drop | Not used or needed |

## Database Schema

### New table: `track_downloads`

```sql
CREATE TABLE track_downloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    track_title TEXT NOT NULL,
    track_number INTEGER NOT NULL DEFAULT 0,
    success INTEGER NOT NULL DEFAULT 0,
    error_message TEXT DEFAULT '',
    youtube_url TEXT DEFAULT '',
    youtube_title TEXT DEFAULT '',
    match_score REAL DEFAULT 0.0,
    duration_seconds INTEGER DEFAULT 0,
    timestamp REAL NOT NULL
);

CREATE INDEX idx_track_dl_album_id ON track_downloads(album_id);
CREATE INDEX idx_track_dl_timestamp ON track_downloads(timestamp);
CREATE INDEX idx_track_dl_youtube_url ON track_downloads(youtube_url);
```

### Modified table: `download_logs`

Drop the `failed_tracks` column. New log types added: `track_success`, `track_error`.

### Dropped tables

- `download_history` — replaced by `track_downloads`
- `failed_tracks` — replaced by `track_downloads`

### Migration: V1 to V2

1. Drop `download_history` table
2. Drop `failed_tracks` table
3. Create `track_downloads` table with indexes
4. Recreate `download_logs` without the `failed_tracks` column
5. Update `schema_version` to 2

## Data Flow Changes

### `downloader.py`

`download_track_youtube()` returns a metadata dict instead of `True`/string:

```python
# Success
{
    "success": True,
    "youtube_url": "https://www.youtube.com/watch?v=abc123",
    "youtube_title": "Artist - Track (Official Audio)",
    "match_score": 0.87,
    "duration_seconds": 234,
}

# Failure
{
    "success": False,
    "error_message": "No suitable YouTube match found",
}
```

### `processing.py`

- `_download_tracks()`: after each track download (success or failure), call `models.add_track_download()` with the metadata
- `process_album_download()` `finally` block: remove `save_failed_tracks()` and `add_history_entry()` calls — per-track recording happens inline
- Failed tracks for retry: query `track_downloads` instead of `failed_tracks` table

### `models.py`

New functions:
- `add_track_download(album_id, album_title, artist_name, track_title, track_number, success, error_message, youtube_url, youtube_title, match_score, duration_seconds)`
- `get_track_downloads_for_album(album_id)` — all track records for an album
- `get_album_history(page, per_page)` — grouped query returning album-level summaries
- Updated: `get_history_album_ids_since(timestamp)` — query `track_downloads`
- Updated: `get_history_count_today()` — count distinct successful albums today from `track_downloads`
- `clear_history()` — delete all `track_downloads` rows

Removed functions:
- `add_history_entry`
- `get_history`
- `save_failed_tracks`
- `get_failed_tracks`
- `get_failed_tracks_context`
- `remove_failed_track`
- `clear_failed_tracks`

## API Changes

### Modified endpoints

| Endpoint | Change |
|----------|--------|
| `GET /api/download/history` | Returns album-grouped summaries (total/success/fail counts, latest timestamp) |
| `GET /api/download/failed` | Queries `track_downloads WHERE success = 0` for the most recent album |
| `GET /api/stats` | Same shape, backed by `track_downloads` |
| `GET /api/logs` | Responses no longer include `failed_tracks` JSON field; track-level log entries appear as rows |

### New endpoints

| Endpoint | Purpose |
|----------|---------|
| `GET /api/download/history/<album_id>/tracks` | All track download records for an album (for expandable UI) |

## UI Changes

### Downloads page (`downloads.html`)

History section changes from flat album rows to expandable album rows:

**Collapsed (default):** Album row with title, artist, color-coded track count badge (green = all success, amber = partial, red = all failed), timestamp. Click arrow to expand.

**Expanded:** Grid of individual tracks showing:
- Track number
- Track title (with "(N attempts)" indicator if downloaded more than once)
- YouTube source link (clickable, opens video, title on hover)
- Match score
- Duration (formatted as M:SS)
- Download timestamp

Failed tracks show red-tinted background with error message in place of YouTube link.

All styling uses existing CSS variables (`--bg`, `--surface`, `--primary`, `--danger`, `--text`, `--text-dim`, `--border`, etc.) and follows the existing glassmorphism patterns.

### Logs page

Track-level log entries (`track_success`, `track_error`) appear as regular log rows. No structural changes to the template.

## Error Handling

- If `download_track_youtube` succeeds but metadata extraction fails (missing URL from yt-dlp response): record with empty YouTube fields, `success=True`
- Failed downloads: `success=0`, `error_message` populated, YouTube fields empty
- Migration failure: transaction rolls back, V1 schema stays intact, startup error logged

## Testing

| Test file | Coverage |
|-----------|----------|
| `test_db.py` | V1→V2 migration: tables dropped/created, column removed |
| `test_models.py` | `add_track_download`, `get_track_downloads_for_album`, `get_album_history`, `get_history_album_ids_since`, `get_history_count_today`, `clear_history` |
| `test_downloader.py` | `download_track_youtube` returns metadata dict |
| `test_processing.py` | `_download_tracks` calls `add_track_download` per track |
| `test_routes.py` | New endpoint, updated response shapes, updated `/api/download/failed` |
