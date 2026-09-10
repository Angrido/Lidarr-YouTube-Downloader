"""Metadata functions for ID3 tagging, XML sidecar files, and iTunes API.

Handles MP3 tagging with MusicBrainz IDs, XML metadata generation for
Lidarr import, and iTunes API lookups for track lists and album artwork.
"""

import base64
import json
import logging
import os
import subprocess
import threading
import time
from xml.sax.saxutils import escape as xml_escape

import requests
from mutagen.flac import Picture
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
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm
from mutagen.oggopus import OggOpus

from lidarr import get_monitored_release
from utils import sanitize_filename
from version import USER_AGENT

logger = logging.getLogger(__name__)

# Identifying User-Agent for the MusicBrainz family of APIs (musicbrainz.org,
# coverartarchive.org), which reject or throttle the default requests one.
_API_HEADERS = {"User-Agent": USER_AGENT}


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
                text=str(album_info.get("releaseDate") or "")[:4],
            )
        )

        try:
            t_num = int(track_info["trackNumber"])
            audio.tags.add(
                TRCK(
                    encoding=3,
                    text=f"{t_num}/{album_info.get('trackCount') or 0}",
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


def tag_opus(file_path, track_info, album_info, cover_data):
    try:
        audio = OggOpus(file_path)

        audio["title"] = [track_info["title"]]
        audio["artist"] = [album_info["artist"]["artistName"]]
        audio["albumartist"] = [album_info["artist"]["artistName"]]
        audio["album"] = [album_info["title"]]
        audio["date"] = [str(album_info.get("releaseDate") or "")[:4]]

        try:
            t_num = int(track_info["trackNumber"])
            total = album_info.get("trackCount") or 0
            audio["tracknumber"] = [f"{t_num}/{total}"]
        except (ValueError, KeyError):
            pass

        release = get_monitored_release(album_info)
        if release:
            if track_info.get("foreignRecordingId"):
                audio["musicbrainz_trackid"] = [track_info["foreignRecordingId"]]
            if release.get("foreignReleaseId"):
                audio["musicbrainz_albumid"] = [release["foreignReleaseId"]]
            if album_info["artist"].get("foreignArtistId"):
                audio["musicbrainz_artistid"] = [album_info["artist"]["foreignArtistId"]]
            if album_info.get("foreignAlbumId"):
                audio["musicbrainz_releasegroupid"] = [album_info["foreignAlbumId"]]
            country = release.get("country")
            if isinstance(country, list):
                country = country[0] if country else None
            if country:
                audio["releasecountry"] = [str(country)]

        if track_info.get("foreignRecordingId"):
            audio["musicbrainz_trackid"] = [track_info["foreignRecordingId"]]

        if cover_data:
            pic = Picture()
            pic.type = 3
            pic.mime = "image/jpeg"
            pic.data = cover_data
            audio["metadata_block_picture"] = [
                base64.b64encode(pic.write()).decode("ascii")
            ]

        audio.save()
        return True
    except Exception as e:
        logger.warning(f"Failed to tag Opus {file_path}: {e}")
        return False


def tag_m4a(file_path, track_info, album_info, cover_data):
    try:
        audio = MP4(file_path)

        audio["\xa9nam"] = [track_info["title"]]
        audio["\xa9ART"] = [album_info["artist"]["artistName"]]
        audio["aART"] = [album_info["artist"]["artistName"]]
        audio["\xa9alb"] = [album_info["title"]]
        audio["\xa9day"] = [str(album_info.get("releaseDate") or "")[:4]]

        try:
            t_num = int(track_info["trackNumber"])
            total = album_info.get("trackCount") or 0
            audio["trkn"] = [(t_num, total)]
        except (ValueError, KeyError):
            pass

        release = get_monitored_release(album_info)
        if release:
            country = release.get("country")
            if isinstance(country, list):
                country = country[0] if country else None
            mb_fields = [
                (track_info.get("foreignRecordingId"),
                 "MusicBrainz Release Track Id"),
                (release.get("foreignReleaseId"), "MusicBrainz Album Id"),
                (album_info["artist"].get("foreignArtistId"),
                 "MusicBrainz Artist Id"),
                (album_info.get("foreignAlbumId"),
                 "MusicBrainz Album Release Group Id"),
                (country, "MusicBrainz Release Country"),
            ]
            for value, desc in mb_fields:
                if value:
                    key = f"----:com.apple.iTunes:{desc}"
                    audio[key] = [MP4FreeForm(str(value).encode())]

        if track_info.get("foreignRecordingId"):
            key = "----:com.apple.iTunes:MusicBrainz Release Track Id"
            audio[key] = [
                MP4FreeForm(track_info["foreignRecordingId"].encode())
            ]

        if cover_data:
            audio["covr"] = [
                MP4Cover(cover_data, imageformat=MP4Cover.FORMAT_JPEG)
            ]

        audio.save()
        return True
    except Exception as e:
        logger.warning(f"Failed to tag M4A {file_path}: {e}")
        return False


def tag_audio_file(file_path, track_info, album_info, cover_data):
    ext = os.path.splitext(file_path)[1].lower()
    if ext == ".opus":
        return tag_opus(file_path, track_info, album_info, cover_data)
    if ext == ".m4a":
        return tag_m4a(file_path, track_info, album_info, cover_data)
    return tag_mp3(file_path, track_info, album_info, cover_data)


_LRCLIB_URL = "https://lrclib.net/api/get"


def write_lyrics_sidecar(audio_file, artist, title, album="", duration=0):
    """Fetch synced lyrics from LRCLIB and write a ``.lrc`` sidecar.

    LRCLIB (https://lrclib.net) is a free, no-auth community lyrics
    database. The .lrc file is written next to the audio file (same base
    name) so Jellyfin/Navidrome/etc. pick it up. Prefers time-synced
    lyrics, falling back to plain. Returns the .lrc path, or None.
    """
    if not artist or not title:
        return None
    params = {"artist_name": artist, "track_name": title}
    if album:
        params["album_name"] = album
    if duration:
        try:
            params["duration"] = int(duration)
        except (TypeError, ValueError):
            pass
    try:
        r = requests.get(
            _LRCLIB_URL, params=params, headers=_API_HEADERS, timeout=10
        )
        if r.status_code != 200:
            return None
        data = r.json() or {}
        lyrics = data.get("syncedLyrics") or data.get("plainLyrics")
        if not lyrics or not lyrics.strip():
            return None
        lrc_path = os.path.splitext(audio_file)[0] + ".lrc"
        with open(lrc_path, "w", encoding="utf-8") as f:
            f.write(lyrics)
        logger.info("Lyrics saved: %s", os.path.basename(lrc_path))
        return lrc_path
    except Exception as e:
        logger.debug("Lyrics fetch failed for %s - %s: %s", artist, title, e)
        return None


# ReplayGain 2.0 reference level (EBU R128 −18 LUFS).
_REPLAYGAIN_REFERENCE_LUFS = -18.0


def _measure_loudness(audio_file):
    """Return ``(integrated_lufs, true_peak_dbtp)`` via ffmpeg, or None.

    Runs a single ffmpeg ``loudnorm`` analysis pass, which prints an
    ``input_i`` (integrated loudness) and ``input_tp`` (true peak) JSON
    block to stderr — cheaper and more portable than a separate scanner.
    """
    try:
        proc = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-nostats", "-i", audio_file,
                "-af", "loudnorm=print_format=json", "-f", "null", "-",
            ],
            capture_output=True, text=True, timeout=180,
        )
    except Exception as e:
        logger.debug("ffmpeg loudness measurement failed: %s", e)
        return None
    out = proc.stderr or ""
    start = out.rfind("{")
    end = out.rfind("}")
    if start == -1 or end == -1 or end < start:
        return None
    try:
        data = json.loads(out[start:end + 1])
        return float(data["input_i"]), float(data["input_tp"])
    except (ValueError, KeyError, TypeError):
        return None


