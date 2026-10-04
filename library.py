import logging
import re
import threading
import time

import db
import lidarr_sync
import models
from lidarr import lidarr_request

logger = logging.getLogger(__name__)

MBID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)
LOOKUP_KINDS = ("artist", "album")
MONITOR_OPTIONS = (
    "all", "future", "missing", "existing", "latest", "first", "none",
)
NEW_ITEMS_OPTIONS = ("all", "new", "none")
LOOKUP_LIMIT = 30
TERM_MAX_LENGTH = 120
TRACK_WAIT_SECONDS = 240
TRACK_POLL_SECONDS = 5
SYNC_DELAYS = (15, 90)
PENDING_KEEP_SECONDS = 3600

_pending = {}
_pending_lock = threading.Lock()
_sleep = time.sleep


class LibraryError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def _int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _check(result, what):
    if isinstance(result, dict) and "error" in result:
        raise LibraryError(f"{what}: {result['error']}", 502)
    return result


def _image(images, *kinds):
    for kind in kinds:
        for img in images or ():
            if not isinstance(img, dict):
                continue
            if (img.get("coverType") or "").lower() != kind:
                continue
            for key in ("remoteUrl", "url"):
                url = img.get(key) or ""
                if isinstance(url, str) and url.startswith(("http://", "https://")):
                    return url
    return ""


def artist_summary(item):
    stats = item.get("statistics") or {}
    artist_id = _int(item.get("id"))
    return {
        "foreignArtistId": item.get("foreignArtistId") or "",
        "name": item.get("artistName") or "",
        "disambiguation": item.get("disambiguation") or "",
        "type": item.get("artistType") or "",
        "overview": (item.get("overview") or "")[:400],
        "genres": [g for g in (item.get("genres") or []) if isinstance(g, str)][:3],
        "image": _image(item.get("images"), "poster", "cover", "fanart", "banner"),
        "id": artist_id or None,
        "inLibrary": artist_id > 0,
        "monitored": bool(item.get("monitored")) if artist_id > 0 else False,
        "albumCount": _int(stats.get("albumCount")) if artist_id > 0 else None,
    }


def album_summary(item):
    artist = item.get("artist") or {}
    stats = item.get("statistics") or {}
    album_id = _int(item.get("id"))
    releases = [r for r in (item.get("releases") or []) if isinstance(r, dict)]
    track_count = max((_int(r.get("trackCount")) for r in releases), default=0)
    if album_id > 0 and _int(stats.get("trackCount")):
        track_count = _int(stats.get("trackCount"))
    missing = None
    if album_id > 0 and stats:
        missing = max(0, _int(stats.get("trackCount")) - _int(stats.get("trackFileCount")))
    remote_cover = item.get("remoteCover") or ""
    if not (isinstance(remote_cover, str) and remote_cover.startswith(("http://", "https://"))):
        remote_cover = _image(item.get("images"), "cover", "disc")
    return {
        "foreignAlbumId": item.get("foreignAlbumId") or "",
        "title": item.get("title") or "",
        "disambiguation": item.get("disambiguation") or "",
        "artistName": artist.get("artistName") or "",
        "foreignArtistId": artist.get("foreignArtistId") or "",
        "year": (item.get("releaseDate") or "")[:4],
        "type": item.get("albumType") or "",
        "secondaryTypes": [
            t for t in (item.get("secondaryTypes") or []) if isinstance(t, str)
        ],
        "image": remote_cover,
        "trackCount": track_count,
        "id": album_id or None,
        "inLibrary": album_id > 0,
        "monitored": bool(item.get("monitored")) if album_id > 0 else False,
        "missingTracks": missing,
        "artistInLibrary": _int(artist.get("id")) > 0,
    }


def _clean_term(term):
    term = " ".join(str(term or "").split())
    if len(term) < 2:
        raise LibraryError("Type at least 2 characters to search.")
    return term[:TERM_MAX_LENGTH]


def search(kind, term):
    if kind not in LOOKUP_KINDS:
        raise LibraryError("type must be 'artist' or 'album'")
    term = _clean_term(term)
    results = _check(
        lidarr_request(f"{kind}/lookup", params={"term": term}),
        "Lidarr search failed",
    )
    if not isinstance(results, list):
        return []
    summarize = artist_summary if kind == "artist" else album_summary
    key = "foreignArtistId" if kind == "artist" else "foreignAlbumId"
    out = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        summary = summarize(item)
        if not summary[key] or summary[key] in seen:
            continue
        seen.add(summary[key])
        out.append(summary)
        if len(out) >= LOOKUP_LIMIT:
            break
    return out


