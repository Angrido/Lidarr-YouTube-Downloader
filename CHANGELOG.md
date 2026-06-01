# Changelog

## 1.8.0

### Added
- **Lidarr download-client bridge** — the app can now be configured inside
  Lidarr as a native **Newznab indexer + SABnzbd download client**, so Lidarr
  searches, grabs and imports automatically. Includes retry-cooldown
  protection against infinite re-grab loops, and the job registry is
  **persisted to SQLite** so downloads survive a restart.
- **Automatic YouTube PO tokens** via a bundled
  [bgutil provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider)
  sidecar (helps with "Sign in to confirm you're not a bot" /
  format-unavailable), plus an optional manual `po_token` field and a
  **Test** button for the provider URL.
- **Loudness normalization** (EBU R128, −14 LUFS) as an opt-in download option.
- **"Unban All"** button on the logs page (URL Banned filter).
- **Health endpoint** `/api/health` + Docker `HEALTHCHECK`.
- **GitHub Actions CI** running `pyflakes` + `pytest` on every push/PR.

### Improved
- YouTube matching now prefers the audio/Topic upload over a music video.
- AcoustID verification accepts a high-score recording from the **expected
  release group** when the exact recording id isn't matched (fewer false
  rejections of the right song).
- Clearer errors when `DOWNLOAD_PATH` is unset or the Lidarr library path
  isn't mounted/writable.

### Fixed
- Tagging crash when Lidarr returns a null `releaseDate`/`trackCount`.
- V5→V6 DB migration made idempotent (could block startup on some DBs).
- SSE progress stream no longer holds the global lock across blocking Lidarr
  HTTP calls, and snapshots track state safely.
- Guards against malformed/missing JSON and Lidarr error responses on the
  queue-reorder, album-details and add-to-queue endpoints.
- SQLite connection leak in the background sync worker.
- TOCTOU race when starting manual/playlist downloads.
- Numeric config values from `config.json` are coerced with safe fallbacks.
- Download-client "Busy" result re-queues instead of failing the grab.
