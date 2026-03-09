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

Everything lives in a single `app.py` (~2000+ lines). There is no package structure or test suite.

**Key data flows:**
1. Lidarr API (`/api/v1/wanted/missing`) → missing albums list shown in UI
2. User triggers download → `download_track_youtube()` searches YouTube, scores candidates, downloads best match via `yt-dlp` + ffmpeg
3. Post-download: metadata applied via `mutagen` (ID3 tags from MusicBrainz/iTunes APIs), optional XML sidecar written
4. Lidarr import triggered via `/api/v1/command` (DownloadedAlbumsScan)

**In-memory state** (persisted to `/config/*.json`):
- `download_queue` — pending downloads
- `download_history` — completed downloads (`/config/download_history.json`)
- `download_logs` — log entries for UI (`/config/download_logs.json`)
- `last_failed_result` — tracks that failed, enabling retry (`/config/last_failed_result.json`)
- `download_process` — current active download state

**Config**: Loaded from env vars + `/config/config.json`. File config overrides env vars. Saved via `save_config()`. `ALLOWED_CONFIG_KEYS` whitelist controls what can be set via the API.

**Threading**: Downloads run in background threads. `queue_lock` (threading.Lock) protects shared state. `_file_write_lock` protects file I/O.

**Scheduler**: Optional `schedule` library job polls for missing albums and auto-downloads at configured intervals.

**Notifications**: Telegram and Discord webhooks, filtered by `log_type` (e.g., `partial_success`, `album_error`).

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

## Key Dependencies

- `yt-dlp` — YouTube search and download
- `mutagen` — MP3 ID3 tag reading/writing
- `Flask` + `gunicorn` — web server
- `schedule` — optional cron-style scheduler
- `ffmpeg` (system package, not pip) — audio conversion

## Version Updates

The version string is defined in `app.py`: `VERSION = "1.5.2"`. The README badge also references it and must be updated manually.

## Persistence Volume

The `/config` directory must be writable. It stores `config.json`, `download_history.json`, `download_logs.json`, and `last_failed_result.json`. In Docker, mount a volume here to persist settings across container restarts.
