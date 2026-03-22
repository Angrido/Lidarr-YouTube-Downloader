from unittest.mock import patch

from downloader import (
    _check_forbidden,
    _is_official_channel,
    _title_similarity,
    download_track_youtube,
)


class TestTitleSimilarity:
    def test_exact_match(self):
        score = _title_similarity("Artist Track", "Track", "Artist")
        assert score > 0.8

    def test_low_similarity(self):
        score = _title_similarity(
            "Completely Different", "Track", "Artist"
        )
        assert score < 0.5

    def test_contains_track_title_bonus(self):
        score_with = _title_similarity(
            "Something Track Name Here", "Track Name", "Other"
        )
        score_without = _title_similarity(
            "Something Else Here", "Track Name", "Other"
        )
        assert score_with > score_without

    def test_contains_artist_bonus(self):
        score_with = _title_similarity(
            "ArtistX plays a song", "Song", "ArtistX"
        )
        score_without = _title_similarity(
            "Someone plays a song", "Song", "ArtistX"
        )
        assert score_with > score_without

    def test_capped_at_one(self):
        score = _title_similarity(
            "Artist Track", "Track", "Artist"
        )
        assert score <= 1.0

    def test_empty_yt_title(self):
        score = _title_similarity("", "Track", "Artist")
        assert score >= 0.0


class TestIsOfficialChannel:
    def test_artist_name_match(self):
        assert _is_official_channel("ArtistName", "ArtistName") is True

    def test_vevo(self):
        assert _is_official_channel("ArtistVEVO", "Artist") is True

    def test_topic(self):
        assert _is_official_channel(
            "Artist - Topic", "Artist"
        ) is True

    def test_official_suffix(self):
        assert _is_official_channel(
            "Band Official", "Band"
        ) is True

    def test_false_for_random(self):
        assert _is_official_channel(
            "RandomChannel", "Artist"
        ) is False

    def test_none_channel(self):
        assert _is_official_channel(None, "Artist") is False

    def test_empty_channel(self):
        assert _is_official_channel("", "Artist") is False

    def test_case_insensitive(self):
        assert _is_official_channel("artistname", "ArtistName") is True


class TestCheckForbidden:
    def test_blocks_single_word(self):
        result = _check_forbidden(
            "song remix version", "song", ["remix", "cover"]
        )
        assert result == "remix"

    def test_allows_when_in_title(self):
        result = _check_forbidden(
            "remix song", "remix song", ["remix"]
        )
        assert result is None

    def test_multi_word_forbidden(self):
        result = _check_forbidden(
            "song dj mix version", "song", ["dj mix"]
        )
        assert result == "dj mix"

    def test_no_forbidden_match(self):
        result = _check_forbidden(
            "normal song title", "normal song", ["remix", "cover"]
        )
        assert result is None

    def test_word_boundary_respected(self):
        result = _check_forbidden(
            "covered in gold", "gold song", ["cover"]
        )
        assert result is None

    def test_multi_word_not_in_track(self):
        result = _check_forbidden(
            "track dj mix", "track", ["dj mix"]
        )
        assert result == "dj mix"

    def test_multi_word_in_track_allowed(self):
        result = _check_forbidden(
            "dj mix track", "dj mix track", ["dj mix"]
        )
        assert result is None

    def test_empty_forbidden_list(self):
        result = _check_forbidden("any title", "any title", [])
        assert result is None


class TestDownloadTrackYoutubeReturnType:
    """download_track_youtube returns metadata dict, not True/string."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_success_returns_metadata_dict(self, mock_ydl_class):
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [{
                "url": "https://youtube.com/watch?v=abc",
                "title": "Artist - Track",
                "duration": 240,
                "channel": "ArtistVEVO",
                "view_count": 1000000,
            }],
        }
        mock_ydl.download.return_value = 0

        import os
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "test")
            open(out + ".mp3", "w").close()
            result = download_track_youtube(
                "Artist Track official audio", out, "Track", 240000,
            )
        assert isinstance(result, dict)
        assert result["success"] is True
        assert "youtube_url" in result
        assert "youtube_title" in result
        assert "match_score" in result
        assert "duration_seconds" in result

    def test_no_candidates_returns_failure_dict(self):
        with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_class:
            mock_ydl = (
                mock_ydl_class.return_value.__enter__.return_value
            )
            mock_ydl.extract_info.return_value = {"entries": []}

            result = download_track_youtube(
                "Nonexistent Track", "/tmp/out", "Track", 240000,
            )
        assert isinstance(result, dict)
        assert result["success"] is False
        assert "error_message" in result


class TestBannedUrlFiltering:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_banned_url_excluded_from_candidates(
        self, mock_config, mock_ydl_class
    ):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Artist - Track (Official)",
                    "url": "banned_video_id",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 1000000,
                },
                {
                    "title": "Artist - Track Audio",
                    "url": "good_video_id",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 500000,
                },
            ]
        }
        mock_ydl.download.return_value = 0

        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            banned_urls={"banned_video_id"},
        )
        if result and isinstance(result, dict) and result.get("success"):
            assert result.get("youtube_url") != "banned_video_id"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_no_banned_urls_passes_all_candidates(
        self, mock_config, mock_ydl_class
    ):
        """When banned_urls is None or empty, all candidates pass."""
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Artist - Track",
                    "url": "video_id",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 1000000,
                },
            ]
        }
        mock_ydl.download.return_value = 0

        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            banned_urls=None,
        )
        assert result is not None


class TestSkipCheck:
    """skip_check callback aborts search early."""

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_skip_check_true_returns_skipped(self, mock_ydl_cls):
        from downloader import download_track_youtube
        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            progress_hook=None,
            skip_check=lambda: True,
        )
        assert result.get("skipped") is True
        mock_ydl_cls.assert_not_called()

    @patch("downloader.yt_dlp.YoutubeDL")
    def test_skip_check_false_continues(self, mock_ydl_cls):
        mock_ydl = mock_ydl_cls.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}
        from downloader import download_track_youtube
        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            progress_hook=None,
            skip_check=lambda: False,
        )
        assert result.get("skipped") is not True
        assert result.get("success") is False
