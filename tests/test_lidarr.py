"""Tests for lidarr.py — Lidarr API wrapper and release helpers."""

from unittest.mock import patch, MagicMock

import pytest

import lidarr


# --- lidarr_request ---


@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_get(mock_get, mock_cfg):
    mock_cfg.return_value = {
        "lidarr_url": "http://lidarr:8686",
        "lidarr_api_key": "key123",
    }
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"version": "2.0"}
    )
    result = lidarr.lidarr_request("system/status")
    assert result["version"] == "2.0"
    mock_get.assert_called_once_with(
        "http://lidarr:8686/api/v1/system/status",
        headers={"X-Api-Key": "key123"},
        params=None,
        timeout=30,
    )


@patch("lidarr.load_config")
@patch("lidarr.requests.post")
def test_lidarr_request_post(mock_post, mock_cfg):
    mock_cfg.return_value = {
        "lidarr_url": "http://lidarr:8686",
        "lidarr_api_key": "key123",
    }
    mock_post.return_value = MagicMock(
        status_code=200, json=lambda: {"success": True}
    )
    result = lidarr.lidarr_request(
        "command", method="POST", data={"name": "RefreshArtist"}
    )
    assert result["success"] is True
    mock_post.assert_called_once_with(
        "http://lidarr:8686/api/v1/command",
        headers={"X-Api-Key": "key123"},
        json={"name": "RefreshArtist"},
        timeout=30,
    )


@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_with_params(mock_get, mock_cfg):
    mock_cfg.return_value = {
        "lidarr_url": "http://lidarr:8686",
        "lidarr_api_key": "key123",
    }
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"records": []}
    )
    result = lidarr.lidarr_request(
        "wanted/missing", params={"page": 1}
    )
    assert result == {"records": []}
    mock_get.assert_called_once_with(
        "http://lidarr:8686/api/v1/wanted/missing",
        headers={"X-Api-Key": "key123"},
        params={"page": 1},
        timeout=30,
    )


@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_error(mock_get, mock_cfg):
    mock_cfg.return_value = {
        "lidarr_url": "http://lidarr:8686",
        "lidarr_api_key": "key123",
    }
    mock_get.side_effect = Exception("connection failed")
    result = lidarr.lidarr_request("system/status")
    assert "error" in result
    assert "connection failed" in result["error"]


@patch("lidarr.load_config")
@patch("lidarr.requests.get")
def test_lidarr_request_http_error(mock_get, mock_cfg):
    mock_cfg.return_value = {
        "lidarr_url": "http://lidarr:8686",
        "lidarr_api_key": "key123",
    }
    mock_response = MagicMock()
    mock_response.raise_for_status.side_effect = Exception("404 Not Found")
    mock_get.return_value = mock_response
    result = lidarr.lidarr_request("bad/endpoint")
    assert "error" in result


# --- get_missing_albums ---


@patch("lidarr.lidarr_request")
def test_get_missing_albums_single_page(mock_req):
    mock_req.return_value = {
        "records": [
            {
                "id": 1,
                "title": "Album One",
                "statistics": {"trackCount": 10, "trackFileCount": 3},
            }
        ],
        "totalRecords": 1,
    }
    result = lidarr.get_missing_albums()
    assert len(result) == 1
    assert result[0]["missingTrackCount"] == 7


@patch("lidarr.lidarr_request")
def test_get_missing_albums_pagination(mock_req):
    """Verify pagination stops when all records are fetched."""
    page1 = {
        "records": [
            {"id": i, "statistics": {"trackCount": 5, "trackFileCount": 0}}
            for i in range(500)
        ],
        "totalRecords": 501,
    }
    page2 = {
        "records": [
            {"id": 500, "statistics": {"trackCount": 3, "trackFileCount": 1}}
        ],
        "totalRecords": 501,
    }
    mock_req.side_effect = [page1, page2]
    result = lidarr.get_missing_albums()
    assert len(result) == 501
    assert result[500]["missingTrackCount"] == 2
    assert mock_req.call_count == 2


@patch("lidarr.lidarr_request")
def test_get_missing_albums_error_returns_empty(mock_req):
    mock_req.return_value = {"error": "connection failed"}
    result = lidarr.get_missing_albums()
    assert result == []


@patch("lidarr.lidarr_request")
def test_get_missing_albums_exception_returns_empty(mock_req):
    mock_req.side_effect = Exception("unexpected")
    result = lidarr.get_missing_albums()
    assert result == []


@patch("lidarr.lidarr_request")
def test_get_missing_albums_no_statistics(mock_req):
    mock_req.return_value = {
        "records": [{"id": 1}],
        "totalRecords": 1,
    }
    result = lidarr.get_missing_albums()
    assert len(result) == 1
    assert result[0]["missingTrackCount"] == 0


# --- get_valid_release_id ---


def test_get_valid_release_id_monitored():
    album = {
        "releases": [
            {"id": 1, "monitored": False},
            {"id": 2, "monitored": True},
        ]
    }
    assert lidarr.get_valid_release_id(album) == 2


def test_get_valid_release_id_fallback():
    album = {"releases": [{"id": 5, "monitored": False}]}
    assert lidarr.get_valid_release_id(album) == 5


def test_get_valid_release_id_empty():
    assert lidarr.get_valid_release_id({"releases": []}) == 0


def test_get_valid_release_id_no_releases_key():
    assert lidarr.get_valid_release_id({}) == 0


def test_get_valid_release_id_zero_id_skipped():
    album = {
        "releases": [
            {"id": 0, "monitored": True},
            {"id": 3, "monitored": False},
        ]
    }
    assert lidarr.get_valid_release_id(album) == 3


# --- get_monitored_release ---


def test_get_monitored_release():
    album = {
        "releases": [
            {"id": 1, "monitored": False},
            {"id": 2, "monitored": True},
        ]
    }
    assert lidarr.get_monitored_release(album)["id"] == 2


def test_get_monitored_release_fallback():
    album = {"releases": [{"id": 1, "monitored": False}]}
    assert lidarr.get_monitored_release(album)["id"] == 1


def test_get_monitored_release_empty():
    assert lidarr.get_monitored_release({"releases": []}) is None


def test_get_monitored_release_no_releases_key():
    assert lidarr.get_monitored_release({}) is None
