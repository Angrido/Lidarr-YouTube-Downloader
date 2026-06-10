# Changelog

## 1.8.3

### Fixed
- **Cookies "Test" no longer misreads real exports as logged out**: the
  signed-in check parses the file with yt-dlp's own cookie jar, which
  understands the `#HttpOnly_` line prefix browsers and yt-dlp use for
  HttpOnly cookies — `LOGIN_INFO`, the login marker, is one of them.
- **The yt-dlp Format Override is honored by the manual track download
  path too** (it previously always forced `bestaudio/best` — thanks
  @Gazz1e), and the override now rides the first selector with a
  slash-fallback (`141/bestaudio/...`): a video that doesn't expose the
  format falls back in the same request instead of sweeping every player
  client per track. Web-family clients — the ones that expose premium
  formats — are tried first when an override is set, and the "List
  formats" tester walks the exact client chain the download path uses.
  With no override the behavior is unchanged.
- **Stopping a download no longer discards a manual/scheduler album's
  finished tracks**: the "drop everything, report nothing" stop semantics
  now apply only to Lidarr download-client grabs; manual downloads import
  and log the tracks that completed, as before. The engine reports a stop
  consistently on every exit path, so a stopped client grab can never
  surface as a blocklist-worthy failure.
- **Queue dispatch no longer blocks head-of-line**: a non-client album
  waiting for the busy foreground slot doesn't hold back client albums
  (with free concurrency slots) queued behind it.
- **Background client jobs are visible on the dashboard** when the
  foreground is idle, and the skip-track button targets the download being
  shown. Client jobs always run in their own state container, created from
  a single state factory so per-download fields can't go stale.
- Hot-path and duplication cleanups: the Newznab/SABnzbd endpoints load
  the config once per request (and the indexer auto-refresh checks its
  debounce before touching the config); SABnzbd queue progress is a cheap
  status tally instead of a deep copy under the queue lock; the
  retry-cooldown window comes from one shared helper across the scheduler,
  feed exclusion, grab refusal and release-guid bucketing.

## 1.8.2

### Added
- **Configurable yt-dlp format** (Settings → "yt-dlp Format Override", env
  `YTDLP_FORMAT`): force a specific stream such as `141` (256 kbps AAC) for
  higher-quality audio with a YouTube Premium account, instead of the
  default best-audio selection. The override is tried first and the
  built-in smart selectors remain as a fallback when the requested format
  isn't available for a video (#58, building on Gazz1e's `supportformat141`
  fork). A "List formats" tester sits next to the field: paste a YouTube
  URL/ID and it shows that video's available audio format IDs (codec,
  bitrate, size); click one to drop it into the override.
- **Concurrent album downloads in Download Client mode** (Settings → Lidarr
  Download Client → "Concurrent Album Downloads", env
  `DOWNLOAD_CLIENT_CONCURRENT_ALBUMS`, default 1): download several
  Lidarr-grabbed albums at once (1–5). Each job tracks its own per-track
  progress so concurrent downloads don't collide, and Lidarr's SABnzbd
  queue still reports per-album status.

### Changed
- **Library auto-refresh on indexer activity**: when Lidarr RSS-syncs or
  searches the Newznab indexer — which happens right after a new artist or
  album is added — a background missing-albums sync is triggered
  (debounced, and only when Lidarr is configured) so newly-added albums
  show up in the app and the indexer feed without waiting for the periodic
  sync loop.

### Fixed
- **Forbidden-word filtering is now robust and case-insensitive**: built-in
  and custom words are merged, stripped, lower-cased and de-duplicated
  through a single helper, so a built-in/API/env word with stray casing or
  whitespace is honored (and a null value no longer risks breaking the
  search path). The default list is consolidated into one constant and
  aligned with the Settings UI (now includes "reaction").

## 1.8.1

### Fixed — Lidarr download-client bridge
- **RSS grabs are no longer rejected as "larger than maximum allowed
  size"**: the Newznab feed estimates release size from the configured
  output bitrate and a conservative track length instead of a flat
  8 MB/track, and finished downloads report their real size to the SABnzbd
  history instead of a 100 MB placeholder.
- **The indexer feed no longer goes empty after a manual/scheduler
  download**, which made Lidarr's indexer test report "no results in the
  configured categories": the feed now hides only albums the download
  client itself handled within the retry cooldown (plus in-flight ones),
  not every album with a recent log.
- **Grabs no longer get blocklisted by Lidarr**: a grab is refused only
  after a recent *client-job* failure (not a manual/scheduler attempt); a
  user stop on any stage drops the job instead of reporting a failure; an
  empty result is reported as failed rather than a "Completed" job with no
  files.
- **No more double imports**: the queue processor passes its
  client-vs-normal routing decision through explicitly, so it can't race
  the in-memory job registry.
- A failed enqueue during a grab now rolls the job back instead of leaving
  the album mapped but never downloaded.
- Newznab search falls back to the next-best match when the top match is
  excluded; release titles no longer show a literal `(None)` year; release
  dates parse full ISO timestamps and stay in UTC.
- Constant-time comparison for the download-client / indexer API key.

### Fixed — YouTube downloads & authentication
- **Cookies "Test" now verifies a real YouTube login**: it requires a
  `LOGIN_INFO` cookie scoped to `youtube.com` (a google.com-only export is
  treated as logged out by YouTube) and tells you to re-export from a
  youtube.com tab when it's missing — so age-restricted ("Sign in to
  confirm your age") tracks can actually be downloaded.
- **PO-token provider "Test" now does a real check**: it queries the bgutil
  provider's `/ping`, confirms the response is genuinely a bgutil provider
  and reports its version, instead of reporting success for any HTTP
  response (even a 404 or an unrelated server).
- **Better PO-token handling for "format not available" (#64)**: web-family
  clients (the only ones that consume PO tokens) are tried before the
  default client when a manual token or bgutil provider is configured, and
  downloads log which `player_client` succeeded plus the PO-token state on
  failure.
- The "format gated behind sign-in" hint is shown only when the final
  attempt actually was a format error.

### Fixed — audio quality
- **AcoustID no longer over-rejects good audio**: a near-perfect acoustic
  score (configurable `acoustid_accept_score`, default 0.98) is accepted
  even when the recording MBID differs (same track, different
  release/edition), instead of being discarded as a mismatch (#58).

### Fixed — library & paths
- **Cover art / library writes to an unmounted `LIDARR_PATH`** now report
  one clear "not mounted — fix it in Settings" error and are skipped,
  instead of a confusing raw `Errno 13` from trying to create a host path
  (#71).

### Changed
- `load_config()` caches the parsed config (invalidated on save, and not
  cached in env-only mode) to avoid re-reading `config.json` on every
  Lidarr poll; single-album lookups use an indexed primary-key query.
- New configurable keys: `acoustid_accept_score`.

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
