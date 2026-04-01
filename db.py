"""SQLite database connection, schema, and migration framework."""

import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DB_PATH = "/config/lidarr-downloader.db"
SCHEMA_VERSION = 5

_local = threading.local()


def get_db():
    """Return a thread-local SQLite connection, creating one if needed."""
    if not hasattr(_local, "connection") or _local.connection is None:
        _local.connection = sqlite3.connect(DB_PATH)
        _local.connection.row_factory = sqlite3.Row
        _local.connection.execute("PRAGMA journal_mode=WAL")
        _local.connection.execute("PRAGMA foreign_keys=ON")
    return _local.connection


def close_db():
    """Close the thread-local connection if open."""
    conn = getattr(_local, "connection", None)
    if conn is not None:
        conn.close()
        _local.connection = None


_SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER NOT NULL,
    applied_at REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    success INTEGER NOT NULL DEFAULT 1,
    partial INTEGER NOT NULL DEFAULT 0,
    manual INTEGER NOT NULL DEFAULT 0,
    track_title TEXT,
    timestamp REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS download_logs (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    album_id INTEGER NOT NULL,
    album_title TEXT NOT NULL,
    artist_name TEXT NOT NULL,
    timestamp REAL NOT NULL,
    details TEXT DEFAULT '',
    failed_tracks TEXT DEFAULT '[]',
    total_file_size INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS failed_tracks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER,
    album_title TEXT DEFAULT '',
    artist_name TEXT DEFAULT '',
    cover_url TEXT DEFAULT '',
    album_path TEXT DEFAULT '',
    lidarr_album_path TEXT DEFAULT '',
    track_title TEXT NOT NULL,
    track_num INTEGER DEFAULT 0,
    reason TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS download_queue (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    album_id INTEGER NOT NULL UNIQUE,
    position INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued'
        CHECK (status IN ('queued', 'downloading'))
);

CREATE INDEX IF NOT EXISTS idx_history_timestamp
    ON download_history(timestamp);
CREATE INDEX IF NOT EXISTS idx_history_album_id
    ON download_history(album_id);
CREATE INDEX IF NOT EXISTS idx_logs_timestamp
    ON download_logs(timestamp);
CREATE INDEX IF NOT EXISTS idx_queue_position
    ON download_queue(position);
"""


_LEGACY_TABLES = (
    "download_attempts", "download_logs", "failed_tracks",
    "download_queue", "download_history", "excluded_tracks", "banned_urls",
)


def _drop_legacy_tables(conn):
    """Drop tables from pre-versioned database so V1 schema can be applied.

    The old db.py (feature/track-history) created tables with incompatible
    columns (e.g. download_logs.log_type vs .type). Since there is no
    schema_version table, we know JSON files are the source of truth and
    these tables can be safely replaced.
    """
    existing = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    dropped = [t for t in _LEGACY_TABLES if t in existing]
    if dropped:
        logger.warning(
            "Dropping legacy tables from pre-versioned database: %s",
            ", ".join(dropped),
        )
        for table in dropped:
            if table not in _LEGACY_TABLES:
                continue
            # Table name from hardcoded allowlist; safe to interpolate.
            conn.execute("DROP TABLE IF EXISTS " + table)  # nosemgrep
        conn.commit()


def init_db():
    """Initialize the database schema, running migrations if needed."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = get_db()
    has_version_table = conn.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if has_version_table is None:
        _drop_legacy_tables(conn)
        conn.executescript(_SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at)"
            " VALUES (?, ?)",
            (1, time.time()),
        )
        conn.commit()
        logger.info("Database initialized at schema version 1")
        _run_migrations(conn, 1)
    else:
        current = conn.execute(
            "SELECT version FROM schema_version"
            " ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current_version = current[0] if current else 0
        _run_migrations(conn, current_version)


def _migrate_v1_to_v2(conn):
    """Replace download_history + failed_tracks with track_downloads.

    Drops all old data (no track-level info to preserve) and recreates
    download_logs without the failed_tracks column.
    """
    conn.execute("DROP TABLE IF EXISTS download_history")
    conn.execute("DROP TABLE IF EXISTS failed_tracks")
    conn.execute("DROP TABLE IF EXISTS download_logs")

    conn.execute("""
        CREATE TABLE track_downloads (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER NOT NULL,
            album_title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            track_title TEXT NOT NULL,
            track_number INTEGER NOT NULL DEFAULT 0,
            success INTEGER NOT NULL DEFAULT 0,
            error_message TEXT DEFAULT '',
            youtube_url TEXT DEFAULT '',
            youtube_title TEXT DEFAULT '',
            match_score REAL DEFAULT 0.0,
            duration_seconds INTEGER DEFAULT 0,
            album_path TEXT DEFAULT '',
            lidarr_album_path TEXT DEFAULT '',
            cover_url TEXT DEFAULT '',
            timestamp REAL NOT NULL
        )
    """)

    conn.execute(
        "CREATE INDEX idx_track_dl_album_id"
        " ON track_downloads(album_id)"
    )
    conn.execute(
        "CREATE INDEX idx_track_dl_album_id_success"
        " ON track_downloads(album_id, success)"
    )
    conn.execute(
        "CREATE INDEX idx_track_dl_timestamp"
        " ON track_downloads(timestamp)"
    )
    conn.execute(
        "CREATE INDEX idx_track_dl_youtube_url"
        " ON track_downloads(youtube_url)"
    )

    conn.execute("""
        CREATE TABLE download_logs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            album_id INTEGER NOT NULL,
            album_title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            details TEXT DEFAULT '',
            total_file_size INTEGER DEFAULT 0
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_logs_timestamp"
        " ON download_logs(timestamp)"
    )


def _migrate_v2_to_v3(conn):
    """Add AcoustID fingerprint columns to track_downloads."""
    conn.execute(
        "ALTER TABLE track_downloads"
        " ADD COLUMN acoustid_fingerprint_id TEXT DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE track_downloads"
        " ADD COLUMN acoustid_score REAL DEFAULT 0.0"
    )
    conn.execute(
        "ALTER TABLE track_downloads"
        " ADD COLUMN acoustid_recording_id TEXT DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE track_downloads"
        " ADD COLUMN acoustid_recording_title TEXT DEFAULT ''"
    )


def _migrate_v3_to_v4(conn):
    """Add banned_urls table and deleted column to track_downloads."""
    conn.execute("""
        CREATE TABLE banned_urls (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            youtube_url TEXT NOT NULL,
            youtube_title TEXT,
            album_id INTEGER NOT NULL,
            album_title TEXT,
            artist_name TEXT,
            track_title TEXT NOT NULL,
            track_number INTEGER,
            banned_at REAL NOT NULL,
            UNIQUE(youtube_url, album_id, track_title)
        )
    """)
    conn.execute(
        "CREATE INDEX idx_banned_urls_lookup"
        " ON banned_urls(album_id, track_title)"
    )
    conn.execute(
        "CREATE INDEX idx_banned_urls_timestamp"
        " ON banned_urls(banned_at)"
    )
    conn.execute(
        "ALTER TABLE track_downloads"
        " ADD COLUMN deleted INTEGER DEFAULT 0"
    )


def _migrate_v4_to_v5(conn):
    """Add candidate_attempts table and track context to download_logs."""
    conn.execute("""
        CREATE TABLE candidate_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            track_download_id INTEGER NOT NULL
                REFERENCES track_downloads(id),
            youtube_url TEXT DEFAULT '',
            youtube_title TEXT DEFAULT '',
            match_score REAL DEFAULT 0.0,
            duration_seconds INTEGER DEFAULT 0,
            outcome TEXT NOT NULL,
            acoustid_matched_id TEXT DEFAULT '',
            acoustid_matched_title TEXT DEFAULT '',
            acoustid_score REAL DEFAULT 0.0,
            expected_recording_id TEXT DEFAULT '',
            error_message TEXT DEFAULT '',
            timestamp REAL NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX idx_ca_track_dl_id"
        " ON candidate_attempts(track_download_id)"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_title TEXT DEFAULT ''"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_number INTEGER DEFAULT NULL"
    )
    conn.execute(
        "ALTER TABLE download_logs"
        " ADD COLUMN track_download_id INTEGER DEFAULT NULL"
    )


def _run_migrations(conn, current_version):
    """Run any pending schema migrations sequentially."""
    migrations = {
        2: _migrate_v1_to_v2,
        3: _migrate_v2_to_v3,
        4: _migrate_v3_to_v4,
        5: _migrate_v4_to_v5,
    }
    for version in sorted(migrations):
        if current_version < version:
            logger.info(
                "Running migration to schema version %d...", version
            )
            try:
                conn.execute("BEGIN")
                migrations[version](conn)
                conn.execute(
                    "INSERT INTO schema_version (version, applied_at)"
                    " VALUES (?, ?)",
                    (version, time.time()),
                )
                conn.commit()
                logger.info("Migration to version %d complete", version)
            except Exception:
                conn.rollback()
                logger.error(
                    "Migration to version %d failed, rolled back",
                    version, exc_info=True,
                )
                raise
