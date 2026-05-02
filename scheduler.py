"""Scheduler for automatic missing album detection and queueing.

Periodically checks Lidarr for missing albums and optionally adds
them to the download queue.
"""

import logging
import time

import schedule

import models
from config import load_config
from lidarr import get_missing_albums
from notifications import send_notifications
from processing import download_process, queue_lock

logger = logging.getLogger(__name__)


def scheduled_check():
    """Check Lidarr for new missing albums and optionally queue them."""
    with queue_lock:
        is_active = download_process["active"]
    if is_active:
        return
    config = load_config()
    albums = get_missing_albums()

    if not albums:
        return

    interval_minutes = int(config.get("scheduler_interval", 60))
    lookback_seconds = interval_minutes * 60 * 2
    recent_history_ids = models.get_history_album_ids_since(
        time.time() - lookback_seconds
    )
    current_download_id = download_process.get("album_id")

    queued_ids = {row["album_id"] for row in models.get_queue()}

    new_albums = [
        album
        for album in albums
        if album["id"] not in queued_ids
        and album["id"] not in recent_history_ids
        and album["id"] != current_download_id
        and album.get("missingTrackCount", 0) > 0
    ]

    if not new_albums:
        return

    if config.get("scheduler_auto_download", True):
        logger.info(
            f"Scheduler: Found {len(new_albums)} new missing albums,"
            " adding to queue..."
        )
        send_notifications(
            f"Scheduler: Adding {len(new_albums)} new missing"
            " albums to queue...",
            log_type="download_started",
            embed_data={
                "title": "Scheduler",
                "description": (
                    f"Adding {len(new_albums)} new missing"
                    " albums to queue"
                ),
                "color": 0x3498DB,
            },
        )
        for album in new_albums:
            models.enqueue_album(album["id"])
    else:
        logger.info(
            f"Scheduler: Found {len(new_albums)} missing albums"
            " (Auto-Download disabled)"
        )
        send_notifications(
            f"Scheduler: Found {len(new_albums)} missing albums"
            " (Auto-DL Disabled)",
            log_type="download_started",
            embed_data={
                "title": "Scheduler",
                "description": (
                    f"Found {len(new_albums)} missing albums"
                    " (Auto-DL Disabled)"
                ),
                "color": 0xE67E22,
            },
        )


def run_scheduler():
    """Run the schedule loop forever, checking every 10 seconds."""
    while True:
        schedule.run_pending()
        time.sleep(10)


def setup_scheduler():
    """Configure the scheduler based on current config settings."""
    config = load_config()
    schedule.clear()
    if config.get("scheduler_enabled"):
        interval = int(config.get("scheduler_interval", 60))
        schedule.every(interval).minutes.do(scheduled_check)
