"""Lidarr download-client integration (Newznab indexer + SABnzbd client).

This module lets the app be configured *inside* Lidarr as a native
download path, instead of pushing finished files to Lidarr itself.

Two protocol surfaces are exposed via a Flask blueprint:

* **Newznab indexer** — ``Settings -> Indexers -> Newznab`` in Lidarr::

      GET /api/newznab/api?t=caps
      GET /api/newznab/api?t=music&artist=..&album=..   (or t=search&q=..)
      GET /api/newznab/api?t=get&id=<album_id>          (fake NZB download)

  When Lidarr searches for a wanted album, we resolve it against the
  local missing-albums cache and return a single "release" whose
  download URL points back at us. The NZB we serve embeds the Lidarr
  ``album_id``.

* **SABnzbd download client** — ``Settings -> Download Clients -> Sabnzbd``::

      GET/POST /api/sabnzbd/api?mode=version|get_config|fullstatus
                                |queue|history|addfile|addurl

  Lidarr hands the grabbed NZB to ``addfile``; we parse the album_id,
  enqueue it through the existing download engine, and report progress
  via ``queue``/``history`` so Lidarr imports the finished files itself.

The audio download itself reuses ``processing.process_album_download``;
a small in-memory job registry tracks each grab through
queued -> downloading -> completed/failed.
"""

import logging
import os
import re
import shutil
import threading
import time
import uuid
from email.utils import formatdate
from xml.sax.saxutils import escape as xml_escape, quoteattr

from flask import Blueprint, Response, jsonify, request

import models
from config import load_config

logger = logging.getLogger(__name__)

bp = Blueprint("download_client", __name__)

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")

# Placeholder size reported to Lidarr (true size is unknown up front).
_NOMINAL_ALBUM_SIZE = 100 * 1024 * 1024

# --- Job registry -----------------------------------------------------

_jobs = {}            # nzo_id -> job dict
_album_to_nzo = {}    # album_id -> nzo_id (active jobs only)
_lock = threading.RLock()

_STATUS_QUEUED = "queued"
_STATUS_DOWNLOADING = "downloading"
_STATUS_COMPLETED = "completed"
_STATUS_FAILED = "failed"

_HISTORY_LIMIT = 200


def _new_nzo_id():
    return "SABnzbd_nzo_" + uuid.uuid4().hex[:12]


def _persist(job):
    """Write a job through to SQLite so it survives a restart."""
    try:
        models.upsert_client_job(job)
    except Exception:
        logger.warning(
            "Failed to persist download-client job %s",
            job.get("nzo_id"), exc_info=True,
        )


def register_grab(album_id, name, category):
    """Register a grabbed release and enqueue it for download.

    Returns the SABnzbd ``nzo_id`` Lidarr will use to track it. If the
    album already has an active job, that job's id is returned so a
    re-grab is idempotent.
    """
    album_id = int(album_id)
    with _lock:
        existing = _album_to_nzo.get(album_id)
        if existing and existing in _jobs:
            return existing
        nzo_id = _new_nzo_id()
        _jobs[nzo_id] = {
            "nzo_id": nzo_id,
            "album_id": album_id,
            "name": name or f"album {album_id}",
            "category": category or "",
            "status": _STATUS_QUEUED,
            "storage": "",
            "size": _NOMINAL_ALBUM_SIZE,
            "error": "",
            "added_ts": time.time(),
            "completed_ts": None,
        }
        _album_to_nzo[album_id] = nzo_id
        _persist(_jobs[nzo_id])
        _prune_history()
    models.enqueue_album(album_id)
    logger.info(
        "Lidarr grabbed album %s (%s) -> %s", album_id, name, nzo_id,
    )
    return nzo_id


def is_client_album(album_id):
    """True if this album was grabbed by Lidarr and is still active."""
    with _lock:
        nzo_id = _album_to_nzo.get(int(album_id))
        if not nzo_id:
            return False
        job = _jobs.get(nzo_id)
        return bool(
            job and job["status"] in (_STATUS_QUEUED, _STATUS_DOWNLOADING)
        )


