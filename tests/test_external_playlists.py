import json
from unittest.mock import MagicMock

import pytest

import external_playlists as ep


def _resp(status=200, payload=None, text=""):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = payload
    r.text = text
    return r


def _embed_html(entity):
    data = {"props": {"pageProps": {"state": {"data": {"entity": entity}}}}}
    return (
        '<html><script id="__NEXT_DATA__" type="application/json">'
        + json.dumps(data) + "</script></html>"
    )


@pytest.fixture(autouse=True)
def _no_spotify_keys(monkeypatch):
    monkeypatch.setattr(ep, "load_config", lambda: {})
    ep._spotify_token.update(value="", expires=0.0, key="")


class TestDetect:
    @pytest.mark.parametrize("url, expected", [
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abc", ("spotify", "playlist", "37i9dQZF1DXcBWIGoYBM5M", "")),
        ("https://open.spotify.com/intl-it/album/4aawyAB9vmqN3uQ7FjRGTy", ("spotify", "album", "4aawyAB9vmqN3uQ7FjRGTy", "")),
        ("spotify:track:4uLU6hMCjMI75M1A2tKUQC", ("spotify", "track", "4uLU6hMCjMI75M1A2tKUQC", "")),
        ("https://www.deezer.com/it/playlist/908622995", ("deezer", "playlist", "908622995", "")),
        ("https://www.deezer.com/album/302127", ("deezer", "album", "302127", "")),
        ("https://music.apple.com/us/album/discovery/697194953", ("apple", "album", "697194953", "us")),
        ("https://music.apple.com/it/album/one-more-time/697194953?i=697195462", ("apple", "track", "697195462", "it")),
        ("https://music.apple.com/us/playlist/todays-hits/pl.f4d106fed2bd41149aaacabb233eb5eb", ("apple", "playlist", "pl.f4d106fed2bd41149aaacabb233eb5eb", "us")),
        ("https://listenbrainz.org/playlist/0b8a8a3e-6c5f-4a83-9f1e-2c1d3f9b7a10/", ("listenbrainz", "playlist", "0b8a8a3e-6c5f-4a83-9f1e-2c1d3f9b7a10", "")),
    ])
    def test_matches(self, url, expected):
        assert ep._match(url) == expected
        assert ep.is_external_url(url)

    @pytest.mark.parametrize("url", [
        "https://www.youtube.com/playlist?list=PL123",
        "https://open.spotify.com.evil.example/playlist/37i9dQZF1DXcBWIGoYBM5M",
        "https://www.deezer.com/en/artist/27",
        "http://localhost/playlist/1",
    ])
    def test_rejects(self, url):
        assert not ep.is_external_url(url)

    def test_short_link_detected(self):
        assert ep.is_external_url("https://link.deezer.com/s/30abc")

    def test_cover_hosts(self):
        assert ep.is_cover_url("https://i.scdn.co/image/ab67616d0000b273")
        assert ep.is_cover_url("https://e-cdns-images.dzcdn.net/images/cover/x/1000x1000.jpg")
        assert ep.is_cover_url("https://is1-ssl.mzstatic.com/image/thumb/a/600x600bb.jpg")
        assert not ep.is_cover_url("http://i.scdn.co/image/x")
        assert not ep.is_cover_url("https://evil.example/scdn.co.jpg")


