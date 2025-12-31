"""
YouTube Music API Backend
Provides search and streaming capabilities using ytmusicapi and yt-dlp
Downloads audio files locally for reliable playback
"""

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from ytmusicapi import YTMusic
import yt_dlp
import os
import hashlib
import threading
import time

app = Flask(__name__)
CORS(app)  # Enable CORS for all routes

# Initialize YTMusic (no auth - public access only)
ytmusic = YTMusic()

# Audio cache directory
CACHE_DIR = os.path.join(os.path.dirname(__file__), 'audio_cache')
os.makedirs(CACHE_DIR, exist_ok=True)

# Track download status
download_status = {}  # {video_id: 'downloading' | 'ready' | 'error'}

# Base yt-dlp options with enhanced compatibility
BASE_YDL_OPTIONS = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
    'no_check_certificate': True,
    'socket_timeout': 60,
    'retries': 5,
    'skip_unavailable_fragments': True,
    # Use exported cookie file for authentication
    'cookiefile': os.path.join(os.path.dirname(__file__), 'youtube_cookies.txt'),
    # Browser-like headers to avoid bot detection
    'http_headers': {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
    },
    # Allow geo-bypass for restricted content
    'geo_bypass': True,
    'geo_bypass_country': 'US',
    # Audio-only format selection
    'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best',
    'outtmpl': os.path.join(CACHE_DIR, '%(id)s.%(ext)s'),
}

# YouTube extraction strategies (try in order - Default works best)
YOUTUBE_STRATEGIES = [
    ('Default', {}),
    ('Web Client', {'extractor_args': {'youtube': {'player_client': ['web']}}}),
    ('Android Client', {'extractor_args': {'youtube': {'player_client': ['android']}}}),
    ('iOS Client', {'extractor_args': {'youtube': {'player_client': ['ios']}}}),
    ('TV Embedded', {'extractor_args': {'youtube': {'player_client': ['tv_embedded']}}}),
]

def get_cached_audio_path(video_id):
    """Get the path to cached audio file if it exists"""
    # Check for common audio formats
    for ext in ['m4a', 'mp3', 'webm', 'opus', 'ogg']:
        path = os.path.join(CACHE_DIR, f'{video_id}.{ext}')
        if os.path.exists(path):
            return path
    return None

def download_audio(video_id):
    """Download audio for a video ID using yt-dlp with multiple strategies"""
    download_status[video_id] = 'downloading'
    url = f"https://music.youtube.com/watch?v={video_id}"
    
    last_error = None
    
    for strategy_name, strategy_opts in YOUTUBE_STRATEGIES:
        try:
            print(f"🎵 Trying strategy: {strategy_name} for {video_id}")
            
            # Merge base options with strategy options
            opts = BASE_YDL_OPTIONS.copy()
            opts.update(strategy_opts)
            
            with yt_dlp.YoutubeDL(opts) as ydl:
                ydl.download([url])
            
            # Verify download
            cached_path = get_cached_audio_path(video_id)
            if cached_path:
                download_status[video_id] = 'ready'
                print(f"✅ Downloaded with {strategy_name}: {video_id} -> {cached_path}")
                return cached_path
                
        except Exception as e:
            last_error = str(e)
            print(f"❌ Strategy '{strategy_name}' failed: {str(e)[:100]}")
            continue
    
    # All strategies failed
    print(f"❌ All strategies failed for {video_id}: {last_error}")
    download_status[video_id] = 'error'
    return None

def start_download_async(video_id):
    """Start download in background thread"""
    if video_id in download_status and download_status[video_id] == 'downloading':
        return  # Already downloading
    thread = threading.Thread(target=download_audio, args=(video_id,))
    thread.daemon = True
    thread.start()

def cleanup_old_cache(max_age_hours=24, max_size_mb=500):
    """Clean up old cached files"""
    try:
        total_size = 0
        files = []
        for f in os.listdir(CACHE_DIR):
            path = os.path.join(CACHE_DIR, f)
            if os.path.isfile(path):
                stat = os.stat(path)
                files.append((path, stat.st_mtime, stat.st_size))
                total_size += stat.st_size
        
        # Sort by age (oldest first)
        files.sort(key=lambda x: x[1])
        
        # Remove old files
        now = time.time()
        for path, mtime, size in files:
            age_hours = (now - mtime) / 3600
            if age_hours > max_age_hours or total_size > max_size_mb * 1024 * 1024:
                os.remove(path)
                total_size -= size
                print(f"🗑️ Cleaned: {os.path.basename(path)}")
    except Exception as e:
        print(f"Cache cleanup error: {e}")

