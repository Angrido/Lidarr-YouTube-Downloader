"""Never fetch the same recording twice.

A Lidarr release can carry one recording at several positions (box releases
that repeat the album on every medium), and the same recording is in the
single, the album and the compilations of an artist. The downloader used to
treat every Lidarr track as new, so each position and each album cost a
search, a download and a fingerprint of audio it already had.

Identity is the MusicBrainz recording id Lidarr gives each track
(``foreignRecordingId``). ``plan_album()`` decides, per track, one of:

* **repeat** — another position of this release has the same recording:
  ``skip`` it, or ``copy`` the first one's file once that exists;
* **satisfied** — this album already holds a verified file for it (kept in
  the history but gone from Lidarr's view, e.g. after the download folder
  was cleaned up): nothing to do;
* **reuse** — a verified file of the recording exists in another album:
  copy it and tag it for this one;
* otherwise the normal search-and-download.

The module only decides and finds files; ``processing`` does the copying so
it can reuse the code that tags and records a downloaded track.

``dedupe_mode`` is ``off``, ``log`` (decide and log, change nothing — the
default) or ``reuse`` (act). A track is reused only when the audio of the
stored file was identified by AcoustID as that same recording, and only from
the library (never from the download folder when a library path is set: the
orphan sweeper deletes those folders). Recordings that MusicBrainz shares
between unrelated tracks (``[silence]``, ``[data track]``) are never merged.
"""

import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field

import models
from utils import sanitize_filename

logger = logging.getLogger(__name__)

MODE_OFF = "off"
MODE_LOG = "log"
MODE_REUSE = "reuse"

SAME_ALBUM_SKIP = "skip"
SAME_ALBUM_COPY = "copy"

# MusicBrainz special-purpose recordings: one id, thousands of unrelated
# tracks. Matching them by id would merge different songs.
_SPECIAL_TITLES = frozenset({"[silence]", "[data track]", "[untitled]"})

_BRACKETS_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_EDITION_WORDS_RE = re.compile(
    r"\b(deluxe|edition|expanded|remaster(?:ed)?|version|anniversary|bonus|"
    r"tracks?|explicit|clean|special|collectors?|super|standard|"
    r"international|japan(?:ese)?|digital|reissue|extended)\b"
)
_NAME_NUMBER_RE = re.compile(r"^(?:.*?\s-\s)?\d{1,3}\s-\s(.+)$")
_DURATION_SLACK_SECONDS = 8
_DURATION_SLACK_RATIO = 0.05
_LOG_LINES_PER_ALBUM = 20  # a 280-track box release must not flood the log


def mode_of(cfg):
    mode = (cfg or {}).get("dedupe_mode", MODE_LOG)
    return mode if mode in (MODE_OFF, MODE_LOG, MODE_REUSE) else MODE_LOG


def recording_key(track):
    """The recording id to match ``track`` by, or ``""`` if it has none."""
    rid = str(track.get("foreignRecordingId") or "").strip().lower()
    title = str(track.get("title") or "").strip().lower()
    if not rid or title in _SPECIAL_TITLES:
        return ""
    return rid


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def track_order(track):
    """Release order: medium, then position (vinyl "A1" sorts by the
    album-wide number Lidarr also gives it)."""
    return (
        _int(track.get("mediumNumber"), 1),
        _int(track.get("absoluteTrackNumber"), 0)
        or _int(track.get("trackNumber"), 0),
        _int(track.get("trackNumber"), 0),
    )


def norm_name(text):
    """Accent-, case- and punctuation-insensitive form for file names."""
    text = unicodedata.normalize("NFKD", text or "")
    text = text.encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "", text)


