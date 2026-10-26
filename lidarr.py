"""Lidarr API wrapper and release helpers.

Provides functions for communicating with the Lidarr API: making requests,
fetching missing albums, and resolving release IDs.
"""

import json
import logging
import threading
import time

import requests

from config import load_config

logger = logging.getLogger(__name__)


def _validation_detail(response):
    if response is None:
        return ""
    try:
        body = response.json()
    except Exception:
        return ""
    if isinstance(body, dict):
        body = [body]
    if not isinstance(body, list):
        return ""
    messages = []
    for item in body:
        if not isinstance(item, dict):
            continue
        text = item.get("errorMessage") or item.get("message") or ""
        if isinstance(text, str) and text.strip() and text not in messages:
            messages.append(text.strip())
    return "; ".join(messages)[:300]


def lidarr_request(endpoint, method="GET", data=None, params=None):
    """Make an authenticated request to the Lidarr API.

    Args:
        endpoint: API endpoint path (appended to /api/v1/).
        method: HTTP method, "GET", "POST" or "PUT".
        data: JSON body for POST/PUT requests.
        params: Query parameters for GET requests.

    Returns:
        Parsed JSON response as a dict, or {"error": "..."} on failure.
    """
    config = load_config()
    base_url = (config.get("lidarr_url") or "").rstrip("/")
    api_key = config.get("lidarr_api_key") or ""
    if not base_url:
        return {"error": "LIDARR_URL not configured", "retryable": False}
    if not api_key:
        return {"error": "LIDARR_API_KEY not configured", "retryable": False}
    url = f"{base_url}/api/v1/{endpoint}"
    headers = {"X-Api-Key": api_key}
    try:
        if method == "GET":
            r = requests.get(
                url, headers=headers, params=params, timeout=30
            )
        elif method == "POST":
            r = requests.post(
                url, headers=headers, json=data, timeout=30
            )
        elif method == "PUT":
            r = requests.put(
                url, headers=headers, json=data, timeout=30
            )
        else:
            return {
                "error": f"Unsupported HTTP method: {method}",
                "retryable": False,
            }
        r.raise_for_status()
        try:
            return r.json()
        except (ValueError, json.JSONDecodeError):
            preview = (r.text or "")[:120].replace("\n", " ").strip()
            ctype = r.headers.get("Content-Type", "unknown")
            logger.warning(
                "Lidarr returned non-JSON response (HTTP %d, content-type=%s) for %s: %r",
                r.status_code, ctype, endpoint, preview,
            )
            return {
                "error": (
                    f"Lidarr returned a non-JSON response (HTTP {r.status_code})."
                    " Check that LIDARR_URL points to Lidarr (not a reverse-proxy"
                    " login page) and that LIDARR_API_KEY is correct."
                ),
                "retryable": False,
            }
    except requests.exceptions.ConnectionError as e:
        logger.warning("Cannot connect to Lidarr at %s: %s", url, e)
        return {"error": f"Cannot connect to Lidarr: {e}", "retryable": True}
    except requests.exceptions.Timeout:
        logger.warning("Lidarr request timed out: %s", endpoint)
        return {"error": "Lidarr request timed out", "retryable": True}
    except requests.exceptions.HTTPError as e:
        status = getattr(e.response, "status_code", "?")
        logger.warning("Lidarr HTTP error %s on %s: %s", status, endpoint, e)
        if status == 401:
            return {
                "error": "Lidarr authentication failed (401). Check LIDARR_API_KEY.",
                "retryable": False,
            }
        if status == 404:
            return {
                "error": f"Lidarr endpoint not found (404): {endpoint}",
                "retryable": False,
            }
        detail = _validation_detail(getattr(e, "response", None))
        return {
            "error": (
                f"Lidarr rejected the request: {detail}" if detail
                else f"Lidarr HTTP {status}: {e}"
            ),
            "retryable": isinstance(status, int) and status >= 500,
        }
    except Exception as e:
        logger.error("Unexpected error calling Lidarr %s: %s", endpoint, e)
        return {"error": str(e), "retryable": False}


