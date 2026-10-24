import os

import pytest

from db import close_db, init_db


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    init_db()
    yield
    close_db()


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr("config.CONFIG_FILE", str(tmp_path / "config.json"))
    monkeypatch.setenv("DOWNLOAD_PATH", str(tmp_path / "downloads"))
    monkeypatch.setenv("LIDARR_URL", "http://localhost:8686")
    monkeypatch.setenv("LIDARR_API_KEY", "test-key")
    from app import app
    from utils import rate_limit_store

    rate_limit_store.clear()
    app.config["TESTING"] = True  # nosemgrep
    with app.test_client() as c:
        yield c
    rate_limit_store.clear()


SEARCH_ENTRY = {
    "title": "One More Time", "artist": "Daft Punk", "album": "Discovery",
    "duration": 320, "search": True,
}


class TestInfoRoute:
    def test_external_link_returns_entries(self, client, monkeypatch):
        import external_playlists
        monkeypatch.setattr(external_playlists, "fetch", lambda url: {
            "service": "spotify", "kind": "playlist", "title": "Mix", "owner": "me",
            "thumbnail": "", "tracks": [{"title": "T", "artist": "A", "album": "", "duration": 5, "thumbnail": ""}],
        })
        resp = client.post("/api/youtube/playlist/info", json={"url": "https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M"})
        data = resp.get_json()
        assert resp.status_code == 200
        assert data["source"] == "Spotify"
        assert data["entries"][0]["search"] is True

    def test_external_error_is_reported(self, client, monkeypatch):
        import external_playlists

        def fail(url):
            raise external_playlists.ExternalPlaylistError("Not found. Is the playlist public?", 404)

        monkeypatch.setattr(external_playlists, "fetch", fail)
        resp = client.post("/api/youtube/playlist/info", json={"url": "https://www.deezer.com/playlist/1"})
        assert resp.status_code == 404
        assert "public" in resp.get_json()["error"]


class TestDownloadRoute:
    def test_search_entries_are_accepted(self, client, monkeypatch):
        import youtube_import
        started = []
        monkeypatch.setattr(
            youtube_import, "_start_playlist_import",
            lambda artist, album, entries, thumb, source: started.append(entries) or ("ok", 200),
        )
        resp = client.post("/api/youtube/playlist/download", json={
            "artist_name": "Mix", "album_title": "",
            "entries": [SEARCH_ENTRY], "thumbnail_url": "", "source_url": "",
        })
        assert resp.status_code == 200
        assert started == [[{"title": "One More Time", "artist": "Daft Punk", "album": "Discovery", "duration": 320}]]

    def test_search_entry_needs_artist(self, client):
        resp = client.post("/api/youtube/playlist/download", json={
            "artist_name": "Mix", "entries": [{"title": "T", "artist": "", "search": True}],
        })
        assert resp.status_code == 400


class TestRunner:
    def test_searched_track_is_downloaded_tagged_and_listed(self, monkeypatch, tmp_path):
        import models
        import youtube_import
        target = tmp_path / "downloads" / "Mix"
        monkeypatch.setattr("utils.DOWNLOAD_DIR", str(tmp_path / "downloads"))
        searched = []
        tagged = []

        def fake_search(query, output_path, title, expected_duration_ms=None, **kw):
            searched.append((query, title, expected_duration_ms))
            with open(output_path + ".mp3", "wb") as f:
                f.write(b"AUDIO")
            return {"success": True, "youtube_url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
                    "youtube_title": "Daft Punk - One More Time", "duration_seconds": 320}

        monkeypatch.setattr(youtube_import, "download_track_youtube", fake_search)
        monkeypatch.setattr(youtube_import, "download_youtube_candidate", lambda *a, **k: pytest.fail("no direct url"))
        monkeypatch.setattr(youtube_import, "tag_audio_file", lambda path, info, album, cover: tagged.append((info["trackArtist"], album["artist"]["artistName"])))
        monkeypatch.setattr(youtube_import, "apply_file_extras", lambda *a, **k: None)
        monkeypatch.setattr(youtube_import, "send_notifications", lambda *a, **k: None)
        monkeypatch.setattr(youtube_import, "set_permissions", lambda *a, **k: None)
        entry = {k: v for k, v in SEARCH_ENTRY.items() if k != "search"}
        ok = youtube_import._execute_playlist_download("Mix", "", [entry], str(target), {"audio_format": "mp3"})
        assert ok is True
        assert searched == [("Daft Punk One More Time official audio", "One More Time", 320000)]
        assert tagged == [("Daft Punk", "Mix")]
        assert (target / "01 - One More Time.mp3").exists()
        m3u = (target / "Mix.m3u8").read_text(encoding="utf-8").splitlines()
        assert m3u == ["#EXTM3U", "#EXTINF:320,Daft Punk - One More Time", "01 - One More Time.mp3"]
        rows = models.get_track_downloads_for_album(-1)
        assert rows[0]["youtube_url"] == "https://www.youtube.com/watch?v=bbbbbbbbbbb"
        assert rows[0]["track_artist"] == "Daft Punk"

    def test_failed_search_records_failure_without_m3u(self, monkeypatch, tmp_path):
        import models
        import youtube_import
        target = tmp_path / "downloads" / "Mix"
        monkeypatch.setattr("utils.DOWNLOAD_DIR", str(tmp_path / "downloads"))
        monkeypatch.setattr(youtube_import, "download_track_youtube", lambda *a, **k: {"success": False, "error_message": "No suitable YouTube match found"})
        monkeypatch.setattr(youtube_import, "send_notifications", lambda *a, **k: None)
        entry = {k: v for k, v in SEARCH_ENTRY.items() if k != "search"}
        youtube_import._execute_playlist_download("Mix", "", [entry], str(target), {"audio_format": "mp3"})
        assert not os.path.exists(target / "Mix.m3u8")
        rows = models.get_track_downloads_for_album(-1)
        assert rows[0]["success"] == 0
        assert "No suitable" in rows[0]["error_message"]


class TestLidarrJobRoutes:
    def test_start_and_status(self, client, monkeypatch):
        import external_playlists
        monkeypatch.setattr(external_playlists, "start_lidarr_job", lambda entries: {"running": True, "done": 0, "total": 1, "albums": []})
        resp = client.post("/api/youtube/external/lidarr", json={"entries": [SEARCH_ENTRY]})
        assert resp.status_code == 200 and resp.get_json()["total"] == 1
        assert client.get("/api/youtube/external/lidarr/status").status_code == 200

    def test_bad_body(self, client):
        assert client.post("/api/youtube/external/lidarr", json={"entries": "x"}).status_code == 400
