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

### Module Structure

| Module | Responsibility |
|--------|---------------|
| `app.py` | Flask app, thin route handlers, startup |
| `db.py` | SQLite connection, schema, migrations |
| `models.py` | All SQL queries, CRUD, pagination |
| `downloader.py` | YouTube search/scoring/download via yt-dlp |
| `processing.py` | Album processing, queue processor, per-track state, skip handling |
| `metadata.py` | ID3 tagging, XML sidecar, iTunes API |
| `lidarr.py` | Lidarr API wrapper |
| `lidarr_sync.py` | Background paginated sync of Lidarr's missing albums into `missing_albums_cache` |
| `library.py` | Search MusicBrainz through Lidarr's lookup API and add artists/albums to Lidarr from the app (`/add`) |
| `notifications.py` | Telegram/Discord webhooks, Ntfy push |
| `config.py` | Config load/save, constants |
| `scheduler.py` | Scheduled polling/auto-download |
| `fingerprint.py` | AcoustID fingerprinting via fpcalc/chromaprint |
| `download_client.py` | Lidarr download-client bridge: Newznab indexer + SABnzbd client emulation (Flask blueprint) |
| `logutil.py` | Console log formatting: timestamp + level icon, consecutive-duplicate collapsing |
| `utils.py` | Shared utilities |

### Key data flows

1. Lidarr API (`/api/v1/wanted/missing`) → missing albums list shown in UI
2. User triggers download → `download_track_youtube()` searches YouTube, scores candidates, downloads best match via `yt-dlp` + ffmpeg
3. Post-download: metadata applied via `mutagen` (ID3 tags from MusicBrainz/iTunes APIs), optional XML sidecar written
4. Optional AcoustID fingerprinting via `fingerprint.py` (requires `fpcalc` binary and API key)
5. Per-track download results recorded in `track_downloads` table via `models.add_track_download()`
6. Lidarr import triggered via `/api/v1/command` (DownloadedAlbumsScan)

### Database

State is stored in SQLite at `/config/lidarr-downloader.db`. Tables: `schema_version`, `track_downloads`, `download_logs`, `download_queue`, `banned_urls`, `candidate_attempts`, `missing_albums_cache`, `sync_state`, `download_client_jobs`.

**`album_id` id space:** a positive `album_id` is a real Lidarr album id. YouTube playlist imports have no Lidarr album, so each import is assigned a **unique negative `album_id`** (`models.next_playlist_album_id()`), keeping its tracks distinct in the history / failed-track retry views. This negative space is disjoint from Lidarr's and must never be joined to Lidarr or sent to the Lidarr API (e.g. `download_client.py` assumes positive ids). Retry resolves a negative id's context from the stored `track_downloads` row instead of Lidarr.

Current schema version: **11**. Migrations:
- V1→V2: Replaced `download_history` + `failed_tracks` with `track_downloads` (per-track download records with YouTube URL, match score, duration, album/track metadata).
- V2→V3: Added AcoustID fingerprint columns to `track_downloads` (`acoustid_fingerprint_id`, `acoustid_score`, `acoustid_recording_id`, `acoustid_recording_title`).
- V3→V4: Added `banned_urls` table for tracking banned YouTube URLs per album/track.
- V4→V5: Added `candidate_attempts` table for per-candidate verification data. Added `track_title`, `track_number`, `track_download_id` columns to `download_logs`.
- V5→V6: Added `missing_albums_cache` and `sync_state` tables for paginated background sync of Lidarr's missing-albums list.
- V6→V7: Added `download_client_jobs` table so the Lidarr download-client bridge (SABnzbd `nzo_id` jobs) survives restarts; `download_client.restore_jobs()` reloads them at startup and re-queues interrupted downloads.
- V7→V8: Reassigned pre-existing YouTube playlist imports (recorded under the shared sentinel `album_id = 0`) to unique negative `album_id`s in `track_downloads` and `download_logs`, so old failed playlist tracks become retryable and distinct playlists stop colliding.
- V8→V9: Added `track_artist` to `track_downloads`, storing the per-track artist resolved via `search_artist_source` (MusicBrainz/iTunes), distinct from the Lidarr album-level `artist_name`, so compilation ("Various Artists") tracks can be searched/retried with their real artist.
- V9→V10: Added `source_format` to `track_downloads`, a human-readable summary of the YouTube source stream actually downloaded (format id · container · bitrate, e.g. `140 · m4a · 128 kbps`), for the per-track audio-quality report in the download history.
- V10→V11: Added `force` to `download_queue`, marking an entry as an explicit user request (manual "Add to Queue"). A forced entry bypasses the per-track retry backoff; the scheduler enqueues without it.

