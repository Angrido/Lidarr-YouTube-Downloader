#!/usr/bin/env python3
"""
Migration script to rename existing album directories to include album type.

Old format: {Album Title} ({Year})
New format: {Album Title} ({Year}) [{Type}]

Usage:
    python migrate_directories.py --dry-run              # Preview changes
    python migrate_directories.py                        # Apply changes
    python migrate_directories.py --path /custom/path    # Use custom path

Environment variables (or use --lidarr-url and --lidarr-api-key):
    LIDARR_URL      - Lidarr server URL (e.g., http://localhost:8686)
    LIDARR_API_KEY  - Lidarr API key
    DOWNLOAD_PATH   - Path to scan (can also use --path)
    LIDARR_PATH     - Alternative path if different from DOWNLOAD_PATH
"""

import argparse
import os
import re
import sys
import requests


def parse_args():
    parser = argparse.ArgumentParser(
        description="Migrate album directories to include album type in folder name"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without renaming directories",
    )
    parser.add_argument(
        "--path",
        help="Path to scan for artist/album directories (default: DOWNLOAD_PATH or LIDARR_PATH env)",
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
        "--rescan",
        action="store_true",
        help="Trigger Lidarr library rescan after migration",
    )
    parser.add_argument(
        "-n",
        type=int,
        default=0,
        help="Limit number of directories to rename (0 = unlimited)",
    )
    return parser.parse_args()


def get_config(args):
    """Get configuration from args or environment variables."""
    config = {
        "lidarr_url": args.lidarr_url or os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": args.lidarr_api_key or os.getenv("LIDARR_API_KEY", ""),
        "scan_path": args.path
        or os.getenv("LIDARR_PATH")
        or os.getenv("DOWNLOAD_PATH", ""),
    }

    if not config["lidarr_url"]:
        print("Error: LIDARR_URL not set. Use --lidarr-url or set LIDARR_URL env var.")
        sys.exit(1)

    if not config["lidarr_api_key"]:
        print(
            "Error: LIDARR_API_KEY not set. Use --lidarr-api-key or set LIDARR_API_KEY env var."
        )
        sys.exit(1)

    if not config["scan_path"]:
        print("Error: No path specified. Use --path or set DOWNLOAD_PATH env var.")
        sys.exit(1)

    if not os.path.isdir(config["scan_path"]):
        print(f"Error: Path does not exist or is not a directory: {config['scan_path']}")
        sys.exit(1)

    return config


def lidarr_request(config, endpoint):
    """Make a request to Lidarr API."""
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def lidarr_command(config, command_name, data=None):
    """Send a command to Lidarr."""
    url = f"{config['lidarr_url']}/api/v1/command"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    payload = {"name": command_name}
    if data:
        payload.update(data)
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": str(e)}


def sanitize_filename(name):
    """Remove invalid filesystem characters."""
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return name.strip()


def parse_existing_folder_name(folder_name):
    """
    Parse an existing folder name to extract album title and year.

    Handles formats:
    - "Album Title (2025)"
    - "Album Title (2025) [Type]"  (already migrated)
    - "Album Title"

    Returns: (album_title, year, existing_type)
    """
    # Check if already has type suffix
    type_match = re.match(r"^(.+?) \((\d{4})\) \[([^\]]+)\]$", folder_name)
    if type_match:
        return type_match.group(1), type_match.group(2), type_match.group(3)

    # Check for year only format
    year_match = re.match(r"^(.+?) \((\d{4})\)$", folder_name)
    if year_match:
        return year_match.group(1), year_match.group(2), None

    # No year format
    return folder_name, None, None


def find_matching_album(config, artist_name, album_title, year):
    """
    Find a matching album in Lidarr by artist name, album title, and year.
    Returns the album data including albumType, or None if not found.
    """
    # First, find the artist
    artists = lidarr_request(config, "artist")
    if "error" in artists:
        return None

    matching_artist = None
    for artist in artists:
        if sanitize_filename(artist.get("artistName", "")).lower() == artist_name.lower():
            matching_artist = artist
            break

    if not matching_artist:
        return None

    # Get albums for this artist
    artist_id = matching_artist["id"]
    albums = lidarr_request(config, f"album?artistId={artist_id}")
    if "error" in albums or not isinstance(albums, list):
        return None

    # Find matching album by title and year
    for album in albums:
        album_title_sanitized = sanitize_filename(album.get("title", ""))
        release_year = str(album.get("releaseDate", ""))[:4]

        if album_title_sanitized.lower() == album_title.lower():
            # If we have a year, it should match
            if year and release_year and year != release_year:
                continue
            return album

    return None