def get_add_options():
    folders = _check(lidarr_request("rootfolder"), "Cannot read Lidarr root folders")
    qualities = _check(
        lidarr_request("qualityprofile"), "Cannot read Lidarr quality profiles",
    )
    metadata = _check(
        lidarr_request("metadataprofile"), "Cannot read Lidarr metadata profiles",
    )
    return {
        "rootFolders": [
            {
                "path": f.get("path"),
                "name": f.get("name") or f.get("path"),
                "freeSpace": f.get("freeSpace"),
                "defaultQualityProfileId": _int(f.get("defaultQualityProfileId")) or None,
                "defaultMetadataProfileId": _int(f.get("defaultMetadataProfileId")) or None,
                "defaultMonitorOption": f.get("defaultMonitorOption") or "",
                "defaultNewItemMonitorOption": f.get("defaultNewItemMonitorOption") or "",
            }
            for f in (folders if isinstance(folders, list) else [])
            if isinstance(f, dict) and f.get("path")
        ],
        "qualityProfiles": [
            {"id": _int(q.get("id")), "name": q.get("name") or ""}
            for q in (qualities if isinstance(qualities, list) else [])
            if isinstance(q, dict) and _int(q.get("id"))
        ],
        "metadataProfiles": [
            {"id": _int(m.get("id")), "name": m.get("name") or ""}
            for m in (metadata if isinstance(metadata, list) else [])
            if isinstance(m, dict) and _int(m.get("id"))
            and (m.get("name") or "").lower() != "none"
        ],
        "monitorOptions": list(MONITOR_OPTIONS),
        "newItemOptions": list(NEW_ITEMS_OPTIONS),
    }


def resolve_options(payload, options):
    folders = options["rootFolders"]
    if not folders:
        raise LibraryError(
            "Lidarr has no root folder. Add one in Lidarr (Settings → Media"
            " Management) first.", 409,
        )
    path = payload.get("rootFolderPath") or folders[0]["path"]
    folder = next((f for f in folders if f["path"] == path), None)
    if folder is None:
        raise LibraryError("Unknown root folder.")
    quality_ids = [q["id"] for q in options["qualityProfiles"]]
    metadata_ids = [m["id"] for m in options["metadataProfiles"]]
    if not quality_ids or not metadata_ids:
        raise LibraryError("Lidarr has no quality or metadata profile.", 409)
    quality = _int(payload.get("qualityProfileId")) or folder["defaultQualityProfileId"] or quality_ids[0]
    metadata = _int(payload.get("metadataProfileId")) or folder["defaultMetadataProfileId"] or metadata_ids[0]
    if quality not in quality_ids:
        raise LibraryError("Unknown quality profile.")
    if metadata not in metadata_ids:
        raise LibraryError("Unknown metadata profile.")
    return {
        "rootFolderPath": path,
        "qualityProfileId": quality,
        "metadataProfileId": metadata,
    }


def _mbid(value, what):
    value = str(value or "").strip().lower()
    if not MBID_RE.match(value):
        raise LibraryError(f"{what} must be a MusicBrainz id.")
    return value


def _lookup_by_id(kind, mbid):
    results = _check(
        lidarr_request(f"{kind}/lookup", params={"term": f"lidarr:{mbid}"}),
        "Lidarr lookup failed",
    )
    key = "foreignArtistId" if kind == "artist" else "foreignAlbumId"
    for item in results if isinstance(results, list) else []:
        if isinstance(item, dict) and (item.get(key) or "").lower() == mbid:
            return item
    raise LibraryError(f"This {kind} was not found on MusicBrainz.", 404)


def _schedule_sync():
    def runner():
        for delay in SYNC_DELAYS:
            _sleep(delay)
            try:
                lidarr_sync.trigger_sync()
            except Exception as e:
                logger.debug("Library sync after add failed: %s", e)

    threading.Thread(target=runner, daemon=True, name="library-sync").start()


def add_artist(payload):
    mbid = _mbid(payload.get("foreignArtistId"), "foreignArtistId")
    monitor = payload.get("monitor") or "all"
    if monitor not in MONITOR_OPTIONS:
        raise LibraryError("Unknown monitor option.")
    new_items = payload.get("monitorNewItems") or "all"
    if new_items not in NEW_ITEMS_OPTIONS:
        raise LibraryError("Unknown new-album option.")
    resolved = resolve_options(payload, get_add_options())
    item = _lookup_by_id("artist", mbid)
    if _int(item.get("id")) > 0:
        raise LibraryError(
            f"{item.get('artistName') or 'This artist'} is already in your library.",
            409,
        )
    body = dict(item)
    body.pop("id", None)
    body.update(resolved)
    body.update({
        "monitored": monitor != "none",
        "monitorNewItems": new_items,
        "addOptions": {"monitor": monitor, "searchForMissingAlbums": False},
    })
    created = _check(
        lidarr_request("artist", method="POST", data=body),
        "Lidarr could not add the artist",
    )
    name = created.get("artistName") or item.get("artistName") or ""
    logger.info("Added artist to Lidarr: %s (monitor=%s)", name, monitor)
    _schedule_sync()
    return {"id": _int(created.get("id")) or None, "name": name, "monitor": monitor}


