import pytest

import db
import lidarr_sync
import models


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    monkeypatch.setattr(lidarr_sync, "PAGE_SIZE", 2)
    monkeypatch.setattr(lidarr_sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(lidarr_sync, "_last_sync_summary", None)
    yield db_path
    db.close_db()


def _album(album_id):
    return {
        "id": album_id,
        "title": f"Album {album_id}",
        "artist": {"id": 1, "artistName": "Artist"},
        "statistics": {"trackCount": 10, "trackFileCount": 0},
    }


def _fake_lidarr(pages, totals=None):
    calls = []

    def fake(endpoint, *args, **kwargs):
        page = int(endpoint.split("page=")[1].split("&")[0])
        calls.append(page)
        records = pages.get(page, [])
        total = (totals or {}).get(page, sum(len(p) for p in pages.values()))
        return {"records": records, "totalRecords": total}

    return fake, calls


def _seed_cache(album_ids):
    run_id = models.bump_sync_run_id()
    models.upsert_missing_albums_batch([_album(i) for i in album_ids], run_id)


def _cached_ids():
    return {a["id"] for a in models.get_cached_missing_albums()}


def test_successful_sync_prunes_stale_albums(monkeypatch):
    _seed_cache([1, 2, 3, 99])
    fake, _ = _fake_lidarr({1: [_album(1), _album(2)], 2: [_album(3)]})
    monkeypatch.setattr(lidarr_sync, "lidarr_request", fake)
    lidarr_sync._run_sync()
    assert _cached_ids() == {1, 2, 3}
    state = models.get_sync_state()
    assert state["status"] == "idle"
    assert state["last_error"] == ""
    assert state["last_full_sync_at"]


def test_failed_page_upsert_keeps_its_albums_and_reports_error(monkeypatch):
    _seed_cache([1, 2, 3, 4, 99])
    fake, calls = _fake_lidarr({
        1: [_album(1), _album(2)],
        2: [_album(3), _album(4)],
        3: [],
    })
    monkeypatch.setattr(lidarr_sync, "lidarr_request", fake)
    real_upsert = models.upsert_missing_albums_batch

    def flaky_upsert(records, run_id):
        if any(r["id"] == 3 for r in records):
            raise RuntimeError("database is locked")
        return real_upsert(records, run_id)

    monkeypatch.setattr(models, "upsert_missing_albums_batch", flaky_upsert)
    lidarr_sync._run_sync()
    assert _cached_ids() == {1, 2, 3, 4, 99}
    state = models.get_sync_state()
    assert state["status"] == "error"
    assert state["last_error"]
    assert not state["last_full_sync_at"]
    assert calls == [1, 2]


def test_total_records_change_mid_sync_skips_prune(monkeypatch):
    _seed_cache([1, 2, 3, 99])
    fake, _ = _fake_lidarr(
        {1: [_album(1), _album(2)], 2: [_album(3)]},
        totals={1: 4, 2: 3},
    )
    monkeypatch.setattr(lidarr_sync, "lidarr_request", fake)
    lidarr_sync._run_sync()
    assert _cached_ids() == {1, 2, 3, 99}
    state = models.get_sync_state()
    assert state["status"] == "idle"
    assert state["last_error"] == ""
    assert state["last_full_sync_at"]


def test_next_stable_sync_prunes_after_skipped_one(monkeypatch):
    _seed_cache([1, 2, 3, 99])
    fake, _ = _fake_lidarr(
        {1: [_album(1), _album(2)], 2: [_album(3)]},
        totals={1: 4, 2: 3},
    )
    monkeypatch.setattr(lidarr_sync, "lidarr_request", fake)
    lidarr_sync._run_sync()
    fake, _ = _fake_lidarr({1: [_album(1), _album(2)], 2: [_album(3)]})
    monkeypatch.setattr(lidarr_sync, "lidarr_request", fake)
    lidarr_sync._run_sync()
    assert _cached_ids() == {1, 2, 3}


def test_fetch_failure_keeps_cache_and_reports_error(monkeypatch):
    _seed_cache([1, 99])
    monkeypatch.setattr(
        lidarr_sync, "lidarr_request",
        lambda *a, **k: {"error": "Lidarr request timed out"},
    )
    lidarr_sync._run_sync()
    assert _cached_ids() == {1, 99}
    state = models.get_sync_state()
    assert state["status"] == "error"
    assert "timed out" in state["last_error"]
