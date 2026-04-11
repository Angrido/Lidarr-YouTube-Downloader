"""Metadata functions for ID3 tagging, XML sidecar files, and iTunes API.

Handles MP3 tagging with MusicBrainz IDs, XML metadata generation for
Lidarr import, and iTunes API lookups for track lists and album artwork.
"""

import logging
import os
from xml.sax.saxutils import escape as xml_escape

import requests
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TDRC,
    TIT2,
    TPE1,
    TPE2,
    TRCK,
    TXXX,
    UFID,
)
from mutagen.mp3 import MP3

from lidarr import get_monitored_release
from utils import sanitize_filename

logger = logging.getLogger(__name__)


def tag_mp3(file_path, track_info, album_info, cover_data):
    """Apply ID3 tags to an MP3 file including MusicBrainz metadata.

    Args:
        file_path: Path to the MP3 file.
        track_info: Dict with title, trackNumber, foreignRecordingId.
        album_info: Dict with title, artist, releaseDate, trackCount,
            foreignAlbumId, and releases list.
        cover_data: Raw bytes of cover art image, or None.

    Returns:
        True on success, False on failure.
    """
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except Exception as e:
            logger.debug("MP3 load with ID3 failed for %s: %s, retrying", file_path, e)
            audio = MP3(file_path)
            audio.add_tags()
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info["title"]))
        audio.tags.add(
            TPE1(encoding=3, text=album_info["artist"]["artistName"])
        )
        audio.tags.add(
            TPE2(encoding=3, text=album_info["artist"]["artistName"])
        )
        audio.tags.add(TALB(encoding=3, text=album_info["title"]))
        audio.tags.add(
            TDRC(
                encoding=3,
                text=str(album_info.get("releaseDate", "")[:4]),
            )
        )

        try:
            t_num = int(track_info["trackNumber"])
            audio.tags.add(
                TRCK(
                    encoding=3,
                    text=f"{t_num}/{album_info.get('trackCount', 0)}",
                )
            )
        except (ValueError, KeyError):
            pass

        release = get_monitored_release(album_info)
        if release:
            _add_musicbrainz_tags(audio, track_info, album_info, release)

        if track_info.get("foreignRecordingId"):
            audio.tags.add(
                UFID(
                    owner="http://musicbrainz.org",
                    data=track_info["foreignRecordingId"].encode(),
                )
            )
        if cover_data:
            audio.tags.add(
                APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=cover_data,
                )
            )

        audio.save(v2_version=3)
        return True
    except Exception as e:
        logger.warning(f"Failed to tag MP3 {file_path}: {e}")
        return False


def _add_musicbrainz_tags(audio, track_info, album_info, release):
    """Add MusicBrainz-specific TXXX frames to the audio tags."""
    mb_fields = [
        (
            track_info.get("foreignRecordingId"),
            "MusicBrainz Release Track Id",
        ),
        (
            release.get("foreignReleaseId"),
            "MusicBrainz Album Id",
        ),
        (
            album_info["artist"].get("foreignArtistId"),
            "MusicBrainz Artist Id",
        ),
        (
            album_info.get("foreignAlbumId"),
            "MusicBrainz Album Release Group Id",
        ),
        (
            release.get("country"),
            "MusicBrainz Release Country",
        ),
    ]
    for value, desc in mb_fields:
        if value:
            audio.tags.add(TXXX(encoding=3, desc=desc, text=value))


def create_xml_metadata(
    output_dir, artist, album, track_num, title,
    album_id=None, artist_id=None,
):
    """Create an XML sidecar file with track metadata for Lidarr import.

    Args:
        output_dir: Directory to write the XML file.
        artist: Artist name.
        album: Album name.
        track_num: Track number (int).
        title: Track title.
        album_id: Optional MusicBrainz album ID.
        artist_id: Optional MusicBrainz artist ID.

    Returns:
        True on success, False on failure.
    """
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        safe_title = xml_escape(title)
        safe_artist = xml_escape(artist)
        safe_album = xml_escape(album)
        mb_album = (
            f"  <musicbrainzalbumid>"
            f"{xml_escape(str(album_id))}"
            f"</musicbrainzalbumid>\n"
            if album_id
            else ""
        )
        mb_artist = (
            f"  <musicbrainzartistid>"
            f"{xml_escape(str(artist_id))}"
            f"</musicbrainzartistid>\n"
            if artist_id
            else ""
        )
        content = (
            f"<song>\n"
            f"  <title>{safe_title}</title>\n"
            f"  <artist>{safe_artist}</artist>\n"
            f"  <performingartist>{safe_artist}</performingartist>\n"
            f"  <albumartist>{safe_artist}</albumartist>\n"
            f"  <album>{safe_album}</album>\n"
            f"{mb_album}{mb_artist}</song>"
        )
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(content)
        return True
    except Exception as e:
        logger.warning(f"Failed to create XML metadata: {e}")
        return False


def get_itunes_tracks(artist, album_name):
    """Look up album tracks from the iTunes Search API.

    Args:
        artist: Artist name to search for.
        album_name: Album name to search for.

    Returns:
        List of track dicts with trackNumber, title, previewUrl, hasFile.
        Returns an empty list on error or no results.
    """
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist} {album_name}",
            "entity": "album",
            "limit": 1,
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            collection_id = data["results"][0]["collectionId"]
            lookup_url = "https://itunes.apple.com/lookup"
            lookup_params = {"id": collection_id, "entity": "song"}
            lookup_r = requests.get(
                lookup_url, params=lookup_params, timeout=10
            )
            lookup_data = lookup_r.json()
            tracks = []
            for item in lookup_data.get("results", [])[1:]:
                tracks.append(
                    {
                        "trackNumber": item.get("trackNumber"),
                        "title": item.get("trackName"),
                        "previewUrl": item.get("previewUrl"),
                        "hasFile": False,
                    }
                )
            return tracks
    except Exception as e:
        logger.debug(f"iTunes tracks lookup failed: {e}")
    return []


def get_itunes_artwork(artist, album):
    """Fetch high-resolution album artwork from the iTunes Search API.

    Args:
        artist: Artist name to search for.
        album: Album name to search for.

    Returns:
        Raw bytes of the artwork image, or None if not found.
    """
    try:
        url = "https://itunes.apple.com/search"
        params = {
            "term": f"{artist} {album}",
            "entity": "album",
            "limit": 1,
        }
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get("resultCount", 0) > 0:
            artwork_url = (
                data["results"][0]
                .get("artworkUrl100", "")
                .replace("100x100", "3000x3000")
            )
            return requests.get(artwork_url, timeout=15).content
    except Exception as e:
        logger.debug(f"iTunes artwork lookup failed: {e}")
    return None