def add_album(payload):
    mbid = _mbid(payload.get("foreignAlbumId"), "foreignAlbumId")
    download = bool(payload.get("download"))
    item = _lookup_by_id("album", mbid)
    title = item.get("title") or ""
    artist = dict(item.get("artist") or {})
    artist_name = artist.get("artistName") or ""
    album_id = _int(item.get("id"))
    if album_id > 0:
        status = "existing"
        if not item.get("monitored"):
            _check(
                lidarr_request(
                    "album/monitor", method="PUT",
                    data={"albumIds": [album_id], "monitored": True},
                ),
                "Lidarr could not monitor the album",
            )
            status = "monitored"
            logger.info("Monitored album in Lidarr: %s — %s", artist_name, title)
            _schedule_sync()
    else:
        if not artist.get("foreignArtistId"):
            raise LibraryError("This album has no artist on MusicBrainz.", 409)
        if _int(artist.get("id")) <= 0:
            artist.pop("id", None)
            artist.update(resolve_options(payload, get_add_options()))
            artist.update({
                "monitored": True,
                "monitorNewItems": "none",
                "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
            })
        body = dict(item)
        body.pop("id", None)
        body.update({
            "artist": artist,
            "monitored": True,
            "anyReleaseOk": True,
            "addOptions": {"searchForNewAlbum": False},
        })
        created = _check(
            lidarr_request("album", method="POST", data=body),
            "Lidarr could not add the album",
        )
        album_id = _int(created.get("id"))
        if album_id <= 0:
            raise LibraryError("Lidarr did not return the new album.", 502)
        status = "added"
        logger.info("Added album to Lidarr: %s — %s", artist_name, title)
        _schedule_sync()
    result = {
        "id": album_id,
        "title": title,
        "artistName": artist_name,
        "status": status,
        "queued": None,
    }
    if download:
        result["queued"] = queue_when_ready(album_id, title, artist_name)
    return result


def _set_pending(album_id, **fields):
    with _pending_lock:
        entry = _pending.setdefault(album_id, {"albumId": album_id})
        entry.update(fields)
        entry["updated"] = time.time()
        return dict(entry)


def pending_adds():
    cutoff = time.time() - PENDING_KEEP_SECONDS
    with _pending_lock:
        for album_id in [k for k, v in _pending.items() if v["updated"] < cutoff]:
            del _pending[album_id]
        return sorted(
            (dict(v) for v in _pending.values()),
            key=lambda v: v["updated"], reverse=True,
        )


def _has_tracks(album_id):
    tracks = lidarr_request("track", params={"albumId": album_id})
    return isinstance(tracks, list) and len(tracks) > 0


def wait_and_enqueue(album_id, deadline_seconds=TRACK_WAIT_SECONDS):
    started = time.monotonic()
    try:
        while True:
            if _has_tracks(album_id):
                models.enqueue_album(album_id, force=True)
                _set_pending(album_id, state="queued", message="Added to the download queue")
                logger.info("Queued album %s after adding it to Lidarr", album_id)
                return True
            if time.monotonic() - started >= deadline_seconds:
                _set_pending(
                    album_id, state="timeout",
                    message=(
                        "Lidarr has not loaded the tracklist yet; queue it"
                        " from the Library once it appears."
                    ),
                )
                return False
            _sleep(TRACK_POLL_SECONDS)
    except Exception as e:
        logger.warning("Could not queue album %s after adding it: %s", album_id, e)
        _set_pending(album_id, state="failed", message=str(e)[:200])
        return False
    finally:
        try:
            db.close_db()
        except Exception:
            pass


def queue_when_ready(album_id, title="", artist_name=""):
    entry = _set_pending(
        album_id, title=title, artistName=artist_name, state="waiting",
        message="Waiting for Lidarr to load the tracklist",
    )
    threading.Thread(
        target=wait_and_enqueue, args=(album_id,), daemon=True,
        name=f"library-queue-{album_id}",
    ).start()
    return entry["state"]