def norm_album(title):
    """An album title without edition decoration: "Album (Deluxe)" and
    "Album - Expanded Edition" both give "album"."""
    text = unicodedata.normalize("NFKD", title or "")
    text = text.encode("ascii", "ignore").decode().lower()
    text = _BRACKETS_RE.sub(" ", text)
    text = _EDITION_WORDS_RE.sub(" ", text)
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def is_edition(src, artist_name, album_title):
    """Is the stored row ``src`` from another edition of this same album
    (same artist, same title once the edition decoration is dropped)?"""
    wanted = norm_album(album_title)
    return bool(
        wanted
        and norm_album(src.get("album_title")) == wanted
        and norm_name(src.get("artist_name")) == norm_name(artist_name)
    )


def _inside(path, roots):
    path = os.path.normcase(os.path.abspath(path))
    for root in roots:
        if not root:
            continue
        root = os.path.normcase(os.path.abspath(root)).rstrip("\\/")
        if path == root or path.startswith(root + os.sep):
            return True
    return False


def _tag_recording_id(path):
    """The MusicBrainz recording id written in the file's tags, or ``""``."""
    try:
        import mutagen
        audio = mutagen.File(path)
    except Exception:
        return ""
    tags = getattr(audio, "tags", None)
    if not tags:
        return ""
    try:
        ufid = tags.get("UFID:http://musicbrainz.org")
        if ufid is not None:
            return ufid.data.decode("ascii", "ignore").strip().lower()
        for key in ("----:com.apple.iTunes:MusicBrainz Track Id",):
            if key in tags:
                value = tags[key][0]
                return bytes(value).decode("ascii", "ignore").strip().lower()
        for key in ("musicbrainz_trackid", "MUSICBRAINZ_TRACKID"):
            if key in tags:
                return str(tags[key][0]).strip().lower()
    except Exception:
        return ""
    return ""


def strip_tags(path):
    """Remove every tag from a copied file, so nothing of the album it was
    copied from is left behind when it is tagged for the new one."""
    try:
        import mutagen
        audio = mutagen.File(path)
        if audio is not None and audio.tags is not None:
            audio.delete()
    except Exception:
        logger.debug("Could not strip tags of %s", path, exc_info=True)


def _file_seconds(path):
    try:
        import mutagen
        audio = mutagen.File(path)
        return float(audio.info.length)
    except Exception:
        return 0.0


def file_matches(path, key, expected_ms=0):
    """Is the file at ``path`` really the recording ``key``?

    A name can lie (two "01 - Intro" in one folder, a file replaced since),
    so the recording id in the tags decides; a file without one is accepted
    only when its length agrees with the expected duration.
    """
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 0:
            return False
    except OSError:
        return False
    tagged = _tag_recording_id(path)
    if tagged:
        return tagged == key
    expected = (_int(expected_ms) or 0) / 1000.0
    if expected <= 0:
        return False
    slack = max(_DURATION_SLACK_SECONDS, expected * _DURATION_SLACK_RATIO)
    actual = _file_seconds(path)
    return actual > 0 and abs(actual - expected) <= slack


def _candidate_files(src, library_root, download_dir, audio_ext):
    """Files that may be the stored copy of ``src``, best guess first.

    With a library path set only the library copy counts: the download
    folder is wiped by the sweeper once the album is imported. Lidarr's
    rename may have moved the file to the artist folder or renamed it
    ("Artist - Album - 05 - Title"), so the album folder and its parent are
    both searched by name.
    """
    if library_root:
        folder = src.get("lidarr_album_path") or ""
        roots = [library_root]
    else:
        folder = src.get("album_path") or ""
        roots = [download_dir]
    if not folder:
        return []
    folders = [folder.rstrip("/\\")]
    if library_root:
        folders.append(os.path.dirname(folders[0]))
    ext = "." + audio_ext.lower().lstrip(".")
    title = src.get("track_title") or ""
    names = []
    if src.get("file_path"):
        names.append(os.path.basename(src["file_path"]))
    number = _int(src.get("track_number"), 0)
    if title and number:
        names.append(f"{number:02d} - {sanitize_filename(title)}{ext}")
    want = norm_name(sanitize_filename(title))
    found = []
    for directory in folders:
        if not _inside(directory, roots):
            continue
        if library_root and download_dir and _inside(directory, [download_dir]):
            continue
        for name in names:
            path = os.path.join(directory, name)
            if os.path.isfile(path) and path not in found:
                found.append(path)
        if not want:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            stem, entry_ext = os.path.splitext(entry.name)
            if entry_ext.lower() != ext or not entry.is_file():
                continue
            match = _NAME_NUMBER_RE.match(stem)
            if norm_name(match.group(1) if match else stem) == want:
                if entry.path not in found:
                    found.append(entry.path)
    return found


