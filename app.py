import os
import json
import time
import threading
import shutil
import re
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC, TXXX, UFID
import yt_dlp
import schedule

app = Flask(__name__)

CONFIG_FILE = '/config/config.json'
DOWNLOAD_DIR = '/spotdl'
download_process = {'active': False, 'stop': False, 'progress': {}, 'album_id': None}

def load_config():
    config = {
        'lidarr_url': os.getenv('LIDARR_URL', ''),
        'lidarr_api_key': os.getenv('LIDARR_API_KEY', ''),
        'scheduler_enabled': os.getenv('SCHEDULER_ENABLED', 'false').lower() == 'true',
        'scheduler_auto_download': os.getenv('SCHEDULER_AUTO_DOWNLOAD', 'true').lower() == 'true',
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
                    if key in file_config:
                        config[key] = file_config[key]
            if 'scheduler_interval' in config:
                config['scheduler_interval'] = int(config['scheduler_interval'])
        except:
            pass
    return config

def save_config(config):
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    if 'scheduler_interval' in config:
        config['scheduler_interval'] = int(config['scheduler_interval'])
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

def lidarr_request(endpoint, method='GET', data=None, params=None):
    config = load_config()
    url = f"{config['lidarr_url']}/api/v1/{endpoint}"
    headers = {'X-Api-Key': config['lidarr_api_key']}
    try:
        if method == 'GET':
            r = requests.get(url, headers=headers, params=params, timeout=30)
        elif method == 'POST':
            r = requests.post(url, headers=headers, json=data, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {'error': str(e)}

def get_missing_albums():
    try:
        wanted = lidarr_request('wanted/missing?pageSize=2000&sortKey=releaseDate&sortDirection=descending&includeArtist=true')
        if isinstance(wanted, dict) and 'records' in wanted:
            records = wanted.get('records', [])
            for album in records:
                stats = album.get('statistics', {})
                total = stats.get('trackCount', 0)
                files = stats.get('trackFileCount', 0)
                album['missingTrackCount'] = total - files
            return records
        return []
    except:
        return []

def get_itunes_tracks(artist, album_name):
    try:
        url = 'https://itunes.apple.com/search'
        params = {'term': f"{artist} {album_name}", 'entity': 'album', 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get('resultCount', 0) > 0:
            collection_id = data['results'][0]['collectionId']
            lookup_url = 'https://itunes.apple.com/lookup'
            lookup_params = {'id': collection_id, 'entity': 'song'}
            lookup_r = requests.get(lookup_url, params=lookup_params, timeout=10)
            lookup_data = lookup_r.json()
            tracks = []
            for item in lookup_data.get('results', [])[1:]:
                tracks.append({
                    'trackNumber': item.get('trackNumber'),
                    'title': item.get('trackName'),
                    'previewUrl': item.get('previewUrl'),
                    'hasFile': False 
                })
            return tracks
    except:
        pass
    return []

def get_itunes_artwork(artist, album):
    try:
        url = 'https://itunes.apple.com/search'
        params = {'term': f"{artist} {album}", 'entity': 'album', 'limit': 1}
        r = requests.get(url, params=params, timeout=10)
        data = r.json()
        if data.get('resultCount', 0) > 0:
            artwork_url = data['results'][0].get('artworkUrl100', '').replace('100x100', '3000x3000')
            return requests.get(artwork_url, timeout=15).content
    except:
        pass
    return None

def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '', name)
    return name.strip()

def download_track_youtube(query, output_path, track_title_original):
    ydl_opts_search = {
        'format': 'bestaudio/best',
        'quiet': True,
        'no_warnings': True,
        'extract_flat': True,
        'noplaylist': True
    }
    
    candidates = []
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts_search) as ydl:
            search_results = ydl.extract_info(f"ytsearch10:{query}", download=False)
            
            forbidden_words = ['remix', 'cover', 'mashup', 'bootleg', 'live', 'dj mix']
            
            for entry in search_results.get('entries', []):
                title = entry.get('title', '').lower()
                url = entry.get('url')
                duration = entry.get('duration', 0)
                
                is_clean = True
                for word in forbidden_words:
                    if word in title and word not in track_title_original.lower():
                        is_clean = False
                        break
                
                if duration > 900 or duration < 30:
                    is_clean = False

                if is_clean and url:
                    candidates.append(url)
    except:
        pass

    if not candidates:
        return False

    for video_url in candidates:
        try:
            ydl_opts_download = {
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
            
            with yt_dlp.YoutubeDL(ydl_opts_download) as ydl_dl:
                ydl_dl.download([video_url])
            return True
        except:
            continue
            
    return False

def update_progress(d):
    if d['status'] == 'downloading':
        download_process['progress'] = {
            'percent': d.get('_percent_str', '0%').strip(),
            'speed': d.get('_speed_str', 'N/A').strip()
        }

def set_permissions(path):
    try:
        if os.path.isdir(path):
            os.chmod(path, 0o777)
            for root, dirs, files in os.walk(path):
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o777)
                for f in files:
                    os.chmod(os.path.join(root, f), 0o666)
        else:
            os.chmod(path, 0o666)
    except:
        pass

def tag_mp3(file_path, track_info, album_info, cover_data):
    try:
        try:
            audio = MP3(file_path, ID3=ID3)
        except:
            audio = MP3(file_path)
            audio.add_tags()
        if audio.tags is None:
            audio.add_tags()

        audio.tags.add(TIT2(encoding=3, text=track_info['title']))
        audio.tags.add(TPE1(encoding=3, text=album_info['artist']['artistName']))
        audio.tags.add(TPE2(encoding=3, text=album_info['artist']['artistName']))
        audio.tags.add(TALB(encoding=3, text=album_info['title']))
        audio.tags.add(TDRC(encoding=3, text=str(album_info.get('releaseDate', '')[:4])))
        
        try:
            t_num = int(track_info['trackNumber'])
            audio.tags.add(TRCK(encoding=3, text=f"{t_num}/{album_info.get('trackCount', 0)}"))
        except:
            pass
        
        if album_info.get('releases'):
            release = album_info['releases'][0]
            if track_info.get('foreignRecordingId'):
                audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Release Track Id', text=track_info['foreignRecordingId']))
            if release.get('foreignReleaseId'):
                audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Id', text=release['foreignReleaseId']))
            if album_info['artist'].get('foreignArtistId'):
                audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Artist Id', text=album_info['artist']['foreignArtistId']))
            if album_info.get('foreignAlbumId'):
                audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Album Release Group Id', text=album_info['foreignAlbumId']))
            if release.get('country'):
                audio.tags.add(TXXX(encoding=3, desc='MusicBrainz Release Country', text=release['country']))

        if track_info.get('foreignRecordingId'):
            audio.tags.add(UFID(owner='http://musicbrainz.org', data=track_info['foreignRecordingId'].encode()))
        if cover_data:
            audio.tags.add(APIC(encoding=3, mime='image/jpeg', type=3, desc='Cover', data=cover_data))
        
        audio.save(v2_version=3)
        return True
    except:
        return False

def create_xml_metadata(output_dir, artist, album, track_num, title, album_id=None, artist_id=None):
    try:
        sanitized_title = sanitize_filename(title)
        filename = f"{track_num:02d} - {sanitized_title}.xml"
        file_path = os.path.join(output_dir, filename)
        mb_album = f"  <musicbrainzalbumid>{album_id}</musicbrainzalbumid>\n" if album_id else ""
        mb_artist = f"  <musicbrainzartistid>{artist_id}</musicbrainzartistid>\n" if artist_id else ""
        content = f"""<song>
  <title>{title}</title>
  <artist>{artist}</artist>
  <performingartist>{artist}</performingartist>
  <albumartist>{artist}</albumartist>
  <album>{album}</album>
{mb_album}{mb_artist}</song>"""
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except:
        return False

def get_valid_release_id(album):
    releases = album.get('releases', [])
    if not releases:
        return 0
    for rel in releases:
        if rel.get('monitored', False) and rel.get('id', 0) > 0:
            return rel['id']
    for rel in releases:
        if rel.get('id', 0) > 0:
            return rel['id']
    return 0

def force_lidarr_import(album_path, artist_id, album_id, release_id):
    lidarr_request('command', method='POST', data={
        'name': 'DownloadedAlbumsScan', 
        'path': album_path
    })
    time.sleep(8)
    
    importables = lidarr_request('manualimport', method='GET', params={'folder': album_path})
    
    if isinstance(importables, list) and len(importables) > 0:
        valid_files = []
        for item in importables:
            lidarr_release_id = item.get('release', {}).get('id', 0)
            final_release_id = int(lidarr_release_id) if lidarr_release_id > 0 else int(release_id)
            
            if final_release_id <= 0:
                 continue

            valid_files.append({
                'path': item['path'],
                'artistId': int(artist_id),
                'albumId': int(album_id),
                'releaseId': final_release_id,
                'quality': item.get('quality'),
                'folderName': item.get('folderName'),
                'disableReleaseCheck': True
            })
        
        if valid_files:
            lidarr_request('command', method='POST', data={
                'name': 'ManualImport',
                'files': valid_files,
                'importMode': 'Move'
            })
            return True
    
    return False

def process_album_download(album_id, force=False):
    if download_process['active']: return {'error': 'Busy'}
    download_process['active'] = True
    download_process['stop'] = False
    download_process['progress'] = {}
    download_process['album_id'] = album_id
    
    try:
        album = lidarr_request(f'album/{album_id}')
        if 'error' in album: return album
        
        tracks = album.get('tracks', [])
        if not tracks:
            try:
                tracks_res = lidarr_request(f'track?albumId={album_id}')
                if isinstance(tracks_res, list) and len(tracks_res) > 0:
                    tracks = tracks_res
            except:
                pass
        
        if not tracks:
            tracks = get_itunes_tracks(album['artist']['artistName'], album['title'])
            
        album['tracks'] = tracks

        artist_name = album['artist']['artistName']
        artist_id = album['artist']['id']
        artist_mbid = album['artist'].get('foreignArtistId', '')
        album_title = album['title']
        release_year = str(album.get('releaseDate', ''))[:4]

        release_id = get_valid_release_id(album)
        if release_id == 0:
            return {'error': 'No valid releases found for this album.'}
            
        album_mbid = album.get('foreignAlbumId', '')

        sanitized_artist = sanitize_filename(artist_name)
        sanitized_album = sanitize_filename(album_title)
        
        artist_path = os.path.join(DOWNLOAD_DIR, sanitized_artist)
        album_folder_name = f"{sanitized_album} ({release_year})" if release_year else sanitized_album
        album_path = os.path.join(artist_path, album_folder_name)
        os.makedirs(album_path, exist_ok=True)

        cover_data = get_itunes_artwork(artist_name, album_title)
        if cover_data:
            with open(os.path.join(album_path, 'cover.jpg'), 'wb') as f:
                f.write(cover_data)

        tracks_to_download = []
        for t in tracks:
            if not force:
                if t.get('hasFile', False): continue
                try:
                    track_num = int(t.get('trackNumber', 0))
                except:
                    track_num = 0
                
                track_title = t['title']
                sanitized_track = sanitize_filename(track_title)
                final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")
                if os.path.exists(final_file): continue
            
            tracks_to_download.append(t)

        if len(tracks_to_download) == 0:
            force_lidarr_import(album_path, artist_id, album_id, release_id)
            return {'success': True, 'message': 'Skipped'}

        for idx, track in enumerate(tracks_to_download, 1):
            if download_process['stop']: return {'stopped': True}
            
            track_title = track['title']
            try:
                track_num = int(track.get('trackNumber', idx))
            except:
                track_num = idx
            
            query = f"{artist_name} {track_title} official audio"
            sanitized_track = sanitize_filename(track_title)
            
            temp_file = os.path.join(album_path, f"temp_{track_num:02d}")
            final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")
            
            download_success = download_track_youtube(query, temp_file, track_title)
            actual_file = temp_file + '.mp3'
            
            if download_success and os.path.exists(actual_file):
                time.sleep(0.5)
                tag_mp3(actual_file, track, album, cover_data)
                create_xml_metadata(album_path, artist_name, album_title, track_num, track_title, album_mbid, artist_mbid)
                shutil.move(actual_file, final_file)
            
            download_process['progress'] = {'current': idx, 'total': len(tracks_to_download)}

        set_permissions(artist_path)
        
        if force_lidarr_import(album_path, artist_id, album_id, release_id):
            lidarr_request('command', method='POST', data={'name': 'RefreshArtist', 'artistId': artist_id})
            send_telegram(f"✅ Album downloaded: {artist_name} - {album_title}")
            return {'success': True}
        else:
            return {'error': 'Import failed'}
            
    except Exception as e:
        return {'error': str(e)}
    finally:
        download_process['active'] = False
        download_process['progress'] = {}
        download_process['album_id'] = None

@app.route('/api/test-connection')
def api_test_connection():
    try:
        system = lidarr_request('system/status')
        return jsonify({'status': 'success' if 'version' in system else 'error', 'lidarr_version': system.get('version', 'Unknown')})
    except:
        return jsonify({'status': 'error'})

@app.route('/')
def index(): return render_template('index.html')

@app.route('/scheduler')
def scheduler(): return render_template('scheduler.html')

@app.route('/favicon.ico')
def favicon():
    return send_from_directory(os.path.join(app.root_path, 'static'), 'favicon.ico', mimetype='image/vnd.microsoft.icon')

@app.route('/api/config', methods=['GET', 'POST'])
def api_config():
    if request.method == 'GET':
        return jsonify(load_config())
    else:
        current = load_config()
        current.update(request.json)
        save_config(current)
        return jsonify({'success': True})

@app.route('/api/missing-albums')
def api_missing_albums(): return jsonify(get_missing_albums())

@app.route('/api/album/<int:album_id>')
def api_album_details(album_id):
    album = lidarr_request(f'album/{album_id}')
    if not album.get('tracks'):
        album['tracks'] = get_itunes_tracks(album['artist']['artistName'], album['title'])
    return jsonify(album)

@app.route('/api/download/<int:album_id>', methods=['POST'])
def api_download(album_id):
    threading.Thread(target=process_album_download, args=(album_id, False)).start()
    return jsonify({'success': True})

@app.route('/api/download/stop', methods=['POST'])
def api_download_stop():
    download_process['stop'] = True
    return jsonify({'success': True})

@app.route('/api/download/status')
def api_download_status(): return jsonify(download_process)

@app.route('/api/scheduler/toggle', methods=['POST'])
def api_scheduler_toggle():
    config = load_config()
    config['scheduler_enabled'] = not config.get('scheduler_enabled', False)
    save_config(config)
    setup_scheduler()
    return jsonify({'enabled': config['scheduler_enabled']})

@app.route('/api/scheduler/autodownload/toggle', methods=['POST'])
def api_autodownload_toggle():
    config = load_config()
    config['scheduler_auto_download'] = not config.get('scheduler_auto_download', True)
    save_config(config)
    return jsonify({'enabled': config['scheduler_auto_download']})

def scheduled_check():
    if download_process['active']: return
    config = load_config()
    albums = get_missing_albums()
    if albums:
        if config.get('scheduler_auto_download', True):
            send_telegram(f"🚀 Scheduler: Downloading {len(albums)} missing albums...")
            for album in albums:
                if download_process.get('stop'): break
                process_album_download(album['id'], False)
                time.sleep(5)
        else:
            send_telegram(f"🔍 Scheduler: Found {len(albums)} missing albums (Auto-DL Disabled)")

def run_scheduler():
    while True: schedule.run_pending(); time.sleep(10)

def setup_scheduler():
    config = load_config()
    schedule.clear()
    if config.get('scheduler_enabled'):
        interval = int(config.get('scheduler_interval', 60))
        schedule.every(interval).minutes.do(scheduled_check)

if __name__ == '__main__':
    setup_scheduler()
    threading.Thread(target=run_scheduler, daemon=True).start()
    app.run(host='0.0.0.0', port=5000, debug=False)
