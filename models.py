"""Data access layer for SQLite-backed persistence.

This is the ONLY module that writes SQL. All other modules call these
functions to read/write track downloads, logs, and the download queue.
"""

import json
import logging
import math
import time
import uuid
from datetime import datetime, timedelta
from enum import Enum

import db
from utils import video_id_from_url

logger = logging.getLogger(__name__)

class CandidateOutcome(str, Enum):
    """Outcome of a single YouTube candidate attempt."""

    VERIFIED = "verified"
    MISMATCH = "mismatch"
    UNVERIFIED = "unverified"
    DOWNLOAD_FAILED = "download_failed"
    ACCEPTED_NO_VERIFY = "accepted_no_verify"
    ACCEPTED_UNVERIFIED_FALLBACK = "accepted_unverified_fallback"
    REJECTED_LOW_SCORE = "rejected_low_score"


QUEUE_STATUS_QUEUED = "queued"
QUEUE_STATUS_DOWNLOADING = "downloading"
QUEUE_STATUSES = {QUEUE_STATUS_QUEUED, QUEUE_STATUS_DOWNLOADING}

MAX_PER_PAGE = 500

HOST_FAILURE_PREFIX = "Audio postprocessing failed"
# Also a host problem, not the track's: not counted toward its backoff.
DISK_FULL_PREFIX = "Download disk is full"
# Also not the track's fault: YouTube refused this IP and the VPN is about to
# change it (see processing._download_tracks and vpn.rotates_on_block).
BLOCKED_PREFIX = "YouTube blocked this IP"


def _paginate(query_with_limit, count_query, params, page, per_page):
    """Run a paginated query and return a standard response dict.

    query_with_limit must include 'LIMIT ? OFFSET ?' placeholders at the end.
    """
    per_page = max(1, min(int(per_page), MAX_PER_PAGE))
    conn = db.get_db()
    total = conn.execute(count_query, params).fetchone()[0]
    pages = max(1, math.ceil(total / per_page))
    page = max(1, min(page, pages))
    offset = (page - 1) * per_page
    rows = conn.execute(
        query_with_limit, (*params, per_page, offset)
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


def next_playlist_album_id():
    """Return a fresh negative album_id for a YouTube playlist import.

    Real Lidarr album ids are positive, so playlist imports live in a
    disjoint negative id space. Each import gets its own id (one below the
    most negative existing one, starting at -1) so its tracks don't collide
    with other playlists in the history and failed-track retry views, which
    key everything on album_id.

    The lowest id is taken across BOTH ``track_downloads`` and
    ``download_logs`` so an import that failed before recording any track
    (its summary log still lands in ``download_logs``) can't have its id
    handed out again to the next import. Callers allocate under
    ``processing.queue_lock`` while no other download is active, which
    serializes the read-then-decrement.
    """
    conn = db.get_db()
    row = conn.execute(
        "SELECT MIN(m) FROM ("
        " SELECT MIN(album_id) AS m FROM track_downloads WHERE album_id < 0"
        " UNION ALL"
        " SELECT MIN(album_id) AS m FROM download_logs WHERE album_id < 0"
        ")"
    ).fetchone()
    least = row[0] if row and row[0] is not None else 0
    return least - 1


def add_track_download(
    *, album_id, album_title, artist_name, track_title, track_number,
    success, error_message, youtube_url, youtube_title, match_score,
    duration_seconds, album_path, lidarr_album_path, cover_url,
    acoustid_fingerprint_id="", acoustid_score=0.0,
    acoustid_recording_id="", acoustid_recording_title="",
    track_artist="", source_format="",
    recording_id="", file_path="", file_size=0, reused_from=0,
):
    """Record a single track download attempt.

    ``recording_id`` is the MusicBrainz recording the track stands for,
    ``file_path`` / ``file_size`` the file written and ``reused_from`` the
    id of the row it was copied from (0 = downloaded). The video id is
    taken from ``youtube_url``.
    """
    conn = db.get_db()
    cursor = conn.execute(
        """INSERT INTO track_downloads
           (album_id, album_title, artist_name, track_title,
            track_number, success, error_message, youtube_url,
            youtube_title, match_score, duration_seconds,
            album_path, lidarr_album_path, cover_url,
            acoustid_fingerprint_id, acoustid_score,
            acoustid_recording_id, acoustid_recording_title,
            track_artist, source_format, timestamp,
            recording_id, video_id, file_path, file_size, reused_from)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                   ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            album_id, album_title, artist_name, track_title,
            track_number, int(success), error_message, youtube_url,
            youtube_title, match_score, duration_seconds,
            album_path, lidarr_album_path, cover_url,
            acoustid_fingerprint_id, acoustid_score,
            acoustid_recording_id, acoustid_recording_title,
            track_artist, source_format, time.time(),
            (recording_id or "").strip().lower(), video_id_from_url(youtube_url),
            file_path or "", int(file_size or 0), int(reused_from or 0),
        ),
    )
    conn.commit()
    return cursor.lastrowid


def get_verified_sources(recording_id, limit=8):
    """Stored successes of ``recording_id`` whose fingerprint matched it.

    Only rows whose AcoustID result is that very recording qualify (the
    audio was identified, not just accepted), newest first. Rows from a
    playlist import (negative ``album_id``) and from earlier reuse are fine:
    a copy of a verified file is as verified as the file.
    """
    if not recording_id:
        return []
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT id, album_id, album_title, artist_name, track_title,
               track_number, album_path, lidarr_album_path, file_path,
               duration_seconds, youtube_url, youtube_title, video_id,
               match_score, source_format, acoustid_fingerprint_id,
               acoustid_score, acoustid_recording_id,
               acoustid_recording_title, timestamp
        FROM track_downloads
        WHERE success = 1
          AND recording_id = ? AND recording_id != ''
          AND acoustid_recording_id = ?
        ORDER BY timestamp DESC
        LIMIT ?
        """,
        (recording_id, recording_id, limit),
    ).fetchall()
    return [dict(r) for r in rows]


def get_recent_library_copy_album_ids(since_timestamp):
    """Album ids this app copied into the library since a time.

    A copy to the library is what ends a normal run; Lidarr can lag behind
    it for hours (``RefreshArtist`` queued behind a rescan), so for that
    long the album looks missing to Lidarr although it is not.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT album_id FROM track_downloads"
        " WHERE success = 1 AND album_id > 0 AND timestamp >= ?"
        " AND COALESCE(lidarr_album_path, '') != ''",
        (since_timestamp,),
    ).fetchall()
    return {row[0] for row in rows}


def get_library_folders(album_id, limit=3):
    """Library folders this app copied ``album_id`` to, newest first."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT lidarr_album_path FROM track_downloads"
        " WHERE album_id = ? AND success = 1"
        " AND COALESCE(lidarr_album_path, '') != ''"
        " GROUP BY lidarr_album_path ORDER BY MAX(timestamp) DESC LIMIT ?",
        (album_id, limit),
    ).fetchall()
    return [row[0] for row in rows]


