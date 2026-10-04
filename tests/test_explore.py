import json
import os

import pytest

import db
import explore
import library
import models

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "ytmusic")


def fx(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


class FakeYT:
    def __init__(self, **overrides):
        self.calls = []
        self.overrides = overrides

    def _serve(self, name, default):
        self.calls.append(name)
        value = self.overrides.get(name, default)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value()
        return value

    def get_home(self, limit=3):
        return self._serve("get_home", fx("get_home"))

    def get_explore(self):
        return self._serve("get_explore", fx("doc_get_explore"))

    def get_charts(self, country="ZZ"):
        return self._serve("get_charts", fx("get_charts_IT"))

    def get_mood_categories(self):
        return self._serve("get_mood_categories", fx("doc_get_mood_categories"))

    def get_mood_playlists(self, params):
        return self._serve("get_mood_playlists", fx("doc_get_mood_playlists"))

    def get_album(self, browse_id):
        return self._serve("get_album", fx("doc_get_album"))

    def get_artist(self, channel_id):
        return self._serve("get_artist", fx("doc_get_artist"))

    def get_playlist(self, playlist_id, limit=100):
        return self._serve("get_playlist", fx("get_playlist_chart"))

    def search(self, query, filter=None, limit=20):
        return self._serve(f"search:{filter}", fx("search_mixed") if filter is None else [])

    def get_search_suggestions(self, query):
        return self._serve("get_search_suggestions", fx("get_search_suggestions"))

    def _send_request(self, endpoint, body):
        name = {
            "FEmusic_explore": "browse_explore_raw",
            "FEmusic_home": "browse_explore_raw",
        }.get(body.get("browseId"))
        return self._serve(f"raw:{body.get('browseId')}", fx(name) if name else {})


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    explore.cache.invalidate()
    explore.reset_clients()
    monkeypatch.setattr(explore, "locale", lambda: ("en", "IT"))
    yield
    explore.cache.invalidate()
    db.close_db()


@pytest.fixture()
def yt(monkeypatch):
    fake = FakeYT()
    client = explore._Client(fake)
    monkeypatch.setattr(explore, "_client", lambda: client)
    return fake


class TestThumbnails:
    def test_picks_largest_within_limit(self):
        thumbs = fx("doc_get_album")["thumbnails"]
        assert explore.best_thumbnail_url(thumbs).endswith("=w544-h544-l90-rj")

    def test_upscales_small_googleusercontent_art(self):
        url = explore.best_thumbnail_url(fx("get_charts_IT")["artists"][0]["thumbnails"])
        assert "=w544-h544" in url

    def test_size_s_form_is_resized(self):
        url = explore.best_thumbnail_url(fx("get_charts_IT")["videos"][0]["thumbnails"])
        assert url.endswith("=s544")

    def test_ytimg_keeps_the_file_within_limit(self):
        url = explore.best_thumbnail_url([
            {"url": "https://i.ytimg.com/vi/x/hq720.jpg", "width": 800, "height": 450},
            {"url": "https://i.ytimg.com/vi/x/hqdefault.jpg", "width": 400, "height": 225},
        ])
        assert url.endswith("hqdefault.jpg")

    def test_foreign_hosts_are_dropped(self):
        assert explore.best_thumbnail_url([{"url": "https://evil.example/x.jpg"}]) == ""
        assert explore.proxied("http://i.ytimg.com/x.jpg") == ""

    def test_images_always_go_through_the_proxy(self):
        item = explore.normalize_item(fx("doc_get_explore")["new_releases"][0])
        assert item["thumbnail"].startswith("/api/thumbnail?url=https%3A%2F%2F")


class TestNormalizeItems:
    def test_real_home_drops_podcasts(self):
        for row in fx("get_home"):
            assert explore.normalize_items(row["contents"]) == []

    def test_search_mixed_kinds_and_shapes(self):
        items = explore.normalize_items(fx("search_mixed"), limit=100)
        kinds = {i["kind"] for i in items}
        assert kinds <= {"artist", "video", "playlist"}
        assert "podcast" not in kinds and "episode" not in kinds
        top = items[0]
        assert top["kind"] == "artist" and top["title"] == "Nirvana"
        assert top["id"] == "UCrPe3hLA51968GwxHSZ1llw"
        playlists = [i for i in items if i["kind"] == "playlist"]
        assert playlists and not playlists[0]["id"].startswith("VL")
        for item in items:
            assert set(item) >= {
                "kind", "id", "title", "subtitle", "artists", "year", "type",
                "thumbnail", "explicit", "duration",
            }

    def test_doc_explore_shapes(self):
        feed = explore._explore_from_parsed(fx("doc_get_explore"))
        assert [a["title"] for a in feed["new_releases"]] == ["Hangang", "Midnight Tapes"]
        assert feed["new_releases"][1]["type"] == "EP"
        assert feed["new_releases"][1]["explicit"] is True
        assert feed["new_releases"][0]["playlistId"].startswith("OLAK5uy_")
        assert [t["title"] for t in feed["trending"]] == ["Permission to Dance"]
        assert feed["top_songs"][0]["kind"] == "song"
        assert feed["top_songs"][0]["album"]["id"] == "MPREb_fX4Yv8frUNv"
        assert feed["moods"] == [{"title": "Chill", "params": "ggMPOg1uXzVuc0dnZlhpV3Ba"}]
        assert feed["trending_playlist"] == "OLAK5uy_kNWGJvgWVqlt5LsFDL9Sdluly4M8TvGkM"

    def test_garbage_year_is_ignored(self):
        item = explore.normalize_item({"title": "Dragon", "year": "Two Steps From Hell", "browseId": "MPREb_M9aDqLRbSeg"})
        assert item["kind"] == "album" and item["year"] == ""

    def test_invalid_ids_are_rejected(self):
        assert explore.normalize_item({"title": "x", "browseId": "MPREb_"}) is None
        assert explore.normalize_item({"title": "x", "videoId": "short"}) is None
        assert explore.normalize_item({"title": "x", "videoId": "aaaaaaaaaaa", "videoType": "MUSIC_VIDEO_TYPE_PODCAST_EPISODE"}) is None
        assert explore.normalize_item("nope") is None

    def test_duplicates_collapse(self):
        raw = {"title": "Song", "videoId": "aaaaaaaaaaa", "videoType": "MUSIC_VIDEO_TYPE_ATV"}
        assert len(explore.normalize_items([raw, dict(raw)])) == 1


class TestRawParser:
    def test_real_explore_page_yields_trending_songs(self):
        feed = explore._explore_from_raw(fx("browse_explore_raw"))
        trending = feed["trending"]
        assert trending, "the live trending shelf must survive the ytmusicapi KeyError"
        first = trending[0]
        assert first["kind"] == "song"
        assert first["title"] == "Patient Zero"
        assert first["artists"][0]["name"] == "Taylor Swift"
        assert first["album"]["name"] == "The Life of a Showgirl: The Encore"
        assert first["rank"] == "1"

    def test_real_charts_page_yields_playlists_and_artists(self):
        shelves = explore._shelves_from_raw(fx("browse_charts_raw"))
        kinds = [{i["kind"] for i in s["items"]} for s in shelves]
        assert {"playlist"} in kinds and {"artist"} in kinds

    def test_new_releases_page_parses_grid(self):
        shelves = explore._shelves_from_raw(fx("browse_new_releases_raw"))
        assert any(i["kind"] == "playlist" for s in shelves for i in s["items"])

    def test_empty_or_malformed_pages(self):
        assert explore.raw_sections({}) == []
        assert explore.raw_sections({"contents": {"x": 1}}) == []
        assert explore._shelves_from_raw({"contents": None}) == []

    def test_mood_buttons(self):
        page = {"contents": {"singleColumnBrowseResultsRenderer": {"tabs": [{"tabRenderer": {"content": {"sectionListRenderer": {"contents": [
            {"gridRenderer": {"items": [
                {"musicNavigationButtonRenderer": {"buttonText": {"runs": [{"text": "Chill"}]}, "clickCommand": {"browseEndpoint": {"browseId": "FEmusic_moods_and_genres_category", "params": "ggMPOg1uXzVuc0dnZlhpV3Ba"}}}},
                {"musicNavigationButtonRenderer": {"buttonText": {"runs": [{"text": "Charts"}]}, "clickCommand": {"browseEndpoint": {"browseId": "FEmusic_charts"}}}},
            ]}}]}}}}]}}}
        feed = explore._explore_from_raw(page)
        assert feed["moods"] == [{"title": "Chill", "params": "ggMPOg1uXzVuc0dnZlhpV3Ba"}]


class TestPages:
    def test_album_page(self, yt):
        page = explore.album("MPREb_abcdef")
        assert page["title"] == "Revival"
        assert page["playlistId"] == "OLAK5uy_nMr9h2VlS-2PULNz3M3XVXQj_P3C2bqaY"
        assert [t["available"] for t in page["tracks"]] == [True, True, False]
        assert page["tracks"][0]["duration"] == 303
        assert page["coverUrl"].endswith("=w1200-h1200-l90-rj")
        assert page["otherVersions"][0]["id"] == "MPREb_fefKFOTEZSp"

    def test_artist_page(self, yt):
        page = explore.artist("UCUDVBtnOQi4c7E8jebpjc9Q")
        assert page["name"] == "Oasis"
        assert [a["title"] for a in page["albums"]][0] == "Familiar To Millions"
        assert page["singles"][0]["type"] == "Single"
        assert {r["title"] for r in page["related"]} == {"The Verve", "Liam Gallagher"}
        assert page["songs"][0]["id"] == "ZrOKjDZOtkA"

    def test_real_artist_without_albums(self, yt):
        yt.overrides["get_artist"] = fx("get_artist_videos_only")
        page = explore.artist("UCrPe3hLA51968GwxHSZ1llw")
        assert page["albums"] == [] and page["singles"] == []
        assert page["songs"] == []
        assert len(page["videos"]) == 2

    def test_playlist_page_marks_unavailable_tracks(self, yt):
        page = explore.playlist("VLPL4fGSI1pDJn5BPviUFX4a3IMnAgyknC68")
        assert page["id"] == "PL4fGSI1pDJn5BPviUFX4a3IMnAgyknC68"
        assert page["tracks"][1]["available"] is False
        assert page["tracks"][1]["id"] == ""
        assert [t["trackNumber"] for t in page["tracks"]] == [1, 2, 3, 4]

    def test_failed_album_raises_explore_error(self, yt):
        yt.overrides["get_album"] = KeyError("Unable to find 'contents'")
        with pytest.raises(explore.ExploreError) as exc:
            explore.album("MPREb_abcdef")
        assert exc.value.status == 502

    @pytest.mark.parametrize("fn,bad", [
        (explore.album, "UC123"), (explore.album, "MPREb_../x"),
        (explore.artist, "MPREb_abcdef"), (explore.playlist, "javascript:1"),
        (explore.mood_playlists, "<script>"),
    ])
    def test_ids_are_validated(self, yt, fn, bad):
        with pytest.raises(explore.ExploreError) as exc:
            fn(bad)
        assert exc.value.status == 400
        assert yt.calls == []


class TestFeeds:
    def test_home_combines_sections(self, yt):
        data = explore.home()
        keys = [s["key"] for s in data["sections"]]
        assert keys[0] == "hero"
        assert "top_songs" in keys and "trending" in keys and "top_artists" in keys
        top = next(s for s in data["sections"] if s["key"] == "top_songs")
        assert top["country"] == "IT" and "ZZ" in top["countries"]
        assert top["items"][0]["title"] == "PER NOI (con Achille Lauro)"
        assert data["moods"][0]["title"] == "For you"

    def test_home_degrades_when_explore_breaks(self, yt):
        yt.overrides["get_explore"] = KeyError("playNavigationEndpoint")
        yt.overrides["get_mood_categories"] = RuntimeError("HTTP 404")
        data = explore.home()
        keys = [s["key"] for s in data["sections"]]
        assert "trending" in keys
        assert "raw:FEmusic_explore" in yt.calls
        assert data["moods"] == []

    def test_home_is_empty_when_youtube_is_down(self, yt):
        for name in ("get_home", "get_explore", "get_charts", "get_mood_categories"):
            yt.overrides[name] = ConnectionError("down")
        yt._send_request = lambda *a: (_ for _ in ()).throw(ConnectionError("down"))
        data = explore.home()
        assert data["sections"] == [] and data["moods"] == []

    def test_charts_fall_back_to_chart_playlist(self, yt):
        data = explore.charts("it")
        assert data["country"] == "IT"
        assert data["chartPlaylist"] == "OLAK5uy_lOjIgcbrxv7bPplDRWt5bu9jLuye6bA8A"
        assert all(s["available"] for s in data["songs"])
        assert data["artists"][0]["rank"] == "1"

    def test_charts_country_validated(self, yt):
        with pytest.raises(explore.ExploreError):
            explore.charts("ITA")

    def test_search_groups(self, yt):
        data = explore.search("  nirvana  ")
        assert data["query"] == "nirvana"
        assert data["top"]["title"] == "Nirvana"
        assert data["results"]["artists"] and data["results"]["playlists"]
        assert {"search:None", "search:albums", "search:artists", "search:songs"} <= set(yt.calls)

    def test_search_requires_two_characters(self, yt):
        with pytest.raises(explore.ExploreError):
            explore.search("a")

    def test_suggestions(self, yt):
        assert explore.suggestions("verdi")[0] == "ben elimi sana verdim"


class TestCache:
    def test_ttl_expiry(self):
        now = [100.0]
        c = explore.TTLCache(clock=lambda: now[0])
        c.set("k", 1, 10)
        assert c.get("k") == 1
        now[0] = 110.0
        assert c.get("k") is None

    def test_eviction_keeps_size_bounded(self):
        now = [0.0]
        c = explore.TTLCache(max_items=3, clock=lambda: now[0])
        for i in range(5):
            now[0] += 1
            c.set(("k", i), i, 100)
        assert len(c) == 3
        assert c.get(("k", 4)) == 4 and c.get(("k", 0)) is None

    def test_invalidate_prefix(self):
        c = explore.TTLCache()
        c.set(("match", 1), 1, 100)
        c.set(("album", 1), 2, 100)
        assert c.invalidate("match") == 1
        assert c.get(("album", 1)) == 2

    def test_pages_are_cached(self, yt):
        explore.album("MPREb_abcdef")
        explore.album("MPREb_abcdef")
        assert yt.calls.count("get_album") == 1
        explore.invalidate()
        explore.album("MPREb_abcdef")
        assert yt.calls.count("get_album") == 2

    def test_failures_are_negatively_cached_briefly(self, yt, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(explore.cache, "clock", lambda: now[0])
        yt.overrides["get_album"] = ConnectionError("down")
        for _ in range(3):
            with pytest.raises(explore.ExploreError):
                explore.album("MPREb_abcdef")
        assert yt.calls.count("get_album") == 1
        now[0] += explore.FAILURE_TTL + 1
        yt.overrides.pop("get_album")
        assert explore.album("MPREb_abcdef")["title"] == "Revival"

    def test_locale_is_part_of_the_key(self, yt, monkeypatch):
        explore.album("MPREb_abcdef")
        monkeypatch.setattr(explore, "locale", lambda: ("it", "IT"))
        explore.album("MPREb_abcdef")
        assert yt.calls.count("get_album") == 2


def cand(title, artist, year="", tracks=0, mbid="0" * 8, kind="Album", **extra):
    base = {
        "foreignAlbumId": f"{mbid}-0000-0000-0000-000000000000"[:36],
        "title": title, "artistName": artist, "year": year,
        "trackCount": tracks, "type": kind, "inLibrary": False,
        "monitored": False, "missingTracks": None,
    }
    base.update(extra)
    return base


def ytm(title, artists, year="", tracks=0, kind="Album"):
    return {"title": title, "artists": [{"name": a} for a in artists],
            "year": year, "trackCount": tracks, "type": kind}


class TestScoring:
    def test_norm_title_strips_editions(self):
        assert explore.norm_title("Nevermind (Remastered)") == "nevermind"
        assert explore.norm_title("Nevermind (30th Anniversary Super Deluxe)") == "nevermind"
        assert explore.norm_title("Abbey Road (2019 Mix)") == "abbey road"
        assert explore.norm_title("Rumours - 2004 Remaster") == "rumours"
        assert explore.norm_title("Kid A (Live)") == "kid a live"
        assert explore.norm_title("Songs (For Lovers)") == "songs for lovers"

    def test_remaster_matches_original_release_group(self):
        decision = explore.decide_album(
            ytm("Nevermind (Remastered)", ["Nirvana"], "2011", 13),
            [cand("Nevermind", "Nirvana", "1991", 13, mbid="1b022e01"),
             cand("Bleach", "Nirvana", "1989", 11, mbid="2b022e01")],
        )
        assert decision["match"]["title"] == "Nevermind"
        assert decision["status"] == explore.STATUS_NOT_IN_LIBRARY

    def test_deluxe_with_extra_tracks(self):
        decision = explore.decide_album(
            ytm("Lover (Deluxe)", ["Taylor Swift"], "2019", 22),
            [cand("Lover", "Taylor Swift", "2019", 18)],
        )
        assert decision["match"]["title"] == "Lover"

    def test_parenthesised_title_is_not_an_edition(self):
        decision = explore.decide_album(
            ytm("(What's the Story) Morning Glory?", ["Oasis"], "1995", 12),
            [cand("(What's the Story) Morning Glory?", "Oasis", "1995", 12, mbid="3b022e01"),
             cand("Definitely Maybe", "Oasis", "1994", 11, mbid="4b022e01")],
        )
        assert decision["match"]["title"] == "(What's the Story) Morning Glory?"

    def test_compilation_various_artists(self):
        decision = explore.decide_album(
            ytm("Guardians of the Galaxy: Awesome Mix Vol. 1", ["Various Artists"], "2014", 12),
            [cand("Guardians of the Galaxy: Awesome Mix Vol. 1", "Various Artists", "2014", 12)],
        )
        assert decision["match"] is not None
        assert explore.artist_similarity(["Various Artists"], "Blue Swede") == 0.0
        assert explore.artist_similarity(["Artisti Vari"], "Various Artists") == 1.0

    def test_compilation_terms_skip_various_artists(self):
        assert explore._album_lookup_terms(
            ytm("Now 100 (Deluxe Edition)", ["Various Artists"]),
        ) == ["Now 100"]

    def test_wrong_artist_is_rejected(self):
        decision = explore.decide_album(
            ytm("Greatest Hits", ["Queen"], "1981", 17),
            [cand("Greatest Hits", "ABBA", "1975", 14)],
        )
        assert decision["status"] == explore.STATUS_NOT_ON_MB

    def test_ambiguous_homonym_albums(self):
        decision = explore.decide_album(
            ytm("Weezer", ["Weezer"]),
            [cand("Weezer", "Weezer", "1994", mbid="5b022e01"),
             cand("Weezer", "Weezer", "2001", mbid="6b022e01")],
        )
        assert decision["status"] == explore.STATUS_AMBIGUOUS
        assert len(decision["candidates"]) == 2

    def test_year_and_track_count_break_homonym_ties(self):
        decision = explore.decide_album(
            ytm("Weezer", ["Weezer"], "2001", 10),
            [cand("Weezer", "Weezer", "1994", 10, mbid="5b022e01"),
             cand("Weezer", "Weezer", "2001", 10, mbid="6b022e01")],
        )
        assert decision["status"] == explore.STATUS_AMBIGUOUS
        assert decision["candidates"][0]["year"] == "2001"

    def test_single_vs_album_type_penalty(self):
        a = explore.score_album_candidate(
            ytm("Blinding Lights", ["The Weeknd"], "2019", 1, "Single"),
            cand("Blinding Lights", "The Weeknd", "2019", 1, kind="Single"),
        )
        b = explore.score_album_candidate(
            ytm("Blinding Lights", ["The Weeknd"], "2019", 1, "Single"),
            cand("Blinding Lights", "The Weeknd", "2019", 1, kind="Album"),
        )
        assert a > b

    def test_featured_artists_on_the_youtube_side(self):
        score = explore.score_album_candidate(
            ytm("Savage Mode II", ["21 Savage", "Metro Boomin"], "2020", 15),
            cand("Savage Mode II", "21 Savage & Metro Boomin", "2020", 15),
        )
        assert score >= explore.MATCH_ACCEPT

    def test_no_candidates(self):
        assert explore.decide_album(ytm("x", ["y"]), [])["status"] == explore.STATUS_NOT_ON_MB

    @pytest.mark.parametrize("extra,status", [
        ({"inLibrary": False}, explore.STATUS_NOT_IN_LIBRARY),
        ({"inLibrary": True, "monitored": False}, explore.STATUS_UNMONITORED),
        ({"inLibrary": True, "monitored": True, "missingTracks": 3}, explore.STATUS_MISSING),
        ({"inLibrary": True, "monitored": True, "missingTracks": 0}, explore.STATUS_COMPLETE),
        ({"inLibrary": True, "monitored": True, "missingTracks": None}, explore.STATUS_MISSING),
    ])
    def test_album_status(self, extra, status):
        assert explore.album_status(cand("A", "B", **extra)) == status

    def test_artist_decisions(self):
        one = explore.decide_artist("Oasis", [{"name": "Oasis", "foreignArtistId": "a"}, {"name": "Oasis Tribute", "foreignArtistId": "b"}])
        assert one["match"]["foreignArtistId"] == "a"
        assert one["status"] == explore.STATUS_NOT_IN_LIBRARY
        two = explore.decide_artist("Nirvana", [{"name": "Nirvana", "foreignArtistId": "a", "disambiguation": "US grunge"}, {"name": "Nirvana", "foreignArtistId": "b", "disambiguation": "UK 60s"}])
        assert two["status"] == explore.STATUS_AMBIGUOUS
        lib = explore.decide_artist("Nirvana", [{"name": "Nirvana", "foreignArtistId": "a", "inLibrary": True}, {"name": "Nirvana", "foreignArtistId": "b"}])
        assert lib["status"] == "in_library" and lib["match"]["foreignArtistId"] == "a"
        none = explore.decide_artist("Zzz", [{"name": "Other", "foreignArtistId": "c"}])
        assert none["status"] == explore.STATUS_NOT_ON_MB


MBID = "1b022e01-4da6-387b-8658-8678046e4cef"


class TestMatchAndAdd:
    def _lidarr(self, monkeypatch, results, calls=None):
        def fake_search(kind, term):
            if calls is not None:
                calls.append((kind, term))
            return results(kind, term) if callable(results) else results
        monkeypatch.setattr(library, "search", fake_search)

    def test_match_album_uses_artist_then_title(self, yt, monkeypatch):
        calls = []
        self._lidarr(monkeypatch, lambda k, t: [] if "Eminem" in t else [
            cand("Revival", "Eminem", "2017", 19, mbid=MBID[:8]),
        ], calls)
        result = explore.match("album", "MPREb_abcdef")
        assert [t for _, t in calls] == ["Eminem Revival", "Revival"]
        assert result["status"] == explore.STATUS_NOT_IN_LIBRARY
        assert result["playlistId"].startswith("OLAK5uy_")

    def test_match_album_is_cached_until_refresh(self, yt, monkeypatch):
        calls = []
        self._lidarr(monkeypatch, [cand("Revival", "Eminem", "2017")], calls)
        explore.match("album", "MPREb_abcdef")
        explore.match("album", "MPREb_abcdef")
        assert len(calls) == 1
        explore.match("album", "MPREb_abcdef", refresh=True)
        assert len(calls) == 2

    def test_match_kind_is_validated(self, yt):
        with pytest.raises(explore.ExploreError):
            explore.match("song", "aaaaaaaaaaa")

    def test_lidarr_errors_keep_their_status(self, yt, monkeypatch):
        def boom(kind, term):
            raise library.LibraryError("Lidarr search failed: down", 502)
        monkeypatch.setattr(library, "search", boom)
        with pytest.raises(explore.ExploreError) as exc:
            explore.match("album", "MPREb_abcdef")
        assert exc.value.status == 502

    def test_add_album_remembers_the_playlist_before_queueing(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [cand("Revival", "Eminem", "2017", 3, foreignAlbumId=MBID)])
        added = {}

        def fake_add(body):
            added.update(body)
            return {"id": 55, "title": "Revival", "artistName": "Eminem", "status": "added", "queued": None}

        order = []
        monkeypatch.setattr(library, "add_album", fake_add)
        monkeypatch.setattr(
            library, "queue_when_ready",
            lambda album_id, title, artist: order.append(models.get_album_source_hint(album_id)) or "waiting",
        )
        result = explore.add({"kind": "album", "id": "MPREb_abcdef", "rootFolderPath": "/music"})
        assert added == {"foreignAlbumId": MBID, "download": False, "rootFolderPath": "/music"}
        assert result["queued"] == "waiting" and result["sourceHint"] is True
        assert order[0]["playlist_id"] == "OLAK5uy_nMr9h2VlS-2PULNz3M3XVXQj_P3C2bqaY"
        assert order[0]["browse_id"] == "MPREb_abcdef"

    def test_add_album_without_download(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [cand("Revival", "Eminem", "2017", 3, foreignAlbumId=MBID)])
        monkeypatch.setattr(library, "add_album", lambda body: {"id": 9, "title": "Revival", "artistName": "Eminem", "status": "added", "queued": None})
        monkeypatch.setattr(library, "queue_when_ready", lambda *a: pytest.fail("must not queue"))
        result = explore.add({"kind": "album", "id": "MPREb_abcdef", "download": False})
        assert result["queued"] is None

    def test_add_ambiguous_album_requires_a_choice(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [
            cand("Revival", "Eminem", mbid="5b022e01"),
            cand("Revival", "Eminem", mbid="6b022e01"),
        ])
        with pytest.raises(explore.ExploreError) as exc:
            explore.add({"kind": "album", "id": "MPREb_abcdef"})
        assert exc.value.status == 409
        monkeypatch.setattr(library, "add_album", lambda body: {"id": 3, "title": "", "artistName": "", "status": "added", "foreignAlbumId": body["foreignAlbumId"]})
        monkeypatch.setattr(library, "queue_when_ready", lambda *a: "waiting")
        result = explore.add({"kind": "album", "id": "MPREb_abcdef", "foreignAlbumId": MBID.upper()})
        assert result["foreignAlbumId"] == MBID

    def test_add_rejects_bad_mbid(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [])
        with pytest.raises(explore.ExploreError):
            explore.add({"kind": "album", "id": "MPREb_abcdef", "foreignAlbumId": "1; drop"})

    def test_add_album_not_on_musicbrainz(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [])
        with pytest.raises(explore.ExploreError) as exc:
            explore.add({"kind": "album", "id": "MPREb_abcdef"})
        assert exc.value.status == 409
        assert "import" in exc.value.message.lower()

    def test_add_artist(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [{"name": "Oasis", "foreignArtistId": MBID}])
        sent = {}
        monkeypatch.setattr(library, "add_artist", lambda body: sent.update(body) or {"id": 4, "name": "Oasis", "monitor": "all"})
        result = explore.add({"kind": "artist", "id": "UCUDVBtnOQi4c7E8jebpjc9Q", "monitor": "future", "evil": 1})
        assert result["name"] == "Oasis"
        assert sent == {"foreignArtistId": MBID, "monitor": "future"}

    def test_add_library_errors_keep_status(self, yt, monkeypatch):
        self._lidarr(monkeypatch, [{"name": "Oasis", "foreignArtistId": MBID}])

        def boom(body):
            raise library.LibraryError("Oasis is already in your library.", 409)

        monkeypatch.setattr(library, "add_artist", boom)
        with pytest.raises(explore.ExploreError) as exc:
            explore.add({"kind": "artist", "id": "UCUDVBtnOQi4c7E8jebpjc9Q"})
        assert exc.value.status == 409


class TestImportPlan:
    def test_album_plan(self, yt):
        plan = explore.import_plan({"kind": "album", "id": "MPREb_abcdef"})
        assert plan["artist_name"] == "Eminem"
        assert plan["album_title"] == "Revival"
        assert [e["url"] for e in plan["entries"]] == [
            "https://music.youtube.com/watch?v=iKLU7z_xdYQ",
            "https://music.youtube.com/watch?v=Mv6JpBpsh6k",
        ]
        assert plan["thumbnail_url"].startswith("https://lh3.googleusercontent.com/")
        assert plan["source_url"].endswith("OLAK5uy_nMr9h2VlS-2PULNz3M3XVXQj_P3C2bqaY")

    def test_playlist_selection(self, yt):
        plan = explore.import_plan({"kind": "playlist", "id": "PL4fGSI1pDJn5BPviUFX4a3IMnAgyknC68", "videoIds": ["XgTSQwZcHH8"]})
        assert len(plan["entries"]) == 1
        assert plan["album_title"] == "I 100 video musicali più visti in Italia"

    @pytest.mark.parametrize("payload", [
        {"kind": "album", "id": "MPREb_abcdef", "videoIds": "x"},
        {"kind": "album", "id": "MPREb_abcdef", "videoIds": ["bad"]},
        {"kind": "album", "id": "MPREb_abcdef", "videoIds": []},
        {"kind": "song", "id": "aaaaaaaaaaa"},
    ])
    def test_invalid_plans(self, yt, payload):
        with pytest.raises(explore.ExploreError):
            explore.import_plan(payload)
