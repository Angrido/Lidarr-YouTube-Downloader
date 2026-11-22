# Running, Docker, tools, dependencies, versioning

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

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
# LIDARR_PATH defaults to /music (the HOST_MUSIC_PATH mount)
WEBUI_PORT=5005
```

Then run:

```bash
docker compose up -d --build
```

### Docker Build & Run (manual)

```bash
docker build --pull --no-cache -t lidarr-downloader .
docker run -p 5005:5000 \
  -e PUID=1000 \
  -e PGID=1000 \
  -e UMASK=002 \
  -e LIDARR_URL=http://192.168.1.X:8686 \
  -e LIDARR_API_KEY=your_key \
  -e DOWNLOAD_PATH=/DATA/Downloads \
  -e LIDARR_PATH=/music \
  -v /DATA/Downloads:/DATA/Downloads \
  -v /DATA/Music:/music \
  -v ./config:/config \
  lidarr-downloader
```

## Utility Tools (`tools/`)

Standalone scripts not part of the main app:
- `fix_metadata.py` — batch fix ID3 tags on existing files
- `list_missing.py` — CLI to list missing albums from Lidarr
- `migrate_directories.py` — migrate album directory structure
- `migrate_json_to_db.py` — migrate JSON state files to SQLite (one-time upgrade)
- `verify_fingerprints.py` — AcoustID fingerprint verification tool
- `explore_preview.py` — run the UI against mocked YT Music + Lidarr (see Explore); `--demo` also seeds the Library, queue, an active download, history, insights and logs with fictional artists and generated cover art (the README screenshots in `docs/screenshots/` come from it)
- `ui_smoke.js` — with the `--demo` preview running, `npx -y -p playwright node tools/ui_smoke.js` loads every page with no stored theme, light, dark and dark + reduced motion, and fails on a JS error, a missing `.app-aura`, a non-dark first visit, text tokens below WCAG AA on `--bg-elevated`, a backdrop filter on `.ui-album`, or an aurora that moves under reduced motion
- `readme_screenshots.js` — with the `--demo` preview running, `npx -y -p playwright node tools/readme_screenshots.js` recaptures every README screenshot in both themes, framed (browser window / three phones) with rounded corners on a transparent background

## Key Dependencies

- `yt-dlp` — YouTube search and download
- `mutagen` — MP3 ID3 tag reading/writing
- `Flask` — web server (its built-in server; `entrypoint.sh` runs `python app.py`)

**Everything floats to its latest release, on purpose.** `requirements.txt`
has no versions, the base image is `python:3-slim` (the newest Python), the
Dockerfile runs `apt-get upgrade` and installs the latest Deno, and the
bgutil sidecar is `:latest` with `pull_policy: always` (it must match the
bgutil plugin pip installs). Build with `--pull --no-cache` (compose sets
both), or cached layers keep the old versions. What keeps "latest" safe:
- a `wheels` build stage with a compiler builds wheels a brand-new Python
  does not have yet (Python 3.15: brotli), so the slim runtime needs no gcc;
- pip is uninstalled (with ensurepip's bundled wheel) at the end of the
  install step: scanners flag the urllib3 / msgpack / setuptools it vendors,
  and nothing at runtime needs it (`ytdlp_update.py` replaces it);
- the image build fails on `pip check` conflicts, on `python -c "import app"`
  or on a missing `deno`/`ffmpeg`/`fpcalc`, instead of shipping a broken image;
- CI tests on Python 3.12 and the newest 3.x with freshly resolved
  dependencies, builds the image, and re-runs every Monday so an upstream
  release that breaks the app is caught with no commit here.
To hold a library back after a breaking release, add `pkg<X` to
`requirements.txt` with a comment saying why and when to lift it.
- `schedule` — optional cron-style scheduler
- `ffmpeg` (system package, not pip) — audio conversion
- `fpcalc`/chromaprint (optional system package) — AcoustID fingerprinting

## Version Updates

The version string is defined in `version.py`: `VERSION = "2.0.2"` (2.0.1 is published, tag `v2.0.1`; 2.0.2 is the version being prepared on `beta`). Changes for the next release go under its own `## X.Y.Z` section at the top of CHANGELOG.md, together with the `version.py` bump. The README badge also references it and must be updated manually.

## Persistence Volume

The `/config` directory must be writable. It stores `config.json` and `lidarr-downloader.db` (SQLite database). In Docker, mount a volume here to persist settings across container restarts.
