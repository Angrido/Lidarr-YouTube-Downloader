"""Background sync worker for Lidarr missing albums.

Paginates the Lidarr `wanted/missing` endpoint in small chunks with
retry/backoff and writes results into the SQLite cache, so the UI can
serve the full library instantly regardless of size.
"""

import logging
import threading
import time

import models
from config import load_config
from lidarr import lidarr_request

logger = logging.getLogger(__name__)

PAGE_SIZE = 100
MAX_RETRIES = 4
INITIAL_BACKOFF_SECONDS = 2
LOOP_TICK_SECONDS = 30
DEFAULT_INTERVAL_MINUTES = 15

_sync_lock = threading.Lock()


def _fetch_page(page):
    backoff = INITIAL_BACKOFF_SECONDS
    last_error = ""
    for attempt in range(MAX_RETRIES):
        wanted = lidarr_request(
            f"wanted/missing?page={page}&pageSize={PAGE_SIZE}"
            f"&sortKey=releaseDate&sortDirection=descending"
            f"&includeArtist=true"
        )
        if isinstance(wanted, dict) and "records" in wanted:
            return wanted, ""
        if isinstance(wanted, dict) and "error" in wanted:
            last_error = wanted["error"]
        else:
            last_error = "Invalid response from Lidarr"
        if attempt < MAX_RETRIES - 1:
            time.sleep(backoff)
            backoff *= 2
    return None, last_error


def _run_sync():
    run_id = models.bump_sync_run_id()
    models.update_sync_state(
        status="running",
        last_attempt_at=time.time(),
        current_page=0,
        total_pages=0,
        total_records=0,
        synced_records=0,
        last_error="",
        current_run_id=run_id,
    )
    page = 1
    synced = 0
    total_records = 0
    total_pages = 0
    while True:
        wanted, error = _fetch_page(page)
        if wanted is None:
            models.update_sync_state(
                status="error",
                last_error=(error or "")[:500],
            )
            logger.warning(
                "Lidarr sync failed at page %d: %s", page, error
            )
            return
        records = wanted.get("records", []) or []
        total_records = wanted.get("totalRecords", total_records) or 0
        if total_records:
            total_pages = max(
                1, (total_records + PAGE_SIZE - 1) // PAGE_SIZE
            )
        for album in records:
            try:
                models.upsert_missing_album(album, run_id)
            except Exception as e:
                logger.warning(
                    "Failed to upsert album %s: %s",
                    album.get("id"), e,
                )
        synced += len(records)
        models.update_sync_state(
            current_page=page,
            total_pages=total_pages,
            total_records=total_records,
            synced_records=synced,
        )
        if not records:
            break
        if total_records and synced >= total_records:
            break
        if len(records) < PAGE_SIZE:
            break
        page += 1
    pruned = models.prune_missing_albums(run_id)
    models.update_sync_state(
        status="idle",
        last_full_sync_at=time.time(),
        last_error="",
    )
    logger.info(
        "Lidarr sync complete: %d albums cached (pruned %d stale)",
        synced, pruned,
    )


def trigger_sync():
    """Start a sync in a background thread; returns False if one is already running."""
    if not _sync_lock.acquire(blocking=False):
        return False

    def runner():
        try:
            _run_sync()
        except Exception as e:
            logger.error(
                "Lidarr sync worker crashed: %s", e, exc_info=True
            )
            try:
                models.update_sync_state(
                    status="error", last_error=str(e)[:500]
                )
            except Exception:
                pass
        finally:
            _sync_lock.release()

    threading.Thread(target=runner, daemon=True).start()
    return True


def sync_loop():
    """Periodically trigger a sync when the cache becomes stale."""
    while True:
        try:
            config = load_config()
            interval_minutes = int(
                config.get("lidarr_sync_interval", DEFAULT_INTERVAL_MINUTES)
            )
            state = models.get_sync_state()
            last = state.get("last_full_sync_at") or 0
            stale = (time.time() - last) >= interval_minutes * 60
            if stale and state.get("status") != "running":
                trigger_sync()
        except Exception as e:
            logger.warning("Sync loop tick failed: %s", e)
        time.sleep(LOOP_TICK_SECONDS)
