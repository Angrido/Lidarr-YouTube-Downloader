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
    assert "download_history" in tables
    assert "download_logs" in tables
    assert "failed_tracks" in tables
    assert "download_queue" in tables


def test_init_db_sets_schema_version(temp_db):
    init_db()
    conn = sqlite3.connect(temp_db)
    row = conn.execute("SELECT version FROM schema_version").fetchone()
    conn.close()
    assert row[0] == 1


def test_init_db_idempotent(temp_db):
    init_db()
    init_db()  # should not raise
    conn = sqlite3.connect(temp_db)
    rows = conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()
    conn.close()
    assert rows[0] == 1


def test_get_db_returns_connection(temp_db):
    init_db()
    conn = get_db()
    assert conn is not None
    result = conn.execute("SELECT 1").fetchone()
    assert result[0] == 1


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
