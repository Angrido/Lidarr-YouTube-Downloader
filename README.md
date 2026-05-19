<div align="center">

# 🎵 Lidarr YouTube Downloader

![Version](https://img.shields.io/badge/version-1.7.4-blue.svg?style=for-the-badge)
![Python Slim](https://img.shields.io/badge/python-3--slim-yellow.svg?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

A free, open-source bridge between **[Lidarr](https://lidarr.audio/)** and **YouTube** that fills the gaps in your self-hosted music library. Powered by **yt-dlp**, **MusicBrainz**, **iTunes**, and **AcoustID**, it searches YouTube for your missing albums, scores the best audio match, downloads it at up to 320 kbps, writes complete ID3 metadata with embedded artwork, and triggers a Lidarr import — all from a clean web UI that runs on any Docker host (NAS, Synology, Unraid, Raspberry Pi, VPS).

[**Quick Start**](#-quick-start) · [**Features**](#-features) · [**How It Works**](#-how-it-works) · [**Configuration**](#️-configuration) · [**Screenshots**](#-screenshots) · [**FAQ**](#-faq)

</div>

---

## ✨ Features

- 🔍 **Smart YouTube matching** — searches up to 15 candidates per track and scores them by title similarity, duration window, official-channel boost, and forbidden-word filtering (remix, live, cover, karaoke…)
- 🎯 **AcoustID fingerprinting** — optional chromaprint verification rejects mismatched audio before import
- 🏷️ **Full metadata tagging** — MP3 / M4A / Opus with MusicBrainz IDs, iTunes 3000×3000 cover art, year, track numbers, and optional XML sidecars for Lidarr re-import
- 📦 **Native Lidarr integration** — copies tagged files into your Lidarr library path and triggers `RefreshArtist`; background paginated sync of `wanted/missing` for instant UI
- ⚡ **Parallel downloads** — configurable concurrent tracks (1–5) with mid-download skip, per-track progress, speed, and ETA
- 🚫 **Banned URLs & candidate retries** — per-track YouTube blacklist; tries up to 15 candidates before giving up
- 📥 **Manual YouTube URL & playlist import** — paste any YouTube or YouTube Music URL (single track or full playlist) with album-art preview
- 🎵 **Audio streaming preview** — listen to candidates and playlist items in the browser before queuing
- ⏱️ **Built-in scheduler** — auto-discover and auto-download new missing albums at a configurable interval with per-run album limits
- 🔔 **Telegram & Discord notifications** — per-channel filters for success, partial success, errors, and manual events
- 🛠️ **yt-dlp tuning UI** — cookies file, player client, retries, sleep intervals, IPv4 force, and one-click yt-dlp upgrade
- 📊 **Stats dashboard & logs** — success rate, average match score, total downloaded size, per-album logs with retry
- 🌓 **Modern dark/light web UI** — responsive design, drag-to-reorder queue, structured logs
- 🐳 **Docker-first** — single container, Compose-ready, works on NAS, home server, Unraid, Synology, or any VPS

---

## 🔄 How It Works

1. **Sync** — A background job paginates Lidarr's `/wanted/missing` endpoint into a local SQLite cache so the dashboard loads instantly.
2. **Search** — For each track, `yt-dlp` queries YouTube and returns the top 15 candidates.
3. **Score** — Candidates are ranked by title similarity (50%), duration match (25%), official-channel bonus (15%), and view-count weight, with forbidden-word filtering.
4. **Verify** — If AcoustID is enabled, the downloaded file is fingerprinted with `fpcalc` and matched against the expected MusicBrainz recording ID before acceptance.
5. **Tag** — Mutagen writes ID3 tags (title, artist, album, track #, year, MusicBrainz IDs) and embeds an iTunes 3000×3000 cover.
6. **Import** — Files are copied into your Lidarr music path, then a `RefreshArtist` command is sent so Lidarr scans and picks up the new tracks.

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

Open the web UI at **`http://localhost:5005`** and configure the rest from the Settings page.

---

## ⚙️ Configuration

### Required environment variables

| Variable         | Example                    | Description                      |
| ---------------- | -------------------------- | -------------------------------- |
| `LIDARR_URL`     | `http://192.168.1.10:8686` | Lidarr base URL (use LAN IP)     |
| `LIDARR_API_KEY` | `abc123…`                  | Lidarr → Settings → General      |
| `DOWNLOAD_PATH`  | `/DATA/Downloads`          | Where new tracks are saved       |
| `LIDARR_PATH`    | `/music`                   | Final music library path         |

Most other settings (audio format, concurrent tracks, match score threshold, forbidden words, scheduler, notifications, AcoustID, yt-dlp tuning) live in the **Settings** page of the web UI.

### YouTube cookies (recommended)

If YouTube returns *"Sign in to confirm you're not a bot"*, supply a cookies file:

1. Install the **Get cookies.txt LOCALLY** browser extension
2. Open a private window and sign in to a **throwaway** Google account
3. Export cookies in **Netscape** format as `cookies.txt`
4. Mount it and set `YT_COOKIES_FILE`:

```yaml
volumes:
  - ./cookies.txt:/cookies/cookies.txt
environment:
  - YT_COOKIES_FILE=/cookies/cookies.txt
```

> ⚠️ Never use your main Google account — cookies expire and accounts can be flagged.

### AcoustID fingerprinting (optional)

Enable in the Settings page and provide an [AcoustID API key](https://acoustid.org/new-application). The container already ships with `fpcalc` (chromaprint).

---

## 📸 Screenshots

<p align="center">
  <img src="https://github.com/user-attachments/assets/3feaa81a-0f2a-4bb4-8130-f721388118b6" width="45%" alt="Lidarr YouTube Downloader dashboard showing missing albums from Lidarr">
  <img src="https://github.com/user-attachments/assets/279647b8-8dca-4273-aaaf-d7dfce12b268" width="45%" alt="Lidarr YouTube Downloader download queue with per-track progress and metadata">
</p>

---

## ❓ FAQ

**Is this a replacement for a real music indexer?**  
No — it's a fallback when albums are unavailable through standard Lidarr indexers. Audio quality is limited to YouTube's source.

**Does it work with Plex, Jellyfin, or Navidrome?**  
Yes. Files are imported into Lidarr's library, which any music server can then index.

**What audio formats are supported?**  
MP3 (default, up to 320 kbps), M4A, and Opus — selectable in Settings.

**Will it download playlists or single YouTube videos?**  
Yes. The **YouTube** page accepts any YouTube or YouTube Music URL, including full playlists, with metadata preview before queuing.

**Does it run on Synology / Unraid / Raspberry Pi?**  
Yes — any platform that runs Docker. The image is multi-arch.

**Is yt-dlp kept up to date?**  
You can upgrade yt-dlp from the Settings page with a single click; the UI shows the installed and latest PyPI versions.

---

## 🔄 Upgrading from older JSON state

Versions before SQLite stored state in JSON files. Migrate with:

```bash
docker exec -it lidarr-downloader python3 tools/migrate_json_to_db.py --config-dir /config
```

The originals are renamed to `*.json.migrated`.

---

## 🛠️ Local Development

```bash
git clone https://github.com/Angrido/Lidarr-YouTube-Downloader.git
cd Lidarr-YouTube-Downloader
pip install -r requirements.txt
python app.py   # http://localhost:5000

# Run tests:
source .venv/bin/activate && python -m pytest tests/ -v
```

---

## ⚠️ Disclaimer

This project is provided for **personal, educational use** to manage your own music library. Users are solely responsible for complying with copyright laws and YouTube's Terms of Service.

---

<a href="https://www.star-history.com/?repos=Angrido%2FLidarr-YouTube-Downloader&type=date&legend=top-left">
 <picture>
   <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&theme=dark&legend=top-left" />
   <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&legend=top-left" />
   <img alt="GitHub star history chart for Lidarr YouTube Downloader" src="https://api.star-history.com/image?repos=Angrido/Lidarr-YouTube-Downloader&type=date&legend=top-left" />
 </picture>
</a>

<div align="center">

**Made with ❤️ for the self-hosted music community**

</div>
