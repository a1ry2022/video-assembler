from flask import Flask, request, send_file, jsonify
import subprocess
import os
import base64
import json
import shutil
import threading

app = Flask(__name__)

FPS = 25
FADE_DUR = 0.4
VIDEO_W = 720
VIDEO_H = 1280
JOBS_ROOT = "/tmp/jobs"
ffmpeg_lock = threading.Lock()


@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok"})


def get_audio_duration(path):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'json', path
    ], capture_output=True, text=True, check=True)
    info = json.loads(result.stdout)
    return float(info['format']['duration'])


@app.route('/add_scene', methods=['POST'])
def add_scene():
    """Build ONE clip from ONE scene: static image + audio, no zoom/pan.
    Keeps memory footprint minimal and predictable regardless of scene
    length, since there is no per-frame filter work at all."""
    data = request.json
    job_id = data['job_id']
    index = int(data['scene_index'])
    total = int(data['total_scenes'])

    work_dir = f"{JOBS_ROOT}/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    img_path = f"{work_dir}/img_{index}.jpg"
    audio_path = f"{work_dir}/audio_{index}.mp3"

    with open(img_path, 'wb') as f:
        f.write(base64.b64decode(data['image_base64']))
    with open(audio_path, 'wb') as f:
        f.write(base64.b64decode(data['audio_base64']))

    duration = get_audio_duration(audio_path)

    vf_fade = []
    if index > 0:
        vf_fade.append(f"fade=t=in:st=0:d={FADE_DUR}")
    if index < total - 1:
        fade_out_start = max(duration - FADE_DUR, 0)
        vf_fade.append(f"fade=t=out:st={fade_out_start}:d={FADE_DUR}")
    fade_chain = ("," + ",".join(vf_fade)) if vf_fade else ""

    clip_path = f"{work_dir}/clip_{index}.mp4"
    subprocess.run([
        'ffmpeg', '-y', '-loop', '1', '-i', img_path, '-i', audio_path,
        '-vf',
        f"scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},format=yuv420p{fade_chain}",
        '-r', str(FPS),
        '-c:v', 'libx264', '-preset', 'ultrafast',
        '-c:a', 'aac', '-b:a', '128k',
        '-t', str(duration),
        clip_path
    ], check=True)

    os.remove(img_path)
    os.remove(audio_path)

    return jsonify({"status": "ok", "job_id": job_id, "index": index})


@app.route('/finalize', methods=['POST'])
def finalize():
    """Concat all clips already saved on disk for this job into the final
    video. Only touches small files on disk, never holds base64 in memory."""
    data = request.json
    job_id = data['job_id']
    total = int(data['total_scenes'])

    work_dir = f"{JOBS_ROOT}/{job_id}"
    clip_paths = [f"{work_dir}/clip_{i}.mp4" for i in range(total)]

    missing = [p for p in clip_paths if not os.path.exists(p)]
    if missing:
        return jsonify({"error": f"missing clips: {missing}"}), 400

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    output_path = f"{work_dir}/output.mp4"
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', concat_list_path, '-c', 'copy', output_path
    ], check=True)

    response = send_file(output_path, mimetype='video/mp4')

    @response.call_on_close
    def cleanup():
        shutil.rmtree(work_dir, ignore_errors=True)

    return response


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