def format_song(item):
    """Format YTMusic result to match our app's song structure"""
    if not item:
        return None
    
    video_id = item.get('videoId')
    if not video_id:
        return None
    
    # Get high-resolution thumbnail using YouTube's standard URL format
    # maxresdefault (1280x720), hqdefault (480x360), mqdefault (320x180)
    thumbnail = f'https://i.ytimg.com/vi/{video_id}/maxresdefault.jpg'
    
    # Get artist info
    artists = item.get('artists', [])
    artist_name = artists[0].get('name') if artists else 'Unknown Artist'
    artist_id = artists[0].get('id') if artists else None
    
    # Get album info
    album = item.get('album', {}) or {}
    album_name = album.get('name', '') if isinstance(album, dict) else str(album)
    
    # Duration in seconds
    duration_str = item.get('duration', '0:00')
    duration_parts = duration_str.split(':')
    try:
        if len(duration_parts) == 2:
            duration = int(duration_parts[0]) * 60 + int(duration_parts[1])
        elif len(duration_parts) == 3:
            duration = int(duration_parts[0]) * 3600 + int(duration_parts[1]) * 60 + int(duration_parts[2])
        else:
            duration = 0
    except:
        duration = 0
    
    return {
        'id': f'ytm_{video_id}',
        'videoId': video_id,
        'name': item.get('title', 'Unknown'),
        'duration': duration,
        'album': {'id': album.get('id', ''), 'name': album_name},
        'artists': {'primary': [{'id': artist_id, 'name': artist_name}]},
        'image': [{'quality': '500x500', 'url': thumbnail}],
        '_source': 'ytmusic'
    }

@app.route('/api/ytmusic/search', methods=['GET'])
def search():
    """Search for songs on YouTube Music"""
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 20))
    filter_type = request.args.get('filter', 'songs')  # songs, albums, artists
    
    if not query:
        return jsonify({'status': 400, 'error': 'Query parameter q is required'}), 400
    
    try:
        results = ytmusic.search(query, filter=filter_type, limit=limit)
        formatted = [format_song(item) for item in results if item.get('videoId')]
        formatted = [s for s in formatted if s]  # Remove None values
        
        return jsonify({
            'status': 200,
            'data': {'results': formatted},
            'message': 'success'
        })
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/stream/<video_id>', methods=['GET'])
def get_stream(video_id):
    """Check status and get info for a video ID"""
    try:
        # Check if already cached
        cached_path = get_cached_audio_path(video_id)
        if cached_path:
            return jsonify({
                'status': 200,
                'data': {
                    'ready': True,
                    'url': f'/api/ytmusic/audio/{video_id}'
                },
                'message': 'success'
            })
        
        # Check download status
        status = download_status.get(video_id, 'not_started')
        
        if status == 'downloading':
            return jsonify({
                'status': 200,
                'data': {'ready': False, 'status': 'downloading'},
                'message': 'Download in progress'
            })
        elif status == 'error':
            return jsonify({'status': 500, 'error': 'Download failed'}), 500
        else:
            # Start download
            start_download_async(video_id)
            return jsonify({
                'status': 200,
                'data': {'ready': False, 'status': 'started'},
                'message': 'Download started'
            })
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/audio/<video_id>', methods=['GET'])
def serve_audio(video_id):
    """Serve the downloaded audio file"""
    try:
        cached_path = get_cached_audio_path(video_id)
        if cached_path:
            # Determine mime type
            ext = os.path.splitext(cached_path)[1].lower()
            mime_types = {
                '.mp3': 'audio/mpeg',
                '.webm': 'audio/webm',
                '.m4a': 'audio/mp4',
                '.opus': 'audio/opus',
                '.ogg': 'audio/ogg'
            }
            mime_type = mime_types.get(ext, 'audio/mpeg')
            
            return send_file(
                cached_path,
                mimetype=mime_type,
                as_attachment=False,
                conditional=True  # Enable range requests
            )
        else:
            # Not cached, start download
            start_download_async(video_id)
            return jsonify({'status': 202, 'message': 'Download started, try again shortly'}), 202
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/proxy/<video_id>', methods=['GET'])
def proxy_stream(video_id):
    """Proxy endpoint - redirects to audio endpoint for compatibility"""
    cached_path = get_cached_audio_path(video_id)
    if cached_path:
        return serve_audio(video_id)
    else:
        # Start download and return status
        start_download_async(video_id)
        return jsonify({
            'status': 202,
            'data': {'ready': False, 'status': 'downloading'},
            'message': 'Download started - poll /api/ytmusic/stream/{video_id} for status'
        }), 202

