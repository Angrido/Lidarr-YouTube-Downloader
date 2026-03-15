"""Data access layer for SQLite-backed persistence.

This is the ONLY module that writes SQL. All other modules call these
functions to read/write track downloads, logs, and the download queue.
"""

import logging
import math
import time
from datetime import datetime

import db

logger = logging.getLogger(__name__)

QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_DOWNLOADING = "downloading"
QUEUE_STATUSES = {QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING}


def _paginate(query, count_query, params, page, per_page):
    """Run a paginated query and return a standard response dict."""
    conn = db.get_db()
    total = conn.execute(count_query, params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = conn.execute(
        query + " LIMIT ? OFFSET ?", (*params, per_page, offset)
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


# --- Track Downloads ---


def get_latest_download_album_id():
    """Return the album_id from the most recent track download, or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT DISTINCT album_id FROM track_downloads"
        " ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def add_track_download(
    *, album_id, album_title, artist_name, track_title, track_number,
    success, error_message, youtube_url, youtube_title, match_score,
    duration_seconds, album_path, lidarr_album_path, cover_url,
):
    """Record a single track download attempt."""
    conn = db.get_db()
    conn.execute(
        """INSERT INTO track_downloads
           (album_id, album_title, artist_name, track_title,
            track_number, success, error_message, youtube_url,
            youtube_title, match_score, duration_seconds,
            album_path, lidarr_album_path, cover_url, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            album_id, album_title, artist_name, track_title,
            track_number, int(success), error_message, youtube_url,
            youtube_title, match_score, duration_seconds,
            album_path, lidarr_album_path, cover_url, time.time(),
        ),
    )
    conn.commit()


def get_track_downloads_for_album(album_id):
    """Return all track download records for an album, newest first."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM track_downloads"
        " WHERE album_id = ? ORDER BY timestamp DESC",
        (album_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_album_history(page=1, per_page=50):
    """Return album-grouped download summaries, newest first."""
    query = """
        SELECT
            album_id,
            album_title,
            artist_name,
            cover_url,
            MAX(timestamp) as latest_timestamp,
            SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as success_count,
            SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as fail_count,
            COUNT(*) as total_count
        FROM track_downloads
        GROUP BY album_id, album_title, artist_name
        ORDER BY latest_timestamp DESC
    """
    count_query = """
        SELECT COUNT(DISTINCT album_id) FROM track_downloads
    """
    conn = db.get_db()
    total = conn.execute(count_query).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = conn.execute(
        query + " LIMIT ? OFFSET ?", (per_page, offset)
    ).fetchall()
    return {
        "items": [dict(row) for row in rows],
        "total": total,
        "page": page,
        "pages": pages,
        "per_page": per_page,
    }


def get_failed_tracks_for_retry(album_id):
    """Return failed tracks for retry UI.

    Returns tracks where the latest attempt for that track has
    success=0. Includes album context from the most recent row.
    """
    conn = db.get_db()
    context_row = conn.execute(
        "SELECT album_title, artist_name, cover_url,"
        " album_path, lidarr_album_path"
        " FROM track_downloads WHERE album_id = ?"
        " ORDER BY timestamp DESC LIMIT 1",
        (album_id,),
    ).fetchone()
    if context_row is None:
        return {
            "failed_tracks": [],
            "album_id": album_id,
            "album_title": "",
            "artist_name": "",
            "cover_url": "",
            "album_path": "",
            "lidarr_album_path": "",
        }
    # Get the latest attempt per track
    rows = conn.execute(
        """
        SELECT t1.track_title, t1.track_number, t1.error_message
        FROM track_downloads t1
        INNER JOIN (
            SELECT track_title, MAX(timestamp) as max_ts
            FROM track_downloads
            WHERE album_id = ?
            GROUP BY track_title
        ) t2 ON t1.track_title = t2.track_title
            AND t1.timestamp = t2.max_ts
        WHERE t1.album_id = ? AND t1.success = 0
        ORDER BY t1.track_number
        """,
        (album_id, album_id),
    ).fetchall()
    ctx = dict(context_row)
    return {
        "failed_tracks": [
            {
                "title": row["track_title"],
                "reason": row["error_message"],
                "track_num": row["track_number"],
            }
            for row in rows
        ],
        "album_id": album_id,
        "album_title": ctx["album_title"],
        "artist_name": ctx["artist_name"],
        "cover_url": ctx["cover_url"],
        "album_path": ctx["album_path"],
        "lidarr_album_path": ctx["lidarr_album_path"],
    }


def get_history_count_today():
    """Count distinct albums with successful tracks since midnight."""
    now = datetime.now()
    today_start = datetime(now.year, now.month, now.day).timestamp()
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(DISTINCT album_id) FROM track_downloads"
        " WHERE success = 1 AND timestamp >= ?",
        (today_start,),
    ).fetchone()
    return row[0]


def get_history_album_ids_since(since_timestamp):
    """Return set of album IDs with successful tracks since timestamp."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT album_id FROM track_downloads"
        " WHERE success = 1 AND timestamp >= ?",
        (since_timestamp,),
    ).fetchall()
    return {row[0] for row in rows}


