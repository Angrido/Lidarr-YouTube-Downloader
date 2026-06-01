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
    def __init__(self, base_dir):
        self.base_dir = base_dir
        super().__init__(
            f"Base directory '{base_dir}' does not exist inside the container. "
            f"Mount it as a volume or correct the path in settings."
        )


def _try_relax_dir(path):
    try:
        mode = os.stat(path).st_mode & 0o777
        os.chmod(path, mode | 0o070)
    except OSError:
        pass


def relax_dir_permissions(path):
    """Best-effort: make an existing directory group-writable.

    Used before creating a subfolder inside a pre-existing artist
    directory that may have been created by another service (e.g. Lidarr
    or root) with restrictive permissions. Only succeeds when the current
    user owns the directory; otherwise it is a no-op.
    """
    _try_relax_dir(path)


def makedirs_within(base_dir, target_path):
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
            _try_relax_dir(current)
        except PermissionError:
            parent = os.path.dirname(current)
            if parent:
                _try_relax_dir(parent)
            try:
                os.mkdir(current)
            except FileExistsError:
                _try_relax_dir(current)
            except PermissionError as exc:
                owner = "?"
                try:
                    st = os.stat(parent)
                    owner = (
                        f"uid={st.st_uid} gid={st.st_gid} "
                        f"mode={oct(st.st_mode & 0o777)}"
                    )
                except OSError:
                    pass
                raise PermissionError(
                    f"Cannot create '{current}': parent '{parent}' is not writable "
                    f"by uid={os.geteuid()} gid={os.getegid()} (parent {owner}). "
                    f"Fix host ownership or set PUID/PGID to match."
                ) from exc


def makedirs_safe(target_path, known_bases):
    real_target = os.path.realpath(target_path)
    for base in known_bases:
        if not base:
            continue
        try:
            real_base = os.path.realpath(base)
        except OSError:
            continue
        if not (
            real_target.startswith(real_base + os.sep)
            or real_target == real_base
        ):
            continue
        if not os.path.isdir(real_base):
            # Base directory absent: try to create it (normal first-run
            # case). If that fails (e.g. unmounted Docker volume on a
            # read-only filesystem), surface a typed error so the
            # operator knows to mount the volume.
            try:
                os.makedirs(real_base, exist_ok=True)
            except OSError as exc:
                raise BaseNotMountedError(base) from exc
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
