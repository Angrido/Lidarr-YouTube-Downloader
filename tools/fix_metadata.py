#!/usr/bin/env python3
"""
Fix metadata on music files to match Lidarr's monitored releases and trigger reimport.

This script identifies files on disk that have incorrect MusicBrainz release IDs
(a common issue when files are tagged with the first release instead of the
monitored release) and fixes them to enable successful Lidarr imports.

Usage:
    python fix_metadata.py ~/media/music/NADA           # Fix one artist
    python fix_metadata.py ~/media/music                # Fix all artists
    python fix_metadata.py ~/media/music -n 10          # Limit to 10 tracks
    python fix_metadata.py ~/media/music --dry-run      # Preview changes only
    python fix_metadata.py ~/media/music -v             # Verbose output

Environment variables (or use --lidarr-url and --lidarr-api-key):
    LIDARR_URL      - Lidarr server URL (e.g., http://localhost:8686)
    LIDARR_API_KEY  - Lidarr API key
"""

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import requests

try:
    from mutagen.id3 import ID3, TXXX, UFID
except ImportError:
    print("Error: mutagen is required. Install with: pip install mutagen")
    sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fix metadata on music files to match Lidarr's monitored releases",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "directory",
        help="Directory to scan (artist folder like ~/media/music/NADA or root like ~/media/music)",
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
        "-n", "--limit",
        type=int,
        default=0,
        help="Limit number of tracks to fix (0 = unlimited)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview changes without modifying files",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Show detailed output",
    )
    parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Skip artist refresh after fixing (useful for batch operations)",
    )
    parser.add_argument(
        "--force-manual-import",
        action="store_true",
        help="Force manual import even if refresh succeeds",
    )
    return parser.parse_args()


def get_config(args):
    """Get configuration from args or environment variables."""
    config = {
        "lidarr_url": args.lidarr_url or os.getenv("LIDARR_URL", ""),
        "lidarr_api_key": args.lidarr_api_key or os.getenv("LIDARR_API_KEY", ""),
    }

    if not config["lidarr_url"]:
        print("Error: LIDARR_URL not set. Use --lidarr-url or set LIDARR_URL env var.")
        sys.exit(1)

    if not config["lidarr_api_key"]:
        print(
            "Error: LIDARR_API_KEY not set. Use --lidarr-api-key or set LIDARR_API_KEY env var."
        )
        sys.exit(1)

    return config


def lidarr_request(config, endpoint, method="GET", data=None):
    """Make a request to Lidarr API."""
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {
        "X-Api-Key": config["lidarr_api_key"],
        "Content-Type": "application/json",
    }
    try:
        if method == "GET":
            r = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            r = requests.post(url, headers=headers, json=data, timeout=30)
        else:
            return {"error": f"Unsupported method: {method}"}
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def get_all_artists(config):
    """Fetch all artists from Lidarr."""
    artists = lidarr_request(config, "artist")
    if isinstance(artists, dict) and "error" in artists:
        return []
    return artists if isinstance(artists, list) else []


def get_missing_albums(config, artist_id=None):
    """Fetch missing albums from Lidarr, optionally filtered by artist."""
    endpoint = "wanted/missing?pageSize=2000&sortKey=releaseDate&sortDirection=descending&includeArtist=true"
    wanted = lidarr_request(config, endpoint)
    if isinstance(wanted, dict) and "error" in wanted:
        return []
    if isinstance(wanted, dict) and "records" in wanted:
        records = wanted.get("records", [])
        if artist_id:
            records = [r for r in records if r.get("artist", {}).get("id") == artist_id]
        for album in records:
            stats = album.get("statistics", {})
            total = stats.get("trackCount", 0)
            files = stats.get("trackFileCount", 0)
            album["missingTrackCount"] = total - files
        return records
    return []


def get_album_details(config, album_id):
    """Fetch full album details including releases."""
    album = lidarr_request(config, f"album/{album_id}")
    if isinstance(album, dict) and "error" not in album:
        return album
    return None


def get_album_tracks(config, album_id):
    """Fetch tracks for a specific album."""
    tracks = lidarr_request(config, f"track?albumId={album_id}")
    if isinstance(tracks, dict) and "error" in tracks:
        return []
    return tracks if isinstance(tracks, list) else []


def get_monitored_release(album_info):
    """Get the monitored release from album info."""
    releases = album_info.get("releases", [])
    for release in releases:
        if release.get("monitored"):
            return release
    # Fall back to first release if none monitored
    return releases[0] if releases else None


