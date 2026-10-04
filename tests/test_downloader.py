import os
from unittest.mock import MagicMock, patch

import pytest

import downloader
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
    list_video_formats,
    match_album_track,
    search_youtube_candidates,
)


@pytest.fixture(autouse=True)
def _reset_ffmpeg_pp_state():
    # download_youtube_candidate caches whether ffmpeg can write output.
    # Force "works" around every test so the normal path is exercised
    # without running a real ffmpeg probe (individual tests override it),
    # and so the cached state can't leak between tests or test modules.
    # Also mark yt-dlp plugins as already loaded so the quiet preload is a
    # no-op and never touches the real yt-dlp during tests.
    downloader._ffmpeg_pp_state = True
    downloader._ffmpeg_pp_observed_broken = False
    downloader._plugins_preloaded = True
    yield
    downloader._ffmpeg_pp_state = True
    downloader._ffmpeg_pp_observed_broken = False


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

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_postprocess_error_flags_and_bounds_retries(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Postprocessing: audio conversion failed:"
            " Error opening output files: Function not implemented"
        )
        candidate = {
            "url": "u", "title": "t", "duration": 200, "score": 0.9,
        }
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False
        assert result.get("postprocess_error") is True
        assert "postprocessing" in result["error_message"].lower()
        assert mock_ydl.download.call_count <= 3

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_cascade_stops_at_the_first_conversion_error(
        self, mock_config, mock_ydl_class,
    ):
        # One failure is proof enough: neither the player client nor the
        # format selector can make ffmpeg able to write its output, so the
        # remaining combinations must not be tried (they only produce
        # identical error lines).
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Postprocessing: Error opening output files:"
            " Function not implemented"
        )
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result.get("postprocess_error") is True
        assert mock_ydl.download.call_count == 1

    def test_is_postprocess_error_classifier(self):
        from downloader import _is_postprocess_error
        assert _is_postprocess_error(
            "error opening output files: function not implemented"
        )
        assert _is_postprocess_error("postprocessing: audio conversion failed")
        assert _is_postprocess_error("conversion failed")
        assert not _is_postprocess_error("http error 403 forbidden")
        assert not _is_postprocess_error("requested format is not available")

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_raw_fallback_when_ffmpeg_postprocessing_unavailable(
        self, mock_config, mock_ydl_class, tmp_path,
    ):
        import os
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "m4a",
        }
        out = str(tmp_path / "output")
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        calls = {"n": 0}

        def _dl(urls):
            calls["n"] += 1
            if calls["n"] <= 3:
                raise Exception(
                    "ERROR: Postprocessing: audio conversion failed:"
                    " Error opening output files: Function not implemented"
                )
            with open(out + ".m4a", "wb") as fh:
                fh.write(b"\x00\x00\x00\x00")
            return 0

        mock_ydl.download.side_effect = _dl
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, out)
        assert result["success"] is True
        assert result.get("postprocess_error") is None
        assert os.path.exists(out + ".m4a")

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_raw_fallback_skipped_for_mp3_target(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Postprocessing: Error opening output files:"
            " Function not implemented"
        )
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False
        assert result.get("postprocess_error") is True
        assert mock_ydl.download.call_count <= 3

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_early_raw_path_when_ffmpeg_known_broken(
        self, mock_config, mock_ydl_class, tmp_path,
    ):
        import os
        downloader._ffmpeg_pp_state = False
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "m4a",
        }
        out = str(tmp_path / "output")
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value

        def _dl(urls):
            with open(out + ".m4a", "wb") as fh:
                fh.write(b"\x00\x00")
            return 0

        mock_ydl.download.side_effect = _dl
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, out)
        assert result["success"] is True
        assert mock_ydl.download.call_count == 1
        assert os.path.exists(out + ".m4a")


class TestFfmpegProbe:
    @patch("downloader.subprocess.run")
    def test_probe_broken_when_ffmpeg_returns_error(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1)
        assert downloader._probe_ffmpeg_can_write_audio(None) is False

    @patch("downloader.subprocess.run")
    def test_probe_broken_when_no_output_file(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0)
        assert downloader._probe_ffmpeg_can_write_audio(None) is False

    @patch("downloader.subprocess.run")
    def test_probe_ok_when_ffmpeg_writes_file(self, mock_run, tmp_path):
        def _run(cmd, **kw):
            with open(cmd[-1], "wb") as fh:
                fh.write(b"\x00" * 64)
            return MagicMock(returncode=0)
        mock_run.side_effect = _run
        assert downloader._probe_ffmpeg_can_write_audio(str(tmp_path)) is True

    @patch("downloader.subprocess.run")
    def test_probe_uses_the_same_flags_as_yt_dlp(self, mock_run, tmp_path):
        # Regression guard: yt-dlp appends "-movflags +faststart" to every
        # output it writes. A probe without it exercises a different code
        # path and passed on a host where the real conversion failed,
        # which let the whole doomed cascade run for every track.
        mock_run.return_value = MagicMock(returncode=1)
        downloader._probe_ffmpeg_can_write_audio(str(tmp_path))
        cmd = mock_run.call_args[0][0]
        assert "-movflags" in cmd
        assert cmd[cmd.index("-movflags") + 1] == "+faststart"
        assert cmd[-1].endswith(".m4a")

    @patch("downloader.subprocess.run")
    def test_postprocess_works_caches_probe(self, mock_run):
        downloader._ffmpeg_pp_state = None
        mock_run.return_value = MagicMock(returncode=1)
        assert downloader._ffmpeg_postprocess_works(None) is False
        assert downloader._ffmpeg_postprocess_works(None) is False
        assert mock_run.call_count == 1


