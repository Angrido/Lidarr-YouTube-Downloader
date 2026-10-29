import hashlib
import importlib.metadata
import io
import logging
import os
import re
import shutil
import sys
import tempfile
import zipfile

import requests

logger = logging.getLogger(__name__)

PACKAGES_DIR = "/config/ytdlp"
PYPI_JSON = "https://pypi.org/pypi/{}/{}/json"
EJS_PIN = re.compile(r"^yt-dlp-ejs==([\w.]+)\s*;.*extra == ['\"]default['\"]")


def version_key(version):
    return tuple(int(part) for part in re.findall(r"\d+", version or ""))


def saved_version(packages_dir=PACKAGES_DIR):
    try:
        names = os.listdir(packages_dir)
    except OSError:
        return None
    for name in names:
        match = re.fullmatch(r"yt_dlp-([\w.]+)\.dist-info", name)
        if match:
            return match.group(1)
    return None


def activate(packages_dir=PACKAGES_DIR):
    saved = saved_version(packages_dir)
    if not saved:
        return None
    try:
        bundled = importlib.metadata.version("yt-dlp")
    except importlib.metadata.PackageNotFoundError:
        bundled = ""
    if version_key(saved) <= version_key(bundled):
        shutil.rmtree(packages_dir, ignore_errors=True)
        return None
    sys.path.insert(0, packages_dir)
    return saved


def _release(name, version):
    resp = requests.get(PYPI_JSON.format(name, version), timeout=15)
    resp.raise_for_status()
    return resp.json()


def _pure_wheel(release):
    for file in release.get("urls", []):
        if file.get("packagetype") == "bdist_wheel" and file.get("filename", "").endswith("-py3-none-any.whl"):
            return file
    raise RuntimeError(f"No pure-Python wheel for {release['info']['name']} {release['info']['version']}")


def _download_wheel(file):
    resp = requests.get(file["url"], timeout=60)
    resp.raise_for_status()
    if hashlib.sha256(resp.content).hexdigest() != file["digests"]["sha256"]:
        raise RuntimeError(f"Checksum mismatch for {file['filename']}")
    return resp.content


def _ejs_version(release):
    for requirement in release["info"].get("requires_dist") or []:
        match = EJS_PIN.match(requirement)
        if match:
            return match.group(1)
    return None


def install(version, packages_dir=PACKAGES_DIR):
    release = _release("yt-dlp", version)
    releases = [release]
    ejs = _ejs_version(release)
    if ejs:
        releases.append(_release("yt-dlp-ejs", ejs))
    wheels = [_download_wheel(_pure_wheel(r)) for r in releases]
    parent = os.path.dirname(packages_dir)
    staging = tempfile.mkdtemp(dir=parent, prefix=".ytdlp-")
    try:
        for wheel in wheels:
            with zipfile.ZipFile(io.BytesIO(wheel)) as archive:
                for member in archive.namelist():
                    target = os.path.realpath(os.path.join(staging, member))
                    if not target.startswith(os.path.realpath(staging) + os.sep):
                        raise RuntimeError(f"Unsafe path in wheel: {member}")
                archive.extractall(staging)
        old = packages_dir + ".old"
        shutil.rmtree(old, ignore_errors=True)
        if os.path.exists(packages_dir):
            os.replace(packages_dir, old)
        os.replace(staging, packages_dir)
        shutil.rmtree(old, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    logger.info("yt-dlp %s saved to %s", version, packages_dir)
    return version