def mark_downloading(album_id):
    with _lock:
        job = _active_job(album_id)
        if job:
            job["status"] = _STATUS_DOWNLOADING
            _persist(job)


def mark_completed(album_id, storage, size=None):
    with _lock:
        job = _active_job(album_id)
        if not job:
            return
        job["status"] = _STATUS_COMPLETED
        job["storage"] = storage or ""
        if size:
            job["size"] = int(size)
        job["completed_ts"] = time.time()
        _album_to_nzo.pop(int(album_id), None)
        _persist(job)


def mark_failed(album_id, error):
    with _lock:
        job = _active_job(album_id)
        if not job:
            return
        job["status"] = _STATUS_FAILED
        job["error"] = (error or "")[:500]
        job["completed_ts"] = time.time()
        _album_to_nzo.pop(int(album_id), None)
        _persist(job)


def _active_job(album_id):
    nzo_id = _album_to_nzo.get(int(album_id))
    return _jobs.get(nzo_id) if nzo_id else None


def _prune_history():
    """Drop the oldest terminal jobs once the registry grows too large."""
    if len(_jobs) <= _HISTORY_LIMIT:
        return
    terminal = sorted(
        (j for j in _jobs.values()
         if j["status"] in (_STATUS_COMPLETED, _STATUS_FAILED)),
        key=lambda j: j.get("completed_ts") or 0,
    )
    while len(_jobs) > _HISTORY_LIMIT and terminal:
        victim = terminal.pop(0)
        _jobs.pop(victim["nzo_id"], None)
        try:
            models.delete_client_job(victim["nzo_id"])
        except Exception:
            logger.warning(
                "Failed to delete pruned job %s", victim["nzo_id"],
                exc_info=True,
            )


def remove_job(nzo_id, delete_files=False):
    """Remove a job from the registry (queue or history delete)."""
    with _lock:
        job = _jobs.pop(nzo_id, None)
        if not job:
            return False
        _album_to_nzo.pop(job["album_id"], None)
    try:
        models.delete_client_job(nzo_id)
    except Exception:
        logger.warning(
            "Failed to delete job %s from DB", nzo_id, exc_info=True,
        )
    if job["status"] in (_STATUS_QUEUED, _STATUS_DOWNLOADING):
        try:
            models.dequeue_album(job["album_id"])
        except Exception:
            logger.warning(
                "Failed to dequeue album %s on job removal",
                job["album_id"], exc_info=True,
            )
    if delete_files and job.get("storage"):
        _safe_rmtree(job["storage"])
    return True


def _safe_rmtree(path):
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(DOWNLOAD_DIR) if DOWNLOAD_DIR else ""
        if base and (real == base or real.startswith(base + os.sep)):
            shutil.rmtree(real, ignore_errors=True)
    except OSError:
        logger.warning("Failed to remove %s", path, exc_info=True)


def restore_jobs():
    """Reload persisted jobs at startup and resume interrupted ones.

    Without this, an in-memory job lost on restart leaves an orphaned
    partial download and Lidarr's poll would see the grab vanish. Jobs
    that were mid-download are reset to 'queued' and re-enqueued so the
    queue processor picks them up again; completed/failed jobs stay in
    history so Lidarr can still import / see them.
    """
    try:
        rows = models.get_all_client_jobs()
    except Exception:
        logger.warning("Failed to restore download-client jobs", exc_info=True)
        return
    with _lock:
        for job in rows:
            if job.get("status") == _STATUS_DOWNLOADING:
                job["status"] = _STATUS_QUEUED  # interrupted: retry
            _jobs[job["nzo_id"]] = job
            if job["status"] == _STATUS_QUEUED:
                _album_to_nzo[int(job["album_id"])] = job["nzo_id"]
        to_resume = [
            j for j in _jobs.values() if j["status"] == _STATUS_QUEUED
        ]
    for job in to_resume:
        try:
            models.enqueue_album(int(job["album_id"]))
            _persist(job)
        except Exception:
            logger.warning(
                "Failed to resume job %s", job["nzo_id"], exc_info=True,
            )
    if _jobs:
        logger.info(
            "Restored %d download-client job(s); resumed %d",
            len(_jobs), len(to_resume),
        )


