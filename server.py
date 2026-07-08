import os
import time
import uuid
import glob
import tempfile
import subprocess
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)

DOWNLOAD_DIR = tempfile.mkdtemp()
MAX_AGE_SECONDS = 3600


def cleanup_old_files():
    now = time.time()
    try:
        for f in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(p) and (now - os.path.getctime(p)) > MAX_AGE_SECONDS:
                os.remove(p)
    except Exception:
        pass


def parse_vtt(vtt_path):
    """Extract plain spoken text from a WebVTT subtitle file (dedup lines)."""
    try:
        with open(vtt_path, 'r', encoding='utf-8', errors='ignore') as fh:
            raw = fh.read()
    except Exception:
        return ''
    out, seen = [], set()
    for line in raw.split('\n'):
        line = line.strip()
        if not line or '-->' in line or line.upper().startswith('WEBVTT') \
           or line.startswith('Kind:') or line.startswith('Language:') or line.isdigit():
            continue
        # strip inline VTT tags like <00:00:01.000><c>word</c>
        import re
        clean = re.sub(r'<[^>]+>', '', line).strip()
        if clean and clean not in seen:
            seen.add(clean)
            out.append(clean)
    return ' '.join(out)[:8000]


def fetch_youtube_transcript(url):
    """Try to grab YouTube auto/manual captions as plain text (no full download)."""
    uid = 'sub_' + uuid.uuid4().hex[:8]
    tmpl = os.path.join(DOWNLOAD_DIR, uid + '.%(ext)s')
    try:
        cmd = [
            'yt-dlp', '--skip-download',
            '--write-auto-sub', '--write-sub',
            '--sub-lang', 'en.*', '--sub-format', 'vtt',
            '--no-playlist', '--no-check-certificate',
            '--extractor-args', 'youtube:player_client=android',
            '--output', tmpl, url
        ]
        subprocess.run(cmd, capture_output=True, text=True, timeout=90)
        vtts = glob.glob(os.path.join(DOWNLOAD_DIR, uid + '*.vtt'))
        text = ''
        if vtts:
            text = parse_vtt(vtts[0])
        for f in glob.glob(os.path.join(DOWNLOAD_DIR, uid + '*')):
            try: os.remove(f)
            except Exception: pass
        return text
    except Exception:
        return ''


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "VORTEX Backend running"})


@app.route('/debug', methods=['GET'])
def debug_info():
    info = {}
    for tool, args in [('yt-dlp', ['yt-dlp', '--version']), ('ffmpeg', ['ffmpeg', '-version'])]:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            info[tool] = {"installed": r.returncode == 0, "version_output": (r.stdout or r.stderr).split('\n')[0][:120]}
        except FileNotFoundError:
            info[tool] = {"installed": False, "version_output": "NOT FOUND on this server"}
        except Exception as e:
            info[tool] = {"installed": False, "version_output": str(e)[:120]}
    info["download_dir_writable"] = os.access(DOWNLOAD_DIR, os.W_OK)
    return jsonify(info)


@app.route('/download', methods=['POST'])
def download_video():
    cleanup_old_files()
    data = request.json or {}
    url = data.get('url', '')
    quality = data.get('quality', '1080')
    audio_only = data.get('audio_only', False)

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    # Grab the transcript first (cheap, no full download) so the AI reads real words
    transcript = fetch_youtube_transcript(url)

    uid = uuid.uuid4().hex[:10]
    out_template = os.path.join(DOWNLOAD_DIR, uid + '.%(ext)s')

    client_strategies = [
        ['--extractor-args', 'youtube:player_client=android'],
        ['--extractor-args', 'youtube:player_client=ios'],
        ['--extractor-args', 'youtube:player_client=tv_embedded'],
        ['--extractor-args', 'youtube:player_client=web_embedded'],
        ['--extractor-args', 'youtube:player_client=mweb'],
        [],
    ]
    last_error = ''

    for strategy in client_strategies:
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(uid):
                try: os.remove(os.path.join(DOWNLOAD_DIR, f))
                except Exception: pass
        try:
            if audio_only:
                cmd = ['yt-dlp', '--format', 'bestaudio/best', '--extract-audio',
                       '--audio-format', 'mp3', '--audio-quality', '0',
                       '--output', out_template, '--no-playlist',
                       '--no-check-certificate', '--socket-timeout', '30'] + strategy + [url]
            else:
                q = 'bestvideo[height<={0}]+bestaudio/best[height<={0}]'.format(quality)
                cmd = ['yt-dlp', '--format', q, '--merge-output-format', 'mp4',
                       '--output', out_template, '--no-playlist',
                       '--no-check-certificate', '--socket-timeout', '30'] + strategy + [url]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if result.returncode == 0:
                matches = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(uid)]
                if matches:
                    filename = matches[0]
                    filepath = os.path.join(DOWNLOAD_DIR, filename)
                    return jsonify({
                        "success": True,
                        "filename": filename,
                        "duration": get_duration(filepath),
                        "transcript": transcript,       # NEW
                        "stream_url": "/stream/" + filename,
                        "download_url": "/file/" + filename
                    })
            last_error = result.stderr[-500:] if result.stderr else 'Unknown yt-dlp error'
        except subprocess.TimeoutExpired:
            last_error = 'Timed out (video too large or connection too slow)'
        except FileNotFoundError:
            return jsonify({"error": "yt-dlp not installed on server"}), 500
        except Exception as e:
            last_error = str(e)

    is_bot_block = 'sign in' in last_error.lower() or 'bot' in last_error.lower()
    friendly = "YOUTUBE_BLOCKED" if is_bot_block else last_error
    # Even if the video download was blocked, we may still have the transcript
    return jsonify({"error": friendly, "blocked": is_bot_block, "transcript": transcript}), 400


