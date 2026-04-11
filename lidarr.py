"""Lidarr API wrapper and release helpers.

Provides functions for communicating with the Lidarr API: making requests,
fetching missing albums, and resolving release IDs.
"""

import logging

import requests

from config import load_config

logger = logging.getLogger(__name__)


def lidarr_request(endpoint, method="GET", data=None, params=None):
    """Make an authenticated request to the Lidarr API.

    Args:
        endpoint: API endpoint path (appended to /api/v1/).
        method: HTTP method, "GET" or "POST".
        data: JSON body for POST requests.
        params: Query parameters for GET requests.

    Returns:
        Parsed JSON response as a dict, or {"error": "..."} on failure.
    """
    config = load_config()
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        if method == "GET":
            r = requests.get(
                url, headers=headers, params=params, timeout=30
            )
        elif method == "POST":
            r = requests.post(
                url, headers=headers, json=data, timeout=30
            )
        r.raise_for_status()
        return r.json()
    except requests.exceptions.ConnectionError as e:
        logger.warning("Cannot connect to Lidarr at %s: %s", url, e)
        return {"error": f"Cannot connect to Lidarr: {e}"}
    except requests.exceptions.Timeout:
        logger.warning("Lidarr request timed out: %s", endpoint)
        return {"error": "Lidarr request timed out"}
    except Exception as e:
        logger.error("Unexpected error calling Lidarr %s: %s", endpoint, e)
        return {"error": str(e)}


def get_missing_albums():
    """Fetch all missing albums from Lidarr with pagination.

    Returns:
        List of album dicts, each augmented with a missingTrackCount field.
        Returns an empty list on error.
    """
    try:
        page = 1
        page_size = 500
        all_records = []
        while True:
            wanted = lidarr_request(
                f"wanted/missing?page={page}&pageSize={page_size}"
                f"&sortKey=releaseDate&sortDirection=descending"
                f"&includeArtist=true"
            )
            if not isinstance(wanted, dict) or "records" not in wanted:
                if isinstance(wanted, dict) and "error" in wanted:
                    logger.warning(
                        "Lidarr returned error fetching missing albums"
                        " (page %d): %s", page, wanted["error"],
                    )
                break
            records = wanted.get("records", [])
            total_records = wanted.get("totalRecords", 0)
            for album in records:
                stats = album.get("statistics", {})
                total = stats.get("trackCount", 0)
                files = stats.get("trackFileCount", 0)
                album["missingTrackCount"] = total - files
            all_records.extend(records)
            if (
                len(all_records) >= total_records
                or len(records) < page_size
            ):
                break
            page += 1
        return all_records
    except Exception as e:
        logger.warning(f"Failed to get missing albums: {e}")
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
