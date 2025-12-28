# 🎵 Lidarr YouTube Downloader

![Lidarr Integration](https://img.shields.io/badge/Integration-Lidarr-green?style=flat-square&logo=lidarr)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?style=flat-square&logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-yellow?style=flat-square&logo=python)

**Lidarr YouTube Downloader** is a modern, lightweight web tool that bridges the gap between **Lidarr** and **YouTube**.

It automatically fetches your "Missing" albums list from Lidarr, allows you to download them directly from YouTube as high-quality MP3s, injects correct metadata (including **MusicBrainz IDs** and **Cover Art**), and triggers an automatic import in Lidarr.

**No torrents or Usenet required.** Perfect for filling gaps in your library.

---

## 📸 Screenshots

<p align="center">
  <a href="https://i.imgur.com/MC8X1qP.png">
    <img src="https://i.imgur.com/MC8X1qP.png" width="45%">
  </a>
  <a href="https://i.imgur.com/mGs2pqV.png">
    <img src="https://i.imgur.com/mGs2pqV.png" width="45%">
  </a>
</p>

---

## ✨ Key Features

- 🕵️ **Seamless Integration:** Connects to your Lidarr API to find missing albums instantly.
- 🎧 **YouTube Engine:** Powered by the latest `yt-dlp` to bypass restrictions and fetch high-quality audio.
- 🏷️ **Advanced Tagging:**
  - Embeds **MusicBrainz Release ID** & **Artist ID** (Fixes "Album match not close enough" errors).
  - Embeds High-Res **Cover Art** directly into the MP3.
  - Sets Title, Artist, Album, and Track Number tags.
- 🔄 **Auto-Import:** Triggers a targeted `DownloadedAlbumsScan` in Lidarr immediately after download.
- 📱 **Responsive UI:** Beautiful dark-mode interface that works on Desktop and Mobile.
- 🚀 **Real-time Feedback:** Shows download speed and import status live.

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

---

## 🐳 Docker Compose

If you prefer using standard Docker Compose:
```yaml
version: '3'
services:
  lidarr-downloader:
    image: angrido/lidarr-downloader:latest
    container_name: lidarr-downloader
    ports:
      - "5005:5000"
    volumes:
      - /DATA/Downloads:/DATA/Downloads
    environment:
      - LIDARR_URL=http://192.168.1.XXX:8686
      - LIDARR_API_KEY=your_lidarr_api_key
      - DOWNLOAD_PATH=/DATA/Downloads
    restart: unless-stopped
```

---

## ⚙️ Configuration Details

| Variable | Description | Example |
|:---|:---|:---|
| `LIDARR_URL` | The full URL to your Lidarr instance. | `http://192.168.1.50:8686` |
| `LIDARR_API_KEY` | Your API Key found in Lidarr (Settings -> General). | `a1b2c3d4e5...` |
| `DOWNLOAD_PATH` | Where the MP3s will be saved inside the container. | `/DATA/Downloads` |

---

## ❓ Troubleshooting

**Q: I get "HTTP Error 403: Forbidden" in logs.**  
**A:** This image uses the latest `yt-dlp` and custom headers to emulate a real browser, minimizing YouTube blocks. Ensure you are using the `:latest` tag.

**Q: Lidarr shows "Import Failed".**  
**A:**
1. Ensure `DOWNLOAD_PATH` matches exactly in both the Container and Lidarr's Root Folder settings.
2. Ensure `LIDARR_URL` is the actual LAN IP of your server, not `localhost`.

**Q: Files are downloaded but Lidarr rejects them.**  
**A:** This tool injects `MusicBrainz Album Id` into the files. Lidarr should accept them automatically. If not, check your Lidarr Metadata Profile settings.

---

## ⚠️ Disclaimer

This tool is intended for **educational purposes** and for managing your own personal library. The user is responsible for complying with all applicable copyright laws and YouTube's Terms of Service.
