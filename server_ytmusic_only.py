"""
YouTube Music API Server - Powered by yt-dlp with multi-strategy extraction
"""
from flask import Flask, jsonify, send_file, request
from flask_cors import CORS
import os
import asyncio
from ytdlp_handler import YTDLPHandler
from ytmusicapi import YTMusic

app = Flask(__name__)
CORS(app)

# Initialize handlers
ytmusic = YTMusic()
ytdlp_handler = YTDLPHandler(download_dir=os.path.join(os.path.dirname(__file__), 'cache'))

print("🎵 YouTube Music API Server starting on port 3000...")
print("📡 Endpoints:")
print("   GET /api/ytmusic/search?q=<query>&limit=20")
print("   GET /api/ytmusic/stream/<videoId>")
print("   GET /api/ytmusic/song/<videoId>")
print("   GET /api/ytmusic/artist/<artistId>")
print("   GET /api/ytmusic/lyrics/<videoId>")
print("   GET /health")


@app.route('/health')
def health():
    """Health check endpoint"""
    return jsonify({"status": "healthy", "service": "YouTube Music API"})


@app.route('/api/ytmusic/search')
def search():
    """Search for songs on YouTube Music"""
    query = request.args.get('q', '')
    limit = int(request.args.get('limit', 20))
    
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    
    try:
        results = ytmusic.search(query, filter='songs', limit=limit)
        return jsonify({"success": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ytmusic/stream/<video_id>')
def stream_audio(video_id):
    """
    Stream audio for a video ID using multi-strategy extraction.
    Uses task-based download system with caching.
    """
    try:
        # Check cache for existing download
        cache_dir = ytdlp_handler.download_dir
        cached_file = None
        
        # Look for cached file
        for ext in ['m4a', 'mp3', 'webm', 'opus']:
            potential_file = os.path.join(cache_dir, f"{video_id}.{ext}")
            if os.path.exists(potential_file):
                cached_file = potential_file
                break
        
        # Also check in active tasks
        if not cached_file:
            for task in ytdlp_handler.active_tasks.values():
                if task.url and video_id in task.url and task.status.value == 'completed':
                    if task.filepath and os.path.exists(task.filepath):
                        cached_file = task.filepath
                        break
        
        # If not cached, download it
        if not cached_file:
            print(f"🎵 Downloading audio for {video_id}...")
            url = f"https://music.youtube.com/watch?v={video_id}"
            
            # Run async download in sync context
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.run_until_complete(
                ytdlp_handler.download_video(
                    url=url,
                    audio_only=True,
                    format_id='best'
                )
            )
            loop.close()
            
            # Check if download succeeded
            if task.status.value == 'failed':
                print(f"❌ Download failed: {task.error}")
                return jsonify({"error": task.error or "Download failed"}), 500
            
            if not task.filepath or not os.path.exists(task.filepath):
                return jsonify({"error": "Audio file not found"}), 404
            
            cached_file = task.filepath
        else:
            print(f"✅ Using cached audio for {video_id}")
        
        # Stream the file
        return send_file(
            cached_file,
            mimetype='audio/mp4',
            as_attachment=False,
            download_name=f"{video_id}.m4a"
        )
        
    except Exception as e:
        print(f"❌ Stream error for {video_id}: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@app.route('/api/ytmusic/song/<video_id>')
def get_song(video_id):
    """Get detailed song information"""
    try:
        song = ytmusic.get_song(video_id)
        return jsonify({"success": True, "data": song})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ytmusic/artist/<artist_id>')
def get_artist(artist_id):
    """Get artist information"""
    try:
        artist = ytmusic.get_artist(artist_id)
        return jsonify({"success": True, "data": artist})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ytmusic/lyrics/<video_id>')
def get_lyrics(video_id):
    """Get lyrics for a song"""
    try:
        # Get song info first to find lyrics browse ID
        song = ytmusic.get_song(video_id)
        lyrics_browse_id = song.get('videoDetails', {}).get('musicVideoType')
        
        if not lyrics_browse_id:
            return jsonify({"error": "No lyrics available"}), 404
        
        lyrics = ytmusic.get_lyrics(lyrics_browse_id)
        return jsonify({"success": True, "lyrics": lyrics})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/ytmusic/lyrics/search')
def search_lyrics():
    """Search for lyrics by song name"""
    query = request.args.get('q', '')
    
    if not query:
        return jsonify({"error": "Query parameter 'q' is required"}), 400
    
    try:
        # Search for the song
        results = ytmusic.search(query, filter='songs', limit=1)
        
        if not results:
            return jsonify({"error": "Song not found"}), 404
        
        video_id = results[0].get('videoId')
        
        # Get lyrics
        song = ytmusic.get_song(video_id)
        lyrics_browse_id = song.get('videoDetails', {}).get('musicVideoType')
        
        if not lyrics_browse_id:
            return jsonify({"error": "No lyrics available"}), 404
        
        lyrics = ytmusic.get_lyrics(lyrics_browse_id)
        return jsonify({
            "success": True,
            "song": results[0],
            "lyrics": lyrics
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 3000))
    app.run(host='0.0.0.0', port=port, debug=False)