Schema is versioned via `schema_version` table. **When changing the DB schema:**

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a migration function `migrate_vN_to_vN+1(conn)` in `db.py`
3. Register it in the `migrations` dict inside `_run_migrations()`
4. Test with `python3 -m pytest tests/test_db.py`

### In-memory state

- `download_process` — current active download state (transient, not persisted). Contains per-track state: `tracks` list (each with `status`, `title`, `track_number`, `error`, `youtube_url`, `progress`, `speed`), `current_track_index`, album metadata. `TrackSkippedException` enables skip-track support during search and download.

### Config

Loaded from env vars + `/config/config.json`. File config overrides env vars, except `lidarr_url`, `lidarr_api_key` and `download_path` (`ENV_PREFERRED_KEYS`), where a non-empty env var wins. Saved via `save_config()`, which writes atomically (temp file + `os.replace`) and persists only `ALLOWED_CONFIG_KEYS` plus values that differ from the env; handlers that read-modify-write use `update_config(mutator)` so concurrent toggles don't lose updates. API input goes through `coerce_config_value()` (typed int/float/bool/list keys; invalid → HTTP 400). `GET /api/config` and the export never include `lidarr_api_key`. `ALLOWED_CONFIG_KEYS` whitelist controls what can be set via the API. Notable config keys beyond the basics: `concurrent_tracks`, `yt_cookies_file`, `yt_force_ipv4`, `yt_player_client`, `yt_retries`, `yt_fragment_retries`, `yt_sleep_requests`, `yt_sleep_interval`, `yt_max_sleep_interval`, `discord_enabled`, `discord_webhook_url`, `discord_log_types`, `acoustid_enabled`, `acoustid_api_key`, `download_client_enabled`, `download_client_api_key`, `download_client_category`, `yt_po_token` (manual yt-dlp PO token(s), comma-separated), `audio_normalize` (EBU R128 loudnorm, forces re-encode), `yt_pot_provider_url` (URL of a bgutil PO-token provider sidecar for automatic PO tokens; `bgutil-ytdlp-pot-provider` plugin is in requirements and a sidecar is wired in docker-compose), `search_artist_source` (per-track YouTube search artist: `album` default, or `mb_itunes`/`itunes_mb`/`mb`/`itunes` to resolve the real artist for compilations), `save_lyrics` (write a `.lrc` synced-lyrics sidecar per track, fetched from LRCLIB), `apply_replaygain` (measure loudness with ffmpeg and write ReplayGain track tags — non-destructive volume normalization), `track_retry_backoff` (default on; a track that keeps failing waits `scheduler_retry_after_hours * 2^(failures-1)`, capped at 30 days, instead of being retried every cycle forever — issue #90), `max_track_retries` (0 = never give up; otherwise drop a track after this many consecutive failures).

### Lidarr download-client bridge (`download_client.py`)

Optional feature letting Lidarr use this app as a native download path. A Flask blueprint exposes a **Newznab indexer** (`/api/newznab/api`: `t=caps`, `t=music`/`t=search`, `t=get`) and a **SABnzbd download client** (`/api/sabnzbd/api`: `version`, `get_config`, `fullstatus`, `queue`, `history`, `addfile`, `addurl`). A search is matched against `missing_albums_cache` to resolve a Lidarr `album_id`; the served NZB embeds that id; on `addfile` the id is parsed back out and enqueued via `models.enqueue_album()`. A job registry (keyed by SABnzbd `nzo_id`, in-memory write-through cache backed by the `download_client_jobs` table) tracks queued→downloading→completed/failed so Lidarr polls `queue`/`history` and imports the finished files itself; `restore_jobs()` reloads it at startup so a restart doesn't orphan a download. Grabbed albums are detected in `processing.process_album_download()` via `download_client.is_client_album()`, which skips the copy-to-library / `RefreshArtist` / cleanup steps; the queue processor routes them through `download_client.run_album_job()`. Both surfaces require `download_client_enabled` plus a matching `apikey`. To avoid infinite re-grab loops, in-flight albums and albums attempted within the retry cooldown (`scheduler_retry_after_hours`) are excluded from the indexer feed/search and refused at grab time; release `guid`s **and `pubDate`s** are time-bucketed on that window so retries are still possible after the cooldown (Lidarr's blocklist matches title + publish date, not guid). A SABnzbd `history` delete of a finished job inside the cooldown keeps its row as `completed_removed`/`failed_removed` (hidden from queue/history/restore, still counted by the cooldown; purged by `restore_jobs()` once the cooldown passes). Deleting a *downloading* job sets `stop` on its state. A queued client grab whose album is already downloading in the foreground waits in the queue instead of being dropped.

