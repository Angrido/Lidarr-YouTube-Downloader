"""Shared utility functions for Lidarr YouTube Downloader."""

import logging
import os
import re
import time

logger = logging.getLogger(__name__)


def sanitize_filename(name):
    """Remove special characters that are invalid in filenames."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    name = name.replace("..", "").replace("~", "")
    name = name.strip(". ")
    if not name:
        name = "untitled"
    return name


def format_bytes(size_bytes):
    """Format byte count as a human-readable string (B, KB, MB, GB, TB)."""
    if size_bytes <= 0:
        return ""
    for unit in ["B", "KB", "MB", "GB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


def check_rate_limit(key, store, window=2, max_requests=5):
    """Check whether a request is allowed under a sliding-window rate limit.

    Args:
        key: Identifier for the rate-limited resource.
        store: Dict mapping keys to lists of timestamps.
        window: Time window in seconds.
        max_requests: Maximum requests allowed within the window.

    Returns:
        True if the request is allowed, False if rate-limited.
    """
    now = time.time()
    if key not in store:
        store[key] = []
    store[key] = [t for t in store[key] if now - t < window]
    if len(store[key]) >= max_requests:
        return False
    store[key].append(now)
    return True


def get_umask():
    """Parse UMASK from environment variable. Defaults to 002 (775/664 permissions)."""
    umask_str = os.getenv("UMASK", "002").strip()
    try:
        if umask_str.startswith(("0o", "0O")):
            return int(umask_str, 0)
        return int(umask_str, 8)
    except ValueError:
        return 0o002


def set_permissions(path):
    """Set permissions based on UMASK environment variable.

    Default UMASK=002 results in:
    - Directories: 775 (rwxrwxr-x)
    - Files: 664 (rw-rw-r--)
    """
    try:
        umask = get_umask()
        dir_mode = 0o777 & ~umask
        file_mode = 0o666 & ~umask

        if os.path.isdir(path):
            os.chmod(path, dir_mode)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    os.chmod(os.path.join(root, d), dir_mode)
                for f in files:
                    os.chmod(os.path.join(root, f), file_mode)
        else:
            os.chmod(path, file_mode)
    except Exception as e:
        logger.debug(f"Failed to set permissions on {path}: {e}")
