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
import time

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# ---------- video settings ----------
FPS = 25
VIDEO_W = 1080
VIDEO_H = 1920
ZOOM_AMOUNT = 0.22          # how much each shot zooms in over its duration
OVERSAMPLE = 1.5            # zoompan works on an upscaled image to reduce jitter

# ---------- caption settings ----------
# Caption sizes are defined on a 720x1280 canvas; libass scales them to the real video size.
CAPTION_CANVAS_W = 720
CAPTION_CANVAS_H = 1280
CAPTION_FONT = "DejaVu Sans"
CAPTION_SIZE = 78
CAPTION_OUTLINE = 6
CAPTION_MARGIN_V = 330      # distance from bottom edge, px
CAPTION_UPPERCASE = True

# ---------- music ----------
MUSIC_DIR = os.environ.get("MUSIC_DIR", "/app/music")
MUSIC_VOLUME = 0.12

# ---------- audio trim ----------
LEAD_PAD = 0.05             # keep this much silence before the first word
TAIL_PAD = 0.15             # keep this much after the last word

JOBS_ROOT = os.environ.get("JOBS_ROOT", "/tmp/jobs")

# ---------- long videos ----------
# DATA_DIR should be a Render persistent disk, otherwise the library is lost on redeploy.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SHORTS_DIR = f"{DATA_DIR}/shorts"
COMPILES_DIR = f"{DATA_DIR}/compiles"
LIBRARY_KEEP_DAYS = 45
LONG_W = 1920
LONG_H = 1080
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
PlayResX: {CAPTION_CANVAS_W}
PlayResY: {CAPTION_CANVAS_H}
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

        # Trim dead air at the start/end of the scene using ElevenLabs timings,
        # so scenes join without pauses between them.
        audio_offset = 0.0
        al_s = (alignment or {}).get('character_start_times_seconds') or []
        al_e = (alignment or {}).get('character_end_times_seconds') or []
        if al_s and al_e and len(al_s) == len(al_e):
            audio_offset = max(min(al_s) - LEAD_PAD, 0.0)
            speech_end = min(max(al_e) + TAIL_PAD, duration)
            if speech_end - audio_offset > 0.5:
                alignment = dict(alignment)
                alignment['character_start_times_seconds'] = [max(t - audio_offset, 0) for t in al_s]
                alignment['character_end_times_seconds'] = [max(t - audio_offset, 0) for t in al_e]
                duration = speech_end - audio_offset
            else:
                audio_offset = 0.0
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

        big_w, big_h = int(VIDEO_W * OVERSAMPLE) // 2 * 2, int(VIDEO_H * OVERSAMPLE) // 2 * 2
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
            '-ss', f"{audio_offset:.3f}", '-i', audio_path,
            '-filter_complex', ';'.join(chains),
            '-map', '[vout]', '-map', f'{n_shots}:a',
            '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '19', '-threads', '2',
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
    # Music: a track sent with the request wins; otherwise pick from the library.
    music_file = None
    if data.get('music_base64'):
        try:
            music_file = f"{work_dir}/music.mp3"
            with open(music_file, 'wb') as f:
                f.write(base64.b64decode(data['music_base64']))
        except (ValueError, TypeError):
            music_file = None

    mood = re.sub(r'[^a-z_\-]', '', str(data.get('music_mood') or '').lower())
    tracks = sorted(glob.glob(f"{MUSIC_DIR}/{mood}/*.mp3")) if mood else []
    if not tracks:
        tracks = sorted(glob.glob(f"{MUSIC_DIR}/**/*.mp3", recursive=True))
    if music_file:
        tracks = [music_file]

    try:
        with ffmpeg_lock:
            run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', concat_list,
                        '-c', 'copy', joined], timeout=FINALIZE_TIMEOUT)
            if tracks:
                music = random.choice(tracks)
                dur = get_duration(joined)
                fade_st = max(dur - 2, 0)
                try:
                  run_ffmpeg([
                    '-i', joined, '-stream_loop', '-1', '-i', music,
                    '-filter_complex',
                    f"[1:a]volume={MUSIC_VOLUME},afade=t=in:st=0:d=1,afade=t=out:st={fade_st:.2f}:d=2[m];"
                    f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                    '-map', '0:v', '-map', '[a]',
                    '-c:v', 'copy', '-c:a', 'aac', '-b:a', '160k',
                    '-t', f"{dur:.3f}", '-movflags', '+faststart', output
                  ], timeout=FINALIZE_TIMEOUT)
                except FFmpegError as e:
                    # a broken music file must not cost us the whole video
                    log.error(f"[{job_id}] music mix failed, publishing without music: {e}")
                    os.replace(joined, output)
                    music = None
                if music:
                  log.info(f"[{job_id}] music: " + ("generated" if music == music_file else f"library ({mood or 'any'}) {os.path.relpath(music, MUSIC_DIR)}"))
            else:
                os.replace(joined, output)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "finalize timeout"}), 504
    except FFmpegError as e:
        log.error(f"[{job_id}] finalize: {e}")
        return jsonify({"error": "finalize failed", "detail": str(e)}), 500

    log.info(f"[{job_id}] final video {os.path.getsize(output) / 1e6:.1f} MB")
    try:
        library_save(job_id, output, data)
    except Exception as e:
        log.error(f"[{job_id}] library save failed: {e}")

    response = send_file(output, mimetype='video/mp4')

    @response.call_on_close
    def cleanup():
        shutil.rmtree(work_dir, ignore_errors=True)

    return response


