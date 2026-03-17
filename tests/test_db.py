import sqlite3

import pytest

from db import close_db, get_db, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    yield db_path
    close_db()


def test_init_db_creates_tables(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
    )
    tables = [row[0] for row in cursor.fetchall()]
    conn.close()
    assert "schema_version" in tables
    assert "track_downloads" in tables
    assert "download_logs" in tables
    assert "download_queue" in tables


def test_init_db_sets_schema_version(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT version FROM schema_version"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] == 3


def test_init_db_idempotent(temp_db):
    init_db()
    init_db()  # should not raise
    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    conn.close()
    # V1 insert + V2 migration + V3 migration = 3 rows
    assert rows[0] == 3


def test_get_db_returns_connection(temp_db):
    init_db()
    conn = get_db()
    assert conn is not None
    result = conn.execute("SELECT 1").fetchone()
    assert result[0] == 1


def test_init_db_drops_legacy_tables(temp_db):
    """Pre-versioned databases (no schema_version) get tables replaced."""
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "CREATE TABLE download_logs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  log_type TEXT NOT NULL,"
        "  message TEXT"
        ")"
    )
    conn.execute(
        "INSERT INTO download_logs (log_type, message)"
        " VALUES ('info', 'old data')"
    )
    conn.commit()
    conn.close()

    init_db()
    new_conn = sqlite3.connect(temp_db)
    cols = [
        row[1]
        for row in new_conn.execute("PRAGMA table_info(download_logs)")
    ]
    new_conn.close()
    assert "type" in cols
    assert "log_type" not in cols


def test_queue_status_check_constraint(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO download_queue"
        " (album_id, position, status) VALUES (1, 1, 'queued')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO download_queue"
            " (album_id, position, status) VALUES (2, 2, 'invalid')"
        )
    conn.close()


# --- V1 to V2 Migration ---


def _create_v1_db(db_path):
    """Create a V1 schema database directly (bypassing init_db)."""
    import time

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            applied_at REAL NOT NULL
        );
        CREATE TABLE download_history (
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
        CREATE TABLE download_logs (
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
        CREATE TABLE failed_tracks (
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
        CREATE TABLE download_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER NOT NULL UNIQUE,
            position INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'downloading'))
        );
    """)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (1, time.time()),
    )
    conn.commit()
    conn.close()


def test_migrate_v1_to_v2_creates_track_downloads(temp_db):
    """V1 schema migrates to V2: old tables dropped, new table created."""
    _create_v1_db(temp_db)

    init_db()

    conn = sqlite3.connect(temp_db)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "track_downloads" in tables
    assert "download_history" not in tables
    assert "failed_tracks" not in tables
    assert "download_logs" in tables
    assert "download_queue" in tables

    row = conn.execute(
        "SELECT version FROM schema_version"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 3
    conn.close()


def test_migrate_v1_to_v2_logs_no_failed_tracks_col(temp_db):
    """After V2 migration, download_logs has no failed_tracks column."""
    _create_v1_db(temp_db)

    init_db()

    conn = sqlite3.connect(temp_db)
    cols = [
        row[1] for row in conn.execute("PRAGMA table_info(download_logs)")
    ]
    assert "failed_tracks" not in cols
    assert "type" in cols
    assert "details" in cols
    conn.close()


def test_migrate_v1_to_v2_indexes(temp_db):
    """V2 migration creates all expected indexes on track_downloads."""
    _create_v1_db(temp_db)

    init_db()

    conn = sqlite3.connect(temp_db)
    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='track_downloads'"
        ).fetchall()
    }
    assert "idx_track_dl_album_id" in indexes
    assert "idx_track_dl_album_id_success" in indexes
    assert "idx_track_dl_timestamp" in indexes
    assert "idx_track_dl_youtube_url" in indexes
    conn.close()


def test_migrate_v1_to_v2_rollback_on_failure(temp_db, monkeypatch):
    """If migration fails, V1 schema remains and version stays at 1."""
    _create_v1_db(temp_db)

    def bad_migration(conn):
        conn.execute("DROP TABLE IF EXISTS download_history")
        raise RuntimeError("Simulated migration failure")

    monkeypatch.setattr("db._migrate_v1_to_v2", bad_migration)

    with pytest.raises(RuntimeError, match="Simulated migration failure"):
        init_db()

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT version FROM schema_version"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 1

    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "failed_tracks" in tables
    assert "track_downloads" not in tables
    conn.close()


# --- V2 to V3 Migration ---


def _create_v2_db(db_path):
    """Create a V2 schema database directly."""
    import time

    conn = sqlite3.connect(db_path)
    conn.executescript("""
        CREATE TABLE schema_version (
            version INTEGER NOT NULL,
            applied_at REAL NOT NULL
        );
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
        );
        CREATE TABLE download_logs (
            id TEXT PRIMARY KEY,
            type TEXT NOT NULL,
            album_id INTEGER NOT NULL,
            album_title TEXT NOT NULL,
            artist_name TEXT NOT NULL,
            timestamp REAL NOT NULL,
            details TEXT DEFAULT '',
            total_file_size INTEGER DEFAULT 0
        );
        CREATE TABLE download_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id INTEGER NOT NULL UNIQUE,
            position INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued'
                CHECK (status IN ('queued', 'downloading'))
        );
    """)
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (1, time.time()),
    )
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (2, time.time()),
    )
    conn.commit()
    conn.close()


def test_migrate_v2_to_v3_adds_acoustid_columns(temp_db):
    """V2 schema migrates to V3: acoustid columns added."""
    _create_v2_db(temp_db)

    init_db()

    conn = sqlite3.connect(temp_db)
    cols = [
        row[1] for row in conn.execute(
            "PRAGMA table_info(track_downloads)"
        )
    ]
    assert "acoustid_fingerprint_id" in cols
    assert "acoustid_score" in cols
    assert "acoustid_recording_id" in cols
    assert "acoustid_recording_title" in cols

    row = conn.execute(
        "SELECT version FROM schema_version"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    assert row[0] == 3
    conn.close()


def test_migrate_v2_to_v3_preserves_existing_data(temp_db):
    """V2→V3 migration preserves existing track_downloads rows."""
    import time as _time

    _create_v2_db(temp_db)

    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO track_downloads"
        " (album_id, album_title, artist_name, track_title,"
        "  track_number, success, timestamp)"
        " VALUES (1, 'Album', 'Artist', 'Track', 1, 1, ?)",
        (_time.time(),),
    )
    conn.commit()
    conn.close()

    init_db()

    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT acoustid_fingerprint_id, acoustid_score,"
        " acoustid_recording_id, acoustid_recording_title"
        " FROM track_downloads WHERE album_id = 1"
    ).fetchone()
    assert row[0] == ""
    assert row[1] == 0.0
    assert row[2] == ""
    assert row[3] == ""
    conn.close()
