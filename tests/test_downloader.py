from unittest.mock import MagicMock, patch

from downloader import (
    _build_common_opts,
    _candidate_display_url,
    _check_forbidden,
    _is_official_channel,
    _looks_like_music_video,
    _title_similarity,
    download_track_youtube,
    download_youtube_candidate,
    find_album_on_ytmusic,
    get_effective_forbidden_words,
    match_album_track,
    search_youtube_candidates,
)


def test_looks_like_music_video():
    assert _looks_like_music_video("Artist - Song (Official Video)")
    assert _looks_like_music_video("Artist - Song [Music Video]")
    assert _looks_like_music_video("Artist - Song MV")
    assert not _looks_like_music_video("Artist - Song (Official Audio)")
    assert not _looks_like_music_video("Artist - Song")
    assert not _looks_like_music_video("")


def test_build_common_opts_po_token():
    cfg = {
        "yt_retries": 3, "yt_fragment_retries": 3, "yt_sleep_requests": 0,
        "yt_sleep_interval": 0, "yt_max_sleep_interval": 1,
        "yt_force_ipv4": False, "yt_po_token": "web.gvs+ABC, web.player+DEF",
    }
    with patch("downloader.load_config", return_value=cfg):
        opts = _build_common_opts(player_client="web")
    yt = opts["extractor_args"]["youtube"]
    assert yt["player_client"] == ["web"]
    assert yt["po_token"] == ["web.gvs+ABC", "web.player+DEF"]


def test_build_common_opts_no_po_token():
    cfg = {
        "yt_retries": 3, "yt_fragment_retries": 3, "yt_sleep_requests": 0,
        "yt_sleep_interval": 0, "yt_max_sleep_interval": 1,
        "yt_force_ipv4": False,
    }
    with patch("downloader.load_config", return_value=cfg):
        opts = _build_common_opts()
    assert "extractor_args" not in opts


def test_build_common_opts_pot_provider_url():
    cfg = {
        "yt_retries": 3, "yt_fragment_retries": 3, "yt_sleep_requests": 0,
        "yt_sleep_interval": 0, "yt_max_sleep_interval": 1,
        "yt_force_ipv4": False,
        "yt_pot_provider_url": "http://bgutil-provider:4416",
    }
    with patch("downloader.load_config", return_value=cfg):
        opts = _build_common_opts()
    assert opts["extractor_args"]["youtubepot-bgutilhttp"]["base_url"] == [
        "http://bgutil-provider:4416"
    ]


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


class TestEffectiveForbiddenWords:
    def test_merges_builtin_and_custom(self):
        words = get_effective_forbidden_words({
            "forbidden_words": ["remix", "live"],
            "forbidden_words_custom": ["8d audio", "speed up"],
        })
        assert words == ["remix", "live", "8d audio", "speed up"]

    def test_normalizes_case_and_whitespace(self):
        # User-configured words with stray casing/whitespace must still match
        # the lower-cased YouTube titles the filter is applied to.
        words = get_effective_forbidden_words({
            "forbidden_words": ["  ReMix ", "LIVE"],
            "forbidden_words_custom": ["  Nightcore"],
        })
        assert words == ["remix", "live", "nightcore"]

    def test_dedupes_across_lists(self):
        words = get_effective_forbidden_words({
            "forbidden_words": ["remix", "cover"],
            "forbidden_words_custom": ["remix", "Cover", "bootleg"],
        })
        assert words == ["remix", "cover", "bootleg"]

    def test_missing_builtin_falls_back_to_default(self):
        words = get_effective_forbidden_words(
            {"forbidden_words_custom": ["foo"]}
        )
        assert "remix" in words and "foo" in words

    def test_non_list_values_are_tolerated(self):
        # A null/garbage value must not crash the search path.
        words = get_effective_forbidden_words({
            "forbidden_words": None,
            "forbidden_words_custom": None,
        })
        assert "remix" in words

    def test_custom_word_is_applied_by_check_forbidden(self):
        # End-to-end: a custom word makes _check_forbidden reject a title.
        words = get_effective_forbidden_words({
            "forbidden_words": [],
            "forbidden_words_custom": ["hardstyle"],
        })
        assert _check_forbidden(
            "track hardstyle edit", "track", words
        ) == "hardstyle"


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
        # Both entries are from the artist's official channel so Phase 1
        # accepts both and the ranking can be asserted.
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Artist - Track B",
                    "url": "url_b",
                    "duration": 200,
                    "channel": "Artist",
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