def run_album_job(album_id, force=False):
    """Queue-processor entry point for a Lidarr-grabbed album.

    Wraps ``processing.process_album_download`` so the in-memory job
    transitions to completed/failed once the engine finishes.
    """
    import processing  # lazy to avoid an import cycle

    mark_downloading(album_id)
    try:
        result = processing.process_album_download(album_id, force)
    except Exception as exc:  # pragma: no cover - defensive
        logger.error(
            "Download job for album %s crashed: %s",
            album_id, exc, exc_info=True,
        )
        mark_failed(album_id, str(exc))
        return
    if result.get("success"):
        mark_completed(album_id, result.get("album_path", ""))
    elif result.get("error") == "Busy":
        # The engine was already running another album: don't fail the
        # grab (Lidarr would blocklist it). Re-queue and leave the job
        # in 'downloading' so the queue processor retries it shortly.
        models.enqueue_album(album_id)
    else:
        mark_failed(album_id, result.get("error", "Download failed"))


def _snapshot_jobs():
    with _lock:
        return [dict(j) for j in _jobs.values()]


# --- Progress -------------------------------------------------------------


def _album_percentage(album_id):
    """Best-effort completion percentage for the active download."""
    try:
        import processing
        snap = processing.get_download_status()
    except Exception:
        return 0
    if not snap.get("active") or snap.get("album_id") != album_id:
        return 0
    tracks = snap.get("tracks") or []
    if not tracks:
        return 0
    done = sum(
        1 for t in tracks
        if t.get("status") in ("done", "failed", "skipped")
    )
    return int(done / len(tracks) * 100)


# --- Auth -----------------------------------------------------------------


def _configured_key():
    return (load_config().get("download_client_api_key") or "").strip()


def _client_enabled():
    cfg = load_config()
    return bool(
        cfg.get("download_client_enabled")
        and (cfg.get("download_client_api_key") or "").strip()
    )


def _check_apikey():
    key = _configured_key()
    if not key:
        return False
    provided = request.values.get("apikey", "") or request.values.get(
        "r", "",
    )
    return provided == key


# --- Newznab indexer ------------------------------------------------------


def _norm(text):
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def _retry_cooldown_seconds():
    """Seconds to wait before re-offering a tried album (0 = disabled)."""
    try:
        hours = float(load_config().get("scheduler_retry_after_hours", 24))
    except (TypeError, ValueError):
        hours = 24.0
    return hours * 3600 if hours > 0 else 0


def _active_album_ids():
    with _lock:
        return set(_album_to_nzo.keys())


def _recently_attempted_ids():
    """Album ids tried within the retry cooldown window."""
    cooldown = _retry_cooldown_seconds()
    if not cooldown:
        return set()
    try:
        return models.get_attempted_album_ids_since(time.time() - cooldown)
    except Exception:
        logger.warning("attempted-ids lookup failed", exc_info=True)
        return set()


def _excluded_album_ids():
    """Albums Lidarr must not (re)grab now.

    Combines albums with an in-flight job and albums attempted within the
    retry cooldown. This breaks the infinite re-grab loop where a failing
    album stays "missing", gets re-offered, re-grabbed and re-fails. After
    the cooldown the album is offered again (with a fresh release guid) so
    transient failures still get retried.
    """
    return _active_album_ids() | _recently_attempted_ids()


def _match_album(artist, album, excluded=None):
    """Resolve a search to the best matching cached album dict, or None."""
    n_artist = _norm(artist)
    n_album = _norm(album)
    if not n_artist and not n_album:
        return None
    try:
        index = models.get_cached_album_index()
    except Exception:
        logger.warning("Album index lookup failed", exc_info=True)
        return None
    best = None
    best_score = 0
    for row in index:
        r_artist = _norm(row.get("artist_name"))
        r_album = _norm(row.get("title"))
        score = 0
        if n_artist:
            if r_artist == n_artist:
                score += 2
            elif n_artist in r_artist or r_artist in n_artist:
                score += 1
            else:
                continue
        if n_album:
            if r_album == n_album:
                score += 2
            elif n_album in r_album or r_album in n_album:
                score += 1
            else:
                continue
        if score > best_score:
            best_score = score
            best = row
    if best and excluded and int(best.get("album_id") or 0) in excluded:
        return None
    return best


