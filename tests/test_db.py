import sqlite3

import pytest

from db import SCHEMA_VERSION, close_db, get_db, init_db


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
    assert row[0] == SCHEMA_VERSION


def test_init_db_idempotent(temp_db):
    init_db()
    init_db()  # should not raise
    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    conn.close()
    # One schema_version row per applied migration (V1 insert + V2..VN).
    assert rows[0] == SCHEMA_VERSION


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
    assert row[0] == SCHEMA_VERSION
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


def test_history_lookups_by_album_and_title_use_an_index(temp_db):
    # The History page groups track_downloads by (album_id, track_title) and
    # the backoff counts failures per title; without this index both scan
    # the whole table on every load. Added outside migrations, so existing
    # databases get it on the next start too.
    init_db()
    conn = get_db()
    plan = " ".join(
        str(row[3]) for row in conn.execute(
            "EXPLAIN QUERY PLAN SELECT MAX(timestamp) FROM track_downloads"
            " WHERE album_id = 1 AND track_title = 'x'"
        ).fetchall()
    )
    assert "idx_track_dl_album_title_ts" in plan


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
    assert row[0] == SCHEMA_VERSION
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


# --- V3 to V4 Migration ---


def test_migration_v3_to_v4_creates_banned_urls(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    tables = [
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    ]
    assert "banned_urls" in tables
    cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(banned_urls)")
    ]
    assert "youtube_url" in cols
    assert "album_id" in cols
    assert "track_title" in cols
    assert "banned_at" in cols
    conn.close()


def test_migration_v3_to_v4_adds_deleted_column(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(track_downloads)")
    ]
    assert "deleted" in cols
    conn.close()


def test_schema_version_is_4(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    row = conn.execute(
        "SELECT version FROM schema_version"
        " ORDER BY version DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert row[0] == SCHEMA_VERSION


# --- V4 to V5 Migration ---


def test_v5_migration_creates_candidate_attempts(temp_db):
    """V5 migration creates candidate_attempts table with all columns."""
    init_db()
    conn = sqlite3.connect(temp_db)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    assert "candidate_attempts" in tables

    cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(candidate_attempts)")
    ]
    expected = [
        "id", "track_download_id", "youtube_url", "youtube_title",
        "match_score", "duration_seconds", "outcome",
        "acoustid_matched_id", "acoustid_matched_title",
        "acoustid_score", "expected_recording_id",
        "error_message", "timestamp",
    ]
    for col in expected:
        assert col in cols, f"Missing column: {col}"

    indexes = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND tbl_name='candidate_attempts'"
        ).fetchall()
    }
    assert "idx_ca_track_dl_id" in indexes
    conn.close()


def test_v5_migration_adds_download_logs_columns(temp_db):
    """V5 migration adds track_title, track_number, track_download_id to download_logs."""
    init_db()
    conn = sqlite3.connect(temp_db)
    cols = [
        row[1]
        for row in conn.execute("PRAGMA table_info(download_logs)")
    ]
    assert "track_title" in cols
    assert "track_number" in cols
    assert "track_download_id" in cols
    conn.close()


def test_v5_migration_preserves_existing_logs(temp_db):
    """Existing download_logs rows get default values for new columns."""
    init_db()
    conn = sqlite3.connect(temp_db)
    conn.execute(
        "INSERT INTO download_logs"
        " (id, type, album_id, album_title, artist_name,"
        "  timestamp, details, total_file_size)"
        " VALUES ('test1', 'download_success', 1, 'Album', 'Artist',"
        "  1234567890.0, 'ok', 0)"
    )
    conn.commit()

    row = conn.execute(
        "SELECT track_title, track_number, track_download_id"
        " FROM download_logs WHERE id = 'test1'"
    ).fetchone()
    assert row[0] == ""
    assert row[1] is None
    assert row[2] is None
    conn.close()


