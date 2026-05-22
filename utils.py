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


class BaseNotMountedError(PermissionError):
    """Raised when a configured base directory is not mounted/accessible.

    Distinct from a generic PermissionError so callers can surface a
    config-pointing message instead of "permission denied on /volume1".
    """

    def __init__(self, base_dir):
        self.base_dir = base_dir
        super().__init__(
            f"Base directory '{base_dir}' does not exist inside the container. "
            f"Mount it as a volume or correct the path in settings."
        )


def _try_relax_dir(path):
    """Best-effort: add group write/execute on an existing dir we own.

    Used before creating a sub-directory inside a pre-existing parent
    that lost group-write between runs (e.g. PUID change). Silent on
    failure since we may not own the dir.
    """
    try:
        mode = os.stat(path).st_mode & 0o777
        os.chmod(path, mode | 0o070)
    except OSError:
        pass


def makedirs_within(base_dir, target_path):
    """Create target_path one segment at a time, anchored at base_dir.

    base_dir is expected to exist (i.e. be a mounted volume). If it
    doesn't, try once to create it — and if THAT fails with a
    permission error (typical when ``/volume1`` etc. isn't mounted),
    raise ``BaseNotMountedError`` so the caller can report a clear
    "volume not mounted" message instead of leaking an obscure
    "Permission denied: '/volume1'" from deep in ``os.makedirs``.
    """
    if not os.path.isdir(base_dir):
        try:
            os.makedirs(base_dir, exist_ok=True)
        except (PermissionError, OSError) as exc:
            raise BaseNotMountedError(base_dir) from exc
    try:
        rel = os.path.relpath(target_path, base_dir)
    except ValueError:
        os.makedirs(target_path, exist_ok=True)
        return
    current = base_dir
    for part in rel.split(os.sep):
        if part in ("", ".", ".."):
            continue
        current = os.path.join(current, part)
        try:
            os.mkdir(current)
        except FileExistsError:
            # If we can't write into the existing parent, try once to
            # add group-write to it (covers the case where an earlier
            # run created it under a stricter umask). See issue #66.
            _try_relax_dir(current)
        except PermissionError as exc:
            parent = os.path.dirname(current)
            owner = "?"
            try:
                st = os.stat(parent)
                owner = f"uid={st.st_uid} gid={st.st_gid} mode={oct(st.st_mode & 0o777)}"
            except OSError:
                pass
            raise PermissionError(
                f"Cannot create '{current}': parent '{parent}' is not writable "
                f"by uid={os.geteuid()} gid={os.getegid()} (parent {owner}). "
                f"Fix host ownership or set PUID/PGID to match."
            ) from exc


def makedirs_safe(target_path, known_bases):
    """Create target_path, anchored at the first matching known base.

    If target falls under one of the known bases, walk-create from
    that base (which must already exist — i.e. mounted). If no base
    matches, fall back to a plain ``os.makedirs``.
    """
    real_target = os.path.realpath(target_path)
    for base in known_bases:
        if not base:
            continue
        try:
            real_base = os.path.realpath(base)
        except OSError:
            continue
        if real_target.startswith(real_base + os.sep) or real_target == real_base:
            makedirs_within(base, target_path)
            return
    os.makedirs(target_path, exist_ok=True)


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