def _quality_token(cfg):
    fmt = (cfg.get("audio_format") or "mp3").lower()
    if fmt == "mp3":
        quality = str(cfg.get("audio_quality") or "320")
        return f"MP3 {quality}"
    if fmt == "flac":
        return "FLAC"
    if fmt in ("m4a", "aac"):
        return "AAC"
    if fmt == "opus":
        return "Opus"
    if fmt == "ogg":
        return "Vorbis"
    return "MP3 320"


def _release_title(album, cfg):
    artist = album.get("artist_name", "")
    title = album.get("title", "")
    year = str(album.get("release_date", ""))[:4]
    parts = [f"{artist} - {title}"]
    if year:
        parts.append(f"({year})")
    parts.append("WEB")
    parts.append(_quality_token(cfg))
    return " ".join(p for p in parts if p)


def _newznab_error(code, description):
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<error code="{code}" description="{xml_escape(description)}"/>'
    )
    return Response(body, mimetype="application/xml", status=200)


def _caps_xml():
    server_title = "Lidarr YouTube Downloader"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<caps>\n"
        f'  <server title={quoteattr(server_title)}/>\n'
        '  <limits max="100" default="50"/>\n'
        "  <searching>\n"
        '    <search available="yes" supportedParams="q"/>\n'
        '    <music-search available="yes"'
        ' supportedParams="q,artist,album,year"/>\n'
        '    <audio-search available="yes"'
        ' supportedParams="q,artist,album,year"/>\n'
        "  </searching>\n"
        "  <categories>\n"
        '    <category id="3000" name="Audio">\n'
        '      <subcat id="3010" name="Audio/MP3"/>\n'
        '      <subcat id="3040" name="Audio/Lossless"/>\n'
        "    </category>\n"
        "  </categories>\n"
        "</caps>"
    )


def _search_xml(albums, cfg, base_url):
    api_key = _configured_key()
    # Time-bucketed guid so that, once the retry cooldown elapses, a
    # failed (and Lidarr-blocklisted) release is re-offered with a fresh
    # guid and can be retried, while staying stable within one window so
    # repeated RSS polls don't trigger duplicate grabs.
    window = _retry_cooldown_seconds() or 86400
    bucket = int(time.time() // window)
    items = []
    for album in albums:
        album_id = album.get("album_id")
        title = _release_title(album, cfg)
        track_count = int(album.get("track_count") or 10) or 10
        size = max(track_count, 1) * 8 * 1024 * 1024
        dl_url = (
            f"{base_url}api/newznab/api?t=get&id={album_id}"
            f"&apikey={api_key}"
        )
        pub = formatdate(
            _release_pubdate(album.get("release_date")), usegmt=True,
        )
        guid = f"lidarr-yt-{album_id}-{bucket}"
        items.append(
            "    <item>\n"
            f"      <title>{xml_escape(title)}</title>\n"
            f'      <guid isPermaLink="false">{guid}</guid>\n'
            f"      <link>{xml_escape(dl_url)}</link>\n"
            f"      <pubDate>{pub}</pubDate>\n"
            f"      <size>{size}</size>\n"
            f"      <category>3000</category>\n"
            f'      <enclosure url={quoteattr(dl_url)} length="{size}"'
            ' type="application/x-nzb"/>\n'
            '      <newznab:attr name="category" value="3000"/>\n'
            '      <newznab:attr name="category" value="3010"/>\n'
            f'      <newznab:attr name="size" value="{size}"/>\n'
            "    </item>"
        )
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"'
        ' xmlns:newznab="http://www.newznab.com/DTD/2010/feeds/attributes/">\n'
        "  <channel>\n"
        "    <title>Lidarr YouTube Downloader</title>\n"
        f"{chr(10).join(items)}\n"
        "  </channel>\n"
        "</rss>"
    )
    return Response(body, mimetype="application/rss+xml")


