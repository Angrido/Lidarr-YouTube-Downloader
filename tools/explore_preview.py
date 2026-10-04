import copy
import hashlib
import io
import json
import os
import struct
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
FIXTURES = os.path.join(ROOT, "tests", "fixtures", "ytmusic")

_tmp = tempfile.mkdtemp(prefix="explore-preview-")
os.environ["DOWNLOAD_PATH"] = os.path.join(_tmp, "downloads")
os.environ["LIDARR_PATH"] = os.path.join(_tmp, "music")
os.environ.setdefault("LIDARR_URL", "http://lidarr.invalid:8686")
os.environ.setdefault("LIDARR_API_KEY", "preview")

import config  # noqa: E402

config.CONFIG_FILE = os.path.join(_tmp, "config.json")

import db  # noqa: E402

db.DB_PATH = os.path.join(_tmp, "preview.db")

from flask import Response, request  # noqa: E402

import app as app_module  # noqa: E402
import explore  # noqa: E402
import library  # noqa: E402


def _slug(text):
    return "".join(c.lower() if c.isalnum() else "-" for c in text).strip("-")


def fx(name):
    with open(os.path.join(FIXTURES, name + ".json"), encoding="utf-8") as f:
        return json.load(f)


def thumbs(seed):
    base = f"https://lh3.googleusercontent.com/preview-{seed}"
    return [
        {"url": f"{base}=w226-h226-l90-rj", "width": 226, "height": 226},
        {"url": f"{base}=w544-h544-l90-rj", "width": 544, "height": 544},
    ]


SCENARIOS = {
    "MPREb_statusNew01": ("Glass Hours", "Aurora Vale", "not_in_library"),
    "MPREb_statusMiss1": ("Midnight Tapes", "Luna Park", "missing"),
    "MPREb_statusFull1": ("Polar Lines", "Nordic Tide", "complete"),
    "MPREb_statusUnmo1": ("Blue Hour", "The Lanterns", "unmonitored"),
    "MPREb_statusNoMB1": ("Bedroom Demos Vol. 2", "Sofia Rey", "not_on_musicbrainz"),
    "MPREb_statusAmbi1": ("Satellites", "Juno Park", "ambiguous"),
}

SONG_TITLES = [
    "Golden Hour Drive", "Paper Planes", "Neon Psalms", "Low Tide", "Glasshouse",
    "Satellite Heart", "Saltwater", "Northern Lights", "Velvet Static", "Afterglow",
    "Slow Motion City", "Cassette Summer", "Echo Park", "Moonlit Avenue", "Kite Club",
    "Silver Lining", "Gravity Falls", "Summer Static", "Lighthouse", "Fever Dream",
    "Daydreamer", "Polaroid", "Midnight Swim", "Wildflower",
]
ARTIST_NAMES = [
    "Aurora Vale", "Luna Park", "Nordic Tide", "The Lanterns", "Sofia Rey",
    "Juno Park", "Velvet Static", "Marea", "Kite Club", "Atlas Grey",
]


