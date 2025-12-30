# 🎵 Lidarr YouTube Downloader

![Lidarr Integration](https://img.shields.io/badge/Integration-Lidarr-green?style=flat-square&logo=lidarr)
![Docker](https://img.shields.io/badge/Docker-Ready-blue?style=flat-square&logo=docker)
![Python](https://img.shields.io/badge/Python-3.12-yellow?style=flat-square&logo=python)

A modern, feature-rich web application that automatically downloads missing albums from your Lidarr library using YouTube as a source. Built with Flask and featuring a beautiful glassmorphism UI with advanced download management.

## ✨ Features

### 🎨 Modern UI
- **Glassmorphism Design** - Beautiful, modern interface with blur effects and gradient animations
- **Dark/Light Theme** - Seamless theme switching with persistent preferences
- **Responsive Layout** - Optimized for desktop, tablet, and mobile devices
- **Multiple View Modes** - Switch between Cards, List, and Table views

### 📥 Advanced Download Management
- **Download Queue System** - Queue multiple albums for sequential processing
- **Persistent Progress** - Real-time progress tracking visible across all pages
- **Minimizable Progress** - Minimize progress to a floating button while browsing
- **In-Card Progress** - See download progress directly on album cards
- **Stop Downloads** - Cancel active downloads anytime
- **Download History** - Track all completed downloads with success/failure status

### 🤖 Automation
- **Smart Scheduler** - Automatically check for missing albums at configurable intervals
- **Duplicate Prevention** - Intelligent filtering to avoid re-downloading existing albums
- **Queue Integration** - Automatically adds new missing albums to the download queue
- **Auto-Download Toggle** - Enable/disable automatic downloads while keeping monitoring active

### 📱 Notifications
- **Telegram Integration** - Get notified about downloads and scheduler activities
- **Real-time Updates** - Live status updates without page refresh

### 📋 Management Pages
- **Dashboard** - Browse and manage your library with powerful filtering
- **Downloads** - Monitor active downloads, queue, and history
- **Settings** - Configure automation, Telegram, and application preferences

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

## 📖 Usage

### Dashboard
1. **Browse Albums** - View all missing albums from your Lidarr library
2. **Search & Filter** - Use the search box and sorting options
3. **Change View** - Switch between Cards, List, or Table view
4. **Add to Queue** - Click "Add to Queue" on any album

### Downloads Page
- **Monitor Progress** - See current download with real-time updates
- **Manage Queue** - Reorder or remove queued albums
- **View History** - Check past downloads and their status
- **Stop Downloads** - Cancel active downloads if needed

### Settings
- **Automation** - Configure scheduler interval and auto-download
- **Telegram** - Set up notification bot
- **Theme** - Switch between dark and light modes

### Progress Container
- **Minimize** - Click X to minimize to a floating button
- **Restore** - Click the floating button to restore full view
- **Queue Preview** - See next 3 albums in queue
- **Stop** - Cancel current download

---

## ⚠️ Disclaimer

This tool is intended for **educational purposes** and for managing your own personal library. The user is responsible for complying with all applicable copyright laws and YouTube's Terms of Service.
