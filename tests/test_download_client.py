"""Tests for the Lidarr download-client bridge (Newznab + SABnzbd)."""

import io

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


def test_empty_rss_sync_returns_no_items(client):
    _seed_album()
    resp = client.get("/api/newznab/api?t=search&apikey=secret")
    assert "<item>" not in resp.get_data(as_text=True)


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


def test_nzb_round_trip_meta_and_subject():
    nzb = download_client._build_nzb(123, "Art - Alb", "music")
    assert download_client._parse_album_id_from_nzb(nzb) == 123
    # subject-only fallback
    assert download_client._parse_album_id_from_nzb(
        "junk lidarr_album_id=777 more"
    ) == 777
