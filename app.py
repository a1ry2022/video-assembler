from flask import Flask, request, send_file, jsonify
import subprocess
import requests
import os
import uuid
import base64
import json

app = Flask(__name__)

FPS = 25
TRANSITION_DUR = 0.6  # seconds, crossfade length between scenes


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


@app.route('/assemble', methods=['POST'])
def assemble():
    data = request.json
    scenes = data['scenes']  # [{image_url, audio_base64}, ...]
    job_id = str(uuid.uuid4())
    work_dir = f"/tmp/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    clip_paths = []
    durations = []

    for i, scene in enumerate(scenes):
        img_path = f"{work_dir}/img_{i}.jpg"
        audio_path = f"{work_dir}/audio_{i}.mp3"

        img_data = requests.get(scene['image_url']).content
        with open(img_path, 'wb') as f:
            f.write(img_data)

        audio_data = base64.b64decode(scene['audio_base64'])
        with open(audio_path, 'wb') as f:
            f.write(audio_data)

        duration = get_audio_duration(audio_path)
        durations.append(duration)
        total_frames = max(int(duration * FPS), 1)

        # alternate zoom-in / zoom-out for visual variety between scenes
        if i % 2 == 0:
            zoom_expr = "min(zoom+0.0012,1.25)"
        else:
            zoom_expr = "if(eq(on,0),1.25,max(zoom-0.0012,1.0))"

        clip_path = f"{work_dir}/clip_{i}.mp4"
        subprocess.run([
            'ffmpeg', '-y', '-loop', '1', '-i', img_path, '-i', audio_path,
            '-filter_complex',
            f"[0:v]scale=1600:900,zoompan=z='{zoom_expr}':d={total_frames}"
            f":s=1280x720:fps={FPS}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)',"
            f"format=yuv420p[v]",
            '-map', '[v]', '-map', '1:a',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-b:a', '128k',
            '-t', str(duration),
            clip_path
        ], check=True)
        clip_paths.append(clip_path)

        os.remove(img_path)
        os.remove(audio_path)

    output_path = f"{work_dir}/output.mp4"

    if len(clip_paths) == 1:
        os.rename(clip_paths[0], output_path)
    else:
        # build a chained xfade / acrossfade filter_complex across all clips
        inputs = []
        for cp in clip_paths:
            inputs += ['-i', cp]

        filter_parts = []
        v_label = "0:v"
        a_label = "0:a"
        cumulative = durations[0]

        for i in range(1, len(clip_paths)):
            next_v = f"{i}:v"
            next_a = f"{i}:a"
            out_v = f"v{i}"
            out_a = f"a{i}"
            offset = max(cumulative - TRANSITION_DUR, 0)

            filter_parts.append(
                f"[{v_label}][{next_v}]xfade=transition=fade:"
                f"duration={TRANSITION_DUR}:offset={offset}[{out_v}]"
            )
            filter_parts.append(
                f"[{a_label}][{next_a}]acrossfade=d={TRANSITION_DUR}[{out_a}]"
            )

            v_label = out_v
            a_label = out_a
            cumulative = offset + durations[i]

        filter_complex = ";".join(filter_parts)

        cmd = ['ffmpeg', '-y'] + inputs + [
            '-filter_complex', filter_complex,
            '-map', f'[{v_label}]', '-map', f'[{a_label}]',
            '-c:v', 'libx264', '-preset', 'ultrafast',
            '-c:a', 'aac', '-b:a', '128k',
            output_path
        ]
        subprocess.run(cmd, check=True)

        for cp in clip_paths:
            os.remove(cp)

    return send_file(output_path, mimetype='video/mp4')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