def test_migrate_v7_to_v8_reassigns_playlist_ids(temp_db):
    """Legacy playlist rows (album_id=0) become unique negative ids in both
    track_downloads and download_logs; distinct playlists get distinct ids."""
    import db as db_mod
    import models
    init_db()
    for title, artist, track in (
        ("PlA", "ArtA", "t1"), ("PlA", "ArtA", "t2"), ("PlB", "ArtB", "t3"),
    ):
        models.add_track_download(
            album_id=0, album_title=title, artist_name=artist,
            track_title=track, track_number=1, success=False,
            error_message="", youtube_url="", youtube_title="",
            match_score=0.0, duration_seconds=0, album_path="/d",
            lidarr_album_path="", cover_url="",
        )
    models.add_log(
        log_type="track_failure", album_id=0,
        album_title="PlA", artist_name="ArtA",
    )
    conn = get_db()
    db_mod._migrate_v7_to_v8(conn)
    conn.commit()

    rows = conn.execute(
        "SELECT DISTINCT album_title, album_id FROM track_downloads"
        " ORDER BY album_title"
    ).fetchall()
    ids = {r[0]: r[1] for r in rows}
    assert ids["PlA"] < 0 and ids["PlB"] < 0
    assert ids["PlA"] != ids["PlB"]
    # The download_logs row for PlA got the same negative id as its tracks.
    log_id = conn.execute(
        "SELECT album_id FROM download_logs WHERE album_title = 'PlA'"
    ).fetchone()[0]
    assert log_id == ids["PlA"]
    # No legacy album_id=0 rows remain.
    assert conn.execute(
        "SELECT COUNT(*) FROM track_downloads WHERE album_id = 0"
    ).fetchone()[0] == 0


def test_migrate_v9_to_v10_adds_source_format(temp_db):
    init_db()
    conn = get_db()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(track_downloads)")]
    assert "source_format" in cols


def test_v10_to_v11_adds_queue_force_column():
    """download_queue gains the force flag used to bypass retry backoff."""
    init_db()
    conn = get_db()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(download_queue)")}
    assert "force" in cols
    version = conn.execute(
        "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
    ).fetchone()[0]
    assert version == SCHEMA_VERSION
    assert conn.execute(
        "SELECT COUNT(*) FROM schema_version WHERE version = 11"
    ).fetchone()[0] == 1
    close_db()


def test_v11_to_v12_adds_album_source_hints(temp_db):
    conn = sqlite3.connect(temp_db)
    conn.executescript(
        "CREATE TABLE schema_version (version INTEGER NOT NULL,"
        " applied_at REAL NOT NULL);"
        "INSERT INTO schema_version VALUES (11, 0);"
    )
    conn.commit()
    conn.close()
    import db as db_mod
    db_mod._migrate_v11_to_v12(get_db())
    get_db().commit()
    cols = {r[1] for r in get_db().execute(
        "PRAGMA table_info(album_source_hints)"
    )}
    assert {"album_id", "playlist_id", "browse_id", "source", "created_at"} <= cols


def test_fresh_install_reaches_current_schema_with_hints_table(temp_db):
    init_db()
    conn = get_db()
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "album_source_hints" in tables
    versions = [r[0] for r in conn.execute(
        "SELECT version FROM schema_version ORDER BY version"
    )]
    assert versions[-1] == SCHEMA_VERSION == 13
    assert 12 in versions
    assert 13 in versions


