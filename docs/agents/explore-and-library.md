# Explore, library adds, external playlists, Lidarr tags

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

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
this app's job. A download request starts `queue_when_ready()`, a thread that
polls `GET track?albumId=` until Lidarr has loaded the tracklist (up to 4
minutes) and then `models.enqueue_album(force=True)`. The only HTTP route left
here is `/api/library/options` (root folders / profiles for Explore's sliders
button); the old **Add music** page is gone and `/add` 301-redirects to
`/explore` (keeping `?q=`). Each add also schedules `lidarr_sync.trigger_sync()`
after 15 s and 90 s so the Library shows the new missing albums.
`lidarr_request()` supports `PUT` and turns Lidarr's 400 validation list into
a readable `"Lidarr rejected the request: …"` error.

### Explore (`explore.py`, `templates/explore.html`)

A music catalog backed by YouTube Music through **ytmusicapi** (unauthenticated).
Live behaviour drives the design, not the docs: from a datacenter IP
`get_explore` raises a `KeyError` (a podcast item lacks `musicVideoType`),
`get_mood_categories` and `get_album` can fail, search filters for albums /
songs return `[]`, charts have no song list without Premium. The real
responses (and the errors) are in `tests/fixtures/ytmusic/`; the `doc_*`
fixtures follow ytmusicapi's docstring shapes for what could not be
captured live. So:

- **One client per (language, country)** (`downloader._ytmusicapi_client`
  with `language`/`location`), each guarded by a lock; every YT call goes
  through `explore.call()`.
- **`TTLCache`**, keyed with the locale: feeds 30 min, artist/album/playlist
  24 h, matches 10 min, suggestions 1 h. A failed loader is cached as a
  60 s negative entry so a dead YouTube isn't hammered. `invalidate(prefix)`.