def set_lidarr_album_path(album_id, since_ts, path):
    """Point this run's track rows at the library copy of the album.

    Negative ids are YouTube imports, which keep their source URL in this
    column, so they are never touched.
    """
    conn = db.get_db()
    conn.execute(
        "UPDATE track_downloads SET lidarr_album_path = ?"
        " WHERE album_id = ? AND timestamp >= ? AND album_id > 0",
        (path, album_id, since_ts),
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


HISTORY_OUTCOMES = ("success", "partial", "error")

# Same rules as getAlbumStatus() in templates/downloads.html.
_OUTCOME_SQL = {
    "success": "fail_count = 0",
    "error": "fail_count > 0 AND success_count = 0",
    "partial": "fail_count > 0 AND success_count > 0",
}

_ALBUM_HISTORY_CTE = """
    WITH latest AS (
        SELECT
            l.album_id,
            a.album_title,
            a.artist_name,
            a.cover_url,
            l.latest_timestamp,
            l.success_count,
            l.fail_count,
            l.total_count
        FROM (
            SELECT
                album_id,
                MAX(timestamp) as latest_timestamp,
                SUM(CASE WHEN latest_success = 1 THEN 1 ELSE 0 END)
                    as success_count,
                SUM(CASE WHEN latest_success = 0 THEN 1 ELSE 0 END)
                    as fail_count,
                COUNT(*) as total_count
            FROM (
                SELECT t1.album_id, t1.timestamp,
                       t1.success as latest_success
                FROM track_downloads t1
                INNER JOIN (
                    SELECT album_id, track_title, MAX(timestamp) as max_ts
                    FROM track_downloads
                    GROUP BY album_id, track_title
                ) t2 ON t1.album_id = t2.album_id
                    AND t1.track_title = t2.track_title
                    AND t1.timestamp = t2.max_ts
            )
            GROUP BY album_id
        ) l
        JOIN track_downloads a ON a.id = (
            SELECT id FROM track_downloads
            WHERE album_id = l.album_id
            ORDER BY timestamp DESC, id DESC
            LIMIT 1
        )
    )
"""


def get_album_history(page=1, per_page=50, outcome=None, q=None):
    """Return album-grouped download summaries, newest first.

    ``outcome`` ('success'|'partial'|'error') keeps only albums with that
    status; ``q`` is a case-insensitive substring of the latest row's album
    title or artist name. ``counts`` holds the per-outcome totals matching
    ``q`` (ignoring ``outcome``) for the filter chips.
    """
    q_where, params = "1", ()
    if q:
        q_where = (
            "(album_title LIKE ? ESCAPE '\\' OR artist_name LIKE ? ESCAPE '\\')"
        )
        escaped = (
            q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        params = ("%" + escaped + "%",) * 2
    where = q_where
    if outcome in _OUTCOME_SQL:
        where += " AND " + _OUTCOME_SQL[outcome]
    query = (
        _ALBUM_HISTORY_CTE
        + f"SELECT * FROM latest WHERE {where}"
        " ORDER BY latest_timestamp DESC, album_id DESC LIMIT ? OFFSET ?"
    )
    count_query = (
        _ALBUM_HISTORY_CTE + f"SELECT COUNT(*) FROM latest WHERE {where}"
    )
    result = _paginate(query, count_query, params, page, per_page)
    sums = ", ".join(
        f"COALESCE(SUM({_OUTCOME_SQL[o]}), 0)" for o in HISTORY_OUTCOMES
    )
    row = db.get_db().execute(
        _ALBUM_HISTORY_CTE
        + f"SELECT COUNT(*), {sums} FROM latest WHERE {q_where}",
        params,
    ).fetchone()
    result["counts"] = dict(zip(("all",) + HISTORY_OUTCOMES, row))
    return result


def get_track_failure_counts(album_id):
    """Consecutive failed attempts per track, since that track last worked.

    Returns ``{track_title: {"failures": int, "last_failure": float}}``.
    Only failures newer than the track's most recent success are counted, so
    a track that succeeded starts from zero again if it later fails (e.g.
    its file was deleted). Tracks whose latest attempt succeeded are absent.

    Track identity is ``(album_id, track_title)``, matching
    ``get_failed_tracks_for_retry``.
    """
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT f.track_title AS track_title,
               COUNT(*) AS failures,
               MAX(f.timestamp) AS last_failure
        FROM track_downloads AS f
        WHERE f.album_id = ?
          AND f.success = 0
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND f.timestamp > COALESCE((
                SELECT MAX(s.timestamp) FROM track_downloads AS s
                WHERE s.album_id = f.album_id
                  AND s.track_title = f.track_title
                  AND s.success = 1
              ), 0)
        GROUP BY f.track_title
        """,
        (album_id, HOST_FAILURE_PREFIX + "%", DISK_FULL_PREFIX + "%",
         BLOCKED_PREFIX + "%"),
    ).fetchall()
    return {
        row["track_title"]: {
            "failures": row["failures"],
            "last_failure": row["last_failure"] or 0.0,
        }
        for row in rows
    }


def get_stuck_tracks(min_failures=3, limit=50):
    """Tracks with the most consecutive failures across all Lidarr albums.

    Same counting rules as ``get_track_failure_counts`` (host, disk-full and
    blocked-IP failures ignored, only failures after the track's last success),
    grouped by ``(album_id, track_title)``. Playlist imports
    (``album_id <= 0``) are left out: they are never retried automatically.
    """
    conn = db.get_db()
    rows = conn.execute(
        """
        SELECT f.album_id AS album_id,
               MAX(f.artist_name) AS artist_name,
               MAX(f.album_title) AS album_title,
               f.track_title AS track_title,
               COUNT(*) AS failures,
               MAX(f.timestamp) AS last_failure
        FROM track_downloads AS f
        WHERE f.album_id > 0
          AND f.success = 0
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND COALESCE(f.error_message, '') NOT LIKE ?
          AND f.timestamp > COALESCE((
                SELECT MAX(s.timestamp) FROM track_downloads AS s
                WHERE s.album_id = f.album_id
                  AND s.track_title = f.track_title
                  AND s.success = 1
              ), 0)
        GROUP BY f.album_id, f.track_title
        HAVING COUNT(*) >= ?
        ORDER BY failures DESC, last_failure DESC
        LIMIT ?
        """,
        (HOST_FAILURE_PREFIX + "%", DISK_FULL_PREFIX + "%",
         BLOCKED_PREFIX + "%", min_failures, limit),
    ).fetchall()
    return [
        {
            "album_id": row["album_id"],
            "artist_name": row["artist_name"] or "",
            "album_title": row["album_title"] or "",
            "track_title": row["track_title"],
            "failures": row["failures"],
            "last_failure": row["last_failure"] or 0.0,
        }
        for row in rows
    ]


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
        SELECT t1.track_title, t1.track_number, t1.error_message,
               t1.track_artist
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
                "artist": row["track_artist"] or ctx["artist_name"],
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


def _quality_bucket(source_format):
    """Reduce a stored source_format to a coarse container/codec label for
    the quality distribution chart.

    ``source_format`` is built by ``downloader._format_source_quality`` as
    ``"<format_id> · <container> · <bitrate> kbps"`` with any part omitted
    when unknown, so the container is not at a fixed position. Pick the
    first token that is neither the numeric format id nor the bitrate.
    """
    if not source_format:
        return "Unknown"
    for part in source_format.split("·"):
        token = part.strip()
        if not token or token.isdigit() or token.lower().endswith("kbps"):
            continue
        return token
    return "Unknown"


def get_insights(days=30):
    """Aggregate download analytics for the insights dashboard.

    Every metric is scoped to the same window — the last ``days`` calendar
    days up to and including today (local time) — so the whole page reflects
    the selector. Returns windowed totals, a zero-filled per-day
    success/failure series, the top artists by successful tracks, and an
    audio-quality breakdown derived from the recorded ``source_format``.
    """
    conn = db.get_db()
    today = datetime.now().date()
    start_date = today - timedelta(days=days - 1)
    # Local midnight of the earliest displayed day, so the query window and
    # the zero-filled day range line up exactly (no boundary day dropped).
    since = datetime(
        start_date.year, start_date.month, start_date.day
    ).timestamp()

    row = conn.execute(
        "SELECT COUNT(*),"
        " SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
        " COUNT(DISTINCT album_id),"
        " COUNT(DISTINCT NULLIF(artist_name, '')),"
        " SUM(CASE WHEN success = 1 THEN duration_seconds ELSE 0 END)"
        " FROM track_downloads WHERE timestamp >= ?",
        (since,),
    ).fetchone()
    total = (row[0] if row else 0) or 0
    successful = (row[1] if row else 0) or 0
    totals = {
        "total_tracks": total,
        "successful": successful,
        "failed": total - successful,
        "success_rate": round(successful / total * 100, 1) if total else 0.0,
        "distinct_albums": (row[2] or 0) if row else 0,
        "distinct_artists": (row[3] or 0) if row else 0,
        "total_duration_seconds": (row[4] or 0) if row else 0,
    }

    rows = conn.execute(
        "SELECT strftime('%Y-%m-%d', timestamp, 'unixepoch', 'localtime') AS day,"
        " SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END),"
        " SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END)"
        " FROM track_downloads WHERE timestamp >= ?"
        " GROUP BY day",
        (since,),
    ).fetchall()
    by_day = {r[0]: ((r[1] or 0), (r[2] or 0)) for r in rows}
    daily = []
    for i in range(days - 1, -1, -1):
        d = (today - timedelta(days=i)).isoformat()
        succ, fail = by_day.get(d, (0, 0))
        daily.append({"date": d, "success": succ, "failed": fail})

    rows = conn.execute(
        "SELECT artist_name, COUNT(*) AS c FROM track_downloads"
        " WHERE success = 1 AND artist_name != '' AND timestamp >= ?"
        " GROUP BY artist_name ORDER BY c DESC, artist_name LIMIT 10",
        (since,),
    ).fetchall()
    top_artists = [{"artist": r[0], "count": r[1]} for r in rows]

    quality = {}
    rows = conn.execute(
        "SELECT source_format, COUNT(*) FROM track_downloads"
        " WHERE success = 1 AND timestamp >= ? GROUP BY source_format",
        (since,),
    ).fetchall()
    for fmt, count in rows:
        label = _quality_bucket(fmt)
        quality[label] = quality.get(label, 0) + count
    quality_list = sorted(
        ({"label": k, "count": v} for k, v in quality.items()),
        key=lambda x: x["count"],
        reverse=True,
    )

    return {
        "totals": totals,
        "daily": daily,
        "top_artists": top_artists,
        "quality": quality_list,
        "window_days": days,
    }


def get_attempted_album_ids_since(since_timestamp):
    """Return album IDs attempted since timestamp: any download log entry
    or any per-track download record.

    Used by the scheduler to avoid hammering a failing album on every
    cycle: an album that has already been attempted (success or failure)
    within the retry window is skipped until other albums get a turn.
    The track records keep that window when the user clears or dismisses
    log rows.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT album_id FROM download_logs WHERE timestamp >= ?"
        " UNION SELECT album_id FROM track_downloads WHERE timestamp >= ?",
        (since_timestamp, since_timestamp),
    ).fetchall()
    return {row[0] for row in rows}


def clear_history():
    """Delete all track download records."""
    conn = db.get_db()
    conn.execute("DELETE FROM candidate_attempts")
    conn.execute("DELETE FROM track_downloads")
    conn.commit()


# --- Banned URLs ---


def add_banned_url(
    youtube_url, youtube_title, album_id, album_title,
    artist_name, track_title, track_number,
):
    """Ban a YouTube URL for a specific track. Ignores duplicates."""
    conn = db.get_db()
    conn.execute(
        """INSERT OR IGNORE INTO banned_urls
           (youtube_url, youtube_title, album_id, album_title,
            artist_name, track_title, track_number, banned_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            youtube_url, youtube_title, album_id, album_title,
            artist_name, track_title, track_number, time.time(),
        ),
    )
    conn.commit()


def get_banned_urls(page=1, per_page=50):
    """Return paginated banned URLs, newest first."""
    query = "SELECT * FROM banned_urls ORDER BY banned_at DESC LIMIT ? OFFSET ?"
    count_query = "SELECT COUNT(*) FROM banned_urls"
    return _paginate(query, count_query, (), page, per_page)


def get_banned_urls_for_track(album_id, track_title):
    """Return set of banned YouTube URLs for a specific track."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT youtube_url FROM banned_urls"
        " WHERE album_id = ? AND track_title = ?",
        (album_id, track_title),
    ).fetchall()
    return {row[0] for row in rows}


def remove_banned_url(ban_id):
    """Delete a ban by ID. Returns True if deleted."""
    conn = db.get_db()
    cursor = conn.execute(
        "DELETE FROM banned_urls WHERE id = ?", (ban_id,)
    )
    conn.commit()
    return cursor.rowcount > 0


def clear_banned_urls():
    """Delete all banned URLs. Returns the number removed."""
    conn = db.get_db()
    cursor = conn.execute("DELETE FROM banned_urls")
    conn.commit()
    return cursor.rowcount


def mark_track_deleted(track_id):
    """Set deleted=1 on a track download. Returns the row dict or None."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM track_downloads WHERE id = ?",
        (track_id,),
    ).fetchone()
    if row is None:
        return None
    conn.execute(
        "UPDATE track_downloads SET deleted = 1 WHERE id = ?",
        (track_id,),
    )
    conn.commit()
    result = dict(row)
    result["deleted"] = 1
    return result


# --- Logs ---


def add_log(
    log_type, album_id, album_title, artist_name,
    details="", total_file_size=0, track_number=None,
    track_title="", track_download_id=None,
):
    """Create a download log entry. Returns the generated log ID."""
    conn = db.get_db()
    ts = int(time.time() * 1000)
    suffix = uuid.uuid4().hex[:8]
    if track_number is not None:
        log_id = f"{ts}_{album_id}_{track_number}_{suffix}"
    else:
        log_id = f"{ts}_{album_id}_{suffix}"
    conn.execute(
        """INSERT INTO download_logs
           (id, type, album_id, album_title, artist_name, timestamp,
            details, total_file_size,
            track_title, track_number, track_download_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            log_id, log_type, album_id, album_title, artist_name,
            time.time(), details, total_file_size,
            track_title, track_number, track_download_id,
        ),
    )
    conn.commit()
    return log_id


def get_logs(page=1, per_page=50, log_type=None):
    """Return paginated download logs, newest first."""
    if log_type:
        query = (
            "SELECT * FROM download_logs"
            " WHERE type = ? ORDER BY timestamp DESC, id DESC"
            " LIMIT ? OFFSET ?"
        )
        count_query = (
            "SELECT COUNT(*) FROM download_logs WHERE type = ?"
        )
        params = (log_type,)
    else:
        query = (
            "SELECT * FROM download_logs ORDER BY timestamp DESC, id DESC"
            " LIMIT ? OFFSET ?"
        )
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


def prune_old_records(cutoff_ts):
    """Delete history older than ``cutoff_ts`` that nothing reads any more.

    Logs and candidate attempts go by age alone. A track_downloads row goes
    only when it is a failure a later success of the same track has
    superseded: other rows still drive the backoff (failures since the last
    success), and successes tell playlist watch and artist import what is
    already imported.
    """
    conn = db.get_db()
    stale = (
        "SELECT f.id FROM track_downloads AS f"
        " WHERE f.success = 0 AND f.timestamp < ?"
        " AND EXISTS (SELECT 1 FROM track_downloads AS s"
        "   WHERE s.album_id = f.album_id AND s.track_title = f.track_title"
        "   AND s.success = 1 AND s.timestamp > f.timestamp)"
    )
    conn.execute(
        "DELETE FROM candidate_attempts WHERE timestamp < ?"
        f" OR track_download_id IN ({stale})",
        (cutoff_ts, cutoff_ts),
    )
    conn.execute(f"DELETE FROM track_downloads WHERE id IN ({stale})", (cutoff_ts,))
    conn.execute("DELETE FROM download_logs WHERE timestamp < ?", (cutoff_ts,))
    conn.commit()


def get_logs_db_size():
    """Estimate the storage used by log text fields."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT SUM(LENGTH(details)) FROM download_logs"
    ).fetchone()
    return row[0] or 0


# --- Candidate Attempts ---


def flush_candidate_attempts(track_download_id, attempts):
    """Bulk insert candidate attempt records for a track download."""
    if not attempts:
        return
    conn = db.get_db()
    conn.executemany(
        """INSERT INTO candidate_attempts
           (track_download_id, youtube_url, youtube_title,
            match_score, duration_seconds, outcome,
            acoustid_matched_id, acoustid_matched_title,
            acoustid_score, expected_recording_id,
            error_message, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        [
            (
                track_download_id,
                a["youtube_url"], a["youtube_title"],
                a["match_score"], a["duration_seconds"],
                a["outcome"].value
                if isinstance(a["outcome"], CandidateOutcome)
                else a["outcome"],
                a["acoustid_matched_id"], a["acoustid_matched_title"],
                a["acoustid_score"], a["expected_recording_id"],
                a["error_message"], a["timestamp"],
            )
            for a in attempts
        ],
    )
    conn.commit()


def get_candidate_attempts(track_download_id):
    """Return candidate attempts for a track download, oldest first."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM candidate_attempts"
        " WHERE track_download_id = ?"
        " ORDER BY timestamp ASC",
        (track_download_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_candidate_attempts_for(track_download_ids):
    """Return {track_download_id: [attempts, oldest first]} in one query."""
    ids = list(track_download_ids)
    if not ids:
        return {}
    conn = db.get_db()
    placeholders = ",".join("?" * len(ids))
    rows = conn.execute(
        "SELECT * FROM candidate_attempts"
        f" WHERE track_download_id IN ({placeholders})"
        " ORDER BY track_download_id, timestamp ASC",
        ids,
    ).fetchall()
    grouped = {}
    for row in rows:
        grouped.setdefault(row["track_download_id"], []).append(dict(row))
    return grouped


def get_banned_urls_for_album(album_id):
    """Return list of {id, youtube_url} dicts for all banned URLs in an album."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT id, youtube_url FROM banned_urls WHERE album_id = ?",
        (album_id,),
    ).fetchall()
    return [{"id": row[0], "youtube_url": row[1]} for row in rows]