class TestSpotifyEmbed:
    def test_playlist(self):
        page = _embed_html({
            "type": "playlist", "name": "Chill Mix", "subtitle": "Spotify",
            "coverArt": {"sources": [{"url": "https://i.scdn.co/image/small", "width": 64}, {"url": "https://i.scdn.co/image/big", "width": 640}]},
            "trackList": [
                {"title": "Song A", "subtitle": "Artist 1, Artist 2", "duration": 201000},
                {"title": "Song B", "subtitle": "Artist 3", "duration": 185500},
                {"title": "", "subtitle": "Nobody", "duration": 1},
            ],
        })
        result = ep.parse_spotify_embed(page, "playlist")
        assert result["title"] == "Chill Mix"
        assert result["thumbnail"] == "https://i.scdn.co/image/big"
        assert [(t["title"], t["artist"], t["duration"]) for t in result["tracks"]] == [
            ("Song A", "Artist 1, Artist 2", 201), ("Song B", "Artist 3", 186),
        ]
        assert result["tracks"][0]["album"] == ""

    def test_album_sets_album_name(self):
        page = _embed_html({
            "type": "album", "name": "Discovery", "subtitle": "Daft Punk",
            "trackList": [{"title": "One More Time", "subtitle": "Daft Punk", "duration": 320000}],
        })
        result = ep.parse_spotify_embed(page, "album")
        assert result["owner"] == "Daft Punk"
        assert result["tracks"][0]["album"] == "Discovery"

    def test_single_track(self):
        page = _embed_html({
            "type": "track", "name": "Around the World", "duration": 429000,
            "artists": [{"name": "Daft Punk"}],
        })
        result = ep.parse_spotify_embed(page, "track")
        assert result["tracks"] == [{
            "title": "Around the World", "artist": "Daft Punk", "album": "",
            "duration": 429, "thumbnail": "",
        }]

    def test_missing_data(self):
        with pytest.raises(ep.ExternalPlaylistError):
            ep.parse_spotify_embed("<html></html>", "playlist")

    def test_fetch_uses_embed_without_keys(self, monkeypatch):
        page = _embed_html({"name": "X", "trackList": [{"title": "T", "subtitle": "A", "duration": 1000}]})
        calls = []
        monkeypatch.setattr(ep, "_get", lambda url, **k: calls.append(url) or _resp(text=page))
        result = ep.fetch("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        assert calls == ["https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M"]
        assert result["service"] == "spotify"


class TestSpotifyApi:
    def test_playlist_pages(self, monkeypatch):
        monkeypatch.setattr(ep, "_spotify_api_token", lambda cfg: "tok")
        pages = {
            "https://api.spotify.com/v1/playlists/37i9dQZF1DXcBWIGoYBM5M?fields=name,owner(display_name),images":
                {"name": "Mine", "owner": {"display_name": "lucio"}, "images": [{"url": "https://i.scdn.co/image/p", "width": 300}]},
            "https://api.spotify.com/v1/playlists/37i9dQZF1DXcBWIGoYBM5M/tracks?limit=100":
                {"items": [
                    {"track": {"type": "track", "name": "S1", "duration_ms": 60000, "artists": [{"name": "A"}], "album": {"name": "Al"}}},
                    {"track": {"type": "episode", "name": "Pod"}},
                ], "next": "https://api.spotify.com/v1/next-page"},
            "https://api.spotify.com/v1/next-page":
                {"items": [{"track": {"type": "track", "name": "S2", "duration_ms": 1000, "artists": [{"name": "B"}, {"name": "C"}], "album": {"name": "Bl"}}}], "next": None},
        }
        monkeypatch.setattr(ep, "_json", lambda url, **k: pages[url])
        result = ep.fetch("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")
        assert result["owner"] == "lucio"
        assert [(t["title"], t["artist"], t["album"]) for t in result["tracks"]] == [
            ("S1", "A", "Al"), ("S2", "B, C", "Bl"),
        ]

    def test_api_failure_falls_back_to_embed(self, monkeypatch):
        monkeypatch.setattr(ep, "_spotify_api_token", lambda cfg: "tok")

        def refuse(url, **k):
            raise ep.ExternalPlaylistError("nope", 404)

        monkeypatch.setattr(ep, "_json", refuse)
        page = _embed_html({"name": "X", "trackList": [{"title": "T", "subtitle": "A", "duration": 1000}]})
        monkeypatch.setattr(ep, "_get", lambda url, **k: _resp(text=page))
        assert ep.fetch("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M")["title"] == "X"


class TestDeezer:
    def test_playlist_paginates(self, monkeypatch):
        pages = {
            "https://api.deezer.com/playlist/9": {"title": "Deez", "picture_xl": "https://e-cdns-images.dzcdn.net/p.jpg", "creator": {"name": "me"}},
            "https://api.deezer.com/playlist/9/tracks?limit=100": {
                "data": [{"title": "T1", "duration": 200, "artist": {"name": "A"}, "album": {"title": "Al", "cover_xl": "https://e-cdns-images.dzcdn.net/a.jpg"}}],
                "next": "https://api.deezer.com/playlist/9/tracks?limit=100&index=100",
            },
            "https://api.deezer.com/playlist/9/tracks?limit=100&index=100": {
                "data": [{"title": "T2", "duration": 100, "artist": {"name": "B"}, "album": {"title": "Bl"}, "readable": False},
                         {"title": "T3", "duration": 90, "artist": {"name": "C"}, "album": {"title": "Cl"}}],
            },
        }
        monkeypatch.setattr(ep, "_json", lambda url, **k: pages[url])
        result = ep.fetch("https://www.deezer.com/playlist/9")
        assert result["owner"] == "me"
        assert [t["title"] for t in result["tracks"]] == ["T1", "T3"]
        assert result["tracks"][0]["thumbnail"] == "https://e-cdns-images.dzcdn.net/a.jpg"

    def test_error_payload(self, monkeypatch):
        monkeypatch.setattr(ep, "_json", lambda url, **k: {"error": {"code": 800}})
        with pytest.raises(ep.ExternalPlaylistError) as e:
            ep.fetch("https://www.deezer.com/playlist/9")
        assert e.value.status == 404


class TestApple:
    def test_album_lookup_sorted(self, monkeypatch):
        payload = {"results": [
            {"wrapperType": "collection", "collectionName": "Discovery", "artistName": "Daft Punk", "artworkUrl100": "https://is1-ssl.mzstatic.com/image/thumb/x/100x100bb.jpg"},
            {"wrapperType": "track", "trackName": "Aerodynamic", "artistName": "Daft Punk", "collectionName": "Discovery", "trackTimeMillis": 212000, "discNumber": 1, "trackNumber": 2},
            {"wrapperType": "track", "trackName": "One More Time", "artistName": "Daft Punk", "collectionName": "Discovery", "trackTimeMillis": 320000, "discNumber": 1, "trackNumber": 1},
        ]}
        monkeypatch.setattr(ep, "_json", lambda url, **k: payload)
        result = ep.fetch("https://music.apple.com/us/album/discovery/697194953")
        assert [t["title"] for t in result["tracks"]] == ["One More Time", "Aerodynamic"]
        assert result["thumbnail"].endswith("/600x600bb.jpg")

    def test_playlist_page(self):
        data = [{"data": {"sections": [{"items": [
            {"title": "Song", "artistName": "Singer", "duration": 180000, "artwork": {"dictionary": {"url": "https://is1-ssl.mzstatic.com/a/{w}x{h}bb.{f}"}}},
            {"title": "Song", "artistName": "Singer", "duration": 180000},
            {"title": "Other", "artistName": "Band", "duration": 200000},
            {"title": "Header", "artistName": "No duration"},
        ]}]}}]
        page = (
            '<meta property="og:title" content="Today&#39;s Hits on Apple Music">'
            '<meta property="og:image" content="https://is1-ssl.mzstatic.com/p.jpg">'
            '<script type="application/json" id="serialized-server-data">' + json.dumps(data) + "</script>"
        )
        result = ep.parse_apple_playlist(page)
        assert result["title"] == "Today's Hits"
        assert [(t["title"], t["artist"]) for t in result["tracks"]] == [("Song", "Singer"), ("Other", "Band")]
        assert result["tracks"][0]["thumbnail"] == "https://is1-ssl.mzstatic.com/a/600x600bb.jpg"


class TestListenBrainz:
    def test_playlist(self, monkeypatch):
        payload = {"playlist": {"title": "Weekly Jams", "creator": "lucio", "track": [
            {"title": "T", "creator": "A", "album": "Al", "duration": 240000},
        ]}}
        monkeypatch.setattr(ep, "_json", lambda url, **k: payload)
        result = ep.fetch("https://listenbrainz.org/playlist/0b8a8a3e-6c5f-4a83-9f1e-2c1d3f9b7a10")
        assert result["title"] == "Weekly Jams"
        assert result["tracks"][0] == {"title": "T", "artist": "A", "album": "Al", "duration": 240, "thumbnail": ""}


class TestInfoResponse:
    def test_shape(self):
        result = {"service": "deezer", "kind": "album", "title": "Al", "owner": "A",
                  "thumbnail": "https://e-cdns-images.dzcdn.net/c.jpg",
                  "tracks": [{"title": "T", "artist": "A", "album": "Al", "duration": 3, "thumbnail": ""}]}
        data = ep.info_response(result)
        assert data["type"] == "album"
        assert data["source"] == "Deezer"
        assert data["entries"][0]["search"] is True
        assert data["entries"][0]["channel"] == "A"
        assert data["entries"][0]["thumbnail"] == result["thumbnail"]

    def test_empty_raises(self):
        with pytest.raises(ep.ExternalPlaylistError):
            ep._result("deezer", "playlist", "x", "", "", [None])


class TestLidarrAlbums:
    def test_unique_albums(self):
        albums = ep.unique_albums([
            {"artist": "A, B", "album": "X"},
            {"artist": "a", "album": "x"},
            {"artist": "C", "album": ""},
            "bad",
        ])
        assert albums == [{"artist": "A", "album": "X"}]

    def _candidate(self, **kw):
        base = {"foreignAlbumId": "11111111-1111-1111-1111-111111111111", "title": "Discovery",
                "artistName": "Daft Punk", "type": "Album", "inLibrary": False}
        base.update(kw)
        return base

    def test_exact_title_breaks_tie(self, monkeypatch):
        import library
        deluxe = self._candidate(foreignAlbumId="22222222-2222-2222-2222-222222222222", title="Discovery (Deluxe)")
        monkeypatch.setattr(library, "search", lambda kind, term: [self._candidate(), deluxe])
        decision = ep._match_lidarr_album("Daft Punk", "Discovery")
        assert decision["match"]["foreignAlbumId"] == "11111111-1111-1111-1111-111111111111"
        assert decision["status"] == "not_in_library"

    def test_job_adds_and_reports(self, monkeypatch):
        import library
        monkeypatch.setattr(library, "search", lambda kind, term: [self._candidate()] if "Discovery" in term else [])
        added = []
        monkeypatch.setattr(library, "add_album", lambda payload: added.append(payload) or {"status": "added"})

        class Now:
            def __init__(self, target, args, **k):
                self.target, self.args = target, args

            def start(self):
                self.target(*self.args)

        monkeypatch.setattr(ep.threading, "Thread", Now)
        status = ep.start_lidarr_job([
            {"artist": "Daft Punk", "album": "Discovery"},
            {"artist": "Nobody", "album": "Nothing"},
        ])
        assert status["total"] == 2
        final = ep.lidarr_job_status()
        assert final["running"] is False
        assert [a["state"] for a in final["albums"]] == ["queued", "not_found"]
        assert added == [{"foreignAlbumId": "11111111-1111-1111-1111-111111111111", "download": True}]

    def test_no_albums(self):
        with pytest.raises(ep.ExternalPlaylistError):
            ep.start_lidarr_job([{"artist": "A", "album": ""}])