def _release_pubdate(release_date):
    if release_date:
        for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d"):
            try:
                return time.mktime(time.strptime(release_date[:19], fmt))
            except (ValueError, TypeError):
                continue
    return time.time()


def _build_nzb(album_id, title, category):
    """Generate a minimal NZB whose meta encodes the Lidarr album_id."""
    subject = f"{title} [lidarr_album_id={album_id}]"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE nzb PUBLIC "-//newzBin//DTD NZB 1.1//EN"'
        ' "http://www.newzbin.com/DTD/nzb/nzb-1.1.dtd">\n'
        '<nzb xmlns="http://www.newzbin.com/DTD/2003/nzb">\n'
        "  <head>\n"
        f'    <meta type="lidarr_album_id">{int(album_id)}</meta>\n'
        f"    <meta type=\"title\">{xml_escape(title)}</meta>\n"
        f"    <meta type=\"category\">{xml_escape(category or '')}</meta>\n"
        "  </head>\n"
        f'  <file poster="lidarr-youtube-downloader@localhost"'
        f' date="{int(time.time())}" subject={quoteattr(subject)}>\n'
        "    <groups><group>alt.binaries.lidarr</group></groups>\n"
        "    <segments>\n"
        '      <segment bytes="1" number="1">placeholder@localhost</segment>\n'
        "    </segments>\n"
        "  </file>\n"
        "</nzb>"
    )


def _parse_album_id_from_nzb(data):
    """Extract the embedded Lidarr album_id from NZB bytes/text."""
    if isinstance(data, bytes):
        try:
            data = data.decode("utf-8", "ignore")
        except Exception:
            return None
    m = re.search(
        r'<meta[^>]*type="lidarr_album_id"[^>]*>\s*(\d+)\s*</meta>', data,
    )
    if m:
        return int(m.group(1))
    m = re.search(r"lidarr_album_id=(\d+)", data)
    if m:
        return int(m.group(1))
    return None


@bp.route("/api/newznab/api", methods=["GET"])
def newznab_api():
    if not _client_enabled():
        return _newznab_error(101, "Download client not enabled")
    t = (request.args.get("t") or "").lower()
    if t == "caps":
        # caps is also used by Lidarr's "Test" before an API key is set
        # in some flows, so allow it without strict auth.
        return Response(_caps_xml(), mimetype="application/xml")
    if not _check_apikey():
        return _newznab_error(100, "Incorrect user credentials")
    if t == "get":
        album_id = request.args.get("id", type=int)
        if not album_id:
            return _newznab_error(200, "Missing id")
        cfg = load_config()
        album = _album_by_id(album_id)
        title = _release_title(album, cfg) if album else f"album {album_id}"
        nzb = _build_nzb(
            album_id, title, cfg.get("download_client_category", "music"),
        )
        resp = Response(nzb, mimetype="application/x-nzb")
        resp.headers["Content-Disposition"] = (
            f'attachment; filename="lidarr-yt-{album_id}.nzb"'
        )
        return resp
    if t in ("search", "music", "album", "audio", "tvsearch", ""):
        cfg = load_config()
        artist = request.args.get("artist", "")
        album = request.args.get("album", "")
        q = request.args.get("q", "")
        excluded = _excluded_album_ids()
        if not artist and not album and not q:
            # No search terms: this is Lidarr's indexer Test / RSS sync.
            # Expose the cached missing-albums list as the feed so the
            # test returns results and Lidarr's RSS sync can auto-grab
            # missing albums. The feed shrinks naturally as albums stop
            # being missing.
            limit = request.args.get("limit", type=int) or 100
            offset = request.args.get("offset", type=int) or 0
            return _search_xml(
                _recent_missing(limit, offset, excluded),
                cfg, request.host_url,
            )
        if q and not (artist or album):
            artist, album = _split_query(q)
        match = _match_album(artist, album, excluded)
        return _search_xml(
            [match] if match else [], cfg, request.host_url,
        )
    return _newznab_error(202, f"No such function: {t}")


