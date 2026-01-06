# 🎵 Lidarr YouTube Downloader

<div align="center">

![Version](https://img.shields.io/badge/version-1.2.0-blue.svg?style=for-the-badge)
![Python](https://img.shields.io/badge/python-3.12+-yellow.svg?style=for-the-badge&logo=python&logoColor=white)
![Docker](https://img.shields.io/badge/docker-ready-blue.svg?style=for-the-badge&logo=docker&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg?style=for-the-badge)

**The missing link between Lidarr and YouTube.**
<br>Automatically find, download, tag, and import albums that are hard to find on standard indexers.

</div>

---

## ✨ Why this?

Lidarr is amazing for managing music libraries, but sometimes the albums you want just aren't available on Usenet or Torrents. **Lidarr YouTube Downloader** solves this by bridging the gap: it uses Lidarr's "Missing" list to find high-quality audio on YouTube, tags it perfectly with MusicBrainz metadata, and imports it right back into your library.

## 🚀 Features

*   **Seamless Lidarr Integration**: Automatically fetches missing albums from your Lidarr wanted list.
*   **High-Quality Audio**: Downloads the best available audio from YouTube (up to 320kbps/Opus) using `yt-dlp`.
*   **Auto-Tagging**: Applies ID3 tags (Artist, Album, Title, Cover Art) using data from Lidarr and iTunes.
*   **Modern Web UI**: A beautiful, responsive dashboard to track downloads in real-time.
    *   Dark & Light mode support 🌙/☀️
    *   Live progress bars & speed stats 📊
    *   Download queue management 📋
*   **Smart Notifications**: Get instant updates via **Telegram** when downloads start, finish, or fail.
*   **Robust Error Handling**:
    *   Handles "Partial Downloads" gracefully (imports what succeeded).
    *   Safety checks to prevent accidental file deletion.
*   **Docker Ready**: configure and deploy in seconds.
---

## 📸 Screenshots

<p align="center">
  <a href="https://github.com/user-attachments/assets/3feaa81a-0f2a-4bb4-8130-f721388118b6">
    <img src="https://github.com/user-attachments/assets/3feaa81a-0f2a-4bb4-8130-f721388118b6" width="45%">
  </a>
  <a href="https://github.com/user-attachments/assets/279647b8-8dca-4273-aaaf-d7dfce12b268">
    <img src="https://github.com/user-attachments/assets/279647b8-8dca-4273-aaaf-d7dfce12b268" width="45%">
  </a>
</p>

---

## 🏠 Installation on CasaOS (Recommended)

1. Open your **CasaOS Dashboard**.
2. Click the **+** button and select **"Install a Custom App"**.
3. Fill in the configuration:

| Field | Value |
|:---|:---|
| **Docker Image** | `angrido/lidarr-downloader:latest` |
| **Title** | `Lidarr Downloader` |
| **Icon URL** | `https://cdn-icons-png.flaticon.com/512/1895/1895657.png` |
| **WebUI Port** | `5000` |

4. **Ports Configuration:**
   - Host Port: `5005` (or any free port)
   - Container Port: `5000`

5. **Volumes Configuration (Crucial):**
   - Host Path: `/DATA/Downloads` (Where your music is downloaded)
   - Container Path: `/DATA/Downloads`

6. **Environment Variables (Add these keys):**

| Key | Value | Description |
|:---|:---|:---|
| `LIDARR_URL` | `http://192.168.1.xxx:8686` | Your Lidarr IP. **Do NOT** use `localhost` or `127.0.0.1`. |
| `LIDARR_API_KEY` | `your_api_key_here` | Found in Lidarr Settings -> General. |
| `DOWNLOAD_PATH` | `/DATA/Downloads` | Must match the Container Path above. |
| `LIDARR_PATH` | `/music` | Lidarr's music library folder (optional, leave empty to use download path). |

---

## 🐳 Docker Compose

If you prefer using standard Docker Compose:
```yaml
services:
  lidarr-downloader:
    image: angrido/lidarr-downloader:latest
    container_name: lidarr-downloader
    ports:
      - "5005:5000"
    volumes:
      - /DATA/Downloads:/DATA/Downloads
      - /DATA/Media/Music:/music
    environment:
      - LIDARR_URL=http://192.168.1.XXX:8686
      - LIDARR_API_KEY=your_lidarr_api_key
      - DOWNLOAD_PATH=/DATA/Downloads
      - LIDARR_PATH=/music
    restart: unless-stopped
```

---

## ⚠️ Disclaimer

This tool is intended for **educational purposes** and for managing your own personal library. The user is responsible for complying with all applicable copyright laws and YouTube's Terms of Service.
