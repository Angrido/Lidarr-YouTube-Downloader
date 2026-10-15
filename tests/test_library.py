import pytest

import db
import library
import models

ARTIST_ID = "f59c5520-5f46-4d2c-b2c4-822eabf53419"
ALBUM_ID = "6e335887-60ba-38f0-95af-fae7774336bf"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    yield
    db.close_db()


@pytest.fixture(autouse=True)
def no_background(monkeypatch):
    syncs = []
    monkeypatch.setattr(library, "_schedule_sync", lambda: syncs.append(1))
    monkeypatch.setattr(library, "_sleep", lambda s: None)
    library._pending.clear()
    yield syncs
    library._pending.clear()


OPTIONS = {
    "rootfolder": [
        {"path": "/music", "name": "Music", "defaultQualityProfileId": 2,
         "defaultMetadataProfileId": 1},
        {"path": "/other", "name": "Other"},
    ],
    "qualityprofile": [{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}],
    "metadataprofile": [
        {"id": 1, "name": "Standard"}, {"id": 5, "name": "None"},
    ],
}


def _artist(**extra):
    item = {
        "artistName": "Daft Punk",
        "foreignArtistId": ARTIST_ID,
        "artistType": "Group",
        "disambiguation": "French duo",
        "images": [{"coverType": "poster", "remoteUrl": "https://img/p.jpg"}],
        "id": 0,
    }
    item.update(extra)
    return item


def _album(**extra):
    item = {
        "title": "Discovery",
        "foreignAlbumId": ALBUM_ID,
        "releaseDate": "2001-03-12T00:00:00Z",
        "albumType": "Album",
        "remoteCover": "https://img/c.jpg",
        "releases": [{"trackCount": 14}, {"trackCount": 16}],
        "artist": _artist(),
        "id": 0,
    }
    item.update(extra)
    return item


class FakeLidarr:
    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    def __call__(self, endpoint, method="GET", data=None, params=None):
        self.calls.append((method, endpoint, data, params))
        value = self.routes.get((method, endpoint))
        if callable(value):
            return value(data, params)
        if value is None:
            return {"error": f"unexpected {method} {endpoint}"}
        return value

    def sent(self, method, endpoint):
        return [c[2] for c in self.calls if c[0] == method and c[1] == endpoint]


def _install(monkeypatch, routes):
    fake = FakeLidarr({**{("GET", k): v for k, v in OPTIONS.items()}, **routes})
    monkeypatch.setattr(library, "lidarr_request", fake)
    return fake


class TestSearch:
    def test_artist_results_are_summarised(self, monkeypatch):
        fake = _install(monkeypatch, {
            ("GET", "artist/lookup"): [_artist(), _artist(), _artist(foreignArtistId="")],
        })
        results = library.search("artist", "  daft   punk ")
        assert fake.calls[0][3] == {"term": "daft punk"}
        assert results == [{
            "foreignArtistId": ARTIST_ID, "name": "Daft Punk",
            "disambiguation": "French duo", "type": "Group", "overview": "",
            "genres": [], "image": "https://img/p.jpg", "id": None,
            "inLibrary": False, "monitored": False, "albumCount": None,
        }]

    def test_album_in_library_reports_missing_tracks(self, monkeypatch):
        _install(monkeypatch, {
            ("GET", "album/lookup"): [_album(
                id=7, monitored=True,
                statistics={"trackCount": 14, "trackFileCount": 10},
            )],
        })
        result = library.search("album", "discovery")[0]
        assert result["inLibrary"] is True
        assert result["missingTracks"] == 4
        assert result["trackCount"] == 14
        assert result["year"] == "2001"
        assert result["artistName"] == "Daft Punk"

    def test_new_album_uses_largest_release(self, monkeypatch):
        _install(monkeypatch, {("GET", "album/lookup"): [_album()]})
        assert library.search("album", "discovery")[0]["trackCount"] == 16

    def test_non_http_images_are_dropped(self, monkeypatch):
        _install(monkeypatch, {("GET", "artist/lookup"): [_artist(
            images=[{"coverType": "poster", "remoteUrl": "javascript:alert(1)"}],
        )]})
        assert library.search("artist", "daft")[0]["image"] == ""

    def test_short_term_is_rejected(self):
        with pytest.raises(library.LibraryError):
            library.search("artist", " a ")

    def test_unknown_kind_is_rejected(self):
        with pytest.raises(library.LibraryError):
            library.search("track", "song")

    def test_lidarr_error_is_a_bad_gateway(self, monkeypatch):
        _install(monkeypatch, {("GET", "artist/lookup"): {"error": "down"}})
        with pytest.raises(library.LibraryError) as exc:
            library.search("artist", "daft")
        assert exc.value.status == 502