# =====================================================================
# Library of published Shorts (kept on the persistent disk)
# =====================================================================

def library_save(job_id, video_path, data):
    os.makedirs(SHORTS_DIR, exist_ok=True)
    shutil.copyfile(video_path, f"{SHORTS_DIR}/{job_id}.mp4")
    meta = {
        "id": job_id,
        "title": str(data.get('title') or ''),
        "topic": str(data.get('topic') or ''),
        "created": time.time(),
        "duration": round(get_duration(video_path), 2),
        "used": False,
    }
    with open(f"{SHORTS_DIR}/{job_id}.json", 'w') as f:
        json.dump(meta, f)
    library_cleanup()
    log.info(f"[{job_id}] saved to library")


def library_items():
    items = []
    for p in glob.glob(f"{SHORTS_DIR}/*.json"):
        try:
            with open(p) as f:
                m = json.load(f)
            m['has_video'] = os.path.exists(f"{SHORTS_DIR}/{m['id']}.mp4")
            items.append(m)
        except Exception:
            pass
    return sorted(items, key=lambda m: m.get('created', 0))


def library_cleanup():
    cutoff = time.time() - LIBRARY_KEEP_DAYS * 86400
    for m in library_items():
        if m.get('created', 0) < cutoff:
            safe_remove(f"{SHORTS_DIR}/{m['id']}.mp4", f"{SHORTS_DIR}/{m['id']}.json")


@app.route('/library', methods=['GET'])
def library():
    items = [m for m in library_items() if m['has_video']]
    unused = [m for m in items if not m.get('used')]
    return jsonify({"total": len(items), "unused": len(unused), "items": items})


# =====================================================================
# Weekly long video from stored Shorts
# =====================================================================

COMPILES = {}          # compile_id -> status dict (in memory)
compile_lock = threading.Lock()


def long_frame_filter(src, out, still=False):
    """Vertical source -> 16:9: sharp video in the middle, blurred copy behind."""
    return (
        f"[{src}]split[bgs][fgs];"
        f"[bgs]scale=384:216:force_original_aspect_ratio=increase,crop=384:216,"
        f"boxblur=8:2,scale={LONG_W}:{LONG_H},eq=brightness=-0.18:saturation=0.8,setsar=1[bg];"
        f"[fgs]scale=-2:{LONG_H},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:0,format=yuv420p[{out}]"
    )


def ass_escape(t):
    return str(t).replace('\\', '').replace('{', '').replace('}', '').replace('\n', ' ')


