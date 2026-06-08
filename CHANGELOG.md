# Changelog

## 1.8.1

### Fixed
- **PO-token provider "Test" button now does a real check**: it queries the
  bgutil provider's `/ping`, verifies the response is actually a bgutil POT
  provider and reports its version, instead of reporting success for any
  HTTP response (even a 404 or an unrelated server).
- **Indexer feed no longer goes empty after a manual/scheduler download**:
  the Newznab feed now only hides albums the download client itself handled
  within the retry cooldown (plus in-flight ones), not every album with a
  recent log, so Lidarr's indexer test keeps returning still-missing albums.
- **Lidarr no longer rejects RSS grabs as "larger than maximum allowed
  size"**: the Newznab feed now estimates release size from the real output
  bitrate (and a conservative track length) instead of a flat 8 MB/track,
  and completed downloads report their actual size to the SABnzbd history
  instead of a 100 MB placeholder.
- **Cover art / library writes to an unmounted LIDARR_PATH** now report one
  clear "not mounted — fix it in Settings" error and are skipped, instead of
  a confusing raw `Errno 13` while trying to create a host path (#71).
- **AcoustID no longer over-rejects good audio**: a near-perfect acoustic
  score (configurable `acoustid_accept_score`, default 0.98) is accepted
  even when the recording MBID differs (same track, different release),
  instead of being discarded as a mismatch (#58).
- **"Format not available" troubleshooting (#64)**: web-family clients
  (which actually consume PO tokens) are now tried before the default
  client when a PO token / bgutil provider is configured, and downloads
  log which player_client was used and the PO-token state on failure.
- **download-client grabs no longer get blocklisted by Lidarr**: a grab is
  refused only after a recent *client-job* failure, not after any manual or
  scheduler attempt; a user stop drops the job instead of reporting a
  failure; an empty result is reported as failed rather than a Completed
  job with no files.
- **No double imports**: the queue processor's client-vs-normal routing
  decision is now passed through explicitly, so it can't race the in-memory
  job registry.
- A failed enqueue during a grab now rolls the job back instead of leaving
  the album mapped but never downloaded.
- Newznab search falls back to the next-best match when the top match is
  excluded; release titles no longer show a literal `(None)` year; release
  dates are kept in UTC.
- Constant-time download-client API-key comparison.
- yt-dlp "format gated behind sign-in" hint is shown only when the final
  attempt was actually a format error.

### Changed
- `load_config()` caches the parsed config (invalidated on save) to avoid
  re-reading `config.json` on every Lidarr poll; cached album lookups use
  an indexed primary-key query.

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