@app.route('/api/ytmusic/song/<video_id>', methods=['GET'])
def get_song(video_id):
    """Get song details by video ID"""
    try:
        info = ytmusic.get_song(video_id)
        if info:
            # Format the response
            video_details = info.get('videoDetails', {})
            return jsonify({
                'status': 200,
                'data': {
                    'id': f'ytm_{video_id}',
                    'videoId': video_id,
                    'name': video_details.get('title', 'Unknown'),
                    'duration': int(video_details.get('lengthSeconds', 0)),
                    'artists': {'primary': [{'name': video_details.get('author', 'Unknown')}]},
                    'image': [{'url': video_details.get('thumbnail', {}).get('thumbnails', [{}])[-1].get('url', '')}],
                    '_source': 'ytmusic'
                },
                'message': 'success'
            })
        return jsonify({'status': 404, 'error': 'Song not found'}), 404
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/artist/<artist_id>', methods=['GET'])
def get_artist(artist_id):
    """Get artist info and songs"""
    try:
        artist = ytmusic.get_artist(artist_id)
        if not artist:
            return jsonify({'status': 404, 'error': 'Artist not found'}), 404
        
        # Format top songs
        songs = []
        if artist.get('songs', {}).get('results'):
            for song in artist['songs']['results'][:20]:
                formatted = format_song(song)
                if formatted:
                    songs.append(formatted)
        
        return jsonify({
            'status': 200,
            'data': {
                'id': artist_id,
                'name': artist.get('name', 'Unknown'),
                'description': artist.get('description', ''),
                'thumbnails': artist.get('thumbnails', []),
                'topSongs': songs,
                '_source': 'ytmusic'
            },
            'message': 'success'
        })
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/lyrics/<video_id>', methods=['GET'])
def get_lyrics(video_id):
    """Get lyrics for a song"""
    try:
        watch_playlist = ytmusic.get_watch_playlist(video_id)
        lyrics_id = watch_playlist.get('lyrics')
        
        if lyrics_id:
            lyrics = ytmusic.get_lyrics(lyrics_id)
            if lyrics:
                return jsonify({
                    'status': 200,
                    'data': {'lyrics': lyrics.get('lyrics', '')},
                    'message': 'success'
                })
        
        return jsonify({'status': 404, 'error': 'Lyrics not found'}), 404
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/api/ytmusic/lyrics/search', methods=['GET'])
def search_lyrics():
    """Search for lyrics by song name - works for any song"""
    query = request.args.get('q', '')
    if not query:
        return jsonify({'status': 400, 'error': 'Query parameter q is required'}), 400
    
    try:
        # Search for the song on YouTube Music
        results = ytmusic.search(query, filter='songs', limit=1)
        
        if results and len(results) > 0:
            video_id = results[0].get('videoId')
            if video_id:
                # Get lyrics for this song
                watch_playlist = ytmusic.get_watch_playlist(video_id)
                lyrics_id = watch_playlist.get('lyrics')
                
                if lyrics_id:
                    lyrics = ytmusic.get_lyrics(lyrics_id)
                    if lyrics and lyrics.get('lyrics'):
                        return jsonify({
                            'status': 200,
                            'data': {
                                'lyrics': lyrics.get('lyrics', ''),
                                'videoId': video_id,
                                'title': results[0].get('title', '')
                            },
                            'message': 'success'
                        })
        
        return jsonify({'status': 404, 'error': 'Lyrics not found'}), 404
    except Exception as e:
        return jsonify({'status': 500, 'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({'status': 'ok', 'service': 'ytmusic-api'})

if __name__ == '__main__':
    print("🎵 YouTube Music API Server starting on port 3000...")
    print("📡 Endpoints:")
    print("   GET /api/ytmusic/search?q=<query>&limit=20")
    print("   GET /api/ytmusic/stream/<videoId>")
    print("   GET /api/ytmusic/song/<videoId>")
    print("   GET /api/ytmusic/artist/<artistId>")
    print("   GET /api/ytmusic/lyrics/<videoId>")
    print("   GET /health")
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False)