def write_long_ass(path, words=None, side_title=None, side_number=None, duration=0):
    """Captions and the chapter label for the 16:9 video (1920x1080 canvas)."""
    left_w = (LONG_W - int(LONG_H * 9 / 16)) // 2   # blurred area on each side
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {LONG_W}
PlayResY: {LONG_H}
WrapStyle: 0
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Word,{CAPTION_FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,40,40,170,1
Style: Num,{CAPTION_FONT},150,&H0000D7FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,7,70,{LONG_W - left_w + 40},330,1
Style: Side,{CAPTION_FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,7,70,{LONG_W - left_w + 40},510,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = []
    if side_title:
        end = ass_time(max(duration, 1))
        if side_number is not None:
            lines.append(f"Dialogue: 0,{ass_time(0)},{end},Num,,0,0,0,,{{\\fad(400,0)}}{int(side_number):02d}")
        lines.append(f"Dialogue: 0,{ass_time(0)},{end},Side,,0,0,0,,{{\\fad(400,0)}}{ass_escape(side_title).upper()}")
    for word, start, stop in (words or []):
        pop = r"{\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(stop)},Word,,0,0,0,,{pop}{ass_escape(word).upper()}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(header + "\n".join(lines) + "\n")


def encode_args(out, duration=None):
    args = ['-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20', '-threads', '2',
            '-r', str(FPS), '-pix_fmt', 'yuv420p',
            '-c:a', 'aac', '-b:a', '192k', '-ar', '44100', '-ac', '2']
    if duration:
        args += ['-t', f"{duration:.3f}"]
    return args + ['-movflags', '+faststart', out]


def build_chapter(src_video, out, number, title, fade_in=True):
    dur = get_duration(src_video)
    ass = out.replace('.mp4', '.ass')
    write_long_ass(ass, side_title=title, side_number=number, duration=dur)
    vf = long_frame_filter('0:v', 'framed') + f";[framed]subtitles={ass}:fontsdir=/usr/share/fonts[vout]"
    af = "[0:a]aformat=sample_rates=44100:channel_layouts=stereo" + (",afade=t=in:st=0:d=0.15" if fade_in else "") + "[aout]"
    with ffmpeg_lock:
        run_ffmpeg(['-i', src_video, '-filter_complex', vf + ';' + af,
                    '-map', '[vout]', '-map', '[aout]'] + encode_args(out, dur), timeout=1800)
    return dur


def build_narrated(work, name, audio_b64, alignment, text, frames):
    """Intro/outro: narration over stills taken from the Shorts, with word captions."""
    audio = f"{work}/{name}.mp3"
    with open(audio, 'wb') as f:
        f.write(base64.b64decode(audio_b64))
    duration = get_duration(audio)
    text = str(text or '').strip()
    words = []
    if text:
        times = char_times(text, alignment, duration)
        words = word_timings(text, times, alignment, duration)
    ass = f"{work}/{name}.ass"
    write_long_ass(ass, words=words)

    frames = frames or []
    n = max(len(frames), 1)
    per = duration / n
    inputs, chains = [], []
    for i, fr in enumerate(frames):
        d = per if i < n - 1 else duration - per * (n - 1)
        frames_n = max(int(round(d * FPS)), 1)
        inputs += ['-i', fr]
        chains.append(
            f"[{i}:v]scale={LONG_W * 3 // 2}:{LONG_H * 3 // 2}:force_original_aspect_ratio=increase,"
            f"crop={LONG_W * 3 // 2}:{LONG_H * 3 // 2},setsar=1,"
            f"zoompan=z='1+0.12*on/{frames_n}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
            f":d={frames_n}:s={LONG_W}x{LONG_H}:fps={FPS},eq=brightness=-0.08,setpts=PTS-STARTPTS[s{i}]"
        )
    concat_in = ''.join(f"[s{i}]" for i in range(len(frames)))
    chains.append(f"{concat_in}concat=n={len(frames)}:v=1:a=0[vc]")
    chains.append(f"[vc]subtitles={ass}:fontsdir=/usr/share/fonts,format=yuv420p[vout]")
    chains.append(f"[{len(frames)}:a]aformat=sample_rates=44100:channel_layouts=stereo[aout]")
    out = f"{work}/{name}.mp4"
    with ffmpeg_lock:
        run_ffmpeg(inputs + ['-i', audio, '-filter_complex', ';'.join(chains),
                             '-map', '[vout]', '-map', '[aout]'] + encode_args(out, duration),
                   timeout=1800)
    return out, duration


def grab_frame(video, out, at=1.5):
    with ffmpeg_lock:
        run_ffmpeg(['-ss', f"{at}", '-i', video, '-frames:v', '1', '-q:v', '3', out], timeout=60)
    return out


def set_status(cid, **kw):
    st = COMPILES.setdefault(cid, {"compile_id": cid})
    st.update(kw)
    st['updated'] = time.time()
    os.makedirs(f"{COMPILES_DIR}/{cid}", exist_ok=True)
    with open(f"{COMPILES_DIR}/{cid}/status.json", 'w') as f:
        json.dump(st, f)


def run_compile(cid, payload):
    work = f"{COMPILES_DIR}/{cid}"
    try:
        items = payload['items']
        total_steps = len(items) + (1 if payload.get('intro') else 0) + (1 if payload.get('outro') else 0) + 1
        step = 0
        set_status(cid, status='running', progress=f"0/{total_steps}")

        videos = [f"{SHORTS_DIR}/{it['id']}.mp4" for it in items]
        missing = [it['id'] for it, v in zip(items, videos) if not os.path.exists(v)]
        if missing:
            raise RuntimeError(f"missing shorts: {missing}")

        frames = [grab_frame(v, f"{work}/frame_{i}.jpg") for i, v in enumerate(videos)]
        segments, chapters, t = [], [], 0.0

        intro = payload.get('intro')
        if intro and intro.get('audio_base64'):
            seg, d = build_narrated(work, 'intro', intro['audio_base64'], intro.get('alignment'),
                                    intro.get('text'), frames)
            segments.append(seg)
            chapters.append({"title": intro.get('chapter_title') or 'Intro', "start": 0.0})
            t += d
            step += 1
            set_status(cid, progress=f"{step}/{total_steps}")

        for i, (it, v) in enumerate(zip(items, videos)):
            out = f"{work}/ch_{i}.mp4"
            title = it.get('chapter_title') or it.get('title') or f"Part {i + 1}"
            d = build_chapter(v, out, i + 1, title)
            segments.append(out)
            chapters.append({"title": title, "start": round(t, 2)})
            t += d
            step += 1
            set_status(cid, progress=f"{step}/{total_steps}")

        outro = payload.get('outro')
        if outro and outro.get('audio_base64'):
            seg, d = build_narrated(work, 'outro', outro['audio_base64'], outro.get('alignment'),
                                    outro.get('text'), list(reversed(frames))[:4])
            segments.append(seg)
            chapters.append({"title": outro.get('chapter_title') or 'Outro', "start": round(t, 2)})
            t += d
            step += 1

        concat_list = f"{work}/concat.txt"
        with open(concat_list, 'w') as f:
            for sgm in segments:
                f.write(f"file '{sgm}'\n")
        final = f"{work}/long.mp4"
        with ffmpeg_lock:
            run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', concat_list, '-c', 'copy',
                        '-movflags', '+faststart', final], timeout=1800)

        for sgm in segments:
            safe_remove(sgm, sgm.replace('.mp4', '.ass'))
        for fr in frames:
            safe_remove(fr)

        # mark the Shorts as used and free the disk
        for it in items:
            mp = f"{SHORTS_DIR}/{it['id']}.json"
            try:
                with open(mp) as f:
                    m = json.load(f)
                m['used'] = True
                with open(mp, 'w') as f:
                    json.dump(m, f)
            except Exception:
                pass
            safe_remove(f"{SHORTS_DIR}/{it['id']}.mp4")

        set_status(cid, status='done', progress=f"{total_steps}/{total_steps}",
                   duration=round(get_duration(final), 2), chapters=chapters,
                   size_mb=round(os.path.getsize(final) / 1e6, 1))
        log.info(f"[{cid}] long video done: {t:.0f}s, {len(chapters)} chapters")
    except Exception as e:
        log.error(f"[{cid}] compile failed: {e}")
        set_status(cid, status='failed', error=str(e)[-1500:])


