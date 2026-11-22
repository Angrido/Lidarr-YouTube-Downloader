# Downloader: ffmpeg, cookies, matching

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

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

A flat search result (`extract_flat`, YT Music's above all) can lack the
duration and the credited artist. While the expected duration is known, such
a candidate is **provisional** (`_evaluate_entry` in `search_youtube_candidates`):
it never counts toward `GOOD_SCORE` (`_has_good()`), and after each search pass
`_settle_provisional()` reads the video's own metadata for the best of them
(`_fetch_video_metadata`: no formats, so a PO-token problem can't fail it;
through `_build_common_opts`, hence the VPN proxy) up to `RESOLVES_PER_SEARCH` /
`MAX_RESOLVES_PER_TRACK`, merges it in (`_merge_resolved_metadata`) and scores
the entry again, which can reject it on the duration window or the artist
check (issue #118). A lookup that fails leaves the candidate provisional in the
pool; each video is looked up once per track.
