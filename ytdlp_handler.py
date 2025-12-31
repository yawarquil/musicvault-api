"""
yt-dlp Handler - Core video extraction and download engine for VidGrabber
"""
import asyncio
import os
import tempfile
import uuid
import re
from typing import Any, Callable, Dict, List, Optional
from dataclasses import dataclass, field
from enum import Enum
import yt_dlp


# Helper functions
def sanitize_filename(filename):
    """Remove invalid characters from filename"""
    return re.sub(r'[<>:"/\\|?*]', '', filename)


def get_friendly_error(error_msg):
    """Convert technical error to user-friendly message"""
    if 'Sign in' in error_msg or 'bot' in error_msg:
        return "YouTube requires authentication. Please check cookie file."
    if 'format' in error_msg.lower():
        return "Requested format not available. Trying alternative..."
    return error_msg



class DownloadStatus(str, Enum):
    PENDING = "pending"
    EXTRACTING = "extracting"
    DOWNLOADING = "downloading"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class VideoFormat:
    """Represents an available video format"""
    format_id: str
    ext: str
    resolution: Optional[str] = None
    filesize: Optional[int] = None
    filesize_approx: Optional[int] = None
    vcodec: Optional[str] = None
    acodec: Optional[str] = None
    fps: Optional[float] = None
    tbr: Optional[float] = None  # Total bitrate
    quality_label: str = ""
    is_audio_only: bool = False
    is_video_only: bool = False
    
    @property
    def size_str(self) -> str:
        size = self.filesize or self.filesize_approx
        if not size:
            return "Unknown size"
        
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"


@dataclass
class VideoInfo:
    """Represents extracted video metadata"""
    id: str
    title: str
    url: str
    thumbnail: Optional[str] = None
    duration: Optional[int] = None
    uploader: Optional[str] = None
    uploader_url: Optional[str] = None
    view_count: Optional[int] = None
    like_count: Optional[int] = None
    description: Optional[str] = None
    upload_date: Optional[str] = None
    extractor: Optional[str] = None
    webpage_url: Optional[str] = None
    formats: List[VideoFormat] = field(default_factory=list)
    subtitles: Dict[str, Any] = field(default_factory=dict)
    is_live: bool = False
    
    @property
    def duration_str(self) -> str:
        if not self.duration:
            return "Unknown"
        
        # Convert to int in case duration is a float
        duration_int = int(self.duration)
        hours, remainder = divmod(duration_int, 3600)
        minutes, seconds = divmod(remainder, 60)
        
        if hours:
            return f"{hours}:{minutes:02d}:{seconds:02d}"
        return f"{minutes}:{seconds:02d}"
    
    @property
    def view_count_str(self) -> str:
        if not self.view_count:
            return "Unknown"
        
        if self.view_count >= 1_000_000:
            return f"{self.view_count / 1_000_000:.1f}M views"
        if self.view_count >= 1_000:
            return f"{self.view_count / 1_000:.1f}K views"
        return f"{self.view_count} views"


@dataclass
class DownloadTask:
    """Represents a download task"""
    task_id: str
    url: str
    status: DownloadStatus = DownloadStatus.PENDING
    progress: float = 0.0
    speed: Optional[str] = None
    eta: Optional[str] = None
    filename: Optional[str] = None
    filepath: Optional[str] = None
    filesize: Optional[int] = None
    error: Optional[str] = None
    video_info: Optional[VideoInfo] = None