### Threading

Downloads run in background threads. `queue_lock` (threading.Lock) in `processing.py` protects shared state.

### Scheduler

Optional `schedule` library job polls for missing albums and auto-downloads at configured intervals. Albums attempted within `scheduler_retry_after_hours` are skipped (`models.get_attempted_album_ids_since`, which keys off **any** `download_logs` row for the album — so every early exit in `process_album_download` must still write a log, or the album is re-queued every cycle; use `processing._log_album_error()`). Failures recorded as host problems (`error_message` starting with `models.HOST_FAILURE_PREFIX`, "Audio postprocessing failed") are not counted toward a track's consecutive failures. `scheduled_check()` and the scheduler loop catch and log exceptions so one bad cycle can't kill the thread. `processing.any_download_active()` (foreground **or** any download-client job) gates `/api/restart` and `/api/backup/import`.

**Retry backoff (issue #90):** `process_album_download` decides what to download *before* any network work — it computes `album_path`, calls `_compute_deferred_tracks()` + `_filter_tracks()`, and returns "Skipped" early if nothing is left. Tracks that keep failing back off exponentially (`scheduler_retry_after_hours * 2^(failures-1)`, capped at 30 days) from their consecutive-failure count in `track_downloads` (`models.get_track_failure_counts`), so a song that simply isn't on YouTube stops costing a cover-art fetch, per-track artist lookups and a YT Music resolution every cycle. Manual queue adds (`download_queue.force`) and Lidarr grabs (`client_grab`) bypass the backoff.

### Logging

`logutil.setup_logging()` (called from `app.py`) installs a console formatter
for the `docker compose logs` stream: `HH:MM:SS`, then the message. Warnings
and errors get an icon, which makes their line protrude rather than adding a
column everything else has to pay for. Milestone messages (ready, album start,
album complete) carry an inline icon from `logutil.ICON_*`. A `DedupeFilter`
collapses consecutive identical lines and reports the streak as its own INFO
line — background loops used to repeat the same sentence hundreds of times.
A streak that never ends is still reported every 100 repeats or 60 s.
Prefer fixing repetition at the source (log at DEBUG when nothing changed,
as `lidarr_sync` does) and treat the filter as a safety net.

`logutil.section(logger, ..., icon=...)` opens a visual block: the line is
preceded by a blank line, so startup and each album run read as separate
paragraphs rather than one flat scroll. `logutil.milestone()` adds an icon
without the break. Messages that belong *inside* an album run are prefixed
with three spaces so they sit under its header — keep that convention when
adding album-flow logging; a line carrying an icon has that indent stripped,
so icons always sit one space from their text and protrude from the flow.

Each track of an album run is processed inside `logutil.track_label("02")`
(a `contextvars` label copied onto records by `TrackLabelFilter`), and the
formatter prints it after the indent/icon: `   [02] Search phase: …`. Tracks
download in parallel, so without it their lines interleave anonymously. A
thread started inside a track must run in `contextvars.copy_context()` to
keep the label (`_download_candidate_threaded` does).

**Icons must be East_Asian_Width "W" (exactly two columns).** The alignment
depends on it, and a test enforces it. This is why the warning icon is not
the obvious "⚠" (U+26A0): that codepoint is Ambiguous width, so terminals
disagree and it left a visible gap.

### ffmpeg capability diagnostics

Some hosts cannot run ffmpeg's audio conversion at all — most often the
container is under CPU emulation (an arm64 image on an amd64 host or vice
versa), where ffmpeg fails with ENOSYS ("Function not implemented") while
everything else works. `downloader._probe_ffmpeg_can_write_audio()` detects
it by running **both shapes yt-dlp uses** on the download filesystem —
encoding 0.1s of silence to m4a, then stream-copying that m4a into another
one — each **with `-movflags +faststart`**, the flag yt-dlp appends to every
output it writes. All three details matter: a host can fail only the
stream-copy (the path a native YouTube m4a takes), and omitting faststart
tests a different code path again. Either omission makes the probe report
healthy on a host where downloads fail.

A probe can only ever approximate, so **a failed real conversion outranks
it**: `_mark_ffmpeg_postprocess_broken()` sets `_ffmpeg_pp_observed_broken`,
and `?refresh=1` deliberately cannot clear that — otherwise Re-check would
erase measured knowledge and report "works" until the next download failed.
It resets on restart, which is what happens when the image is fixed.

`downloader.ffmpeg_status()` turns that into guidance and is served by
`/api/ffmpeg/status` (`?refresh=1` re-probes) and summarised as `ffmpeg_ok`
in `/api/health`. Settings renders it as a panel that is always
visible and always states a verdict — hiding it when healthy made the
Re-check button look like it had broken something. Key distinction: with an `m4a` target
(`NATIVE_AUDIO_FORMATS`) downloads still work, because YouTube serves that
container directly and the native stream is kept as-is; `opus` is served inside
WebM and needs ffmpeg to remux, so it is **not** native; with `mp3`/`opus` nothing
can be downloaded, so the panel offers switching to m4a as the first fix.

A failed conversion only marks ffmpeg broken when the error is host-wide:
the message says "function not implemented", or a **fresh** probe run right
after the failure also fails (`_conversion_failure_is_host_wide()`). A corrupt
source stream fails only that candidate, and "no space left on device" never
marks ffmpeg broken. `audio_normalize` is a separate ffmpeg loudnorm pass after
the download (`_normalize_loudness()`: −14 LUFS, re-encoded to the target codec
at `audio_quality`); if it fails the un-normalized file is kept and ffmpeg is
never marked broken.

### YouTube cookies and unavailable videos

yt-dlp is **never** handed the user's `yt_cookies_file`:
`downloader.cookiefile_for_ytdlp()` validates it (`inspect_cookies_text()`:
Netscape rows of 7 tab-separated fields, `#HttpOnly_` allowed, JSON
rejected), adds the `# Netscape HTTP Cookie File` header when missing, and
returns a private per-thread copy. yt-dlp rewrites its cookie file on every
close without locking, so a shared file broke parallel runs ("does not look
like a Netscape format cookies file") and could overwrite a signed-in export
with a rotated jar. An unusable file is skipped with one warning, and
`/api/cookies/status` reports `valid`/`entries`/`reason`. Every place that
builds yt-dlp options (including `app.py`) must go through it.

`download_youtube_candidate()` classifies extraction errors with
`_classify_unavailable()`: an age gate without cookies, or a private /
removed / members-only / region-locked video, stops immediately; an age gate
with cookies tries each player client once. The result carries
`unavailable: True` and a one-line hint instead of ~40 retries.

### Album-first matching

`match_album_track()` matches by title (bracket-insensitive, closest duration
wins ties) and otherwise **by position** when the YT Music album has as many
tracks as Lidarr's and the entry at the same position (`processing.
_album_position()`: medium, then track number) has the same duration
(±max(5 s, 5 %)). A positional match is marked `matched_by: "position"` and is
**not** flagged `from_official_album`, so an AcoustID mismatch rejects it.
When the album candidate fails (download error, mismatch), `_download_tracks`
lazily extends the candidate list with a per-track search, skipping the same
video id. An unverified file is kept on disk as the fallback rather than
deleted and downloaded again. In the search, generic titles (`_is_generic_title`:
Intro, Outro, Interlude, Skit, [untitled]…) require the artist's channel,
YT Music artist credit or name in the title, and phase 2 reuses phase 1's
fetched results.

### MusicBrainz id frames

Lidarr's track resource carries **two different** MusicBrainz ids, and they are
not interchangeable: `foreignRecordingId` identifies the recording, while
`foreignTrackId` identifies that recording's slot in one specific release's
tracklist. Writing the recording id into the release-track tag makes taggers
that validate it against the release tracklist reject the file outright
(issue #93), so `metadata._musicbrainz_fields()` is the single source of truth
for the mapping and both the MP3 and M4A paths go through it (`mp4=True`
selects the MP4 key names).

Frame names follow **Picard's** mapping, because that is what other taggers
read — `MusicBrainz Release Group Id` (not "Album Release Group Id") and
`MusicBrainz Album Release Country` (not "Release Country"). `_STALE_MB_DESCS`
lists the descriptions earlier versions got wrong; they are deleted before
writing so re-tagging an existing file cannot leave a mislabeled value behind.
For MP4 Picard stores the **recording** id under
`----:com.apple.iTunes:MusicBrainz Track Id` (the release-track id stays under
`MusicBrainz Release Track Id`); `_STALE_MP4_MB_DESCS` adds the old
`MusicBrainz Recording Id` key to the delete list.

Vorbis Comments (Opus) invert the ID3 naming: on disk `musicbrainz_trackid`
holds the **recording** id and `musicbrainz_releasetrackid` holds the
release-track id, so `tag_opus()` deliberately does not mirror `tag_mp3()`.

### Adding artists and albums (`library.py`)

Everything goes through Lidarr's own API, so Lidarr stays the single source
of truth and its metadata server does the MusicBrainz lookups:
`GET artist/lookup` / `album/lookup?term=` (a `lidarr:<mbid>` term resolves
one id), `POST artist`, `POST album` (Lidarr adds an unknown artist itself;
we send it with `addOptions.monitor = "none"` so only that album is
monitored), and `PUT album/monitor` for an album already in the library but
unmonitored. An item counts as "in library" when the lookup returns an `id`
> 0. The server re-looks up the MBID instead of trusting a client payload and
validates root folder / profiles against Lidarr (the reserved "None"
metadata profile is hidden). It never asks Lidarr to search its indexers
(`searchForMissingAlbums` / `searchForNewAlbum` are false): downloads are
this app's job. *Download now* starts `queue_when_ready()`, a thread that
polls `GET track?albumId=` until Lidarr has loaded the tracklist (up to 4
minutes) and then `models.enqueue_album(force=True)`; its state is served by
`/api/library/pending`. Each add also schedules `lidarr_sync.trigger_sync()`
after 15 s and 90 s so the Library shows the new missing albums.
`lidarr_request()` supports `PUT` and turns Lidarr's 400 validation list into
a readable `"Lidarr rejected the request: …"` error.

### Notifications

Telegram, Discord webhooks, and Ntfy push notifications, filtered by `log_type` (e.g., `partial_success`, `album_error`). Ntfy is called with a JSON body on the **server root** (`POST {ntfy_url}/`, topic inside the JSON — ntfy ignores JSON posted to `/<topic>`) and an integer priority (1–5). Exception messages are logged through `_redact()` so bot tokens / webhook tokens never reach the logs.

## Templates

Every page is built on one design system; **don't add per-page colour
variables, theme toggles, navigation or Font Awesome**.

- `static/components.css` — the design system. Tokens on `:root` (4px
  spacing scale `--sp-*`, radii `--r-*`, type scale `--text-*`, layered
  surfaces `--bg-base/-elevated/-elevated-2/-sunken/-overlay`, text
  `--text-1/2/3`, one accent `--accent*`, semantic `--success/warning/
  danger/info` with `-mark` and `-soft` variants, shadows `--shadow-1..3`,
  motion `--ease` = `cubic-bezier(0.32, 0.72, 0, 1)` and `--dur-*`). Dark
  values are declared twice: under `@media (prefers-color-scheme: dark)`
  guarded by `:root:not([data-theme="light"])`, and under
  `:root[data-theme="dark"]` (manual override) — change both. Components
  are `ui-*` (buttons, badges, fields, `.ui-switch`, `.ui-segmented`,
  `.ui-check`, chips, cards, iOS grouped lists `.ui-group`, stat tiles,
  responsive tables that become cards on phones, lists with a coloured
  status edge, album cards, per-track progress `.ui-track`, now-playing
  card, empty states, skeletons, callouts, modals that become bottom
  sheets on phones, stacked toasts, bulk bar, drop zone, steps, tabs,
  tooltip). `prefers-reduced-motion` collapses all durations. Contrast
  targets WCAG AA in both themes. Under `(max-width: 720px), (pointer:
  coarse)` controls grow to touch-sized hit targets (36–42px); keep new
  controls on the shared classes so they inherit this. Frosted surfaces use
  `--bg-overlay` (high opacity on purpose, so text behind never competes).
- `static/app.js` — `window.UI`: theme (`auto`/`light`/`dark`, stored in
  `localStorage.theme`, `themechange` event, `theme-color` meta kept in
  sync), `UI.icon(name)`, `UI.escape`, `UI.toast(msg, {type, action})`,
  `UI.openModal/closeModal` (Esc, backdrop, focus trap/restore),
  `UI.confirm({...})` → Promise, `UI.guard(btn, fn)` (in-flight guard +
  spinner), `UI.fetchJSON`, `UI.poll(fn, ms)` (never overlaps, pauses in
  hidden tabs), nav badges from `/api/stats` (`queued`, `active`), and the
  `/` shortcut that focuses the page's `input[type="search"]`.
- `static/icons.svg` — SVG symbol sprite (`#i-<name>`), used as
  `<svg class="ico"><use href="/static/icons.svg#i-name"></use></svg>`.
- `templates/_head.html` (meta, manifest, CSS, pre-paint theme script,
  app.js), `templates/_nav.html` (sidebar + phone tab bar; set
  `active_page` before including), `templates/_topbar.html` (phone header).
- `templates/index.html` — Library: status tiles, missing-albums grid /
  list / table, search and sort. Per-album checkboxes drive the floating
  **bulk-action bar** (`/api/download/queue/bulk`); `selectedAlbums` Set
  survives view re-renders.
- `templates/downloads.html` — now-downloading card with per-track states,
  reorderable queue, history filtered by outcome and audio quality.
- `templates/insights.html` — analytics (`/insights`); fetches
  `/api/insights?days=N` and draws dependency-free inline-SVG charts
  rendered at the measured container width (fixed-size labels, tooltips,
  legend, table view). Aggregation is `models.get_insights()`.
- `templates/logs.html` — download log entries with type filters and
  inline retry/dismiss/unban.
- `templates/settings.html` — configuration in anchored sections, incl.
  **Backup & Restore** (`/api/backup/export` streams a `sqlite3`-consistent
  copy of the DB; `/api/backup/import` validates the upload, atomically
  replaces the DB, and restarts — refused while any download is active).
- `templates/youtube.html` — manual YouTube URL / playlist import.
- `templates/add.html` — **Add music** (`/add`): search artists or albums
  (`/api/library/search?type=artist|album&term=`), then add them to Lidarr
  in a sheet with root folder, quality and metadata profile (defaults from
  the root folder, last choice remembered in `localStorage.addMusicPrefs`),
  artist monitor / new-release options, and for albums a *Download now*
  switch. Reachable from the sidebar and the Library header; the phone tab
  bar has no room for it, so `_nav.html` skips the `add` item there and
  highlights Library instead. The Library accepts `/?q=` to pre-fill its
  search.
- `templates/setup.html` — first-run setup wizard (`/setup`, no nav); the
  dashboard redirects unconfigured instances here (client-side, skippable).
- `static/favicon.svg` + PNG icons (`apple-touch-icon.png`, `icon-192.png`,
  `icon-512.png`, `icon-maskable-512.png`).
- PWA: `/manifest.webmanifest` and `/sw.js` are served from the app root;
  `_head.html` links the manifest and `app.js` registers the (no-op-fetch)
  service worker so the UI is installable.

## Utility Tools (`tools/`)

Standalone scripts not part of the main app:
- `fix_metadata.py` — batch fix ID3 tags on existing files
- `list_missing.py` — CLI to list missing albums from Lidarr
- `migrate_directories.py` — migrate album directory structure
- `migrate_json_to_db.py` — migrate JSON state files to SQLite (one-time upgrade)
- `verify_fingerprints.py` — AcoustID fingerprint verification tool

## Key Dependencies

- `yt-dlp` — YouTube search and download
- `mutagen` — MP3 ID3 tag reading/writing
- `Flask` + `gunicorn` — web server
- `schedule` — optional cron-style scheduler
- `ffmpeg` (system package, not pip) — audio conversion
- `fpcalc`/chromaprint (optional system package) — AcoustID fingerprinting

## Version Updates

The version string is defined in `version.py`: `VERSION = "2.1.0"`. The README badge also references it and must be updated manually.

## Persistence Volume

The `/config` directory must be writable. It stores `config.json` and `lidarr-downloader.db` (SQLite database). In Docker, mount a volume here to persist settings across container restarts.

## Testing

Run tests with the venv:

```bash
source .venv/bin/activate && python -m pytest tests/ -v
```

Tests are in `tests/` directory mirroring module structure: `test_db.py`, `test_models.py`, `test_config.py`, `test_utils.py`, `test_notifications.py`, `test_lidarr.py`, `test_metadata.py`, `test_downloader.py`, `test_routes.py`, `test_processing.py`, `test_fingerprint.py`, `test_migrate_tool.py`, `test_download_client.py`, `test_scheduler.py`, `test_lidarr_sync.py`, `test_logutil.py`, `test_app_paths.py`, `test_fix_metadata_tool.py`, `test_library.py`.
