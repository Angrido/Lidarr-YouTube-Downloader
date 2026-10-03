"""Tests for the fix_metadata tool's MusicBrainz frame repair (#93).

Files written before the fix carry the recording MBID in the
"MusicBrainz Release Track Id" frame, which taggers validate against the
release tracklist. The tool has to move it and put the release-track MBID
in its place.
"""

import importlib.util
import os

import pytest

RECORDING_ID = "4a106fab-8841-4d7e-a028-bc2a61228dbd"
RELEASE_TRACK_ID = "49f1df87-7cde-4186-9178-bcbd8c3cb7d5"
RELEASE_ID = "d638506c-0087-4754-9ed5-b9373ce0320d"


@pytest.fixture(scope="module")
def tool():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "tools",
        "fix_metadata.py",
    )
    spec = importlib.util.spec_from_file_location("fix_metadata_tool", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _legacy_mp3(path, release_id="old-release", country_desc="MusicBrainz Release Country"):
    from mutagen.id3 import TXXX, UFID
    from mutagen.mp3 import MP3

    frame_header = bytes([0xFF, 0xFB, 0x90, 0x00])
    with open(str(path), "wb") as f:
        for _ in range(10):
            f.write(frame_header + b"\x00" * 413)

    audio = MP3(str(path))
    audio.add_tags()
    audio.tags.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=release_id))
    audio.tags.add(TXXX(encoding=3, desc=country_desc, text="GB"))
    audio.tags.add(
        TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=RECORDING_ID)
    )
    audio.tags.add(
        UFID(owner="http://musicbrainz.org", data=RECORDING_ID.encode())
    )
    audio.save(v2_version=3)
    return path


def _frames(path):
    from mutagen.id3 import ID3

    audio = ID3(str(path))
    return {f.desc: str(f.text[0]) for f in audio.getall("TXXX") if f.text}


class TestGetMp3Metadata:
    def test_reads_the_release_track_frame(self, tool, tmp_path):
        meta = tool.get_mp3_metadata(str(_legacy_mp3(tmp_path / "a.mp3")))
        assert meta["release_track_id"] == RECORDING_ID
        assert meta["recording_id"] == RECORDING_ID

    def test_reads_country_under_the_legacy_frame_name(self, tool, tmp_path):
        meta = tool.get_mp3_metadata(str(_legacy_mp3(tmp_path / "b.mp3")))
        assert meta["country"] == "GB"

    def test_reads_country_under_the_picard_frame_name(self, tool, tmp_path):
        path = _legacy_mp3(
            tmp_path / "c.mp3",
            country_desc="MusicBrainz Album Release Country",
        )
        assert tool.get_mp3_metadata(str(path))["country"] == "GB"


class TestFixMp3Metadata:
    def test_moves_the_recording_id_out_of_the_release_track_frame(
        self, tool, tmp_path
    ):
        path = _legacy_mp3(tmp_path / "d.mp3")
        changes = tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "US", RECORDING_ID, RELEASE_TRACK_ID
        )
        assert changes

        frames = _frames(path)
        assert frames["MusicBrainz Release Track Id"] == RELEASE_TRACK_ID
        assert frames["MusicBrainz Recording Id"] == RECORDING_ID
        assert frames["MusicBrainz Album Id"] == RELEASE_ID

    def test_keeps_the_recording_id_in_ufid(self, tool, tmp_path):
        from mutagen.id3 import ID3

        path = _legacy_mp3(tmp_path / "e.mp3")
        tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "US", RECORDING_ID, RELEASE_TRACK_ID
        )
        ufid = ID3(str(path)).getall("UFID:http://musicbrainz.org")
        assert ufid[0].data.decode() == RECORDING_ID

    def test_migrates_the_country_frame_name(self, tool, tmp_path):
        path = _legacy_mp3(tmp_path / "f.mp3")
        tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "US", RECORDING_ID, RELEASE_TRACK_ID
        )
        frames = _frames(path)
        assert frames["MusicBrainz Album Release Country"] == "US"
        assert "MusicBrainz Release Country" not in frames

    def test_drops_a_stale_release_track_frame_when_none_is_known(
        self, tool, tmp_path
    ):
        path = _legacy_mp3(tmp_path / "g.mp3")
        tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "US", RECORDING_ID, None
        )
        assert "MusicBrainz Release Track Id" not in _frames(path)

    def test_dry_run_changes_nothing_on_disk(self, tool, tmp_path):
        path = _legacy_mp3(tmp_path / "h.mp3")
        before = _frames(path)
        changes = tool.fix_mp3_metadata(
            str(path),
            RELEASE_ID,
            "US",
            RECORDING_ID,
            RELEASE_TRACK_ID,
            dry_run=True,
        )
        assert changes
        assert _frames(path) == before

    def test_reports_the_release_track_change(self, tool, tmp_path):
        path = _legacy_mp3(tmp_path / "i.mp3")
        changes = tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "US", RECORDING_ID, RELEASE_TRACK_ID
        )
        assert any("Release Track Id" in c for c in changes)

    def test_adds_the_recording_frame_even_when_the_id_already_matches(
        self, tool, tmp_path
    ):
        """The write must be reported, or it would never be saved to disk."""
        path = _legacy_mp3(tmp_path / "j.mp3", release_id=RELEASE_ID)
        changes = tool.fix_mp3_metadata(
            str(path), RELEASE_ID, "GB", RECORDING_ID, RECORDING_ID
        )
        assert changes
        assert _frames(path)["MusicBrainz Recording Id"] == RECORDING_ID
