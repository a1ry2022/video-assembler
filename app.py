from flask import Flask, request, send_file, jsonify
import subprocess
import os
import re
import glob
import random
import base64
import json
import shutil
import threading
import logging

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# ---------- video settings ----------
FPS = 25
VIDEO_W = 720
VIDEO_H = 1280
ZOOM_AMOUNT = 0.22          # how much each shot zooms in over its duration
OVERSAMPLE = 2              # zoompan works on a 2x image to reduce jitter

# ---------- caption settings ----------
CAPTION_FONT = "DejaVu Sans"
CAPTION_SIZE = 78
CAPTION_OUTLINE = 6
CAPTION_MARGIN_V = 330      # distance from bottom edge, px
CAPTION_UPPERCASE = True

# ---------- music ----------
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/app/music")
MUSIC_VOLUME = 0.12

JOBS_ROOT = os.environ.get("JOBS_ROOT", "/tmp/jobs")
SCENE_TIMEOUT = 480
FINALIZE_TIMEOUT = 600

ffmpeg_lock = threading.Lock()


class FFmpegError(Exception):
    pass


def run_ffmpeg(args, timeout):
    cmd = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostats', '-y'] + args
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        raise FFmpegError(result.stderr[-2000:])


def get_duration(path):
    result = subprocess.run([
        'ffprobe', '-v', 'error', '-show_entries', 'format=duration',
        '-of', 'json', path
    ], capture_output=True, text=True, check=True, timeout=30)
    return float(json.loads(result.stdout)['format']['duration'])


def safe_remove(*paths):
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def job_dir(job_id):
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', str(job_id)):
        raise ValueError(f"bad job_id: {job_id!r}")
    d = f"{JOBS_ROOT}/{job_id}"
    os.makedirs(d, exist_ok=True)
    return d


# ---------- timing helpers ----------

def char_times(scene_text, alignment, duration):
    """Return start time (seconds) for every character of scene_text.
    Uses ElevenLabs alignment when it matches the text, otherwise spreads
    characters evenly over the audio duration."""
    n = len(scene_text)
    if alignment:
        chars = alignment.get('characters') or []
        starts = alignment.get('character_start_times_seconds') or []
        if chars and len(chars) == len(starts):
            if ''.join(chars) == scene_text:
                return starts
            # lengths differ (e.g. normalization): map proportionally
            m = len(starts)
            return [starts[min(int(i * m / max(n, 1)), m - 1)] for i in range(n)]
    return [duration * i / max(n, 1) for i in range(n)]


def shot_boundaries(shot_texts, times, duration):
    """Start/end seconds of every shot inside the scene audio."""
    starts = []
    offset = 0
    for i, t in enumerate(shot_texts):
        starts.append(0.0 if i == 0 else times[min(offset, len(times) - 1)])
        offset += len(t) + 1  # +1 for the joining space
    ends = starts[1:] + [duration]
    return list(zip(starts, ends))


def word_timings(scene_text, times, alignment, duration):
    """List of (word, start, end) for captions."""
    ends_by_char = None
    if alignment:
        e = alignment.get('character_end_times_seconds') or []
        if len(e) == len(scene_text):
            ends_by_char = e
    words = []
    for m in re.finditer(r'\S+', scene_text):
        clean = re.sub(r'^[^\w%$#]+|[^\w%$#]+$', '', m.group(0))
        if not clean:
            continue
        start = times[m.start()]
        end = ends_by_char[m.end() - 1] if ends_by_char else None
        words.append([clean, start, end])
    for i, w in enumerate(words):
        nxt = words[i + 1][1] if i + 1 < len(words) else duration
        # keep the word on screen until the next one starts
        w[2] = max(nxt, w[2] or 0) if i + 1 < len(words) else duration
        if w[2] <= w[1]:
            w[2] = w[1] + 0.2
    return words


