import os
import json
import time
import threading
import shutil
from pathlib import Path
from datetime import datetime
from flask import Flask, render_template, request, jsonify, send_from_directory
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, APIC, TXXX, UFID
import yt_dlp
import schedule
from bing_image_downloader import downloader

app = Flask(__name__)

CONFIG_FILE = '/config/config.json'
DOWNLOAD_DIR = '/downloads'
download_process = {'active': False, 'stop': False, 'progress': {}}

def load_config():
    config = {
        'lidarr_url': os.getenv('LIDARR_URL', ''),
        'lidarr_api_key': os.getenv('LIDARR_API_KEY', ''),
        'scheduler_enabled': os.getenv('SCHEDULER_ENABLED', 'false').lower() == 'true',
        'scheduler_interval': int(os.getenv('SCHEDULER_INTERVAL', '60')),
        'telegram_enabled': os.getenv('TELEGRAM_ENABLED', 'false').lower() == 'true',
        'telegram_bot_token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
        'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID', '')
    }
    
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r') as f:
                file_config = json.load(f)
                for key in config.keys():
                    if key in file_config and file_config[key]:
                        config[key] = file_config[key]
        except:
            pass
    
    return config

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, 'w') as f:
        json.dump(config, f, indent=2)

def send_telegram(message):
    config = load_config()
    if config.get('telegram_enabled') and config.get('telegram_bot_token') and config.get('telegram_chat_id'):
        try:
            url = f"https://api.telegram.org/bot{config['telegram_bot_token']}/sendMessage"
            requests.post(url, json={'chat_id': config['telegram_chat_id'], 'text': message}, timeout=10)
        except:
            pass

def lidarr_request(endpoint, method='GET', data=None):
    config = load_config()
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {'X-Api-Key': config['lidarr_api_key']}
    try:
        if method == 'GET':
            r = requests.get(url, headers=headers, timeout=30)
        elif method == 'POST':
            r = requests.post(url, headers=headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'error': str(e)}

def get_missing_albums():
    try:
        print("DEBUG: Calling wanted/missing endpoint...")
        wanted = lidarr_request('wanted/missing?pageSize=500&sortKey=releaseDate&sortDirection=descending&includeArtist=true')
        
        if isinstance(wanted, dict) and 'records' in wanted:
            records = wanted.get('records', [])
            print(f"DEBUG: Found {len(records)} missing albums")
            
            if len(records) == 0:
                return []
            
            for idx, album in enumerate(records):
                statistics = album.get('statistics', {})
                track_count = statistics.get('trackCount', 0)
                track_file_count = statistics.get('trackFileCount', 0)
                missing_count = track_count - track_file_count
                
                album['missingTrackCount'] = missing_count
                
                if idx < 3:
                    print(f"DEBUG: '{album.get('title')}' - Total: {track_count}, Files: {track_file_count}, Missing: {missing_count}")
            
            print(f"DEBUG: Returning {len(records)} albums")
            return records
        else:
            print("DEBUG: wanted/missing failed")
            return []
            
    except Exception as e:
        print(f"DEBUG: Exception: {e}")
        import traceback
        traceback.print_exc()
        return []

def get_missing_albums_fallback():
    try:
        print("DEBUG: Starting fallback method...")
        albums = lidarr_request('album')
        
        if isinstance(albums, dict) and 'error' in albums:
            print(f"DEBUG: Error getting albums: {albums['error']}")
            return []
        
        print(f"DEBUG: Got {len(albums)} total albums")
        
        missing = []
        checked = 0
        
        for album in albums:
            if not album.get('monitored'):
                continue
            
            checked += 1
            statistics = album.get('statistics', {})
            percent = statistics.get('percentOfTracks', 100)
            
            if checked <= 5:
                print(f"DEBUG: Album '{album.get('title')}' - monitored: True, percentOfTracks: {percent}")
            
            if percent < 100:
                album_id = album.get('id')
                album_detail = lidarr_request(f'album/{album_id}')
                
                if isinstance(album_detail, dict) and 'error' not in album_detail:
                    tracks = album_detail.get('tracks', [])
                    missing_count = sum(1 for t in tracks if not t.get('hasFile'))
                    
                    if missing_count > 0:
                        album_detail['missingTrackCount'] = missing_count
                        missing.append(album_detail)
                        if len(missing) <= 3:
                            print(f"DEBUG: Added missing album: {album_detail.get('title')} ({missing_count} tracks missing)")
        
        print(f"DEBUG: Checked {checked} monitored albums, found {len(missing)} with missing tracks")
        return missing
        
    except Exception as e:
        print(f"DEBUG: Exception in fallback: {e}")
        return []

