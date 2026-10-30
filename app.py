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
import shutil

app = Flask(__name__)
CORS(app, resources={r"/api/*": {"origins": "*"}})

# Rate Limiting for WebToApp compatibility
limiter = Limiter(get_remote_address, app=app, default_limits=["300 per minute"], storage_uri="memory://")

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

def save_history():
    try:
        with history_lock:
            with open(HISTORY_FILE, 'w', encoding='utf-8') as f:
                json.dump(download_history, f, ensure_ascii=False, indent=4)
    except: pass

def load_history():
    global download_history
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, 'r', encoding='utf-8') as f:
                download_history = json.load(f)
        except: download_history = []

load_history()

def get_safe_filename(title):
    if not title: return "SnapPro_Video"
    title = re.sub(r'[\r\n\t]+', ' ', title)
    title = re.sub(r'[\\/*?:"<>|]', '', title)
    while len(title.encode('utf-8')) > 80: title = title[:-1]
    return title.strip() or "SnapPro_Video"

def sanitize_basename(name):
    return os.path.basename(str(name)).strip() if name else ""

@app.route('/')
def index():
    return render_template('index.html', token=APP_SECRET_TOKEN)

@app.route('/api/info', methods=['POST'])
@limiter.limit("30 per minute")
def get_info():
    verify_token()
    url = (request.json or {}).get('url', '').strip()
    if not url or not re.match(r'^https?://', url): return jsonify({"error": "Invalid URL"}), 400
    
    try:
        with yt_dlp.YoutubeDL({'quiet': True, 'no_warnings': True, 'nocheckcertificate': True}) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = []
            seen = set()
            for f in info.get('formats', []):
                h, v = f.get('height'), f.get('vcodec')
                if h and v != 'none':
                    label = f"{h}p"
                    if label not in seen:
                        seen.add(label)
                        formats.append({'resolution': label, 'height': h, 'ext': f.get('ext', 'mp4'), 'filesize': f.get('filesize') or f.get('filesize_approx', 0)})
            formats.sort(key=lambda x: x['height'], reverse=True)
            vc = info.get('view_count', 0)
            views = f"{vc/1000000:.1f}M views" if vc >= 1000000 else (f"{vc/1000:.1f}K views" if vc >= 1000 else f"{vc} views")
            
            return jsonify({
                "title": info.get('title', 'Unknown'), "thumbnail": info.get('thumbnail', ''),
                "duration": info.get('duration_string', ''), "formats": formats,
                "uploader": info.get('uploader', 'Channel'), "views": views, "description": info.get('description', '')
            })
    except Exception as e: return jsonify({"error": str(e)}), 500

def download_worker(tid, url, height, title, thumb, uploader, views, desc):
    outtmpl = str(DOWNLOAD_DIR / f"{tid}_%(title).100B.%(ext)s")
    
    def hook(d):
        if d['status'] == 'downloading':
            total = d.get('total_bytes') or d.get('total_bytes_estimate', 0)
            dl = d.get('downloaded_bytes', 0)
            tasks[tid].update({'status': 'downloading', 'percent': (dl/total*100) if total>0 else 0,
                               'downloaded': dl, 'total': total, 'speed': d.get('speed', 0), 'eta': d.get('eta', 0)})
        elif d['status'] == 'finished':
            tasks[tid].update({'status': 'merging', 'percent': 100})

    ffmpeg = shutil.which("ffmpeg")
    opts = {'outtmpl': outtmpl, 'quiet': True, 'no_warnings': True, 'progress_hooks': [hook], 'nocheckcertificate': True}
    if ffmpeg: opts['ffmpeg_location'] = ffmpeg

    if height == 'audio':
        opts['format'] = 'bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio'
    else:
        # For 16K/8K support, if ffmpeg exists, it will merge. If not, it will fallback to best single file.
        opts['format'] = f'bestvideo[height<={height}]+bestaudio/best[height<={height}]/best' if ffmpeg else f'best[height<={height}]/best'
        opts['merge_output_format'] = 'mp4'

    try:
        with yt_dlp.YoutubeDL(opts) as ydl: ydl.extract_info(url, download=True)
        
        candidates = [f for f in DOWNLOAD_DIR.glob(f"{tid}_*") if f.suffix.lower() in MEDIA_EXTS]
        candidates.sort(key=lambda p: p.stat().st_size, reverse=True)
        if not candidates: raise Exception("Download failed or file format not supported.")

        filename = str(candidates[0])
        tasks[tid].update({'status': 'done', 'filename': filename})

        with history_lock:
            download_history.append({'task_id': tid, 'title': title, 'filename': os.path.basename(filename),
                                     'thumbnail': thumb, 'resolution': 'Audio' if height == 'audio' else f"{height}p",
                                     'time': time.strftime("%Y-%m-%d %H:%M"), 'uploader': uploader, 'views': views, 'description': desc})
        save_history()
    except Exception as e:
        tasks[tid].update({'status': 'error', 'error': str(e)})

