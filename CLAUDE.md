# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Lidarr YouTube Downloader is a Flask web app that bridges Lidarr and YouTube. It queries Lidarr's API for missing albums, searches YouTube for matching tracks via `yt-dlp`, downloads them, applies MP3 metadata (ID3 tags), and imports them back into Lidarr. Deployed as a Docker container.

## Running Locally

The app is designed to run inside Docker. To run locally for development:

```bash
pip install -r requirements.txt
# Set required env vars first
export LIDARR_URL=http://your-lidarr:8686
export LIDARR_API_KEY=your_key
export DOWNLOAD_PATH=/tmp/downloads
export LIDARR_PATH=/tmp/downloads
export PUID=1000
export PGID=1000
export UMASK=002
python app.py
```

The app runs on port 5000.

### Docker Compose (recommended)

Create a `.env` file with the required variables (see `docker-compose.yml`):

```env
HOST_DOWNLOAD_PATH=/DATA/Downloads
DOWNLOAD_PATH=/DATA/Downloads
HOST_MUSIC_PATH=/DATA/Music
HOST_CONFIG=./config
PUID=1000
PGID=1000
UMASK=002
LIDARR_URL=http://192.168.1.X:8686
LIDARR_API_KEY=your_key
LIDARR_PATH=/DATA/Downloads
WEBUI_PORT=5005
```

Then run:

```bash
docker compose up -d
```

### Docker Build & Run (manual)

```bash
docker build -t lidarr-downloader .
docker run -p 5005:5000 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e UMASK=002 \
  -e LIDARR_URL=http://192.168.1.X:8686 \
  -e LIDARR_API_KEY=your_key \
  -e DOWNLOAD_PATH=/DATA/Downloads \
  -e LIDARR_PATH=/DATA/Downloads \
  -v /DATA/Downloads:/DATA/Downloads \
  -v /DATA/Music:/music \
  -v ./config:/config \
  lidarr-downloader
```

## Architecture

### Module Structure

| Module | Responsibility |
|--------|---------------|
| `app.py` | Flask app, thin route handlers, startup |
| `db.py` | SQLite connection, schema, migrations |
| `models.py` | All SQL queries, CRUD, pagination |
| `downloader.py` | YouTube search/scoring/download via yt-dlp |
| `processing.py` | Album processing, queue processor, per-track state, skip handling |
| `metadata.py` | ID3 tagging, XML sidecar, iTunes API |
| `lidarr.py` | Lidarr API wrapper |
| `notifications.py` | Telegram/Discord webhooks |
| `config.py` | Config load/save, constants |
| `scheduler.py` | Scheduled polling/auto-download |
| `fingerprint.py` | AcoustID fingerprinting via fpcalc/chromaprint |
| `download_client.py` | Lidarr download-client bridge: Newznab indexer + SABnzbd client emulation (Flask blueprint) |
| `utils.py` | Shared utilities |

### Key data flows

1. Lidarr API (`/api/v1/wanted/missing`) → missing albums list shown in UI
2. User triggers download → `download_track_youtube()` searches YouTube, scores candidates, downloads best match via `yt-dlp` + ffmpeg
3. Post-download: metadata applied via `mutagen` (ID3 tags from MusicBrainz/iTunes APIs), optional XML sidecar written
4. Optional AcoustID fingerprinting via `fingerprint.py` (requires `fpcalc` binary and API key)
5. Per-track download results recorded in `track_downloads` table via `models.add_track_download()`
6. Lidarr import triggered via `/api/v1/command` (DownloadedAlbumsScan)

### Database

State is stored in SQLite at `/config/lidarr-downloader.db`. Tables: `schema_version`, `track_downloads`, `download_logs`, `download_queue`, `banned_urls`, `candidate_attempts`, `missing_albums_cache`, `sync_state`, `download_client_jobs`.

Current schema version: **7**. Migrations:
- V1→V2: Replaced `download_history` + `failed_tracks` with `track_downloads` (per-track download records with YouTube URL, match score, duration, album/track metadata).
- V2→V3: Added AcoustID fingerprint columns to `track_downloads` (`acoustid_fingerprint_id`, `acoustid_score`, `acoustid_recording_id`, `acoustid_recording_title`).
- V3→V4: Added `banned_urls` table for tracking banned YouTube URLs per album/track.
- V4→V5: Added `candidate_attempts` table for per-candidate verification data. Added `track_title`, `track_number`, `track_download_id` columns to `download_logs`.
- V5→V6: Added `missing_albums_cache` and `sync_state` tables for paginated background sync of Lidarr's missing-albums list.
- V6→V7: Added `download_client_jobs` table so the Lidarr download-client bridge (SABnzbd `nzo_id` jobs) survives restarts; `download_client.restore_jobs()` reloads them at startup and re-queues interrupted downloads.

Schema is versioned via `schema_version` table. **When changing the DB schema:**

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a migration function `migrate_vN_to_vN+1(conn)` in `db.py`
3. Register it in the `migrations` dict inside `_run_migrations()`
4. Test with `python3 -m pytest tests/test_db.py`

