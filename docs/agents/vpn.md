# VPN with gluetun

<!-- Moved verbatim from CLAUDE.md; keep it in sync with the code. -->

### VPN with gluetun (`vpn.py`)

Two independent halves, both off until configured. `vpn_proxy_url` (gluetun's
HTTP proxy) is added to **every** yt-dlp option dict and `requests` call that
talks to YouTube on behalf of a download or a preview, via `vpn.ytdlp_opts()` /
`vpn.requests_proxies()` — `_build_common_opts` covers search, album browsing
and downloads; the YouTube search, the preview stream (googlevideo URLs are
bound to the IP that resolved them, so its fetch must use the same proxy), the
cookies test, the playlist info and the YT Music cover page add it themselves,
and the ytmusicapi album lookups a download makes ask for
`_ytmusicapi_client(via_vpn=True)`. A new yt-dlp call site must do the same.
Lidarr, notifications, Explore browsing (`_ytmusicapi_client` without
`via_vpn`) and thumbnails stay direct. Each call site has its own test in
`tests/test_vpn.py` (`TestProxyCallSites`). The bgutil PO-token sidecar needs no code here: yt-dlp's plugin passes
the `proxy` option to it and it makes its own requests through it (seen in
its log), so `vpn_proxy_url` must be reachable from the sidecar's container
too (the compose service name is). `gluetun_control_url` + `gluetun_api_key` (`X-API-Key`) let
`vpn.rotate()` do `PUT /v1/vpn/status` stopped → running, poll until the tunnel
is back and compare `GET /v1/publicip/ip` before/after; a restart that keeps
the same IP is a success with `changed: false`. Gluetun's responses are never
echoed to the browser: IPs go through `ipaddress`, the status through an enum.

Automatic rotation: `vpn.note_block()` (called at the three downloader sites
that see a yt-dlp error — download, format listing, search — and matching
"not a bot" / HTTP 429 only) and `vpn.note_album_finished()` (once per album
that actually attempted its tracks: in `process_album_download` just before
`_handle_post_download`, which covers Lidarr albums and download-client grabs,
and in `youtube_import._execute_playlist_download` after the loop unless
stopped) feed `vpn.due_reason()` (`vpn_rotate_on_block`,
`vpn_rotate_every_albums`, at most one attempt per `AUTO_ROTATE_COOLDOWN`
(300 s; a block waits only `BLOCK_ROTATE_COOLDOWN`, 60 s: the address is
useless meanwhile), failures included; the triggers reset only after a
successful rotation, and a block also survives a restart that came back on
the very same IP).
`_dispatch_next_from_queue()` rotates only when nothing is downloading, and
while a rotation is due but downloads are still active it starts nothing new,
so the running albums drain and the rotation is not postponed forever by a
busy queue; `vpn.defer()` backs off if the check itself raises. While a block waits for
its rotation (`vpn.block_pending()`, which ignores the cooldown) the
dispatcher starts nothing either: an album started on a refused address would
fail at once, leave the queue and sit out the scheduler's retry window. The
hold ends by itself after `MAX_HOLD_FAILURES` failed rotations in a row, so a
broken VPN cannot freeze the queue. It calls
`vpn.rotate_if_due(busy=any_download_active)`, which asks again once
`_rotate_lock` is held (every download entry point refuses while it is), so a
download that started after the first look is seen.

A block is also the IP's fault, not the track's: `vpn.blocks_seen()` is a
monotonic counter (not the `block_seen` flag, which stays set until a
rotation succeeds) that `processing._download_tracks` compares before and
after each track. When `vpn.rotates_on_block()` (control URL set and
`vpn_rotate_on_block`) and a track fails with the counter higher, the reason
gets `models.BLOCKED_PREFIX` ("YouTube blocked this IP"), which — like the
host and disk-full prefixes — is left out of `get_track_failure_counts` /
`get_stuck_tracks`, and the tracks of that run not started yet are skipped
(no row, so the next run downloads them on the new IP). A block that a track
survives (another player client worked) stops nothing; without a rotation to
come nothing changes at all. A burnt IP must not hold the album either:
`download_youtube_candidate()` counts the blocks it sees (`vpn.note_block()`
returns whether the message was one) and, once there are as many as clients
in its chain, returns `ip_blocked: True` instead of going on through every
selector × client combination (that took 13+ minutes per track on a refused
IP, and the rotation only runs when nothing is downloading);
`_download_candidate_threaded` copies the flag to `track_state["ip_blocked"]`
and the candidate loop stops. Only with `vpn.rotates_on_block()`; `vpn.is_block()`
is the side-effect-free matcher (markers include YouTube's session
rate-limit message). A foreground run that stopped this way is
remembered (`processing._blocked_albums`, set by `_label_block`, dropped on a
user stop and at the start of the next run; download-client jobs are never
marked, Lidarr retries its own grabs), and `_run_queued_album` puts the album
back at the head of the queue (`_requeue_after_block`, keeping its `force`)
only while `vpn.block_pending()` (the dispatcher holds the queue until the
rotation has run, so it does not stop on the same block again) and at most
`MAX_BLOCK_REQUEUES` times in a row; after that the scheduler's retry window
takes over. The failed-tracks retry
(`POST /api/download/manual`) downloads inside its request and holds no slot,
so it runs inside `processing.inline_download()`, which
`any_download_active()` counts; it enters it *before* re-checking
`vpn.rotating()`, the counterpart of the check-after-lock above.
`automatic_downloads_held()` is true while `vpn.rotating()`, and the manual
downloads (409), playlist imports (409) and the Explore artist import wait
while it is. `POST /api/vpn/rotate` takes `_rotate_lock` first, refuses (409)
while a download is active or another rotation runs, and answers 202 after
starting `rotate()` in a thread; the caller follows `GET /api/vpn/status`
(`rotating`, then `last_rotation`). `vpn_proxy_url` and `gluetun_api_key` are
in `SECRET_CONFIG_KEYS` (the proxy URL may carry `user:pass@`); the test route
sends the saved key only to the saved control URL. `tests/test_vpn.py` runs
rotation against a fake gluetun HTTP server.