class TestPluginPreload:
    def test_preload_runs_once_and_mutes_stderr(self, capsys):
        downloader._plugins_preloaded = False
        fake_plugins = MagicMock()
        try:
            with patch.object(downloader, "yt_dlp") as mock_ytdlp:
                def _noisy():
                    import sys
                    sys.stderr.write("PoTokenProvider BgUtilHTTP already registered\n")
                mock_ytdlp.plugins = fake_plugins
                fake_plugins.load_all_plugins.side_effect = _noisy
                downloader.preload_ytdlp_plugins_quietly()
                downloader.preload_ytdlp_plugins_quietly()
        finally:
            downloader._plugins_preloaded = True
        assert fake_plugins.load_all_plugins.call_count == 1
        assert "already registered" not in capsys.readouterr().err


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
        # The override leads the first selector with a slash-fallback, so a
        # video that doesn't expose it falls back to best audio within the
        # same request instead of wasting a full sweep of every client.
        assert formats[0].startswith("141/")
        assert "bestaudio" in formats[0]

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_custom_format_promotes_web_clients(
        self, mock_config, mock_ydl_class,
    ):
        # Premium formats (e.g. 141) are only exposed to the web-family
        # clients, so an override must put web before the configured
        # android default.
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
        clients = _called_clients(mock_ydl_class)
        web_idx = next((i for i, c in enumerate(clients) if c == "web"), -1)
        android_idx = next(
            (i for i, c in enumerate(clients) if c == "android"), -1
        )
        assert web_idx != -1 and android_idx != -1
        assert web_idx < android_idx

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


class TestListVideoFormats:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_filters_video_only_and_sorts(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "title": "Some Song",
            "formats": [
                {"format_id": "137", "ext": "mp4",
                 "vcodec": "avc1", "acodec": "none"},  # video-only -> dropped
                {"format_id": "140", "ext": "m4a", "vcodec": "none",
                 "acodec": "mp4a.40.2", "abr": 128, "filesize": 1048576},
                {"format_id": "141", "ext": "m4a", "vcodec": "none",
                 "acodec": "mp4a.40.2", "abr": 256},
                {"format_id": "18", "ext": "mp4", "vcodec": "avc1",
                 "acodec": "mp4a.40.2", "abr": 96},  # muxed audio+video
            ],
        }
        res = list_video_formats(
            "https://www.youtube.com/watch?v=abcdefghijk"
        )
        ids = [f["format_id"] for f in res["formats"]]
        # Video-only 137 dropped; audio-only first (141, 140 by bitrate),
        # then the muxed audio+video stream.
        assert ids == ["141", "140", "18"]
        assert res["title"] == "Some Song"
        assert res["formats"][0]["audio_only"] is True
        assert res["formats"][0]["abr"] == 256
        assert res["formats"][2]["audio_only"] is False

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_uses_same_client_chain_as_download_path(
        self, mock_config, mock_ydl_class,
    ):
        # The lister must fall back through the download path's exact client
        # chain (after the default-clients attempt), so it can't recommend a
        # format from a client the real download never tries.
        from downloader import _client_fallback_chain
        cfg = {"yt_player_client": "android"}
        mock_config.return_value = cfg
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"title": "x", "formats": []}
        list_video_formats("https://www.youtube.com/watch?v=abcdefghijk")
        tried = _called_clients(mock_ydl_class)
        assert tried == [None] + _client_fallback_chain(cfg)

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_lists_without_format_selection_error(
        self, mock_config, mock_ydl_class,
    ):
        # Must set ignore_no_formats_error so a video that only exposes split
        # DASH streams lists its formats (full processing, like `yt-dlp -F`)
        # instead of raising "Requested format is not available".
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "title": "x",
            "formats": [{"format_id": "140", "ext": "m4a", "vcodec": "none",
                         "acodec": "mp4a", "abr": 128}],
        }
        list_video_formats("abcdefghijk")
        opts = mock_ydl_class.call_args.args[0]
        assert opts.get("ignore_no_formats_error") is True

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_tries_default_clients_first(self, mock_config, mock_ydl_class):
        # The first attempt forces no player_client, so yt-dlp uses its own
        # defaults (what `yt-dlp -F` does) rather than the format-starved
        # android client.
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "title": "x",
            "formats": [{"format_id": "140", "ext": "m4a", "vcodec": "none",
                         "acodec": "mp4a", "abr": 128}],
        }
        list_video_formats("abcdefghijk")
        first_opts = mock_ydl_class.call_args_list[0].args[0]
        extractor_args = first_opts.get("extractor_args", {})
        assert "youtube" not in extractor_args or (
            "player_client" not in extractor_args.get("youtube", {})
        )

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_falls_back_to_next_client_when_no_formats(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.side_effect = [
            {"title": "x", "formats": []},  # default client: nothing usable
            {"title": "x", "formats": [  # next client succeeds
                {"format_id": "140", "ext": "m4a", "vcodec": "none",
                 "acodec": "mp4a", "abr": 128}]},
        ]
        res = list_video_formats("abcdefghijk")
        assert [f["format_id"] for f in res["formats"]] == ["140"]
        assert mock_ydl.extract_info.call_count == 2

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_playlist_uses_first_entry(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                None,
                {"title": "first", "formats": [
                    {"format_id": "140", "ext": "m4a", "vcodec": "none",
                     "acodec": "mp4a", "abr": 128}]},
            ],
        }
        res = list_video_formats("https://youtube.com/playlist?list=PL")
        assert res["title"] == "first"
        assert [f["format_id"] for f in res["formats"]] == ["140"]


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


