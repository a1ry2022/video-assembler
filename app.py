from flask import Flask, request, send_file, jsonify
import subprocess
import requests
import os
import uuid

app = Flask(__name__)

@app.route('/', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

@app.route('/assemble', methods=['POST'])
def assemble():
    data = request.json
    scenes = data['scenes']  # [{image_url, audio_url}, ...]
    job_id = str(uuid.uuid4())
    work_dir = f"/tmp/{job_id}"
    os.makedirs(work_dir, exist_ok=True)

    clip_paths = []
    for i, scene in enumerate(scenes):
        img_path = f"{work_dir}/img_{i}.jpg"
        audio_path = f"{work_dir}/audio_{i}.mp3"

        img_data = requests.get(scene['image_url']).content
        with open(img_path, 'wb') as f:
            f.write(img_data)

        audio_data = requests.get(scene['audio_url']).content
        with open(audio_path, 'wb') as f:
            f.write(audio_data)

        clip_path = f"{work_dir}/clip_{i}.mp4"
        subprocess.run([
            'ffmpeg', '-y', '-loop', '1', '-i', img_path, '-i', audio_path,
            '-c:v', 'libx264', '-tune', 'stillimage', '-c:a', 'aac',
            '-b:a', '192k', '-pix_fmt', 'yuv420p', '-shortest',
            '-vf', 'scale=1920:1080',
            clip_path
        ], check=True)
        clip_paths.append(clip_path)

    concat_list_path = f"{work_dir}/concat.txt"
    with open(concat_list_path, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    output_path = f"{work_dir}/output.mp4"
    subprocess.run([
        'ffmpeg', '-y', '-f', 'concat', '-safe', '0',
        '-i', concat_list_path, '-c', 'copy', output_path
    ], check=True)

    return send_file(output_path, mimetype='video/mp4')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8080)
