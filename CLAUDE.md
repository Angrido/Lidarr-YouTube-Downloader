# CLAUDE.md

Flask app (port 5000, shipped as a Docker image) that reads Lidarr's missing
albums, finds the tracks on YouTube with `yt-dlp`, downloads + tags them
(`mutagen`) and hands them back to Lidarr. State: SQLite in `/config`.
Branches: work on `beta`, PRs to `main`.

## Commands

```bash
pip install -r requirements.txt pytest pyflakes
python -m pytest tests/ -q                      # full suite (CI: Python 3.12 + newest 3.x)
python -m pytest tests/test_processing.py -q    # one module: tests/test_<module>.py
python -m pyflakes $(git ls-files '*.py')       # CI lint, must be clean
python tools/explore_preview.py [--demo]        # UI on :5099 with faked YT Music + Lidarr, never downloads
```

`tests/test_frontend_js.py` runs `tests/js/*.test.js` under node (skipped
without node). UI smoke: `npx -y -p playwright node tools/ui_smoke.js` with
the `--demo` preview running. Real run / Docker / env vars: `docs/agents/ops.md`.

## Where things live

| Module | Job |
|---|---|
| `app.py` | Flask app, thin routes, startup (calls `ytdlp_update.activate()` before anything imports `yt_dlp`) |
| `processing.py` | Album runs, queue dispatcher, per-track state, skip, retry backoff |
| `downloader.py` | YouTube search / scoring / download, ffmpeg probe, cookies |
| `models.py` / `db.py` | All SQL / connection, schema, migrations |
| `metadata.py` | ID3/MP4/Vorbis tags, sidecars, cover + artist image, `apply_file_extras()` |
| `dedupe.py` | Reuse files of the same MusicBrainz recording instead of re-downloading |
| `lidarr.py`, `lidarr_sync.py`, `library.py` | Lidarr API, missing-albums cache sync, add artist/album |
| `explore.py`, `explore_routes.py` | `/explore` catalog over ytmusicapi + Lidarr mapping, artist imports |
| `youtube_import.py`, `external_playlists.py`, `manual_download.py` | `/import` playlists (YouTube/Spotify/Deezer/Apple/ListenBrainz), watched playlists, manual/retry downloads |
| `download_client.py` | Newznab indexer + SABnzbd emulation so Lidarr can grab through the app |
| `vpn.py` | gluetun proxy for YouTube traffic + IP rotation |
| `scheduler.py`, `queue_eta.py` | Periodic auto-download, queue time-left estimate |
| `config.py`, `auth.py`, `notifications.py` | Config load/save/validation, optional login, Telegram/Discord/Ntfy/Apprise |
| `fingerprint.py`, `logutil.py`, `ytdlp_update.py`, `utils.py`, `version.py` | AcoustID, console logging, yt-dlp self-update, helpers, version |
| `templates/`, `static/` | Jinja pages on one design system (`static/components.css`, `static/app.js` = `window.UI`) |
| `tools/` | Standalone scripts (preview server, screenshot/smoke Playwright scripts, migration/fix CLIs) |

## Read before changing an area (on demand, not every session)

| Touching | Read |
|---|---|
| module map, data flow, threads, queue rows | `docs/agents/architecture.md` |
| schema, `album_id` id spaces, retention | `docs/agents/database.md` |
| config keys, secrets masking, login, notifications | `docs/agents/config.md` |
| VPN / gluetun / IP blocks / rotation | `docs/agents/vpn.md` |
| Newznab / SABnzbd bridge | `docs/agents/download-client.md` |
| scheduler, retry backoff, queue ETA | `docs/agents/scheduler-and-queue.md` |
| console log format | `docs/agents/logging.md` |
| ffmpeg probe, cookies, unavailable videos, album/track matching | `docs/agents/downloader.md` |
| duplicate-recording reuse | `docs/agents/dedupe.md` |
| tags, multi-disc numbering, MusicBrainz id frames | `docs/agents/metadata.md` |
| Explore, library adds, external playlists, Lidarr tags, For you | `docs/agents/explore-and-library.md` |
| templates, CSS tokens, `static/*.js` | `docs/agents/frontend.md` |
| Docker, deps policy, tools, version bump | `docs/agents/ops.md` |
| manual UI/API checklist | `TESTING.md` |

## Rules that break things when missed

- **Schema change:** bump `SCHEMA_VERSION` in `db.py`, add `migrate_vN_to_vN+1(conn)`, register it in `_run_migrations()`, run `tests/test_db.py`. Update the schema list in `docs/agents/database.md`.
- **Negative `album_id`** = YouTube playlist import. Never join it to Lidarr or send it to the Lidarr API.
- **Every early exit of `process_album_download`** writes a log (`processing._log_album_error()`), or the scheduler re-queues the album every cycle.
- **New yt-dlp / YouTube `requests` call site:** options via `_build_common_opts` or `vpn.ytdlp_opts()` / `vpn.requests_proxies()`, cookies only via `downloader.cookiefile_for_ytdlp()` (never the user's file), and add a test to `tests/test_vpn.py::TestProxyCallSites`.
- **Every download path** (Lidarr album, manual/retry, YouTube import) calls `metadata.apply_file_extras()` on the final file and records `dl_result["source_format"]`.
- **New config key:** add to `ALLOWED_CONFIG_KEYS`, typed in `coerce_config_value()`; secrets go in `SECRET_CONFIG_KEYS` (masked, kept when the mask is POSTed back). Auth keys never go in `ALLOWED_CONFIG_KEYS`.
- **MusicBrainz ids:** `foreignRecordingId` ≠ `foreignTrackId`; map them only through `metadata._musicbrainz_fields()`.
- **Never prune `track_downloads` successes by age** (playlist watch and artist import rely on them).
- **Frontend:** no per-page colour variables, theme toggles, navigation or Font Awesome; accent colours derive from `--accent-base`; dark tokens are declared twice in `components.css` (change both); no backdrop blur on `.ui-album`.
- **Log icons** must be East_Asian_Width "W" (a test enforces it); album-run lines are indented three spaces; threads started inside a track run in `contextvars.copy_context()`.
- **Dependencies are unpinned on purpose** (latest everything). Hold one back only with `pkg<X` plus a comment saying why and when to lift it.
- **Release:** bump `version.py`, add a `## X.Y.Z` section at the top of `CHANGELOG.md`, update the README badge. Current: 2.0.2 being prepared on `beta` (2.0.1 published).

## Don't read whole

- `templates/settings.html` (176 KB), `index.html`, `downloads.html`, `explore.html` (100–120 KB each), `static/components.css` (85 KB), `processing.py` (128 KB), `downloader.py` (95 KB): search for the symbol, then read a line range.
- `CHANGELOG.md` (58 KB): only the top section matters.
- `tests/fixtures/ytmusic/` (captured API JSON), `docs/screenshots/`, `static/*.png`: data, not code.

When you change behaviour documented in `docs/agents/*.md`, update that file in the same change.