def test_format_source_quality():
    from downloader import _format_source_quality
    assert _format_source_quality(
        {"format_id": "140", "ext": "m4a", "abr": 128}
    ) == "140 · m4a · 128 kbps"
    assert _format_source_quality(
        {"format_id": "251", "ext": "webm", "abr": 143.6}
    ) == "251 · webm · 144 kbps"
    assert _format_source_quality({}) == ""
    assert _format_source_quality({"ext": "opus"}) == "opus"
    assert _format_source_quality({"format_id": "140", "abr": None}) == "140"


class TestFfmpegStatus:
    """The probe result turned into something the user can act on."""

    def _cfg(self, fmt):
        return {"audio_format": fmt, "yt_player_client": "android"}

    @patch("downloader.load_config")
    def test_working_ffmpeg_still_reports_a_verdict(self, mock_config):
        mock_config.return_value = self._cfg("mp3")
        downloader._ffmpeg_pp_state = True
        st = downloader.ffmpeg_status()
        assert st["ok"] is True
        assert st["downloads_work"] is True
        assert st["summary"]
        assert st["detail"]
        assert "fixes" not in st
        assert "impact" not in st

    @patch("downloader.load_config")
    def test_broken_with_native_format_still_downloads(self, mock_config):
        mock_config.return_value = self._cfg("m4a")
        downloader._ffmpeg_pp_state = False
        st = downloader.ffmpeg_status()
        assert st["ok"] is False
        assert st["downloads_work"] is True
        assert "ENOSYS" in st["detail"]
        assert len(st["fixes"]) == 1
        assert "architecture" in st["fixes"][0]

    @patch("downloader.load_config")
    def test_broken_with_mp3_offers_the_in_app_fix_first(self, mock_config):
        mock_config.return_value = self._cfg("mp3")
        downloader._ffmpeg_pp_state = False
        st = downloader.ffmpeg_status()
        assert st["downloads_work"] is False
        assert "mp3" in st["impact"]
        assert "m4a" in st["fixes"][0]
        assert len(st["fixes"]) == 2

    @patch("downloader.subprocess.run")
    @patch("downloader.load_config")
    def test_refresh_reruns_the_probe(self, mock_config, mock_run, tmp_path):
        mock_config.return_value = self._cfg("m4a")
        downloader._ffmpeg_pp_state = True
        mock_run.return_value = MagicMock(returncode=1)
        assert downloader.ffmpeg_status(str(tmp_path))["ok"] is True
        assert mock_run.call_count == 0
        assert downloader.ffmpeg_status(
            str(tmp_path), refresh=True
        )["ok"] is False
        assert mock_run.call_count == 1


class TestObservedFailureIsAuthoritative:
    """A failed real conversion outranks the probe's approximation."""

    def _cfg(self):
        return {"audio_format": "m4a", "yt_player_client": "android"}

    @patch("downloader._run_ffmpeg")
    @patch("downloader.load_config")
    def test_recheck_cannot_clear_an_observed_failure(
        self, mock_config, mock_ffmpeg, tmp_path,
    ):
        # The reported bug: Re-check reported "works" after a download had
        # already proved otherwise, then went red again on the next song.
        mock_config.return_value = self._cfg()
        downloader._mark_ffmpeg_postprocess_broken()

        def _pass(args, timeout=30):
            open(args[-1], "wb").write(b"\0" * 64)
            return MagicMock(returncode=0)

        mock_ffmpeg.side_effect = _pass
        st = downloader.ffmpeg_status(str(tmp_path), refresh=True)
        assert st["ok"] is False
        assert st["observed"] is True
        assert "measured, not predicted" in st["detail"]

    @patch("downloader._run_ffmpeg")
    @patch("downloader.load_config")
    def test_recheck_still_works_without_an_observed_failure(
        self, mock_config, mock_ffmpeg, tmp_path,
    ):
        mock_config.return_value = self._cfg()
        downloader._ffmpeg_pp_state = False

        def _pass(args, timeout=30):
            open(args[-1], "wb").write(b"\0" * 64)
            return MagicMock(returncode=0)

        mock_ffmpeg.side_effect = _pass
        st = downloader.ffmpeg_status(str(tmp_path), refresh=True)
        assert st["ok"] is True
        assert st["observed"] is False


class TestProbeCoversBothFfmpegShapes:
    @patch("downloader._run_ffmpeg")
    def test_a_failing_stream_copy_is_caught(self, mock_ffmpeg, tmp_path):
        # A native YouTube m4a is stream-copied, not encoded. A host that
        # can encode but not copy must not be reported as healthy.
        calls = {"n": 0}

        def _run(args, timeout=30):
            calls["n"] += 1
            if "copy" in args:
                return MagicMock(returncode=1)
            open(args[-1], "wb").write(b"\0" * 64)
            return MagicMock(returncode=0)

        mock_ffmpeg.side_effect = _run
        assert downloader._probe_ffmpeg_can_write_audio(str(tmp_path)) is False
        assert calls["n"] == 2

    @patch("downloader._run_ffmpeg")
    def test_both_shapes_passing_reports_healthy(self, mock_ffmpeg, tmp_path):
        def _run(args, timeout=30):
            open(args[-1], "wb").write(b"\0" * 64)
            return MagicMock(returncode=0)

        mock_ffmpeg.side_effect = _run
        assert downloader._probe_ffmpeg_can_write_audio(str(tmp_path)) is True


