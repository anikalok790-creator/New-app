import os
import yt_dlp
from flask import Flask, render_template, request, jsonify, send_file, abort
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
import pathlib
import uuid
import threading
import time
import re
import json
import urllib.parse
import imageio_ffmpeg

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=["120 per minute"],
    storage_uri="memory://",
)

APP_SECRET_TOKEN = "SnapPro-Secure-X9F2K1-2026"

DOWNLOAD_DIR = pathlib.Path("downloads")
DOWNLOAD_DIR.mkdir(exist_ok=True)

HISTORY_FILE = pathlib.Path("history.json")

tasks = {}
download_history = []
history_lock = threading.Lock()

AUDIO_EXTS = {'.m4a', '.mp3', '.webm', '.opus', '.ogg', '.aac', '.flac', '.wav'}
VIDEO_EXTS = {'.mp4', '.mkv', '.webm', '.mov', '.avi'}
MEDIA_EXTS = AUDIO_EXTS | VIDEO_EXTS

def verify_token():
    token = request.headers.get('X-App-Token')
    if token != APP_SECRET_TOKEN:
        abort(403)

def save_history_to_file():
    try:
        with history_lock:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(download_history, f, ensure_ascii=False, indent=4)
    except Exception as e:
        print(f"Error saving history: {e}")

def load_history_from_file():
    global download_history
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                download_history = json.load(f)
        except Exception as e:
            print(f"Error loading history: {e}")
            download_history = []

load_history_from_file()

def get_safe_filename(title):
    if not title:
        return "SnapPro_Video"
    title = re.sub(r'[\r\n\t]+', ' ', title)
    title = re.sub(r'[\\/*?:"<>|]', '', title)
    while len(title.encode('utf-8')) > 80:
        title = title[:-1]
    return title.strip() or "SnapPro_Video"

def sanitize_basename(name):
    if not name:
        return ""
    return os.path.basename(str(name)).strip()

@app.route('/')
def index():
    return render_template('index.html', token=APP_SECRET_TOKEN)

@app.route('/api/info', methods=['POST'])
@limiter.limit("20 per minute")
def get_info():
    verify_token()
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url or not re.match(r'^https?://', url):
        return jsonify({"error": "Invalid URL"}), 400

    ydl_opts = {'quiet': True, 'no_warnings': True, 'nocheckcertificate': True}

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = []
            seen_res = set()

            for f in info.get('formats', []):
                height = f.get('height')
                vcodec = f.get('vcodec')

                if height and vcodec != 'none':
                    res_label = f"{height}p"
                    if res_label not in seen_res:
                        seen_res.add(res_label)
                        filesize = f.get('filesize') or f.get('filesize_approx', 0)
                        formats.append({
                            'resolution': res_label,
                            'height': height,
                            'ext': f.get('ext', 'mp4'),
                            'filesize': filesize
                        })

            formats.sort(key=lambda x: x['height'], reverse=True)
            view_count = info.get('view_count', 0)
            views_str = f"{view_count/1000000:.1f}M views" if view_count >= 1000000 else (f"{view_count/1000:.1f}K views" if view_count >= 1000 else f"{view_count} views")

            return jsonify({
                "title": info.get('title', 'Unknown Video'),
                "thumbnail": info.get('thumbnail', ''),
                "duration": info.get('duration_string', ''),
                "formats": formats,
                "uploader": info.get('uploader', 'Unknown Channel'),
                "views": views_str,
                "description": info.get('description', 'No description available.')
            })
    except Exception as e:
        return jsonify({"error": f"Error: {str(e)}"}), 500

def download_worker(task_id, url, height, video_title, video_thumb, uploader, views, description, save_thumb):
    outtmpl = str(DOWNLOAD_DIR / f"{task_id}_%(title).100B.%(ext)s")

    def progress_hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            downloaded = d.get('downloaded_bytes', 0)
            percent = (downloaded / total * 100) if total > 0 else 0
            tasks[task_id].update({
                'status': 'downloading',
                'percent': percent,
                'downloaded': downloaded,
                'total': total,
                'speed': d.get('speed', 0),
                'eta': d.get('eta', 0)
            })
        elif d['status'] == 'finished':
            tasks[task_id]['status'] = 'merging'
            tasks[task_id]['percent'] = 100

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        ffmpeg_exe = None

    if height == 'audio':
        ydl_opts = {'format': 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio', 'outtmpl': outtmpl, 'quiet': True, 'no_warnings': True, 'progress_hooks': [progress_hook]}
    else:
        # 16K/8K ডাউনলোড নিশ্চিত করার জন্য ফরম্যাট ফলব্যাক
        ydl_opts = {
            'format': f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best',
            'outtmpl': outtmpl,
            'quiet': True,
            'no_warnings': True,
            'merge_output_format': 'mp4',
            'progress_hooks': [progress_hook],
            'nocheckcertificate': True
        }

    if save_thumb: ydl_opts['writethumbnail'] = True
    if ffmpeg_exe and os.path.exists(ffmpeg_exe): ydl_opts['ffmpeg_location'] = ffmpeg_exe

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            candidates = [f for f in DOWNLOAD_DIR.glob(f"{task_id}_*") if f.is_file() and f.suffix.lower() in MEDIA_EXTS]
            candidates.sort(key=lambda p: p.stat().st_size, reverse=True)

            if not candidates: raise Exception("Download failed")

            filename = str(candidates[0])
            tasks[task_id]['status'] = 'done'
            tasks[task_id]['filename'] = filename

            with history_lock:
                download_history.append({
                    'task_id': task_id, 'title': video_title, 'filename': os.path.basename(filename),
                    'thumbnail': video_thumb, 'resolution': 'Audio' if height == 'audio' else f"{height}p",
                    'time': time.strftime("%Y-%m-%d %H:%M"), 'uploader': uploader, 'views': views, 'description': description
                })
            save_history_to_file()
    except Exception as e:
        tasks[task_id]['status'] = 'error'
        tasks[task_id]['error'] = str(e)

@app.route('/api/start-download', methods=['POST'])
@limiter.limit("10 per minute")
def start_download():
    verify_token()
    data = request.json or {}
    url = (data.get('url') or '').strip()
    if not url: return jsonify({"error": "URL Required"}), 400

    task_id = str(uuid.uuid4())[:8]
    tasks[task_id] = {'status': 'starting', 'percent': 0, 'filename': ''}

    thread = threading.Thread(target=download_worker, args=(task_id, url, data.get('height'), data.get('title', 'Video'), data.get('thumb', ''), data.get('uploader', 'Channel'), data.get('views', '0 views'), data.get('description', ''), data.get('save_thumb', False)))
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id})