class YTDLPHandler:
    """
    Wrapper class for yt-dlp operations with async support
    """
    
    def __init__(self, download_dir: Optional[str] = None):
        self.download_dir = download_dir or tempfile.mkdtemp(prefix="vidgrabber_")
        self.active_tasks: Dict[str, DownloadTask] = {}
        self._progress_callbacks: Dict[str, Callable] = {}
        
        # Base yt-dlp options with enhanced compatibility
        self.base_opts = {
            'quiet': True,
            'no_warnings': True,
            'extract_flat': False,
            'no_check_certificate': True,
            # Cookie file for YouTube authentication
            'cookiefile': os.path.join(os.path.dirname(__file__), 'youtube_cookies.txt'),
            # Increased timeout for slow platforms
            'socket_timeout': 60,
            'retries': 5,
            # Skip unavailable fragments for live streams
            'skip_unavailable_fragments': True,
            # Browser-like headers to avoid bot detection
            'http_headers': {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.9',
            },
            # Platform-specific extractor arguments - REMOVED YouTube player_client to get all formats
            'extractor_args': {
                'instagram': {
                    'api': ['graphql'],
                },
                'tiktok': {
                    'api_hostname': ['api22-normal-c-useast2a.tiktokv.com'],
                },
                'twitter': {
                    'legacy_api': ['true'],
                },
            },
            # Allow geo-bypass for restricted content
            'geo_bypass': True,
            'geo_bypass_country': 'US',
        }
    
    def _get_ydl_opts(self, **kwargs) -> dict:
        """Get yt-dlp options with custom overrides"""
        opts = self.base_opts.copy()
        opts.update(kwargs)
        return opts
    
    async def extract_info(self, url: str) -> VideoInfo:
        """
        Extract video metadata without downloading.
        """
        # Define fallback extraction strategies
        extraction_strategies = self._get_extraction_strategies(url)
        
        last_error = None
        
        for strategy_name, strategy_opts in extraction_strategies:
            opts = self._get_ydl_opts(
                skip_download=True,
                extract_flat=False,
                noplaylist=True,
                **strategy_opts
            )
            
            def _extract():
                with yt_dlp.YoutubeDL(opts) as ydl:
                    return ydl.extract_info(url, download=False)
            
            try:
                loop = asyncio.get_event_loop()
                info = await loop.run_in_executor(None, _extract)
                if info:
                    print(f"✓ Extraction successful with strategy: {strategy_name}")
                    return self._parse_video_info(info, url)
            except Exception as e:
                last_error = str(e)
                print(f"✗ Strategy '{strategy_name}' failed: {str(e)[:100]}")
                continue
        
        # Try fallback downloaders (you-get, streamlink, gallery-dl)
        print(f"⚠️ All yt-dlp strategies failed, trying alternative downloaders...")
        try:
            from .fallback_downloaders import try_fallback_extractors
            fallback_result = await try_fallback_extractors(url)
            if fallback_result.success:
                print(f"✅ Fallback extractor succeeded: {fallback_result.data.get('extractor')}")
                # Note: Fallback extractors don't provide full VideoInfo format
                # You'll need to implement conversion logic here based on your needs
                raise Exception(f"Alternative extractor {fallback_result.data.get('extractor')} succeeded but format conversion not implemented yet")
        except Exception as e:
            print(f"✗ Fallback downloaders also failed: {str(e)}")
        
        # All strategies failed
        raise Exception(get_friendly_error(last_error or "All extraction methods failed"))
    
    def _get_extraction_strategies(self, url: str) -> list:
        """
        Get list of extraction strategies to try based on the URL.
        Each strategy is a tuple of (name, options_dict).
        """
        strategies = []
        url_lower = url.lower()
        
        if 'youtube.com' in url_lower or 'youtu.be' in url_lower:
            # YouTube strategies for cloud servers
            # Use multiple player clients to bypass sign-in requirements
            strategies = [
                ('YouTube Web Client', {
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['web'],
                        }
                    }
                }),
                ('YouTube Android Client', {
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['android'],
                        }
                    }
                }),
                ('YouTube iOS Client', {
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['ios'],
                        }
                    }
                }),
                ('YouTube TV Embedded', {
                    'extractor_args': {
                        'youtube': {
                            'player_client': ['tv_embedded'],
                        }
                    }
                }),
                ('YouTube Default', {}),
            ]
        elif 'instagram.com' in url_lower or 'instagr.am' in url_lower:
            # Instagram strategies
            strategies = [
                ('Instagram GraphQL', {
                    'extractor_args': {
                        'instagram': {'api': ['graphql']}
                    }
                }),
                ('Instagram Default', {}),
                ('Instagram with Cookies', {
                    'cookiesfrombrowser': ('chrome',),
                }),
            ]
        elif 'tiktok.com' in url_lower:
            # TikTok strategies
            strategies = [
                ('TikTok API', {
                    'extractor_args': {
                        'tiktok': {'api_hostname': ['api22-normal-c-useast2a.tiktokv.com']}
                    }
                }),
                ('TikTok Default', {}),
            ]
        elif 'twitter.com' in url_lower or 'x.com' in url_lower:
            # Twitter/X strategies
            strategies = [
                ('Twitter Legacy API', {
                    'extractor_args': {
                        'twitter': {'legacy_api': ['true']}
                    }
                }),
                ('Twitter Default', {}),
            ]
        else:
            # Generic strategies for other sites
            strategies = [
                ('Default', {}),
                ('No Browser Headers', {
                    'http_headers': {}
                }),
                ('With Geo Bypass', {
                    'geo_bypass': True,
                    'geo_bypass_country': 'US',
                }),
            ]
        
        return strategies
    
    def _parse_video_info(self, info: dict, original_url: str) -> VideoInfo:
        """Parse yt-dlp info dict into VideoInfo dataclass"""
        
        # Parse formats
        formats = []
        raw_formats = info.get('formats', [])
        
        print(f"📊 Total raw formats from yt-dlp: {len(raw_formats)}")
        
        for fmt in raw_formats:
            # Skip formats without proper identifiers
            if not fmt.get('format_id'):
                continue
            
            # Skip storyboard/thumbnail formats
            format_note = fmt.get('format_note', '').lower()
            if 'storyboard' in format_note:
                continue
            
            # Skip non-video/audio formats (like mhtml)
            ext = fmt.get('ext', '').lower()
            if ext in ['mhtml', 'html', 'json', 'none']:
                continue
            
            vcodec = fmt.get('vcodec', 'none')
            acodec = fmt.get('acodec', 'none')
            
            # Skip if neither video nor audio codec
            if vcodec == 'none' and acodec == 'none':
                continue
            
            is_video_only = vcodec != 'none' and acodec == 'none'
            is_audio_only = vcodec == 'none' and acodec != 'none'
            
            # Skip very low resolution videos (likely thumbnails)
            height = fmt.get('height')
            if height and not is_audio_only:
                height = int(height)
                if height < 144:  # Skip anything below 144p
                    continue
            
            # Build quality label
            if height:
                if height >= 2160:
                    quality_label = "4K"
                elif height >= 1440:
                    quality_label = "2K"
                elif height >= 1080:
                    quality_label = "1080p HD"
                elif height >= 720:
                    quality_label = "720p HD"
                elif height >= 480:
                    quality_label = "480p"
                elif height >= 360:
                    quality_label = "360p"
                elif height >= 240:
                    quality_label = "240p"
                else:
                    quality_label = f"{height}p"
            elif is_audio_only:
                abr = fmt.get('abr', 0)
                quality_label = f"Audio {int(abr)}kbps" if abr else "Audio"
            else:
                quality_label = fmt.get('format_note', 'Unknown')
            
            # Build resolution string
            width = fmt.get('width')
            height_val = fmt.get('height')
            if height_val:
                resolution = f"{int(width) if width else '?'}x{int(height_val)}"
            else:
                resolution = None
            
            formats.append(VideoFormat(
                format_id=fmt.get('format_id', ''),
                ext=fmt.get('ext', 'mp4'),
                resolution=resolution,
                filesize=fmt.get('filesize'),
                filesize_approx=fmt.get('filesize_approx'),
                vcodec=vcodec if vcodec != 'none' else None,
                acodec=acodec if acodec != 'none' else None,
                fps=fmt.get('fps'),
                tbr=fmt.get('tbr'),
                quality_label=quality_label,
                is_audio_only=is_audio_only,
                is_video_only=is_video_only,
            ))
        
        # Sort formats: video first (by resolution), then audio
        def get_resolution_height(f):
            """Safely extract height from resolution string"""
            try:
                if f.resolution and 'x' in f.resolution:
                    return int(float(f.resolution.split('x')[1]))
                return 0
            except (ValueError, IndexError, TypeError):
                return 0
        
        formats.sort(key=lambda f: (
            f.is_audio_only,  # Videos first
            -get_resolution_height(f),  # Higher res first
            -(f.tbr or 0),  # Higher bitrate first
        ))
        
        print(f"✅ Parsed {len(formats)} valid formats (after filtering)")
        if formats:
            print(f"🎬 Top 5 formats: {[(f.quality_label, f.resolution, f.is_video_only) for f in formats[:5]]}")
        
        # Get best thumbnail
        thumbnails = info.get('thumbnails', [])
        thumbnail = None
        if thumbnails:
            # Prefer higher quality thumbnails
            sorted_thumbs = sorted(
                [t for t in thumbnails if t.get('url')],
                key=lambda t: t.get('preference', 0) or t.get('width', 0) or 0,
                reverse=True
            )
            if sorted_thumbs:
                thumbnail = sorted_thumbs[0].get('url')
        
        if not thumbnail:
            thumbnail = info.get('thumbnail')
        
        return VideoInfo(
            id=info.get('id', ''),
            title=info.get('title', 'Unknown Title'),
            url=original_url,
            thumbnail=thumbnail,
            duration=info.get('duration'),
            uploader=info.get('uploader') or info.get('channel'),
            uploader_url=info.get('uploader_url') or info.get('channel_url'),
            view_count=info.get('view_count'),
            like_count=info.get('like_count'),
            description=info.get('description'),
            upload_date=info.get('upload_date'),
            extractor=info.get('extractor'),
            webpage_url=info.get('webpage_url', original_url),
            formats=formats,
            subtitles=info.get('subtitles', {}),
            is_live=info.get('is_live', False),
        )
    
    async def download_video(
        self,
        url: str,
        format_id: str = "best",
        embed_subs: bool = False,
        embed_thumbnail: bool = False,
        audio_only: bool = False,
        progress_callback: Optional[Callable[[DownloadTask], None]] = None,
    ) -> DownloadTask:
        """
        Download a video with the specified options.
        Works without FFmpeg by selecting pre-merged formats.
        """
        task_id = str(uuid.uuid4())
        task = DownloadTask(task_id=task_id, url=url, status=DownloadStatus.EXTRACTING)
        self.active_tasks[task_id] = task
        
        if progress_callback:
            self._progress_callbacks[task_id] = progress_callback
        
        def progress_hook(d):
            if d['status'] == 'downloading':
                task.status = DownloadStatus.DOWNLOADING
                
                # Calculate progress
                downloaded = d.get('downloaded_bytes', 0)
                total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
                
                if total > 0:
                    task.progress = (downloaded / total) * 100
                
                task.speed = d.get('_speed_str', d.get('speed'))
                if task.speed and isinstance(task.speed, (int, float)):
                    task.speed = f"{task.speed / 1024 / 1024:.1f} MB/s"
                
                task.eta = d.get('_eta_str', d.get('eta'))
                if task.eta and isinstance(task.eta, (int, float)):
                    task.eta = f"{int(task.eta)}s"
                
                task.filename = d.get('filename', '').split(os.sep)[-1]
                task.filepath = d.get('filename')
                
            elif d['status'] == 'finished':
                task.status = DownloadStatus.PROCESSING
                task.progress = 100
                task.filepath = d.get('filename')
                task.filename = os.path.basename(d.get('filename', ''))
            
            # Call progress callback
            if task_id in self._progress_callbacks:
                try:
                    self._progress_callbacks[task_id](task)
                except Exception:
                    pass
        
        # Build format string - prefer H.264 (avc1) for browser compatibility
        # Detect if this is Instagram (separate video/audio streams need merging)
        is_instagram = 'instagram.com' in url.lower() or 'instagr.am' in url.lower()
        
        if audio_only:
            # Get best audio format
            format_str = "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best"
        elif format_id and format_id != "best":
            # Check if it's a video-only format - if so, we need to merge with audio
            video_info = self.active_tasks.get(task_id)
            is_video_only = False
            if video_info and video_info.video_info:
                fmt = next((f for f in video_info.video_info.formats if f.format_id == format_id), None)
                if fmt and fmt.is_video_only:
                    is_video_only = True
            
            if is_video_only:
                # Video-only format needs audio merged
                format_str = f"{format_id}+bestaudio[ext=m4a]/{format_id}+bestaudio/{format_id}"
            else:
                # Combined format or audio-only
                format_str = f"{format_id}/best[ext=mp4][vcodec^=avc1]/best[ext=mp4]/best"
        else:
            # Best quality - different strategy for Instagram vs others
            if is_instagram:
                # Instagram: prefer combined formats first, then try merging (requires FFmpeg)
                # Format: best combined > merge separate streams > fallback to any
                format_str = "best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
            else:
                # Other platforms: FORCE H.264 (avc1) codec for browser preview compatibility
                # VP9 (vp9) and AV1 (av01) cause playback issues in browser video players
                format_str = "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]/bestvideo[vcodec^=avc1]+bestaudio/best[ext=mp4][vcodec^=avc1]/best[ext=mp4]/best"
        
        # Build output template
        output_template = os.path.join(
            self.download_dir,
            f"%(title)s_%(id)s.%(ext)s"
        )
        
        # Build postprocessors list
        postprocessors = []
        
        # Add FFmpeg merge postprocessor when merging separate streams
        # This is required for Instagram and other platforms with separate video/audio
        if not audio_only and (is_instagram or '+' in format_str):
            postprocessors.append({
                'key': 'FFmpegVideoConvertor',
                'preferedformat': 'mp4',
            })
        
        # Build options
        opts = self._get_ydl_opts(
            format=format_str,
            outtmpl=output_template,
            progress_hooks=[progress_hook],
            postprocessors=postprocessors,
            # Merge output format for combined downloads
            merge_output_format='mp4' if not audio_only else None,
            # These options help with downloads
            noplaylist=True,
            ignoreerrors=False,
        )
        
        def _download():
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=True)
        
        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, _download)
            
            # Update task with final info
            task.status = DownloadStatus.COMPLETED
            task.video_info = self._parse_video_info(info, url)
            
            # Find the actual downloaded file
            if not task.filepath or not os.path.exists(task.filepath):
                # Try to find the file
                expected_name = sanitize_filename(info.get('title', 'video'))
                for f in os.listdir(self.download_dir):
                    if info.get('id', '') in f:
                        task.filepath = os.path.join(self.download_dir, f)
                        task.filename = f
                        break
            
            if task.filepath and os.path.exists(task.filepath):
                task.filesize = os.path.getsize(task.filepath)
            
        except Exception as e:
            task.status = DownloadStatus.FAILED
            task.error = get_friendly_error(str(e))
        
        finally:
            # Cleanup callback
            if task_id in self._progress_callbacks:
                del self._progress_callbacks[task_id]
        
        return task
    
    def get_task(self, task_id: str) -> Optional[DownloadTask]:
        """Get a download task by ID"""
        return self.active_tasks.get(task_id)
    
    def cancel_task(self, task_id: str) -> bool:
        """Cancel a download task"""
        task = self.active_tasks.get(task_id)
        if task and task.status not in (DownloadStatus.COMPLETED, DownloadStatus.FAILED):
            task.status = DownloadStatus.CANCELLED
            return True
        return False
    
    @staticmethod
    def get_supported_sites() -> List[Dict[str, str]]:
        """Get list of supported sites from yt-dlp"""
        extractors = []
        
        for extractor in yt_dlp.list_extractors():
            name = extractor.IE_NAME
            if name and not name.endswith(':playlist') and name != 'generic':
                extractors.append({
                    'name': name,
                    'description': getattr(extractor, 'IE_DESC', None) or name,
                })
        
        # Sort alphabetically
        extractors.sort(key=lambda x: x['name'].lower())
        
        return extractors
    
    def get_recommended_formats(self, info: VideoInfo) -> List[Dict[str, Any]]:
        """Get recommended format options for user selection - expanded options"""
        recommendations = []
        
        def safe_get_height(f):
            """Safely extract height from resolution string"""
            try:
                if f.resolution and 'x' in f.resolution:
                    return int(float(f.resolution.split('x')[1]))
                return 0
            except (ValueError, IndexError, TypeError):
                return 0
        
        def get_bitrate(f):
            """Get format bitrate"""
            return f.tbr or 0
        
        # Find best video+audio combined formats
        video_formats = [f for f in info.formats if not f.is_audio_only and not f.is_video_only]
        video_only = [f for f in info.formats if f.is_video_only]
        audio_only = [f for f in info.formats if f.is_audio_only]
        
        # Best available quality
        all_video = video_formats + video_only
        if all_video:
            best_video = max(all_video, key=lambda f: (safe_get_height(f), get_bitrate(f)))
            height = safe_get_height(best_video)
            quality_badge = "4K" if height >= 2160 else "2K" if height >= 1440 else "HD" if height >= 720 else "SD"
            recommendations.append({
                'id': best_video.format_id,
                'label': f"Best Quality - {best_video.quality_label}",
                'description': f"{best_video.resolution} • {best_video.ext.upper()} • {best_video.size_str}",
                'type': 'video',
                'badge': quality_badge,
                'height': height,
                'filesize': best_video.filesize or best_video.filesize_approx,
            })
        
        # Quality targets - expanded to include more options
        quality_targets = [2160, 1440, 1080, 720, 480, 360, 240]
        quality_labels = {
            2160: '4K Ultra HD',
            1440: '2K QHD',
            1080: '1080p Full HD',
            720: '720p HD',
            480: '480p SD',
            360: '360p',
            240: '240p Low',
        }
        seen_qualities = set()
        
        for target in quality_targets:
            for fmt in all_video:
                height = safe_get_height(fmt)
                # Allow some tolerance for height matching
                if abs(height - target) <= 20 and target not in seen_qualities:
                    seen_qualities.add(target)
                    recommendations.append({
                        'id': fmt.format_id,
                        'label': quality_labels.get(target, fmt.quality_label),
                        'description': f"{fmt.resolution} • {fmt.ext.upper()} • {fmt.size_str}",
                        'type': 'video',
                        'height': height,
                        'filesize': fmt.filesize or fmt.filesize_approx,
                    })
                    break
        
        # Audio options - show multiple quality levels
        if audio_only:
            # Sort by bitrate
            sorted_audio = sorted(audio_only, key=lambda f: f.tbr or 0, reverse=True)
            
            # Best audio
            if sorted_audio:
                best_audio = sorted_audio[0]
                recommendations.append({
                    'id': best_audio.format_id,
                    'label': "Audio - Best Quality",
                    'description': f"{best_audio.quality_label} • {best_audio.ext.upper()} • {best_audio.size_str}",
                    'type': 'audio',
                    'filesize': best_audio.filesize or best_audio.filesize_approx,
                })
            
            # Medium audio (if available)
            if len(sorted_audio) > 2:
                mid_audio = sorted_audio[len(sorted_audio) // 2]
                recommendations.append({
                    'id': mid_audio.format_id,
                    'label': "Audio - Medium Quality",
                    'description': f"{mid_audio.quality_label} • {mid_audio.ext.upper()} • {mid_audio.size_str}",
                    'type': 'audio',
                    'filesize': mid_audio.filesize or mid_audio.filesize_approx,
                })
        
        return recommendations
    
    def cleanup_old_files(self, max_age_hours: int = 1):
        """Clean up old download files"""
        import time
        
        now = time.time()
        max_age_seconds = max_age_hours * 3600
        
        for filename in os.listdir(self.download_dir):
            filepath = os.path.join(self.download_dir, filename)
            if os.path.isfile(filepath):
                file_age = now - os.path.getmtime(filepath)
                if file_age > max_age_seconds:
                    try:
                        os.remove(filepath)
                    except Exception:
                        pass