def get_track_download_youtube_urls():
    """Every YouTube URL ever attempted (success or failure), as a set."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT youtube_url FROM track_downloads"
        " WHERE youtube_url != ''"
    ).fetchall()
    return {row[0] for row in rows}


# --- Queue ---


def enqueue_album(album_id, force=False, front=False):
    """Add an album to the download queue. Returns False if duplicate.

    ``force`` marks the entry as explicitly requested by the user, which
    makes the download bypass the per-track retry backoff. Re-adding an
    album that is already queued still returns False, but a forced request
    upgrades the existing entry so the manual intent isn't lost.

    ``front`` puts it at the head instead of the tail: an album that was
    dispatched but found the download slot taken goes back where it was,
    not behind the whole queue.
    """
    conn = db.get_db()
    if front:
        position = conn.execute(
            "SELECT COALESCE(MIN(position), 1) - 1 FROM download_queue"
        ).fetchone()[0]
    else:
        position = conn.execute(
            "SELECT COALESCE(MAX(position), 0) + 1 FROM download_queue"
        ).fetchone()[0]
    cursor = conn.execute(
        "INSERT OR IGNORE INTO download_queue"
        " (album_id, position, status, force)"
        " VALUES (?, ?, ?, ?)",
        (album_id, position, QUEUE_STATUS_QUEUED, 1 if force else 0),
    )
    added = cursor.rowcount > 0
    if not added and force:
        conn.execute(
            "UPDATE download_queue SET force = 1 WHERE album_id = ?",
            (album_id,),
        )
    conn.commit()
    return added


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


def get_queue_details():
    """Queued albums in order, with display fields from missing_albums_cache.

    One query for the whole queue: a queue of thousands must not cost one
    Lidarr call per album (issue #95). Fields are empty for albums the
    cache doesn't know.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT q.album_id,"
        " COALESCE(m.title, '') AS title,"
        " COALESCE(m.artist_name, '') AS artist_name,"
        " COALESCE(m.cover_url, '') AS cover_url,"
        " COALESCE(m.track_count, 0) AS track_count"
        " FROM download_queue q"
        " LEFT JOIN missing_albums_cache m ON m.album_id = q.album_id"
        " WHERE q.status = ?"
        " ORDER BY q.position",
        (QUEUE_STATUS_QUEUED,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_queue_length():
    """Return the number of albums waiting in the queue.

    The album downloading in the foreground keeps its row (as 'downloading')
    until it ends; it is counted as active, not queued.
    """
    conn = db.get_db()
    return conn.execute(
        "SELECT COUNT(*) FROM download_queue WHERE status = ?",
        (QUEUE_STATUS_QUEUED,),
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


def clear_queue(include_active=True):
    """Delete entries from the download queue.

    With ``include_active=False`` only waiting albums go: the album being
    downloaded keeps its row, so a restart still re-queues it.
    """
    conn = db.get_db()
    if include_active:
        conn.execute("DELETE FROM download_queue")
        conn.commit()
        return
    conn.execute(
        "DELETE FROM download_queue WHERE status = ?", (QUEUE_STATUS_QUEUED,)
    )
    conn.commit()
    _reorder_queue(conn)


def reorder_queue(new_order):
    """Set queue positions according to the given list of album_ids."""
    conn = db.get_db()
    for i, album_id in enumerate(new_order, 1):
        conn.execute(
            "UPDATE download_queue SET position = ? WHERE album_id = ?",
            (i, album_id),
        )
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


_SYNC_STATE_FIELDS = {
    "status",
    "last_full_sync_at",
    "last_attempt_at",
    "last_error",
    "current_page",
    "total_pages",
    "total_records",
    "synced_records",
    "current_run_id",
}


def get_sync_state():
    """Return the current Lidarr sync state row as a dict."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT * FROM sync_state WHERE id = 1"
    ).fetchone()
    if row is None:
        conn.execute(
            "INSERT INTO sync_state (id, status) VALUES (1, 'idle')"
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM sync_state WHERE id = 1"
        ).fetchone()
    return dict(row) if row else {}


def update_sync_state(**fields):
    """Update one or more columns on the sync_state singleton row."""
    cols = [k for k in fields if k in _SYNC_STATE_FIELDS]
    if not cols:
        return
    set_clause = ", ".join(f"{c} = ?" for c in cols)
    values = [fields[c] for c in cols]
    conn = db.get_db()
    conn.execute(
        f"UPDATE sync_state SET {set_clause} WHERE id = 1",
        values,
    )
    conn.commit()


def bump_sync_run_id():
    """Increment current_run_id and return the new value."""
    conn = db.get_db()
    get_sync_state()
    conn.execute(
        "UPDATE sync_state"
        " SET current_run_id = COALESCE(current_run_id, 0) + 1"
        " WHERE id = 1"
    )
    conn.commit()
    row = conn.execute(
        "SELECT current_run_id FROM sync_state WHERE id = 1"
    ).fetchone()
    return row["current_run_id"] if row else 1


def _album_upsert_params(album, run_id):
    """Build the parameter tuple for a missing_albums_cache upsert."""
    album_id = album.get("id")
    if not album_id:
        return None
    stats = album.get("statistics") or {}
    total = stats.get("trackCount", 0) or 0
    files = stats.get("trackFileCount", 0) or 0
    missing = max(0, total - files)
    artist = album.get("artist") or {}
    cover_url = ""
    for img in album.get("images") or []:
        if img.get("coverType") == "cover":
            cover_url = img.get("remoteUrl") or img.get("url") or ""
            break
    return (
        int(album_id),
        album.get("foreignAlbumId", "") or "",
        album.get("title", "") or "",
        int(artist.get("id", 0) or 0),
        artist.get("artistName", "") or "",
        album.get("releaseDate", "") or "",
        int(total),
        int(missing),
        cover_url,
        json.dumps(album),
        time.time(),
        int(run_id),
    )


_ALBUM_UPSERT_SQL = """
    INSERT INTO missing_albums_cache (
        album_id, foreign_album_id, title, artist_id, artist_name,
        release_date, track_count, missing_track_count, cover_url,
        raw_json, synced_at, sync_run_id
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ON CONFLICT(album_id) DO UPDATE SET
        foreign_album_id = excluded.foreign_album_id,
        title = excluded.title,
        artist_id = excluded.artist_id,
        artist_name = excluded.artist_name,
        release_date = excluded.release_date,
        track_count = excluded.track_count,
        missing_track_count = excluded.missing_track_count,
        cover_url = excluded.cover_url,
        raw_json = excluded.raw_json,
        synced_at = excluded.synced_at,
        sync_run_id = excluded.sync_run_id
"""


def upsert_missing_album(album, run_id):
    """Insert or update a single album in the missing_albums_cache."""
    params = _album_upsert_params(album, run_id)
    if params is None:
        return
    conn = db.get_db()
    conn.execute(_ALBUM_UPSERT_SQL, params)
    conn.commit()


def upsert_missing_albums_batch(albums, run_id):
    """Upsert many albums in a single transaction to reduce lock contention."""
    rows = [
        params for params in (
            _album_upsert_params(a, run_id) for a in albums
        ) if params is not None
    ]
    if not rows:
        return
    conn = db.get_db()
    with conn:
        conn.executemany(_ALBUM_UPSERT_SQL, rows)


def prune_missing_albums(run_id):
    """Delete cached albums not seen in the given sync run. Returns count."""
    conn = db.get_db()
    cur = conn.execute(
        "DELETE FROM missing_albums_cache WHERE sync_run_id != ?",
        (int(run_id),),
    )
    conn.commit()
    return cur.rowcount


def get_cached_missing_albums():
    """Return all cached missing albums, sorted by release date desc."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT raw_json, missing_track_count FROM missing_albums_cache"
        " ORDER BY release_date DESC, album_id DESC"
    ).fetchall()
    out = []
    for row in rows:
        try:
            album = json.loads(row["raw_json"])
        except (ValueError, TypeError):
            continue
        album["missingTrackCount"] = row["missing_track_count"]
        out.append(album)
    return out


def get_cached_missing_albums_summary():
    """Return the cached missing albums with only the fields the Library
    and the scheduler read, built from the indexed columns (no raw_json).

    Same order and nested shape as get_cached_missing_albums().
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT album_id, foreign_album_id, title, artist_id, artist_name,"
        " release_date, track_count, missing_track_count, cover_url"
        " FROM missing_albums_cache"
        " ORDER BY release_date DESC, album_id DESC"
    ).fetchall()
    return [
        {
            "id": row["album_id"],
            "foreignAlbumId": row["foreign_album_id"],
            "title": row["title"],
            "releaseDate": row["release_date"],
            "missingTrackCount": row["missing_track_count"],
            "statistics": {"trackCount": row["track_count"]},
            "artist": {
                "id": row["artist_id"],
                "artistName": row["artist_name"],
            },
            "images": (
                [{"coverType": "cover", "remoteUrl": row["cover_url"]}]
                if row["cover_url"] else []
            ),
        }
        for row in rows
    ]


def count_cached_missing_albums():
    """Return the number of albums currently in the cache."""
    conn = db.get_db()
    row = conn.execute(
        "SELECT COUNT(*) FROM missing_albums_cache"
    ).fetchone()
    return row[0] if row else 0


# --- Download-client jobs (SABnzbd bridge) ---

_CLIENT_JOB_COLS = (
    "nzo_id", "album_id", "name", "category", "status",
    "storage", "size", "error", "added_ts", "completed_ts",
)


def upsert_client_job(job):
    """Insert or replace a download-client job row from a job dict."""
    conn = db.get_db()
    conn.execute(
        "INSERT OR REPLACE INTO download_client_jobs"
        " (nzo_id, album_id, name, category, status, storage, size,"
        "  error, added_ts, completed_ts)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            job["nzo_id"], int(job["album_id"]), job.get("name", ""),
            job.get("category", ""), job.get("status", "queued"),
            job.get("storage", ""), int(job.get("size") or 0),
            job.get("error", ""), float(job.get("added_ts") or 0),
            job.get("completed_ts"),
        ),
    )
    conn.commit()


def get_all_client_jobs():
    """Return all persisted download-client jobs as dicts."""
    conn = db.get_db()
    rows = conn.execute(
        "SELECT * FROM download_client_jobs ORDER BY added_ts"
    ).fetchall()
    return [dict(row) for row in rows]


def delete_client_job(nzo_id):
    """Delete a download-client job by nzo_id."""
    conn = db.get_db()
    conn.execute(
        "DELETE FROM download_client_jobs WHERE nzo_id = ?", (nzo_id,)
    )
    conn.commit()


def get_failed_client_album_ids_since(since_timestamp):
    """Album ids whose download-client job failed since ``since_timestamp``.

    Used to refuse a Lidarr re-grab only when this client previously
    failed the same album within the retry cooldown (breaking the
    grab -> fail -> re-grab loop) without blocking manual/scheduler runs.
    """
    conn = db.get_db()
    # Only block when the album's *most recent* client job is the failure,
    # so a later successful re-grab clears the block even if the old failed
    # row is still inside the cooldown window.
    rows = conn.execute(
        "SELECT album_id FROM download_client_jobs j1"
        " WHERE status = 'failed' AND COALESCE(completed_ts, 0) >= ?"
        " AND NOT EXISTS ("
        "   SELECT 1 FROM download_client_jobs j2"
        "   WHERE j2.album_id = j1.album_id"
        "     AND COALESCE(j2.completed_ts, 0) > COALESCE(j1.completed_ts, 0)"
        " )",
        (since_timestamp,),
    ).fetchall()
    return {row[0] for row in rows}


def get_recent_client_album_ids_since(since_timestamp):
    """Album ids the download client finished (any status) since a time.

    Used to hide albums the client has just handled from the indexer feed
    without hiding albums merely touched by manual/scheduler runs (which
    would needlessly empty the feed and fail Lidarr's indexer test).
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT DISTINCT album_id FROM download_client_jobs"
        " WHERE COALESCE(completed_ts, 0) >= ?",
        (since_timestamp,),
    ).fetchall()
    return {row[0] for row in rows}


def get_cached_album_index():
    """Return a lightweight index of cached albums for search matching.

    Used by the Lidarr download-client bridge to resolve an incoming
    Newznab search (artist/album strings) back to a Lidarr album_id
    without deserializing every cached ``raw_json`` blob.
    """
    conn = db.get_db()
    rows = conn.execute(
        "SELECT album_id, title, artist_name, release_date,"
        " track_count, cover_url"
        " FROM missing_albums_cache"
    ).fetchall()
    return [dict(row) for row in rows]


def get_cached_album(album_id):
    """Return a single cached album dict by id (primary-key lookup), or None.

    Indexed counterpart to ``get_cached_album_index`` for the download
    client's ``t=get`` / grab paths, which only need one album.
    """
    conn = db.get_db()
    row = conn.execute(
        "SELECT album_id, title, artist_name, release_date,"
        " track_count, cover_url"
        " FROM missing_albums_cache WHERE album_id = ?",
        (int(album_id),),
    ).fetchone()
    return dict(row) if row else None


def set_album_source_hint(album_id, playlist_id, browse_id="", source="explore"):
    if not isinstance(album_id, int) or album_id <= 0 or not playlist_id:
        return False
    conn = db.get_db()
    conn.execute(
        "INSERT INTO album_source_hints"
        " (album_id, playlist_id, browse_id, source, created_at)"
        " VALUES (?, ?, ?, ?, ?)"
        " ON CONFLICT(album_id) DO UPDATE SET"
        " playlist_id = excluded.playlist_id,"
        " browse_id = excluded.browse_id,"
        " source = excluded.source,"
        " created_at = excluded.created_at",
        (album_id, playlist_id, browse_id or "", source, time.time()),
    )
    conn.commit()
    return True


def get_album_source_hint(album_id):
    if not isinstance(album_id, int) or album_id <= 0:
        return None
    row = db.get_db().execute(
        "SELECT album_id, playlist_id, browse_id, source, created_at"
        " FROM album_source_hints WHERE album_id = ?",
        (album_id,),
    ).fetchone()
    return dict(row) if row else None


def delete_album_source_hint(album_id):
    conn = db.get_db()
    conn.execute(
        "DELETE FROM album_source_hints WHERE album_id = ?", (album_id,),
    )
    conn.commit()


def get_imported_youtube_album_titles(artist_name):
    rows = db.get_db().execute(
        "SELECT album_title FROM track_downloads"
        " WHERE album_id < 0 AND success = 1 AND artist_name = ? COLLATE NOCASE"
        " GROUP BY album_title",
        (artist_name or "",),
    ).fetchall()
    return [r[0] for r in rows]
