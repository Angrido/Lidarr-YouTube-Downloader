"""Tests for lidarr.py — Lidarr API wrapper and release helpers."""

from unittest.mock import patch, MagicMock

import pytest
import requests

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


# --- get_missing_albums (cache-backed) ---


@patch("models.get_cached_missing_albums")
def test_get_missing_albums_reads_from_cache(mock_cached):
    mock_cached.return_value = [
        {"id": 1, "title": "Album One", "missingTrackCount": 7}
    ]
    result = lidarr.get_missing_albums()
    assert len(result) == 1
    assert result[0]["missingTrackCount"] == 7
    mock_cached.assert_called_once()


@patch("models.get_cached_missing_albums")
def test_get_missing_albums_empty_cache(mock_cached):
    mock_cached.return_value = []
    assert lidarr.get_missing_albums() == []


@patch("models.get_cached_missing_albums")
def test_get_missing_albums_exception_returns_empty(mock_cached):
    mock_cached.side_effect = Exception("db error")
    assert lidarr.get_missing_albums() == []


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


# --- retryability ---


def _http_error_response(status):
    response = MagicMock(status_code=status)
    response.raise_for_status.side_effect = requests.exceptions.HTTPError(
        f"{status} error", response=response,
    )
    return response


_CFG = {"lidarr_url": "http://lidarr:8686", "lidarr_api_key": "key123"}


@pytest.mark.parametrize("status,retryable", [
    (400, False), (401, False), (403, False), (404, False),
    (500, True), (502, True), (503, True),
])
@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.get")
def test_lidarr_request_http_error_retryable_flag(
    mock_get, mock_cfg, status, retryable,
):
    mock_get.return_value = _http_error_response(status)
    result = lidarr.lidarr_request("album/1")
    assert "error" in result
    assert result["retryable"] is retryable


@pytest.mark.parametrize("exc", [
    requests.exceptions.ConnectionError("refused"),
    requests.exceptions.Timeout("slow"),
])
@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.get")
def test_lidarr_request_transport_errors_are_retryable(mock_get, mock_cfg, exc):
    mock_get.side_effect = exc
    result = lidarr.lidarr_request("album/1")
    assert result["retryable"] is True


@patch("lidarr.load_config", return_value={})
def test_lidarr_request_not_configured_is_not_retryable(mock_cfg):
    result = lidarr.lidarr_request("album/1")
    assert result["retryable"] is False


@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.get")
def test_lidarr_request_success_has_no_retryable_key(mock_get, mock_cfg):
    mock_get.return_value = MagicMock(
        status_code=200, json=lambda: {"version": "2.0"}
    )
    assert lidarr.lidarr_request("system/status") == {"version": "2.0"}


@pytest.mark.parametrize("status", [400, 401, 404])
@patch("lidarr.time.sleep")
@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.post")
def test_retry_gives_up_immediately_on_permanent_error(
    mock_post, mock_cfg, mock_sleep, status,
):
    mock_post.return_value = _http_error_response(status)
    result = lidarr.lidarr_request_with_retry(
        "command", data={"name": "RefreshArtist"},
    )
    assert "error" in result
    assert mock_post.call_count == 1
    mock_sleep.assert_not_called()


@patch("lidarr.time.sleep")
@patch("lidarr.load_config", return_value={})
def test_retry_gives_up_immediately_when_not_configured(mock_cfg, mock_sleep):
    result = lidarr.lidarr_request_with_retry("command", data={})
    assert "error" in result
    mock_sleep.assert_not_called()


@patch("lidarr.time.sleep")
@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.post")
def test_retry_retries_server_errors_then_succeeds(
    mock_post, mock_cfg, mock_sleep,
):
    mock_post.side_effect = [
        _http_error_response(503),
        requests.exceptions.ConnectionError("down"),
        MagicMock(status_code=201, json=lambda: {"id": 9}),
    ]
    result = lidarr.lidarr_request_with_retry("command", data={})
    assert result == {"id": 9}
    assert mock_post.call_count == 3
    assert mock_sleep.call_count == 2


@patch("lidarr.time.sleep")
@patch("lidarr.lidarr_request", return_value={"error": "legacy"})
def test_retry_without_retryable_key_keeps_retrying(mock_req, mock_sleep):
    lidarr.lidarr_request_with_retry("command", data={}, max_attempts=3)
    assert mock_req.call_count == 3


@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.put")
def test_lidarr_request_supports_put(mock_put, mock_cfg):
    mock_put.return_value = MagicMock(status_code=202)
    mock_put.return_value.json.return_value = [{"id": 7}]
    result = lidarr.lidarr_request(
        "album/monitor", method="PUT", data={"albumIds": [7]},
    )
    assert result == [{"id": 7}]
    assert mock_put.call_args.kwargs["json"] == {"albumIds": [7]}


@patch("lidarr.load_config", return_value=_CFG)
@patch("lidarr.requests.post")
def test_lidarr_validation_errors_are_readable(mock_post, mock_cfg):
    response = _http_error_response(400)
    response.json.return_value = [
        {"propertyName": "Path", "errorMessage": "Path is already configured"},
        {"propertyName": "Path", "errorMessage": "Path is already configured"},
    ]
    mock_post.return_value = response
    result = lidarr.lidarr_request("artist", method="POST", data={})
    assert result["error"] == (
        "Lidarr rejected the request: Path is already configured"
    )
    assert result["retryable"] is False