def lidarr_request_with_retry(
    endpoint, *, method="POST", data=None, max_attempts=4, base_delay=5
):
    result = {"error": "no attempts made"}
    for attempt in range(max_attempts):
        result = lidarr_request(endpoint, method=method, data=data)
        if "error" not in result:
            return result
        if not result.get("retryable", True):
            logger.error(
                "Lidarr command %s failed: %s", endpoint, result["error"],
            )
            return result
        if attempt < max_attempts - 1:
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "Lidarr command %s failed (attempt %d/%d): %s. Retrying in %ds...",
                endpoint, attempt + 1, max_attempts, result["error"], delay,
            )
            time.sleep(delay)
        else:
            logger.error(
                "Lidarr command %s failed after %d attempts: %s",
                endpoint, max_attempts, result["error"],
            )
    return result


_tag_ids = {}
_tag_lock = threading.Lock()


def _lidarr_tag_id(label):
    key = label.casefold()
    with _tag_lock:
        if key in _tag_ids:
            return _tag_ids[key]
        tags = lidarr_request("tag")
        if not isinstance(tags, list):
            logger.warning(
                "Cannot read Lidarr tags: %s", (tags or {}).get("error", "?"),
            )
            return None
        for tag in tags:
            if isinstance(tag, dict) and str(tag.get("label") or "").casefold() == key:
                _tag_ids[key] = tag.get("id")
                return _tag_ids[key]
        created = lidarr_request("tag", method="POST", data={"label": label})
        if not isinstance(created, dict) or not created.get("id"):
            logger.warning(
                "Cannot create Lidarr tag '%s': %s",
                label, (created or {}).get("error", "?"),
            )
            return None
        _tag_ids[key] = created["id"]
        return created["id"]


def _forget_lidarr_tags():
    with _tag_lock:
        _tag_ids.clear()


def _tag_artist_by_put(artist_id, tag_id):
    artist = lidarr_request(f"artist/{artist_id}")
    if not isinstance(artist, dict) or "error" in artist:
        return artist if isinstance(artist, dict) else {"error": "no artist"}
    tags = [t for t in (artist.get("tags") or []) if isinstance(t, int)]
    if tag_id in tags:
        return artist
    artist["tags"] = tags + [tag_id]
    return lidarr_request(f"artist/{artist_id}", method="PUT", data=artist)


def tag_artist(artist_id, label=None):
    if label is None:
        label = load_config().get("lidarr_tag") or ""
    label = str(label).strip().lower()
    try:
        artist_id = int(artist_id)
    except (TypeError, ValueError):
        return False
    if not label or artist_id <= 0:
        return False
    tag_id = _lidarr_tag_id(label)
    if not tag_id:
        return False
    result = lidarr_request(
        "artist/editor", method="PUT",
        data={"artistIds": [artist_id], "tags": [tag_id], "applyTags": "add"},
    )
    if isinstance(result, dict) and "error" in result:
        result = _tag_artist_by_put(artist_id, tag_id)
    if isinstance(result, dict) and "error" in result:
        _forget_lidarr_tags()
        logger.warning(
            "Cannot tag Lidarr artist %s with '%s': %s",
            artist_id, label, result["error"],
        )
        return False
    logger.info("   Tagged the artist in Lidarr with '%s'", label)
    return True


def get_missing_albums():
    """Return missing albums from the local SQLite cache.

    The cache is populated by the background worker in `lidarr_sync.py`,
    so this call is instant and works on libraries of any size.

    Returns:
        Slim album dicts (see models.get_cached_missing_albums_summary).
        Returns an empty list on error or when the cache has not been
        populated yet.
    """
    try:
        import models
        return models.get_cached_missing_albums_summary()
    except Exception as e:
        logger.warning(f"Failed to read cached missing albums: {e}")
        return []


def get_valid_release_id(album):
    """Get a valid release ID from an album, preferring monitored releases.

    Args:
        album: Album dict containing a "releases" list.

    Returns:
        The release ID (int), or 0 if no valid release found.
    """
    releases = album.get("releases", [])
    if not releases:
        return 0
    for rel in releases:
        if rel.get("monitored", False) and rel.get("id", 0) > 0:
            return rel["id"]
    for rel in releases:
        if rel.get("id", 0) > 0:
            return rel["id"]
    return 0


def get_monitored_release(album):
    """Get the monitored release from an album, or fall back to first.

    Args:
        album: Album dict containing a "releases" list.

    Returns:
        The release dict, or None if no releases exist.
    """
    releases = album.get("releases", [])
    if not releases:
        return None
    for rel in releases:
        if rel.get("monitored", False):
            return rel
    return releases[0]
