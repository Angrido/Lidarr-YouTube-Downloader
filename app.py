import os
import json
import time
import threading
import shutil
import re
import logging
from pathlib import Path
from flask import Flask, render_template, request, jsonify, send_from_directory
import requests
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TPE2, TALB, TDRC, TRCK, APIC, TXXX, UFID
import yt_dlp
import schedule

logging.basicConfig(
    level=logging.INFO,
    format='%(message)s',
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

log = logging.getLogger('werkzeug')
log.setLevel(logging.ERROR)

app = Flask(__name__)

VERSION = "1.0.2"

CONFIG_FILE = '/config/config.json'
DOWNLOAD_DIR = os.getenv('DOWNLOAD_PATH', '')
download_process = {
    'active': False, 
    'stop': False, 
    'progress': {}, 
    'album_id': None,
    'album_title': '',
    'artist_name': '',
    'current_track_title': ''
}

download_queue = []
download_history = []
queue_lock = threading.Lock()

def load_config():
    config = {
        'lidarr_url': os.getenv('LIDARR_URL', ''),
        'lidarr_api_key': os.getenv('LIDARR_API_KEY', ''),
        'scheduler_enabled': os.getenv('SCHEDULER_ENABLED', 'false').lower() == 'true',
        'scheduler_auto_download': os.getenv('SCHEDULER_AUTO_DOWNLOAD', 'true').lower() == 'true',
        'scheduler_interval': int(os.getenv('SCHEDULER_INTERVAL', '60')),
        'telegram_enabled': os.getenv('TELEGRAM_ENABLED', 'false').lower() == 'true',
        'telegram_bot_token': os.getenv('TELEGRAM_BOT_TOKEN', ''),
        'telegram_chat_id': os.getenv('TELEGRAM_CHAT_ID', ''),
        'xml_metadata_enabled': os.getenv('XML_METADATA_ENABLED', 'true').lower() == 'true'
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
        download_process['progress'].update({
            'percent': d.get('_percent_str', '0%').strip(),
            'speed': d.get('_speed_str', 'N/A').strip()
        })

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
    download_process['progress'] = {'current': 0, 'total': 0, 'percent': '0%', 'speed': 'N/A', 'overall_percent': 0}
    download_process['album_id'] = album_id
    download_process['album_title'] = ''
    download_process['artist_name'] = ''
    download_process['current_track_title'] = ''
    
    try:
        album = lidarr_request(f'album/{album_id}')
        if 'error' in album:
            logger.error(f"❌ Error fetching album {album_id}: {album['error']}")
            return album
        
        logger.info(f"🎵 Starting download for album: {album.get('title', 'Unknown')} - {album.get('artist', {}).get('artistName', 'Unknown')}")
        
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
        
        # Update download process with album info
        download_process['album_title'] = album_title
        download_process['artist_name'] = artist_name

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

        logger.info(f"📦 Total tracks to download: {len(tracks_to_download)}")
        
        failed_tracks = []
        
        for idx, track in enumerate(tracks_to_download, 1):
            if download_process['stop']:
                logger.warning(f"⏹️  Download stopped by user")
                return {'stopped': True}
            
            track_title = track['title']
            try:
                track_num = int(track.get('trackNumber', idx))
            except:
                track_num = idx
            
            # Update current track info
            download_process['current_track_title'] = track_title
            download_process['progress']['current'] = idx
            download_process['progress']['total'] = len(tracks_to_download)
            download_process['progress']['overall_percent'] = int((idx / len(tracks_to_download)) * 100)
            
            logger.info(f"⬇️  Downloading track {idx}/{len(tracks_to_download)}: {track_title}")
            
            query = f"{artist_name} {track_title} official audio"
            sanitized_track = sanitize_filename(track_title)
            
            temp_file = os.path.join(album_path, f"temp_{track_num:02d}")
            final_file = os.path.join(album_path, f"{track_num:02d} - {sanitized_track}.mp3")
            
            download_success = download_track_youtube(query, temp_file, track_title)
            actual_file = temp_file + '.mp3'
            
            if download_success and os.path.exists(actual_file):
                logger.info(f"✅ Track downloaded successfully: {track_title}")
                time.sleep(0.5)
                logger.info(f"🏷️  Adding metadata tags...")
                tag_mp3(actual_file, track, album, cover_data)
                config = load_config()
                if config.get('xml_metadata_enabled', True):
                    logger.info(f"📄 Creating XML metadata file...")
                    create_xml_metadata(album_path, artist_name, album_title, track_num, track_title, album_mbid, artist_mbid)
                shutil.move(actual_file, final_file)
            else:
                logger.warning(f"⚠️  Failed to download track: {track_title}")
                failed_tracks.append(track_title)
            
            # Update progress after track completion
            download_process['progress']['current'] = idx
            download_process['progress']['total'] = len(tracks_to_download)
            download_process['progress']['overall_percent'] = int((idx / len(tracks_to_download)) * 100)

        set_permissions(artist_path)
        
        if failed_tracks:
            failed_list = "\n".join([f"• {t}" for t in failed_tracks])
            send_telegram(f"❌ Download failed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n\nFailed tracks:\n{failed_list}")
            logger.warning(f"⚠️  Download completed with {len(failed_tracks)} failed tracks")
        else:
            send_telegram(f"✅ Download successful\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}\n📦 Tracks: {len(tracks_to_download)}/{len(tracks_to_download)}")
            logger.info(f"✅ All tracks downloaded successfully")
        
        logger.info(f"📥 Importing album to Lidarr...")
        if force_lidarr_import(album_path, artist_id, album_id, release_id):
            logger.info(f"✅ Album imported successfully: {artist_name} - {album_title}")
            lidarr_request('command', method='POST', data={'name': 'RefreshArtist', 'artistId': artist_id})
            return {'success': True}
        else:
            logger.error(f"❌ Import failed for album: {artist_name} - {album_title}")
            return {'error': 'Import failed'}
            
    except Exception as e:
        logger.error(f"❌ Error during album download: {str(e)}")
        artist_name = download_process.get('artist_name', 'Unknown')
        album_title = download_process.get('album_title', 'Unknown')
        send_telegram(f"❌ Download failed\n🎵 Album: {album_title}\n🎤 Artist: {artist_name}")
        return {'error': str(e)}
    finally:
        with queue_lock:
            download_history.append({
                'album_id': download_process.get('album_id'),
                'album_title': download_process.get('album_title', ''),
                'artist_name': download_process.get('artist_name', ''),
                'success': 'error' not in locals() or not locals().get('e'),
                'timestamp': time.time()
            })
        download_process['active'] = False
        download_process['progress'] = {}
        download_process['album_id'] = None
        download_process['album_title'] = ''
        download_process['artist_name'] = ''
        download_process['current_track_title'] = ''

@app.route('/api/test-connection')
def api_test_connection():
    try:
        system = lidarr_request('system/status')
        return jsonify({'status': 'success' if 'version' in system else 'error', 'lidarr_version': system.get('version', 'Unknown')})
    except:
        return jsonify({'status': 'error'})

@app.route('/')
def index(): return render_template('index.html')

@app.route('/downloads')
def downloads(): return render_template('downloads.html')

@app.route('/settings')
def settings(): return render_template('settings.html')

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
    with queue_lock:
        if album_id not in download_queue and download_process.get('album_id') != album_id:
            download_queue.append(album_id)
            return jsonify({'success': True, 'queued': True})
        else:
            return jsonify({'success': False, 'message': 'Already in queue or downloading'})

@app.route('/api/download/stop', methods=['POST'])
def api_download_stop():
    download_process['stop'] = True
    with queue_lock:
        download_queue.clear()
    return jsonify({'success': True})

@app.route('/api/download/status')
def api_download_status(): return jsonify(download_process)

@app.route('/api/version')
def api_version(): return jsonify({'version': VERSION})

@app.route('/api/download/queue', methods=['GET'])
def api_get_queue():
    with queue_lock:
        queue_with_details = []
        for album_id in download_queue:
            album = lidarr_request(f'album/{album_id}')
            if 'error' not in album:
                queue_with_details.append({
                    'id': album_id,
                    'title': album.get('title', ''),
                    'artist': album.get('artist', {}).get('artistName', ''),
                    'cover': next((img['remoteUrl'] for img in album.get('images', []) if img['coverType'] == 'cover'), '')
                })
        return jsonify(queue_with_details)

@app.route('/api/download/queue', methods=['POST'])
def api_add_to_queue():
    album_id = request.json.get('album_id')
    with queue_lock:
        if album_id not in download_queue and download_process.get('album_id') != album_id:
            download_queue.append(album_id)
    return jsonify({'success': True, 'queue_length': len(download_queue)})

@app.route('/api/download/queue/<int:album_id>', methods=['DELETE'])
def api_remove_from_queue(album_id):
    with queue_lock:
        if album_id in download_queue:
            download_queue.remove(album_id)
    return jsonify({'success': True})

@app.route('/api/download/queue/clear', methods=['POST'])
def api_clear_queue():
    with queue_lock:
        download_queue.clear()
    return jsonify({'success': True})

@app.route('/api/download/history')
def api_download_history():
    return jsonify(download_history[-20:])

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

@app.route('/api/xmlmetadata/toggle', methods=['POST'])
def api_xmlmetadata_toggle():
    config = load_config()
    config['xml_metadata_enabled'] = not config.get('xml_metadata_enabled', True)
    save_config(config)
    return jsonify({'enabled': config['xml_metadata_enabled']})

def scheduled_check():
    if download_process['active']: return
    config = load_config()
    albums = get_missing_albums()
    
    if not albums: return
    
    with queue_lock:
        recent_history_ids = [h['album_id'] for h in download_history[-50:] if h.get('success')]
        current_download_id = download_process.get('album_id')
        
        new_albums = [
            album for album in albums 
            if album['id'] not in download_queue 
            and album['id'] not in recent_history_ids
            and album['id'] != current_download_id
            and album.get('missingTrackCount', 0) > 0
        ]
    
    if new_albums:
        if config.get('scheduler_auto_download', True):
            logger.info(f"🤖 Scheduler: Found {len(new_albums)} new missing albums, adding to queue...")
            send_telegram(f"🚀 Scheduler: Adding {len(new_albums)} new missing albums to queue...")
            with queue_lock:
                for album in new_albums:
                    download_queue.append(album['id'])
        else:
            logger.info(f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-Download disabled)")
            send_telegram(f"🔍 Scheduler: Found {len(new_albums)} missing albums (Auto-DL Disabled)")

def run_scheduler():
    while True: schedule.run_pending(); time.sleep(10)

def setup_scheduler():
    config = load_config()
    schedule.clear()
    if config.get('scheduler_enabled'):
        interval = int(config.get('scheduler_interval', 60))
        schedule.every(interval).minutes.do(scheduled_check)

def process_download_queue():
    while True:
        try:
            if not download_process['active'] and download_queue:
                with queue_lock:
                    if download_queue:
                        next_album_id = download_queue.pop(0)
                        threading.Thread(target=process_album_download, args=(next_album_id, False)).start()
        except:
            pass
        time.sleep(2)

if __name__ == '__main__':
    logger.info("🚀 Starting Lidarr YouTube Downloader...")
    logger.info(f"📌 Version: {VERSION}")
    logger.info(f"📂 Download directory: {DOWNLOAD_DIR if DOWNLOAD_DIR else 'Not set (check DOWNLOAD_PATH env)'}")
    setup_scheduler()
    threading.Thread(target=run_scheduler, daemon=True).start()
    threading.Thread(target=process_download_queue, daemon=True).start()
    logger.info("✅ Application started successfully on http://0.0.0.0:5000")
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