def test_ensure_current_tables_recreates_missing_hints_table(temp_db):
    init_db()
    conn = get_db()
    conn.execute("DROP TABLE album_source_hints")
    conn.commit()
    init_db()
    tables = {r[0] for r in get_db().execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "album_source_hints" in tables


def _v12_track_downloads(temp_db):
    """A schema-12 database holding a few rows to backfill."""
    init_db()
    conn = get_db()
    conn.execute("DELETE FROM schema_version WHERE version = 13")
    conn.execute("DROP INDEX IF EXISTS idx_track_dl_recording_id")
    conn.execute("DROP INDEX IF EXISTS idx_track_dl_video_id")
    for col in ("recording_id", "video_id", "file_path", "file_size",
                "reused_from"):
        conn.execute(f"ALTER TABLE track_downloads DROP COLUMN {col}")
    rows = [
        # (album, title, success, url, acoustid recording)
        (1, "A", 1, "https://music.youtube.com/watch?v=abcdefghijk", "rec-1"),
        (1, "B", 1, "https://www.youtube.com/watch?v=lmnopqrstuv&t=3", ""),
        (2, "C", 0, "https://www.youtube.com/watch?v=zzzzzzzzzzz", "rec-9"),
        (2, "D", 1, "", "rec-2"),
        (2, "E", 1, "not a url", ""),
    ]
    for album_id, title, ok, url, rec in rows:
        conn.execute(
            "INSERT INTO track_downloads (album_id, album_title, artist_name,"
            " track_title, success, youtube_url, acoustid_recording_id,"
            " timestamp) VALUES (?, 'Alb', 'Art', ?, ?, ?, ?, 1.0)",
            (album_id, title, ok, url, rec),
        )
    conn.commit()
    return conn


def test_v12_to_v13_adds_columns_and_backfills(temp_db):
    conn = _v12_track_downloads(temp_db)
    import db as db_mod
    db_mod._migrate_v12_to_v13(conn)
    conn.commit()
    cols = {r[1] for r in conn.execute("PRAGMA table_info(track_downloads)")}
    assert {"recording_id", "video_id", "file_path", "file_size",
            "reused_from"} <= cols
    got = {
        r["track_title"]: (r["recording_id"], r["video_id"])
        for r in conn.execute(
            "SELECT track_title, recording_id, video_id FROM track_downloads"
        )
    }
    assert got["A"] == ("rec-1", "abcdefghijk")
    assert got["B"] == ("", "lmnopqrstuv")
    # Failures are not backfilled: only a stored success can be reused.
    assert got["C"] == ("", "")
    assert got["D"] == ("rec-2", "")
    assert got["E"] == ("", "")


def test_v12_to_v13_is_idempotent_and_keeps_existing_values(temp_db):
    conn = _v12_track_downloads(temp_db)
    import db as db_mod
    db_mod._migrate_v12_to_v13(conn)
    conn.execute(
        "UPDATE track_downloads SET recording_id = 'kept', file_size = 7"
        " WHERE track_title = 'A'"
    )
    conn.commit()
    db_mod._migrate_v12_to_v13(conn)  # second run must not fail or clobber
    conn.commit()
    row = conn.execute(
        "SELECT recording_id, file_size FROM track_downloads"
        " WHERE track_title = 'A'"
    ).fetchone()
    assert (row["recording_id"], row["file_size"]) == ("kept", 7)


def test_v13_indexes_exist_and_are_used(temp_db):
    init_db()
    conn = get_db()
    names = {r[1] for r in conn.execute("PRAGMA index_list(track_downloads)")}
    assert {"idx_track_dl_recording_id", "idx_track_dl_video_id"} <= names
    plan = " ".join(
        r[3] for r in conn.execute(
            "EXPLAIN QUERY PLAN SELECT id FROM track_downloads"
            " WHERE recording_id = 'x' AND recording_id != ''"
        )
    )
    assert "idx_track_dl_recording_id" in plan


def test_upgrade_from_v12_runs_the_migration_on_init(temp_db):
    _v12_track_downloads(temp_db)
    close_db()
    init_db()
    conn = get_db()
    assert conn.execute(
        "SELECT MAX(version) FROM schema_version"
    ).fetchone()[0] == SCHEMA_VERSION
    row = conn.execute(
        "SELECT recording_id, video_id FROM track_downloads"
        " WHERE track_title = 'A'"
    ).fetchone()
    assert (row["recording_id"], row["video_id"]) == ("rec-1", "abcdefghijk")
