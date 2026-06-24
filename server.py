import os
import time
import uuid
import tempfile
import subprocess
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}}, supports_credentials=False)  # Allow any frontend (Netlify etc) to call this

DOWNLOAD_DIR = tempfile.mkdtemp()
MAX_AGE_SECONDS = 3600  # auto-cleanup files older than 1 hour


def cleanup_old_files():
    now = time.time()
    try:
        for f in os.listdir(DOWNLOAD_DIR):
            p = os.path.join(DOWNLOAD_DIR, f)
            if os.path.isfile(p) and (now - os.path.getctime(p)) > MAX_AGE_SECONDS:
                os.remove(p)
    except Exception:
        pass


@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok", "message": "VORTEX Backend running"})


@app.route('/debug', methods=['GET'])
def debug_info():
    """
    Quick diagnostic endpoint — checks that yt-dlp and ffmpeg are actually
    installed and runnable on this server, without attempting a real
    download. Visit this URL directly in a browser if downloads are
    failing, to immediately see whether the issue is missing tools vs
    a YouTube-side block.
    """
    info = {}
    for tool, args in [('yt-dlp', ['yt-dlp', '--version']), ('ffmpeg', ['ffmpeg', '-version'])]:
        try:
            r = subprocess.run(args, capture_output=True, text=True, timeout=15)
            info[tool] = {
                "installed": r.returncode == 0,
                "version_output": (r.stdout or r.stderr).split('\n')[0][:120]
            }
        except FileNotFoundError:
            info[tool] = {"installed": False, "version_output": "NOT FOUND on this server"}
        except Exception as e:
            info[tool] = {"installed": False, "version_output": str(e)[:120]}
    info["download_dir_writable"] = os.access(DOWNLOAD_DIR, os.W_OK)
    return jsonify(info)