def apply_replaygain_tags(audio_file):
    """Measure loudness and write ReplayGain *track* tags (non-destructive).

    Writes ``REPLAYGAIN_TRACK_GAIN`` / ``REPLAYGAIN_TRACK_PEAK`` so players
    can normalize volume without re-encoding the audio. Supports MP3 (TXXX),
    M4A (iTunes freeform) and Opus (Vorbis comments). Returns the gain
    string (e.g. ``"-2.34 dB"``) on success, or None.
    """
    measured = _measure_loudness(audio_file)
    if measured is None:
        return None
    integrated, true_peak = measured
    if integrated in (float("-inf"), float("inf")):
        return None
    gain_db = _REPLAYGAIN_REFERENCE_LUFS - integrated
    peak_linear = 10 ** (true_peak / 20.0)
    gain_str = f"{gain_db:.2f} dB"
    peak_str = f"{peak_linear:.6f}"
    ext = os.path.splitext(audio_file)[1].lower()
    try:
        if ext == ".m4a":
            audio = MP4(audio_file)
            audio["----:com.apple.iTunes:replaygain_track_gain"] = [
                MP4FreeForm(gain_str.encode())
            ]
            audio["----:com.apple.iTunes:replaygain_track_peak"] = [
                MP4FreeForm(peak_str.encode())
            ]
            audio.save()
        elif ext == ".opus":
            audio = OggOpus(audio_file)
            audio["REPLAYGAIN_TRACK_GAIN"] = gain_str
            audio["REPLAYGAIN_TRACK_PEAK"] = peak_str
            audio.save()
        else:
            audio = MP3(audio_file, ID3=ID3)
            if audio.tags is None:
                audio.add_tags()
            audio.tags.add(
                TXXX(encoding=3, desc="REPLAYGAIN_TRACK_GAIN", text=gain_str)
            )
            audio.tags.add(
                TXXX(encoding=3, desc="REPLAYGAIN_TRACK_PEAK", text=peak_str)
            )
            audio.save()
        logger.info(
            "ReplayGain written: %s (%s)",
            os.path.basename(audio_file), gain_str,
        )
        return gain_str
    except Exception as e:
        logger.warning(
            "Failed to write ReplayGain tags to %s: %s", audio_file, e
        )
        return None


