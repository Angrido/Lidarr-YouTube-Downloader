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
os.environ.setdefault("DOWNLOAD_PATH", os.path.join(_tmp, "downloads"))
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
    "MPREb_statusNew01": ("Revival", "Eminem", "not_in_library"),
    "MPREb_statusMiss1": ("Midnight Tapes", "Luna Park", "missing"),
    "MPREb_statusFull1": ("Hangang", "Dept", "complete"),
    "MPREb_statusUnmo1": ("Blue Hour", "The Lanterns", "unmonitored"),
    "MPREb_statusNoMB1": ("Bedroom Demos Vol. 2", "Sofia Rey", "not_on_musicbrainz"),
    "MPREb_statusAmbi1": ("Weezer", "Weezer", "ambiguous"),
}


def album_raw(browse_id):
    raw = copy.deepcopy(fx("doc_get_album"))
    title, artist, _ = SCENARIOS.get(browse_id, ("Revival", "Eminem", ""))
    raw["title"] = title
    raw["artists"] = [{"name": artist, "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}]
    raw["thumbnails"] = thumbs(browse_id)
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
         "browseId": bid, "audioPlaylistId": f"OLAK5uy_preview{bid[-5:]}0000000000", "thumbnails": thumbs(bid),
         "isExplicit": i == 0}
        for i, (bid, (t, a, _)) in enumerate(SCENARIOS.items())
    ]
    songs = []
    for i in range(16):
        songs.append({
            "title": f"Trending song {i + 1}", "videoId": f"trend{i:06d}"[:11],
            "videoType": "MUSIC_VIDEO_TYPE_ATV",
            "artists": [{"name": ["Luna Park", "Dept", "Sofia Rey", "The Lanterns"][i % 4], "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
            "album": {"name": "Hangang", "id": "MPREb_statusFull1"}, "thumbnails": thumbs(f"t{i}"),
            "isExplicit": i % 5 == 0,
        })
    data["trending"]["items"] = songs
    data["new_videos"] = [
        {"title": f"New video {i + 1}", "videoId": f"video{i:06d}"[:11], "videoType": "MUSIC_VIDEO_TYPE_OMV",
         "artists": [{"name": "Dept", "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}], "thumbnails": thumbs(f"v{i}"), "views": f"{i + 2}M"}
        for i in range(8)
    ]
    return data


def charts_raw():
    data = copy.deepcopy(fx("get_charts_IT"))
    data["countries"]["options"] = ["ZZ", "IT", "US", "GB", "DE", "FR", "ES", "JP"]
    for i, a in enumerate(data["artists"]):
        a["browseId"] = "UCUDVBtnOQi4c7E8jebpjc9Q" if i == 0 else a["browseId"]
    return data


def playlist_raw():
    data = copy.deepcopy(fx("get_playlist_chart"))
    tracks = []
    for i in range(24):
        tracks.append({
            "videoId": None if i == 3 else f"chart{i:06d}"[:11], "title": f"Chart hit {i + 1}",
            "artists": [{"name": ["Geolier", "Sfera Ebbasta", "Shiva", "Anna"][i % 4], "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
            "album": None, "thumbnails": thumbs(f"c{i}"), "isAvailable": i != 3,
            "isExplicit": i % 6 == 0, "duration_seconds": 170 + i * 3,
        })
    data["tracks"] = tracks
    data["trackCount"] = len(tracks)
    data["thumbnails"] = thumbs("chartpl")
    return data


def artist_raw():
    data = copy.deepcopy(fx("doc_get_artist"))
    data["albums"]["results"] = [
        {"title": t, "year": str(2010 + i), "browseId": bid, "thumbnails": thumbs(bid), "type": "Album"}
        for i, (bid, (t, _, _)) in enumerate(SCENARIOS.items())
    ]
    data["songs"]["results"] = [
        {"videoId": f"song{i:07d}"[:11], "title": f"Popular song {i + 1}", "thumbnails": thumbs(f"s{i}"),
         "artists": [{"name": "Oasis", "id": "UCUDVBtnOQi4c7E8jebpjc9Q"}],
         "album": {"name": "Hangang", "id": "MPREb_statusFull1"}}
        for i in range(5)
    ]
    for block in ("singles", "related"):
        for r in data[block]["results"]:
            r["thumbnails"] = thumbs(r.get("browseId", block))
    data["thumbnails"] = thumbs("oasis")
    return data


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
    if kind == "artist":
        return [
            {"name": "Oasis", "foreignArtistId": "39ab1aed-75e0-4140-bd47-540276886b60", "disambiguation": "UK rock band", "type": "Group", "genres": ["britpop"], "inLibrary": False},
            {"name": "Oasis", "foreignArtistId": "49ab1aed-75e0-4140-bd47-540276886b60", "disambiguation": "US jazz trio", "type": "Group", "genres": [], "inLibrary": False},
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
    return {"id": 99, "title": "Revival", "artistName": "Eminem", "status": "added", "queued": None}


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


def fake_thumbnail():
    url = request.args.get("url", "")
    h = hashlib.md5(url.split("=")[0].encode()).hexdigest()
    a, b = f"#{h[:6]}", f"#{h[6:12]}"
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="544" height="544" viewBox="0 0 544 544">'
        f'<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{a}"/>'
        f'<stop offset="1" stop-color="{b}"/></linearGradient></defs><rect width="544" height="544" fill="url(#g)"/>'
        f'<circle cx="272" cy="272" r="120" fill="none" stroke="rgba(255,255,255,.35)" stroke-width="18"/></svg>'
    )
    return Response(svg, mimetype="image/svg+xml")


def install(down=False):
    db.init_db()
    client = explore._Client(PreviewYT())
    if down:
        explore._client = lambda: None
    else:
        explore._client = lambda: client
    library.search = fake_search
    library.add_album = fake_add_album
    library.add_artist = lambda body: {"id": 7, "name": "Oasis", "monitor": body.get("monitor", "all")}
    library.queue_when_ready = lambda *a: "waiting"
    library.get_add_options = fake_options
    app_module._execute_playlist_download = lambda *a, **k: None
    app_module.app.view_functions["api_youtube_stream"] = fake_stream
    app_module.app.view_functions["api_thumbnail_proxy"] = fake_thumbnail
    app_module.check_rate_limit = lambda *a, **k: True
    return app_module.app


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "5099"))
    install(down="--down" in sys.argv).run(host="127.0.0.1", port=port, threaded=True)
