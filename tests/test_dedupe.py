"""Tests for dedupe: never fetch the same recording twice."""

import os

import pytest
from mutagen.id3 import ID3, TIT2, UFID

import db
import dedupe
import models
import processing

REC = "11111111-1111-1111-1111-111111111111"
OTHER = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def temp_db(tmp_path, monkeypatch):
    monkeypatch.setattr("db.DB_PATH", str(tmp_path / "test.db"))
    db.init_db()
    yield
    db.close_db()


def make_mp3(path, recording_id=None, frames=100):
    """A tiny valid MP3 (MPEG frames), ~2.6 s for 100 frames."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write((b"\xff\xfb\x90\x00" + b"\x00" * 413) * frames)
    if recording_id:
        tags = ID3()
        tags.add(UFID(owner="http://musicbrainz.org",
                      data=recording_id.encode()))
        tags.add(TIT2(encoding=3, text="old title"))
        tags.save(path)
    return path


def trk(title, number=1, rec=REC, medium=1, abs_number=None, **extra):
    t = {
        "id": hash((title, number, medium)) & 0xFFFFF,
        "title": title, "trackNumber": number, "mediumNumber": medium,
        "absoluteTrackNumber": abs_number or number,
        "foreignRecordingId": rec, "duration": 2600, "hasFile": False,
    }
    t.update(extra)
    return t


def store(album_id, title, rec=REC, *, verified=True, library="", number=1,
          album_title="Single", success=True, album_path=""):
    return models.add_track_download(
        album_id=album_id, album_title=album_title, artist_name="Artist",
        track_title=title, track_number=number, success=success,
        error_message="", youtube_url="https://music.youtube.com/watch?v="
        "abcdefghijk", youtube_title="yt", match_score=0.9,
        duration_seconds=3, album_path=album_path, lidarr_album_path=library,
        cover_url="", acoustid_recording_id=rec if verified else "",
        acoustid_score=0.99 if verified else 0.0, recording_id=rec,
    )


CFG = {"dedupe_mode": "reuse", "dedupe_same_album": "skip",
       "dedupe_cross_album": True}


def plan(tracks, pending=None, cfg=None, tmp=None, **kw):
    return dedupe.plan_album(
        7, tracks, tracks if pending is None else pending,
        dict(CFG, **(cfg or {})), library_root=str(tmp) if tmp else "",
        download_dir="/downloads-not-used", **kw,
    )


class TestKeys:
    def test_recording_key_normalises(self):
        assert dedupe.recording_key({"foreignRecordingId": f" {REC.upper()} ",
                                     "title": "x"}) == REC

    def test_no_id_means_no_key(self):
        assert dedupe.recording_key({"title": "x"}) == ""
        assert dedupe.recording_key({"foreignRecordingId": None}) == ""

    @pytest.mark.parametrize("title", ["[silence]", "[Data Track]", "[untitled]"])
    def test_special_purpose_recordings_are_never_matched(self, title):
        assert dedupe.recording_key(
            {"foreignRecordingId": REC, "title": title}) == ""

    def test_track_order_is_medium_then_position(self):
        ordered = sorted(
            [trk("c", "A2", medium=2, abs_number=2),
             trk("b", 5, medium=1), trk("a", "A1", medium=1, abs_number=1)],
            key=dedupe.track_order)
        assert [t["title"] for t in ordered] == ["a", "b", "c"]

    def test_norm_name_ignores_accents_case_and_punctuation(self):
        assert dedupe.norm_name("Señorita (Live)!") == dedupe.norm_name(
            "senorita live")


class TestFileMatches:
    def test_tag_with_same_recording_matches(self, tmp_path):
        p = make_mp3(str(tmp_path / "a.mp3"), REC)
        assert dedupe.file_matches(p, REC)

    def test_tag_with_other_recording_is_rejected(self, tmp_path):
        p = make_mp3(str(tmp_path / "a.mp3"), OTHER)
        assert not dedupe.file_matches(p, REC, expected_ms=2600)

    def test_untagged_file_needs_a_matching_duration(self, tmp_path):
        p = make_mp3(str(tmp_path / "a.mp3"))
        assert dedupe.file_matches(p, REC, expected_ms=2600)
        assert not dedupe.file_matches(p, REC, expected_ms=60000)
        assert not dedupe.file_matches(p, REC, expected_ms=0)

    def test_missing_or_empty_file(self, tmp_path):
        assert not dedupe.file_matches(str(tmp_path / "nope.mp3"), REC)
        empty = tmp_path / "e.mp3"
        empty.write_bytes(b"")
        assert not dedupe.file_matches(str(empty), REC)


class TestFindSourceFile:
    def src(self, folder, **kw):
        row = {"lidarr_album_path": str(folder), "album_path": "",
               "track_title": "Song", "track_number": 3}
        row.update(kw)
        return row

    def find(self, src, tmp_path, library=True, **kw):
        return dedupe.find_source_file(
            src, REC,
            library_root=str(tmp_path / "lib") if library else "",
            download_dir=str(tmp_path / "dl"), audio_ext="mp3",
            expected_ms=2600, **kw)

    def test_finds_the_app_named_file(self, tmp_path):
        p = make_mp3(str(tmp_path / "lib/A/Alb/03 - Song.mp3"), REC)
        assert self.find(self.src(tmp_path / "lib/A/Alb"), tmp_path) == p

    def test_finds_a_file_lidarr_renamed_into_the_artist_folder(self, tmp_path):
        p = make_mp3(str(tmp_path / "lib/A/A - Alb - 03 - Song.mp3"), REC)
        assert self.find(self.src(tmp_path / "lib/A/Alb"), tmp_path) == p

    def test_skips_a_namesake_that_is_another_recording(self, tmp_path):
        make_mp3(str(tmp_path / "lib/A/Alb/03 - Song.mp3"), OTHER)
        good = make_mp3(str(tmp_path / "lib/A/A - Alb - 09 - Song.mp3"), REC)
        assert self.find(self.src(tmp_path / "lib/A/Alb"), tmp_path) == good

    def test_download_folder_is_never_a_source_when_a_library_is_set(
        self, tmp_path,
    ):
        make_mp3(str(tmp_path / "dl/A/Alb/03 - Song.mp3"), REC)
        src = self.src("", album_path=str(tmp_path / "dl/A/Alb"))
        assert self.find(src, tmp_path) == ""

    def test_without_a_library_the_download_folder_is_used(self, tmp_path):
        p = make_mp3(str(tmp_path / "dl/A/Alb/03 - Song.mp3"), REC)
        src = self.src("", album_path=str(tmp_path / "dl/A/Alb"))
        assert self.find(src, tmp_path, library=False) == p

    def test_a_folder_outside_the_library_is_ignored(self, tmp_path):
        make_mp3(str(tmp_path / "elsewhere/Alb/03 - Song.mp3"), REC)
        src = self.src(tmp_path / "elsewhere/Alb")
        assert self.find(src, tmp_path) == ""

    def test_other_extension_is_not_a_source(self, tmp_path):
        make_mp3(str(tmp_path / "lib/A/Alb/03 - Song.flac"), REC)
        assert self.find(self.src(tmp_path / "lib/A/Alb"), tmp_path) == ""


class TestPlanRepeatsInOneRelease:
    def box(self):
        return [trk("Tides", 1, medium=1), trk("Tides", 1, medium=2),
                trk("Tides", 1, medium=3), trk("Shivers", 2, rec=OTHER)]

    def test_skip_downloads_the_first_position_only(self):
        tracks = self.box()
        p = plan(tracks)
        assert p.download == [tracks[0], tracks[3]]
        assert [t for t, _why in p.skipped] == tracks[1:3]
        assert p.keep == [tracks[0], tracks[3]]

    def test_the_leader_is_the_first_position_of_the_release(self):
        tracks = [trk("Tides", 1, medium=2), trk("Tides", 1, medium=1)]
        p = plan(tracks)
        assert [t["mediumNumber"] for t in p.download] == [1]

    def test_copy_keeps_every_position_and_downloads_one(self):
        p = plan(self.box(), cfg={"dedupe_same_album": "copy"})
        assert [t["title"] for t in p.download] == ["Tides", "Shivers"]
        assert len(p.repeats) == 2 and p.skipped == []
        assert len(p.keep) == 4

    def test_a_grab_always_gets_every_position(self):
        p = plan(self.box(), client_grab=True)
        assert p.skipped == [] and len(p.repeats) == 2

    def test_a_sibling_already_in_lidarr_satisfies_the_rest(self):
        tracks = self.box()
        tracks[0]["hasFile"] = True
        p = plan(tracks, pending=tracks[1:])
        assert [t["title"] for t in p.download] == ["Shivers"]
        assert len(p.skipped) == 2

    def test_a_sibling_that_keeps_failing_blocks_the_rest(self):
        tracks = self.box()
        p = plan(tracks, pending=tracks[1:],
                 deferred={"Tides": {"failures": 3, "retry_at": 1}})
        assert [t["title"] for t in p.download] == ["Shivers"]

    def test_a_finished_sibling_file_is_the_copy_source(self):
        tracks = self.box()
        p = plan(tracks, pending=tracks[1:], cfg={"dedupe_same_album": "copy"},
                 local_file=lambda t: "/dl/01 - Tides.mp3"
                 if t is tracks[0] else "")
        assert {path for _t, _l, path in p.repeats} == {"/dl/01 - Tides.mp3"}
        assert [t["title"] for t in p.download] == ["Shivers"]

    def test_tracks_without_a_recording_id_are_never_merged(self):
        tracks = [trk("Intro", 1, rec=None), trk("Intro", 2, rec=None)]
        assert plan(tracks).skipped == []

    def test_special_recordings_are_never_merged(self):
        tracks = [trk("[silence]", 1), trk("[silence]", 2)]
        assert plan(tracks).skipped == []


class TestPlanModes:
    def test_off_changes_nothing(self):
        tracks = [trk("Tides", 1), trk("Tides", 2)]
        p = plan(tracks, cfg={"dedupe_mode": "off"})
        assert p.download == tracks and p.keep == tracks and p.would == []

    def test_log_reports_but_changes_nothing(self):
        tracks = [trk("Tides", 1), trk("Tides", 2)]
        p = plan(tracks, cfg={"dedupe_mode": "log"})
        assert p.download == tracks and p.keep == tracks
        assert p.reuse == [] and p.skipped == [] and p.repeats == []
        assert len(p.would) == 1 and "would skip" in p.would[0]

    def test_unknown_mode_falls_back_to_log(self):
        assert dedupe.mode_of({"dedupe_mode": "bogus"}) == "log"

    def test_force_ignores_deduplication(self):
        tracks = [trk("Tides", 1), trk("Tides", 2)]
        p = plan(tracks, force=True)
        assert p.download == tracks and p.skipped == []


class TestPlanAcrossAlbums:
    def lib(self, tmp_path, rec=REC, tag=REC):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), tag)
        store(5, "Song", rec, library=str(folder))
        return folder

    def test_a_verified_file_of_another_album_is_reused(self, tmp_path):
        self.lib(tmp_path)
        t = trk("Song", 4)
        p = plan([t], tmp=tmp_path / "lib")
        assert p.download == [] and [r.track for r in p.reuse] == [t]
        assert p.keep == [t]

    def test_an_unverified_source_is_never_reused(self, tmp_path):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        store(5, "Song", REC, verified=False, library=str(folder))
        p = plan([trk("Song", 4)], tmp=tmp_path / "lib")
        assert p.reuse == [] and len(p.download) == 1

    def test_a_source_verified_as_another_recording_is_not_reused(
        self, tmp_path,
    ):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        models.add_track_download(
            album_id=5, album_title="S", artist_name="A", track_title="Song",
            track_number=1, success=True, error_message="", youtube_url="",
            youtube_title="", match_score=1, duration_seconds=1,
            album_path="", lidarr_album_path=str(folder), cover_url="",
            acoustid_recording_id=OTHER, recording_id=REC)
        assert plan([trk("Song", 4)], tmp=tmp_path / "lib").reuse == []

    def test_a_failed_attempt_is_not_a_source(self, tmp_path):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        store(5, "Song", REC, library=str(folder), success=False)
        assert plan([trk("Song", 4)], tmp=tmp_path / "lib").reuse == []

    def test_a_deleted_file_means_a_normal_download(self, tmp_path):
        folder = self.lib(tmp_path)
        os.remove(folder / "01 - Song.mp3")
        p = plan([trk("Song", 4)], tmp=tmp_path / "lib")
        assert p.reuse == [] and len(p.download) == 1

    def test_cross_album_can_be_switched_off(self, tmp_path):
        self.lib(tmp_path)
        p = plan([trk("Song", 4)], tmp=tmp_path / "lib",
                 cfg={"dedupe_cross_album": False})
        assert p.reuse == []

    def test_a_file_already_downloaded_for_this_album_is_not_fetched_again(
        self, tmp_path,
    ):
        folder = tmp_path / "lib/Artist/Album (2020)"
        make_mp3(str(folder / "04 - Song.mp3"), REC)
        store(7, "Song", REC, library=str(folder), album_title="Album")
        t = trk("Song", 4)
        p = plan([t], tmp=tmp_path / "lib")
        assert p.download == [] and p.reuse == [] and p.keep == []
        assert "already downloaded" in p.skipped[0][1]

    def test_a_grab_does_not_skip_on_history(self, tmp_path):
        folder = tmp_path / "lib/Artist/Album (2020)"
        make_mp3(str(folder / "04 - Song.mp3"), REC)
        store(7, "Song", REC, library=str(folder), album_title="Album")
        p = plan([trk("Song", 4)], tmp=tmp_path / "lib", client_grab=True)
        assert p.skipped == [] and len(p.download) == 1

    def test_log_mode_only_says_what_it_would_do(self, tmp_path):
        self.lib(tmp_path)
        p = plan([trk("Song", 4)], tmp=tmp_path / "lib",
                 cfg={"dedupe_mode": "log"})
        assert len(p.download) == 1 and p.reuse == []
        assert "would copy" in p.would[0]


class TestEditions:
    """Another edition of the same album is reused even with
    dedupe_cross_album off; singles and compilations are not."""

    def source(self, tmp_path, album_title, artist="Artist"):
        folder = tmp_path / "lib/Artist/Ed"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        models.add_track_download(
            album_id=5, album_title=album_title, artist_name=artist,
            track_title="Song", track_number=1, success=True,
            error_message="", youtube_url="", youtube_title="",
            match_score=1, duration_seconds=1, album_path="",
            lidarr_album_path=str(folder), cover_url="",
            acoustid_recording_id=REC, recording_id=REC)

    def run(self, tmp_path):
        return plan([trk("Song", 4)], tmp=tmp_path / "lib",
                    cfg={"dedupe_cross_album": False},
                    artist_name="Artist", album_title="Depois do Fim")

    def test_deluxe_of_the_same_album_is_an_edition(self, tmp_path):
        self.source(tmp_path, "Depois do Fim (Deluxe)")
        assert len(self.run(tmp_path).reuse) == 1

    def test_another_album_is_not_an_edition(self, tmp_path):
        self.source(tmp_path, "Greatest Hits")
        assert self.run(tmp_path).reuse == []

    def test_same_title_by_another_artist_is_not_an_edition(self, tmp_path):
        self.source(tmp_path, "Depois do Fim (Deluxe)", artist="Someone Else")
        assert self.run(tmp_path).reuse == []

    @pytest.mark.parametrize("a,b,same", [
        ("Album - Expanded Edition", "Album", True),
        ("Foco, Força e Fé", "Foco, força e fé", True),
        ("Greatest Hits", "Greatest Hits II", False),
        ("(Deluxe)", "(Special)", False),  # nothing left to compare
    ])
    def test_norm_album(self, a, b, same):
        equal = dedupe.norm_album(a) == dedupe.norm_album(b) != ""
        assert equal is same


def album_dict(tracks):
    return {
        "title": "Album", "releaseDate": "2020-01-01", "trackCount": 3,
        "artist": {"artistName": "Artist", "foreignArtistId": "ar"},
        "releases": [], "tracks": tracks,
    }


def ctx(album_path, **kw):
    c = {"artist_name": "Artist", "album_title": "Album", "album_id": 7,
         "album_mbid": "mb", "artist_mbid": "ar", "cover_data": None,
         "cover_url": "", "lidarr_album_path": ""}
    c.update(kw)
    return c


@pytest.fixture
def quiet_extras(monkeypatch):
    monkeypatch.setattr(processing, "apply_file_extras", lambda *a, **k: None)
    monkeypatch.setattr(processing, "create_xml_metadata",
                        lambda *a, **k: True)
    monkeypatch.setattr(processing, "load_config", lambda: {
        "audio_format": "mp3", "xml_metadata_enabled": False,
        "dedupe_mode": "reuse"})


class TestReuseEffect:
    def setup_source(self, tmp_path):
        folder = tmp_path / "lib/Artist/Single (2020)"
        src_file = make_mp3(str(folder / "01 - Song.mp3"), REC)
        sid = store(5, "Song", REC, library=str(folder))
        return src_file, sid

    def run_reuse(self, tmp_path, tracks):
        album_path = str(tmp_path / "dl/Artist/Album (2020)")
        os.makedirs(album_path)
        p = plan(tracks, tmp=tmp_path / "lib")
        run, res = processing._apply_dedupe_reuse(
            p, tracks, album_dict(tracks), album_path, ctx(album_path))
        return album_path, p, run, res

    def test_the_file_is_copied_retagged_and_recorded(
        self, tmp_path, quiet_extras,
    ):
        src_file, sid = self.setup_source(tmp_path)
        t = trk("Song", 4)
        album_path, _p, run, res = self.run_reuse(tmp_path, [t])
        dest = os.path.join(album_path, "04 - Song.mp3")
        assert os.path.isfile(dest) and run == []
        tags = ID3(dest)
        assert str(tags["TALB"]) == "Album" and str(tags["TIT2"]) == "Song"
        assert dedupe._tag_recording_id(dest) == REC
        assert [s["title"] for s in res["succeeded"]] == ["Song"]
        assert res["size"] == os.path.getsize(dest)
        row = db.get_db().execute(
            "SELECT * FROM track_downloads WHERE album_id = 7").fetchone()
        assert row["success"] == 1 and row["reused_from"] == sid
        assert row["source_format"] == "reused"
        assert row["recording_id"] == REC and row["acoustid_recording_id"] == REC
        assert row["file_path"] == dest and row["video_id"] == "abcdefghijk"

    def test_the_original_is_left_untouched(self, tmp_path, quiet_extras):
        src_file, _sid = self.setup_source(tmp_path)
        before = open(src_file, "rb").read()
        self.run_reuse(tmp_path, [trk("Song", 4)])
        assert os.path.isfile(src_file)
        assert open(src_file, "rb").read() == before
        assert str(ID3(src_file)["TIT2"]) == "old title"

    def test_a_reused_file_is_itself_a_verified_source(
        self, tmp_path, quiet_extras,
    ):
        self.setup_source(tmp_path)
        self.run_reuse(tmp_path, [trk("Song", 4)])
        assert {s["album_id"] for s in models.get_verified_sources(REC)} \
            == {5, 7}

    def test_a_failed_copy_falls_back_to_a_download(
        self, tmp_path, quiet_extras, monkeypatch,
    ):
        self.setup_source(tmp_path)
        monkeypatch.setattr(processing, "tag_audio_file",
                            lambda *a, **k: False)
        t = trk("Song", 4)
        album_path, _p, run, res = self.run_reuse(tmp_path, [t])
        assert run == [t] and res["succeeded"] == []
        assert os.listdir(album_path) == []  # no temp file left behind
        assert db.get_db().execute(
            "SELECT COUNT(*) FROM track_downloads WHERE album_id = 7"
        ).fetchone()[0] == 0

    def test_repeat_positions_are_copied_from_the_first_one(
        self, tmp_path, quiet_extras,
    ):
        album_path = str(tmp_path / "dl/Artist/Album (2020)")
        tracks = [trk("Tides", 1, medium=1, abs_number=1),
                  trk("Tides", 1, medium=2, abs_number=15)]
        p = plan(tracks, cfg={"dedupe_same_album": "copy"})
        os.makedirs(album_path)
        make_mp3(os.path.join(album_path, "01 - Tides.mp3"), REC)
        res = processing._copy_repeat_positions(
            p, tracks, album_dict(tracks), album_path, ctx(album_path),
            [{"title": "Tides", "track_download_id": 41}], {"stop": False})
        assert os.path.isfile(os.path.join(album_path, "15 - Tides.mp3"))
        assert len(res["succeeded"]) == 1
        row = db.get_db().execute(
            "SELECT reused_from FROM track_downloads").fetchone()
        assert row["reused_from"] == 41

    def test_repeat_positions_wait_when_the_first_has_no_file(
        self, tmp_path, quiet_extras,
    ):
        album_path = str(tmp_path / "dl/Artist/Album (2020)")
        os.makedirs(album_path)
        tracks = [trk("Tides", 1, medium=1, abs_number=1),
                  trk("Tides", 1, medium=2, abs_number=15)]
        p = plan(tracks, cfg={"dedupe_same_album": "copy"})
        res = processing._copy_repeat_positions(
            p, tracks, album_dict(tracks), album_path, ctx(album_path),
            [], {"stop": False})
        assert res["succeeded"] == [] and os.listdir(album_path) == []


class TestProcessAlbumWiring:
    """process_album_download hands only the remaining tracks to the
    downloader and counts the reused ones as delivered."""

    def wire(self, monkeypatch, tmp_path, tracks, mode, captured):
        album = {
            "id": 7, "title": "Album", "foreignAlbumId": "mbid",
            "releaseDate": "2020-01-01",
            "artist": {"artistName": "Artist", "id": 1,
                       "path": str(tmp_path / "lib/Artist")},
            "tracks": tracks, "images": [], "trackCount": len(tracks),
            "releases": [],
        }
        monkeypatch.setattr(processing, "DOWNLOAD_DIR", str(tmp_path / "dl"))
        monkeypatch.setattr(processing, "lidarr_request",
                            lambda *a, **k: album)
        monkeypatch.setattr(processing, "lidarr_request_with_retry",
                            lambda *a, **k: {})
        monkeypatch.setattr(processing, "get_valid_release_id", lambda a: 1)
        monkeypatch.setattr(processing, "relax_dir_permissions",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "set_permissions", lambda *a: None)
        monkeypatch.setattr(processing, "_send_album_notification",
                            lambda *a, **k: None)
        for name in ("get_itunes_artwork", "get_cover_art_archive_artwork",
                     "get_deezer_artwork"):
            monkeypatch.setattr(processing, name, lambda *a, **k: None)
        monkeypatch.setattr(processing, "find_album_on_ytmusic",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "_resolve_track_artists",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "apply_file_extras",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "_copy_to_lidarr",
                            lambda *a, **k: ("", "", False))
        monkeypatch.setattr(processing, "_log_import_result",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "_tag_artist_quietly",
                            lambda *a, **k: None)
        monkeypatch.setattr(processing, "_ensure_artist_image",
                            lambda *a, **k: None)
        monkeypatch.setattr(
            processing, "load_config",
            lambda: {"audio_format": "mp3", "xml_metadata_enabled": False,
                     "dedupe_mode": mode, "dedupe_same_album": "skip",
                     "dedupe_cross_album": True,
                     "lidarr_path": str(tmp_path / "lib"),
                     "track_retry_backoff": False})

        def fake_download(run_tracks, album_path, a, c, state=None):
            captured["run"] = [t["title"] for t in run_tracks]
            captured["recording_ids"] = c["recording_ids"]
            return ([], [{"title": t["title"], "track_download_id": None}
                         for t in run_tracks], 0,
                    {"verified_count": 0})
        monkeypatch.setattr(processing, "_download_tracks", fake_download)

        def fake_post(failed, succeeded, to_download, *a, **k):
            captured["post"] = ([s["title"] for s in succeeded],
                                [t["title"] for t in to_download])
            return {"success": True}
        monkeypatch.setattr(processing, "_handle_post_download", fake_post)

    def source(self, tmp_path):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        store(5, "Song", REC, library=str(folder))

    def test_reuse_mode_downloads_only_what_is_missing(
        self, tmp_path, monkeypatch,
    ):
        self.source(tmp_path)
        tracks = [trk("Song", 1), trk("Other", 2, rec=OTHER)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "reuse", cap)
        assert processing.process_album_download(7) == {"success": True}
        assert cap["run"] == ["Other"]
        assert sorted(cap["post"][0]) == ["Other", "Song"]
        assert sorted(cap["post"][1]) == ["Other", "Song"]
        assert os.path.isfile(
            tmp_path / "dl/Artist/Album (2020)/01 - Song.mp3")

    def test_log_mode_downloads_everything(self, tmp_path, monkeypatch):
        self.source(tmp_path)
        tracks = [trk("Song", 1), trk("Other", 2, rec=OTHER)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "log", cap)
        processing.process_album_download(7)
        assert cap["run"] == ["Song", "Other"]
        assert not os.path.exists(
            tmp_path / "dl/Artist/Album (2020)/01 - Song.mp3")

    def test_force_downloads_everything_even_in_reuse_mode(
        self, tmp_path, monkeypatch,
    ):
        self.source(tmp_path)
        tracks = [trk("Song", 1)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "reuse", cap)
        processing.process_album_download(7, force=True)
        assert cap["run"] == ["Song"]

    def test_unverified_source_is_downloaded_not_copied(
        self, tmp_path, monkeypatch,
    ):
        folder = tmp_path / "lib/Artist/Single (2020)"
        make_mp3(str(folder / "01 - Song.mp3"), REC)
        store(5, "Song", REC, verified=False, library=str(folder))
        cap = {}
        self.wire(monkeypatch, tmp_path, [trk("Song", 1)], "reuse", cap)
        processing.process_album_download(7)
        assert cap["run"] == ["Song"]

    def test_repeated_positions_are_downloaded_once(
        self, tmp_path, monkeypatch,
    ):
        tracks = [trk("Tides", 1, medium=1), trk("Tides", 1, medium=2),
                  trk("Tides", 1, medium=3)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "reuse", cap)
        processing.process_album_download(7)
        assert cap["run"] == ["Tides"]

    def test_everything_skipped_is_reported_as_nothing_to_do(
        self, tmp_path, monkeypatch,
    ):
        tracks = [trk("Tides", 1, medium=1, hasFile=True),
                  trk("Tides", 1, medium=2)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "reuse", cap)
        monkeypatch.setattr(processing, "makedirs_safe",
                            lambda *a, **k: None)
        result = processing.process_album_download(7)
        assert result["message"] == "Skipped" and "run" not in cap

    def test_a_planning_error_never_blocks_the_download(
        self, tmp_path, monkeypatch,
    ):
        tracks = [trk("Song", 1)]
        cap = {}
        self.wire(monkeypatch, tmp_path, tracks, "reuse", cap)

        def boom(*a, **k):
            raise RuntimeError("db down")
        monkeypatch.setattr(dedupe, "plan_album", boom)
        processing.process_album_download(7)
        assert cap["run"] == ["Song"]


class TestLogging:
    def test_a_box_release_does_not_flood_the_log(self, caplog):
        import logging
        tracks = [trk("Song", i, medium=i) for i in range(1, 60)]
        p = plan(tracks, cfg={"dedupe_mode": "log"})
        assert len(p.would) == 58
        with caplog.at_level(logging.INFO, logger="dedupe"):
            dedupe.log_plan(p, "Box")
        lines = [r.getMessage() for r in caplog.records]
        assert len(lines) == 22  # 20 decisions + "... and 38 more" + summary
        assert "38 more" in lines[20] and "58 track(s)" in lines[21]
        assert all("log only" in line for line in lines)

    def test_nothing_to_report_logs_nothing(self, caplog):
        import logging
        with caplog.at_level(logging.INFO, logger="dedupe"):
            dedupe.log_plan(plan([trk("A", 1), trk("B", 2, rec=OTHER)]), "X")
        assert caplog.records == []


class TestRecordingIdIsRecorded:
    def test_accept_track_file_stores_the_expected_recording(
        self, tmp_path, quiet_extras,
    ):
        album_path = tmp_path / "dl"
        album_path.mkdir()
        src = make_mp3(str(album_path / "temp_03.mp3"))
        c = ctx(str(album_path))
        c["recording_ids"] = {(3, "Song"): REC.upper()}
        size, _td = processing._accept_track_file(
            src, 3, "Song",
            {"youtube_url": "https://youtu.be/abcdefghijk"}, {},
            track_state={}, track_title="Song", album_path=str(album_path),
            album_ctx=c)
        row = db.get_db().execute(
            "SELECT recording_id, video_id, file_path, file_size, reused_from"
            " FROM track_downloads").fetchone()
        assert row["recording_id"] == REC
        assert row["video_id"] == "abcdefghijk"
        assert row["file_path"] == str(album_path / "03 - Song.mp3")
        assert (row["file_size"], row["reused_from"]) == (size, 0)

    def test_an_unknown_track_records_no_recording(
        self, tmp_path, quiet_extras,
    ):
        album_path = tmp_path / "dl"
        album_path.mkdir()
        src = make_mp3(str(album_path / "temp_03.mp3"))
        processing._accept_track_file(
            src, 3, "Song", {}, {}, track_state={}, track_title="Song",
            album_path=str(album_path), album_ctx=ctx(str(album_path)))
        assert db.get_db().execute(
            "SELECT recording_id FROM track_downloads").fetchone()[0] == ""

    def test_process_album_download_hands_the_ids_to_the_downloader(
        self, tmp_path, monkeypatch,
    ):
        tracks = [trk("Song", 1), trk("Other", 2, rec=OTHER)]
        cap = {}
        TestProcessAlbumWiring().wire(monkeypatch, tmp_path, tracks, "off", cap)
        processing.process_album_download(7)
        assert cap["recording_ids"] == {(1, "Song"): REC, (2, "Other"): OTHER}
