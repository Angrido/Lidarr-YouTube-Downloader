import os
import threading
import requests
import yt_dlp
from flask import Flask, render_template, request, jsonify

app = Flask(__name__)

# --- CONFIGURAZIONE ---
LIDARR_URL = os.getenv("LIDARR_URL", "http://127.0.0.1:8686")
LIDARR_API_KEY = os.getenv("LIDARR_API_KEY", "")
DOWNLOAD_PATH = os.getenv("DOWNLOAD_PATH", "/DATA/Downloads")

download_status = {}

def get_headers():
    return {"X-Api-Key": LIDARR_API_KEY}

def get_missing_albums():
    try:
        endpoint = f"{LIDARR_URL}/api/v1/wanted/missing"
        # Scarichiamo fino a 200 album per avere una lista corposa
        params = {"apikey": LIDARR_API_KEY, "sortKey": "releaseDate", "sortDir": "desc", "pageSize": 200}
        response = requests.get(endpoint, params=params)
        response.raise_for_status()
        return response.json().get('records', [])
    except Exception as e:
        print(f"Error fetching albums: {e}")
        return []

def trigger_rescan(artist_id):
    """Dice a Lidarr di scansionare l'artista alla fine del download"""
    try:
        endpoint = f"{LIDARR_URL}/api/v1/command"
        payload = {
            "name": "RescanArtist",
            "artistId": artist_id
        }
        requests.post(endpoint, json=payload, headers=get_headers())
        print(f"Rescan triggered for artist {artist_id}")
    except Exception as e:
        print(f"Failed to trigger rescan: {e}")

def download_album_task(album_id, album_title, artist_name, artist_id, mb_release_id):
    """Logica di download con injection del MusicBrainz ID"""
    global download_status
    
    download_status[album_id] = {'state': 'preparing', 'current': 0, 'total': 0, 'percent': 0}
    
    try:
        # 1. Ottieni tracce da Lidarr
        tracks_resp = requests.get(f"{LIDARR_URL}/api/v1/track?albumId={album_id}", headers=get_headers())
        tracks = tracks_resp.json()
        valid_tracks = [t for t in tracks if t.get('title')]
        
        total_tracks = len(valid_tracks)
        download_status[album_id] = {'state': 'downloading', 'current': 0, 'total': total_tracks, 'percent': 0}

        # 2. Prepara cartella
        safe_artist = "".join([c for c in artist_name if c.isalnum() or c in (' ', '-', '_')]).strip()
        safe_album = "".join([c for c in album_title if c.isalnum() or c in (' ', '-', '_')]).strip()
        
        save_path = os.path.join(DOWNLOAD_PATH, safe_artist, safe_album)
        if not os.path.exists(save_path):
            os.makedirs(save_path)

        count = 0
        
        # 3. Loop download tracce
        for track in valid_tracks:
            track_title = track.get('title')
            track_number = track.get('trackNumber', 0)
            
            # Nome file standard: 01 - Titolo.mp3
            file_template = f"{save_path}/{int(track_number):02d} - {track_title}.%(ext)s"

            # --- FIX CRITICO PER IMPORT LIDARR ---
            # Creiamo gli argomenti per FFmpeg per inserire l'ID MusicBrainz
            ffmpeg_args = []
            if mb_release_id:
                 ffmpeg_args = ['-metadata', f'MusicBrainz Album Id={mb_release_id}']

            # Configurazione yt-dlp
            ydl_opts = {
                'format': 'bestaudio/best',
                'outtmpl': file_template,
                'postprocessors': [
                    {'key': 'FFmpegExtractAudio', 'preferredcodec': 'mp3', 'preferredquality': '192'},
                    {'key': 'FFmpegMetadata'}, # Scrive i tag base (Artista, Titolo)
                ],
                # Qui inseriamo il tag magico MBID
                'postprocessor_args': {
                    'ffmpeg': ffmpeg_args
                },
                'quiet': True,
                'no_warnings': True,
                'nocheckcertificate': True,
                # --- FIX CRITICO PER YOUTUBE 403 ---
                'http_headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-us,en;q=0.5',
                }
            }

            search_query = f"ytsearch1:{artist_name} {track_title} audio"
            
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([search_query])
            except Exception as e:
                print(f"Track download error: {e}")
            
            count += 1
            # Calcolo percentuale
            percent = int((count / total_tracks) * 100) if total_tracks > 0 else 0
            download_status[album_id] = {
                'state': 'downloading',
                'current': count,
                'total': total_tracks,
                'percent': percent
            }

        # 4. Fine download: Trigger Lidarr Rescan
        trigger_rescan(artist_id)

        download_status[album_id]['state'] = 'completed'
        download_status[album_id]['percent'] = 100

    except Exception as e:
        print(f"Album download error: {e}")
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
            'artistId': item['artistId'],
            # Recuperiamo l'ID MusicBrainz (fondamentale per il matching)
            'mbId': item.get('foreignReleaseId', ''), 
            'year': str(item.get('releaseDate', ''))[:4]
        })
    
    # Ordiniamo per artista per il raggruppamento nella UI
    albums.sort(key=lambda x: x['artist'])
    
    return render_template('index.html', albums=albums)

@app.route('/start_download', methods=['POST'])
def start_download():
    data = request.json
    album_id = data.get('id')
    
    if album_id in download_status and download_status[album_id]['state'] == 'downloading':
        return jsonify({"status": "already_downloading"})

    # Avviamo il thread passando anche mbId
    thread = threading.Thread(target=download_album_task, args=(
        album_id, 
        data.get('title'), 
        data.get('artist'), 
        data.get('artistId'),
        data.get('mbId') 
    ))
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