@app.route('/transcribe', methods=['POST'])
def transcribe_file():
    """
    Transcribe a previously uploaded file using OpenAI Whisper if available.
    Falls back gracefully (returns success:false) if Whisper isn't installed,
    so the frontend can use its niche-aware fallback instead.
    """
    cleanup_old_files()
    data = request.json or {}
    filename = data.get('filename', '')
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"success": False, "error": "File not found"}), 404

    try:
        import whisper  # openai-whisper
    except Exception:
        return jsonify({"success": False, "error": "Whisper not installed on server"}), 200

    try:
        # Extract audio to 16k mono wav for faster transcription
        wav = filepath + '.16k.wav'
        subprocess.run(['ffmpeg', '-y', '-i', filepath, '-ar', '16000', '-ac', '1', '-vn', wav],
                       capture_output=True, text=True, timeout=180)
        model = whisper.load_model("base")   # "tiny" is faster, "base" is more accurate
        result = model.transcribe(wav, fp16=False)
        text = (result.get('text') or '').strip()
        try: os.remove(wav)
        except Exception: pass
        return jsonify({"success": True, "transcript": text[:8000]})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)[:200]}), 200


@app.route('/upload', methods=['POST'])
def upload_video():
    cleanup_old_files()
    if 'file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files['file']
    if f.filename == '':
        return jsonify({"error": "Empty filename"}), 400
    uid = uuid.uuid4().hex[:10]
    ext = os.path.splitext(f.filename)[1] or '.mp4'
    filename = uid + ext
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    f.save(filepath)
    return jsonify({
        "success": True, "filename": filename,
        "duration": get_duration(filepath),
        "stream_url": "/stream/" + filename,
        "download_url": "/file/" + filename
    })


def get_duration(filepath):
    try:
        cmd = ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
               '-of', 'default=noprint_wrappers=1:nokey=1', filepath]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return float(result.stdout.strip())
    except Exception:
        return 0


@app.route('/file/<filename>', methods=['GET'])
def serve_file(filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(filepath, as_attachment=True)


@app.route('/stream/<filename>', methods=['GET'])
def stream_file(filename):
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(filepath, as_attachment=False, conditional=True)


@app.route('/clip', methods=['POST'])
def cut_clip():
    cleanup_old_files()
    data = request.json or {}
    filename = data.get('filename', '')
    start = float(data.get('start', 0))
    end = float(data.get('end', 30))
    caption = data.get('caption', '')
    out_format = data.get('format', 'mp4')
    quality = data.get('quality', '1080')
    ratio = data.get('ratio', '9:16')

    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Source file not found or expired. Please re-download/upload."}), 404

    duration = max(0.5, end - start)
    try:
        out_uid = uuid.uuid4().hex[:8]
        if out_format == 'mp3':
            out_name = 'clip_' + out_uid + '.mp3'
            out_path = os.path.join(DOWNLOAD_DIR, out_name)
            cmd = ['ffmpeg', '-y', '-ss', str(start), '-i', filepath, '-t', str(duration),
                   '-vn', '-acodec', 'libmp3lame', '-q:a', '2', out_path]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                return jsonify({"error": result.stderr[-500:]}), 400
            return jsonify({"success": True, "clip_filename": out_name, "download_url": "/file/" + out_name})

        base_h = 720 if quality == '720' else 1080
        if ratio == '1:1':
            scale_filter = 'scale={0}:{0}:force_original_aspect_ratio=increase,crop={0}:{0}'.format(base_h)
        elif ratio == '16:9':
            w = round(base_h * 16 / 9)
            scale_filter = 'scale={0}:{1}:force_original_aspect_ratio=increase,crop={0}:{1}'.format(w, base_h)
        elif ratio == '4:5':
            w = round(base_h * 4 / 5)
            scale_filter = 'scale={0}:{1}:force_original_aspect_ratio=increase,crop={0}:{1}'.format(w, base_h)
        else:
            w = round(base_h * 9 / 16)
            scale_filter = 'scale={0}:{1}:force_original_aspect_ratio=increase,crop={0}:{1}'.format(w, base_h)

        vf_parts = [scale_filter]
        if caption:
            safe_caption = caption[:70].replace("'", "").replace(':', '').replace('"', '')
            font_size = max(20, round(base_h * 0.035))
            drawtext = ("drawtext=text='" + safe_caption + "':fontsize=" + str(font_size) +
                        ":fontcolor=white:box=1:boxcolor=black@0.7:boxborderw=10:x=(w-text_w)/2:y=h-text_h-50")
            vf_parts.append(drawtext)
        vf = ",".join(vf_parts)

        ext = 'mov' if out_format == 'mov' else 'webm' if out_format == 'webm' else 'mp4'
        out_name = 'clip_' + out_uid + '.' + ext
        out_path = os.path.join(DOWNLOAD_DIR, out_name)
        crf = '23' if quality == '720' else '19'

        if out_format == 'webm':
            video_codec = ['-c:v', 'libvpx-vp9', '-crf', crf, '-b:v', '0']
            audio_codec = ['-c:a', 'libopus']
        else:
            video_codec = ['-c:v', 'libx264', '-preset', 'medium', '-crf', crf, '-movflags', '+faststart']
            audio_codec = ['-c:a', 'aac', '-b:a', '192k']

        cmd = ['ffmpeg', '-y', '-ss', str(start), '-i', filepath, '-t', str(duration),
               '-vf', vf] + video_codec + audio_codec + [out_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            return jsonify({"error": result.stderr[-600:]}), 400
        return jsonify({"success": True, "clip_filename": out_name, "download_url": "/file/" + out_name})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Clip processing timed out"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
