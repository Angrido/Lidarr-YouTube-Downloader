"""Tests for metadata module — ID3 tagging, XML metadata, and iTunes API."""

import os
from unittest.mock import MagicMock, patch

import pytest

import metadata


class TestCreateXmlMetadata:
    def test_creates_xml_file(self, tmp_path):
        result = metadata.create_xml_metadata(
            str(tmp_path),
            "Artist",
            "Album",
            1,
            "Track Title",
            album_id="mb-album-123",
            artist_id="mb-artist-456",
        )
        assert result is True
        xml_file = tmp_path / "01 - Track Title.xml"
        assert xml_file.exists()
        content = xml_file.read_text()
        assert "<title>Track Title</title>" in content
        assert "<artist>Artist</artist>" in content
        assert "<musicbrainzalbumid>mb-album-123</musicbrainzalbumid>" in content
        assert "<musicbrainzartistid>mb-artist-456</musicbrainzartistid>" in content

    def test_escapes_special_chars(self, tmp_path):
        result = metadata.create_xml_metadata(
            str(tmp_path), "Art & Craft", "Album <1>", 1, "Track & Roll"
        )
        assert result is True
        xml_file = tmp_path / "01 - Track & Roll.xml"
        content = xml_file.read_text()
        assert "&amp;" in content
        assert "&lt;1&gt;" in content

    def test_omits_musicbrainz_when_none(self, tmp_path):
        result = metadata.create_xml_metadata(
            str(tmp_path), "Artist", "Album", 3, "Song"
        )
        assert result is True
        xml_file = tmp_path / "03 - Song.xml"
        content = xml_file.read_text()
        assert "musicbrainzalbumid" not in content
        assert "musicbrainzartistid" not in content

    def test_returns_false_on_invalid_dir(self):
        result = metadata.create_xml_metadata(
            "/nonexistent/path/xyz", "A", "B", 1, "C"
        )
        assert result is False

    def test_track_number_zero_padded(self, tmp_path):
        metadata.create_xml_metadata(
            str(tmp_path), "Artist", "Album", 9, "Nine"
        )
        assert (tmp_path / "09 - Nine.xml").exists()


class TestGetItunesTracks:
    @patch("metadata.requests.get")
    def test_returns_tracks(self, mock_get):
        mock_get.side_effect = [
            MagicMock(
                json=lambda: {
                    "resultCount": 1,
                    "results": [{"collectionId": 123}],
                }
            ),
            MagicMock(
                json=lambda: {
                    "results": [
                        {"wrapperType": "collection"},
                        {
                            "trackNumber": 1,
                            "trackName": "Song1",
                            "previewUrl": "http://preview",
                        },
                        {
                            "trackNumber": 2,
                            "trackName": "Song2",
                            "previewUrl": "http://preview2",
                        },
                    ]
                }
            ),
        ]
        tracks = metadata.get_itunes_tracks("Artist", "Album")
        assert len(tracks) == 2
        assert tracks[0]["title"] == "Song1"
        assert tracks[0]["trackNumber"] == 1
        assert tracks[1]["title"] == "Song2"

    @patch("metadata.requests.get")
    def test_returns_empty_on_no_results(self, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"resultCount": 0, "results": []}
        )
        assert metadata.get_itunes_tracks("Unknown", "Album") == []

    @patch("metadata.requests.get")
    def test_returns_empty_on_exception(self, mock_get):
        mock_get.side_effect = Exception("network error")
        assert metadata.get_itunes_tracks("Artist", "Album") == []