def _search_ydl(entries):
    def _factory(opts):
        ydl = MagicMock()
        ydl.__enter__.return_value.extract_info.return_value = (
            {"entries": entries} if entries is not None else None
        )
        return ydl
    return _factory


_SEARCH_CFG = {
    "forbidden_words": [],
    "duration_tolerance": 15,
    "yt_player_client": "android",
}


class TestLoudnessNormalization:
    def _cfg(self, fmt):
        return {
            "yt_player_client": "android",
            "audio_format": fmt,
            "audio_quality": "320",
            "audio_normalize": True,
        }

    def _write_download(self, mock_ydl_class, target):
        def _dl(urls):
            with open(target, "wb") as fh:
                fh.write(b"raw")
            return 0
        mock_ydl_class.return_value.__enter__.return_value.download \
            .side_effect = _dl

    @pytest.mark.parametrize("fmt,encoder", [
        ("mp3", "libmp3lame"), ("m4a", "aac"), ("opus", "libopus"),
    ])
    @patch("downloader._run_ffmpeg")
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_loudnorm_runs_as_a_separate_reencode_pass(
        self, mock_config, mock_ydl_class, mock_ffmpeg, tmp_path,
        fmt, encoder,
    ):
        import os
        mock_config.return_value = self._cfg(fmt)
        out = str(tmp_path / "output")
        self._write_download(mock_ydl_class, f"{out}.{fmt}")

        def _ff(args, timeout=30):
            with open(args[-1], "wb") as fh:
                fh.write(b"normalized")
            return MagicMock(returncode=0, stderr="")

        mock_ffmpeg.side_effect = _ff
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, out)
        assert result["success"] is True
        for call in mock_ydl_class.call_args_list:
            assert "postprocessor_args" not in call[0][0]
        args = mock_ffmpeg.call_args[0][0]
        assert any("loudnorm" in a for a in args)
        assert args[args.index("-c:a") + 1] == encoder
        assert args[-1] != f"{out}.{fmt}"
        with open(f"{out}.{fmt}", "rb") as fh:
            assert fh.read() == b"normalized"
        assert os.listdir(tmp_path) == [f"output.{fmt}"]

    @patch("downloader._run_ffmpeg")
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_failed_normalization_keeps_file_and_host_healthy(
        self, mock_config, mock_ydl_class, mock_ffmpeg, tmp_path,
    ):
        import os
        mock_config.return_value = self._cfg("opus")
        out = str(tmp_path / "output")
        self._write_download(mock_ydl_class, out + ".opus")

        def _ff(args, timeout=30):
            with open(args[-1], "wb") as fh:
                fh.write(b"partial")
            return MagicMock(
                returncode=1,
                stderr="Error opening output files: Function not implemented",
            )

        mock_ffmpeg.side_effect = _ff
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, out)
        assert result["success"] is True
        with open(out + ".opus", "rb") as fh:
            assert fh.read() == b"raw"
        assert os.listdir(tmp_path) == ["output.opus"]
        assert downloader._ffmpeg_pp_observed_broken is False
        assert downloader._ffmpeg_pp_state is True

    @patch("downloader._run_ffmpeg")
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_normalization_timeout_keeps_file(
        self, mock_config, mock_ydl_class, mock_ffmpeg, tmp_path,
    ):
        import subprocess
        mock_config.return_value = self._cfg("mp3")
        out = str(tmp_path / "output")
        self._write_download(mock_ydl_class, out + ".mp3")
        mock_ffmpeg.side_effect = subprocess.TimeoutExpired("ffmpeg", 1)
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, out)
        assert result["success"] is True
        with open(out + ".mp3", "rb") as fh:
            assert fh.read() == b"raw"
        assert downloader._ffmpeg_pp_observed_broken is False

    @patch("downloader._run_ffmpeg")
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_no_pass_when_normalization_disabled(
        self, mock_config, mock_ydl_class, mock_ffmpeg, tmp_path,
    ):
        cfg = self._cfg("mp3")
        cfg["audio_normalize"] = False
        mock_config.return_value = cfg
        out = str(tmp_path / "output")
        self._write_download(mock_ydl_class, out + ".mp3")
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        assert download_youtube_candidate(candidate, out)["success"] is True
        assert mock_ffmpeg.call_count == 0


class TestOpusIsNotNative:
    @patch("downloader.yt_dlp.YoutubeDL")
    def test_raw_fallback_refuses_opus(self, mock_ydl_class, tmp_path):
        assert "opus" not in downloader.NATIVE_AUDIO_FORMATS
        result = downloader._download_raw_audio(
            {"url": "u"}, str(tmp_path / "o"), "opus", False,
            {"yt_player_client": "android"},
        )
        assert result is None
        assert mock_ydl_class.call_count == 0

    @patch("downloader.load_config")
    def test_status_says_opus_needs_ffmpeg(self, mock_config):
        mock_config.return_value = {"audio_format": "opus"}
        downloader._ffmpeg_pp_state = False
        st = downloader.ffmpeg_status()
        assert st["downloads_work"] is False
        assert "opus" in st["impact"]
        assert "m4a" in st["fixes"][0]
        assert "opus" not in st["fixes"][0]
        assert st["native_formats"] == ["m4a"]

    @patch("downloader.load_config")
    def test_status_for_mp3_does_not_suggest_opus(self, mock_config):
        mock_config.return_value = {"audio_format": "mp3"}
        downloader._ffmpeg_pp_state = False
        st = downloader.ffmpeg_status()
        assert "opus" not in st["fixes"][0]