def get_itunes_preview(artist, track):
    try:
        url = 'https://itunes.apple.com/search'
        params = {'term': f"{artist} {track}", 'media': 'music', 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get('resultCount', 0) > 0:
            return data['results'][0].get('previewUrl')
    except:
        pass
    return None

def get_itunes_artwork(artist, album):
    try:
        url = 'https://itunes.apple.com/search'
        params = {'term': f"{artist} {album}", 'entity': 'album', 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get('resultCount', 0) > 0:
            artwork_url = data['results'][0].get('artworkUrl100', '').replace('100x100', '3000x3000')
            if artwork_url:
                img_data = requests.get(artwork_url, timeout=15).content
                return img_data
    except:
        pass
    return None

def download_track_youtube(query, output_path):
    ydl_opts = {
        'format': 'bestaudio/best',
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '320',
        }],
        'outtmpl': output_path,
        'quiet': True,
        'no_warnings': True,
        'progress_hooks': [lambda d: update_progress(d)]
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([f"ytsearch1:{query}"])

def update_progress(d):
    if d['status'] == 'downloading':
        download_process['progress'] = {
            'percent': d.get('_percent_str', '0%').strip(),
            'speed': d.get('_speed_str', 'N/A').strip()
        }

def tag_mp3(file_path, track_info, album_info, cover_data):
    try:
        audio = MP3(file_path, ID3=ID3)
        audio.delete()
        audio.save()
        
        audio = MP3(file_path, ID3=ID3)
        audio.add_tags()
        
        audio.tags.add(TIT2(encoding=3, text=track_info['title']))
        audio.tags.add(TPE1(encoding=3, text=album_info['artist']['artistName']))
        audio.tags.add(TALB(encoding=3, text=album_info['title']))
        audio.tags.add(TDRC(encoding=3, text=str(album_info.get('releaseDate', '')[:4])))
        audio.tags.add(TRCK(encoding=3, text=f"{track_info['trackNumber']}/{album_info['trackCount']}"))
        
        if album_info.get('releases'):
            release = album_info['releases'][0]
            audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Release Track Id', text=track_info.get('foreignRecordingId', '')))
            audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=release.get('foreignReleaseId', '')))
            audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Artist Id', text=album_info['artist'].get('foreignArtistId', '')))
            audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Release Group Id', text=album_info.get('foreignAlbumId', '')))
            audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Release Country', text=release.get('country', '')))
            audio.tags.add(TXXX(encoding=3, desc='Media', text='Digital Media'))
            if release.get('label'):
                audio.tags.add(TXXX(encoding=3, desc='LABEL', text=','.join(release['label'])))
            if release.get('barcode'):
                audio.tags.add(TXXX(encoding=3, desc='BARCODE', text=release['barcode']))
        
        if track_info.get('foreignRecordingId'):
            audio.tags.add(UFID(owner='http://musicbrainz.org', data=track_info['foreignRecordingId'].encode()))
        
        if cover_data:
            audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover_data))
        
        audio.save(v2_version=4)
        return True
    except Exception as e:
        return False