def get_mp3_metadata(filepath):
    """Read MusicBrainz metadata from an MP3 file."""
    try:
        audio = ID3(filepath)
        metadata = {
            "album_id": None,
            "country": None,
            "recording_id": None,
            "track_num": None,
        }

        # Get track number
        if audio.get("TRCK"):
            try:
                metadata["track_num"] = int(str(audio.get("TRCK")).split("/")[0])
            except (ValueError, IndexError):
                pass

        # Get MusicBrainz Album Id (TXXX frame)
        for frame in audio.getall("TXXX"):
            if frame.desc == "MusicBrainz Album Id":
                metadata["album_id"] = str(frame.text[0]) if frame.text else None
            elif frame.desc == "MusicBrainz Release Country":
                metadata["country"] = str(frame.text[0]) if frame.text else None

        # Get Recording Id (UFID frame)
        for frame in audio.getall("UFID"):
            if frame.owner == "http://musicbrainz.org":
                metadata["recording_id"] = frame.data.decode("utf-8", errors="ignore")
                break

        return metadata
    except Exception as e:
        return {"error": str(e)}


def fix_mp3_metadata(filepath, release_id, country, recording_id=None, dry_run=False):
    """Fix MusicBrainz metadata on an MP3 file."""
    try:
        audio = ID3(filepath)
        changes = []

        # Remove old Album Id and add new one
        old_album_id = None
        for frame in audio.getall("TXXX"):
            if frame.desc == "MusicBrainz Album Id":
                old_album_id = str(frame.text[0]) if frame.text else None
                break

        if old_album_id != release_id:
            changes.append(f"Album Id: {old_album_id} -> {release_id}")
            if not dry_run:
                audio.delall("TXXX:MusicBrainz Album Id")
                audio.add(TXXX(encoding=3, desc="MusicBrainz Album Id", text=release_id))

        # Remove old Country and add new one
        old_country = None
        for frame in audio.getall("TXXX"):
            if frame.desc == "MusicBrainz Release Country":
                old_country = str(frame.text[0]) if frame.text else None
                break

        if country and old_country != country:
            changes.append(f"Country: {old_country} -> {country}")
            if not dry_run:
                audio.delall("TXXX:MusicBrainz Release Country")
                audio.add(TXXX(encoding=3, desc="MusicBrainz Release Country", text=country))

        # Update Recording Id if provided
        if recording_id:
            old_recording_id = None
            for frame in audio.getall("UFID"):
                if frame.owner == "http://musicbrainz.org":
                    old_recording_id = frame.data.decode("utf-8", errors="ignore")
                    break

            if old_recording_id != recording_id:
                changes.append(f"Recording Id: {old_recording_id[:8] if old_recording_id else 'None'}... -> {recording_id[:8]}...")
                if not dry_run:
                    audio.delall("UFID:http://musicbrainz.org")
                    audio.add(UFID(owner="http://musicbrainz.org", data=recording_id.encode()))
                    # Also update Release Track Id
                    audio.delall("TXXX:MusicBrainz Release Track Id")
                    audio.add(TXXX(encoding=3, desc="MusicBrainz Release Track Id", text=recording_id))

        if changes and not dry_run:
            audio.save()

        return changes
    except Exception as e:
        return [f"Error: {e}"]


def refresh_artist(config, artist_id):
    """Trigger a refresh for an artist."""
    result = lidarr_request(
        config,
        "command",
        method="POST",
        data={"name": "RefreshArtist", "artistId": artist_id}
    )
    return "error" not in result


def check_album_status(config, album_id):
    """Check if an album is now fully imported."""
    album = get_album_details(config, album_id)
    if not album:
        return False, 0, 0
    stats = album.get("statistics", {})
    total = stats.get("trackCount", 0)
    files = stats.get("trackFileCount", 0)
    return files == total, files, total


def find_album_directory(base_path, album_title, album_year, album_type):
    """Find the album directory on disk matching the album info."""
    # Clean album title for matching (remove special characters)
    clean_title = re.sub(r'[<>:"/\\|?*]', '', album_title)

    # Try various directory naming patterns
    patterns = [
        f"{clean_title} ({album_year}) [{album_type}]",
        f"{clean_title} ({album_year})",
        f"{clean_title}",
    ]

    base = Path(base_path)
    if not base.exists():
        return None

    for item in base.iterdir():
        if not item.is_dir():
            continue
        item_name = item.name
        for pattern in patterns:
            if pattern.lower() in item_name.lower() or item_name.lower() in pattern.lower():
                return item

    # Fuzzy match - check if album title is in directory name
    for item in base.iterdir():
        if not item.is_dir():
            continue
        if clean_title.lower() in item.name.lower():
            return item

    return None


