# Database: schema, id spaces, migrations, retention

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

### Database

State is stored in SQLite at `/config/lidarr-downloader.db`. Tables: `schema_version`, `track_downloads`, `download_logs`, `download_queue`, `banned_urls`, `candidate_attempts`, `missing_albums_cache`, `sync_state`, `download_client_jobs`, `album_source_hints`.

**`album_id` id space:** a positive `album_id` is a real Lidarr album id. YouTube playlist imports have no Lidarr album, so each import is assigned a **unique negative `album_id`** (`models.next_playlist_album_id()`), keeping its tracks distinct in the history / failed-track retry views. This negative space is disjoint from Lidarr's and must never be joined to Lidarr or sent to the Lidarr API (e.g. `download_client.py` assumes positive ids). Retry resolves a negative id's context from the stored `track_downloads` row instead of Lidarr.

Current schema version: **13**. Migrations:
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
- V11→V12: Added `album_source_hints` (`album_id` → YT Music `playlist_id` + `browse_id`), written when Explore adds an album so `process_album_download` downloads from that album playlist instead of searching for it. Positive `album_id`s only.

- V12→V13: Added `recording_id`, `video_id`, `file_path`, `file_size`, `reused_from` to `track_downloads` plus partial indexes on `recording_id` / `video_id`, and backfilled the successes (`recording_id` from `acoustid_recording_id`, `video_id` from the URL; ~1.2 s on a 58 k-success database, safe to run twice). `recording_id` is the MusicBrainz recording the track stands for (what Lidarr expects for new rows), `reused_from` the `track_downloads.id` a copied file came from (0 = downloaded).

**Retention:** after each successful Lidarr sync, `lidarr_sync._prune_old_history()` calls `models.prune_old_records()` with a cutoff of `HISTORY_RETENTION_DAYS` (90), or the retry cooldown if longer. `download_logs` and `candidate_attempts` go by age; a `track_downloads` row goes only when it is a failure a later success of the same track superseded — the remaining rows drive the backoff, and successes are how playlist watch and artist import know what is already imported, so never prune those by age.

Schema is versioned via `schema_version` table. **When changing the DB schema:**

1. Increment `SCHEMA_VERSION` in `db.py`
2. Add a migration function `migrate_vN_to_vN+1(conn)` in `db.py`
3. Register it in the `migrations` dict inside `_run_migrations()`
4. Test with `python3 -m pytest tests/test_db.py`
