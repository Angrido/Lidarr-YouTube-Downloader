"""Tests for the Lidarr download-client bridge (Newznab + SABnzbd)."""

import io
import time

import pytest

import db
import download_client
import models
from config import load_config, save_config


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test.db")
    monkeypatch.setattr("db.DB_PATH", db_path)
    db.init_db()
    yield db_path
    db.close_db()


@pytest.fixture(autouse=True)
def clear_registry():
    download_client._jobs.clear()
    download_client._album_to_nzo.clear()
    yield
    download_client._jobs.clear()
    download_client._album_to_nzo.clear()


@pytest.fixture()
def configured(tmp_path, monkeypatch):
    config_file = str(tmp_path / "config.json")
    monkeypatch.setattr("config.CONFIG_FILE", config_file)
    cfg = load_config()
    cfg["download_client_enabled"] = True
    cfg["download_client_api_key"] = "secret"
    cfg["download_client_category"] = "music"
    save_config(cfg)
    yield cfg


@pytest.fixture()
def client(configured):
    from app import app

    app.config["TESTING"] = True  # nosemgrep
    with app.test_client() as c:
        yield c


def _seed_album(album_id=42, artist="Daft Punk", title="Discovery"):
    album = {
        "id": album_id,
        "foreignAlbumId": f"mbid-{album_id}",
        "title": title,
        "releaseDate": "2001-03-12",
        "artist": {"id": 7, "artistName": artist},
        "statistics": {"trackCount": 14, "trackFileCount": 0},
        "images": [{"coverType": "cover", "remoteUrl": "http://c/x.jpg"}],
    }
    models.upsert_missing_album(album, run_id=1)
    return album


# --- Newznab indexer ------------------------------------------------------


def test_caps_returns_music_search(client):
    resp = client.get("/api/newznab/api?t=caps")
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "music-search" in body
    assert '<category id="3000"' in body


def test_newznab_disabled(client, monkeypatch):
    cfg = load_config()
    cfg["download_client_enabled"] = False
    save_config(cfg)
    resp = client.get("/api/newznab/api?t=caps")
    assert "error" in resp.get_data(as_text=True)


def test_search_requires_apikey(client):
    _seed_album()
    resp = client.get("/api/newznab/api?t=music&artist=Daft+Punk&album=Discovery")
    assert "Incorrect user credentials" in resp.get_data(as_text=True)


def test_search_matches_album(client):
    _seed_album(album_id=42, artist="Daft Punk", title="Discovery")
    resp = client.get(
        "/api/newznab/api?t=music&artist=Daft+Punk&album=Discovery"
        "&apikey=secret"
    )
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "<item>" in body
    assert "id=42" in body
    assert "application/x-nzb" in body
    assert "Daft Punk - Discovery" in body


def test_search_no_match_returns_empty(client):
    _seed_album()
    resp = client.get(
        "/api/newznab/api?t=music&artist=Nobody&album=Nothing&apikey=secret"
    )
    body = resp.get_data(as_text=True)
    assert "<item>" not in body


def test_rss_feed_lists_missing_albums(client):
    # No search terms = Lidarr indexer Test / RSS sync: must return the
    # cached missing albums so the test passes and RSS can auto-grab.
    _seed_album(album_id=42, artist="Daft Punk", title="Discovery")
    resp = client.get("/api/newznab/api?t=search&apikey=secret")
    body = resp.get_data(as_text=True)
    assert "<item>" in body
    assert "id=42" in body


def test_rss_feed_empty_when_no_cache(client):
    resp = client.get("/api/newznab/api?t=search&apikey=secret")
    assert "<item>" not in resp.get_data(as_text=True)


def _log_attempt(album_id=42, artist="Daft Punk", title="Discovery"):
    models.add_log(
        log_type="album_error", album_id=album_id,
        album_title=title, artist_name=artist, details="fail",
    )


