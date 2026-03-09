"""SQLite database connection, schema, and migration framework."""

import logging
import os
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

DB_PATH = "/config/lidarr-downloader.db"
SCHEMA_VERSION = 1

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


def init_db():
    """Initialize the database schema, running migrations if needed."""
    db_dir = os.path.dirname(DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = get_db()
    row = conn.execute(
        "SELECT name FROM sqlite_master"
        " WHERE type='table' AND name='schema_version'"
    ).fetchone()
    if row is None:
        conn.executescript(_SCHEMA_V1)
        conn.execute(
            "INSERT INTO schema_version (version, applied_at)"
            " VALUES (?, ?)",
            (SCHEMA_VERSION, time.time()),
        )
        conn.commit()
        logger.info("Database initialized at schema version %d", SCHEMA_VERSION)
    else:
        current = conn.execute(
            "SELECT version FROM schema_version"
            " ORDER BY version DESC LIMIT 1"
        ).fetchone()
        current_version = current[0] if current else 0
        _run_migrations(conn, current_version)


def _run_migrations(conn, current_version):
    """Run any pending schema migrations sequentially."""
    migrations = {
        # 2: _migrate_v1_to_v2,
    }
    for version in sorted(migrations):
        if current_version < version:
            logger.info("Running migration to schema version %d...", version)
            migrations[version](conn)
            conn.execute(
                "INSERT INTO schema_version (version, applied_at)"
                " VALUES (?, ?)",
                (version, time.time()),
            )
            conn.commit()
            logger.info("Migration to version %d complete", version)
