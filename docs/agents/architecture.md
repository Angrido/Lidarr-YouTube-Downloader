# Architecture: modules, data flows, in-memory state, threading

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

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
| `dedupe.py` | Never fetch the same recording twice: `plan_album()` decides, per track still pending, whether it is a repeat of another position of the same release, already downloaded for this album, or reusable from a verified file of another album; `processing` does the copying (`_apply_dedupe_reuse`, `_copy_repeat_positions`, `_copy_track_for_album`). See *Never fetch the same recording twice* |
| `lidarr_sync.py` | Background paginated sync of Lidarr's missing albums into `missing_albums_cache` |
| `queue_eta.py` | Time left on the queue: `estimate()` (cached 15 s) → `GET /api/queue/eta`. See *Queue ETA* |
| `library.py` | Lidarr lookup / add API used by Explore: MusicBrainz search through Lidarr, add artists/albums, monitor, queue when the tracklist is ready |
| `explore.py` | Explore catalog: shared YT Music client, TTL cache, normalizers + raw-shelf fallback parser, YT Music → MusicBrainz/Lidarr mapping, add / YouTube-import actions (`/explore`) |
| `explore_routes.py` | Explore blueprint: the `/explore` page, every `/api/explore/*` route with its rate-limit bucket (`_explore_json`), and the in-memory artist-import runner (`_artist_imports`, `_run_artist_import`) |
| `notifications.py` | Telegram/Discord webhooks, Ntfy push, Apprise |
| `auth.py` | Optional login: `before_request` guard, `/login`, `/api/auth/*`, API key, session key in `/config/secret_key` |
| `external_playlists.py` | Spotify / Deezer / Apple Music / ListenBrainz playlist and album readers for the YouTube page, and the "Add albums to Lidarr" job |
| `config.py` | Config load/save, constants |
| `scheduler.py` | Scheduled polling/auto-download |
| `fingerprint.py` | AcoustID fingerprinting via fpcalc/chromaprint |
| `vpn.py` | Optional gluetun integration: `vpn_proxy_url` → yt-dlp / `requests` proxy for YouTube traffic, control-server client (public IP, status, restart), rotation triggers and the `/api/vpn/{status,test,rotate}` blueprint |
| `youtube_import.py` | Import blueprint: `/import` page (`/youtube` 301-redirects there, keeping the query), `/api/youtube/playlist/info`, `/api/youtube/playlist/download`, `/api/youtube/recent`, the playlist download runner (`_execute_playlist_download`, shared with Explore imports) and the watched-playlist loop |
| `manual_download.py` | Manual-download blueprint: `POST /api/download/manual` (retry a failed track, incl. negative-id playlist imports) and `POST /api/album/<id>/track/manual-download` (with live progress), the shared `_manual_dl_core` (download, tag, AcoustID, record, notify, `RefreshArtist`), album path builders and the Lidarr album cache (`_get_album_cached`, also used by the queue routes) |
| `download_client.py` | Lidarr download-client bridge: Newznab indexer + SABnzbd client emulation (Flask blueprint) |
| `logutil.py` | Console log formatting: timestamp + level icon, consecutive-duplicate collapsing |
| `ytdlp_update.py` | yt-dlp updates without pip (the image has none): `install()` unpacks the PyPI wheels of yt-dlp and its pinned yt-dlp-ejs (SHA-256 checked, paths kept inside) into `/config/ytdlp`; `activate()`, called at the top of `app.py` before anything imports `yt_dlp`, puts that folder first on `sys.path` when it is newer than the image's copy and deletes it otherwise. Used by the startup update and `POST /api/ytdlp/update` (`app._update_ytdlp`) |
| `utils.py` | Shared utilities |

### Key data flows

1. Lidarr API (`/api/v1/wanted/missing`) → missing albums list shown in UI
2. User triggers download → `download_track_youtube()` searches YouTube, scores candidates, downloads best match via `yt-dlp` + ffmpeg
3. Post-download: metadata applied via `mutagen` (ID3 tags from MusicBrainz/iTunes APIs), optional XML sidecar written
4. Optional AcoustID fingerprinting via `fingerprint.py` (requires `fpcalc` binary and API key)
5. Per-track download results recorded in `track_downloads` table via `models.add_track_download()`
6. Lidarr import triggered via `/api/v1/command` (DownloadedAlbumsScan)

### In-memory state

- `download_process` — current active download state (transient, not persisted). Contains per-track state: `tracks` list (each with `status`, `title`, `track_number`, `error`, `youtube_url`, `progress`, `speed`), `current_track_index`, album metadata. `TrackSkippedException` enables skip-track support during search and download.

### Threading

Downloads run in background threads. `queue_lock` (threading.Lock) in `processing.py` protects shared state.

A foreground album's `download_queue` row is not deleted when it starts: `_dispatch_next_from_queue()` marks it `downloading` and `_run_queued_album()` removes it when the run ends (or sets it back to `queued` on "Busy", or when a Lidarr grab for the same album landed mid-run). So a restart mid-download re-queues it via `models.reset_downloading_to_queued()`. The queue list and count (`get_queue_details`, `get_queue_length`) show only `queued` rows; `get_queue()` returns every row, which the scheduler uses to skip albums already in flight. Download-client grabs are still dequeued at dispatch — `download_client_jobs` is their record.

**SQLite connections** (`db.get_db()`): one per thread, kept for the thread's whole life, WAL, 30 s busy timeout. They are `db._Connection`s: a statement that fails with "database is locked" rolls the open transaction back. Without that, Python's implicit transaction stayed open after the failed write, the next read (the dispatcher's `get_queue()`) pinned a WAL snapshot, and once another connection committed every write on that connection failed instantly with the same error — for the dispatcher thread that meant the queue stopped until a restart (`Queue processor error: database is locked` every 2 s in the log). Other errors leave the transaction alone, so a query error never undoes earlier uncommitted work. The same failure logs which other threads hold a write transaction open (`db.lock_holders()`: age, running or idle in the transaction, last statement, where the thread is; at most one report every 10 s), because the timeout alone does not name the holder; the registry behind it is `db._open_writes`, kept in step by `_Connection.execute/executemany/commit/rollback/close`. `PRAGMA journal_size_limit` (64 MB) keeps the WAL file from only ever growing.
