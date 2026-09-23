from flask import Flask, request, send_file, jsonify
import subprocess
import os
import uuid
import base64
import json
import re

app = Flask(__name__)

FPS = 25
FADE_DUR = 0.4
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
VIDEO_W = 1080
VIDEO_H = 1920


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


def escape_drawtext(text):
    return (text.replace('\\', '\\\\')
                .replace(':', '\\:')
                .replace("'", "\u2019")
                .replace('%', '\\%'))


def build_caption_filters(narration_text, duration):
    words = narration_text.split()
    if not words:
        return ""
    chunk_size = 3
    chunks = [' '.join(words[i:i + chunk_size]) for i in range(0, len(words), chunk_size)]
    n = len(chunks)
    seg_dur = duration / n
    filters = []
    for i, chunk in enumerate(chunks):
        start = i * seg_dur
        end = start + seg_dur
        safe_text = escape_drawtext(chunk.upper())
        filters.append(
            f"drawtext=fontfile={FONT_PATH}:text='{safe_text}':"
            f"fontsize=64:fontcolor=white:borderw=6:bordercolor=black:"
            f"x=(w-text_w)/2:y=h*0.75:"
            f"enable='between(t,{start:.2f},{end:.2f})'"
        )
    return "," + ",".join(filters)


@app.route('/assemble', methods=['POST'])
def assemble():
    data = request.json
    scenes = data['scenes']  # [{image_base64, audio_base64, narration_text}, ...]
    job_id = str(uuid.uuid4())
    work_dir = f"/tmp/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    clip_paths = []
    n = len(scenes)

    for i, scene in enumerate(scenes):
        img_path = f"{work_dir}/img_{i}.jpg"
        audio_path = f"{work_dir}/audio_{i}.mp3"

        img_data = base64.b64decode(scene['image_base64'])
        with open(img_path, 'wb') as f:
            f.write(img_data)

        audio_data = base64.b64decode(scene['audio_base64'])
        with open(audio_path, 'wb') as f:
            f.write(audio_data)

        duration = get_audio_duration(audio_path)
        total_frames = max(int(duration * FPS), 1)

        zoom_expr = "min(zoom+0.0012,1.2)"

        vf_fade = []
        if i > 0:
            vf_fade.append(f"fade=t=in:st=0:d={FADE_DUR}")
        if i < n - 1:
            fade_out_start = max(duration - FADE_DUR, 0)
            vf_fade.append(f"fade=t=out:st={fade_out_start}:d={FADE_DUR}")
        fade_chain = ("," + ",".join(vf_fade)) if vf_fade else ""

        caption_chain = build_caption_filters(scene.get('narration_text', ''), duration)

        clip_path = f"{work_dir}/clip_{i}.mp4"
        subprocess.run([
            'ffmpeg', '-y', '-loop', '1', '-i', img_path, '-i', audio_path,
            '-filter_complex',
            f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
            f"crop={VIDEO_W}:{VIDEO_H},"
            f"zoompan=z='{zoom_expr}':d={total_frames}"
            f":s={VIDEO_W}x{VIDEO_H}:fps={FPS}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
            f"format=yuv420p{fade_chain}{caption_chain}[v]",
            '-map', '[v]', '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-b:a', '128k',
            '-t', str(duration),
            clip_path
        ], check=True)
        clip_paths.append(clip_path)

        os.remove(img_path)
        os.remove(audio_path)

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    output_path = f"{work_dir}/output.mp4"
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', concat_list_path, '-c', 'copy', output_path
    ], check=True)

    for cp in clip_paths:
        os.remove(cp)

    return send_file(output_path, mimetype='video/mp4')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
