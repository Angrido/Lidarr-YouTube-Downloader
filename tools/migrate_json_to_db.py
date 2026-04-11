#!/usr/bin/env python3
"""Migrate JSON state files to SQLite database.

Standalone script with no Flask dependency. Reads download_history.json,
download_logs.json, and last_failed_result.json from a config directory,
imports them into the SQLite database, and renames originals to *.json.migrated.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import db


def load_json(path):
    """Load and return parsed JSON from path, or None if missing/corrupt."""
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Failed to load {path}: {e}")
        return None


def migrate_history(conn, records):
    """Insert download_history records into the database."""
    count = 0
    for rec in records:
        conn.execute(
            "INSERT INTO download_history"
            " (album_id, album_title, artist_name, success, partial,"
            "  manual, track_title, timestamp)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.get("album_id", 0),
                rec.get("album_title", ""),
                rec.get("artist_name", ""),
                int(rec.get("success", True)),
                int(rec.get("partial", False)),
                int(rec.get("manual", False)),
                rec.get("track_title"),
                rec.get("timestamp", 0),
            ),
        )
        count += 1
    conn.commit()
    return count


def migrate_logs(conn, records):
    """Insert download_logs records into the database."""
    count = 0
    for rec in records:
        failed_tracks = rec.get("failed_tracks", [])
        if isinstance(failed_tracks, list):
            failed_tracks = json.dumps(failed_tracks)
        conn.execute(
            "INSERT OR IGNORE INTO download_logs"
            " (id, type, album_id, album_title, artist_name,"
            "  timestamp, details, failed_tracks, total_file_size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rec.get("id", ""),
                rec.get("type", ""),
                rec.get("album_id", 0),
                rec.get("album_title", ""),
                rec.get("artist_name", ""),
                rec.get("timestamp", 0),
                rec.get("details", ""),
                failed_tracks,
                rec.get("total_file_size", 0),
            ),
        )
        count += 1
    conn.commit()
    return count


def migrate_failed(conn, data):
    """Insert failed tracks from last_failed_result into the database."""
    tracks = data.get("failed_tracks", [])
    if not tracks:
        return 0
    album_id = data.get("album_id")
    album_title = data.get("album_title", "")
    artist_name = data.get("artist_name", "")
    cover_url = data.get("cover_url", "")
    album_path = data.get("album_path", "")
    lidarr_album_path = data.get("lidarr_album_path", "")

    count = 0
    for track in tracks:
        conn.execute(
            "INSERT INTO failed_tracks"
            " (album_id, album_title, artist_name, cover_url,"
            "  album_path, lidarr_album_path, track_title,"
            "  track_num, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                album_id,
                album_title,
                artist_name,
                cover_url,
                album_path,
                lidarr_album_path,
                track.get("title", ""),
                track.get("track_num", 0),
                track.get("reason", ""),
            ),
        )
        count += 1
    conn.commit()
    return count


def rename_migrated(path):
    """Rename a JSON file to *.json.migrated."""
    dest = path + ".migrated"
    os.rename(path, dest)


def main():
    parser = argparse.ArgumentParser(
        description="Migrate JSON state files to SQLite database"
    )
    parser.add_argument(
        "--config-dir",
        default="/config",
        help="Config directory containing JSON files (default: /config)",
    )
    args = parser.parse_args()
    config_dir = args.config_dir

    history_path = os.path.join(config_dir, "download_history.json")
    logs_path = os.path.join(config_dir, "download_logs.json")
    failed_path = os.path.join(config_dir, "last_failed_result.json")

    files_found = [
        p
        for p in (history_path, logs_path, failed_path)
        if os.path.exists(p)
    ]
    if not files_found:
        print("No JSON state files found in", config_dir)
        return

    db.DB_PATH = os.path.join(config_dir, "lidarr-downloader.db")
    db.init_db()
    conn = db.get_db()

    migrations = [
        (history_path, "download_history", migrate_history),
        (logs_path, "download_logs", migrate_logs),
        (failed_path, "failed_tracks", migrate_failed),
    ]

    for path, label, migrate_fn in migrations:
        data = load_json(path)
        if data is None:
            continue
        try:
            count = migrate_fn(conn, data)
            print(f"Migrated {count} {label} records")
            rename_migrated(path)
        except Exception as e:
            print(f"ERROR: Failed to migrate {label} from {path}: {e}")
            print(f"  File NOT renamed — fix the issue and retry.")

    db.close_db()
    print("Migration complete.")


if __name__ == "__main__":
    main()