@app.route('/compile', methods=['POST'])
def compile_start():
    """Body: compile_id, items [{id, chapter_title}], intro/outro {audio_base64, alignment, text}.
    Returns at once; poll GET /compile/<id>."""
    payload = request.get_json(force=True)
    cid = str(payload.get('compile_id') or '')
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', cid):
        return jsonify({"error": "bad compile_id"}), 400
    if not payload.get('items'):
        return jsonify({"error": "no items"}), 400
    with compile_lock:
        running = [c for c, st in COMPILES.items() if st.get('status') == 'running']
        if cid in COMPILES and COMPILES[cid].get('status') in ('running', 'done'):
            return jsonify(COMPILES[cid]), 202
        if running:
            return jsonify({"error": "another compile is running", "running": running}), 409
        # remove old compile folders
        for d in glob.glob(f"{COMPILES_DIR}/*"):
            if os.path.isdir(d) and time.time() - os.path.getmtime(d) > 7 * 86400:
                shutil.rmtree(d, ignore_errors=True)
        set_status(cid, status='queued', items=len(payload['items']))
        threading.Thread(target=run_compile, args=(cid, payload), daemon=True).start()
    return jsonify(COMPILES[cid]), 202


@app.route('/compile/<cid>', methods=['GET'])
def compile_status(cid):
    if cid in COMPILES:
        return jsonify(COMPILES[cid])
    p = f"{COMPILES_DIR}/{cid}/status.json"
    if os.path.exists(p):
        with open(p) as f:
            st = json.load(f)
        if st.get('status') in ('queued', 'running'):
            st['status'] = 'failed'
            st['error'] = 'server restarted during the compile'
        return jsonify(st)
    return jsonify({"error": "unknown compile_id"}), 404


@app.route('/compile/<cid>/video', methods=['GET'])
def compile_video(cid):
    p = f"{COMPILES_DIR}/{cid}/long.mp4"
    if not re.fullmatch(r'[A-Za-z0-9_\-]+', cid) or not os.path.exists(p):
        return jsonify({"error": "not ready"}), 404
    return send_file(p, mimetype='video/mp4')


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