class TestConversionErrorNeedsHostEvidence:
    _BAD_SOURCE = (
        "ERROR: Postprocessing: audio conversion failed:"
        " /dl/temp_01.webm: Invalid data found when processing input"
    )

    @patch("downloader._probe_ffmpeg_can_write_audio", return_value=True)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_source_specific_error_only_fails_this_candidate(
        self, mock_config, mock_ydl_class, mock_probe,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(self._BAD_SOURCE)
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False
        assert not result.get("postprocess_error")
        assert downloader._ffmpeg_pp_observed_broken is False
        assert downloader._ffmpeg_pp_state is True
        assert mock_probe.call_count == 1

    @patch("downloader._probe_ffmpeg_can_write_audio", return_value=False)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_error_with_failing_fresh_probe_marks_broken(
        self, mock_config, mock_ydl_class, mock_probe,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(self._BAD_SOURCE)
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result.get("postprocess_error") is True
        assert downloader._ffmpeg_pp_observed_broken is True
        assert mock_probe.call_count == 1

    @patch("downloader._probe_ffmpeg_can_write_audio", return_value=False)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_disk_full_never_marks_host_broken(
        self, mock_config, mock_ydl_class, mock_probe,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Postprocessing: audio conversion failed:"
            " /dl/x.mp3: No space left on device"
        )
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is False
        assert not result.get("postprocess_error")
        assert downloader._ffmpeg_pp_observed_broken is False

    @patch("downloader._probe_ffmpeg_can_write_audio", return_value=True)
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_enosys_marks_broken_without_reprobing(
        self, mock_config, mock_ydl_class, mock_probe,
    ):
        mock_config.return_value = {
            "yt_player_client": "android", "audio_format": "mp3",
        }
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(
            "ERROR: Postprocessing: Error opening output files:"
            " Function not implemented"
        )
        candidate = {"url": "u", "title": "t", "duration": 200, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result.get("postprocess_error") is True
        assert downloader._ffmpeg_pp_observed_broken is True
        assert mock_probe.call_count == 0


class TestUnicodeFolding:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_typographic_apostrophe_matches_ascii(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Don't Stop Me Now (Remastered 2011)",
            "url": "https://music.youtube.com/watch?v=eeeeeeeeeee",
            "duration": 210,
            "channel": "Queen - Topic",
        }])
        candidates = search_youtube_candidates(
            "Queen Don’t Stop Me Now official audio",
            "Don’t Stop Me Now", 210000,
        )
        assert len(candidates) == 1

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_accents_are_folded(self, mock_config, mock_ydl_class):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Beyonce - Deja Vu",
            "url": "https://www.youtube.com/watch?v=fffffffffff",
            "duration": 240,
            "channel": "Beyonce",
        }])
        candidates = search_youtube_candidates(
            "Beyoncé Déjà Vu official audio",
            "Déjà Vu", 240000,
        )
        assert len(candidates) == 1

    def test_title_similarity_folds_both_sides(self):
        assert _title_similarity(
            "Queen - Don't Stop Me Now", "Don’t Stop Me Now", "Queen",
        ) == 1.0
        assert _title_similarity(
            "Beyonce - Deja Vu", "Déjà Vu", "Beyoncé",
        ) == 1.0

    def test_normalize_yt_title_folds(self):
        assert downloader._normalize_yt_title(
            "“Déjà Vu”"
        ) == '"deja vu"'

    def test_channel_matching_folds_accents(self):
        assert _is_official_channel("Beyonce", "Beyoncé")
        assert downloader._is_topic_channel("Bjork - Topic", "Björk")
        assert not _is_official_channel("Some Channel", "")

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_accented_artist_is_not_an_explicit_mismatch(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Deja Vu",
            "url": "https://music.youtube.com/watch?v=hhhhhhhhhhh",
            "duration": 240,
            "channel": "Beyonce",
            "artists": [{"name": "Beyonce"}],
        }])
        candidates = search_youtube_candidates(
            "Beyoncé Déjà Vu official audio",
            "Déjà Vu", 240000,
        )
        assert len(candidates) == 1
        assert candidates[0]["score"] > 0.6