### In-memory state

- `download_process` — current active download state (transient, not persisted). Contains per-track state: `tracks` list (each with `status`, `title`, `track_number`, `error`, `youtube_url`, `progress`, `speed`), `current_track_index`, album metadata. `TrackSkippedException` enables skip-track support during search and download.

### Config

Loaded from env vars + `/config/config.json`. File config overrides env vars. Saved via `save_config()`. `ALLOWED_CONFIG_KEYS` whitelist controls what can be set via the API. Notable config keys beyond the basics: `concurrent_tracks`, `yt_cookies_file`, `yt_force_ipv4`, `yt_player_client`, `yt_retries`, `yt_fragment_retries`, `yt_sleep_requests`, `yt_sleep_interval`, `yt_max_sleep_interval`, `discord_enabled`, `discord_webhook_url`, `discord_log_types`, `acoustid_enabled`, `acoustid_api_key`, `download_client_enabled`, `download_client_api_key`, `download_client_category`, `yt_po_token` (manual yt-dlp PO token(s), comma-separated), `audio_normalize` (EBU R128 loudnorm, forces re-encode), `yt_pot_provider_url` (URL of a bgutil PO-token provider sidecar for automatic PO tokens; `bgutil-ytdlp-pot-provider` plugin is in requirements and a sidecar is wired in docker-compose).

### Lidarr download-client bridge (`download_client.py`)

Optional feature letting Lidarr use this app as a native download path. A Flask blueprint exposes a **Newznab indexer** (`/api/newznab/api`: `t=caps`, `t=music`/`t=search`, `t=get`) and a **SABnzbd download client** (`/api/sabnzbd/api`: `version`, `get_config`, `fullstatus`, `queue`, `history`, `addfile`, `addurl`). A search is matched against `missing_albums_cache` to resolve a Lidarr `album_id`; the served NZB embeds that id; on `addfile` the id is parsed back out and enqueued via `models.enqueue_album()`. A job registry (keyed by SABnzbd `nzo_id`, in-memory write-through cache backed by the `download_client_jobs` table) tracks queued→downloading→completed/failed so Lidarr polls `queue`/`history` and imports the finished files itself; `restore_jobs()` reloads it at startup so a restart doesn't orphan a download. Grabbed albums are detected in `processing.process_album_download()` via `download_client.is_client_album()`, which skips the copy-to-library / `RefreshArtist` / cleanup steps; the queue processor routes them through `download_client.run_album_job()`. Both surfaces require `download_client_enabled` plus a matching `apikey`. To avoid infinite re-grab loops, in-flight albums and albums attempted within the retry cooldown (`scheduler_retry_after_hours`) are excluded from the indexer feed/search and refused at grab time; release `guid`s are time-bucketed on that window so retries are still possible after the cooldown.

### Threading

Downloads run in background threads. `queue_lock` (threading.Lock) in `processing.py` protects shared state.

### Scheduler

Optional `schedule` library job polls for missing albums and auto-downloads at configured intervals.

### Notifications

Telegram and Discord webhooks, filtered by `log_type` (e.g., `partial_success`, `album_error`).

## Templates

- `templates/index.html` — main dashboard, missing albums list
- `templates/downloads.html` — download queue and history
- `templates/logs.html` — download log entries with retry support
- `templates/settings.html` — configuration UI
- `static/favicon.svg` — app icon

## Utility Tools (`tools/`)

Standalone scripts not part of the main app:
- `fix_metadata.py` — batch fix ID3 tags on existing files
- `list_missing.py` — CLI to list missing albums from Lidarr
- `migrate_directories.py` — migrate album directory structure
- `migrate_json_to_db.py` — migrate JSON state files to SQLite (one-time upgrade)
- `verify_fingerprints.py` — AcoustID fingerprint verification tool

## Key Dependencies

- `yt-dlp` — YouTube search and download
- `mutagen` — MP3 ID3 tag reading/writing
- `Flask` + `gunicorn` — web server
- `schedule` — optional cron-style scheduler
- `ffmpeg` (system package, not pip) — audio conversion
- `fpcalc`/chromaprint (optional system package) — AcoustID fingerprinting

## Version Updates

The version string is defined in `app.py`: `VERSION = "1.8.1"`. The README badge also references it and must be updated manually.

## Persistence Volume

The `/config` directory must be writable. It stores `config.json` and `lidarr-downloader.db` (SQLite database). In Docker, mount a volume here to persist settings across container restarts.

## Testing

Run tests with the venv:

```bash
source .venv/bin/activate && python -m pytest tests/ -v
```

Tests are in `tests/` directory mirroring module structure: `test_db.py`, `test_models.py`, `test_config.py`, `test_utils.py`, `test_notifications.py`, `test_lidarr.py`, `test_metadata.py`, `test_downloader.py`, `test_routes.py`, `test_processing.py`, `test_fingerprint.py`, `test_migrate_tool.py`, `test_download_client.py`.