def find_source_file(src, key, *, library_root, download_dir, audio_ext,
                     expected_ms=0):
    """Path of a verified file for ``src`` (a stored success), else ``""``."""
    for path in _candidate_files(src, library_root, download_dir, audio_ext):
        if file_matches(path, key, expected_ms):
            return path
    return ""


@dataclass
class Reuse:
    track: dict
    source: dict
    path: str


@dataclass
class Plan:
    mode: str = MODE_OFF
    download: list = field(default_factory=list)
    reuse: list = field(default_factory=list)
    repeats: list = field(default_factory=list)  # [(track, leader, path)]
    skipped: list = field(default_factory=list)  # [(track, reason)]
    would: list = field(default_factory=list)
    # Every track this run still has to deliver (downloaded, copied or
    # reused), in the order given.
    keep: list = field(default_factory=list)

    @property
    def acting(self):
        return self.mode == MODE_REUSE


def plan_album(
    album_id, album_tracks, tracks_to_download, cfg, *,
    force=False, client_grab=False, deferred=None, local_file=None,
    library_root="", download_dir="", audio_ext="mp3",
    artist_name="", album_title="",
):
    """Decide, per pending track, what is left to do. See the module doc.

    ``tracks_to_download`` is what ``_filter_tracks`` left; ``album_tracks``
    the whole tracklist, so repeats can be told from the other positions
    (``local_file(track)`` gives the finished file of a position, or
    ``""``); ``artist_name`` / ``album_title`` tell editions of this album
    from other albums. With ``dedupe_mode=log`` the answer is only logged: the plan
    returns every track as a normal download.
    """
    mode = mode_of(cfg)
    plan = Plan(mode=mode, download=list(tracks_to_download))
    plan.keep = list(tracks_to_download)
    if mode == MODE_OFF or force or not tracks_to_download:
        return plan
    deferred = deferred or {}
    local_file = local_file or (lambda track: "")
    same_album = (cfg or {}).get("dedupe_same_album", SAME_ALBUM_SKIP)
    if client_grab:
        # Lidarr imports what is in the job folder: a missing position
        # would fail the import, so every position gets its file.
        same_album = SAME_ALBUM_COPY
    cross_album = bool((cfg or {}).get("dedupe_cross_album", True))

    pending_ids = {id(t) for t in tracks_to_download}
    groups = {}
    for track in sorted(album_tracks or [], key=track_order):
        key = recording_key(track)
        if key:
            groups.setdefault(key, []).append(track)

    skipped = {}   # id(track) -> reason
    repeat_of = {}  # id(track) -> (leader, path)
    leaders = set()
    for key, members in groups.items():
        if len(members) < 2:
            continue
        pending = [m for m in members if id(m) in pending_ids]
        if not pending:
            continue
        others = [m for m in members if id(m) not in pending_ids]
        blocked = [m for m in others if m.get("title", "") in deferred]
        done = [m for m in others if m.get("hasFile")]
        on_disk = [(m, local_file(m)) for m in others if not m.get("hasFile")]
        on_disk = [(m, p) for m, p in on_disk if p]
        if blocked:
            for m in pending:
                skipped[id(m)] = (
                    "same recording as a position that keeps failing"
                )
        elif same_album == SAME_ALBUM_SKIP and (done or on_disk):
            for m in pending:
                skipped[id(m)] = (
                    "same recording already present in this release"
                )
        elif same_album == SAME_ALBUM_COPY and on_disk:
            # write every position from the file that is already there
            leader, path = on_disk[0]
            for m in pending:
                repeat_of[id(m)] = (leader, path)
        else:
            leaders.add(id(pending[0]))
            for m in pending[1:]:
                if same_album == SAME_ALBUM_SKIP:
                    skipped[id(m)] = (
                        f"repeats '{pending[0].get('title', '')}' in this"
                        " release"
                    )
                else:
                    repeat_of[id(m)] = (pending[0], "")

    sources_for = {}
    for track in tracks_to_download:
        if id(track) in skipped or id(track) in repeat_of:
            continue
        key = recording_key(track)
        if not key:
            continue
        if key not in sources_for:
            try:
                sources_for[key] = models.get_verified_sources(key)
            except Exception:
                logger.debug("Dedupe source lookup failed", exc_info=True)
                sources_for[key] = []
        same_album_hit = ""
        other_hit = None
        for src in sources_for[key]:
            is_same = src.get("album_id") == album_id
            if is_same and client_grab:
                continue  # Lidarr needs the file in the job folder
            # Another edition of the same album is never "another album":
            # dedupe_cross_album only decides about singles, compilations…
            if not is_same and not cross_album and not is_edition(
                src, artist_name, album_title,
            ):
                continue
            path = find_source_file(
                src, key, library_root=library_root,
                download_dir=download_dir, audio_ext=audio_ext,
                expected_ms=track.get("duration") or 0,
            )
            if not path:
                continue
            if is_same:
                same_album_hit = path
                break
            if other_hit is None:
                other_hit = (src, path)
        if same_album_hit:
            skipped[id(track)] = (
                f"already downloaded for this album ({same_album_hit})"
            )
        elif other_hit is not None:
            plan.reuse.append(Reuse(track, other_hit[0], other_hit[1]))

    reuse_ids = {id(r.track) for r in plan.reuse}
    for track in tracks_to_download:
        title = track.get("title", "")
        if id(track) in skipped:
            plan.skipped.append((track, skipped[id(track)]))
            plan.would.append(f"'{title}': would skip - {skipped[id(track)]}")
        elif id(track) in repeat_of:
            leader, path = repeat_of[id(track)]
            plan.repeats.append((track, leader, path))
            plan.would.append(
                f"'{title}': would copy the file of"
                f" '{leader.get('title', '')}' (same recording, this release)"
            )
        elif id(track) in reuse_ids:
            reuse = next(r for r in plan.reuse if id(r.track) == id(track))
            plan.would.append(
                f"'{title}': would copy from"
                f" {reuse.source.get('artist_name', '')} -"
                f" {reuse.source.get('album_title', '')} ({reuse.path})"
            )

    if mode == MODE_REUSE:
        plan.download = [
            t for t in tracks_to_download
            if id(t) not in skipped and id(t) not in repeat_of
            and id(t) not in reuse_ids
        ]
        plan.keep = [t for t in tracks_to_download if id(t) not in skipped]
    else:
        # log: nothing changes, only what would have happened is recorded
        plan.reuse = []
        plan.repeats = []
        plan.skipped = []
    return plan


def log_plan(plan, album_title=""):
    """One line per decision plus a summary (``log`` and ``reuse``)."""
    if not plan.would:
        return
    verb = "Dedupe" if plan.acting else "Dedupe (log only)"
    for line in plan.would[:_LOG_LINES_PER_ALBUM]:
        logger.info("   %s: %s", verb, line)
    if len(plan.would) > _LOG_LINES_PER_ALBUM:
        logger.info(
            "   %s: ... and %d more",
            verb, len(plan.would) - _LOG_LINES_PER_ALBUM,
        )
    logger.info(
        "   %s: %d track(s) of '%s' need no download",
        verb, len(plan.would), album_title,
    )