def _recent_missing(limit=100, offset=0, excluded=None):
    """Cached missing albums as release dicts, newest first, paged.

    Albums in ``excluded`` (in-flight or recently attempted) are dropped
    so Lidarr does not re-grab a failing album every sync.
    """
    try:
        index = models.get_cached_album_index()
    except Exception:
        logger.warning("Album index lookup failed", exc_info=True)
        return []
    if excluded:
        index = [
            r for r in index
            if int(r.get("album_id") or 0) not in excluded
        ]
    index.sort(key=lambda r: str(r.get("release_date") or ""), reverse=True)
    limit = max(1, min(int(limit), 500))
    offset = max(0, int(offset))
    return index[offset:offset + limit]


def _split_query(q):
    """Split a free-text 'Artist Album' query for matching.

    We cannot reliably separate artist from album, so we hand the whole
    string to both fields and let _match_album's substring scoring sort
    it out.
    """
    return q, q


def _album_by_id(album_id):
    for row in models.get_cached_album_index():
        if int(row.get("album_id") or 0) == int(album_id):
            return row
    return None


# --- SABnzbd download client ---------------------------------------------


def _sab_error(message):
    return jsonify({"status": False, "error": message})


@bp.route("/api/sabnzbd/api", methods=["GET", "POST"])
def sabnzbd_api():
    if not _client_enabled():
        return _sab_error("Download client not enabled")
    mode = (request.values.get("mode") or "").lower()
    if mode == "version":
        return jsonify({"version": "4.2.0"})
    if not _check_apikey():
        return _sab_error("API Key Incorrect")
    if mode == "auth":
        return jsonify({"auth": "apikey"})
    if mode == "get_config":
        return jsonify(_sab_config())
    if mode == "fullstatus":
        return jsonify({"status": {"completedir": _complete_dir()}})
    if mode == "queue":
        return _sab_queue()
    if mode == "history":
        return _sab_history()
    if mode == "addfile":
        return _sab_addfile()
    if mode == "addurl":
        return _sab_addurl()
    if mode in ("change_cat", "switch", "config", "retry", "pause", "resume"):
        return jsonify({"status": True})
    return _sab_error(f"Unknown mode: {mode}")


def _complete_dir():
    return DOWNLOAD_DIR or "/downloads"


def _sab_config():
    category = load_config().get("download_client_category", "music")
    return {
        "config": {
            "misc": {
                "complete_dir": _complete_dir(),
                "pre_check": False,
                "enable_tv_sorting": False,
                "tv_categories": [],
                "enable_movie_sorting": False,
                "movie_categories": [],
                "enable_date_sorting": False,
                "date_categories": [],
                "history_retention": "",
                "history_retention_option": "all",
                "history_retention_number": 0,
                "my_home": _complete_dir(),
            },
            "categories": [
                {"name": "*", "dir": ""},
                {"name": category, "dir": ""},
            ],
            "sorters": [],
        }
    }


def _sab_priority():
    return "Normal"