class TestYouTubeMusicSourceAcceptedWithoutChannel:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_with_artists_field_accepted_in_phase1(
        self, mock_config, mock_ydl_class,
    ):
        # YT Music entries with the structured ``artists`` field credit the
        # song to a specific artist; that's enough to prove this is an
        # artist-official catalogue hit even when channel is empty.
        mock_config.return_value = {
            "forbidden_words": ["remix", "live"],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg",
                    "url": "ytmusic_video_id",
                    "duration": 207,
                    "channel": "",
                    "artists": [{"name": "Zaho"}],
                    "view_count": 0,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=207000,
        )
        urls = [c["url"] for c in candidates]
        assert "ytmusic_video_id" in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_without_any_artist_signal_accepted_in_phase1(
        self, mock_config, mock_ydl_class,
    ):
        # yt-dlp's extract_flat on music.youtube can return bare entries
        # with no channel/uploader/artists field. Phase 1 must still accept
        # these (lenient) because YT Music is by definition an official
        # catalogue; explicit-mismatch detection prevents wrong-artist hits.
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg",
                    "url": "bare_ytmusic_id",
                    "duration": 207,
                    "view_count": 0,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=207000,
        )
        urls = [c["url"] for c in candidates]
        assert "bare_ytmusic_id" in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_with_explicit_wrong_artist_rejected_in_phase1(
        self, mock_config, mock_ydl_class,
    ):
        # ytmusic entry crediting a different artist is rejected outright
        # in phase 1 (explicit mismatch on the ``artists`` field).
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg",
                    "url": "wrong_artist_id",
                    "duration": 207,
                    "artists": [{"name": "Different Person"}],
                    "view_count": 10_000_000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=207000,
        )
        # The entry can still surface in phase 2 (fallback) but with a
        # large artist-mismatch penalty in score.
        for c in candidates:
            if c["url"] == "wrong_artist_id":
                assert c["score"] < 0.80

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_with_topic_uploader_accepted_in_phase1(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": ["remix", "live"],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg",
                    "url": "yt_topic_uploader",
                    "duration": 207,
                    "uploader": "Zaho - Topic",
                    "view_count": 0,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=207000,
        )
        urls = [c["url"] for c in candidates]
        assert "yt_topic_uploader" in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_entry_with_wrong_artists_field_penalised(
        self, mock_config, mock_ydl_class,
    ):
        # When YT Music explicitly credits a different artist, the entry
        # may still surface (phase 2 fallback) but its score must take a
        # significant artist-mismatch hit so it never beats a correct hit.
        mock_config.return_value = {
            "forbidden_words": [],
            "duration_tolerance": 10,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg",
                    "url": "wrong_artist_url",
                    "duration": 207,
                    "artists": [{"name": "Some Other Artist"}],
                    "view_count": 10_000_000,
                },
                {
                    "title": "Iceberg",
                    "url": "right_artist_url",
                    "duration": 207,
                    "artists": [{"name": "Zaho"}],
                    "view_count": 100,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=207000,
        )
        assert candidates
        assert candidates[0]["url"] == "right_artist_url"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ytmusic_remix_blocked_by_forbidden_words(
        self, mock_config, mock_ydl_class,
    ):
        # ytmusic source alone shouldn't exempt remixes/covers when the
        # channel name doesn't confirm the artist's official source.
        mock_config.return_value = {
            "forbidden_words": ["remix"],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Iceberg (Some DJ Remix)",
                    "url": "remix_url",
                    "duration": 200,
                    "channel": "",
                    "view_count": 1000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Zaho Iceberg", "Iceberg", expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "remix_url" not in urls


class TestOfficialChannelForbiddenExemption:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_official_channel_live_in_title_still_accepted(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "forbidden_words": ["live"],
            "duration_tolerance": 15,
            "yt_player_client": "android",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                {
                    "title": "Chelsea Smile (Live at Wembley)",
                    "url": "official_url",
                    "duration": 200,
                    "channel": "Bring Me The Horizon",
                    "view_count": 500000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Bring Me The Horizon Chelsea Smile official audio",
            "Chelsea Smile",
            expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "official_url" in urls


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


class TestTitleScoreFloor:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_wrong_song_same_artist_rejected(
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
                    "title": "Won't Go Home Without You (Official Music Video)",
                    "url": "wrong_song_id",
                    "duration": None,
                    "channel": "Maroon 5",
                    "view_count": 0,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Maroon 5 Good at Being Gone official audio", "Good at Being Gone",
            expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "wrong_song_id" not in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_artist_match_but_different_track_rejected(
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
                    "title": "Maroon 5 - Nothing Lasts Forever (Lyrics)",
                    "url": "wrong_id",
                    "duration": 200,
                    "channel": "Maroon 5",
                    "view_count": 500000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Maroon 5 Everyday Goodbyes official audio", "Everyday Goodbyes",
            expected_duration_ms=200000,
        )
        urls = [c["url"] for c in candidates]
        assert "wrong_id" not in urls

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_track_title_in_yt_title_accepted(
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
                    "title": "Max Cooper - Pattern Index",
                    "url": "right_id",
                    "duration": 381,
                    "channel": "Max Cooper - Topic",
                    "view_count": 100000,
                },
            ]
        }
        candidates = search_youtube_candidates(
            "Max Cooper Pattern Index official audio", "Pattern Index",
            expected_duration_ms=381000,
        )
        urls = [c["url"] for c in candidates]
        assert "right_id" in urls


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


class TestYtdlpFormatOverride:
    @staticmethod
    def _formats_tried(mock_ydl_class):
        formats = []
        for call in mock_ydl_class.call_args_list:
            opts = call.args[0] if call.args else call.kwargs
            if isinstance(opts, dict) and "format" in opts:
                formats.append(opts["format"])
        return formats

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_custom_format_tried_first(self, mock_config, mock_ydl_class):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "m4a",
            "audio_quality": "320",
            "ytdlp_format": "141",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "requested format is not available"
        )
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytsearch",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        formats = self._formats_tried(mock_ydl_class)
        assert formats, "expected at least one format selector to be tried"
        # The override is attempted first, before the built-in fallbacks.
        assert formats[0] == "141"

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_no_override_uses_builtin_selectors(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android",
            "audio_format": "mp3",
            "audio_quality": "320",
            "ytdlp_format": "",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "requested format is not available"
        )
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "ytsearch",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        formats = self._formats_tried(mock_ydl_class)
        assert "141" not in formats
        assert formats[0] == "bestaudio/best"


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


class TestMatchAlbumTrack:
    def test_returns_candidate_for_exact_title_match(self):
        entries = [
            {
                "url": "https://music.youtube.com/watch?v=abc",
                "title": "Iceberg",
                "duration": 207,
                "channel": "Zaho",
            },
            {
                "url": "https://music.youtube.com/watch?v=def",
                "title": "Stockholm",
                "duration": 182,
                "channel": "Zaho",
            },
        ]
        cand = match_album_track(
            entries, "Iceberg", expected_duration_ms=207000,
        )
        assert cand is not None
        assert cand["url"] == "https://music.youtube.com/watch?v=abc"
        assert cand["score"] == 1.0
        assert cand["from_album_playlist"] is True

    def test_returns_none_when_no_similar_title(self):
        entries = [
            {
                "url": "u",
                "title": "Completely Different",
                "duration": 200,
                "channel": "A",
            },
        ]
        cand = match_album_track(
            entries, "Iceberg", expected_duration_ms=200000,
        )
        assert cand is None

    def test_rejects_entry_with_far_duration(self):
        entries = [
            {
                "url": "u",
                "title": "Iceberg",
                "duration": 400,
                "channel": "Zaho",
            },
        ]
        cand = match_album_track(
            entries, "Iceberg", expected_duration_ms=200000,
        )
        assert cand is None

    def test_empty_entries_returns_none(self):
        assert match_album_track([], "x") is None

    def test_partial_title_match_still_accepts(self):
        entries = [
            {
                "url": "u",
                "title": "Iceberg (feat. X)",
                "duration": 210,
                "channel": "Zaho",
            },
        ]
        cand = match_album_track(
            entries, "Iceberg", expected_duration_ms=210000,
        )
        assert cand is not None


class TestFindAlbumOnYtmusic:
    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_returns_none_when_no_playlist_id_in_results(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.return_value = {
            "entries": [{"id": "abc123", "title": "song"}],
        }
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is None

    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_finds_album_via_url_with_list_param(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [
            {
                "entries": [
                    {
                        "url": (
                            "https://music.youtube.com/playlist"
                            "?list=OLAK5uy_xyz"
                        ),
                    },
                ],
            },
            {
                "entries": [
                    {
                        "id": "vid1",
                        "title": "Iceberg",
                        "duration": 207,
                        "uploader": "Zaho",
                    },
                    {
                        "id": "vid2",
                        "title": "Stockholm",
                        "duration": 182,
                        "uploader": "Zaho",
                    },
                ],
            },
        ]
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is not None
        assert result["playlist_id"] == "OLAK5uy_xyz"
        assert "playlist?list=OLAK5uy_xyz" in result["playlist_url"]
        assert len(result["entries"]) == 2
        assert result["entries"][0]["title"] == "Iceberg"
        assert "watch?v=vid1" in result["entries"][0]["url"]

    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_albums_filter_url_is_attempted_first(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [
            {"entries": [{"id": "OLAK5uy_filtered"}]},
            {"entries": [{"id": "v", "title": "T", "duration": 100}]},
        ]
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is not None
        assert result["playlist_id"] == "OLAK5uy_filtered"
        first_url = ydl.extract_info.call_args_list[0].args[0]
        assert "sp=" in first_url

    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_finds_album_via_recursive_scan(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        # YT Music sometimes nests album shelves in structured fields;
        # the recursive scan must dig into nested dicts/lists.
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [
            {
                "sections": [
                    {"shelf": "Songs", "items": []},
                    {
                        "shelf": "Albums",
                        "items": [
                            {
                                "album": {
                                    "browse_id": "OLAK5uy_nested",
                                    "title": "VERSATILE",
                                },
                            },
                        ],
                    },
                ],
            },
            {"entries": [{"id": "v", "title": "T", "duration": 100}]},
        ]
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is not None
        assert result["playlist_id"] == "OLAK5uy_nested"

    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_finds_album_via_direct_id_field(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.side_effect = [
            {"entries": [{"id": "OLAK5uy_abc"}]},
            {
                "entries": [
                    {"id": "vid1", "title": "A", "duration": 100},
                ],
            },
        ]
        result = find_album_on_ytmusic("Artist", "Album")
        assert result is not None
        assert result["playlist_id"] == "OLAK5uy_abc"

    @patch("downloader._find_album_via_ytmusicapi", return_value=None)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_returns_none_when_extract_raises(
        self, mock_cfg, mock_ydl_cls, _mock_api,
    ):
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.side_effect = Exception("network")
        result = find_album_on_ytmusic("Artist", "Album")
        assert result is None

    def test_returns_none_for_empty_inputs(self):
        assert find_album_on_ytmusic("", "x") is None
        assert find_album_on_ytmusic("x", "") is None

    @patch("downloader._ytmusicapi_client")
    def test_ytmusicapi_resolves_album_directly(self, mock_client):
        # ytmusicapi returns OLAK5uy_ playlistId from search(albums) and the
        # full track list from get_album(browseId). yt-dlp is never called.
        yt = MagicMock()
        yt.search.return_value = [
            {
                "resultType": "album",
                "browseId": "MPREb_xyz",
                "playlistId": "OLAK5uy_realalbum",
                "title": "VERSATILE",
                "artist": "Zaho",
            },
        ]
        yt.get_album.return_value = {
            "audioPlaylistId": "OLAK5uy_realalbum",
            "tracks": [
                {
                    "videoId": "vid_iceberg",
                    "title": "Iceberg",
                    "duration": "3:27",
                    "artists": [{"name": "Zaho"}],
                },
                {
                    "videoId": "vid_stockholm",
                    "title": "Stockholm",
                    "duration": "3:02",
                    "artists": [{"name": "Zaho"}],
                },
            ],
        }
        mock_client.return_value = yt
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is not None
        assert result["playlist_id"] == "OLAK5uy_realalbum"
        assert len(result["entries"]) == 2
        assert result["entries"][0]["url"].endswith("v=vid_iceberg")
        assert result["entries"][0]["duration"] == 207
        assert result["entries"][1]["duration"] == 182

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    @patch("downloader._ytmusicapi_client")
    def test_ytmusicapi_rejects_wrong_artist_album(
        self, mock_client, mock_cfg, mock_ydl_cls,
    ):
        # Album results from ytmusicapi may include a homonym album by
        # another artist; the picker must require an artist-field match.
        # yt-dlp fallback is mocked to also return nothing so the test
        # avoids real network calls.
        mock_cfg.return_value = {"yt_player_client": "android"}
        ydl = mock_ydl_cls.return_value.__enter__.return_value
        ydl.extract_info.return_value = {"entries": []}
        yt = MagicMock()
        yt.search.return_value = [
            {
                "resultType": "album",
                "browseId": "MPREb_other",
                "playlistId": "OLAK5uy_otherartist",
                "title": "VERSATILE",
                "artist": "Different Person",
            },
        ]
        mock_client.return_value = yt
        result = find_album_on_ytmusic("Zaho", "VERSATILE")
        assert result is None

    @patch("downloader._ytmusicapi_client", return_value=None)
    def test_ytmusicapi_unavailable_falls_back_to_ytdlp(self, _mock_client):
        # When ytmusicapi is not installed _ytmusicapi_client returns None;
        # find_album_on_ytmusic must continue to the yt-dlp fallback path
        # rather than raising or returning eagerly.
        with patch("downloader.yt_dlp.YoutubeDL") as mock_ydl_cls, \
             patch("downloader.load_config") as mock_cfg:
            mock_cfg.return_value = {"yt_player_client": "android"}
            ydl = mock_ydl_cls.return_value.__enter__.return_value
            ydl.extract_info.side_effect = [
                {"entries": [{"id": "OLAK5uy_fb"}]},
                {"entries": [{"id": "v", "title": "T", "duration": 100}]},
            ]
            result = find_album_on_ytmusic("A", "B")
            assert result is not None
            assert result["playlist_id"] == "OLAK5uy_fb"


def _called_clients(mock_ydl_class):
    """Flatten the player_client used in each YoutubeDL(...) construction."""
    clients = []
    for call in mock_ydl_class.call_args_list:
        opts = call.args[0] if call.args else call.kwargs
        if not isinstance(opts, dict):
            continue
        yt_args = opts.get("extractor_args", {}).get("youtube", {})
        pc = yt_args.get("player_client", [None])
        clients.extend(pc if isinstance(pc, list) else [pc])
    return clients


class TestPoTokenClientPriority:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_web_client_prioritized_when_po_token_set(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "m4a",
            "audio_quality": "320", "yt_po_token": "TOKEN123",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("requested format is not available")
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "youtube",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        clients = _called_clients(mock_ydl_class)
        web_idx = next((i for i, c in enumerate(clients) if c == "web"), -1)
        android_idx = next(
            (i for i, c in enumerate(clients) if c == "android"), -1
        )
        assert web_idx != -1
        assert android_idx == -1 or web_idx < android_idx

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_client_order_unchanged_without_po_token(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("requested format is not available")
        candidate = {
            "url": "abc12345678", "title": "t", "duration": 200,
            "score": 0.9, "source": "youtube",
        }
        download_youtube_candidate(candidate, "/tmp/out")
        clients = _called_clients(mock_ydl_class)
        web_idx = next((i for i, c in enumerate(clients) if c == "web"), -1)
        android_idx = next(
            (i for i, c in enumerate(clients) if c == "android"), -1
        )
        # Default order: the configured default (android) comes before web.
        assert android_idx != -1 and android_idx < web_idx

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_success_logs_player_client(
        self, mock_config, mock_ydl_class, caplog,
    ):
        import logging
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "m4a",
            "audio_quality": "320",
        }
        mock_ydl_class.return_value.__enter__.return_value.download.return_value = None
        candidate = {
            "url": "abc12345678", "title": "MyTrack", "duration": 200,
            "score": 0.9, "source": "youtube",
        }
        with caplog.at_level(logging.INFO, logger="downloader"):
            result = download_youtube_candidate(candidate, "/tmp/out")
        assert result["success"] is True
        assert any(
            "player_client" in r.message and "MyTrack" in r.message
            for r in caplog.records
        )