class TestGetItunesArtwork:
    @patch("metadata.requests.get")
    def test_returns_artwork_data(self, mock_get):
        mock_get.side_effect = [
            MagicMock(
                json=lambda: {
                    "resultCount": 1,
                    "results": [
                        {"artworkUrl100": "http://img/100x100bb.jpg"}
                    ],
                }
            ),
            MagicMock(content=b"image_data"),
        ]
        result = metadata.get_itunes_artwork("Artist", "Album")
        assert result == b"image_data"

    @patch("metadata.requests.get")
    def test_replaces_resolution_in_url(self, mock_get):
        mock_get.side_effect = [
            MagicMock(
                json=lambda: {
                    "resultCount": 1,
                    "results": [
                        {"artworkUrl100": "http://img/100x100bb.jpg"}
                    ],
                }
            ),
            MagicMock(content=b"hires"),
        ]
        metadata.get_itunes_artwork("Artist", "Album")
        second_call_url = mock_get.call_args_list[1][0][0]
        assert "3000x3000" in second_call_url
        assert "100x100" not in second_call_url

    @patch("metadata.requests.get")
    def test_returns_none_on_no_results(self, mock_get):
        mock_get.return_value = MagicMock(
            json=lambda: {"resultCount": 0, "results": []}
        )
        assert metadata.get_itunes_artwork("Artist", "Album") is None

    @patch("metadata.requests.get")
    def test_returns_none_on_exception(self, mock_get):
        mock_get.side_effect = Exception("timeout")
        assert metadata.get_itunes_artwork("Artist", "Album") is None


class TestTagMp3:
    @patch("metadata.get_monitored_release")
    def test_tags_mp3_file(self, mock_release, tmp_path):
        """Test tagging a real MP3 file with metadata."""
        from mutagen.mp3 import MP3

        mp3_path = _create_minimal_mp3(tmp_path / "test.mp3")
        mock_release.return_value = {
            "foreignReleaseId": "rel-123",
            "country": "US",
        }

        track_info = {
            "title": "Test Song",
            "trackNumber": "3",
            "foreignRecordingId": "rec-456",
        }
        album_info = {
            "title": "Test Album",
            "artist": {
                "artistName": "Test Artist",
                "foreignArtistId": "art-789",
            },
            "releaseDate": "2024-01-15",
            "trackCount": 10,
            "foreignAlbumId": "alb-012",
            "releases": [{"monitored": True}],
        }

        result = metadata.tag_mp3(str(mp3_path), track_info, album_info, None)
        assert result is True

        audio = MP3(str(mp3_path))
        assert str(audio.tags["TIT2"]) == "Test Song"
        assert str(audio.tags["TPE1"]) == "Test Artist"
        assert str(audio.tags["TALB"]) == "Test Album"
        assert str(audio.tags["TDRC"]) == "2024"
        assert str(audio.tags["TRCK"]) == "3/10"

    @patch("metadata.get_monitored_release")
    def test_tags_with_cover_data(self, mock_release, tmp_path):
        mp3_path = _create_minimal_mp3(tmp_path / "cover.mp3")
        mock_release.return_value = None

        track_info = {"title": "Song", "trackNumber": "1"}
        album_info = {
            "title": "Album",
            "artist": {"artistName": "Artist"},
            "releaseDate": "2023-06-01",
            "trackCount": 5,
        }

        result = metadata.tag_mp3(
            str(mp3_path), track_info, album_info, b"fake_cover"
        )
        assert result is True

        from mutagen.mp3 import MP3

        audio = MP3(str(mp3_path))
        apic_frames = audio.tags.getall("APIC")
        assert len(apic_frames) == 1
        assert apic_frames[0].data == b"fake_cover"

    def test_returns_false_on_invalid_file(self, tmp_path):
        bad_file = tmp_path / "not_mp3.txt"
        bad_file.write_text("not an mp3")
        result = metadata.tag_mp3(
            str(bad_file),
            {"title": "X", "trackNumber": "1"},
            {
                "title": "A",
                "artist": {"artistName": "B"},
                "releaseDate": "2024",
                "trackCount": 1,
            },
            None,
        )
        assert result is False