def clear_history():
    """Delete all track download records."""
    conn = db.get_db()
    conn.execute("DELETE FROM track_downloads")
    conn.commit()


# --- Logs ---


def add_log(
    log_type, album_id, album_title, artist_name,
    details="", total_file_size=0, track_number=None,
):
    """Create a download log entry. Returns the generated log ID."""
    conn = db.get_db()
    ts = int(time.time() * 1000)
    if track_number is not None:
        log_id = f"{ts}_{album_id}_{track_number}"
    else:
        log_id = f"{ts}_{album_id}"
    conn.execute(
        """INSERT INTO download_logs
           (id, type, album_id, album_title, artist_name, timestamp,
            details, total_file_size)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log_id, log_type, album_id, album_title, artist_name,
            time.time(), details, total_file_size,
        ),
    )
    conn.commit()
    return log_id


def get_logs(page=1, per_page=50, log_type=None):
    """Return paginated download logs, newest first."""
    if log_type:
        query = (
            "SELECT * FROM download_logs"
            " WHERE type = ? ORDER BY timestamp DESC"
        )
        count_query = (
            "SELECT COUNT(*) FROM download_logs WHERE type = ?"
        )
        params = (log_type,)
    else:
        query = "SELECT * FROM download_logs ORDER BY timestamp DESC"
        count_query = "SELECT COUNT(*) FROM download_logs"
        params = ()
    return _paginate(query, count_query, params, page, per_page)


def delete_log(log_id):
    """Delete a single log entry by ID. Returns True if deleted."""
    conn = db.get_db()
    cursor = conn.execute(
        "DELETE FROM download_logs WHERE id = ?", (log_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def clear_logs():
    """Delete all download log entries."""
    conn = db.get_db()
    conn.execute("DELETE FROM download_logs")
    conn.commit()


def get_logs_db_size():
    """Estimate the storage used by log text fields."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT SUM(LENGTH(details)) FROM download_logs"
    ).fetchone()
    return row[0] or 0


# --- Queue ---


def enqueue_album(album_id):
    """Add an album to the download queue. Returns False if duplicate."""
    conn = db.get_db()
    existing = conn.execute(
        "SELECT id FROM download_queue WHERE album_id = ?", (album_id,)
    ).fetchone()
    if existing:
        return False
    max_pos = conn.execute(
        "SELECT COALESCE(MAX(position), 0) FROM download_queue"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO download_queue (album_id, position, status)"
        " VALUES (?, ?, ?)",
        (album_id, max_pos + 1, QUEUE_STATUS_QUEUED),
    )
    conn.commit()
    return True


def dequeue_album(album_id):
    """Remove an album from the queue and reorder positions."""
    conn = db.get_db()
    conn.execute(
        "DELETE FROM download_queue WHERE album_id = ?", (album_id,)
    )
    conn.commit()
    _reorder_queue(conn)


def get_queue():
    """Return all queued albums ordered by position."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM download_queue ORDER BY position"
    ).fetchall()
    return [dict(row) for row in rows]


def get_queue_length():
    """Return the number of albums in the queue."""
    conn = db.get_db()
    return conn.execute(
        "SELECT COUNT(*) FROM download_queue"
    ).fetchone()[0]


def pop_next_from_queue():
    """Remove and return the next queued album_id, or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT album_id FROM download_queue"
        " WHERE status = ? ORDER BY position LIMIT 1",
        (QUEUE_STATUS_QUEUED,),
    ).fetchone()
    if row is None:
        return None
    album_id = row[0]
    conn.execute(
        "DELETE FROM download_queue WHERE album_id = ?", (album_id,)
    )
    conn.commit()
    _reorder_queue(conn)
    return album_id


def set_queue_status(album_id, status):
    """Update the status of a queued album."""
    if status not in QUEUE_STATUSES:
        raise ValueError(
            f"Invalid queue status: {status}."
            f" Must be one of {QUEUE_STATUSES}"
        )
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE album_id = ?",
        (status, album_id),
    )
    conn.commit()


def reset_downloading_to_queued():
    """Reset any 'downloading' entries back to 'queued' on startup."""
    conn = db.get_db()
    conn.execute(
        "UPDATE download_queue SET status = ? WHERE status = ?",
        (QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING),
    )
    conn.commit()


def clear_queue():
    """Delete all entries from the download queue."""
    conn = db.get_db()
    conn.execute("DELETE FROM download_queue")
    conn.commit()


def _reorder_queue(conn):
    """Renumber queue positions sequentially starting at 1."""
    rows = conn.execute(
        "SELECT id FROM download_queue ORDER BY position"
    ).fetchall()
    for i, row in enumerate(rows, 1):
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE id = ?",
            (i, row[0]),
        )
    conn.commit()