def ass_time(t):
    t = max(t, 0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def write_ass(path, words):
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {VIDEO_W}
PlayResY: {VIDEO_H}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{CAPTION_FONT},{CAPTION_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,{CAPTION_OUTLINE},2,2,40,40,{CAPTION_MARGIN_V},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    for word, start, end in words:
        text = word.upper() if CAPTION_UPPERCASE else word
        text = text.replace('{', '').replace('}', '')
        pop = r"{\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{pop}{text}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header + "\n".join(lines) + "\n")


# ---------- routes ----------

@app.route('/', methods=['GET', 'HEAD'])
def health():
    return jsonify({"status": "ok"})


@app.route('/add_image', methods=['POST'])
def add_image():
    data = request.get_json(force=True)
    try:
        work_dir = job_dir(data['job_id'])
        scene = int(data['scene_index'])
        shot = int(data['shot_index'])
        raw = base64.b64decode(data['image_base64'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400

    path = f"{work_dir}/img_{scene}_{shot}.jpg"
    tmp = path + ".tmp"
    with open(tmp, 'wb') as f:
        f.write(raw)
    os.replace(tmp, path)
    return jsonify({"status": "ok", "scene_index": scene, "shot_index": shot, "bytes": len(raw)})


@app.route('/add_scene', methods=['POST'])
def add_scene():
    """Body: job_id, scene_index, total_scenes, shot_texts (list, in order),
    audio_base64, alignment (ElevenLabs 'alignment' object, optional)."""
    data = request.get_json(force=True)
    try:
        job_id = data['job_id']
        work_dir = job_dir(job_id)
        index = int(data['scene_index'])
        total = int(data['total_scenes'])
        shot_texts = [str(t).strip() for t in data['shot_texts']]
        alignment = data.get('alignment')
        audio_raw = base64.b64decode(data['audio_base64'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    del data

    clip_path = f"{work_dir}/clip_{index}.mp4"
    if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
        return jsonify({"status": "ok", "job_id": job_id, "index": index, "cached": True})

    img_paths = [f"{work_dir}/img_{index}_{i}.jpg" for i in range(len(shot_texts))]
    missing = [i for i, p in enumerate(img_paths) if not os.path.exists(p)]
    if missing:
        return jsonify({"error": "missing images", "scene_index": index, "missing_shots": missing}), 400

    audio_path = f"{work_dir}/audio_{index}.mp3"
    ass_path = f"{work_dir}/subs_{index}.ass"
    tmp_clip = f"{work_dir}/clip_{index}.tmp.mp4"
    with open(audio_path, 'wb') as f:
        f.write(audio_raw)
    del audio_raw

    try:
        duration = get_duration(audio_path)
        scene_text = ' '.join(shot_texts)
        times = char_times(scene_text, alignment, duration)
        bounds = shot_boundaries(shot_texts, times, duration)
        write_ass(ass_path, word_timings(scene_text, times, alignment, duration))

        # frame counts per shot, summing exactly to the audio length
        total_frames = max(int(round(duration * FPS)), 1)
        frames, acc = [], 0
        for i, (s, e) in enumerate(bounds):
            if i == len(bounds) - 1:
                n = total_frames - acc
            else:
                n = int(round(e * FPS)) - acc
            n = max(n, 1)
            frames.append(n)
            acc += n

        big_w, big_h = VIDEO_W * OVERSAMPLE, VIDEO_H * OVERSAMPLE
        inputs, chains = [], []
        for i, (img, n) in enumerate(zip(img_paths, frames)):
            inputs += ['-i', img]
            focus_y = 0.5 if i % 2 == 0 else 0.42
            z = f"1+{ZOOM_AMOUNT}*on/{n}"
            chains.append(
                f"[{i}:v]scale={big_w}:{big_h}:force_original_aspect_ratio=increase,"
                f"crop={big_w}:{big_h},setsar=1,"
                f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih*{focus_y}-(ih/zoom*{focus_y})'"
                f":d={n}:s={VIDEO_W}x{VIDEO_H}:fps={FPS},setpts=PTS-STARTPTS[v{i}]"
            )
        n_shots = len(img_paths)
        concat_in = ''.join(f"[v{i}]" for i in range(n_shots))
        chains.append(f"{concat_in}concat=n={n_shots}:v=1:a=0[vc]")
        chains.append(f"[vc]subtitles={ass_path}:fontsdir=/usr/share/fonts,format=yuv420p[vout]")

        args = inputs + [
            '-i', audio_path,
            '-filter_complex', ';'.join(chains),
            '-map', '[vout]', '-map', f'{n_shots}:a',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '21', '-threads', '2',
            '-r', str(FPS),
            '-c:a', 'aac', '-b:a', '128k', '-ar', '44100', '-ac', '1',
            '-t', f"{duration:.3f}",
            '-movflags', '+faststart',
            tmp_clip,
        ]

        log.info(f"[{job_id}] scene {index}/{total - 1}: waiting for lock")
        with ffmpeg_lock:
            log.info(f"[{job_id}] scene {index}: {n_shots} shots, {duration:.1f}s")
            run_ffmpeg(args, timeout=SCENE_TIMEOUT)
        os.replace(tmp_clip, clip_path)
        log.info(f"[{job_id}] scene {index}: done")

    except subprocess.TimeoutExpired:
        safe_remove(tmp_clip)
        log.error(f"[{job_id}] scene {index}: timeout")
        return jsonify({"error": "ffmpeg timeout", "index": index}), 504
    except (FFmpegError, subprocess.CalledProcessError) as e:
        safe_remove(tmp_clip)
        log.error(f"[{job_id}] scene {index}: {e}")
        return jsonify({"error": "ffmpeg failed", "index": index, "detail": str(e)}), 500
    finally:
        safe_remove(audio_path)

    for p in img_paths:
        safe_remove(p)
    return jsonify({"status": "ok", "job_id": job_id, "index": index,
                    "duration": round(duration, 2), "shots": len(img_paths)})


@app.route('/finalize', methods=['POST'])
def finalize():
    data = request.get_json(force=True)
    try:
        job_id = data['job_id']
        work_dir = job_dir(job_id)
        total = int(data['total_scenes'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400

    clip_paths = [f"{work_dir}/clip_{i}.mp4" for i in range(total)]
    missing = [i for i, p in enumerate(clip_paths)
               if not os.path.exists(p) or os.path.getsize(p) == 0]
    if missing:
        return jsonify({"error": "missing clips", "missing_indexes": missing}), 400

    concat_list = f"{work_dir}/concat.txt"
    with open(concat_list, 'w') as f:
        for cp in clip_paths:
            f.write(f"file '{cp}'\n")

    joined = f"{work_dir}/joined.mp4"
    output = f"{work_dir}/output.mp4"
    tracks = sorted(glob.glob(f"{MUSIC_DIR}/*.mp3"))

    try:
        with ffmpeg_lock:
            run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', concat_list,
                        '-c', 'copy', joined], timeout=FINALIZE_TIMEOUT)
            if tracks:
                music = random.choice(tracks)
                dur = get_duration(joined)
                fade_st = max(dur - 2, 0)
                run_ffmpeg([
                    '-i', joined, '-stream_loop', '-1', '-i', music,
                    '-filter_complex',
                    f"[1:a]volume={MUSIC_VOLUME},afade=t=out:st={fade_st:.2f}:d=2[m];"
                    f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                    '-map', '0:v', '-map', '[a]',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
                    '-t', f"{dur:.3f}", '-movflags', '+faststart', output
                ], timeout=FINALIZE_TIMEOUT)
                log.info(f"[{job_id}] music: {os.path.basename(music)}")
            else:
                os.replace(joined, output)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "finalize timeout"}), 504
    except FFmpegError as e:
        log.error(f"[{job_id}] finalize: {e}")
        return jsonify({"error": "finalize failed", "detail": str(e)}), 500

    log.info(f"[{job_id}] final video {os.path.getsize(output) / 1e6:.1f} MB")
    response = send_file(output, mimetype='video/mp4')

    @response.call_on_close
    def cleanup():
        shutil.rmtree(work_dir, ignore_errors=True)

    return response


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