def process_album_download(album_id):
    if download_process['active']:
        return {'error': 'Download already in progress'}
    
    download_process['active'] = True
    download_process['stop'] = False
    download_process['progress'] = {}
    
    try:
        print(f"DEBUG: Downloading album ID {album_id}")
        album = lidarr_request(f'album/{album_id}')
        if 'error' in album:
            print(f"DEBUG: Error getting album: {album['error']}")
            return album
        
        print(f"DEBUG: Got album: {album.get('title')}")
        
        tracks = album.get('tracks', [])
        if not tracks:
            print(f"DEBUG: No tracks found, trying track endpoint")
            tracks_response = lidarr_request(f'track?albumId={album_id}')
            if isinstance(tracks_response, list):
                tracks = tracks_response
                album['tracks'] = tracks
        
        print(f"DEBUG: Album has {len(tracks)} tracks")
        
        artist_name = album['artist']['artistName']
        album_title = album['title']
        
        root_folders = lidarr_request('rootFolder')
        if not root_folders or isinstance(root_folders, dict):
            return {'error': 'No root folder configured'}
        
        root_path = root_folders[0]['path']
        sanitized_artist = "".join(c for c in artist_name if c.isalnum() or c in (' ', '-', '_')).strip()
        sanitized_album = "".join(c for c in album_title if c.isalnum() or c in (' ', '-', '_')).strip()
        album_path = os.path.join(root_path, sanitized_artist, sanitized_album)
        os.makedirs(album_path, exist_ok=True)
        
        print(f"DEBUG: Download path: {album_path}")
        
        cover_data = get_itunes_artwork(artist_name, album_title)
        if not cover_data and album.get('images'):
            for img in album['images']:
                if img['coverType'] == 'cover':
                    try:
                        cover_data = requests.get(img['remoteUrl'], timeout=15).content
                        break
                    except:
                        pass
        
        if cover_data:
            with open(os.path.join(album_path, 'cover.jpg'), 'wb') as f:
                f.write(cover_data)
        
        tracks_to_download = [t for t in tracks if not t.get('hasFile')]
        print(f"DEBUG: {len(tracks_to_download)} tracks to download")
        
        for idx, track in enumerate(tracks_to_download, 1):
            if download_process['stop']:
                return {'stopped': True}
            
            track_title = track['title']
            try:
                track_num = int(track.get('trackNumber', idx))
            except (ValueError, TypeError):
                track_num = idx
            
            print(f"DEBUG: Downloading track {idx}/{len(tracks_to_download)}: {track_title}")
            
            query = f"{artist_name} {album_title} {track_title}"
            sanitized_track = "".join(c for c in track_title if c.isalnum() or c in (' ', '-', '_')).strip()
            temp_file = os.path.join(album_path, f"temp_{track_num:02d}")
            final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")
            
            try:
                download_track_youtube(query, temp_file)
                actual_file = temp_file + '.mp3'
                
                if os.path.exists(actual_file):
                    tag_mp3(actual_file, track, album, cover_data)
                    shutil.move(actual_file, final_file)
                    print(f"DEBUG: Downloaded: {final_file}")
                
                download_process['progress'] = {'current': idx, 'total': len(tracks_to_download)}
            except Exception as e:
                print(f"DEBUG: Error downloading track: {e}")
                continue
        
        print(f"DEBUG: Triggering Lidarr rescan")
        lidarr_request('command', method='POST', data={'name': 'RescanFolders', 'folders': [album_path]})
        
        send_telegram(f"✅ Album downloaded: {artist_name} - {album_title}")
        
        return {'success': True, 'album_path': album_path}
    except Exception as e:
        print(f"DEBUG: Exception in download: {e}")
        import traceback
        traceback.print_exc()
        return {'error': str(e)}
    finally:
        download_process['active'] = False
        download_process['progress'] = {}

@app.route('/api/test-connection')
def api_test_connection():
    config = load_config()
    if not config.get('lidarr_url') or not config.get('lidarr_api_key'):
        return jsonify({'error': 'Configuration missing', 'status': 'error'})
    
    try:
        system = lidarr_request('system/status')
        if 'error' in system:
            return jsonify({'error': system['error'], 'status': 'error'})
        
        wanted = lidarr_request('wanted/missing?pageSize=5')
        album_count = lidarr_request('album')
        
        return jsonify({
            'status': 'success',
            'lidarr_version': system.get('version', 'unknown'),
            'total_albums': len(album_count) if isinstance(album_count, list) else 0,
            'wanted_endpoint': 'available' if isinstance(wanted, dict) and 'records' in wanted else 'unavailable',
            'missing_count': wanted.get('totalRecords', 0) if isinstance(wanted, dict) else 0
        })
    except Exception as e:
        return jsonify({'error': str(e), 'status': 'error'})

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/metadata')
def metadata():
    return render_template('metadata.html')

@app.route('/scheduler')
def scheduler():
    return render_template('scheduler.html')

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(load_config())
    else:
        config = request.json
        save_config(config)
        return jsonify({'success': True})

@app.route('/api/missing-albums')
def api_missing_albums():
    print("Fetching missing albums...")
    albums = get_missing_albums()
    print(f"Found {len(albums)} missing albums")
    return jsonify(albums)