def test_rss_feed_excludes_recently_attempted(client):
    _seed_album(album_id=42)
    _log_attempt(42)
    resp = client.get("/api/newznab/api?t=search&apikey=secret")
    assert "id=42" not in resp.get_data(as_text=True)


def test_targeted_search_excludes_recently_attempted(client):
    _seed_album(album_id=42, artist="Daft Punk", title="Discovery")
    _log_attempt(42)
    resp = client.get(
        "/api/newznab/api?t=music&artist=Daft+Punk&album=Discovery"
        "&apikey=secret"
    )
    assert "<item>" not in resp.get_data(as_text=True)


def test_rss_feed_excludes_active_job(client):
    _seed_album(album_id=42)
    download_client.register_grab(42, "x", "music")
    resp = client.get("/api/newznab/api?t=search&apikey=secret")
    assert "id=42" not in resp.get_data(as_text=True)


def test_addfile_blocked_after_failed_client_job(client):
    _seed_album(album_id=42)
    # A recent *client* job failure should refuse a re-grab (this is the
    # grab -> fail -> re-grab loop we guard against).
    models.upsert_client_job({
        "nzo_id": "old", "album_id": 42, "status": "failed",
        "added_ts": time.time(), "completed_ts": time.time(),
    })
    nzb = download_client._build_nzb(42, "Daft Punk - Discovery", "music")
    data = {"name": (io.BytesIO(nzb.encode()), "release.nzb")}
    resp = client.post(
        "/api/sabnzbd/api?mode=addfile&apikey=secret&cat=music",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.get_json()["status"] is False
    assert models.get_queue_length() == 0
    assert not download_client.is_client_album(42)


def test_addfile_not_blocked_after_manual_attempt(client):
    _seed_album(album_id=42)
    # A generic download_logs row (manual/scheduler) must NOT block a grab;
    # only a recent client-job failure does.
    _log_attempt(42)
    nzb = download_client._build_nzb(42, "Daft Punk - Discovery", "music")
    data = {"name": (io.BytesIO(nzb.encode()), "release.nzb")}
    resp = client.post(
        "/api/sabnzbd/api?mode=addfile&apikey=secret&cat=music",
        data=data,
        content_type="multipart/form-data",
    )
    assert resp.get_json()["status"] is True
    assert models.get_queue_length() == 1
    assert download_client.is_client_album(42)


def test_nzb_download_embeds_album_id(client):
    _seed_album(album_id=99)
    resp = client.get("/api/newznab/api?t=get&id=99&apikey=secret")
    assert resp.status_code == 200
    assert resp.mimetype == "application/x-nzb"
    parsed = download_client._parse_album_id_from_nzb(
        resp.get_data(as_text=True)
    )
    assert parsed == 99


# --- SABnzbd download client ---------------------------------------------


def test_sab_version_no_auth(client):
    resp = client.get("/api/sabnzbd/api?mode=version")
    assert resp.get_json()["version"]


def test_sab_bad_apikey(client):
    resp = client.get("/api/sabnzbd/api?mode=queue&apikey=wrong")
    assert resp.get_json()["status"] is False


def test_sab_get_config(client):
    resp = client.get("/api/sabnzbd/api?mode=get_config&apikey=secret")
    cfg = resp.get_json()["config"]
    names = [c["name"] for c in cfg["categories"]]
    assert "music" in names
    assert cfg["misc"]["enable_tv_sorting"] is False
    assert cfg["misc"]["complete_dir"]


def test_sab_addfile_enqueues(client):
    _seed_album(album_id=42)
    nzb = download_client._build_nzb(42, "Daft Punk - Discovery", "music")
    data = {"name": (io.BytesIO(nzb.encode()), "release.nzb")}
    resp = client.post(
        "/api/sabnzbd/api?mode=addfile&apikey=secret&cat=music",
        data=data,
        content_type="multipart/form-data",
    )
    body = resp.get_json()
    assert body["status"] is True
    assert body["nzo_ids"]
    # album landed in the shared download queue
    assert models.get_queue_length() == 1
    assert download_client.is_client_album(42)


def test_sab_queue_and_history_lifecycle(client):
    _seed_album(album_id=42)
    nzo = download_client.register_grab(42, "Daft Punk - Discovery", "music")

    q = client.get("/api/sabnzbd/api?mode=queue&apikey=secret").get_json()
    assert q["queue"]["noofslots"] == 1
    assert q["queue"]["slots"][0]["nzo_id"] == nzo
    assert q["queue"]["slots"][0]["cat"] == "music"

    download_client.mark_completed(42, "/downloads/Daft Punk/Discovery")
    h = client.get("/api/sabnzbd/api?mode=history&apikey=secret").get_json()
    assert h["history"]["noofslots"] == 1
    slot = h["history"]["slots"][0]
    assert slot["status"] == "Completed"
    assert slot["storage"] == "/downloads/Daft Punk/Discovery"
    # finished job leaves the active queue
    q2 = client.get("/api/sabnzbd/api?mode=queue&apikey=secret").get_json()
    assert q2["queue"]["noofslots"] == 0


def test_sab_history_delete(client):
    download_client.register_grab(42, "X", "music")
    download_client.mark_failed(42, "boom")
    resp = client.get(
        "/api/sabnzbd/api?mode=history&name=delete&value=all&apikey=secret"
    )
    assert resp.get_json()["status"] is True
    h = client.get("/api/sabnzbd/api?mode=history&apikey=secret").get_json()
    assert h["history"]["noofslots"] == 0


def test_register_grab_idempotent(client):
    a = download_client.register_grab(42, "X", "music")
    b = download_client.register_grab(42, "X", "music")
    assert a == b
    assert models.get_queue_length() == 1


# --- registry / job marking ----------------------------------------------


def test_is_client_album_only_while_active():
    download_client.register_grab(5, "Y", "music")
    assert download_client.is_client_album(5)
    download_client.mark_completed(5, "/tmp/x")
    assert not download_client.is_client_album(5)


def test_job_is_persisted(client):
    nzo = download_client.register_grab(42, "Daft Punk - Discovery", "music")
    rows = models.get_all_client_jobs()
    assert len(rows) == 1
    assert rows[0]["nzo_id"] == nzo
    assert rows[0]["album_id"] == 42
    assert rows[0]["status"] == "queued"


def test_completed_job_persisted(client):
    download_client.register_grab(42, "X", "music")
    download_client.mark_completed(42, "/downloads/X")
    rows = models.get_all_client_jobs()
    assert rows[0]["status"] == "completed"
    assert rows[0]["storage"] == "/downloads/X"


def test_restore_resumes_interrupted_download(client):
    # Simulate a job left mid-download by a restart.
    models.upsert_client_job({
        "nzo_id": "SABnzbd_nzo_old", "album_id": 42, "name": "X",
        "category": "music", "status": "downloading", "storage": "",
        "size": 100, "error": "", "added_ts": 1.0, "completed_ts": None,
    })
    download_client._jobs.clear()
    download_client._album_to_nzo.clear()

    download_client.restore_jobs()

    assert "SABnzbd_nzo_old" in download_client._jobs
    # interrupted download is reset to queued and re-enqueued for retry
    assert download_client.is_client_album(42)
    assert models.get_queue_length() == 1
    assert models.get_all_client_jobs()[0]["status"] == "queued"


def test_restore_keeps_completed_in_history(client):
    models.upsert_client_job({
        "nzo_id": "SABnzbd_nzo_done", "album_id": 7, "name": "Y",
        "category": "music", "status": "completed", "storage": "/d/Y",
        "size": 100, "error": "", "added_ts": 1.0, "completed_ts": 2.0,
    })
    download_client._jobs.clear()
    download_client._album_to_nzo.clear()

    download_client.restore_jobs()

    assert not download_client.is_client_album(7)
    h = client.get("/api/sabnzbd/api?mode=history&apikey=secret").get_json()
    assert h["history"]["noofslots"] == 1
    assert h["history"]["slots"][0]["storage"] == "/d/Y"


def test_remove_job_deletes_from_db(client):
    nzo = download_client.register_grab(42, "X", "music")
    download_client.remove_job(nzo)
    assert models.get_all_client_jobs() == []


def test_nzb_round_trip_meta_and_subject():
    nzb = download_client._build_nzb(123, "Art - Alb", "music")
    assert download_client._parse_album_id_from_nzb(nzb) == 123
    # subject-only fallback
    assert download_client._parse_album_id_from_nzb(
        "junk lidarr_album_id=777 more"
    ) == 777


# --- Regression tests for review fixes ------------------------------------


def test_release_title_handles_none_release_date():
    # An explicit None release_date must not render a literal "(None)" year.
    title = download_client._release_title(
        {"artist_name": "A", "title": "B", "release_date": None},
        {"audio_format": "mp3", "audio_quality": "320"},
    )
    assert "(None)" not in title
    assert title.startswith("A - B")


def test_match_album_falls_back_to_next_best_when_top_excluded(client):
    # Higher-scoring album is excluded; a second matching album must still
    # be returned instead of None.
    _seed_album(album_id=1, artist="The Beatles", title="Help")
    _seed_album(album_id=2, artist="The Beatles", title="Help Deluxe")
    match = download_client._match_album(
        "The Beatles", "Help", excluded={1},
    )
    assert match is not None
    assert match["album_id"] == 2


def test_run_album_job_stopped_removes_job(client, monkeypatch):
    nzo = download_client.register_grab(42, "X", "music")
    monkeypatch.setattr(
        "processing.process_album_download",
        lambda *a, **kw: {"stopped": True},
    )
    download_client.run_album_job(42)
    # A user stop drops the job entirely (no 'Failed' slot for Lidarr to
    # blocklist).
    assert download_client._jobs.get(nzo) is None
    assert not download_client.is_client_album(42)


def test_run_album_job_empty_path_marks_failed(client, monkeypatch):
    download_client.register_grab(42, "X", "music")
    monkeypatch.setattr(
        "processing.process_album_download",
        lambda *a, **kw: {"success": True, "album_path": ""},
    )
    download_client.run_album_job(42)
    jobs = models.get_all_client_jobs()
    assert len(jobs) == 1
    assert jobs[0]["status"] == "failed"


def test_run_album_job_busy_resets_to_queued(client, monkeypatch):
    nzo = download_client.register_grab(42, "X", "music")
    monkeypatch.setattr(
        "processing.process_album_download",
        lambda *a, **kw: {"error": "Busy"},
    )
    download_client.run_album_job(42)
    # Job stays active but is reset to queued (no phantom 'downloading').
    assert download_client._jobs[nzo]["status"] == "queued"
    assert download_client.is_client_album(42)


def test_register_grab_rolls_back_on_enqueue_failure(client, monkeypatch):
    def boom(_album_id):
        raise RuntimeError("db locked")

    monkeypatch.setattr("models.enqueue_album", boom)
    with pytest.raises(RuntimeError):
        download_client.register_grab(42, "X", "music")
    # Nothing left mapped/persisted, so the album can be grabbed later.
    assert not download_client.is_client_album(42)
    assert models.get_all_client_jobs() == []


def test_get_cached_album_returns_single_row():
    _seed_album(album_id=55, artist="Air", title="Moon Safari")
    row = models.get_cached_album(55)
    assert row is not None
    assert row["album_id"] == 55
    assert row["title"] == "Moon Safari"
    assert models.get_cached_album(99999) is None


def test_release_pubdate_is_utc():
    # 2001-03-12 parsed as UTC midnight, independent of host TZ.
    import calendar
    ts = download_client._release_pubdate("2001-03-12")
    assert ts == calendar.timegm((2001, 3, 12, 0, 0, 0, 0, 0, 0))