- **One item shape** from `normalize_item()`: `{kind, id, title, subtitle,
  artists[], year, type, thumbnail, explicit, duration}` (+ `album`,
  `available`, `rank` for songs; `playlistId` for albums). Podcasts and
  episodes are dropped, ids are validated by regex, bogus years ("Two Steps
  From Hell") ignored. `thumbnail` is always a `/api/thumbnail?url=` path;
  `best_thumbnail_url()` picks the largest ≤ 544 px and resizes
  googleusercontent `=wN-hN` / `=sN` URLs.
- **Raw fallback**: when a ytmusicapi parser raises, `raw_browse()` fetches
  the InnerTube page and `raw_sections()` reads carousel / shelf / grid
  renderers itself (`musicTwoRowItemRenderer`,
  `musicResponsiveListItemRenderer`, mood buttons). That keeps the live
  trending shelf when `get_explore` breaks.
- **Every section degrades**: loaders never raise to the UI, empty sections
  are omitted, top songs fall back to the first chart playlist's tracks.
  `/api/explore/home` answers 502 only when *everything* is empty;
  unexpected exceptions become a generic 502 message.
- **Mapping** (`match_album`): the YT album is searched through
  `library.search("album", …)` (first "artist title", then the title alone,
  "Various Artists" never used as a term) and every candidate scored —
  0.6 × title (`norm_title()` drops edition brackets / " - 2004 Remaster"
  suffixes, keeps real parenthesised titles) + 0.3 × artist (any credited
  artist, Various Artists only matches Various Artists) + bonuses for year,
  track count, release type, exact title, already in library. Accept at
  ≥ 0.85 with a 0.08 lead; otherwise `ambiguous` with up to 5 candidates;
  no candidate past the title/artist gates → `not_on_musicbrainz`. Status of
  the match: `complete` / `missing` / `unmonitored` / `not_in_library`.
  Artists: exact (folded) name, one hit or one already in the library.
- **Add & download** (`add_album`): `library.add_album(download=False)`,
  then `models.set_album_source_hint()`, **then**
  `library.queue_when_ready()` — the hint exists before the album can be
  processed. `processing._hinted_ytmusic_album()` turns it into the same
  `ytmusic_album` dict `find_album_on_ytmusic()` returns
  (`downloader.album_from_ytmusic_hint`), logging "Using the YT Music album
  picked in Explore"; an unusable hint falls back to the search.
- **YouTube import** (`import_plan`): album or playlist pages are re-read
  server-side (the client only sends ids and an optional `videoIds`
  selection) and handed to `youtube_import._start_playlist_import()`, the same path as
  `/api/youtube/playlist/download` (negative `album_id`, cover from the
  1200 px art).
- **Artists not on MusicBrainz** (`artist_import_plan`, `release_import`,
  `explore_routes._run_artist_import`): Lidarr cannot hold them, so *Add from YouTube*
  builds the library folder itself — `<lidarr_path or download base>/<Artist>/
  <Release (Year)>/` (`_release_folder`), `artist.jpg` + `folder.jpg` in the
  artist folder — and runs one `_execute_playlist_download` per release in
  a background thread, one at a time (it returns `False` when another
  download held the slot, and the runner retries). Releases are the
  artist's albums + singles; when YouTube Music lists none, the videos
  (or songs) playlist becomes one single per song with titles cleaned by
  `clean_video_title()` (artist prefix, "(Clip Officiel)"/"[Official
  Video]"/"Directed by @…" brackets and bidi marks removed, "Artist x
  Guest - Title" → "Title (feat. Guest)"). Titles already imported with at
  least one successful track (`models.get_imported_youtube_album_titles`)
  are skipped. Job state lives in `explore_routes._artist_imports` (in memory):
  `GET /api/explore/import-artist/plan?id=`, `POST /api/explore/import-artist`,
  `GET …/status?id=` (also the DB count of imported releases),
  `POST …/stop` (stops after the current release). Explore album imports
  use the same layout; `_execute_playlist_download` takes `release_year`
  and embeds the saved cover in every file.
- **Artist image** (`metadata.ensure_artist_image`): after any successful
  download for an artist — a Lidarr album (`processing._ensure_artist_image`
  on the final artist folder, i.e. the library copy when `lidarr_path` is
  set; skipped for download-client grabs), a YouTube import into an
  `Artist/Release` folder (`youtube_import._ensure_import_artist_image`) or an artist
  import — the artist folder gets `artist.jpg` + `folder.jpg` **only if it
  has no artist/folder image yet** (any of jpg/jpeg/png, case-insensitive);
  an existing one is never touched. Sources in order: Lidarr's artist
  `poster` remoteUrl (or the YT Music artist art for artist imports),
  Deezer's artist search (exact name), then YT Music's artist search
  (`explore.artist_image_for_name`). Tests stub the two network lookups in
  `tests/conftest.py`; mark a test `real_artist_image` to keep them.
- **Endpoints**: `GET /api/explore/home?country=`, `/charts?country=`,
  `/moods`, `/mood/<params>`, `/artist/<id>`, `/album/<id>`,
  `/playlist/<id>`, `/search?q=`, `/suggestions?q=`,
  `/match?kind=album|artist&id=[&refresh=1]`; `POST /api/explore/add`
  (`{kind, id, foreignAlbumId|foreignArtistId?, download?, rootFolderPath?,
  qualityProfileId?, metadataProfileId?, monitor?}`) and
  `POST /api/explore/import` (`{kind: album|playlist, id, videoIds?}`).
  Each has its own rate-limit bucket (`_explore_json`).
- `tools/explore_preview.py` runs the app on port 5099 with fixture-backed
  YT Music and Lidarr fakes, a silent audio stream and generated
  thumbnails (`--down` simulates YouTube being unreachable). Use it for UI
  work and Playwright checks; it never downloads.

### External playlists (`external_playlists.py`)

`/api/youtube/playlist/info` hands Spotify (embed page `__NEXT_DATA__`, or the Web API with `spotify_client_id`/`spotify_client_secret`), Deezer (public API), Apple Music (iTunes lookup for albums/songs, the page's `serialized-server-data` for playlists, best effort) and ListenBrainz (JSPF API) links to `fetch()`, and returns the YouTube-page shape with `search: true` entries (title, artist, album, duration, no URL). `/api/youtube/playlist/download` keeps such entries, and `_execute_playlist_download` resolves each one with `download_track_youtube("<artist> <title> official audio", …)`, tags it with `track_info["trackArtist"]` (TPE1 / artist / ©ART, album artist stays the playlist name), records `track_artist`, and writes `<name>.m3u8` when any entry was searched. `POST /api/youtube/external/lidarr` starts one in-memory job that matches each (first artist, album) via `library.search` + `explore.decide_album` (an exact title + artist breaks a tie between editions), and `library.add_album(download=True)` for anything not already complete; `GET …/status` reports it. Fetches only go to these fixed hosts; cover art is accepted from `is_cover_url()` hosts.

### Lidarr tags (issue #89)

`lidarr.tag_artist(artist_id)` adds the `lidarr_tag` config value (lowercase letters, digits, hyphens; empty = off) to the artist through `PUT artist/editor` (`applyTags: add`), falling back to `GET`/`PUT artist/{id}`; the tag id is created on first use and cached. Called after any album run with at least one downloaded track (`processing._tag_artist_quietly`) and after a manual download. Lidarr has no album or track tags.

### Explore "For you"

`explore.for_you()` (`/api/explore/for-you`, cached 6 h per locale) picks up to 8 library artists (`pick_seed_artists`: the most recently added half, the rest random), finds each on YT Music (`_artist_channel`, exact folded name) and ranks their `related` artists by how many seeds share them, minus artists already in Lidarr (`rank_recommendations`). The page loads it after the home shelves and inserts it after the hero.