def scan_directories(config):
    """
    Scan the path for artist/album directory structure.
    Returns list of (artist_path, album_folder, artist_name, album_title, year, existing_type)
    """
    scan_path = config["scan_path"]
    directories = []

    for artist_folder in os.listdir(scan_path):
        artist_path = os.path.join(scan_path, artist_folder)
        if not os.path.isdir(artist_path):
            continue

        for album_folder in os.listdir(artist_path):
            album_path = os.path.join(artist_path, album_folder)
            if not os.path.isdir(album_path):
                continue

            album_title, year, existing_type = parse_existing_folder_name(album_folder)
            directories.append(
                {
                    "artist_path": artist_path,
                    "album_folder": album_folder,
                    "album_path": album_path,
                    "artist_name": artist_folder,
                    "album_title": album_title,
                    "year": year,
                    "existing_type": existing_type,
                }
            )

    return directories


def migrate_directory(dir_info, album_data, dry_run=True):
    """
    Rename a directory to include album type.
    Returns (success, old_name, new_name, message)
    """
    album_type = album_data.get("albumType", "Album")
    album_title = dir_info["album_title"]
    year = dir_info["year"]

    # Build new folder name
    sanitized_album = sanitize_filename(album_title)
    if year:
        new_folder_name = f"{sanitized_album} ({year}) [{album_type}]"
    else:
        new_folder_name = f"{sanitized_album} [{album_type}]"

    old_path = dir_info["album_path"]
    new_path = os.path.join(dir_info["artist_path"], new_folder_name)

    # Check if rename is needed
    if old_path == new_path:
        return False, dir_info["album_folder"], new_folder_name, "Already correct"

    if os.path.exists(new_path):
        return False, dir_info["album_folder"], new_folder_name, "Target path already exists"

    if dry_run:
        return True, dir_info["album_folder"], new_folder_name, "Would rename"

    try:
        os.rename(old_path, new_path)
        return True, dir_info["album_folder"], new_folder_name, "Renamed"
    except Exception as e:
        return False, dir_info["album_folder"], new_folder_name, f"Error: {e}"


def main():
    args = parse_args()
    config = get_config(args)

    print(f"Scanning: {config['scan_path']}")
    print(f"Lidarr:   {config['lidarr_url']}")
    print(f"Mode:     {'DRY RUN (no changes)' if args.dry_run else 'LIVE (will rename)'}")
    print()

    # Test Lidarr connection
    status = lidarr_request(config, "system/status")
    if "error" in status:
        print(f"Error connecting to Lidarr: {status['error']}")
        sys.exit(1)
    print(f"Connected to Lidarr v{status.get('version', 'unknown')}")
    print()

    # Scan directories
    directories = scan_directories(config)
    print(f"Found {len(directories)} album directories")
    print()

    # Process each directory
    stats = {"renamed": 0, "skipped": 0, "not_found": 0, "errors": 0}
    limit = args.n if args.n > 0 else None

    for dir_info in directories:
        # Stop if we've hit the limit
        if limit and stats["renamed"] >= limit:
            print(f"\nReached limit of {limit} renames, stopping.")
            break

        # Skip if already has type
        if dir_info["existing_type"]:
            print(
                f"  SKIP: {dir_info['artist_name']}/{dir_info['album_folder']} (already has type)"
            )
            stats["skipped"] += 1
            continue

        # Find matching album in Lidarr
        album_data = find_matching_album(
            config,
            dir_info["artist_name"],
            dir_info["album_title"],
            dir_info["year"],
        )

        if not album_data:
            print(
                f"  NOT FOUND: {dir_info['artist_name']}/{dir_info['album_folder']} (not in Lidarr)"
            )
            stats["not_found"] += 1
            continue

        # Migrate the directory
        success, old_name, new_name, message = migrate_directory(
            dir_info, album_data, dry_run=args.dry_run
        )

        if success:
            print(f"  {message}: {dir_info['artist_name']}/{old_name}")
            print(f"        -> {dir_info['artist_name']}/{new_name}")
            stats["renamed"] += 1
        elif "Already correct" in message:
            stats["skipped"] += 1
        else:
            print(f"  ERROR: {dir_info['artist_name']}/{old_name} - {message}")
            stats["errors"] += 1

    # Summary
    print()
    print("=" * 60)
    print("Summary:")
    print(f"  Renamed:    {stats['renamed']}")
    print(f"  Skipped:    {stats['skipped']}")
    print(f"  Not found:  {stats['not_found']}")
    print(f"  Errors:     {stats['errors']}")

    if args.dry_run and stats["renamed"] > 0:
        print()
        print("This was a dry run. Run without --dry-run to apply changes.")

    # Trigger rescan if requested
    if args.rescan and not args.dry_run and stats["renamed"] > 0:
        print()
        print("Triggering Lidarr library rescan...")
        result = lidarr_command(config, "RescanFolders")
        if "error" in result:
            print(f"  Error: {result['error']}")
        else:
            print("  Rescan command sent successfully")


if __name__ == "__main__":
    main()