class TestTagM4a:
    @patch("metadata.get_monitored_release")
    @patch("metadata.MP4")
    def test_tags_m4a_file(self, mock_mp4_cls, mock_release):
        store = {}
        mock_audio = MagicMock()
        mock_audio.__setitem__ = lambda self, k, v: store.update({k: v})
        mock_audio.__getitem__ = lambda self, k: store[k]
        mock_mp4_cls.return_value = mock_audio
        mock_release.return_value = {
            "foreignReleaseId": "rel-123",
            "country": "US",
        }

        track_info = {
            "title": "Test Song",
            "trackNumber": "3",
            "foreignRecordingId": "rec-456",
        }
        album_info = {
            "title": "Test Album",
            "artist": {
                "artistName": "Test Artist",
                "foreignArtistId": "art-789",
            },
            "releaseDate": "2024-01-15",
            "trackCount": 10,
            "foreignAlbumId": "alb-012",
            "releases": [{"monitored": True}],
        }

        result = metadata.tag_m4a("/fake/path.m4a", track_info, album_info, None)
        assert result is True
        mock_audio.save.assert_called_once()
        assert store["\xa9nam"] == ["Test Song"]
        assert store["\xa9ART"] == ["Test Artist"]
        assert store["aART"] == ["Test Artist"]
        assert store["\xa9alb"] == ["Test Album"]
        assert store["\xa9day"] == ["2024"]
        assert store["trkn"] == [(3, 10)]

    @patch("metadata.get_monitored_release")
    @patch("metadata.MP4")
    def test_tags_m4a_with_cover(self, mock_mp4_cls, mock_release):
        store = {}
        mock_audio = MagicMock()
        mock_audio.__setitem__ = lambda self, k, v: store.update({k: v})
        mock_mp4_cls.return_value = mock_audio
        mock_release.return_value = None

        track_info = {"title": "Song", "trackNumber": "1"}
        album_info = {
            "title": "Album",
            "artist": {"artistName": "Artist"},
            "releaseDate": "2023-01-01",
            "trackCount": 5,
        }

        result = metadata.tag_m4a("/fake/path.m4a", track_info, album_info, b"cover")
        assert result is True
        assert len(store["covr"]) == 1

    @patch("metadata.MP4")
    def test_returns_false_on_exception(self, mock_mp4_cls):
        mock_mp4_cls.side_effect = Exception("bad file")
        result = metadata.tag_m4a(
            "/fake/bad.m4a",
            {"title": "X", "trackNumber": "1"},
            {"title": "A", "artist": {"artistName": "B"}, "releaseDate": "2024", "trackCount": 1},
            None,
        )
        assert result is False


class TestTagAudioFileDispatch:
    @patch("metadata.tag_opus")
    def test_dispatches_opus(self, mock_opus):
        mock_opus.return_value = True
        metadata.tag_audio_file("/path/file.opus", {}, {}, None)
        mock_opus.assert_called_once()

    @patch("metadata.tag_m4a")
    def test_dispatches_m4a(self, mock_m4a):
        mock_m4a.return_value = True
        metadata.tag_audio_file("/path/file.m4a", {}, {}, None)
        mock_m4a.assert_called_once()

    @patch("metadata.tag_mp3")
    def test_dispatches_mp3(self, mock_mp3):
        mock_mp3.return_value = True
        metadata.tag_audio_file("/path/file.mp3", {}, {}, None)
        mock_mp3.assert_called_once()


def _create_minimal_mp3(path):
    """Create a minimal valid MP3 file for testing."""
    from mutagen.mp3 import MP3

    # Minimal MP3 frame: MPEG1 Layer3, 128kbps, 44100Hz, stereo
    # Frame header + enough padding for mutagen to accept it
    frame_header = bytes(
        [0xFF, 0xFB, 0x90, 0x00]
    )  # sync, MPEG1/Layer3/128k/44.1k
    frame_data = b"\x00" * 413  # pad to full frame size (417 bytes total)
    # Write multiple frames so mutagen recognizes it
    with open(str(path), "wb") as f:
        for _ in range(10):
            f.write(frame_header + frame_data)
    return path
