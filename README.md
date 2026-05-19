<div align="center">

# 🎵 Lidarr YouTube Downloader

![Version](https://img.shields.io/badge/version-1.7.3-blue.svg?style=for-the-badge)
![Python Slim](https://img.shields.io/badge/python-3--slim-yellow.svg?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

**Automatically download missing music from YouTube directly into your Lidarr library**

Fill gaps in your self-hosted music collection — searches YouTube, downloads the best match, applies full MusicBrainz metadata, and triggers a Lidarr import. All from a clean web UI.

[Quick Start](#-quick-start) · [Configuration](#️-configuration) · [Screenshots](#-screenshots) · [Disclaimer](#️-disclaimer)

</div>

---

## ✨ Features

- 🔍 **Smart YouTube search** — scores candidates by title, duration, and channel to find the best audio match (up to 320 kbps)
- 🏷️ **Automatic metadata tagging** — enriches MP3 files with MusicBrainz + iTunes ID3 tags (artist, album, track number, artwork)
- 📦 **One-click Lidarr import** — triggers `DownloadedAlbumsScan` via the Lidarr API after every download
- 🌓 **Dark/light web UI** — monitor downloads, browse logs, and manage settings from the browser
- 🎚️ **Configurable filters** — custom forbidden-word lists to skip low-quality or live recordings
- 🔔 **Telegram & Discord notifications** — get alerted on success, partial success, or error events
- 🔄 **Download queue** — parallel track downloads with per-track progress, speed, and skip support
- 🎵 **AcoustID fingerprinting** (optional) — verify audio identity with chromaprint/fpcalc
- ⏱️ **Built-in scheduler** — automatically poll Lidarr for missing albums at a configurable interval
- 🐳 **Docker-first** — single container, works on any NAS, home server, or VPS

---

## 🚀 Quick Start

### Docker Compose (recommended)

```yaml
services:
  lidarr-downloader:
    image: angrido/lidarr-downloader:latest
    container_name: lidarr-downloader
    ports:
      - "5005:5000"
    volumes:
      - ./config:/config
      - /DATA/Downloads:/DATA/Downloads
      - /DATA/Media/Music:/music
    environment:
      - LIDARR_URL=http://192.168.1.XXX:8686
      - LIDARR_API_KEY=your_api_key_here
      - DOWNLOAD_PATH=/DATA/Downloads
      - LIDARR_PATH=/music
      - PUID=1000
      - PGID=1000
      - UMASK=002
    restart: unless-stopped
```

**Access the web UI at** `http://localhost:5005`

---

## ⚙️ Configuration

### Required Settings

| Variable         | Example                    | Description                      |
| ---------------- | -------------------------- | -------------------------------- |
| `LIDARR_URL`     | `http://192.168.1.10:8686` | Lidarr base URL (use LAN IP)     |
| `LIDARR_API_KEY` | `abc123...`                | From Lidarr → Settings → General |
| `DOWNLOAD_PATH`  | `/DATA/Downloads`          | Folder where tracks are saved    |

### Optional Settings

| Variable             | Default | Description                                       |
| -------------------- | ------- | ------------------------------------------------- |
| `LIDARR_PATH`        | —       | Final library path (if different from download)   |
| `AUDIO_FORMAT`       | `mp3`   | Output format: `mp3`, `m4a`, `opus`               |
| `SCHEDULER_ENABLED`  | `false` | Automatically check for missing albums on a timer |
| `SCHEDULER_INTERVAL` | `60`    | Polling interval in minutes                       |
| `YT_COOKIES_FILE`    | —       | Path to Netscape-format YouTube cookies file      |
| `ACOUSTID_ENABLED`   | `false` | Enable AcoustID audio fingerprint verification    |
| `ACOUSTID_API_KEY`   | —       | AcoustID API key (required when enabled)          |

> 💡 All settings can also be changed at runtime from the **Settings** page in the web UI.

### YouTube Cookies (Recommended)

YouTube may block downloads with a "Sign in to confirm you're not a bot" error. To bypass this:

1. Install a browser extension such as **Get cookies.txt LOCALLY**
2. Open a private/incognito window and log into a **throwaway** Google account on youtube.com
3. Export cookies in **Netscape** format and save the file as `cookies.txt`
4. Mount the file and point the app to it:

```yaml
volumes:
  - ./cookies.txt:/cookies/cookies.txt
environment:
  - YT_COOKIES_FILE=/cookies/cookies.txt
```

> ⚠️ **Do not use your main Google account.** Cookies expire periodically and will need re-exporting.

---

## 📸 Screenshots

<p align="center">
  <img src="https://github.com/user-attachments/assets/3feaa81a-0f2a-4bb4-8130-f721388118b6" width="45%" alt="Lidarr YouTube Downloader – missing albums dashboard">
  <img src="https://github.com/user-attachments/assets/279647b8-8dca-4273-aaaf-d7dfce12b268" width="45%" alt="Lidarr YouTube Downloader – download queue and progress">
</p>

---

## 🔄 Upgrading from JSON to SQLite

If you are upgrading from an older version that stored state in JSON files (`download_history.json`, `download_logs.json`, `last_failed_result.json`), migrate your data with:

```bash
# Inside the running container:
python3 tools/migrate_json_to_db.py --config-dir /config

# Or from the host (if /config is mounted locally):
python3 tools/migrate_json_to_db.py --config-dir ./config
```

The script imports all historical data into the SQLite database and renames the original JSON files to `*.json.migrated`.

---

## 🛠️ Local Development

```bash
git clone https://github.com/Angrido/Lidarr-YouTube-Downloader.git
cd Lidarr-YouTube-Downloader
pip install -r requirements.txt

export LIDARR_URL=http://your-lidarr:8686
export LIDARR_API_KEY=your_key
export DOWNLOAD_PATH=/tmp/downloads
export LIDARR_PATH=/tmp/downloads
export PUID=1000
export PGID=1000
export UMASK=002

python app.py   # runs on http://localhost:5000
```

Run the test suite:

```bash
source .venv/bin/activate && python -m pytest tests/ -v
```

---

## ⚠️ Disclaimer

This tool is intended for **personal and educational use** to manage your own music library.  
Users are solely responsible for complying with applicable copyright laws and YouTube's Terms of Service.

---

<a href="https://www.star-history.com/?repos=Angrido%2FLidarr-YouTube-Downloader&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&legend=top-left" />
   <img alt="Star History Chart for Lidarr YouTube Downloader" src="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&legend=top-left" />
 </picture>
</a>

<div align="center">

**Made with ❤️**

</div>
