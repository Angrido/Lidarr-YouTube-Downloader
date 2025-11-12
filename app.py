import os
import threading
import requests
import yt_dlp
from flask import Flask, render_template, request, jsonify, Response, stream_with_context

app = Flask(__name__)

LIDARR_URL = os.getenv("LIDARR_URL", "http://127.0.0.1:8686")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY", "")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/DATA/Downloads")

download_status = {}

def get_headers():
    return {"X-Api-Key": LIDARR_API_KEY}

def get_missing_albums():
    try:
        endpoint = f"{LIDARR_URL}/api/v1/wanted/missing"
        params = {"apikey": LIDARR_API_KEY, "sortKey": "releaseDate", "sortDir": "desc", "pageSize": 50}
        response = requests.get(endpoint, params=params)
        response.raise_for_status()
        return response.json().get('records', [])
    except Exception as e:
        print(e)
        return []

@app.route('/cover_proxy/<int:album_id>')
def cover_proxy(album_id):
    try:
        resp = requests.get(f"{LIDARR_URL}/api/v1/album/{album_id}", headers=get_headers())
        album_data = resp.json()
        
        remote_url = None
        if 'images' in album_data:
            for img in album_data['images']:
                if img['coverType'] == 'Cover':
                    remote_url = img['url']
                    break
        
        if not remote_url:
            return "No Cover", 404

        full_url = f"{LIDARR_URL}{remote_url}"
        req_img = requests.get(full_url, headers=get_headers(), stream=True)
        
        return Response(stream_with_context(req_img.iter_content(chunk_size=1024)), 
                        content_type=req_img.headers['content-type'])
    except Exception:
        return "Error", 404

def download_album_task(album_id, album_title, artist_name):
    global download_status
    
    download_status[album_id] = {'state': 'preparing', 'current': 0, 'total': 0, 'percent': 0}
    
    try:
        tracks_resp = requests.get(f"{LIDARR_URL}/api/v1/track?albumId={album_id}", headers=get_headers())
        tracks = tracks_resp.json()
        valid_tracks = [t for t in tracks if t.get('title')]
        
        total_tracks = len(valid_tracks)
        download_status[album_id] = {'state': 'downloading', 'current': 0, 'total': total_tracks, 'percent': 0}

        safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).strip()
        safe_album = "".join([c for c in album_title if c.isalnum() or c in (' ', '-', '_')]).strip()
        save_path = os.path.join(DOWNLOAD_PATH, safe_artist, safe_album)
        
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        ydl_opts = {
            'format': 'bestaudio/best',
            'outtmpl': f'{save_path}/%(title)s.%(ext)s',
            'postprocessors': [
                {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                {'key': 'FFmpegMetadata'},
                {'key': 'EmbedThumbnail'}
            ],
            'quiet': True,
            'no_warnings': True,
        }

        count = 0
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            for track in valid_tracks:
                track_title = track.get('title')
                search_query = f"ytsearch1:{artist_name} {track_title} audio"
                
                try:
                    ydl.download([search_query])
                except Exception:
                    pass
                
                count += 1
                percent = int((count / total_tracks) * 100) if total_tracks > 0 else 0
                download_status[album_id] = {
                    'state': 'downloading',
                    'current': count,
                    'total': total_tracks,
                    'percent': percent
                }

        download_status[album_id]['state'] = 'completed'
        download_status[album_id]['percent'] = 100

    except Exception:
        download_status[album_id]['state'] = 'error'

@app.route('/')
def index():
    raw_albums = get_missing_albums()
    albums = []
    for item in raw_albums:
        albums.append({
            'id': item['id'],
            'title': item['title'],
            'artist': item['artist']['artistName'],
            'year': str(item.get('releaseDate', ''))[:4]
        })
    return render_template('index.html', albums=albums)

@app.route('/start_download', methods=['POST'])
def start_download():
    data = request.json
    album_id = data.get('id')
    
    if album_id in download_status and download_status[album_id]['state'] == 'downloading':
        return jsonify({"status": "already_downloading"})

    thread = threading.Thread(target=download_album_task, args=(album_id, data.get('title'), data.get('artist')))
    thread.start()
    return jsonify({"status": "started"})

@app.route('/status/<int:album_id>')
def status(album_id):
    stat = download_status.get(album_id, {'state': 'idle', 'percent': 0})
    return jsonify(stat)

if __name__ == '__main__':
    if not os.path.exists(DOWNLOAD_PATH):
        os.makedirs(DOWNLOAD_PATH)
    app.run(host='0.0.0.0', port=5000)