@app.route('/api/progress/<task_id>', methods=['GET'])
def get_progress(task_id):
    verify_token()
    if task_id not in tasks: return jsonify({"error": "Not found"}), 404
    task = tasks[task_id]
    return jsonify({'status': task['status'], 'percent': round(task['percent'], 2), 'speed': round((task.get('speed', 0) or 0) / (1024 * 1024), 2), 'downloaded_mb': round((task.get('downloaded', 0) or 0) / (1024 * 1024), 2), 'total_mb': round((task.get('total', 0) or 0) / (1024 * 1024), 2), 'eta': task.get('eta', 0), 'error': task.get('error', '')})

@app.route('/api/local-videos', methods=['GET'])
def get_local_videos():
    verify_token()
    local_videos = []
    history_map = {item['filename']: item for item in download_history}
    files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS and f.stat().st_size > 1000]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    for fpath in files:
        meta = history_map.get(fpath.name, {})
        stem = fpath.stem
        parts = stem.split('_', 1)
        embedded_task_id = parts[0] if len(parts) > 1 and len(parts[0]) == 8 else ''
        fallback_title = parts[1] if len(parts) > 1 else stem
        is_audio = fpath.suffix.lower() in AUDIO_EXTS and meta.get('resolution') == 'Audio'

        local_videos.append({
            'task_id': meta.get('task_id', embedded_task_id), 'title': meta.get('title') or fallback_title,
            'filename': fpath.name, 'thumbnail': meta.get('thumbnail', ''), 'uploader': meta.get('uploader', 'Unknown'),
            'views': meta.get('views', ''), 'resolution': meta.get('resolution', 'Audio' if is_audio else 'Local'),
            'description': meta.get('description', ''), 'time': meta.get('time') or time.strftime('%Y-%m-%d %H:%M', time.localtime(fpath.stat().st_mtime)),
            'url': f"/api/stream/{urllib.parse.quote(fpath.name)}", 'timestamp': fpath.stat().st_mtime
        })
    return jsonify(local_videos)

@app.route('/api/history', methods=['GET'])
def get_history():
    verify_token()
    return jsonify(download_history[::-1])

@app.route('/api/clear-history', methods=['POST'])
def clear_history():
    verify_token()
    with history_lock: download_history.clear()
    save_history_to_file()
    return jsonify({"status": "success"})

@app.route('/api/delete-video', methods=['POST'])
def delete_video():
    verify_token()
    data = request.json or {}
    filename = sanitize_basename(data.get('filename'))
    if not filename: return jsonify({"error": "Filename required"}), 400

    file_path = DOWNLOAD_DIR / filename
    if os.path.exists(file_path):
        try:
            os.remove(file_path)
            global download_history
            with history_lock: download_history = [item for item in download_history if item['filename'] != filename]
            save_history_to_file()
            return jsonify({"status": "success"})
        except Exception as e: return jsonify({"error": str(e)}), 500
    return jsonify({"error": "File not found"}), 404

@app.route('/api/stream/<path:filename>', methods=['GET'])
def stream_video(filename):
    safe_name = sanitize_basename(filename)
    if not safe_name:
        return jsonify({"error": "Invalid filename"}), 400
    file_path = DOWNLOAD_DIR / safe_name
    if os.path.exists(file_path): return send_file(file_path, conditional=True)
    return jsonify({"error": "File not found"}), 404

@app.route('/api/download-history-file/<task_id>', methods=['GET'])
def download_history_file(task_id):
    for item in download_history:
        if item['task_id'] == task_id:
            file_path = DOWNLOAD_DIR / sanitize_basename(item['filename'])
            if os.path.exists(file_path):
                ext = item['filename'].split('.')[-1]
                safe_title = get_safe_filename(item['title'])
                return send_file(file_path, as_attachment=True, download_name=f"[{item['resolution']}] {safe_title}.{ext}")
    return jsonify({"error": "File not found"}), 404

@app.route('/api/get-file/<task_id>', methods=['GET'])
def get_file(task_id):
    if task_id not in tasks or tasks[task_id]['status'] != 'done': return jsonify({"error": "Not ready"}), 400
    filename = tasks[task_id]['filename']
    file_path = pathlib.Path(filename).resolve()
    
    if not str(file_path).startswith(str(DOWNLOAD_DIR.resolve())) or not file_path.exists():
        return jsonify({"error": "File not found"}), 404

    ext = file_path.suffix.lstrip('.')
    video_title = "SnapPro_Video"
    resolution = "Video"
    for item in download_history:
        if item['task_id'] == task_id:
            video_title = item['title']
            resolution = item['resolution']
            break
    
    # WebToApp (WebView) এ ডাউনলোড কাজ করার জন্য conditional=True
    return send_file(file_path, as_attachment=True, conditional=True, download_name=f"[{resolution}] {get_safe_filename(video_title)}.{ext}")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
