from flask import Flask, request, send_file, jsonify
import subprocess
import os
import base64
import json
import shutil
import threading
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

FPS = 25
FADE_DUR = 0.4
VIDEO_W = 720
VIDEO_H = 1280
JOBS_ROOT = "/tmp/jobs"
SCENE_TIMEOUT = 300    # seconds per scene encode
FINALIZE_TIMEOUT = 600

# Module-level lock: one ffmpeg at a time across all request threads.
ffmpeg_lock = threading.Lock()


class FFmpegError(Exception):
    pass


def run_ffmpeg(args, timeout):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-y'] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(result.stderr[-2000:])


def get_audio_duration(path):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'json', path
    ], capture_output=True, text=True, check=True, timeout=30)
    info = json.loads(result.stdout)
    return float(info['format']['duration'])


def safe_remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


@app.route('/', methods=['GET', 'HEAD'])
def health():
    return jsonify({"status": "ok"})


@app.route('/add_scene', methods=['POST'])
def add_scene():
    data = request.get_json(force=True)
    job_id = data['job_id']
    index = int(data['scene_index'])
    total = int(data['total_scenes'])

    work_dir = f"{JOBS_ROOT}/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    clip_path = f"{work_dir}/clip_{index}.mp4"
    tmp_clip_path = f"{work_dir}/clip_{index}.tmp.mp4"

    # Idempotent: a retry for an already built scene returns immediately.
    if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
        return jsonify({"status": "ok", "job_id": job_id, "index": index, "cached": True})

    img_path = f"{work_dir}/img_{index}.png"
    audio_path = f"{work_dir}/audio_{index}.mp3"

    with open(img_path, 'wb') as f:
        f.write(base64.b64decode(data['image_base64']))
    with open(audio_path, 'wb') as f:
        f.write(base64.b64decode(data['audio_base64']))
    del data  # drop base64 strings from memory before encoding

    try:
        duration = get_audio_duration(audio_path)

        vf_fade = []
        if index > 0:
            vf_fade.append(f"fade=t=in:st=0:d={FADE_DUR}")
        if index < total - 1:
            fade_out_start = max(duration - FADE_DUR, 0)
            vf_fade.append(f"fade=t=out:st={fade_out_start:.3f}:d={FADE_DUR}")
        fade_chain = ("," + ",".join(vf_fade)) if vf_fade else ""

        args = [
            '-loop', '1', '-framerate', str(FPS), '-i', img_path,
            '-i', audio_path,
            '-vf',
            f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_W}:{VIDEO_H},format=yuv420p{fade_chain}",
            '-c:v', 'libx264', '-preset', 'ultrafast', '-tune', 'stillimage',
            '-threads', '2',
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '1',
            '-t', f"{duration:.3f}",
            '-movflags', '+faststart',
            tmp_clip_path,
        ]

        log.info(f"[{job_id}] scene {index}/{total - 1}: waiting for lock")
        with ffmpeg_lock:
            log.info(f"[{job_id}] scene {index}: encoding {duration:.1f}s")
            run_ffmpeg(args, timeout=SCENE_TIMEOUT)
        os.replace(tmp_clip_path, clip_path)
        log.info(f"[{job_id}] scene {index}: done")

    except subprocess.TimeoutExpired:
        safe_remove(tmp_clip_path)
        log.error(f"[{job_id}] scene {index}: timeout")
        return jsonify({"error": "ffmpeg timeout", "index": index}), 504
    except (FFmpegError, subprocess.CalledProcessError) as e:
        safe_remove(tmp_clip_path)
        log.error(f"[{job_id}] scene {index}: {e}")
        return jsonify({"error": "ffmpeg failed", "index": index, "detail": str(e)}), 500
    finally:
        safe_remove(img_path, audio_path)

    return jsonify({"status": "ok", "job_id": job_id, "index": index})


@app.route('/finalize', methods=['POST'])
def finalize():
    data = request.get_json(force=True)
    job_id = data['job_id']
    total = int(data['total_scenes'])

    work_dir = f"{JOBS_ROOT}/{job_id}"
    clip_paths = [f"{work_dir}/clip_{i}.mp4" for i in range(total)]

    missing = [i for i, p in enumerate(clip_paths)
               if not os.path.exists(p) or os.path.getsize(p) == 0]
    if missing:
        return jsonify({"error": "missing clips", "missing_indexes": missing}), 400

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    output_path = f"{work_dir}/output.mp4"
    try:
        with ffmpeg_lock:
            run_ffmpeg([
                '-f', 'concat', '-safe', '0', '-i', concat_list_path,
                '-c', 'copy', '-movflags', '+faststart', output_path
            ], timeout=FINALIZE_TIMEOUT)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "finalize timeout"}), 504
    except FFmpegError as e:
        log.error(f"[{job_id}] finalize: {e}")
        return jsonify({"error": "finalize failed", "detail": str(e)}), 500

    log.info(f"[{job_id}] final video {os.path.getsize(output_path) / 1e6:.1f} MB")
    response = send_file(output_path, mimetype='video/mp4')

    @response.call_on_close
    def cleanup():
        shutil.rmtree(work_dir, ignore_errors=True)

    return response


if __name__ == '__main__':
    # Fallback for local runs. On Render use gunicorn (see start command).
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
