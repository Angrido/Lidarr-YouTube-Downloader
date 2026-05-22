import os
import time

import pytest

import utils


class TestSanitizeFilename:
    def test_removes_special_chars(self):
        assert utils.sanitize_filename('test<>:"/\\|?*file') == "testfile"

    def test_empty_string(self):
        assert utils.sanitize_filename("") == "untitled"

    def test_only_dots(self):
        assert utils.sanitize_filename("..") == "untitled"

    def test_tilde_removed(self):
        assert utils.sanitize_filename("~file~") == "file"

    def test_strips_leading_trailing_dots_and_spaces(self):
        assert utils.sanitize_filename("  .hello. ") == "hello"

    def test_normal_name_unchanged(self):
        assert utils.sanitize_filename("My Album (2024)") == "My Album (2024)"

    def test_only_special_chars(self):
        assert utils.sanitize_filename('<>:"/\\|?*') == "untitled"

    def test_double_dots_in_middle(self):
        assert utils.sanitize_filename("foo..bar") == "foobar"


class TestFormatBytes:
    def test_zero(self):
        assert utils.format_bytes(0) == ""

    def test_negative(self):
        assert utils.format_bytes(-1) == ""

    def test_bytes(self):
        result = utils.format_bytes(500)
        assert "500.0 B" == result

    def test_kilobytes(self):
        result = utils.format_bytes(2048)
        assert "KB" in result

    def test_megabytes(self):
        result = utils.format_bytes(1048576)
        assert "MB" in result
        assert "1.0 MB" == result

    def test_gigabytes(self):
        result = utils.format_bytes(1073741824)
        assert "GB" in result
        assert "1.0 GB" == result

    def test_terabytes(self):
        result = utils.format_bytes(1099511627776)
        assert "TB" in result
        assert "1.0 TB" == result


class TestCheckRateLimit:
    def test_allows_within_limit(self):
        store = {}
        assert utils.check_rate_limit("key1", store, window=60, max_requests=3)

    def test_allows_up_to_max(self):
        store = {}
        for _ in range(3):
            assert utils.check_rate_limit("k", store, window=60, max_requests=3)

    def test_blocks_over_max(self):
        store = {}
        for _ in range(5):
            utils.check_rate_limit("k", store, window=60, max_requests=5)
        assert not utils.check_rate_limit("k", store, window=60, max_requests=5)

    def test_separate_keys_independent(self):
        store = {}
        for _ in range(3):
            utils.check_rate_limit("a", store, window=60, max_requests=3)
        assert not utils.check_rate_limit("a", store, window=60, max_requests=3)
        assert utils.check_rate_limit("b", store, window=60, max_requests=3)

    def test_expired_requests_cleared(self):
        store = {"k": [time.time() - 10]}
        assert utils.check_rate_limit("k", store, window=2, max_requests=1)


class TestGetUmask:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("UMASK", raising=False)
        assert utils.get_umask() == 0o002

    def test_custom_octal_string(self, monkeypatch):
        monkeypatch.setenv("UMASK", "022")
        assert utils.get_umask() == 0o022

    def test_with_0o_prefix(self, monkeypatch):
        monkeypatch.setenv("UMASK", "0o077")
        assert utils.get_umask() == 0o077

    def test_invalid_falls_back(self, monkeypatch):
        monkeypatch.setenv("UMASK", "notanumber")
        assert utils.get_umask() == 0o002

    def test_whitespace_stripped(self, monkeypatch):
        monkeypatch.setenv("UMASK", "  022  ")
        assert utils.get_umask() == 0o022


class TestMakedirsSafe:
    def test_creates_path_under_existing_base(self, tmp_path):
        base = tmp_path / "downloads"
        base.mkdir()
        target = base / "Artist" / "Album (2024)"
        utils.makedirs_safe(str(target), [str(base)])
        assert target.is_dir()

    def test_falls_back_to_plain_makedirs_when_no_base_matches(self, tmp_path):
        target = tmp_path / "x" / "y" / "z"
        utils.makedirs_safe(str(target), ["/totally/unrelated/base"])
        assert target.is_dir()

    def test_skips_empty_base_entries(self, tmp_path):
        base = tmp_path / "downloads"
        base.mkdir()
        target = base / "Artist"
        utils.makedirs_safe(str(target), ["", None, str(base)])
        assert target.is_dir()

    def test_unmounted_base_raises_typed_error(self, tmp_path, monkeypatch):
        unmounted_base = "/proc/self/nonexistent-vol"
        target = unmounted_base + "/Artist/Album"
        with pytest.raises(utils.BaseNotMountedError) as exc_info:
            utils.makedirs_safe(target, [unmounted_base])
        assert unmounted_base in str(exc_info.value)
        assert exc_info.value.base_dir == unmounted_base

    def test_existing_artist_dir_without_group_write_gets_relaxed(
        self, tmp_path,
    ):
        base = tmp_path / "downloads"
        base.mkdir()
        artist = base / "Artist"
        artist.mkdir(mode=0o755)
        try:
            os.chmod(str(artist), 0o755)
        except OSError:
            pytest.skip("chmod not supported")
        target = artist / "Album (2024)"
        utils.makedirs_safe(str(target), [str(base)])
        assert target.is_dir()
        mode = os.stat(str(artist)).st_mode & 0o777
        assert mode & 0o070 == 0o070

    def test_already_existing_target_is_noop(self, tmp_path):
        base = tmp_path / "downloads"
        base.mkdir()
        target = base / "Artist" / "Album"
        target.mkdir(parents=True)
        utils.makedirs_safe(str(target), [str(base)])
        assert target.is_dir()


class TestSetPermissions:
    def test_sets_file_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UMASK", "002")
        f = tmp_path / "test.mp3"
        f.write_text("data")
        utils.set_permissions(str(f))
        mode = os.stat(str(f)).st_mode & 0o777
        assert mode == 0o664

    def test_sets_directory_permissions(self, tmp_path, monkeypatch):
        monkeypatch.setenv("UMASK", "002")
        d = tmp_path / "album"
        d.mkdir()
        (d / "track.mp3").write_text("data")
        utils.set_permissions(str(d))
        dir_mode = os.stat(str(d)).st_mode & 0o777
        file_mode = os.stat(str(d / "track.mp3")).st_mode & 0o777
        assert dir_mode == 0o775
        assert file_mode == 0o664

    def test_nonexistent_path_no_error(self):
        utils.set_permissions("/nonexistent/path/that/does/not/exist")