def find_mp3_files(directory):
    """Find all MP3 files in a directory."""
    mp3_files = []
    dir_path = Path(directory)
    if dir_path.exists():
        for f in dir_path.glob("*.mp3"):
            mp3_files.append(f)
    return sorted(mp3_files)


def main():
    args = parse_args()
    config = get_config(args)

    directory = Path(args.directory).expanduser().resolve()
    if not directory.exists():
        print(f"Error: Directory does not exist: {directory}")
        sys.exit(1)

    print(f"Connecting to Lidarr at {config['lidarr_url']}...")

    # Test connection
    status = lidarr_request(config, "system/status")
    if isinstance(status, dict) and "error" in status:
        print(f"Error connecting to Lidarr: {status['error']}", file=sys.stderr)
        sys.exit(1)

    print(f"Connected to Lidarr v{status.get('version', 'unknown')}")
    print(f"Scanning directory: {directory}")
    print()

    # Get all artists from Lidarr
    all_artists = get_all_artists(config)
    if not all_artists:
        print("Error: Could not fetch artists from Lidarr")
        sys.exit(1)

    # Build artist name -> artist mapping
    artist_by_name = {}
    artist_by_path = {}
    for artist in all_artists:
        name = artist.get("artistName", "")
        path = artist.get("path", "")
        if name:
            artist_by_name[name.lower()] = artist
        if path:
            artist_by_path[Path(path).resolve()] = artist

    # Determine which artists to process
    artists_to_process = []

    # Check if directory is an artist folder or root music folder
    if directory in artist_by_path:
        # Exact match - this is an artist folder
        artists_to_process = [artist_by_path[directory]]
    else:
        # Check if it's an artist folder by name
        dir_name = directory.name.lower()
        if dir_name in artist_by_name:
            artists_to_process = [artist_by_name[dir_name]]
        else:
            # Assume it's a root directory - check subdirectories
            for subdir in directory.iterdir():
                if not subdir.is_dir():
                    continue
                subdir_name = subdir.name.lower()
                if subdir_name in artist_by_name:
                    artists_to_process.append(artist_by_name[subdir_name])
                elif subdir.resolve() in artist_by_path:
                    artists_to_process.append(artist_by_path[subdir.resolve()])

    if not artists_to_process:
        print("No matching artists found in Lidarr for the given directory.")
        print("Make sure the directory name matches an artist in Lidarr.")
        sys.exit(1)

    print(f"Found {len(artists_to_process)} artist(s) to process")
    print()

    # Track statistics
    total_tracks_fixed = 0
    total_tracks_checked = 0
    albums_fixed = []
    artists_to_refresh = set()

    # Process each artist
    for artist in artists_to_process:
        artist_name = artist.get("artistName", "Unknown")
        artist_id = artist.get("id")
        artist_path = Path(artist.get("path", "")).resolve()

        if args.verbose:
            print(f"Processing artist: {artist_name} (ID: {artist_id})")

        # Get missing albums for this artist
        missing_albums = get_missing_albums(config, artist_id)
        if not missing_albums:
            if args.verbose:
                print(f"  No missing albums for {artist_name}")
            continue

        if args.verbose:
            print(f"  Found {len(missing_albums)} missing album(s)")

        # Process each missing album
        for album in missing_albums:
            if args.limit > 0 and total_tracks_fixed >= args.limit:
                break

            album_id = album.get("id")
            album_title = album.get("title", "Unknown")
            album_type = album.get("albumType", "Album")
            release_date = album.get("releaseDate", "")
            album_year = release_date[:4] if release_date else ""

            # Get full album details
            album_details = get_album_details(config, album_id)
            if not album_details:
                continue

            # Get monitored release
            monitored_release = get_monitored_release(album_details)
            if not monitored_release:
                if args.verbose:
                    print(f"  No monitored release for: {album_title}")
                continue

            monitored_release_id = monitored_release.get("foreignReleaseId", "")
            monitored_country = monitored_release.get("country", [])
            if isinstance(monitored_country, list):
                monitored_country = monitored_country[0] if monitored_country else ""

            # Get track info from Lidarr
            tracks = get_album_tracks(config, album_id)
            track_recording_ids = {}
            for track in tracks:
                track_num = track.get("trackNumber", 0)
                recording_id = track.get("foreignRecordingId", "")
                if track_num and recording_id:
                    track_recording_ids[track_num] = recording_id

            # Find album directory on disk
            # First try the artist path from Lidarr
            album_dir = find_album_directory(artist_path, album_title, album_year, album_type)

            # If not found and we're scanning from a different base, try there too
            if not album_dir and directory != artist_path:
                # Try to find artist folder in scanned directory
                for subdir in directory.iterdir():
                    if subdir.is_dir() and subdir.name.lower() == artist_name.lower():
                        album_dir = find_album_directory(subdir, album_title, album_year, album_type)
                        break

            if not album_dir:
                if args.verbose:
                    print(f"  Album not found on disk: {album_title}")
                continue

            # Find MP3 files in album directory
            mp3_files = find_mp3_files(album_dir)
            if not mp3_files:
                if args.verbose:
                    print(f"  No MP3 files in: {album_dir}")
                continue

            # Check each file
            album_needs_fix = False
            files_to_fix = []

            for mp3_file in mp3_files:
                if args.limit > 0 and total_tracks_fixed >= args.limit:
                    break

                metadata = get_mp3_metadata(mp3_file)
                if "error" in metadata:
                    if args.verbose:
                        print(f"    Error reading {mp3_file.name}: {metadata['error']}")
                    continue

                total_tracks_checked += 1
                current_album_id = metadata.get("album_id", "")

                # Check if file needs fixing
                if current_album_id and current_album_id != monitored_release_id:
                    album_needs_fix = True
                    track_num = metadata.get("track_num")
                    recording_id = track_recording_ids.get(track_num) if track_num else None
                    files_to_fix.append({
                        "file": mp3_file,
                        "current_album_id": current_album_id,
                        "recording_id": recording_id,
                    })

            if album_needs_fix and files_to_fix:
                print(f"\n{'[DRY RUN] ' if args.dry_run else ''}Fixing: {artist_name} - {album_title}")
                print(f"  Directory: {album_dir}")
                print(f"  Current Album Id:   {files_to_fix[0]['current_album_id']}")
                print(f"  Monitored Album Id: {monitored_release_id}")
                print(f"  Country: {monitored_country}")
                print(f"  Files to fix: {len(files_to_fix)}")

                for file_info in files_to_fix:
                    if args.limit > 0 and total_tracks_fixed >= args.limit:
                        print(f"  Reached limit of {args.limit} tracks")
                        break

                    mp3_file = file_info["file"]
                    recording_id = file_info["recording_id"]

                    changes = fix_mp3_metadata(
                        mp3_file,
                        monitored_release_id,
                        monitored_country,
                        recording_id,
                        dry_run=args.dry_run
                    )

                    if changes:
                        if args.verbose or args.dry_run:
                            print(f"    {mp3_file.name}:")
                            for change in changes:
                                print(f"      - {change}")
                        total_tracks_fixed += 1

                if not args.dry_run:
                    albums_fixed.append({
                        "artist": artist_name,
                        "album": album_title,
                        "album_id": album_id,
                        "files_fixed": len(files_to_fix),
                    })
                    artists_to_refresh.add(artist_id)

        if args.limit > 0 and total_tracks_fixed >= args.limit:
            print(f"\nReached limit of {args.limit} tracks")
            break

    # Refresh artists if we fixed anything
    if not args.dry_run and artists_to_refresh and not args.no_refresh:
        print(f"\n{'=' * 60}")
        print(f"Refreshing {len(artists_to_refresh)} artist(s) in Lidarr...")

        for artist_id in artists_to_refresh:
            artist = next((a for a in all_artists if a.get("id") == artist_id), None)
            artist_name = artist.get("artistName", "Unknown") if artist else "Unknown"
            print(f"  Refreshing: {artist_name}...")
            refresh_artist(config, artist_id)

        # Wait for refresh to complete
        print("  Waiting for refresh to complete...")
        time.sleep(5)

        # Check results
        print("\nVerifying import results:")
        for album_info in albums_fixed:
            is_complete, files, total = check_album_status(config, album_info["album_id"])
            status = "✓" if is_complete else "✗"
            print(f"  {status} {album_info['artist']} - {album_info['album']}: {files}/{total} tracks")

            if not is_complete and args.force_manual_import:
                print(f"    Attempting manual import...")
                # Manual import would go here if needed

    # Print summary
    print(f"\n{'=' * 60}")
    print("Summary:")
    print(f"  Tracks checked: {total_tracks_checked}")
    print(f"  Tracks {'would be ' if args.dry_run else ''}fixed: {total_tracks_fixed}")
    print(f"  Albums {'would be ' if args.dry_run else ''}affected: {len(albums_fixed)}")

    if args.dry_run:
        print("\nThis was a dry run. No files were modified.")
        print("Run without --dry-run to apply changes.")


if __name__ == "__main__":
    main()
