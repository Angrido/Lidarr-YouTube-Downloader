#!/usr/bin/env python3
"""
Verify downloaded audio files against MusicBrainz recordings using AcoustID.

Compares the acoustic fingerprint of each MP3 against the MusicBrainz recording
ID stored in its ID3 tags to detect mismatched or wrong tracks.

Usage:
    python verify_fingerprints.py /path/to/artist --acoustid-api-key KEY
    python verify_fingerprints.py /path/to/music -a --acoustid-api-key KEY
    python verify_fingerprints.py --acoustid-api-key KEY   # uses Lidarr artists
    python verify_fingerprints.py /path -a --acoustid-api-key KEY | jq '.status'

Environment variables (also read from ./.env and ~/.env):
    LIDARR_URL         - Lidarr server URL (e.g., http://localhost:8686)
    LIDARR_API_KEY     - Lidarr API key
    ACOUSTID_API_KEY   - AcoustID API key (get one at https://acoustid.org/new-application)

Requires fpcalc (chromaprint) to be installed:
    brew install chromaprint    # macOS
    apk add chromaprint         # Alpine
    apt install libchromaprint-tools  # Debian/Ubuntu
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import requests

try:
    from mutagen.id3 import ID3
except ImportError:
    print("Error: mutagen is required. Install with: pip install mutagen")
    sys.exit(1)

ACOUSTID_API_URL = "https://api.acoustid.org/v2/lookup"
RATE_LIMIT_INTERVAL = 0.34  # ~3 requests per second

_last_request_time = 0.0


DOTENV_KEYS = {"LIDARR_URL", "LIDARR_API_KEY", "ACOUSTID_API_KEY"}


def load_dotenv():
    """Load allowed keys from .env files into os.environ.

    Checks ./.env first, then $HOME/.env. The first value found for a key
    wins — existing env vars are never overwritten.
    """
    candidates = [
        Path.cwd() / ".env",
        Path.home() / ".env",
    ]
    for env_path in candidates:
        if not env_path.is_file():
            continue
        for line in env_path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key in DOTENV_KEYS and key not in os.environ:
                os.environ[key] = value


def parse_args():
    parser = argparse.ArgumentParser(
        description="Verify audio files against MusicBrainz using AcoustID fingerprinting",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "directory",
        nargs="?",
        help="Path to artist folder or root music folder",
    )
    parser.add_argument(
        "--lidarr-url",
        help="Lidarr server URL (default: LIDARR_URL env)",
    )
    parser.add_argument(
        "--lidarr-api-key",
        help="Lidarr API key (default: LIDARR_API_KEY env)",
    )
    parser.add_argument(
        "--acoustid-api-key",
        help="AcoustID API key (default: ACOUSTID_API_KEY env)",
    )
    parser.add_argument(
        "-a", "--all-artists",
        action="store_true",
        help="Treat directory as root folder, scan all artist subdirs",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=0.85,
        help="Confidence threshold for matching (default: 0.85)",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        dest="show_all",
        help="Include verified matches in output (default: mismatches only)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON instead of a human-readable table",
    )
    parser.add_argument(
        "-n", "--limit",
        type=int,
        default=0,
        help="Max files to check (0 = unlimited)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Extra progress on stderr",
    )
    return parser.parse_args()


def get_config(args):
    """Extract and validate config from args and env vars."""
    config = {
        "lidarr_url": args.lidarr_url or os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": args.lidarr_api_key or os.getenv("LIDARR_API_KEY", ""),
        "acoustid_api_key": args.acoustid_api_key or os.getenv("ACOUSTID_API_KEY", ""),
    }
    if not config["acoustid_api_key"]:
        log("Error: AcoustID API key required. "
            "Use --acoustid-api-key or set ACOUSTID_API_KEY env var.")
        log("Get a free key at https://acoustid.org/new-application")
        sys.exit(1)
    return config


def check_fpcalc():
    """Verify fpcalc binary exists at startup."""
    if not shutil.which("fpcalc"):
        log("Error: fpcalc not found. Install chromaprint:")
        log("  macOS:   brew install chromaprint")
        log("  Alpine:  apk add chromaprint")
        log("  Debian:  apt install libchromaprint-tools")
        sys.exit(1)


def lidarr_request(config, endpoint):
    """GET request to Lidarr API."""
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def resolve_scan_paths(config, args):
    """Determine directories to scan based on args and Lidarr config."""
    if args.directory:
        directory = Path(args.directory).expanduser().resolve()
        if not directory.exists():
            log(f"Error: Directory does not exist: {directory}")
            sys.exit(1)
        if args.all_artists:
            paths = sorted(
                p for p in directory.iterdir() if p.is_dir()
            )
            if not paths:
                log(f"Error: No subdirectories found in {directory}")
                sys.exit(2)
            return paths
        return [directory]

    if not config["lidarr_url"] or not config["lidarr_api_key"]:
        log("Error: No directory provided and Lidarr not configured.")
        log("Provide a directory or set LIDARR_URL and LIDARR_API_KEY.")
        sys.exit(1)

    artists = lidarr_request(config, "artist")
    if isinstance(artists, dict) and "error" in artists:
        log(f"Error fetching artists from Lidarr: {artists['error']}")
        sys.exit(1)
    if not isinstance(artists, list) or not artists:
        log("Error: No artists found in Lidarr")
        sys.exit(1)

    paths = []
    for artist in artists:
        artist_path = artist.get("path", "")
        if artist_path and Path(artist_path).exists():
            paths.append(Path(artist_path))
    if not paths:
        log("Error: No valid artist paths found from Lidarr")
        sys.exit(2)
    return sorted(paths)


def find_mp3_files(directory):
    """Find all MP3 files recursively in a directory."""
    return sorted(Path(directory).rglob("*.mp3"))


def extract_tag_metadata(filepath):
    """Read UFID recording ID and basic tags via mutagen."""
    try:
        audio = ID3(filepath)
    except Exception:
        return None

    recording_id = None
    for frame in audio.getall("UFID"):
        if frame.owner == "http://musicbrainz.org":
            recording_id = frame.data.decode("utf-8", errors="ignore")
            break

    artist = str(audio.get("TPE1", "")) or None
    album = str(audio.get("TALB", "")) or None
    title = str(audio.get("TIT2", "")) or None

    return {
        "recording_id": recording_id,
        "artist": artist,
        "album": album,
        "track": title,
    }


def run_fpcalc(filepath):
    """Run fpcalc and return duration + fingerprint."""
    try:
        result = subprocess.run(
            ["fpcalc", "-json", str(filepath)],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return {
            "duration": data.get("duration"),
            "fingerprint": data.get("fingerprint"),
        }
    except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError):
        return None


def throttle():
    """Rate limit to ~3 requests per second for AcoustID API."""
    global _last_request_time
    elapsed = time.monotonic() - _last_request_time
    if elapsed < RATE_LIMIT_INTERVAL:
        time.sleep(RATE_LIMIT_INTERVAL - elapsed)
    _last_request_time = time.monotonic()


def lookup_acoustid(api_key, duration, fingerprint):
    """Look up a fingerprint against the AcoustID API."""
    params = {
        "client": api_key,
        "duration": int(duration),
        "fingerprint": fingerprint,
        "meta": "recordings",
    }
    try:
        r = requests.get(ACOUSTID_API_URL, params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get("status") != "ok":
            return None
        return data.get("results", [])
    except requests.exceptions.RequestException:
        return None


def compare_fingerprint(expected_id, results, threshold):
    """Compare expected recording ID against AcoustID results.

    Returns (status, matched_id, score).
    """
    if not results:
        return "unverified", None, 0.0

    for result in results:
        score = result.get("score", 0.0)
        if score < threshold:
            continue
        for recording in result.get("recordings", []):
            if recording.get("id") == expected_id:
                return "verified", expected_id, score

    best_score = 0.0
    best_id = None
    for result in results:
        score = result.get("score", 0.0)
        for recording in result.get("recordings", []):
            if score > best_score:
                best_score = score
                best_id = recording.get("id")

    if best_id:
        return "mismatch", best_id, best_score
    return "unverified", None, 0.0


STATUS_ORDER = ["mismatch", "unverified", "no_id", "error", "verified"]
STATUS_LABELS = {
    "mismatch": "Mismatch",
    "unverified": "Unverified",
    "no_id": "No ID in Tags",
    "error": "Error",
    "verified": "Verified",
}


def relative_path(filepath, base_dir):
    """Return filepath relative to base_dir, or just the filename."""
    try:
        return str(Path(filepath).relative_to(base_dir))
    except ValueError:
        return str(filepath)


def print_table(results, base_dir):
    """Print a human-readable table of results to stdout."""
    if not results:
        return

    file_col = max(len(relative_path(r["file"], base_dir)) for r in results)
    file_col = max(file_col, 4)

    header = (
        f"{'Status':<14}  "
        f"{'File':<{file_col}}  "
        f"{'Score':>5}  "
        f"{'Expected ID':<36}  "
        f"{'Matched ID':<36}"
    )
    print(header)
    print("-" * len(header))

    for result in results:
        rel = relative_path(result["file"], base_dir)
        label = STATUS_LABELS.get(result["status"], result["status"])
        score = f"{result['score']:.2f}" if result["score"] else "  -  "
        expected = result.get("expected_id") or "-"
        matched = result.get("matched_id") or "-"
        print(
            f"{label:<14}  "
            f"{rel:<{file_col}}  "
            f"{score:>5}  "
            f"{expected:<36}  "
            f"{matched:<36}"
        )


def print_json(results, base_dir):
    """Print grouped JSON output to stdout."""
    groups = []
    for status in STATUS_ORDER:
        items = [r for r in results if r["status"] == status]
        if not items:
            continue
        group_items = []
        for r in items:
            group_items.append({
                "file": relative_path(r["file"], base_dir),
                "artist": r.get("artist"),
                "album": r.get("album"),
                "track": r.get("track"),
                "expected_id": r.get("expected_id"),
                "matched_id": r.get("matched_id"),
                "score": r["score"],
            })
        groups.append({
            "status": status,
            "label": STATUS_LABELS.get(status, status),
            "count": len(group_items),
            "files": group_items,
        })
    print(json.dumps(groups, indent=2, ensure_ascii=False))


def log(msg):
    """Print to stderr."""
    print(msg, file=sys.stderr)


def process_file(filepath, api_key, threshold):
    """Per-file verification pipeline. Returns a result dict."""
    tags = extract_tag_metadata(filepath)
    base = {
        "file": str(filepath),
        "artist": tags["artist"] if tags else None,
        "album": tags["album"] if tags else None,
        "track": tags["track"] if tags else None,
    }

    if not tags or not tags["recording_id"]:
        return {**base, "status": "no_id",
                "expected_id": None, "matched_id": None, "score": 0.0}

    expected_id = tags["recording_id"]
    fp = run_fpcalc(filepath)
    if not fp or not fp["fingerprint"]:
        return None  # skip silently on fpcalc failure

    throttle()
    results = lookup_acoustid(api_key, fp["duration"], fp["fingerprint"])
    if results is None:
        return {**base, "status": "unverified",
                "expected_id": expected_id, "matched_id": None, "score": 0.0}

    status, matched_id, score = compare_fingerprint(
        expected_id, results, threshold
    )
    return {**base, "status": status,
            "expected_id": expected_id, "matched_id": matched_id,
            "score": round(score, 4)}


def main():
    args = parse_args()
    load_dotenv()
    config = get_config(args)
    check_fpcalc()

    scan_paths = resolve_scan_paths(config, args)
    base_dir = (
        Path(args.directory).expanduser().resolve() if args.directory else None
    )

    if args.verbose:
        log(f"Scanning {len(scan_paths)} path(s)")

    counts = {"verified": 0, "mismatch": 0, "unverified": 0, "no_id": 0, "errors": 0}
    all_results = []
    files_scanned = 0

    for scan_path in scan_paths:
        if args.verbose:
            log(f"Scanning: {scan_path}")

        mp3_files = find_mp3_files(scan_path)
        if not mp3_files and args.verbose:
            log(f"  No MP3 files found in {scan_path}")

        for filepath in mp3_files:
            if args.limit > 0 and files_scanned >= args.limit:
                break

            if args.verbose:
                log(f"  Checking: {filepath.name}")

            try:
                result = process_file(filepath, config["acoustid_api_key"], args.threshold)
            except Exception as e:
                log(f"  Error processing {filepath}: {e}")
                counts["errors"] += 1
                all_results.append({
                    "file": str(filepath), "status": "error",
                    "artist": None, "album": None, "track": None,
                    "expected_id": None, "matched_id": None, "score": 0.0,
                })
                files_scanned += 1
                continue

            if result is None:
                if args.verbose:
                    log(f"  Skipped (fpcalc failed): {filepath.name}")
                counts["errors"] += 1
                all_results.append({
                    "file": str(filepath), "status": "error",
                    "artist": None, "album": None, "track": None,
                    "expected_id": None, "matched_id": None, "score": 0.0,
                })
                files_scanned += 1
                continue

            files_scanned += 1
            counts[result["status"]] = counts.get(result["status"], 0) + 1
            all_results.append(result)

        if args.limit > 0 and files_scanned >= args.limit:
            break

    if files_scanned == 0:
        log("Error: No MP3 files found")
        sys.exit(2)

    output_results = sorted(
        all_results,
        key=lambda r: STATUS_ORDER.index(r["status"])
        if r["status"] in STATUS_ORDER else len(STATUS_ORDER),
    )
    if not args.show_all:
        output_results = [r for r in output_results if r["status"] != "verified"]

    if base_dir is None:
        base_dir = Path("/")

    if args.json_output:
        print_json(output_results, base_dir)
    else:
        if output_results:
            print_table(output_results, base_dir)
        log("")
        log("Summary:")
        log(f"  Files scanned: {files_scanned}")
        log(f"  Verified:      {counts['verified']}")
        log(f"  Mismatches:    {counts['mismatch']}")
        log(f"  Unverified:    {counts['unverified']}")
        log(f"  No ID in tags: {counts['no_id']}")
        log(f"  Errors:        {counts['errors']}")


if __name__ == "__main__":
    main()