class TestMissingEntryFields:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_none_duration_candidate_downloads(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        candidate = {"url": "u", "title": "t", "duration": None, "score": 0.9}
        result = download_youtube_candidate(candidate, "/tmp/output")
        assert result["success"] is True
        assert result["duration_seconds"] == 0
        assert mock_ydl_class.return_value.__enter__.return_value \
            .download.call_count == 1

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_none_title_does_not_discard_the_page(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([
            None,
            {
                "title": None,
                "url": "https://www.youtube.com/watch?v=bbbbbbbbbbb",
                "duration": 200,
            },
            {
                "title": "Artist - Song",
                "url": "https://www.youtube.com/watch?v=ccccccccccc",
                "duration": None,
                "channel": "Artist - Topic",
            },
        ])
        candidates = search_youtube_candidates(
            "Artist Song official audio", "Song", 200000,
        )
        assert len(candidates) == 1
        assert candidates[0]["duration"] == 0

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_none_search_result_is_not_an_error(
        self, mock_config, mock_ydl_class, caplog,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl(None)
        with caplog.at_level("ERROR", logger="downloader"):
            assert search_youtube_candidates("Artist Song", "Song") == []
        assert not [r for r in caplog.records if r.levelname == "ERROR"]

    @patch("downloader.download_youtube_candidate")
    @patch("downloader.search_youtube_candidates")
    def test_download_track_logs_none_duration(self, mock_search, mock_dl):
        mock_search.return_value = [
            {"url": "u", "title": "t", "duration": None, "score": 0.9},
        ]
        mock_dl.return_value = {"success": True}
        assert download_track_youtube("q", "/tmp/o", "t")["success"] is True


class TestBannedUrlForms:
    @pytest.mark.parametrize("banned", [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://youtu.be/aaaaaaaaaaa",
        "https://music.youtube.com/watch?v=aaaaaaaaaaa&list=x",
        "aaaaaaaaaaa",
    ])
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_ban_matches_any_url_form(
        self, mock_config, mock_ydl_class, banned,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Artist - Song",
            "url": "https://music.youtube.com/watch?v=aaaaaaaaaaa",
            "duration": 200,
            "channel": "Artist - Topic",
        }])
        candidates = search_youtube_candidates(
            "Artist Song official audio", "Song", 200000,
            banned_urls={banned},
        )
        assert candidates == []


class TestShortTracks:
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_track_shorter_than_15s_can_match(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Artist - Intro",
            "url": "https://www.youtube.com/watch?v=ddddddddddd",
            "duration": 8,
            "channel": "Artist - Topic",
        }])
        candidates = search_youtube_candidates(
            "Artist Intro official audio", "Intro", 8000,
        )
        assert len(candidates) == 1

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_short_video_rejected_when_duration_unknown(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = _SEARCH_CFG
        mock_ydl_class.side_effect = _search_ydl([{
            "title": "Artist - Intro",
            "url": "https://www.youtube.com/watch?v=ddddddddddd",
            "duration": 8,
            "channel": "Artist - Topic",
        }])
        assert search_youtube_candidates(
            "Artist Intro official audio", "Intro",
        ) == []


class TestRawAudioGlobEscaping:
    @patch("downloader.yt_dlp.YoutubeDL")
    def test_leftovers_removed_in_bracketed_dir(
        self, mock_ydl_class, tmp_path,
    ):
        import os
        album = tmp_path / "Album [Deluxe]"
        album.mkdir()
        out = str(album / "01 - Song")
        with open(out + ".m4a.part", "w") as fh:
            fh.write("stale")
        result = downloader._download_raw_audio(
            {"url": "u"}, out, "m4a", False,
            {"yt_player_client": "android"},
        )
        assert result is None
        assert os.listdir(album) == []


def test_encoder_quality_args_follow_yt_dlp_mapping():
    from downloader import _encoder_quality_args
    assert _encoder_quality_args("libmp3lame", "320") == ["-b:a", "320k"]
    assert _encoder_quality_args("libopus", "320") == ["-b:a", "256k"]
    assert _encoder_quality_args("libmp3lame", "0") == ["-q:a", "0.0"]
    assert _encoder_quality_args("libopus", "5") == []
    assert _encoder_quality_args("aac", "best") == []


_COOKIE_ROW = (
    ".youtube.com\tTRUE\t/\tTRUE\t1999999999\tLOGIN_INFO\tabc\n"
)


class TestCookiesFile:
    def test_headered_export_is_valid(self):
        info = downloader.inspect_cookies_text(
            "# Netscape HTTP Cookie File\n" + _COOKIE_ROW
        )
        assert info == {
            "valid": True, "entries": 1, "header": True, "reason": "",
        }

    def test_headerless_export_is_valid_but_flagged(self):
        info = downloader.inspect_cookies_text(_COOKIE_ROW)
        assert info["valid"] is True
        assert info["header"] is False

    def test_httponly_rows_count(self):
        info = downloader.inspect_cookies_text("#HttpOnly_" + _COOKIE_ROW)
        assert info["entries"] == 1

    def test_json_export_is_rejected(self):
        info = downloader.inspect_cookies_text('[{"name": "SID"}]')
        assert info["valid"] is False
        assert "JSON" in info["reason"]

    def test_garbage_is_rejected(self):
        info = downloader.inspect_cookies_text("hello world\nnot cookies\n")
        assert info["valid"] is False

    def test_empty_is_rejected(self):
        assert downloader.inspect_cookies_text("")["valid"] is False

    def test_snapshot_adds_header_and_leaves_original_alone(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(downloader.tempfile, "gettempdir", lambda: str(tmp_path))
        source = tmp_path / "cookies.txt"
        source.write_text(_COOKIE_ROW)
        snapshot = downloader.cookiefile_for_ytdlp(str(source))
        assert snapshot and snapshot != str(source)
        assert source.read_text() == _COOKIE_ROW
        from yt_dlp.cookies import YoutubeDLCookieJar
        jar = YoutubeDLCookieJar(snapshot)
        jar.load(ignore_discard=True, ignore_expires=True)
        assert {c.name for c in jar} == {"LOGIN_INFO"}

    def test_snapshot_is_private_to_each_thread(self, tmp_path, monkeypatch):
        import threading
        monkeypatch.setattr(downloader.tempfile, "gettempdir", lambda: str(tmp_path))
        source = tmp_path / "cookies.txt"
        source.write_text("# Netscape HTTP Cookie File\n" + _COOKIE_ROW)
        paths = []
        workers = [
            threading.Thread(
                target=lambda: paths.append(
                    downloader.cookiefile_for_ytdlp(str(source))
                ),
            )
            for _ in range(2)
        ]
        main_path = downloader.cookiefile_for_ytdlp(str(source))
        for w in workers:
            w.start()
            w.join()
        assert main_path not in paths

    def test_invalid_file_is_skipped_and_warned_once(
        self, tmp_path, monkeypatch, caplog,
    ):
        monkeypatch.setattr(downloader.tempfile, "gettempdir", lambda: str(tmp_path))
        monkeypatch.setattr(downloader, "_cookies_warned", set())
        source = tmp_path / "cookies.txt"
        source.write_text("not a cookies file\n")
        with caplog.at_level("WARNING", logger="downloader"):
            assert downloader.cookiefile_for_ytdlp(str(source)) is None
            assert downloader.cookiefile_for_ytdlp(str(source)) is None
        warnings = [
            r for r in caplog.records if "Ignoring YouTube cookies" in r.message
        ]
        assert len(warnings) == 1

    def test_common_opts_never_hand_ytdlp_the_original(
        self, tmp_path, monkeypatch,
    ):
        monkeypatch.setattr(downloader.tempfile, "gettempdir", lambda: str(tmp_path))
        source = tmp_path / "cookies.txt"
        source.write_text("# Netscape HTTP Cookie File\n" + _COOKIE_ROW)
        monkeypatch.setattr(
            downloader, "load_config",
            lambda: {"yt_cookies_file": str(source)},
        )
        opts = _build_common_opts()
        assert opts["cookiefile"] != str(source)
        assert os.path.exists(opts["cookiefile"])


class TestUnavailableVideos:
    _AGE = (
        "ERROR: [youtube] F9EIBnTVckM: Sign in to confirm your age. This"
        " video may be inappropriate for some users."
    )

    def _candidate(self):
        return {"url": "u", "title": "All Night Long", "duration": 200, "score": 1.0}

    def test_classify(self):
        classify = downloader._classify_unavailable
        assert classify(self._AGE.lower()) == "age"
        assert classify("error: private video. sign in") == "gone"
        assert classify("video unavailable. this video is not available") == "unavailable"
        assert classify("video unavailable. this content isn't available, try again later") is None
        assert classify("requested format is not available") is None

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_age_gate_without_cookies_stops_after_one_attempt(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(self._AGE)
        result = download_youtube_candidate(self._candidate(), "/tmp/output")
        assert result["success"] is False
        assert result["unavailable"] is True
        assert "cookies" in result["error_message"]
        assert mock_ydl.download.call_count == 1

    @patch("downloader.cookiefile_for_ytdlp", return_value="/tmp/c.txt")
    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_age_gate_with_cookies_tries_each_client_once(
        self, mock_config, mock_ydl_class, _cookies,
    ):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception(self._AGE)
        clients = downloader._client_fallback_chain(
            mock_config.return_value, False,
        ) + [None]
        result = download_youtube_candidate(self._candidate(), "/tmp/output")
        assert result["unavailable"] is True
        assert "not signed in" in result["error_message"]
        assert mock_ydl.download.call_count == len(clients)

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_private_video_stops_immediately(self, mock_config, mock_ydl_class):
        mock_config.return_value = {"yt_player_client": "android"}
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.download.side_effect = Exception("ERROR: [youtube] x: Private video")
        result = download_youtube_candidate(self._candidate(), "/tmp/output")
        assert result["unavailable"] is True
        assert mock_ydl.download.call_count == 1

    def test_age_gate_errors_from_ytdlp_are_not_warnings(self, caplog):
        with caplog.at_level("DEBUG", logger="downloader"):
            downloader._SILENT_YDL_LOGGER.error(self._AGE)
        assert all(r.levelname == "DEBUG" for r in caplog.records)


def _search_entry(title, channel, duration=150, url=None):
    return {
        "title": title,
        "url": url or f"https://www.youtube.com/watch?v={(title + 'x' * 11)[:11].replace(' ', '_')}",
        "duration": duration,
        "channel": channel,
        "view_count": 1000,
    }


class TestGenericAndBracketedTitles:
    _CFG = {
        "forbidden_words": [],
        "duration_tolerance": 15,
        "yt_player_client": "android",
    }

    def test_generic_title_detection(self):
        generic = downloader._is_generic_title
        assert generic("[untitled]")
        assert generic("Intro")
        assert generic("Interlude II")
        assert generic("Skit 3")
        assert not generic("Into the Night")

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_bracketed_title_matches_unbracketed_upload(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = dict(self._CFG)
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [_search_entry("Untitled", "Mac DeMarco - Topic", 163)],
        }
        candidates = search_youtube_candidates(
            "Mac DeMarco [untitled] official audio", "[untitled]",
            expected_duration_ms=163000,
        )
        assert [c["title"] for c in candidates] == ["Untitled"]

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_generic_title_needs_artist_evidence(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = dict(self._CFG)
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                _search_entry(
                    "Epic Dj intro - Tomorrowland style by Micro Jingles",
                    "Micro Jingles", 67,
                ),
                _search_entry("Intro - Album 2023", "Some Uploader", 67),
            ],
        }
        candidates = search_youtube_candidates(
            "Gigi D'Alessio Intro official audio", "Intro",
            expected_duration_ms=67000,
        )
        assert candidates == []

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_generic_title_from_artist_channel_is_kept(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = dict(self._CFG)
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {
            "entries": [
                _search_entry("SUNDRENCHED: EAGLES [OUTRO]", "Someone", 53),
                _search_entry("Outro", "Gigi D'Alessio - Topic", 53),
            ],
        }
        candidates = search_youtube_candidates(
            "Gigi D'Alessio Outro official audio", "Outro",
            expected_duration_ms=53000,
        )
        assert [c["title"] for c in candidates] == ["Outro"]

    @patch("downloader.yt_dlp.YoutubeDL")
    @patch("downloader.load_config")
    def test_fallback_phase_reuses_phase_one_results(
        self, mock_config, mock_ydl_class,
    ):
        mock_config.return_value = dict(self._CFG)
        mock_ydl = mock_ydl_class.return_value.__enter__.return_value
        mock_ydl.extract_info.return_value = {"entries": []}
        search_youtube_candidates(
            "Artist Song official audio", "Song", expected_duration_ms=200000,
        )
        targets = [c.args[0] for c in mock_ydl.extract_info.call_args_list]
        assert targets
        assert len(targets) == len(set(targets))


class TestAlbumTrackMatching:
    def _entries(self, *items):
        return [
            {"url": f"https://music.youtube.com/watch?v={i:011d}",
             "title": title, "duration": duration}
            for i, (title, duration) in enumerate(items)
        ]

    def test_bracketed_title_matches(self):
        entries = self._entries(("Untitled", 163))
        cand = match_album_track(entries, "[untitled]", 163000)
        assert cand and cand["title"] == "Untitled"

    def test_duration_breaks_title_ties(self):
        entries = self._entries(("Intro", 40), ("Intro", 67))
        cand = match_album_track(entries, "Intro", 67000)
        assert cand["duration"] == 67

    def test_positional_fallback_when_titles_differ(self):
        entries = self._entries(("One", 163), ("Two", 468), ("Three", 132))
        cand = match_album_track(
            entries, "[untitled]", 468500, position=2, total_tracks=3,
        )
        assert cand["title"] == "Two"
        assert cand["matched_by"] == "position"

    def test_positional_fallback_needs_same_track_count(self):
        entries = self._entries(("One", 163), ("Two", 468))
        assert match_album_track(
            entries, "[untitled]", 468000, position=2, total_tracks=3,
        ) is None

    def test_positional_fallback_needs_matching_duration(self):
        entries = self._entries(("One", 163), ("Two", 300))
        assert match_album_track(
            entries, "[untitled]", 468000, position=2, total_tracks=2,
        ) is None

    def test_title_match_is_marked(self):
        entries = self._entries(("Song", 200))
        assert match_album_track(entries, "Song", 200000)["matched_by"] == "title"


class TestAlbumFromYtmusicHint:
    def test_builds_entries_from_the_hinted_album(self, monkeypatch):
        import downloader

        class FakeYT:
            def get_album(self, browse_id):
                assert browse_id == "MPREb_abcdef"
                return {
                    "audioPlaylistId": "OLAK5uy_hinted123",
                    "tracks": [
                        {"videoId": "aaaaaaaaaaa", "title": "One ",
                         "artists": [{"name": "Band"}], "duration": "3:01",
                         "duration_seconds": 181},
                        {"videoId": None, "title": "Unavailable"},
                        {"videoId": "bbbbbbbbbbb", "title": "Two",
                         "artists": [], "duration": "1:02:03"},
                    ],
                }

        monkeypatch.setattr(downloader, "_ytmusicapi_client", lambda *a, **k: FakeYT())
        result = downloader.album_from_ytmusic_hint(
            "OLAK5uy_hinted123", "MPREb_abcdef", "Band",
        )
        assert result["playlist_url"].endswith("list=OLAK5uy_hinted123")
        assert [e["title"] for e in result["entries"]] == ["One", "Two"]
        assert [e["duration"] for e in result["entries"]] == [181, 3723]
        assert result["entries"][1]["channel"] == "Band"

    def test_falls_back_to_playlist_extraction(self, monkeypatch):
        import downloader
        monkeypatch.setattr(downloader, "_ytmusicapi_client", lambda *a, **k: None)
        monkeypatch.setattr(downloader, "load_config", lambda: {})
        monkeypatch.setattr(
            downloader, "_extract_ytm_album_entries",
            lambda url, pc: [{"url": "u", "title": "T", "duration": 5, "channel": "c"}],
        )
        result = downloader.album_from_ytmusic_hint("OLAK5uy_hinted123", "MPREb_abcdef")
        assert result["playlist_id"] == "OLAK5uy_hinted123"
        assert len(result["entries"]) == 1

    def test_mismatched_album_details_are_ignored(self, monkeypatch):
        import downloader

        class FakeYT:
            def get_album(self, browse_id):
                return {"audioPlaylistId": "OLAK5uy_other99999",
                        "tracks": [{"videoId": "aaaaaaaaaaa", "title": "X"}]}

        monkeypatch.setattr(downloader, "_ytmusicapi_client", lambda *a, **k: FakeYT())
        monkeypatch.setattr(downloader, "load_config", lambda: {})
        monkeypatch.setattr(downloader, "_extract_ytm_album_entries", lambda u, p: [])
        assert downloader.album_from_ytmusic_hint(
            "OLAK5uy_hinted123", "MPREb_abcdef",
        ) is None

    def test_rejects_invalid_ids(self, monkeypatch):
        import downloader
        monkeypatch.setattr(
            downloader, "_ytmusicapi_client",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("no call")),
        )
        assert downloader.album_from_ytmusic_hint("../etc", "MPREb_x") is None
        assert downloader.album_from_ytmusic_hint("", "") is None