def _add_musicbrainz_tags(audio, track_info, album_info, release):
    """Add MusicBrainz-specific TXXX frames to the audio tags."""
    country = release.get("country")
    if isinstance(country, list):
        country = country[0] if country else None
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
            country,
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
                        "artist": item.get("artistName"),
                        "previewUrl": item.get("previewUrl"),
                        "hasFile": False,
                    }
                )
            return tracks
    except Exception as e:
        logger.debug(f"iTunes tracks lookup failed: {e}")
    return []


def _hires_artwork_url(artwork_url):
    if not artwork_url:
        return ""
    return (
        artwork_url
        .replace("100x100", "3000x3000")
        .replace("600x600", "3000x3000")
    )


def get_artwork_from_url(artwork_url):
    """Fetch artwork bytes from a known artwork URL."""
    try:
        artwork_url = _hires_artwork_url(artwork_url)
        if not artwork_url.startswith(("http://", "https://")):
            return None
        data = requests.get(artwork_url, timeout=15).content
        return data or None
    except Exception as e:
        logger.debug(f"Artwork URL fetch failed: {e}")
    return None


def get_itunes_artwork(artist, album):
    """Fetch high-resolution album artwork from the iTunes Search API.

    The iTunes Search API serves Apple Music's catalogue: ``artworkUrl100``
    points to the same artwork that Apple Music displays, just resizable
    to 3000x3000.

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
            artwork_url = data["results"][0].get("artworkUrl100", "")
            return get_artwork_from_url(artwork_url)
    except Exception as e:
        logger.debug(f"iTunes artwork lookup failed: {e}")
    return None


def get_deezer_artwork(artist, album):
    """Fetch album artwork from the Deezer public search API.

    Deezer exposes ``cover_xl`` (1000x1000) without authentication and
    covers a slightly different long-tail catalogue than iTunes/Apple
    Music (notably better for European indie / non-US releases).
    """
    try:
        url = "https://api.deezer.com/search/album"
        params = {"q": f'artist:"{artist}" album:"{album}"', "limit": 5}
        r = requests.get(url, params=params, timeout=10)
        data = r.json() or {}
        results = data.get("data", []) or []
        if not results:
            params = {"q": f"{artist} {album}", "limit": 5}
            r = requests.get(url, params=params, timeout=10)
            data = r.json() or {}
            results = data.get("data", []) or []
        artist_lower = (artist or "").lower()
        for entry in results:
            entry_artist = (
                (entry.get("artist", {}) or {}).get("name", "")
            )
            if artist_lower and artist_lower not in entry_artist.lower():
                continue
            cover_url = (
                entry.get("cover_xl")
                or entry.get("cover_big")
                or entry.get("cover_medium")
                or ""
            )
            if cover_url:
                data_bytes = requests.get(cover_url, timeout=15).content
                if data_bytes:
                    return data_bytes
    except Exception as e:
        logger.debug(f"Deezer artwork lookup failed: {e}")
    return None


_mb_rate_lock = threading.Lock()
_mb_last_request_time = 0.0
_MB_MIN_INTERVAL = 1.0


def _musicbrainz_throttle():
    """Space out MusicBrainz requests to at most 1 per second.

    MusicBrainz throttles by source IP address: once a client's request
    rate is measured too high, *all* of its requests get declined with
    HTTP 503 until the rate drops again, and that measured rate is
    currently ~1 request/second on average.
    source: https://musicbrainz.org/doc/MusicBrainz_API/Rate_Limiting
    """
    global _mb_last_request_time
    with _mb_rate_lock:
        wait = _mb_last_request_time + _MB_MIN_INTERVAL - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _mb_last_request_time = time.monotonic()


_MB_RETRY_ATTEMPTS = 5
_MB_RETRY_BACKOFF = 2.0
_MB_RETRY_STATUS = (429, 500, 502, 503, 504)


def _musicbrainz_get(url, params, label="lookup"):
    """GET a MusicBrainz endpoint, throttled and retried on 503/timeouts.

    MusicBrainz answers with HTTP 503 ("currently busy") whenever the
    server is loaded or our source IP has been rate-limited, so a single
    attempt fails spuriously. Retries up to ``_MB_RETRY_ATTEMPTS`` times
    with exponential backoff (honouring ``Retry-After`` when present) and
    the usual 1 req/s throttle between attempts.

    Returns the successful ``Response``, or None if every attempt failed.
    """
    for attempt in range(1, _MB_RETRY_ATTEMPTS + 1):
        try:
            _musicbrainz_throttle()
            r = requests.get(
                url, params=params, headers=_API_HEADERS, timeout=10
            )
            if r.status_code == 200:
                return r
            if r.status_code in _MB_RETRY_STATUS \
                    and attempt < _MB_RETRY_ATTEMPTS:
                delay = _MB_RETRY_BACKOFF ** (attempt - 1)
                try:
                    delay = max(delay, float(r.headers.get("Retry-After", 0)))
                except (TypeError, ValueError):
                    pass
                logger.info(
                    "      MB %s: HTTP %s, retrying in %.1fs (attempt %d/%d)",
                    label, r.status_code, delay, attempt, _MB_RETRY_ATTEMPTS,
                )
                time.sleep(delay)
                continue
            logger.info(
                "      MB %s failed: HTTP %s after %d attempt(s), body: %.300s",
                label, r.status_code, attempt, r.text,
            )
            return None
        except Exception as e:
            if attempt < _MB_RETRY_ATTEMPTS:
                delay = _MB_RETRY_BACKOFF ** (attempt - 1)
                logger.info(
                    "      MB %s: %s: %s, retrying in %.1fs"
                    " (attempt %d/%d)",
                    label, type(e).__name__, e, delay,
                    attempt, _MB_RETRY_ATTEMPTS,
                )
                time.sleep(delay)
                continue
            logger.info(
                "      MB %s failed after %d attempt(s): %s: %s",
                label, attempt, type(e).__name__, e,
            )
    return None


def _musicbrainz_release_id(artist, album):
    """Resolve a MusicBrainz release id (uuid) for artist+album, or None."""
    try:
        url = "https://musicbrainz.org/ws/2/release/"
        params = {
            "query": f'artist:"{artist}" AND release:"{album}"',
            "fmt": "json",
            "limit": 5,
        }
        r = _musicbrainz_get(url, params, label="release lookup")
        if r is None:
            return None
        data = r.json() or {}
        releases = data.get("releases", []) or []
        artist_lower = (artist or "").lower()
        for rel in releases:
            rel_artists = rel.get("artist-credit", []) or []
            rel_artist_name = " ".join(
                (
                    a.get("name", "")
                    if isinstance(a, dict)
                    else (
                        (a.get("artist", {}) or {}).get("name", "")
                        if isinstance(a, dict) else ""
                    )
                )
                for a in rel_artists
            ).lower()
            if artist_lower and artist_lower not in rel_artist_name:
                continue
            rid = rel.get("id") or ""
            if rid:
                return rid
        if releases:
            return releases[0].get("id") or None
    except Exception as e:
        logger.debug(f"MusicBrainz lookup failed: {e}")
    return None


def get_musicbrainz_recording_artist(recording_id):
    """Resolve a MusicBrainz recording's artist-credit name.

    Used to find the real per-track artist for compilation albums, where
    Lidarr's album-level artist is "Various Artists" but each track's
    MusicBrainz recording (``foreignRecordingId``) has its own credited
    artist(s).

    Returns the artist-credit phrase (e.g. "Artist A feat. Artist B"), or
    None if unresolvable.
    """
    if not recording_id:
        return None
    try:
        url = f"https://musicbrainz.org/ws/2/recording/{recording_id}"
        params = {"fmt": "json", "inc": "artist-credits"}
        r = _musicbrainz_get(url, params, label="artist lookup")
        if r is None:
            return None
        data = r.json() or {}
        credits = data.get("artist-credit", []) or []
        name = "".join(
            (c.get("name", "") if isinstance(c, dict) else "")
            + (c.get("joinphrase", "") if isinstance(c, dict) else "")
            for c in credits
        ).strip()
        return name or None
    except Exception as e:
        logger.debug(f"MusicBrainz recording artist lookup failed: {e}")
    return None


def get_cover_art_archive_artwork(artist, album):
    """Fetch album artwork from MusicBrainz Cover Art Archive (CAA).

    The Cover Art Archive is a free, no-auth project run by MusicBrainz
    in partnership with the Internet Archive. We first resolve a release
    id via the MusicBrainz API and then fetch ``/release/<id>/front``
    from coverartarchive.org.
    """
    release_id = _musicbrainz_release_id(artist, album)
    if not release_id:
        return None
    try:
        cover_url = (
            f"https://coverartarchive.org/release/{release_id}/front"
        )
        r = requests.get(
            cover_url, headers=_API_HEADERS, timeout=15, allow_redirects=True
        )
        if r.status_code == 200 and r.content:
            return r.content
    except Exception as e:
        logger.debug(f"Cover Art Archive fetch failed: {e}")
    return None
