from unittest.mock import patch

from downloader import (
    _candidate_display_url,
    _check_forbidden,
    _is_official_channel,
    _title_similarity,
    download_track_youtube,
    download_youtube_candidate,
    search_youtube_candidates,
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

        download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            banned_urls={"banned_video_id"},
        )
        # Verify the banned URL was never passed to ydl.download
        if mock_ydl.download.called:
            download_url = mock_ydl.download.call_args[0][0][0]
            assert download_url != "banned_video_id"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_all_candidates_banned_returns_failure(
        self, mock_config, mock_ydl_class
    ):
        """When every candidate is banned, download fails gracefully."""
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
                    "url": "only_video_id",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 1000000,
                },
            ]
        }

        result = download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            banned_urls={"only_video_id"},
        )
        # Should fail — no candidates remain after filtering
        assert not mock_ydl.download.called
        assert isinstance(result, dict)
        assert result.get("success") is False

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

        download_track_youtube(
            "Artist Track official audio",
            "/tmp/test_output",
            "Track",
            expected_duration_ms=200000,
            banned_urls=None,
        )
        # With no bans, the candidate should reach the download phase
        assert mock_ydl.download.called


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


class TestSearchYoutubeCandidates:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_returns_ranked_candidates(self, mock_config, mock_ydl_class):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Artist - Track B",
                    "url": "url_b",
                    "duration": 200,
                    "channel": "Other",
                    "view_count": 100,
                },
                {
                    "title": "Artist - Track A (Official Audio)",
                    "url": "url_a",
                    "duration": 200,
                    "channel": "ArtistVEVO",
                    "view_count": 1000000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Artist Track official audio", "Track", expected_duration_ms=200000
        )
        assert len(candidates) == 2
        assert candidates[0]["score"] >= candidates[1]["score"]

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_respects_banned_urls(self, mock_config, mock_ydl_class):
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
                    "url": "banned_url",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 1000,
                },
                {
                    "title": "Artist - Track Alt",
                    "url": "good_url",
                    "duration": 200,
                    "channel": "Artist",
                    "view_count": 1000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Artist Track", "Track", expected_duration_ms=200000,
            banned_urls={"banned_url"},
        )
        urls = [c["url"] for c in candidates]
        assert "banned_url" not in urls
        assert "good_url" in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_caps_at_10_candidates(self, mock_config, mock_ydl_class):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": f"Track {i}",
                    "url": f"url_{i}",
                    "duration": 200,
                    "channel": "Ch",
                    "view_count": 1000,
                }
                for i in range(15)
            ]
        }
        candidates = search_youtube_candidates(
            "Artist Track", "Track", expected_duration_ms=200000
        )
        assert len(candidates) <= 10

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_skip_check_returns_empty(self, mock_config, mock_ydl_class):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        candidates = search_youtube_candidates(
            "Artist Track", "Track", skip_check=lambda: True
        )
        assert candidates == []

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_no_candidates_returns_empty(self, mock_config, mock_ydl_class):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}
        candidates = search_youtube_candidates("Artist Track", "Track")
        assert candidates == []