class TestOptions:
    def test_none_metadata_profile_is_hidden(self, monkeypatch):
        _install(monkeypatch, {})
        options = library.get_add_options()
        assert [m["name"] for m in options["metadataProfiles"]] == ["Standard"]
        assert options["rootFolders"][0]["defaultQualityProfileId"] == 2

    def test_defaults_come_from_the_root_folder(self, monkeypatch):
        _install(monkeypatch, {})
        resolved = library.resolve_options({}, library.get_add_options())
        assert resolved == {
            "rootFolderPath": "/music", "qualityProfileId": 2,
            "metadataProfileId": 1,
        }

    @pytest.mark.parametrize("payload", [
        {"rootFolderPath": "/etc"},
        {"qualityProfileId": 99},
        {"metadataProfileId": 5},
    ])
    def test_unknown_choices_are_rejected(self, monkeypatch, payload):
        _install(monkeypatch, {})
        with pytest.raises(library.LibraryError):
            library.resolve_options(payload, library.get_add_options())

    def test_no_root_folder(self, monkeypatch):
        _install(monkeypatch, {("GET", "rootfolder"): []})
        with pytest.raises(library.LibraryError) as exc:
            library.resolve_options({}, library.get_add_options())
        assert exc.value.status == 409


class TestAddArtist:
    def test_posts_the_looked_up_artist_with_options(self, monkeypatch, no_background):
        fake = _install(monkeypatch, {
            ("GET", "artist/lookup"): [_artist()],
            ("POST", "artist"): {"id": 12, "artistName": "Daft Punk"},
        })
        result = library.add_artist({
            "foreignArtistId": ARTIST_ID.upper(), "monitor": "missing",
            "rootFolderPath": "/other", "qualityProfileId": 1,
        })
        assert result == {"id": 12, "name": "Daft Punk", "monitor": "missing"}
        lookup = [c for c in fake.calls if c[1] == "artist/lookup"][0]
        assert lookup[3] == {"term": f"lidarr:{ARTIST_ID}"}
        body = fake.sent("POST", "artist")[0]
        assert body["foreignArtistId"] == ARTIST_ID
        assert body["rootFolderPath"] == "/other"
        assert body["qualityProfileId"] == 1
        assert body["metadataProfileId"] == 1
        assert body["monitored"] is True
        assert body["addOptions"] == {
            "monitor": "missing", "searchForMissingAlbums": False,
        }
        assert "id" not in body
        assert no_background == [1]

    def test_existing_artist_is_a_conflict(self, monkeypatch):
        fake = _install(monkeypatch, {("GET", "artist/lookup"): [_artist(id=3)]})
        with pytest.raises(library.LibraryError) as exc:
            library.add_artist({"foreignArtistId": ARTIST_ID})
        assert exc.value.status == 409
        assert not fake.sent("POST", "artist")

    def test_invalid_mbid(self):
        with pytest.raises(library.LibraryError):
            library.add_artist({"foreignArtistId": "../../etc"})

    def test_invalid_monitor_option(self):
        with pytest.raises(library.LibraryError):
            library.add_artist({"foreignArtistId": ARTIST_ID, "monitor": "everything"})

    def test_not_found(self, monkeypatch):
        _install(monkeypatch, {("GET", "artist/lookup"): []})
        with pytest.raises(library.LibraryError) as exc:
            library.add_artist({"foreignArtistId": ARTIST_ID})
        assert exc.value.status == 404

    def test_lidarr_rejection_is_reported(self, monkeypatch):
        _install(monkeypatch, {
            ("GET", "artist/lookup"): [_artist()],
            ("POST", "artist"): {"error": "Lidarr rejected the request: Path exists"},
        })
        with pytest.raises(library.LibraryError) as exc:
            library.add_artist({"foreignArtistId": ARTIST_ID})
        assert "Path exists" in exc.value.message


