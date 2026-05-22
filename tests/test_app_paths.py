"""Tests for album path resolution helpers in app.py."""
import os

import pytest

import app


@pytest.fixture
def dual_paths(tmp_path, monkeypatch):
    download_dir = tmp_path / "downloads"
    lidarr_dir = tmp_path / "lidarr"
    download_dir.mkdir()
    lidarr_dir.mkdir()
    monkeypatch.setattr(app, "DOWNLOAD_DIR", str(download_dir))
    config = {
        "download_path": str(download_dir),
        "lidarr_path": str(lidarr_dir),
    }
    return download_dir, lidarr_dir, config


def test_build_album_paths_writes_under_download_dir_uses_lidarr_artist_folder(
    dual_paths,
):
    download_dir, lidarr_dir, config = dual_paths
    album_data = {
        "title": "Test Album",
        "releaseDate": "2024-01-01",
        "artist": {
            "artistName": "K/DA",
            "path": str(lidarr_dir / "K+DA"),
        },
    }
    target_path, makedirs_bases, lidarr_import_path = app._build_album_paths(
        album_data, config,
    )
    assert target_path == os.path.join(
        str(download_dir), "K+DA", "Test Album (2024)",
    )
    assert makedirs_bases == [str(download_dir)]
    assert lidarr_import_path == os.path.join(
        str(lidarr_dir), "K+DA", "Test Album (2024)",
    )


def test_makedirs_bases_includes_lidarr_when_target_under_lidarr(dual_paths):
    _, lidarr_dir, config = dual_paths
    target = os.path.join(str(lidarr_dir), "Artist", "Album (2024)")
    bases = app._makedirs_bases_for(target, config)
    assert str(lidarr_dir) in bases


def test_makedirs_bases_matches_target_not_only_download_dir(dual_paths):
    download_dir, lidarr_dir, config = dual_paths
    target = os.path.join(str(lidarr_dir), "Artist", "Album")
    bases = app._makedirs_bases_for(target, config)
    assert str(download_dir) not in bases
    assert str(lidarr_dir) in bases