def album_raw(browse_id):
    raw = copy.deepcopy(fx("doc_get_album"))
    title, artist, _ = SCENARIOS.get(browse_id, ("Glass Hours", "Aurora Vale", ""))
    raw["title"] = title
    raw["artists"] = [{"name": artist, "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}]
    raw["thumbnails"] = thumbs(_slug(artist + "-" + title))
    names = [
        "Opening Night", "Neon Rain (feat. Ari)", "Cassette Heart", "Blue Hour",
        "Afterglow", "Late Train", "Paper Moons", "Static Bloom", "Interlude",
        "Northern Lights", "Echoes", "Closing Credits",
    ]
    raw["tracks"] = [
        {
            "videoId": f"vid{i:08d}"[:11], "title": n,
            "artists": [{"name": artist, "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
            "isAvailable": i != 8, "isExplicit": i in (1, 4),
            "duration_seconds": 150 + i * 17, "trackNumber": i + 1,
        }
        for i, n in enumerate(names)
    ]
    raw["tracks"][8]["videoId"] = None
    raw["trackCount"] = len(names)
    raw["duration_seconds"] = sum(t["duration_seconds"] for t in raw["tracks"])
    raw["audioPlaylistId"] = "OLAK5uy_preview" + browse_id[-5:] + "0000000000"
    return raw


def explore_raw():
    data = copy.deepcopy(fx("doc_get_explore"))
    data["new_releases"] = [
        {"title": t, "type": "EP" if i == 1 else "Album", "artists": [{"name": a, "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
         "browseId": bid, "audioPlaylistId": f"OLAK5uy_preview{bid[-5:]}0000000000", "thumbnails": thumbs(_slug(a + "-" + t)),
         "isExplicit": i == 0}
        for i, (bid, (t, a, _)) in enumerate(SCENARIOS.items())
    ]
    songs = []
    for i in range(16):
        songs.append({
            "title": SONG_TITLES[i], "videoId": f"trend{i:06d}"[:11],
            "videoType": "MUSIC_VIDEO_TYPE_ATV",
            "artists": [{"name": ARTIST_NAMES[i % len(ARTIST_NAMES)], "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
            "album": {"name": "Polar Lines", "id": "MPREb_statusFull1"}, "thumbnails": thumbs(f"t{i}"),
            "isExplicit": i % 5 == 0,
        })
    data["trending"]["items"] = songs
    data["new_videos"] = [
        {"title": f"{SONG_TITLES[-i - 1]} (Official Video)", "videoId": f"video{i:06d}"[:11], "videoType": "MUSIC_VIDEO_TYPE_OMV",
         "artists": [{"name": ARTIST_NAMES[(i + 3) % len(ARTIST_NAMES)], "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}], "thumbnails": thumbs(f"v{i}"), "views": f"{i + 2}M"}
        for i in range(8)
    ]
    return data


def charts_raw():
    data = copy.deepcopy(fx("get_charts_IT"))
    data["countries"]["options"] = ["ZZ", "IT", "US", "GB", "DE", "FR", "ES", "JP"]
    for i, a in enumerate(data["artists"]):
        a["browseId"] = "UCUDVBtnOQi4c7E8jebpjc9Q" if i == 0 else a["browseId"]
        a["title"] = ARTIST_NAMES[i % len(ARTIST_NAMES)]
        a["thumbnails"] = thumbs(f"artist{i}")
    titles = ["Trending 20 · Italy", "Daily top music videos · Italy", "Top 100 music videos · Italy"]
    for i, v in enumerate(data["videos"]):
        v["title"] = titles[i % len(titles)]
        v["thumbnails"] = thumbs(f"chart{i}")
    return data


def playlist_raw():
    data = copy.deepcopy(fx("get_playlist_chart"))
    tracks = []
    for i in range(24):
        tracks.append({
            "videoId": None if i == 3 else f"chart{i:06d}"[:11], "title": SONG_TITLES[i % len(SONG_TITLES)],
            "artists": [{"name": ARTIST_NAMES[(i + 1) % len(ARTIST_NAMES)], "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
            "album": None, "thumbnails": thumbs(f"c{i}"), "isAvailable": i != 3,
            "isExplicit": i % 6 == 0, "duration_seconds": 170 + i * 3,
        })
    data["tracks"] = tracks
    data["trackCount"] = len(tracks)
    data["title"] = "Top 100 music videos · Italy"
    data["description"] = "This week's most watched music videos."
    data["author"] = {"name": "Charts", "id": None}
    data["thumbnails"] = thumbs("chartpl")
    return data


def artist_raw():
    data = copy.deepcopy(fx("doc_get_artist"))
    data["albums"]["results"] = [
        {"title": t, "year": str(2010 + i), "browseId": bid, "thumbnails": thumbs(_slug(a + "-" + t)), "type": "Album"}
        for i, (bid, (t, a, _)) in enumerate(SCENARIOS.items())
    ]
    data["name"] = "Aurora Vale"
    data["subscribers"] = "1.2M"
    data["monthlyListeners"] = "8.4M monthly listeners"
    data["description"] = (
        "Aurora Vale is a dream-pop project mixing analog synths, tape echo"
        " and close-mic vocals. Their records move between late-night drives"
        " and early-morning light."
    )
    data["songs"]["results"] = [
        {"videoId": f"song{i:07d}"[:11], "title": SONG_TITLES[i + 4], "thumbnails": thumbs(f"s{i}"),
         "artists": [{"name": "Aurora Vale", "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
         "album": {"name": "Glass Hours", "id": "MPREb_statusNew01"}}
        for i in range(5)
    ]
    data["singles"]["results"] = [
        {"title": SONG_TITLES[i + 12], "type": "Single", "year": str(2019 + i), "browseId": f"MPREb_single{i:05d}", "thumbnails": thumbs(f"single{i}")}
        for i in range(5)
    ]
    data["related"]["results"] = [
        {"browseId": f"UCrelated{i:015d}", "subscribers": f"{300 + i * 40}K", "title": ARTIST_NAMES[i + 1], "thumbnails": thumbs(f"rel{i}")}
        for i in range(6)
    ]
    data["thumbnails"] = thumbs("auroravale")
    return data


VIDEO_ONLY_ARTIST = "UCZCjpHpj2MJ2Z_txErorumg"


def video_only_artist_raw():
    titles = [
        "Zo Killeuh -  KOUMAY FENN( Clip Officiel )",
        "Zo Killeuh - Back In The Days [Directed by @AFROCONNECTIONSN ]",
        "ZO KILLEUH_L.W.M.D( Clip Officiel )",
        "Zo Killeuh x Dip Doundou guiss - GALSEN VERSUZ (Clip Officiel)",
        "Zo Killeuh - Notification ( Clip Officiel )",
    ]
    return {
        "name": "Zo killeuh", "description": "", "subscribers": "20.3K",
        "thumbnails": thumbs("zokilleuh"),
        "songs": {"browseId": None},
        "videos": {"browseId": None, "results": [
            {"title": t, "videoId": f"zovid{i:06d}"[:11], "videoType": "MUSIC_VIDEO_TYPE_OMV",
             "artists": [{"name": "Zo killeuh", "id": VIDEO_ONLY_ARTIST}], "thumbnails": thumbs(f"zo{i}"),
             "views": f"{i + 1}00K"}
            for i, t in enumerate(titles)
        ]},
    }


class PreviewYT:
    def get_home(self, limit=3):
        return fx("get_home")

    def get_explore(self):
        return explore_raw()

    def get_charts(self, country="ZZ"):
        return charts_raw()

    def get_mood_categories(self):
        return fx("doc_get_mood_categories")

    def get_mood_playlists(self, params):
        items = copy.deepcopy(fx("doc_get_mood_playlists"))
        for i, it in enumerate(items):
            it["playlistId"] = "PL4fGSI1pDJn5BPviUFX4a3IMnAgyknC68"
            it["thumbnails"] = thumbs(f"m{i}")
        return items * 3

    def get_album(self, browse_id):
        return album_raw(browse_id)

    def get_artist(self, channel_id):
        if channel_id == VIDEO_ONLY_ARTIST:
            return video_only_artist_raw()
        return artist_raw()

    def get_playlist(self, playlist_id, limit=100):
        return playlist_raw()

    def search(self, query, filter=None, limit=20):
        if filter == "albums":
            return [dict(r, resultType="album") for r in explore_raw()["new_releases"]]
        if filter == "songs":
            return [dict(s, resultType="song") for s in explore_raw()["trending"]["items"][:8]]
        if filter == "artists":
            return fx("search_artists")
        return fx("search_mixed")

    def get_search_suggestions(self, query):
        return [f"{query} {s}" for s in ("live", "acoustic", "remix", "album", "lyrics")]


def candidate(title, artist, **extra):
    base = {
        "foreignAlbumId": "1b022e01-4da6-387b-8658-" + hashlib.md5(title.encode()).hexdigest()[:12],
        "title": title, "artistName": artist, "year": "2017", "type": "Album",
        "trackCount": 12, "inLibrary": False, "monitored": False, "missingTracks": None,
        "id": None, "disambiguation": "", "secondaryTypes": [], "image": "",
    }
    base.update(extra)
    return base


def fake_search(kind, term):
    if kind == "artist" and "killeuh" in term.lower():
        return []
    if kind == "artist":
        return [
            {"name": "Aurora Vale", "foreignArtistId": "39ab1aed-75e0-4140-bd47-540276886b60", "disambiguation": "dream-pop duo", "type": "Group", "genres": ["dream pop"], "inLibrary": False},
            {"name": "Aurora Vale", "foreignArtistId": "49ab1aed-75e0-4140-bd47-540276886b60", "disambiguation": "jazz trio", "type": "Group", "genres": [], "inLibrary": False},
        ]
    for bid, (title, artist, status) in SCENARIOS.items():
        if title.lower() in term.lower():
            if status == "not_on_musicbrainz":
                return []
            if status == "ambiguous":
                return [candidate(title, artist, year="1994", trackCount=10), candidate(title, artist, year="2001", trackCount=10, foreignAlbumId="2b022e01-4da6-387b-8658-8678046e4cef")]
            extra = {
                "not_in_library": {},
                "missing": {"inLibrary": True, "monitored": True, "missingTracks": 4, "id": 11},
                "complete": {"inLibrary": True, "monitored": True, "missingTracks": 0, "id": 12},
                "unmonitored": {"inLibrary": True, "monitored": False, "missingTracks": 12, "id": 13},
            }[status]
            return [candidate(title, artist, **extra)]
    return []


def fake_add_album(body):
    return {"id": 99, "title": "Glass Hours", "artistName": "Aurora Vale", "status": "added", "queued": None}


def fake_options():
    return {
        "rootFolders": [{"path": "/music", "name": "Music", "freeSpace": 4 * 1024 ** 4, "defaultQualityProfileId": 1, "defaultMetadataProfileId": 1}],
        "qualityProfiles": [{"id": 1, "name": "Any"}, {"id": 2, "name": "Lossless"}],
        "metadataProfiles": [{"id": 1, "name": "Standard"}],
        "monitorOptions": list(library.MONITOR_OPTIONS), "newItemOptions": list(library.NEW_ITEMS_OPTIONS),
    }


def silent_wav(seconds=30, rate=8000):
    frames = b"\x00\x00" * rate * seconds
    buf = io.BytesIO()
    buf.write(b"RIFF" + struct.pack("<I", 36 + len(frames)) + b"WAVEfmt ")
    buf.write(struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16))
    buf.write(b"data" + struct.pack("<I", len(frames)) + frames)
    return buf.getvalue()


WAV = silent_wav()


def fake_stream():
    return Response(WAV, mimetype="audio/wav", headers={"Accept-Ranges": "none"})


PALETTES = [
    ("#ff7a59", "#ffc46b", "#2a1633"),
    ("#5b8cff", "#b18cff", "#0d1430"),
    ("#2ecf8f", "#c4f06a", "#06261b"),
    ("#ff6fa8", "#ffb199", "#2f0b24"),
    ("#2ec5e6", "#4f7cff", "#071d33"),
    ("#ffb020", "#ff5a4e", "#2e0d05"),
    ("#e9edf5", "#9aa6bd", "#121826"),
    ("#19c2b0", "#9be15d", "#03241f"),
    ("#c38bff", "#ffa3e6", "#22093d"),
    ("#ffe08a", "#ff9f9f", "#33150a"),
]


def art_svg(seed):
    h = hashlib.sha256(seed.encode()).digest()
    c1, c2, bg = PALETTES[h[0] % len(PALETTES)]
    kind = h[1] % 6
    x = 140 + h[2] % 260
    y = 140 + h[3] % 260
    parts = [
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{bg}"/>'
        f'<stop offset="1" stop-color="{c1}" stop-opacity=".55"/></linearGradient>'
        f'<radialGradient id="r" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{c2}"/>'
        f'<stop offset="1" stop-color="{c1}"/></radialGradient>'
        f'<filter id="b"><feGaussianBlur stdDeviation="46"/></filter></defs>',
        '<rect width="544" height="544" fill="url(#g)"/>',
    ]
    if kind == 0:
        parts.append('<circle cx="272" cy="300" r="150" fill="url(#r)"/>')
        for i in range(6):
            yy = 330 + i * 22
            parts.append(f'<rect x="0" y="{yy}" width="544" height="{4 + i * 2}" fill="{bg}"/>')
    elif kind == 1:
        for i in range(7, 0, -1):
            parts.append(f'<circle cx="{x}" cy="{y}" r="{i * 38}" fill="none" stroke="{c1 if i % 2 else c2}" stroke-width="10" opacity="{0.25 + i * 0.08:.2f}"/>')
    elif kind == 2:
        for i in range(-3, 8):
            parts.append(f'<rect x="{i * 90}" y="-200" width="44" height="944" fill="{c1 if i % 2 else c2}" opacity=".7" transform="rotate(32 272 272)"/>')
    elif kind == 3:
        parts.append(f'<g filter="url(#b)"><circle cx="{x}" cy="{y}" r="190" fill="{c1}"/><circle cx="{544 - x}" cy="{544 - y}" r="170" fill="{c2}"/></g>')
    elif kind == 4:
        for gx in range(8):
            for gy in range(8):
                rr = 6 + ((gx * 7 + gy * 3 + h[4]) % 14)
                parts.append(f'<circle cx="{44 + gx * 65}" cy="{44 + gy * 65}" r="{rr}" fill="{c1 if (gx + gy) % 2 else c2}" opacity=".85"/>')
    else:
        for i in range(9):
            yy = 120 + i * 40
            amp = 30 + (h[5 + i] % 40)
            parts.append(
                f'<path d="M-20 {yy} C 120 {yy - amp}, 220 {yy + amp}, 300 {yy} S 480 {yy - amp}, 570 {yy}" '
                f'fill="none" stroke="{c1 if i % 2 else c2}" stroke-width="7" stroke-linecap="round" opacity=".85"/>'
            )
    body = "".join(parts)
    return f'<svg xmlns="http://www.w3.org/2000/svg" width="544" height="544" viewBox="0 0 544 544">{body}</svg>'


def fake_thumbnail():
    url = request.args.get("url", "").split("=")[0]
    seed = url.split("preview-", 1)[1] if "preview-" in url else url
    return Response(art_svg(seed), mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


def demo_cover(seed):
    return Response(art_svg(seed), mimetype="image/svg+xml", headers={"Cache-Control": "public, max-age=3600"})


DEMO_ALBUMS = [
    ("Glass Hours", "Aurora Vale", "2026-09-26", 12, 12),
    ("Midnight Tapes", "Luna Park", "2026-09-19", 10, 4),
    ("Polar Lines", "Nordic Tide", "2026-09-12", 11, 11),
    ("Neon Psalms", "Velvet Static", "2026-08-29", 9, 9),
    ("Sal y Sol", "Marea", "2026-08-22", 13, 2),
    ("Paper Planes", "Kite Club", "2026-08-15", 8, 8),
    ("Northern Lights", "Atlas Grey", "2026-07-31", 12, 7),
    ("Blue Hour", "The Lanterns", "2026-07-18", 10, 10),
    ("Bedroom Demos Vol. 2", "Sofia Rey", "2026-07-04", 7, 7),
    ("Satellites", "Juno Park", "2026-06-20", 14, 14),
    ("Low Tide", "Okra Moon", "2026-06-06", 9, 3),
    ("Greenhouse", "Mono Garden", "2026-05-23", 11, 11),
    ("Summer Static", "Ivory Coast Line", "2026-05-09", 10, 10),
    ("Wildflower", "Fern & Fable", "2026-04-25", 12, 1),
    ("Silver Lining", "Cloud Atlas Club", "2026-04-11", 9, 9),
    ("Fever Dream", "Nova Lux", "2026-03-28", 10, 10),
]


def demo_album(i, base_url):
    title, artist, date, total, missing = DEMO_ALBUMS[i]
    return {
        "id": 101 + i,
        "foreignAlbumId": f"{i:08d}-0000-4000-8000-000000000000",
        "title": title,
        "releaseDate": date + "T00:00:00Z",
        "albumType": "EP" if total < 9 else "Album",
        "monitored": True,
        "artist": {"artistName": artist, "id": 201 + i, "foreignArtistId": ""},
        "images": [{"coverType": "cover", "remoteUrl": f"{base_url}/demo/cover/{_slug(artist + '-' + title)}.svg"}],
        "statistics": {"trackCount": total, "trackFileCount": total - missing},
    }


def seed_demo(base_url):
    import time as _time

    import models
    import processing

    albums = [demo_album(i, base_url) for i in range(len(DEMO_ALBUMS))]
    by_id = {a["id"]: a for a in albums}
    run_id = models.bump_sync_run_id()
    models.upsert_missing_albums_batch(albums, run_id)
    now = _time.time()
    models.update_sync_state(
        status="idle", last_full_sync_at=now - 600, last_attempt_at=now - 600,
        total_records=len(albums), synced_records=len(albums), current_run_id=run_id,
    )

    def fake_lidarr(endpoint, *a, **k):
        if endpoint == "system/status":
            return {"version": "2.9.6.4552"}
        if endpoint.startswith("album/"):
            album = by_id.get(int(endpoint.split("/")[1]) if endpoint.split("/")[1].isdigit() else 0)
            return album or {"error": "not found"}
        return {}

    app_module.lidarr_request = fake_lidarr
    for album_id in (104, 106, 112):
        models.enqueue_album(album_id)

    active = by_id[102]
    track_names = SONG_TITLES[:10]
    states = ["done", "done", "done", "done", "downloading", "searching", "pending", "pending", "pending", "pending"]
    processing.download_process.update({
        "active": True,
        "album_id": active["id"],
        "album_title": active["title"],
        "artist_name": active["artist"]["artistName"],
        "cover_url": active["images"][0]["remoteUrl"],
        "current_track_index": 4,
        "tracks": [
            {
                "track_title": name, "track_number": n + 1, "status": states[n],
                "youtube_url": f"https://music.youtube.com/watch?v=demo{n:07d}" if states[n] != "pending" else "",
                "youtube_title": f"{active['artist']['artistName']} - {name} (Official Audio)" if states[n] == "done" else "",
                "progress_percent": "62.4%" if states[n] == "downloading" else "",
                "progress_speed": "3.18MiB/s" if states[n] == "downloading" else "",
                "error_message": "", "skip": False,
            }
            for n, name in enumerate(track_names)
        ],
    })

    formats = ["251 · webm · 160 kbps", "140 · m4a · 128 kbps", "774 · webm · 256 kbps", "141 · m4a · 256 kbps"]
    conn = __import__("db").get_db()
    history = [(i, d) for i, d in zip((100, 107, 109, 110, 112, 114, 115, 113, 108, 111), (0.1, 0.4, 1.2, 2.5, 3.1, 5.3, 7.8, 11.2, 16.5, 24.0))]
    for idx, days_ago in history:
        album = by_id[101 + (idx - 100)]
        total = album["statistics"]["trackCount"]
        for n in range(total):
            ok = not (idx in (110, 113) and n in (2, 7)) and not (idx == 108 and n == 4)
            rid = models.add_track_download(
                album_id=album["id"], album_title=album["title"],
                artist_name=album["artist"]["artistName"],
                track_title=SONG_TITLES[(n + idx) % len(SONG_TITLES)], track_number=n + 1,
                success=ok,
                error_message="" if ok else "No candidate passed verification",
                youtube_url=f"https://music.youtube.com/watch?v=h{idx:03d}{n:06d}"[:43],
                youtube_title=SONG_TITLES[(n + idx) % len(SONG_TITLES)],
                match_score=0.86 + ((n * 7 + idx) % 13) / 100,
                duration_seconds=170 + n * 9, album_path="", lidarr_album_path="",
                cover_url=album["images"][0]["remoteUrl"],
                acoustid_score=0.97 if ok and n % 3 else 0.0,
                source_format=formats[(n + idx) % len(formats)] if ok else "",
            )
            conn.execute(
                "UPDATE track_downloads SET timestamp = ? WHERE id = ?",
                (now - days_ago * 86400 - n * 40, rid),
            )
        conn.commit()
        failed = sum(1 for n in range(total) if (idx in (110, 113) and n in (2, 7)) or (idx == 108 and n == 4))
        models.add_log(
            log_type="partial_success" if failed else "download_success",
            album_id=album["id"], album_title=album["title"],
            artist_name=album["artist"]["artistName"],
            details=f"{total - failed}/{total} tracks downloaded",
            total_file_size=total * 7_800_000,
        )
    models.add_log(
        log_type="album_error", album_id=113, album_title="Wildflower",
        artist_name="Fern & Fable", details="No YouTube match passed verification for 1 track",
    )


def install(down=False, demo=False, base_url="http://127.0.0.1:5099"):
    db.init_db()
    client = explore._Client(PreviewYT())
    if down:
        explore._client = lambda: None
    else:
        explore._client = lambda: client
    library.search = fake_search
    library.add_album = fake_add_album
    library.add_artist = lambda body: {"id": 7, "name": "Aurora Vale", "monitor": body.get("monitor", "all")}
    library.queue_when_ready = lambda *a: "waiting"
    library.get_add_options = fake_options
    app_module._execute_playlist_download = lambda *a, **k: None
    app_module.app.view_functions["api_youtube_stream"] = fake_stream
    app_module.app.view_functions["api_thumbnail_proxy"] = fake_thumbnail
    app_module.check_rate_limit = lambda *a, **k: True
    app_module.app.add_url_rule("/demo/cover/<seed>.svg", "demo_cover", demo_cover)
    if demo:
        seed_demo(base_url)
    return app_module.app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5099"))
    app = install(
        down="--down" in sys.argv, demo="--demo" in sys.argv,
        base_url=f"http://127.0.0.1:{port}",
    )
    app.run(host="127.0.0.1", port=port, threaded=True)