@app.route('/api/album/<int:album_id>')
def api_album_details(album_id):
    album = lidarr_request(f'album/{album_id}')
    return jsonify(album)

@app.route('/api/preview/<int:album_id>/<int:track_number>')
def api_preview(album_id, track_number):
    album = lidarr_request(f'album/{album_id}')
    if 'error' in album:
        return jsonify({'error': 'Album not found'})
    
    track = next((t for t in album.get('tracks', []) if t['trackNumber'] == track_number), None)
    if not track:
        return jsonify({'error': 'Track not found'})
    
    preview_url = get_itunes_preview(album['artist']['artistName'], track['title'])
    return jsonify({'previewUrl': preview_url})

@app.route('/api/download/<int:album_id>', methods=['POST'])
def api_download(album_id):
    thread = threading.Thread(target=process_album_download, args=(album_id,))
    thread.start()
    return jsonify({'success': True})

@app.route('/api/download/stop', methods=['POST'])
def api_download_stop():
    download_process['stop'] = True
    return jsonify({'success': True})

@app.route('/api/download/status')
def api_download_status():
    return jsonify({
        'active': download_process['active'],
        'progress': download_process['progress']
    })

@app.route('/api/artists')
def api_artists():
    root_folders = lidarr_request('rootFolder')
    if not root_folders or isinstance(root_folders, dict):
        return jsonify([])
    
    root_path = root_folders[0]['path']
    if not os.path.exists(root_path):
        return jsonify([])
    
    artists = []
    for artist_dir in os.listdir(root_path):
        full_path = os.path.join(root_path, artist_dir)
        if os.path.isdir(full_path):
            albums = []
            for album_dir in os.listdir(full_path):
                album_path = os.path.join(full_path, album_dir)
                if os.path.isdir(album_path):
                    albums.append({'name': album_dir, 'path': album_path})
            if albums:
                artists.append({'name': artist_dir, 'albums': albums})
    
    return jsonify(artists)

@app.route('/api/metadata/download', methods=['POST'])
def api_metadata_download():
    data = request.json
    artist_name = data['artist']
    album_path = data['path']
    
    try:
        os.makedirs(album_path, exist_ok=True)
        
        assets = {
            'logo.png': f"{artist_name} logo transparent png",
            'fanart.jpg': f"{artist_name} concert stage wallpaper hd",
            'banner.jpg': f"{artist_name} banner header",
            'poster.jpg': f"{artist_name} poster artwork"
        }
        
        for filename, query in assets.items():
            file_path = os.path.join(album_path, filename)
            if not os.path.exists(file_path):
                try:
                    downloader.download(query, limit=1, output_dir=album_path, adult_filter_off=True, force_replace=False, timeout=15, verbose=False)
                    downloaded = os.path.join(album_path, query)
                    if os.path.exists(downloaded):
                        files = os.listdir(downloaded)
                        if files:
                            shutil.move(os.path.join(downloaded, files[0]), file_path)
                        shutil.rmtree(downloaded)
                except:
                    pass
        
        nfo_path = os.path.join(os.path.dirname(album_path), 'artist.nfo')
        if not os.path.exists(nfo_path):
            nfo_content = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<artist>
    <name>{artist_name}</name>
</artist>"""
            with open(nfo_path, 'w', encoding='utf-8') as f:
                f.write(nfo_content)
        
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)})

def scheduled_check():
    albums = get_missing_albums()
    if albums:
        send_telegram(f"🔍 Found {len(albums)} missing albums")

def run_scheduler():
    while True:
        schedule.run_pending()
        time.sleep(10)

def setup_scheduler():
    config = load_config()
    schedule.clear()
    if config.get('scheduler_enabled'):
        interval = config.get('scheduler_interval', 60)
        schedule.every(interval).minutes.do(scheduled_check)

@app.route('/api/scheduler/toggle', methods=['POST'])
def api_scheduler_toggle():
    config = load_config()
    config['scheduler_enabled'] = not config.get('scheduler_enabled', False)
    save_config(config)
    setup_scheduler()
    return jsonify({'enabled': config['scheduler_enabled']})

if __name__ == '__main__':
    setup_scheduler()
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    app.run(host='0.0.0.0', port=5000, debug=False)