class TestAddAlbum:
    def test_new_album_with_new_artist(self, monkeypatch):
        fake = _install(monkeypatch, {
            ("GET", "album/lookup"): [_album()],
            ("POST", "album"): {"id": 44},
        })
        result = library.add_album({"foreignAlbumId": ALBUM_ID})
        assert result == {
            "id": 44, "title": "Discovery", "artistName": "Daft Punk",
            "status": "added", "queued": None,
        }
        body = fake.sent("POST", "album")[0]
        assert body["monitored"] is True
        assert body["addOptions"] == {"searchForNewAlbum": False}
        assert body["artist"]["rootFolderPath"] == "/music"
        assert body["artist"]["qualityProfileId"] == 2
        assert body["artist"]["addOptions"]["monitor"] == "none"
        assert "id" not in body and "id" not in body["artist"]

    def test_new_album_of_existing_artist_keeps_the_artist(self, monkeypatch):
        fake = _install(monkeypatch, {
            ("GET", "album/lookup"): [_album(artist=_artist(id=9, rootFolderPath="/x"))],
            ("POST", "album"): {"id": 45},
        })
        library.add_album({"foreignAlbumId": ALBUM_ID})
        artist = fake.sent("POST", "album")[0]["artist"]
        assert artist["id"] == 9
        assert artist["rootFolderPath"] == "/x"
        assert not [c for c in fake.calls if c[1] == "rootfolder"]

    def test_unmonitored_album_in_library_gets_monitored(self, monkeypatch):
        fake = _install(monkeypatch, {
            ("GET", "album/lookup"): [_album(id=7, monitored=False)],
            ("PUT", "album/monitor"): [{"id": 7}],
        })
        result = library.add_album({"foreignAlbumId": ALBUM_ID})
        assert result["status"] == "monitored"
        assert fake.sent("PUT", "album/monitor") == [
            {"albumIds": [7], "monitored": True},
        ]
        assert not fake.sent("POST", "album")

    def test_monitored_album_in_library_is_left_alone(self, monkeypatch, no_background):
        fake = _install(monkeypatch, {
            ("GET", "album/lookup"): [_album(id=7, monitored=True)],
        })
        assert library.add_album({"foreignAlbumId": ALBUM_ID})["status"] == "existing"
        assert not fake.sent("PUT", "album/monitor")
        assert no_background == []

    def test_download_starts_the_queue_waiter(self, monkeypatch):
        started = []
        monkeypatch.setattr(
            library, "queue_when_ready",
            lambda album_id, title, artist: started.append(album_id) or "waiting",
        )
        _install(monkeypatch, {
            ("GET", "album/lookup"): [_album()],
            ("POST", "album"): {"id": 44},
        })
        result = library.add_album({"foreignAlbumId": ALBUM_ID, "download": True})
        assert result["queued"] == "waiting"
        assert started == [44]


class TestQueueWhenReady:
    def test_enqueues_once_tracks_exist(self, monkeypatch):
        answers = iter([[], [], [{"id": 1}]])
        _install(monkeypatch, {("GET", "track"): lambda d, p: next(answers)})
        library._set_pending(44, title="Discovery", state="waiting")
        assert library.wait_and_enqueue(44) is True
        queue = models.get_queue()
        assert [q["album_id"] for q in queue] == [44]
        assert library.pending_adds()[0]["state"] == "queued"

    def test_gives_up_after_the_deadline(self, monkeypatch):
        _install(monkeypatch, {("GET", "track"): []})
        assert library.wait_and_enqueue(44, deadline_seconds=0) is False
        assert models.get_queue() == []
        assert library.pending_adds()[0]["state"] == "timeout"

    def test_old_entries_expire(self, monkeypatch):
        library._set_pending(1, state="queued")
        library._pending[1]["updated"] -= library.PENDING_KEEP_SECONDS + 1
        assert library.pending_adds() == []
