#!/usr/bin/env python3
"""
List missing albums and tracks from Lidarr for debugging.

Usage:
    python list_missing.py                           # List all missing albums
    python list_missing.py --verbose                 # Show track-level details
    python list_missing.py --artist "Artist Name"    # Filter by artist
    python list_missing.py --limit 10                # Limit output
    python list_missing.py --json                    # Output as JSON

Environment variables (or use --lidarr-url and --lidarr-api-key):
    LIDARR_URL      - Lidarr server URL (e.g., http://localhost:8686)
    LIDARR_API_KEY  - Lidarr API key
"""

import argparse
import json
import os
import sys
from datetime import datetime

import requests


def parse_args():
    parser = argparse.ArgumentParser(
        description="List missing albums and tracks from Lidarr"
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
        "--verbose", "-v",
        action="store_true",
        help="Show track-level details for each album",
    )
    parser.add_argument(
        "--artist", "-a",
        help="Filter by artist name (case-insensitive partial match)",
    )
    parser.add_argument(
        "--album",
        help="Filter by album title (case-insensitive partial match)",
    )
    parser.add_argument(
        "--limit", "-n",
        type=int,
        default=0,
        help="Limit number of albums to show (0 = unlimited)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="Output as JSON instead of formatted text",
    )
    parser.add_argument(
        "--sort",
        choices=["date", "artist", "album", "missing"],
        default="date",
        help="Sort order: date (default), artist, album, or missing (track count)",
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


def lidarr_request(config, endpoint):
    """Make a request to Lidarr API."""
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {"X-Api-Key": config["lidarr_api_key"]}
    try:
        r = requests.get(url, headers=headers, timeout=30)
        r.raise_for_status()
        return r.json()
    except requests.exceptions.RequestException as e:
        return {"error": str(e)}


def get_missing_albums(config):
    """Fetch missing albums from Lidarr."""
    wanted = lidarr_request(
        config,
        "wanted/missing?pageSize=2000&sortKey=releaseDate&sortDirection=descending&includeArtist=true"
    )
    if isinstance(wanted, dict) and "error" in wanted:
        return wanted
    if isinstance(wanted, dict) and "records" in wanted:
        records = wanted.get("records", [])
        for album in records:
            stats = album.get("statistics", {})
            total = stats.get("trackCount", 0)
            files = stats.get("trackFileCount", 0)
            album["missingTrackCount"] = total - files
        return records
    return []


def get_album_tracks(config, album_id):
    """Fetch tracks for a specific album."""
    tracks = lidarr_request(config, f"track?albumId={album_id}")
    if isinstance(tracks, dict) and "error" in tracks:
        return []
    return tracks if isinstance(tracks, list) else []


def get_album_details(config, album_id):
    """Fetch full album details including releases."""
    album = lidarr_request(config, f"album/{album_id}")
    if isinstance(album, dict) and "error" not in album:
        return album
    return None


def format_date(date_str):
    """Format ISO date string to readable format."""
    if not date_str:
        return "Unknown"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return date_str[:10] if len(date_str) >= 10 else date_str


def format_duration(ms):
    """Format milliseconds to MM:SS."""
    if not ms:
        return "--:--"
    seconds = int(ms / 1000)
    minutes = seconds // 60
    secs = seconds % 60
    return f"{minutes}:{secs:02d}"


def print_album_summary(album, index=None):
    """Print a summary line for an album."""
    artist = album.get("artist", {}).get("artistName", "Unknown Artist")
    title = album.get("title", "Unknown Album")
    album_type = album.get("albumType", "Album")
    release_date = format_date(album.get("releaseDate", ""))
    stats = album.get("statistics", {})
    total_tracks = stats.get("trackCount", 0)
    missing = album.get("missingTrackCount", total_tracks)
    album_id = album.get("id", "?")

    prefix = f"{index:3d}. " if index is not None else ""
    print(f"{prefix}[{album_id}] {artist} - {title}")
    print(f"     Type: {album_type} | Released: {release_date} | Missing: {missing}/{total_tracks} tracks")


def print_album_details(album, config, show_tracks=False):
    """Print detailed information about an album."""
    artist = album.get("artist", {})
    artist_name = artist.get("artistName", "Unknown Artist")
    artist_id = artist.get("id", "?")
    foreign_artist_id = artist.get("foreignArtistId", "")

    title = album.get("title", "Unknown Album")
    album_id = album.get("id", "?")
    foreign_album_id = album.get("foreignAlbumId", "")
    album_type = album.get("albumType", "Album")
    release_date = format_date(album.get("releaseDate", ""))

    stats = album.get("statistics", {})
    total_tracks = stats.get("trackCount", 0)
    track_files = stats.get("trackFileCount", 0)
    missing = album.get("missingTrackCount", total_tracks)
    size_on_disk = stats.get("sizeOnDisk", 0)

    monitored = album.get("monitored", False)
    genres = album.get("genres", [])
    releases = album.get("releases", [])

    print("=" * 70)
    print(f"Album: {title}")
    print(f"Artist: {artist_name}")
    print("-" * 70)
    print(f"  Album ID:        {album_id}")
    print(f"  MusicBrainz ID:  {foreign_album_id or 'N/A'}")
    print(f"  Album Type:      {album_type}")
    print(f"  Release Date:    {release_date}")
    print(f"  Monitored:       {'Yes' if monitored else 'No'}")
    print(f"  Genres:          {', '.join(genres) if genres else 'N/A'}")
    print()
    print(f"  Artist ID:       {artist_id}")
    print(f"  Artist MBID:     {foreign_artist_id or 'N/A'}")
    print()
    print(f"  Total Tracks:    {total_tracks}")
    print(f"  Files on Disk:   {track_files}")
    print(f"  Missing Tracks:  {missing}")
    if size_on_disk > 0:
        print(f"  Size on Disk:    {size_on_disk / (1024*1024):.1f} MB")

    # Show releases info
    if releases:
        print()
        print(f"  Releases ({len(releases)}):")
        for rel in releases[:5]:  # Limit to first 5 releases
            rel_id = rel.get("id", "?")
            foreign_rel_id = rel.get("foreignReleaseId", "")
            monitored_rel = rel.get("monitored", False)
            track_count = rel.get("trackCount", 0)
            country = rel.get("country", "")
            label = rel.get("label", "")
            status = "*" if monitored_rel else " "
            print(f"    {status} [{rel_id}] {foreign_rel_id[:36] if foreign_rel_id else 'N/A'}")
            print(f"        Tracks: {track_count} | Country: {country or 'N/A'} | Label: {label or 'N/A'}")
        if len(releases) > 5:
            print(f"    ... and {len(releases) - 5} more releases")

    # Show images
    images = album.get("images", [])
    if images:
        print()
        print(f"  Images ({len(images)}):")
        for img in images:
            cover_type = img.get("coverType", "unknown")
            remote_url = img.get("remoteUrl", "")
            print(f"    - {cover_type}: {remote_url[:60]}..." if len(remote_url) > 60 else f"    - {cover_type}: {remote_url}")

    # Show tracks if requested
    if show_tracks:
        tracks = get_album_tracks(config, album_id)
        if tracks:
            print()
            print(f"  Tracks ({len(tracks)}):")
            for track in sorted(tracks, key=lambda t: (t.get("mediumNumber", 1), t.get("trackNumber", 0))):
                track_num = int(track.get("trackNumber", 0) or 0)
                medium_num = int(track.get("mediumNumber", 1) or 1)
                track_title = track.get("title", "Unknown")
                duration = format_duration(track.get("duration", 0))
                has_file = track.get("hasFile", False)
                foreign_recording_id = track.get("foreignRecordingId", "")

                status = "[x]" if has_file else "[ ]"
                disc_prefix = f"{medium_num}-" if medium_num > 1 else ""
                mbid_short = foreign_recording_id[:8] if foreign_recording_id else "N/A"

                print(f"    {status} {disc_prefix}{track_num:02d}. {track_title} ({duration}) [MBID: {mbid_short}...]")
        else:
            print()
            print("  Tracks: Could not fetch track list")

    print()


def build_json_output(albums, config, verbose=False):
    """Build JSON output structure."""
    output = {
        "total_albums": len(albums),
        "total_missing_tracks": sum(a.get("missingTrackCount", 0) for a in albums),
        "albums": []
    }

    for album in albums:
        album_data = {
            "id": album.get("id"),
            "title": album.get("title"),
            "artist": album.get("artist", {}).get("artistName"),
            "artist_id": album.get("artist", {}).get("id"),
            "foreign_album_id": album.get("foreignAlbumId"),
            "foreign_artist_id": album.get("artist", {}).get("foreignArtistId"),
            "album_type": album.get("albumType"),
            "release_date": album.get("releaseDate"),
            "monitored": album.get("monitored"),
            "genres": album.get("genres", []),
            "statistics": album.get("statistics", {}),
            "missing_track_count": album.get("missingTrackCount", 0),
            "releases": album.get("releases", []),
            "images": album.get("images", []),
        }

        if verbose:
            tracks = get_album_tracks(config, album.get("id"))
            album_data["tracks"] = tracks

        output["albums"].append(album_data)

    return output


def main():
    args = parse_args()
    config = get_config(args)

    if not args.json_output:
        print(f"Connecting to Lidarr at {config['lidarr_url']}...")

    # Test connection
    status = lidarr_request(config, "system/status")
    if "error" in status:
        print(f"Error connecting to Lidarr: {status['error']}", file=sys.stderr)
        sys.exit(1)

    if not args.json_output:
        print(f"Connected to Lidarr v{status.get('version', 'unknown')}")
        print()

    # Fetch missing albums
    albums = get_missing_albums(config)
    if isinstance(albums, dict) and "error" in albums:
        print(f"Error fetching missing albums: {albums['error']}", file=sys.stderr)
        sys.exit(1)

    if not albums:
        if args.json_output:
            print(json.dumps({"total_albums": 0, "albums": []}, indent=2))
        else:
            print("No missing albums found.")
        return

    # Apply filters
    if args.artist:
        filter_text = args.artist.lower()
        albums = [
            a for a in albums
            if filter_text in a.get("artist", {}).get("artistName", "").lower()
        ]

    if args.album:
        filter_text = args.album.lower()
        albums = [
            a for a in albums
            if filter_text in a.get("title", "").lower()
        ]

    # Apply sorting
    if args.sort == "artist":
        albums.sort(key=lambda a: a.get("artist", {}).get("artistName", "").lower())
    elif args.sort == "album":
        albums.sort(key=lambda a: a.get("title", "").lower())
    elif args.sort == "missing":
        albums.sort(key=lambda a: a.get("missingTrackCount", 0), reverse=True)
    # "date" is default from API

    # Apply limit
    if args.limit > 0:
        albums = albums[:args.limit]

    # Output
    if args.json_output:
        output = build_json_output(albums, config, verbose=args.verbose)
        print(json.dumps(output, indent=2, default=str))
    else:
        total_missing = sum(a.get("missingTrackCount", 0) for a in albums)
        print(f"Found {len(albums)} missing albums ({total_missing} total missing tracks)")
        print()

        if args.verbose:
            for album in albums:
                # Fetch full album details for releases info
                full_album = get_album_details(config, album.get("id"))
                if full_album:
                    album.update(full_album)
                print_album_details(album, config, show_tracks=True)
        else:
            for i, album in enumerate(albums, 1):
                print_album_summary(album, index=i)
                print()


if __name__ == "__main__":
    main()