@app.route('/api/start-download', methods=['POST'])
@limiter.limit("20 per minute")
def start_download():
    verify_token()
    d = request.json or {}
    url = d.get('url', '').strip()
    if not url: return jsonify({"error": "URL Required"}), 400
    
    tid = str(uuid.uuid4())[:8]
    tasks[tid] = {'status': 'starting', 'percent': 0, 'filename': ''}
    threading.Thread(target=download_worker, args=(tid, url, d.get('height'), d.get('title', 'Video'), d.get('thumb', ''), d.get('uploader', 'Channel'), d.get('views', '0'), d.get('description', '')), daemon=True).start()
    return jsonify({"task_id": tid})

@app.route('/api/progress/<tid>', methods=['GET'])
def get_progress(tid):
    verify_token()
    t = tasks.get(tid, {'status': 'error', 'error': 'Not found', 'percent': 0})
    return jsonify({'status': t.get('status'), 'percent': round(t.get('percent', 0), 2), 'speed': round((t.get('speed') or 0)/(1024*1024), 2),
                    'downloaded_mb': round((t.get('downloaded') or 0)/(1024*1024), 2), 'total_mb': round((t.get('total') or 0)/(1024*1024), 2),
                    'eta': t.get('eta', 0), 'error': t.get('error', '')})

@app.route('/api/local-videos', methods=['GET'])
def get_local():
    verify_token()
    vids = []
    hm = {i['filename']: i for i in download_history}
    files = [f for f in DOWNLOAD_DIR.iterdir() if f.is_file() and f.suffix.lower() in MEDIA_EXTS and f.stat().st_size > 1000]
    for f in sorted(files, key=lambda p: p.stat().st_mtime, reverse=True):
        m = hm.get(f.name, {})
        vids.append({'task_id': m.get('task_id', ''), 'title': m.get('title', f.stem), 'filename': f.name, 'thumbnail': m.get('thumbnail', ''),
                     'uploader': m.get('uploader', 'Unknown'), 'views': m.get('views', ''), 'resolution': m.get('resolution', 'Local'),
                     'description': m.get('description', ''), 'time': m.get('time', time.strftime('%Y-%m-%d %H:%M', time.localtime(f.stat().st_mtime))),
                     'url': f"/api/stream/{urllib.parse.quote(f.name)}"})
    return jsonify(vids)

@app.route('/api/history', methods=['GET'])
def get_history():
    verify_token()
    return jsonify(download_history[::-1])

@app.route('/api/delete-video', methods=['POST'])
def delete_video():
    verify_token()
    fname = sanitize_basename((request.json or {}).get('filename'))
    if not fname: return jsonify({"error": "Filename required"}), 400
    fpath = DOWNLOAD_DIR / fname
    if fpath.exists():
        try:
            os.remove(fpath)
            global download_history
            with history_lock: download_history = [i for i in download_history if i['filename'] != fname]
            save_history()
            return jsonify({"status": "success"})
        except Exception as e: return jsonify({"error": str(e)}), 500
    return jsonify({"error": "File not found"}), 404

@app.route('/api/stream/<path:filename>', methods=['GET'])
def stream(filename):
    fpath = DOWNLOAD_DIR / sanitize_basename(filename)
    if fpath.exists(): return send_file(fpath, conditional=True)
    return jsonify({"error": "Not found"}), 404

# CRITICAL FOR WEBTOAPP: This route must return a standard HTTP file stream to trigger native download
@app.route('/api/get-file/<tid>', methods=['GET'])
def get_file(tid):
    if tid not in tasks or tasks[tid]['status'] != 'done': return jsonify({"error": "Not ready"}), 400
    fpath = pathlib.Path(tasks[tid]['filename']).resolve()
    if not fpath.exists(): return jsonify({"error": "Missing"}), 404
    
    ext = fpath.suffix.lstrip('.')
    title, res = "SnapPro_Video", "Video"
    for i in download_history:
        if i['task_id'] == tid: title, res = i['title'], i['resolution']; break
    
    # as_attachment=True is crucial so Android DownloadManager catches it
    return send_file(fpath, as_attachment=True, conditional=True, download_name=f"[{res}] {get_safe_filename(title)}.{ext}")

@app.route('/api/download-history-file/<tid>', methods=['GET'])
def download_history_file(tid):
    for i in download_history:
        if i['task_id'] == tid:
            fpath = DOWNLOAD_DIR / sanitize_basename(i['filename'])
            if fpath.exists():
                ext = i['filename'].split('.')[-1]
                return send_file(fpath, as_attachment=True, conditional=True, download_name=f"[{i['resolution']}] {get_safe_filename(i['title'])}.{ext}")
    return jsonify({"error": "Not found"}), 404

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)
