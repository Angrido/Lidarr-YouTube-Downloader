"""Shared utility functions for Lidarr YouTube Downloader."""

import logging
import os
import re
import time
import urllib.parse

from flask import request

logger = logging.getLogger(__name__)


_MAX_FILENAME_BYTES = 200


def sanitize_filename(name):
    """Remove special characters that are invalid in filenames."""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f\x7f]', "", name)
    name = name.replace("..", "").replace("~", "")
    name = name.strip(". ")
    encoded = name.encode("utf-8", "surrogatepass")
    if len(encoded) > _MAX_FILENAME_BYTES:
        name = encoded[:_MAX_FILENAME_BYTES].decode("utf-8", "ignore")
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


# --- Shared by app.py and youtube_import.py ---

DOWNLOAD_DIR = os.getenv("DOWNLOAD_PATH", "")


rate_limit_store = {}


class InvalidRequestBody(ValueError):
    pass


def _json_object():
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise InvalidRequestBody("Request body must be a JSON object")
    return data


def _str_field(data, key, nullable=False):
    value = data.get(key)
    if value is None and (nullable or key not in data):
        return ""
    if not isinstance(value, str):
        raise InvalidRequestBody(f"{key} must be a string")
    return value.strip()


class _YtdlpSilentLogger:
    def debug(self, msg): pass
    def info(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass


def _synthetic_album_data(title, artist, track_count=0):
    """A minimal Lidarr-album-shaped dict for content with no real Lidarr
    album — YouTube playlist imports and their retries. Carries enough for
    tagging, path building and ``_resolve_track_info``; ``artist.id`` is 0
    so ``_refresh_lidarr_artist`` safely no-ops.
    """
    return {
        "title": title or "",
        "artist": {
            "artistName": artist or "",
            "id": 0,
            "foreignArtistId": "",
        },
        "releaseDate": "",
        "trackCount": track_count,
        "foreignAlbumId": "",
        "releases": [],
        "images": [],
    }


def _is_safe_stream_url(stream_url):
    if not isinstance(stream_url, str) or not stream_url:
        return False
    parsed = urllib.parse.urlparse(stream_url)
    if parsed.scheme not in ("http", "https"):
        return False
    hostname = parsed.hostname or ""
    safe_domains = (
        ".googlevideo.com",
        ".youtube.com",
        ".ytimg.com",
        ".googleusercontent.com",
        ".gvt1.com",
        ".ggpht.com",
    )
    return any(
        hostname.endswith(domain) or hostname == domain.lstrip(".")
        for domain in safe_domains
    )


def _validate_youtube_url(youtube_url):
    if not youtube_url.startswith("http"):
        if not re.match(r"^[a-zA-Z0-9_-]{11}$", youtube_url):
            return None
        return f"https://www.youtube.com/watch?v={youtube_url}"  # nosemgrep
    parsed = urllib.parse.urlparse(youtube_url)
    allowed_hosts = {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
        "music.youtube.com",
    }
    if parsed.hostname not in allowed_hosts:
        return None
    return youtube_url


def _writable_path_bases(config):
    """Configured write bases (existence is checked later by makedirs_safe)."""
    bases = []
    seen = set()
    for candidate in (DOWNLOAD_DIR, config.get("lidarr_path", "")):
        if not candidate:
            continue
        try:
            real = os.path.realpath(candidate)
        except OSError:
            continue
        if real in seen:
            continue
        seen.add(real)
        bases.append(candidate)
    return bases


def _resolve_write_base(config):
    """Primary write base for new downloads (same priority as processing.py)."""
    for candidate in (DOWNLOAD_DIR, config.get("lidarr_path", "")):
        if candidate:
            return candidate
    return ""


def _makedirs_bases_for(target_path, config):
    """Bases under which target_path lives; must match makedirs_safe allowlist."""
    real_target = os.path.realpath(target_path)
    matching = []
    for base in _writable_path_bases(config):
        try:
            real_base = os.path.realpath(base)
        except OSError:
            continue
        if real_target.startswith(real_base + os.sep) or real_target == real_base:
            matching.append(base)
    if matching:
        return matching
    write_base = _resolve_write_base(config)
    return [write_base] if write_base else []


def _validate_target_path(target_path, config):
    real_target = os.path.realpath(target_path)
    return any(
        real_target.startswith(os.path.realpath(base) + os.sep)
        or real_target == os.path.realpath(base)
        for base in _writable_path_bases(config)
    )


def _cleanup_temp_files(temp_file):
    for ext in [".mp3", ".opus", ".flac", ".aac", ".ogg", ".webm", ".m4a", ".part"]:
        tmp = temp_file + ext
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError as rm_err:
                logger.debug("Failed to remove temp file %s: %s", tmp, rm_err)