class TestDownloadYoutubeCandidate:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_success_returns_result(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.return_value = 0
        candidate = {
            "url": "test_url",
            "title": "Test Title",
            "duration": 200,
            "score": 0.9,
        }
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is True
        assert result["youtube_url"] == "test_url"
        assert result["youtube_title"] == "Test Title"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_download_failure_returns_error(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("Network error")
        candidate = {
            "url": "test_url",
            "title": "Test Title",
            "duration": 200,
            "score": 0.9,
        }
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_skip_check_returns_skipped(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        candidate = {
            "url": "test_url",
            "title": "Test",
            "duration": 200,
            "score": 0.9,
        }
        result = download_youtube_candidate(
            candidate, "/tmp/output", skip_check=lambda: True
        )
        assert result.get("skipped") is True

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_format_unavailable_falls_through_chain(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Requested format is not available"
        )
        candidate = {
            "url": "u", "title": "t", "duration": 200, "score": 0.9,
        }
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False
        assert "format" in result["error_message"].lower() \
            or "no downloadable" in result["error_message"].lower()


class TestTopicChannelForbiddenExemption:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_topic_channel_track_with_remix_word_still_accepted(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": ["remix"],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Calling (Remix)",
                    "url": "topic_url",
                    "duration": 200,
                    "channel": "Some Artist - Topic",
                    "view_count": 100000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Some Artist Calling official audio", "Calling",
            expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "topic_url" in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_non_topic_remix_still_blocked(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": ["remix"],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Calling (Some DJ Remix)",
                    "url": "remix_url",
                    "duration": 200,
                    "channel": "RandomDJ",
                    "view_count": 50000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Some Artist Calling official audio", "Calling",
            expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "remix_url" not in urls


class TestFlatEntryWithoutDuration:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_without_duration_still_accepted(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Pattern Index",
                    "url": "v4GurmZYFwk",
                    "duration": None,
                    "channel": "Max Cooper - Topic",
                    "view_count": 0,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Max Cooper Pattern Index official audio", "Pattern Index",
            expected_duration_ms=381000,
        )
        assert any(c["url"] == "v4GurmZYFwk" for c in candidates)


class TestCandidateDisplayUrl:
    def test_ytmusic_id_renders_music_url(self):
        c = {"url": "abc12345678", "source": "ytmusic"}
        assert _candidate_display_url(c) == (
            "https://music.youtube.com/watch?v=abc12345678"
        )

    def test_ytmusic_youtube_url_rewritten_to_music(self):
        c = {
            "url": "https://www.youtube.com/watch?v=abc12345678",
            "source": "ytmusic",
        }
        assert _candidate_display_url(c) == (
            "https://music.youtube.com/watch?v=abc12345678"
        )

    def test_ytsearch_id_renders_youtube_url(self):
        c = {"url": "abc12345678", "source": "ytsearch"}
        assert _candidate_display_url(c) == (
            "https://www.youtube.com/watch?v=abc12345678"
        )

    def test_unknown_url_passes_through(self):
        c = {"url": "not-a-yt-url", "source": "ytmusic"}
        assert _candidate_display_url(c) == "not-a-yt-url"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_download_result_records_music_url_for_ytmusic_candidate(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.return_value = 0
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytmusic",
        }
        result = download_youtube_candidate(candidate, "/tmp/out")
        assert result["success"] is True
        assert result["youtube_url"] == (
            "https://music.youtube.com/watch?v=abc12345678"
        )


class TestMusicClientPriority:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_music_source_adds_music_clients(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("Please sign in")
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytmusic",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        called_clients = []
        for call in mock_ydl_class.call_args_list:
            opts = call.args[0] if call.args else call.kwargs
            extractor_args = opts.get("extractor_args", {}) if isinstance(opts, dict) else {}
            yt_args = extractor_args.get("youtube", {}) if isinstance(extractor_args, dict) else {}
            pc = yt_args.get("player_client", [None])
            called_clients.extend(pc if isinstance(pc, list) else [pc])
        assert any("music" in str(c) for c in called_clients if c)

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_music_clients_tried_before_user_default(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("Please sign in")
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytmusic",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        called_clients = []
        for call in mock_ydl_class.call_args_list:
            opts = call.args[0] if call.args else call.kwargs
            extractor_args = opts.get("extractor_args", {}) if isinstance(opts, dict) else {}
            yt_args = extractor_args.get("youtube", {}) if isinstance(extractor_args, dict) else {}
            pc = yt_args.get("player_client", [None])
            called_clients.extend(pc if isinstance(pc, list) else [pc])
        first_music = next(
            (i for i, c in enumerate(called_clients) if c and "music" in str(c)),
            -1,
        )
        first_android = next(
            (i for i, c in enumerate(called_clients) if c == "android"),
            -1,
        )
        assert first_music != -1
        assert first_android == -1 or first_music < first_android

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_non_music_source_skips_music_clients(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("Please sign in")
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytsearch",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        called_clients = []
        for call in mock_ydl_class.call_args_list:
            opts = call.args[0] if call.args else call.kwargs
            extractor_args = opts.get("extractor_args", {}) if isinstance(opts, dict) else {}
            yt_args = extractor_args.get("youtube", {}) if isinstance(extractor_args, dict) else {}
            pc = yt_args.get("player_client", [None])
            called_clients.extend(pc if isinstance(pc, list) else [pc])
        assert not any("music" in str(c) for c in called_clients if c)


class TestVideoIdDedup:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_same_video_from_ytmusic_and_ytsearch_keeps_ytmusic_source(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        ytmusic_entry = {
            "title": "Pattern Index",
            "url": "https://www.youtube.com/watch?v=v4GurmZYFwk",
            "duration": 380,
            "channel": "Max Cooper - Topic",
            "view_count": 1000,
        }
        ytsearch_entry = {
            "title": "Pattern Index",
            "url": "v4GurmZYFwk",
            "duration": 380,
            "channel": "Max Cooper - Topic",
            "view_count": 1000,
        }
        results = iter([
            {"entries": [ytmusic_entry]},
            {"entries": [ytmusic_entry]},
            {"entries": [ytsearch_entry]},
            {"entries": []},
            {"entries": []},
            {"entries": []},
            {"entries": []},
            {"entries": []},
            {"entries": []},
            {"entries": []},
        ])
        mock_ydl.extract_info.side_effect = lambda *a, **kw: next(results)
        candidates = search_youtube_candidates(
            "Max Cooper Pattern Index official audio", "Pattern Index",
            expected_duration_ms=380000,
        )
        matching = [c for c in candidates if "v4GurmZYFwk" in c["url"]]
        assert len(matching) == 1
        assert matching[0]["source"] == "ytmusic"


class TestYouTubeMusicSearch:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_url_used_before_ytsearch(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}
        search_youtube_candidates(
            "Artist Track official audio", "Track",
            expected_duration_ms=200000,
        )
        called_targets = [
            call.args[0]
            for call in mock_ydl.extract_info.call_args_list
            if call.args
        ]
        first_ytmusic = next(
            (i for i, t in enumerate(called_targets)
             if t.startswith("https://music.youtube.com/search")),
            -1,
        )
        first_ytsearch = next(
            (i for i, t in enumerate(called_targets)
             if t.startswith("ytsearch")),
            -1,
        )
        assert first_ytmusic != -1
        if first_ytsearch != -1:
            assert first_ytmusic < first_ytsearch