def _sab_queue():
    name = (request.values.get("name") or "").lower()
    if name == "delete":
        value = request.values.get("value", "")
        del_files = request.values.get("del_files", "0") in ("1", "true")
        if value == "all":
            for nzo in [
                j["nzo_id"] for j in _snapshot_jobs()
                if j["status"] in (_STATUS_QUEUED, _STATUS_DOWNLOADING)
            ]:
                remove_job(nzo, delete_files=del_files)
        elif value:
            remove_job(value, delete_files=del_files)
        return jsonify({"status": True})

    slots = []
    for job in _snapshot_jobs():
        if job["status"] not in (_STATUS_QUEUED, _STATUS_DOWNLOADING):
            continue
        pct = (
            _album_percentage(job["album_id"])
            if job["status"] == _STATUS_DOWNLOADING else 0
        )
        size_mb = job["size"] / (1024 * 1024)
        left_mb = size_mb * (1 - pct / 100.0)
        status = (
            "Downloading" if job["status"] == _STATUS_DOWNLOADING
            else "Queued"
        )
        slots.append({
            "index": len(slots),
            "nzo_id": job["nzo_id"],
            "filename": job["name"],
            "cat": job["category"],
            "status": status,
            "mb": f"{size_mb:.2f}",
            "mbleft": f"{left_mb:.2f}",
            "size": f"{size_mb:.2f} MB",
            "sizeleft": f"{left_mb:.2f} MB",
            "percentage": str(pct),
            "timeleft": "0:00:00",
            "priority": _sab_priority(),
        })
    return jsonify({
        "queue": {
            "paused": False,
            "my_home": _complete_dir(),
            "speed": "0",
            "kbpersec": "0.0",
            "mbleft": f"{sum(float(s['mbleft']) for s in slots):.2f}",
            "noofslots": len(slots),
            "slots": slots,
        }
    })


def _sab_history():
    name = (request.values.get("name") or "").lower()
    if name == "delete":
        value = request.values.get("value", "")
        del_files = request.values.get("del_files", "0") in ("1", "true")
        if value == "all":
            for nzo in [
                j["nzo_id"] for j in _snapshot_jobs()
                if j["status"] in (_STATUS_COMPLETED, _STATUS_FAILED)
            ]:
                remove_job(nzo, delete_files=del_files)
        elif value:
            remove_job(value, delete_files=del_files)
        return jsonify({"status": True})

    slots = []
    for job in _snapshot_jobs():
        if job["status"] not in (_STATUS_COMPLETED, _STATUS_FAILED):
            continue
        status = (
            "Completed" if job["status"] == _STATUS_COMPLETED else "Failed"
        )
        slots.append({
            "nzo_id": job["nzo_id"],
            "name": job["name"],
            "nzb_name": job["name"],
            "category": job["category"],
            "status": status,
            "storage": job.get("storage", ""),
            "path": job.get("storage", ""),
            "bytes": int(job.get("size") or 0),
            "fail_message": job.get("error", ""),
            "download_time": 0,
        })
    return jsonify({
        "history": {
            "noofslots": len(slots),
            "slots": slots,
        }
    })


def _sab_addfile():
    category = (
        request.values.get("cat")
        or load_config().get("download_client_category", "music")
    )
    data = None
    for field in ("name", "nzbfile", "file"):
        if field in request.files:
            data = request.files[field].read()
            break
    if data is None:
        # some callers post the NZB as a raw form value
        data = request.values.get("name") or request.get_data()
    album_id = _parse_album_id_from_nzb(data) if data else None
    if not album_id:
        return _sab_error("Could not parse album id from NZB")
    if _grab_blocked(album_id):
        return _sab_error(
            "Album attempted recently; skipping to avoid a retry loop"
        )
    nzo_id = register_grab(album_id, _grab_name(album_id), category)
    return jsonify({"status": True, "nzo_ids": [nzo_id]})


def _sab_addurl():
    url = request.values.get("name", "")
    category = (
        request.values.get("cat")
        or load_config().get("download_client_category", "music")
    )
    m = re.search(r"[?&]id=(\d+)", url)
    if not m:
        return _sab_error("Could not parse album id from URL")
    album_id = int(m.group(1))
    if _grab_blocked(album_id):
        return _sab_error(
            "Album attempted recently; skipping to avoid a retry loop"
        )
    nzo_id = register_grab(album_id, _grab_name(album_id), category)
    return jsonify({"status": True, "nzo_ids": [nzo_id]})


def _grab_blocked(album_id):
    """True if a grab should be refused: recently attempted and not the
    same in-flight job (register_grab is idempotent for active albums)."""
    album_id = int(album_id)
    if is_client_album(album_id):
        return False
    return album_id in _recently_attempted_ids()


def _grab_name(album_id):
    album = _album_by_id(album_id)
    if album:
        return _release_title(album, load_config())
    return f"album {album_id}"
