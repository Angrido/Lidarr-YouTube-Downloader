# Changelog

## 2.0.1

### Fixed — found in real download logs
- **Age-restricted videos no longer stall an album for minutes.** yt-dlp's
  "Sign in to confirm your age" was retried for every player client ×
  format selector × postprocessor (≈40 attempts, 6+ minutes per track) and
  every attempt was logged as a warning. Without cookies the download now
  stops at the first age gate; with cookies each client is tried once.
  Private, removed, members-only and region-locked videos stop at once.
  The track gets one line explaining what to do (upload a signed-in
  cookies.txt, or re-export it if it is not signed in).
- **An unusable album-playlist track falls back to a normal search.** When
  the official YT Music album entry cannot be downloaded (e.g. it is
  age-restricted) other uploads of the same song are tried instead of
  failing the track.
- **cookies.txt no longer breaks under parallel downloads.** yt-dlp rewrites
  its cookie file at the end of every run without locking, so two tracks
  downloading at once could read a half-written file ("does not look like a
  Netscape format cookies file") and the rewrite could replace a signed-in
  export with a rotated, logged-out jar. Every yt-dlp run now gets a private
  copy; your file is never modified. Files without the Netscape header line
  are accepted (the header is added), JSON exports are rejected on upload
  with a clear message, and an unusable file is ignored with a single
  warning. Settings shows when the configured file is being ignored.
- **Tracks are no longer downloaded twice.** When AcoustID had no data for
  any candidate, the best one was deleted and downloaded again; the first
  download is now kept.
- **Official album tracks without AcoustID data are kept** right away
  instead of going through the unverified fallback.
- **AcoustID results without linked recordings count as "no data", not as a
  mismatch**, so the candidate is no longer banned ("got=None").
- **Generic titles (Intro, Outro, Interlude, Skit, [untitled]…) need the
  artist's channel or name.** Any video containing "intro" used to qualify,
  so an album intro could match a DJ jingle.
- **Bracketed titles such as "[untitled]" match uploads without the
  brackets**, both in the per-track search and in the album playlist.
- **Album-playlist matching by position.** When titles differ but the YT
  Music album has the same number of tracks and the track at the same
  position has the same duration, it is used. Such a match is still checked
  by AcoustID (a mismatch rejects it and falls back to search).
  Among identically titled album tracks the closest duration wins.
- The "any source" search phase reuses the results of the "artist channel"
  phase instead of running the same ten queries again.

### Changed
- Log lines of an album run carry the track number (`[02]`), so tracks
  downloading in parallel no longer interleave anonymously, and the few
  album-run lines that lacked the indent now have it.

## 2.0.0

### Changed
- **A completely redesigned interface.** Every page now shares one design
  system (`static/components.css` + `static/app.js`): a sidebar on desktop
  and a tab bar on phones with a live queue badge and a "downloading" dot,
  light and dark themes that follow the system (or a manual choice that is
  remembered), system typography, one indigo accent, layered surfaces,
  frosted navigation, bulk-action bar and dialogs, soft shadows and
  motion that respects *Reduce motion*.
  - **Library** opens with status tiles (Lidarr, ffmpeg, queue, scheduler)
    and a cover-art grid with hover actions, elegant selection and a
    floating bulk-action bar; list and table views remain.
  - **Downloads** shows the active album as a "Now playing" card with
    per-track progress (searching, downloading with speed, checking,
    tagging, done, failed, skipped), a reorderable queue and a history you
    can filter by outcome and audio quality.
  - **Insights** charts were redrawn: readable labels at any width, hover
    tooltips, legends and a table view.
  - **Logs** colour-codes severity, filters by type and retries inline.
  - **YouTube import** has a prominent URL field and a preview of the
    playlist before importing.
  - **Settings** is organised in sections with a jump list, iOS-style
    switches and segmented controls, inline validation, a clear
    saved/unsaved state, an always-visible ffmpeg verdict and a drop zone
    for restoring backups.
  - **Setup** is a step-by-step wizard with a live connection test.
- Icons are a bundled SVG sprite; the Font Awesome CDN is no longer loaded,
  so the UI works fully offline.
- New app icon; the PWA manifest ships PNG and maskable icons and an iOS
  touch icon, and the browser/status bar colour follows the theme.
- `/api/stats` also returns `queued` and `active` (any download, including
  Lidarr download-client jobs).
- Press `/` to jump to the search field on Library and Settings.
- Phone-friendly hit targets: buttons, fields, chips and segmented controls
  grow to 36–42px on touch screens.

### Fixed — downloads and library
- A track whose worker crashed was reported as a success and its temp file
  could be copied into the library; it is now recorded as a failure.
- Albums that stopped early (Lidarr error, no release, path not mounted,
  permission denied, empty tracklist) wrote no log, so the scheduler retried
  them every cycle. An empty tracklist was reported as "already complete".
- A queued album could vanish when a manual or playlist download took the
  slot first; a Lidarr grab of an album already downloading stayed
  "Queued" in Lidarr forever.
- One error in the scheduled check stopped the scheduler until restart.
- Vinyl-style track numbers ("A1") made tracks download again every run.
- A non-numeric track number in a manual download blocked all downloads
  until restart.
- Skipping a track during conversion could leave a temp file in the album.
- Bans now match the video, not the exact URL form.
- Failures caused by a broken ffmpeg no longer push tracks into retry
  backoff or "give up" them.
- Loudness normalization broke opus downloads and did nothing for m4a; it is
  now a separate pass that can never mark ffmpeg as broken.
- opus is no longer advertised as working without ffmpeg (YouTube serves it
  in WebM, which needs remuxing).
- A single corrupt source no longer disables audio conversion until restart.
- An AcoustID outage no longer makes every candidate "unverified" (all of
  them were downloaded and thrown away); fixing an invalid AcoustID key works
  without a restart.
- Titles with typographic apostrophes or accents ("Don’t", "Déjà Vu",
  "Beyoncé") no longer fail to match; tracks shorter than 15 s can match.
- Very long (e.g. Japanese) titles no longer fail with "File name too long".
- Renamed albums no longer appear twice in the history.
- A failed sync page no longer removes albums from the missing list.

### Fixed — settings, API and integrations
- Settings could be wiped by two toggles at the same time or a crash while
  saving; `config.json` is now written atomically.
- `LIDARR_URL` / `LIDARR_API_KEY` / `DOWNLOAD_PATH` set in the environment
  are no longer frozen into `config.json` by the first save.
- Invalid settings return a clear error instead of a server error or being
  stored as text (`"false"` was treated as on).
- The Lidarr API key is no longer returned by `/api/config` or the export.
- Restore and restart are refused while a Lidarr download-client job runs.
- Lidarr commands no longer retry for 35 s on errors that can't succeed.
- Ntfy notifications showed raw JSON and ignored title and priority.
- Telegram/Discord/ntfy tokens no longer appear in the logs; Telegram no
  longer rejects messages with a backslash or a long failure reason.
- Removing a failed job from Lidarr's history no longer lets Lidarr grab the
  same album again immediately; removing a downloading job stops it.
- Error pages are no longer embedded as cover art.
- "Recent imports" on the YouTube page was always empty.

### Fixed — interface
- Many UI bugs found in an audit: Save reporting success on errors,
  double-submits (manual downloads, add to queue, toggles), XSS through
  server messages and cover URLs, Esc/focus handling in dialogs, keyboard
  access, contrast in the light theme, layouts overflowing on phones,
  polling that kept running in background tabs, the Logs "All" view hiding
  entries, insights labels unreadable on phones, and more.
- Design review of 2.0: the queue badge and "downloading" dot no longer
  cover the Downloads icon in the compact sidebar and the phone tab bar;
  frosted bars and toasts are opaque enough to read over busy content; the
  four status tiles no longer leave one orphan tile on tablets; the
  floating selection bar no longer hides the last row of albums; the
  YouTube link field no longer clips its placeholder on phones; the list
  view on phones shows the missing-track count instead of cutting it off;
  table actions line up; unit labels in Settings no longer touch the card
  edge; empty states are less tall on phones; links such as
  `/settings#audio` open the right section; the retry dialog keeps each
  field and its button on one row on phones.

## 1.9.2

### Fixed
- **MusicBrainz tags are no longer mislabeled, so external taggers accept
  the files again** (#93). `MusicBrainz Release Track Id` carried the
  *recording* id — the same value already in the `UFID` frame — and the
  release-specific track id was written nowhere at all. Taggers that check
  that id against the release's tracklist found no match and refused to
  write any tags. The release-track id now goes in that frame, the recording
  id stays in `UFID` and also gets its own `MusicBrainz Recording Id`. Both
  ids come from Lidarr, so nothing extra is fetched.
- **Two tag names nothing could read** — `MusicBrainz Album Release Group
  Id` and `MusicBrainz Release Country` were not the names taggers look for,
  so the release group and release country were silently ignored by every
  player and tagger. They are now written as `MusicBrainz Release Group Id`
  and `MusicBrainz Album Release Country`.
- **The album artist's id is now also written as `MusicBrainz Album Artist
  Id`**, not only as `MusicBrainz Artist Id`.
- **Opus files get the release-track id too** (`musicbrainz_releasetrackid`).
  Vorbis Comments name these tags the other way round from MP3, so Opus was
  already labeling the recording id correctly and only lacked this one.
- **Re-tagging a file cleans up after the old versions** — the wrongly named
  tags are removed before writing, so a file tagged by an earlier release
  doesn't keep a stale value alongside the correct one. If Lidarr has no
  release-track id for a track, the tag is left out rather than filled with
  the recording id.
- **`tools/fix_metadata.py` repairs files you already downloaded.** It used
  to write the recording id into the release-track tag itself, and only
  looked at the album id — so it would not have touched a single affected
  file. It now detects a wrong release-track id, moves the recording id to
  its proper tags and migrates the renamed ones. Its per-track lookup was
  also comparing Lidarr's track number as text against the file's as a
  number, so no recording id was ever applied; that is fixed too.

## 1.9.1

### Added
- **Ntfy push notifications** (Settings → "Ntfy Notifications", env
  `NTFY_ENABLED` / `NTFY_TOPIC`, default off): a third notification channel
  alongside Telegram and Discord, using [ntfy](https://ntfy.sh) — an
  open-source HTTP pub-sub service you can self-host or use through the
  public server, with apps for Android, iOS and the browser. Set a topic
  and you get push notifications on your phone, with no bot to register and
  no webhook to create. Supports a custom server (`NTFY_URL`, default
  `https://ntfy.sh`), an optional bearer token for protected topics
  (`NTFY_TOKEN`), a default priority (`NTFY_PRIORITY`) and the same
  per-event filters as the other channels — download started, download
  success/partial, import success/partial and album errors. A "Send Ntfy
  test" button verifies the setup before you save it.
  (#92 — thanks @pheonix14)

## 1.9.0

### Added
- **Insights dashboard** (`/insights`): a new analytics page with
  dependency-free inline-SVG charts — downloads over time (successful vs
  failed per day), overall success-rate donut, audio-quality distribution
  (from the per-track source format) and your most-downloaded artists —
  plus headline stats (tracks attempted, success rate, albums, artists,
  total listening time). Pick a 7 / 30 / 90 / 365-day window.
- **Bulk actions on the library**: select multiple missing albums with the
  new per-album checkboxes (in every view — cards, list and table) and queue
  them all at once from a floating action bar, instead of adding them one by
  one.
- **Backup & Restore** (Settings → Import / Export): download a consistent
  snapshot of the whole database (settings, history, queue) as a single
  `.db` file, and restore it later by uploading it. The restore validates
  the file, swaps the database atomically and restarts the app; it's
  refused while a download is in progress to avoid corruption.

### Improved
- **The container log is readable again.** Every line now carries a
  timestamp, warnings and errors are marked with an icon so they stand out,
  and a handful of milestones (ready, album started, album finished) get one
  too — the rest stays plain. Consecutive identical lines are collapsed into
  a single "repeated N×" note: the periodic Lidarr sync used to print the
  same sentence hundreds of times in a row, and now only speaks up when the
  result actually changes. ffmpeg postprocessing failures are no longer
  printed twice (once by yt-dlp and once by us), the ten-line schema
  migration chatter is a single line, and Flask's duplicate startup banner
  is gone. Startup and each album download are separated by a blank line
  and read as their own block, with the steps of a download indented
  under the album they belong to.

- **Settings now explains it when this machine cannot convert audio.** The
  app already detected the problem, but only mentioned it in the log, where
  an errno is no help. A panel now appears (only when there is something to
  act on) saying what fails, what it means, whether downloads still work,
  and how to fix it — including the one fix that can be applied from inside
  the app: switching Audio Format to m4a/opus, which YouTube serves
  natively so nothing needs converting. The panel always states a verdict,
  good or bad, and shows the detected architecture and audio format, so
  the Re-check button always answers rather than leaving you guessing.
  `/api/health` reports `ffmpeg_ok`.

### Fixed
- **Tracks that can never be found stop being retried forever** (#90). A song
  that simply isn't on YouTube failed identically on every scheduler cycle,
  forever. Each consecutive failure now doubles the wait before that track is
  tried again (capped at 30 days), so the futile work decays instead of
  repeating daily — but it is still retried eventually, in case the track
  shows up later. Set *Max Retries per Track* to give up for good instead.
  Adding an album to the queue by hand always retries everything immediately.
- **Albums with nothing to download no longer cost a full round of API
  calls.** The "what needs downloading" check now runs *before* the cover-art
  fetch, the per-track artist lookups and the YouTube Music album
  resolution, so an album that is already complete (or fully backed off)
  exits immediately instead of burning those every cycle. Per-track artist
  lookups are also limited to the tracks actually being fetched.
- **The ffmpeg check now matches what yt-dlp actually runs.** The probe
  that decides whether audio conversion is possible wrote its test file
  without `-movflags +faststart`, which yt-dlp appends to every output it
  produces. On hosts where that is the operation that fails, the probe
  passed while every real conversion failed, so each track still ran the
  full format-selector cascade before falling back. The probe now uses the
  same flags, a single failure is enough to stop the cascade (no client or
  selector can make ffmpeg able to write its output), and concurrent
  downloads notice as soon as another track has proved it. Loudness
  normalisation, which needs a re-encode, now warns once and downloads
  without it instead of failing every track.
- **Downloads survive a broken/emulated ffmpeg.** On hosts where ffmpeg
  can't write output files (e.g. an emulated CPU architecture, or a
  download filesystem returning ENOSYS for the mp4 muxer), audio conversion
  failed for every track. The app now probes ffmpeg once at first download
  and, when conversion isn't possible, downloads the native m4a/opus stream
  directly with no ffmpeg step — so downloads still succeed, without the
  endless "Postprocessing: Error opening output files" retry loop that
  previously had to be stopped by hand.
- **Quiet yt-dlp plugin loading.** The bgutil PO-token provider registered
  under both of yt-dlp's plugin mechanisms, printing an alarming
  "PoTokenProvider ... already registered" traceback on the first download.
  Plugins are now loaded once at startup with that harmless message
  suppressed; PO-token support is unaffected.

## 1.8.8

### Added
- **First-run setup wizard** (`/setup`): a short guided flow that tests the
  Lidarr connection (and explains the `LIDARR_URL`/`LIDARR_API_KEY` env vars
  when it can't reach it) and lets you set your download/library folders.
  Unconfigured instances are sent here automatically (skippable).
- **Per-track audio quality report**: the download history now shows the
  actual YouTube source stream each track was downloaded from — format id,
  container and bitrate (e.g. `140 · m4a · 128 kbps`) — so it's obvious at a
  glance when a track came from a low-bitrate source. Stored per track
  (schema v10).

### Improved
- **Shared UI component system** (`static/components.css`): a consistent set
  of buttons, badges, inputs, cards, modals and toasts (`.ui-*`) used by the
  new wizard and adopted across the retry UI, so the interface stays visually
  consistent instead of drifting per page.

## 1.8.7

### Added
- **Installable web app (PWA)** — the UI now ships a web manifest and a
  minimal service worker, so you can "Add to Home Screen" on Android/iOS
  and run it as a standalone app with its own icon and theme color.
- **ReplayGain tags** (Settings → "ReplayGain Tags", env `APPLY_REPLAYGAIN`,
  default off): measures each track's loudness with ffmpeg and writes
  `REPLAYGAIN_TRACK_GAIN`/`PEAK` tags (MP3/M4A/Opus) so players like
  Jellyfin/Navidrome can normalize volume **without re-encoding** the audio
  — non-destructive, unlike Loudness Normalization.
- **Synced lyrics `.lrc` sidecars** (Settings → "Save Synced Lyrics", env
  `SAVE_LYRICS`, default off): fetches time-synced lyrics from
  [LRCLIB](https://lrclib.net) and writes a `.lrc` next to each track (falls
  back to plain lyrics), so Jellyfin/Navidrome/Plex can display lyrics.

### Improved
- **Settings page is searchable and collapsible** — a search box filters
  options live as you type, and each section can be collapsed (state
  remembered per section), so the long settings page is much easier to
  navigate.

## 1.8.6

### Added
- **Per-track artist for compilations** (Settings → "Search Artist Source",
  env `SEARCH_ARTIST_SOURCE`, default `album`): on compilation albums Lidarr
  sets the album artist to "Various Artists", so the old YouTube search
  (`Various Artists - <title>`) matched nothing. The app can now resolve
  each track's real artist via MusicBrainz and/or iTunes and search for
  `<track artist> - <title>` instead, falling back to the album artist when
  it can't be resolved. Default keeps the previous album-artist behavior.
  (#86 — thanks @aki-ks)

### Fixed
- **Third-party API requests now send a proper `User-Agent`** — MusicBrainz,
  AcoustID and the Cover Art Archive require an identifying User-Agent;
  requests now carry the real app version, avoiding throttling/rejection.
  MusicBrainz lookups are throttled to 1 req/s and retried with backoff on
  `503`/`Retry-After`. (thanks @aki-ks)

## 1.8.5

### Fixed
- **Playlist import progress is restored again after a page reload** —
  reloading the YouTube import page while a playlist was downloading left
  the progress panel blank and the live updates disconnected (a regression
  from the 1.8.4 per-import id change: the resume check still assumed the
  old "playlist = album id 0" marker). It now recognizes an in-progress
  import and re-attaches its progress and live stream.
- **Retrying failed tracks from playlists imported before 1.8.4 now works**
  — those imports were all stored under the shared internal id `0`, so
  their retry hit *"No album context available."* A one-time migration
  reassigns each old playlist to its own id (in both the download records
  and the logs), so their failed tracks can be retried into the right
  folder like new imports, and distinct playlists stop colliding in the
  history.
- **The manual-download endpoint returns a clean error for a malformed
  request** — a non-numeric `album_id` now yields a normal "invalid
  request" response instead of a 500.

### Changed
- Playlist import ids are allocated race-safely (reserved under the queue
  lock, counting both download records and logs), so two imports can't
  collide even back-to-back.

## 1.8.4

### Fixed
- **Failed tracks from a YouTube playlist import can now be retried** (#83):
  playlist imports used to share `album_id = 0`, so the manual "search &
  select" retry couldn't resolve a context and returned *"No album context
  available. Please re-download the album first."* Each import now gets its
  own id, and its retry rebuilds the context from the stored record and
  re-downloads into the same folder — no Lidarr album required. Different
  playlists no longer collide in the history / retry views.

## 1.8.3

### Added
- **Save YouTube playlist imports to the music library** (Settings →
  Download Options, env `PLAYLIST_TO_LIBRARY`, default off): a "Playlist
  creation from YouTube" import can now be written into your Lidarr music
  library (`LIDARR_PATH`) instead of only the download folder, and a
  path-based Lidarr library scan (`DownloadedAlbumsScan`) is requested
  when it finishes — so the tracks show up in Jellyfin (after its scan)
  and Lidarr imports whatever it can match (#79). Off keeps the previous
  download-folder-only behavior.

### Fixed
- **Manual / playlist / retry downloads no longer fail with "Requested
  format is not available"** (#80): every single-URL download path
  (manual track download, the failed-track retry, and YouTube playlist
  import) now goes through the same multi-client / multi-selector fallback
  as the automatic album download — trying `web`/`ios`/etc. and looser
  selectors — instead of a single `bestaudio` attempt on the configured
  `android` client (which yt-dlp often can't satisfy without a PO token).
  The format override, cookies and PO tokens apply to these paths too.
- **The download-retry button no longer navigates away from the Downloads
  page** (#78): it opens the search/paste-a-link retry overlay in place,
  so you can retry one failed album after another without being bounced to
  the home page and back.
- **Cookies "Test" no longer misreads real exports as logged out**: the
  signed-in check parses the file with yt-dlp's own cookie jar, which
  understands the `#HttpOnly_` line prefix browsers and yt-dlp use for
  HttpOnly cookies — `LOGIN_INFO`, the login marker, is one of them. The
  verdict now mirrors yt-dlp's own `_has_auth_cookies` (`LOGIN_INFO` +
  a SAPISID-family cookie), and the rotated-session case gets its own
  diagnosis: account cookies survive YouTube's rotation while
  `LOGIN_INFO` is cleared (and yt-dlp rewrites the file after every run),
  so the Test now says the session was rotated/invalidated and to
  re-export from a private window — instead of generic export advice.
- **The yt-dlp Format Override is honored by the manual track download
  path too** (it previously always forced `bestaudio/best` — thanks
  @Gazz1e), and the override now rides the first selector with a
  slash-fallback (`141/bestaudio/...`): a video that doesn't expose the
  format falls back in the same request instead of sweeping every player
  client per track. Web-family clients — the ones that expose premium
  formats like 141 — are tried first when an override is set (on the
  manual single-track path too, which otherwise stayed pinned to the
  configured `android` client and could never see the format), and the
  "List formats" tester walks the same client chain the download path
  uses (including the music clients for a `music.youtube.com` URL). With
  no override the behavior is unchanged.
- **The yt-dlp updater now offers the "Restart App" step** after
  installing a new version: the button's success handler was immediately
  reset by its own `finally` block, so the freshly-installed yt-dlp was
  never applied and a second click just hit the rate limit.
- **The "yt-dlp updater" / cookies-test UI no longer overflows on
  mobile**: the format-tester result box wraps long error text and video
  titles (it was being clipped invisibly by the page's `overflow-x:hidden`
  at ~360px), the format list scrolls when long, format-ID chips use a
  calm style instead of the page's animated primary button, the "List
  formats" button is disabled while a lookup is in flight (no racing /
  stale results), Enter submits the URL field, and that diagnostic field
  no longer flags the form as having unsaved changes.
- **Concurrent Album Downloads out-of-range values are clamped to 1–5**
  in config load, so an env/file value outside the range can't render the
  Settings dropdown blank and then silently save back `1` over it.
- **Download-client API-key check no longer 500s on a non-ASCII key**:
  the timing-safe comparison now runs on bytes (`hmac.compare_digest`
  raises `TypeError` on a non-ASCII `str`), returning a clean credential
  rejection instead of an unhandled error.
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