@app.route('/download', methods=['POST'])
def download_video():
    """
    Download a YouTube (or other supported site) video using yt-dlp.

    NOTE: YouTube actively blocks requests coming from datacenter IPs
    (which is what cloud hosts like Railway use) with a "Sign in to
    confirm you're not a bot" error. We try several yt-dlp client
    spoofing strategies in order, since different clients (android,
    ios, web embedded) get flagged at different rates. This is the
    same fundamental limitation every free YouTube-downloader backend
    faces — even commercial tools rely on paid residential proxies to
    fully solve it. This gives the best free-tier success rate.
    """
    cleanup_old_files()
    data = request.json or {}
    url = data.get('url', '')
    quality = data.get('quality', '1080')
    audio_only = data.get('audio_only', False)

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    uid = uuid.uuid4().hex[:10]
    out_template = os.path.join(DOWNLOAD_DIR, uid + '.%(ext)s')

    # Strategies tried in order. Each uses only yt-dlp's built-in client
    # spoofing (no extra system dependencies needed), so this never crashes
    # even on minimal hosting environments. Different clients (android, ios,
    # embedded) get flagged by YouTube's bot-detection at different rates,
    # so trying several in sequence gives the best realistic free-tier
    # success rate without needing paid residential proxies.
    client_strategies = [
        ['--extractor-args', 'youtube:player_client=android'],
        ['--extractor-args', 'youtube:player_client=ios'],
        ['--extractor-args', 'youtube:player_client=tv_embedded'],
        ['--extractor-args', 'youtube:player_client=web_embedded'],
        ['--extractor-args', 'youtube:player_client=mweb'],
        [],  # plain default, last resort
    ]

    last_error = ''

    for strategy in client_strategies:
        # Clean up any partial file from a previous failed attempt
        for f in os.listdir(DOWNLOAD_DIR):
            if f.startswith(uid):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, f))
                except Exception:
                    pass

        try:
            if audio_only:
                cmd = [
                    'yt-dlp',
                    '--format', 'bestaudio/best',
                    '--extract-audio',
                    '--audio-format', 'mp3',
                    '--audio-quality', '0',
                    '--output', out_template,
                    '--no-playlist',
                    '--no-check-certificate',
                    '--socket-timeout', '30',
                ] + strategy + [url]
            else:
                q = 'bestvideo[height<={0}]+bestaudio/best[height<={0}]'.format(quality)
                cmd = [
                    'yt-dlp',
                    '--format', q,
                    '--merge-output-format', 'mp4',
                    '--output', out_template,
                    '--no-playlist',
                    '--no-check-certificate',
                    '--socket-timeout', '30',
                ] + strategy + [url]

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)

            if result.returncode == 0:
                matches = [f for f in os.listdir(DOWNLOAD_DIR) if f.startswith(uid)]
                if matches:
                    filename = matches[0]
                    filepath = os.path.join(DOWNLOAD_DIR, filename)
                    duration = get_duration(filepath)
                    return jsonify({
                        "success": True,
                        "filename": filename,
                        "duration": duration,
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

    # All strategies failed — YouTube is blocking this server's IP for this video.
    is_bot_block = 'sign in' in last_error.lower() or 'bot' in last_error.lower()
    friendly = (
        "YOUTUBE_BLOCKED"  # special code the frontend recognises to show its own in-page fallback uploader
    ) if is_bot_block else last_error

    return jsonify({"error": friendly, "blocked": is_bot_block}), 400


@app.route('/upload', methods=['POST'])
def upload_video():
    """Accept a user-uploaded video file directly (no YouTube needed)."""
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

    duration = get_duration(filepath)

    return jsonify({
        "success": True,
        "filename": filename,
        "duration": duration,
        "stream_url": "/stream/" + filename,
        "download_url": "/file/" + filename
    })


def get_duration(filepath):
    """Get video duration in seconds using ffprobe."""
    try:
        cmd = [
            'ffprobe', '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'default=noprint_wrappers=1:nokey=1',
            filepath
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        return float(result.stdout.strip())
    except Exception:
        return 0


@app.route('/file/<filename>', methods=['GET'])
def serve_file(filename):
    """Force-download a file (used for final clip)."""
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(filepath, as_attachment=True)


@app.route('/stream/<filename>', methods=['GET'])
def stream_file(filename):
    """Stream a file inline (used for in-browser preview / editor scrubbing)."""
    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "File not found or expired"}), 404
    return send_file(filepath, as_attachment=False, conditional=True)


@app.route('/clip', methods=['POST'])
def cut_clip():
    """
    Cut + caption + format-convert a clip from a previously downloaded/uploaded video.
    All heavy lifting (encoding) happens here on the server via FFmpeg — fast,
    reliable, and works the same on every device since the phone/browser does
    no encoding itself.
    """
    cleanup_old_files()
    data = request.json or {}
    filename = data.get('filename', '')
    start = float(data.get('start', 0))
    end = float(data.get('end', 30))
    caption = data.get('caption', '')
    out_format = data.get('format', 'mp4')      # mp4 | mov | webm | mp3
    quality = data.get('quality', '1080')        # 720 | 1080
    ratio = data.get('ratio', '9:16')             # 9:16 | 1:1 | 16:9 | 4:5

    filepath = os.path.join(DOWNLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "Source file not found or expired. Please re-download/upload."}), 404

    duration = max(0.5, end - start)

    try:
        out_uid = uuid.uuid4().hex[:8]

        # AUDIO ONLY
        if out_format == 'mp3':
            out_name = 'clip_' + out_uid + '.mp3'
            out_path = os.path.join(DOWNLOAD_DIR, out_name)
            cmd = [
                'ffmpeg', '-y',
                '-ss', str(start), '-i', filepath, '-t', str(duration),
                '-vn', '-acodec', 'libmp3lame', '-q:a', '2',
                out_path
            ]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
            if result.returncode != 0:
                return jsonify({"error": result.stderr[-500:]}), 400
            return jsonify({"success": True, "clip_filename": out_name, "download_url": "/file/" + out_name})

        # Resolution from quality + ratio
        base_h = 720 if quality == '720' else 1080
        if ratio == '1:1':
            scale_filter = 'scale=' + str(base_h) + ':' + str(base_h) + ':force_original_aspect_ratio=increase,crop=' + str(base_h) + ':' + str(base_h)
        elif ratio == '16:9':
            w = round(base_h * 16 / 9)
            scale_filter = 'scale=' + str(w) + ':' + str(base_h) + ':force_original_aspect_ratio=increase,crop=' + str(w) + ':' + str(base_h)
        elif ratio == '4:5':
            w = round(base_h * 4 / 5)
            scale_filter = 'scale=' + str(w) + ':' + str(base_h) + ':force_original_aspect_ratio=increase,crop=' + str(w) + ':' + str(base_h)
        else:  # 9:16
            w = round(base_h * 9 / 16)
            scale_filter = 'scale=' + str(w) + ':' + str(base_h) + ':force_original_aspect_ratio=increase,crop=' + str(w) + ':' + str(base_h)

        vf_parts = [scale_filter]

        if caption:
            safe_caption = caption[:70].replace("'", "").replace(':', '').replace('"', '')
            font_size = max(20, round(base_h * 0.035))
            drawtext = (
                "drawtext=text='" + safe_caption + "':"
                "fontsize=" + str(font_size) + ":fontcolor=white:"
                "box=1:boxcolor=black@0.7:boxborderw=10:"
                "x=(w-text_w)/2:y=h-text_h-50"
            )
            vf_parts.append(drawtext)

        vf = ",".join(vf_parts)

        ext = 'mov' if out_format == 'mov' else 'webm' if out_format == 'webm' else 'mp4'
        out_name = 'clip_' + out_uid + '.' + ext
        out_path = os.path.join(DOWNLOAD_DIR, out_name)

        crf = '23' if quality == '720' else '19'  # lower = higher quality

        if out_format == 'webm':
            video_codec = ['-c:v', 'libvpx-vp9', '-crf', crf, '-b:v', '0']
            audio_codec = ['-c:a', 'libopus']
        else:
            video_codec = ['-c:v', 'libx264', '-preset', 'medium', '-crf', crf, '-movflags', '+faststart']
            audio_codec = ['-c:a', 'aac', '-b:a', '192k']

        cmd = [
            'ffmpeg', '-y',
            '-ss', str(start), '-i', filepath, '-t', str(duration),
            '-vf', vf,
        ] + video_codec + audio_codec + [out_path]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=240)
        if result.returncode != 0:
            return jsonify({"error": result.stderr[-600:]}), 400

        return jsonify({
            "success": True,
            "clip_filename": out_name,
            "download_url": "/file/" + out_name
        })

    except subprocess.TimeoutExpired:
        return jsonify({"error": "Clip processing timed out"}), 408
    except Exception as e:
        return jsonify({"error": str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
