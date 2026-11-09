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
import requests

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
log = app.logger

# =====================================================================
# Settings
# =====================================================================

# ---------- shorts (9:16) ----------
FPS = 25
VIDEO_W = 1080
VIDEO_H = 1920
ZOOM_AMOUNT = 0.22          # how much each shot zooms in over its duration
OVERSAMPLE = 1.5            # zoompan works on an upscaled image to reduce jitter

# Caption sizes are defined on a 720x1280 canvas; libass scales them to the real video size.
CAPTION_CANVAS_W = 720
CAPTION_CANVAS_H = 1280
CAPTION_FONT = "DejaVu Sans"
CAPTION_SIZE = 78
CAPTION_OUTLINE = 6
CAPTION_MARGIN_V = 330      # distance from bottom edge, px
CAPTION_SIDE_MARGIN = 40    # left and right margin, px
CAPTION_MIN_SIZE = 34       # long words are shrunk to fit the frame, but not below this
CAPTION_UPPERCASE = True
CAPTION_FONT_FILE = os.environ.get("CAPTION_FONT_FILE", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

MUSIC_DIR = os.environ.get("MUSIC_DIR", "/app/music")
MUSIC_VOLUME = 0.12

HIGHLIGHT_COLOR = "&H0000D7FF"   # ASS is BGR: this is gold/yellow
SFX_DIR = os.environ.get("SFX_DIR", "/app/sfx")
SFX_VOLUME = 0.35               # whoosh on every cut; hit on the very first frame

LEAD_PAD = 0.05             # keep this much silence before the first word
TAIL_PAD = 0.15             # keep this much after the last word

JOBS_ROOT = os.environ.get("JOBS_ROOT", "/tmp/jobs")

# ---------- storage ----------
# DATA_DIR should be a Render persistent disk, otherwise the library is lost on redeploy.
DATA_DIR = os.environ.get("DATA_DIR", "/data")
SHORTS_DIR = f"{DATA_DIR}/shorts"
COMPILES_DIR = f"{DATA_DIR}/compiles"
IDEAS_DIR = f"{DATA_DIR}/ideas"
LIBRARY_KEEP_DAYS = 45
COMPILE_KEEP_DAYS = int(os.environ.get("COMPILE_KEEP_DAYS", "5"))   # old compile/long folders are purged on start
MIN_FREE_GB = float(os.environ.get("MIN_FREE_GB", "3"))             # refuse to start a long video below this
STALE_JOB_SECONDS = int(os.environ.get("STALE_JOB_SECONDS", str(3 * 3600)))  # a "running" job silent this long is dead

# ---------- long videos (16:9) ----------
LONG_W = 1920
LONG_H = 1080
SCENE_TIMEOUT = 480
FINALIZE_TIMEOUT = 600

OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-2.5-flare")
IMG_INTERVAL = float(os.environ.get("IMG_INTERVAL", "13"))   # seconds between image requests (rate limit)
LONG_MOVE = 0.06              # slow zoom for long videos (0 = still images)
LONG_OVERSAMPLE = 4           # canvas multiplier used only when LONG_MOVE == 0 fallback scaling is not used
LONG_MUSIC_VOLUME = 0.08
LONG_SUB_WORDS = 7            # words per subtitle line

THUMB_W, THUMB_H = 1280, 720

# ---------- topic ideas ----------
IDEA_KINDS = ('short', 'long')
IDEA_QUEUE_MAX = 60
IDEA_DONE_MAX = 300

ID_RE = re.compile(r'[A-Za-z0-9_\-]+')
CHANNEL_RE = re.compile(r'[a-z0-9_\-]{1,30}')

ffmpeg_lock = threading.Lock()
compile_lock = threading.Lock()
ideas_lock = threading.Lock()
COMPILES = {}          # compile_id -> status dict (in memory)


# =====================================================================
# Helpers
# =====================================================================

class FFmpegError(Exception):
    pass


def valid_id(value):
    return bool(ID_RE.fullmatch(str(value)))


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


def dir_size(path):
    total = 0
    for root, _, files in os.walk(path):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    return total


def free_gb(path=None):
    p = path or DATA_DIR
    try:
        return shutil.disk_usage(p).free / 1024 ** 3
    except OSError:
        return 999.0


def job_dir(job_id):
    if not valid_id(job_id):
        raise ValueError(f"bad job_id: {job_id!r}")
    d = f"{JOBS_ROOT}/{job_id}"
    os.makedirs(d, exist_ok=True)
    return d


def ass_doc(width, height, styles, events, wrap=0):
    """Build an ASS subtitle file. styles and events are lists of ready lines."""
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {width}\n"
        f"PlayResY: {height}\n"
        f"WrapStyle: {wrap}\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        + "\n".join(styles) + "\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
        + "\n".join(events) + "\n"
    )


def ass_time(t):
    t = max(t, 0)
    h = int(t // 3600)
    m = int(t % 3600 // 60)
    s = t % 60
    return f"{h}:{m:02d}:{s:05.2f}"


def ass_escape(t):
    return str(t).replace('\\', '').replace('{', '').replace('}', '').replace('\n', ' ')


# ---------- timing helpers ----------

def char_times(scene_text, alignment, duration):
    """Start time (seconds) for every character of scene_text.
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


def frame_counts(bounds, duration):
    """Frames per shot, summing exactly to the audio length."""
    total_frames = max(int(round(duration * FPS)), 1)
    frames, acc = [], 0
    for i, (_, e) in enumerate(bounds):
        n = total_frames - acc if i == len(bounds) - 1 else int(round(e * FPS)) - acc
        n = max(n, 1)
        frames.append(n)
        acc += n
    return frames


def word_timings(scene_text, times, alignment, duration):
    """List of [word, start, end] for captions."""
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


_font_cache = {}


def text_width(text, size):
    """Width in caption-canvas px of `text` in DejaVu Sans Bold at ASS font size `size`.
    libass treats the size as the line height (about 1.164 em for DejaVu), so the real
    em size is size / 1.164."""
    em = size / 1.164
    try:
        if 'f' not in _font_cache:
            from PIL import ImageFont
            _font_cache['f'] = ImageFont.truetype(CAPTION_FONT_FILE, 100)
        return _font_cache['f'].getlength(text) * em / 100
    except Exception:
        return len(text) * 0.80 * em       # rough estimate for capital letters


def fit_caption_size(text, pop_scale):
    """Largest font size (up to CAPTION_SIZE) at which the word fits between the side margins,
    also at the start of the pop animation. Long German compounds need this."""
    avail = CAPTION_CANVAS_W - 2 * CAPTION_SIDE_MARGIN - 2 * CAPTION_OUTLINE
    w = text_width(text, CAPTION_SIZE) * pop_scale
    if w <= avail:
        return CAPTION_SIZE
    size = int(CAPTION_SIZE * avail / w)
    return max(size, CAPTION_MIN_SIZE)


def write_ass(path, words, highlight=None):
    style = (f"Style: Word,{CAPTION_FONT},{CAPTION_SIZE},&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,"
             f"-1,0,0,0,100,100,0,0,1,{CAPTION_OUTLINE},2,2,{CAPTION_SIDE_MARGIN},{CAPTION_SIDE_MARGIN},{CAPTION_MARGIN_V},1")
    norm = lambda w: re.sub(r'[^\w%$#]+', '', str(w)).lower()
    hl = {norm(w) for w in (highlight or []) if norm(w)}
    events = []
    for word, start, end in words:
        text = word.upper() if CAPTION_UPPERCASE else word
        text = text.replace('{', '').replace('}', '')
        if norm(word) in hl:
            size = fit_caption_size(text, 1.30)
            pop = r"{\c" + HIGHLIGHT_COLOR + r"&\fscx130\fscy130\t(0,110,\fscx108\fscy108)}"
        else:
            size = fit_caption_size(text, 1.18)
            pop = r"{\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"
        if size < CAPTION_SIZE:
            pop = pop[:-1] + r"\fs" + str(size) + "}"
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Word,,0,0,0,,{pop}{text}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(ass_doc(CAPTION_CANVAS_W, CAPTION_CANVAS_H, [style], events, wrap=2))


# ---------- status of compile / long jobs ----------

def load_status(cid):
    """Status from memory, or from disk after a server restart."""
    if cid in COMPILES:
        return COMPILES[cid]
    p = f"{COMPILES_DIR}/{cid}/status.json"
    if os.path.exists(p):
        try:
            with open(p) as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def set_status(cid, **kw):
    """Update memory first, then try to persist. A full disk must never stop
    a job from being marked failed, otherwise it stays 'running' forever."""
    if cid not in COMPILES:
        COMPILES[cid] = load_status(cid) or {"compile_id": cid}
    st = COMPILES[cid]
    st.update(kw)
    st['updated'] = time.time()
    try:
        d = f"{COMPILES_DIR}/{cid}"
        os.makedirs(d, exist_ok=True)
        tmp = f"{d}/status.json.tmp"
        with open(tmp, 'w') as f:
            json.dump(st, f)
        os.replace(tmp, f"{d}/status.json")
    except OSError as e:
        log.error(f"[{cid}] cannot write status.json: {e}")


def running_jobs():
    """Ids of jobs that are really running. A job silent for STALE_JOB_SECONDS is marked failed."""
    now = time.time()
    alive = []
    for cid, st in COMPILES.items():
        if st.get('status') not in ('running', 'queued'):
            continue
        if now - st.get('updated', now) > STALE_JOB_SECONDS:
            log.error(f"[{cid}] no progress for {STALE_JOB_SECONDS}s, marking as failed")
            st['status'] = 'failed'
            st['error'] = 'stale: no progress, probably killed'
        else:
            alive.append(cid)
    return alive


def purge_old_compiles(keep=()):
    """Delete compile folders older than COMPILE_KEEP_DAYS (never an active job)."""
    active = set(running_jobs()) | set(keep)
    cutoff = time.time() - COMPILE_KEEP_DAYS * 86400
    for d in glob.glob(f"{COMPILES_DIR}/*"):
        name = os.path.basename(d)
        if name in active or not os.path.isdir(d):
            continue
        if os.path.getmtime(d) < cutoff:
            shutil.rmtree(d, ignore_errors=True)
            COMPILES.pop(name, None)
            log.info(f"purged old folder {name}")


# =====================================================================
# Routes: health, disk
# =====================================================================

@app.route('/', methods=['GET', 'HEAD'])
def health():
    return jsonify({"status": "ok"})


@app.route('/disk', methods=['GET'])
def disk():
    """Free space and the biggest folders. Handy for a weekly Telegram alert."""
    usage = shutil.disk_usage(DATA_DIR)
    folders = []
    for d in glob.glob(f"{COMPILES_DIR}/*"):
        if os.path.isdir(d):
            folders.append({"id": os.path.basename(d), "mb": round(dir_size(d) / 1048576)})
    folders.sort(key=lambda x: -x['mb'])
    return jsonify({
        "free_gb": round(usage.free / 1024 ** 3, 2),
        "total_gb": round(usage.total / 1024 ** 3, 2),
        "shorts_mb": round(dir_size(SHORTS_DIR) / 1048576) if os.path.isdir(SHORTS_DIR) else 0,
        "compiles": folders[:20],
        "running": running_jobs(),
    })


# =====================================================================
# Routes: Shorts rendering
# =====================================================================

@app.route('/add_image', methods=['POST'])
def add_image():
    data = request.get_json(force=True)
    try:
        work_dir = job_dir(data['job_id'])
        scene = int(data['scene_index'])
        shot = int(data['shot_index'])
        b64 = data.get('image_base64') or ''
        raw = base64.b64decode(b64) if b64 else b''
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400

    if len(raw) < 1000:
        # the image failed upstream; /add_scene will use a neighbouring frame
        return jsonify({"status": "skipped", "scene_index": scene, "shot_index": shot})

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
        highlight = data.get('highlight_words') or []
        audio_raw = base64.b64decode(data['audio_base64'])
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    del data

    clip_path = f"{work_dir}/clip_{index}.mp4"
    if os.path.exists(clip_path) and os.path.getsize(clip_path) > 0:
        return jsonify({"status": "ok", "job_id": job_id, "index": index, "cached": True})

    img_paths = [f"{work_dir}/img_{index}_{i}.jpg" for i in range(len(shot_texts))]
    # Fallback: a missing frame borrows the nearest frame of the same scene,
    # or the last frame of the previous scene if the whole scene is missing.
    have = [i for i, p in enumerate(img_paths) if os.path.exists(p)]
    last_frame = f"{work_dir}/last_frame.jpg"
    fallback_shots = []
    if len(have) < len(img_paths):
        for i, p in enumerate(img_paths):
            if os.path.exists(p):
                continue
            if have:
                src = img_paths[min(have, key=lambda h: (abs(h - i), h > i))]
            elif os.path.exists(last_frame):
                src = last_frame
            else:
                return jsonify({"error": "missing images", "scene_index": index,
                                "missing_shots": list(range(len(img_paths)))}), 400
            shutil.copyfile(src, p)
            fallback_shots.append(i)
        log.warning(f"[{job_id}] scene {index}: fallback frames for shots {fallback_shots}")

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
        write_ass(ass_path, word_timings(scene_text, times, alignment, duration), highlight)
        frames = frame_counts(bounds, duration)

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

        # voice + optional sound effects
        audio_inputs = ['-ss', f"{audio_offset:.3f}", '-i', audio_path]
        a_idx = n_shots
        chains.append(f"[{a_idx}:a]aformat=sample_rates=44100:channel_layouts=mono[voice]")
        mix = ['[voice]']
        whoosh = f"{SFX_DIR}/whoosh.mp3"
        hit = f"{SFX_DIR}/hit.mp3"
        cuts = [s_ for (s_, e_) in bounds[1:] if s_ > 0.2]
        next_idx = a_idx + 1
        if os.path.exists(whoosh) and cuts:
            audio_inputs += ['-i', whoosh]
            if len(cuts) > 1:
                labels = ''.join(f"[w{k}]" for k in range(len(cuts)))
                chains.append(f"[{next_idx}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                              f"volume={SFX_VOLUME},asplit={len(cuts)}{labels}")
            else:
                chains.append(f"[{next_idx}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                              f"volume={SFX_VOLUME}[w0]")
            for k, t in enumerate(cuts):
                ms = int(max(t - 0.12, 0) * 1000)
                chains.append(f"[w{k}]adelay=delays={ms}:all=1[wd{k}]")
                mix.append(f"[wd{k}]")
            next_idx += 1
        if os.path.exists(hit) and index == 0:
            audio_inputs += ['-i', hit]
            chains.append(f"[{next_idx}:a]aformat=sample_rates=44100:channel_layouts=mono,"
                          f"volume={SFX_VOLUME * 1.3:.2f}[hd]")
            mix.append('[hd]')
            next_idx += 1
        if len(mix) > 1:
            chains.append(f"{''.join(mix)}amix=inputs={len(mix)}:duration=first:"
                          f"dropout_transition=0:normalize=0[aout]")
        else:
            chains.append("[voice]anull[aout]")

        args = inputs + audio_inputs + [
            '-filter_complex', ';'.join(chains),
            '-map', '[vout]', '-map', '[aout]',
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

    try:
        shutil.copyfile(img_paths[-1], last_frame)   # for a later scene that has no images at all
    except OSError:
        pass
    for p in img_paths:
        safe_remove(p)
    return jsonify({"status": "ok", "job_id": job_id, "index": index,
                    "duration": round(duration, 2), "shots": len(img_paths),
                    "fallback_shots": fallback_shots})


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
            music = None
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
            else:
                os.replace(joined, output)
            if music:
                src = "generated" if music == music_file else \
                    f"library ({mood or 'any'}) {os.path.relpath(music, MUSIC_DIR)}"
                log.info(f"[{job_id}] music: {src}")
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
        "channel": re.sub(r'[^a-z0-9_\-]', '', str(data.get('channel') or 'cosmic').lower()) or 'cosmic',
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
    """?channel=cosmic returns only that channel; Shorts saved before channels existed count as cosmic."""
    channel = (request.args.get('channel') or '').lower()
    items = [m for m in library_items() if m['has_video']]
    if channel:
        items = [m for m in items if (m.get('channel') or 'cosmic') == channel]
    unused = [m for m in items if not m.get('used')]
    return jsonify({"total": len(items), "unused": len(unused), "items": items})


@app.route('/library/<job_id>/discard', methods=['POST'])
def library_discard(job_id):
    """Remove a Short from the library, e.g. when it was rejected in Telegram."""
    if not valid_id(job_id):
        return jsonify({"error": "bad id"}), 400
    existed = os.path.exists(f"{SHORTS_DIR}/{job_id}.json")
    safe_remove(f"{SHORTS_DIR}/{job_id}.mp4", f"{SHORTS_DIR}/{job_id}.json")
    log.info(f"[{job_id}] removed from library")
    return jsonify({"status": "ok", "removed": existed})


# =====================================================================
# Weekly long video compiled from stored Shorts
# =====================================================================

def long_frame_filter(src, out):
    """Vertical source -> 16:9: sharp video in the middle, blurred copy behind."""
    return (
        f"[{src}]split[bgs][fgs];"
        f"[bgs]scale=384:216:force_original_aspect_ratio=increase,crop=384:216,"
        f"boxblur=8:2,scale={LONG_W}:{LONG_H},eq=brightness=-0.18:saturation=0.8,setsar=1[bg];"
        f"[fgs]scale=-2:{LONG_H},setsar=1[fg];"
        f"[bg][fg]overlay=(W-w)/2:0,format=yuv420p[{out}]"
    )


def write_long_ass(path, words=None, side_title=None, side_number=None, duration=0):
    """Captions and the chapter label for the 16:9 compile (1920x1080 canvas)."""
    left_w = (LONG_W - int(LONG_H * 9 / 16)) // 2   # blurred area on each side
    right_margin = LONG_W - left_w + 40
    styles = [
        f"Style: Word,{CAPTION_FONT},92,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,7,2,2,40,40,170,1",
        f"Style: Num,{CAPTION_FONT},150,&H0000D7FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,6,2,7,70,{right_margin},330,1",
        f"Style: Side,{CAPTION_FONT},58,&H00FFFFFF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,100,100,0,0,1,5,2,7,70,{right_margin},510,1",
    ]
    events = []
    if side_title:
        end = ass_time(max(duration, 1))
        if side_number is not None:
            events.append(f"Dialogue: 0,{ass_time(0)},{end},Num,,0,0,0,,{{\\fad(400,0)}}{int(side_number):02d}")
        events.append(f"Dialogue: 0,{ass_time(0)},{end},Side,,0,0,0,,{{\\fad(400,0)}}{ass_escape(side_title).upper()}")
    for word, start, stop in (words or []):
        pop = r"{\fscx118\fscy118\t(0,90,\fscx100\fscy100)}"
        events.append(f"Dialogue: 0,{ass_time(start)},{ass_time(stop)},Word,,0,0,0,,{pop}{ass_escape(word).upper()}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(ass_doc(LONG_W, LONG_H, styles, events, wrap=0))


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
    af = "[0:a]aformat=sample_rates=44100:channel_layouts=stereo" + \
         (",afade=t=in:st=0:d=0.15" if fade_in else "") + "[aout]"
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
    if not valid_id(cid):
        return jsonify({"error": "bad compile_id"}), 400
    if not payload.get('items'):
        return jsonify({"error": "no items"}), 400
    with compile_lock:
        running = running_jobs()
        if cid in COMPILES and COMPILES[cid].get('status') in ('running', 'done'):
            return jsonify(COMPILES[cid]), 202
        if running:
            return jsonify({"error": "another compile is running", "running": running}), 409
        purge_old_compiles(keep=(cid,))
        set_status(cid, status='queued', items=len(payload['items']))
        threading.Thread(target=run_compile, args=(cid, payload), daemon=True).start()
    return jsonify(COMPILES[cid]), 202


@app.route('/compile/<cid>', methods=['GET'])
def compile_status(cid):
    if cid in COMPILES:
        return jsonify(COMPILES[cid])
    if valid_id(cid):
        st = load_status(cid)
        if st:
            if st.get('status') in ('queued', 'running'):
                st['status'] = 'failed'
                st['error'] = 'server restarted during the job'
            return jsonify(st)
    return jsonify({"error": "unknown compile_id"}), 404


@app.route('/compile/<cid>/video', methods=['GET'])
def compile_video(cid):
    p = f"{COMPILES_DIR}/{cid}/long.mp4"
    if not valid_id(cid) or not os.path.exists(p):
        return jsonify({"error": "not ready"}), 404
    return send_file(p, mimetype='video/mp4')


@app.route('/cleanup/<cid>', methods=['POST'])
def cleanup_job(cid):
    """Delete the folder of a finished job. Refuses while the job runs, and refuses
    if the video never reached YouTube (add ?force=1 to override)."""
    if not valid_id(cid):
        return jsonify({"error": "bad id"}), 400
    work = f"{COMPILES_DIR}/{cid}"
    if not os.path.isdir(work):
        return jsonify({"ok": True, "freed_mb": 0, "note": "not found"})
    with compile_lock:
        if cid in running_jobs():
            return jsonify({"error": "job is running"}), 409
        st = load_status(cid)
        if not st.get('youtube_id') and request.args.get('force') != '1':
            return jsonify({"error": "not uploaded to youtube yet"}), 409
        size = dir_size(work)
        shutil.rmtree(work, ignore_errors=True)
        COMPILES.pop(cid, None)
    log.info(f"[{cid}] cleaned up, freed {size / 1048576:.0f} MB")
    return jsonify({"ok": True, "freed_mb": round(size / 1048576)})


# =====================================================================
# YouTube: upload from this server (a 10-minute 1080p file is too big
# to pass through n8n Cloud memory)
# Env vars on Render per channel:
#   cosmic (default):  YT_CLIENT_ID, YT_CLIENT_SECRET, YT_REFRESH_TOKEN
#   other channels:    YT_CLIENT_ID_<CHANNEL>, YT_CLIENT_SECRET_<CHANNEL>, YT_REFRESH_TOKEN_<CHANNEL>
# =====================================================================

# The German channel is the former Cosmic Unknown channel: same Google account, so it uses the
# default (unsuffixed) YT_* variables. Add other aliases here if a channel shares an account.
CHANNEL_ALIASES = {'german': 'cosmic'}


def yt_env(name, channel=None):
    if channel:
        channel = CHANNEL_ALIASES.get(str(channel).lower(), channel)
    if channel and str(channel).lower() != 'cosmic':
        key = f"{name}_{str(channel).upper()}"
        v = os.environ.get(key)
        if not v:
            # never fall back to another channel's account: that uploads to the wrong channel
            raise RuntimeError(f"{key} is not set on the server")
        return v
    return os.environ[name]


def yt_access_token(channel=None):
    r = requests.post('https://oauth2.googleapis.com/token', data={
        'client_id': yt_env('YT_CLIENT_ID', channel),
        'client_secret': yt_env('YT_CLIENT_SECRET', channel),
        'refresh_token': yt_env('YT_REFRESH_TOKEN', channel),
        'grant_type': 'refresh_token',
    }, timeout=30)
    r.raise_for_status()
    return r.json()['access_token']


@app.route('/compile/<cid>/youtube', methods=['POST'])
def compile_youtube(cid):
    """Body: title, description, tags (list or comma string), privacy (public/private/unlisted),
    channel (optional), language (optional, default en; use de for German)."""
    if not valid_id(cid):
        return jsonify({"error": "bad compile_id"}), 400
    path = f"{COMPILES_DIR}/{cid}/long.mp4"
    if not os.path.exists(path):
        return jsonify({"error": "video not ready"}), 404

    st = load_status(cid)
    if st.get('youtube_id'):
        # already uploaded: do not upload twice on a retry
        return jsonify({"status": "ok", "youtube_id": st['youtube_id'], "cached": True})

    data = request.get_json(force=True)
    tags = data.get('tags') or []
    if isinstance(tags, str):
        tags = [t.strip() for t in tags.split(',') if t.strip()]
    kept, total = [], 0
    for t in tags:
        total += len(t) + 1
        if total > 450:
            break
        kept.append(t)

    lang = re.sub(r'[^A-Za-z\-]', '', str(data.get('language') or st.get('language') or 'en'))[:8] or 'en'
    meta = {
        "snippet": {
            "title": str(data.get('title') or 'Untitled')[:100],
            "description": str(data.get('description') or '')[:4900],
            "tags": kept,
            "categoryId": str(data.get('category_id') or '28'),
            "defaultLanguage": lang,
            "defaultAudioLanguage": lang,
        },
        "status": {
            "privacyStatus": data.get('privacy') or 'public',
            "selfDeclaredMadeForKids": False,
        },
    }
    size = os.path.getsize(path)
    try:
        token = yt_access_token(data.get('channel') or st.get('channel'))
        init = requests.post(
            'https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status',
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json; charset=UTF-8',
                'X-Upload-Content-Type': 'video/mp4',
                'X-Upload-Content-Length': str(size),
            },
            json=meta, timeout=60)
        if init.status_code >= 300:
            return jsonify({"error": "youtube init failed", "detail": init.text[-1500:]}), 502
        location = init.headers['Location']
        with open(path, 'rb') as f:
            put = requests.put(location, data=f, headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'video/mp4',
                'Content-Length': str(size),
            }, timeout=1800)
        if put.status_code >= 300:
            return jsonify({"error": "youtube upload failed", "detail": put.text[-1500:]}), 502
        vid = put.json().get('id')
    except (KeyError, RuntimeError) as e:
        return jsonify({"error": f"missing env var or header: {e}"}), 500
    except requests.RequestException as e:
        return jsonify({"error": "youtube request error", "detail": str(e)[-1500:]}), 502

    set_status(cid, youtube_id=vid)
    log.info(f"[{cid}] uploaded to YouTube: {vid}")
    return jsonify({"status": "ok", "youtube_id": vid, "url": f"https://youtu.be/{vid}"})


@app.route('/youtube/<channel>/publish', methods=['POST'])
def youtube_publish(channel):
    """Body: video_id, privacy (public by default). Used by the Telegram control bot."""
    if not CHANNEL_RE.fullmatch(channel):
        return jsonify({"error": "bad channel"}), 400
    body = request.get_json(silent=True) or {}
    vid = str(body.get('video_id') or '')
    privacy = body.get('privacy') or 'public'
    if not re.fullmatch(r'[A-Za-z0-9_\-]{11}', vid) or privacy not in ('public', 'unlisted', 'private'):
        return jsonify({"error": "bad video_id or privacy"}), 400
    try:
        token = yt_access_token(channel)
    except Exception as e:
        return jsonify({"error": f"no access for channel: {e}"}), 500
    h = {'Authorization': 'Bearer ' + token}
    r = requests.get('https://www.googleapis.com/youtube/v3/videos',
                     params={'part': 'status', 'id': vid}, headers=h, timeout=30)
    items = (r.json().get('items') or []) if r.ok else []
    if not items:
        return jsonify({"error": "video not found on this channel", "detail": r.text[:300]}), 404
    old = items[0].get('status') or {}
    # send back only the editable fields, otherwise YouTube resets the ones we leave out
    status = {k: old[k] for k in ('embeddable', 'license', 'publicStatsViewable', 'selfDeclaredMadeForKids')
              if k in old}
    status['privacyStatus'] = privacy
    u = requests.put('https://www.googleapis.com/youtube/v3/videos', params={'part': 'status'},
                     headers=h, json={'id': vid, 'status': status}, timeout=30)
    if not u.ok:
        return jsonify({"error": "youtube refused", "detail": u.text[:300]}), 502
    return jsonify({"ok": True, "video_id": vid, "privacy": privacy})


# =====================================================================
# Thumbnail for the long video: image + big text, then set it on YouTube
# =====================================================================

def compose_thumbnail(src, out, text):
    ass = out.replace('.jpg', '.ass')
    words = ass_escape(text).upper().strip()
    style = (f"Style: T,{CAPTION_FONT},118,&H0000D7FF,&H00FFFFFF,&H00000000,&H96000000,"
             f"-1,0,0,0,100,100,1,0,1,10,5,4,60,560,0,1")
    event = f"Dialogue: 0,0:00:00.00,0:00:05.00,T,,0,0,0,,{words}"
    with open(ass, 'w', encoding='utf-8') as f:
        f.write(ass_doc(THUMB_W, THUMB_H, [style], [event], wrap=0))
    # soft dark gradient on the left so the text reads on any image
    fc = (f"[0:v]scale={THUMB_W}:{THUMB_H}:force_original_aspect_ratio=increase,crop={THUMB_W}:{THUMB_H},"
          f"eq=saturation=1.15:contrast=1.08,format=rgba[base];"
          f"color=black:s={THUMB_W}x{THUMB_H},format=rgba,"
          f"geq=r=0:g=0:b=0:a='190*max(0\\,1-X/(W*0.7))'[shade];"
          f"[base][shade]overlay=0:0,subtitles={ass}:fontsdir=/usr/share/fonts,format=yuvj420p[out]")
    with ffmpeg_lock:
        run_ffmpeg(['-i', src, '-filter_complex', fc, '-map', '[out]', '-frames:v', '1', '-q:v', '2', out],
                   timeout=60)
    safe_remove(ass)
    return out


@app.route('/compile/<cid>/thumbnail', methods=['POST'])
def compile_thumbnail(cid):
    """Body: image_base64, text, video_id (optional, defaults to the uploaded long video)."""
    if not valid_id(cid):
        return jsonify({"error": "bad compile_id"}), 400
    work = f"{COMPILES_DIR}/{cid}"
    os.makedirs(work, exist_ok=True)
    data = request.get_json(force=True)

    st = load_status(cid)
    video_id = data.get('video_id') or st.get('youtube_id')
    if not video_id:
        return jsonify({"error": "no video id: upload the video first"}), 400

    src = f"{work}/thumb_src.jpg"
    out = f"{work}/thumb.jpg"
    try:
        with open(src, 'wb') as f:
            f.write(base64.b64decode(data['image_base64']))
        compose_thumbnail(src, out, data.get('text') or '')
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({"error": f"bad request: {e}"}), 400
    except FFmpegError as e:
        return jsonify({"error": "thumbnail render failed", "detail": str(e)}), 500

    try:
        token = yt_access_token(data.get('channel') or st.get('channel'))
        with open(out, 'rb') as f:
            r = requests.post(
                f'https://www.googleapis.com/upload/youtube/v3/thumbnails/set?videoId={video_id}&uploadType=media',
                headers={'Authorization': f'Bearer {token}', 'Content-Type': 'image/jpeg'},
                data=f.read(), timeout=120)
        if r.status_code >= 300:
            return jsonify({"error": "youtube thumbnail failed", "detail": r.text[-1500:]}), 502
    except (KeyError, RuntimeError) as e:
        return jsonify({"error": f"missing env var: {e}"}), 500
    except requests.RequestException as e:
        return jsonify({"error": "youtube request error", "detail": str(e)[-1500:]}), 502

    set_status(cid, thumbnail_set=True)
    log.info(f"[{cid}] thumbnail set on {video_id}")
    return jsonify({"status": "ok", "video_id": video_id})


@app.route('/compile/<cid>/thumbnail.jpg', methods=['GET'])
def compile_thumbnail_preview(cid):
    p = f"{COMPILES_DIR}/{cid}/thumb.jpg"
    if not valid_id(cid) or not os.path.exists(p):
        return jsonify({"error": "no thumbnail"}), 404
    return send_file(p, mimetype='image/jpeg')


# =====================================================================
# Original long videos (16:9): the server generates the images and the
# voiceover itself, so n8n only sends the script. Uses the compile
# folders and status, so /compile/<id>, /compile/<id>/youtube and
# /compile/<id>/thumbnail work for these jobs too.
# Env: OPENAI_API_KEY, ELEVEN_API_KEY_LONG (or ELEVEN_API_KEY)
# =====================================================================

def http_retry(fn, tries=5, wait=20, what="request"):
    last = None
    for i in range(tries):
        try:
            r = fn()
            if r.status_code < 300:
                return r
            last = f"{r.status_code} {r.text[-400:]}"
            if r.status_code not in (408, 409, 429, 500, 502, 503, 504):
                break
        except requests.RequestException as e:
            last = str(e)
        time.sleep(wait * (i + 1))
    raise RuntimeError(f"{what} failed: {last}")


def gen_image(prompt, quality, out):
    key = os.environ['OPENAI_API_KEY']
    r = http_retry(lambda: requests.post(
        'https://api.openai.com/v1/images/generations',
        headers={'Authorization': f'Bearer {key}'},
        json={'model': OPENAI_IMAGE_MODEL, 'prompt': prompt, 'size': '1536x1024',
              'quality': quality, 'output_format': 'jpeg', 'output_compression': 85, 'n': 1},
        timeout=240), what="image")
    b64 = r.json()['data'][0]['b64_json']
    with open(out + '.tmp', 'wb') as f:
        f.write(base64.b64decode(b64))
    os.replace(out + '.tmp', out)


def eleven_key():
    return os.environ.get('ELEVEN_API_KEY_LONG') or os.environ['ELEVEN_API_KEY']


BAD_SUBSCRIPTION = ('past_due', 'unpaid', 'incomplete', 'incomplete_expired', 'canceled')


def eleven_preflight(chapters):
    """Check the ElevenLabs account BEFORE any image is paid for. Returns None when all is fine,
    otherwise (json, http_code). A failed check on our side (timeout, no key) does not block."""
    need = sum(len(str(s.get('text') or '')) + 1 for c in chapters for s in c.get('shots', []))
    try:
        r = requests.get('https://api.elevenlabs.io/v1/user/subscription',
                         headers={'xi-api-key': eleven_key()}, timeout=20)
    except (requests.RequestException, KeyError):
        return None
    if r.status_code in (401, 402):
        return {"error": "elevenlabs refused the key: unpaid invoice or wrong key",
                "detail": r.text[-300:]}, 402
    if r.status_code >= 300:
        return None
    try:
        js = r.json()
    except ValueError:
        return None
    if js.get('status') in BAD_SUBSCRIPTION:
        return {"error": f"elevenlabs subscription is {js.get('status')}: pay the invoice first"}, 402
    left = (js.get('character_limit') or 0) - (js.get('character_count') or 0)
    if js.get('character_limit') and left < need:
        return {"error": "not enough elevenlabs characters for this script",
                "need": need, "left": left}, 402
    return None


def gen_voice(text, voice_id, model_id, out_mp3, out_align):
    r = http_retry(lambda: requests.post(
        f'https://api.elevenlabs.io/v1/text-to-speech/{voice_id}/with-timestamps',
        headers={'xi-api-key': eleven_key()},
        json={'text': text, 'model_id': model_id,
              'voice_settings': {'stability': 0.5, 'similarity_boost': 0.75}},
        timeout=300), what="voiceover")
    js = r.json()
    with open(out_mp3, 'wb') as f:
        f.write(base64.b64decode(js['audio_base64']))
    with open(out_align, 'w') as f:
        json.dump(js.get('alignment') or {}, f)


def gen_music(prompt, out_mp3, length_ms=90000):
    r = http_retry(lambda: requests.post(
        'https://api.elevenlabs.io/v1/music?output_format=mp3_44100_128',
        headers={'xi-api-key': eleven_key()},
        json={'prompt': prompt, 'music_length_ms': length_ms, 'model_id': 'music_v2',
              'force_instrumental': True},
        timeout=600), tries=3, what="music")
    with open(out_mp3, 'wb') as f:
        f.write(r.content)


def write_long_subs(path, words):
    """Sentence-style subtitles at the bottom of a 16:9 frame."""
    style = (f"Style: Sub,{CAPTION_FONT},54,&H00FFFFFF,&H00FFFFFF,&H00000000,&H78000000,"
             f"-1,0,0,0,100,100,0,0,1,4,2,2,160,160,70,1")
    events = []
    for i in range(0, len(words), LONG_SUB_WORDS):
        chunk = words[i:i + LONG_SUB_WORDS]
        text = ' '.join(ass_escape(w[0]) for w in chunk)
        events.append(f"Dialogue: 0,{ass_time(chunk[0][1])},{ass_time(chunk[-1][2])},Sub,,0,0,0,,{text}")
    with open(path, 'w', encoding='utf-8') as f:
        f.write(ass_doc(LONG_W, LONG_H, [style], events, wrap=0))


def render_zoom_shot(img_path, n, out, zoom_in=True):
    """Perfectly smooth zoom: every frame is resampled from the source image with
    sub-pixel precision (Pillow), so there is no stair-step shake like zoompan."""
    from PIL import Image
    im = Image.open(img_path).convert('RGB')
    sw, sh = im.size
    base = max(LONG_W / sw, LONG_H / sh)       # cover the 16:9 frame
    cx, cy = sw / 2, sh / 2
    p = subprocess.Popen(
        ['ffmpeg', '-v', 'error', '-y', '-f', 'rawvideo', '-pix_fmt', 'rgb24',
         '-s', f'{LONG_W}x{LONG_H}', '-r', str(FPS), '-i', '-',
         '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '17',
         '-pix_fmt', 'yuv420p', '-threads', '2', out],
        stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for k in range(n):
            t = k / max(n - 1, 1)
            z = 1 + LONG_MOVE * (t if zoom_in else 1 - t)
            a = 1 / (base * z)
            fr = im.transform((LONG_W, LONG_H), Image.AFFINE,
                              (a, 0, cx - LONG_W / 2 * a, 0, a, cy - LONG_H / 2 * a),
                              resample=Image.BILINEAR)
            p.stdin.write(fr.tobytes())
        p.stdin.close()
        err = p.stderr.read().decode(errors='ignore')
        if p.wait(timeout=600) != 0:
            raise FFmpegError(err[-1500:])
    except BrokenPipeError:
        raise FFmpegError(p.stderr.read().decode(errors='ignore')[-1500:])


def render_long_chapter(work, ci, shot_texts, audio, alignment, out):
    duration = get_duration(audio)
    text = ' '.join(shot_texts)
    times = char_times(text, alignment, duration)
    bounds = shot_boundaries(shot_texts, times, duration)
    ass = f"{work}/ch_{ci}.ass"
    write_long_subs(ass, word_timings(text, times, alignment, duration))
    frames = frame_counts(bounds, duration)

    # Each shot is rendered on its own, then the shots are joined and the voice
    # and subtitles are added in one final pass. Keeps memory low.
    shot_files = []
    with ffmpeg_lock:
        for i, n in enumerate(frames):
            img = f"{work}/img_{ci}_{i}.jpg"
            sf = f"{work}/shot_{ci}_{i}.mp4"
            if LONG_MOVE > 0:
                render_zoom_shot(img, n, sf, zoom_in=(i % 2 == 0))
            else:   # still image, no motion at all
                run_ffmpeg([
                    '-loop', '1', '-framerate', str(FPS), '-i', img,
                    '-vf', f"scale={LONG_W}:{LONG_H}:force_original_aspect_ratio=increase:flags=lanczos,"
                           f"crop={LONG_W}:{LONG_H},setsar=1,fps={FPS},format=yuv420p",
                    '-frames:v', str(n), '-an', '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '17',
                    '-threads', '2', sf], timeout=900)
            shot_files.append(sf)

        lst = f"{work}/shots_{ci}.txt"
        with open(lst, 'w') as f:
            for sf in shot_files:
                f.write(f"file '{sf}'\n")
        silent = f"{work}/silent_{ci}.mp4"
        run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', silent], timeout=600)

        run_ffmpeg(['-i', silent, '-i', audio,
                    '-filter_complex',
                    f"[0:v]subtitles={ass}:fontsdir=/usr/share/fonts,format=yuv420p[vout];"
                    f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo[aout]",
                    '-map', '[vout]', '-map', '[aout]'] + encode_args(out, duration),
                   timeout=3600)
    safe_remove(silent, lst, *shot_files)
    return duration


def fill_missing_images(work, chapters):
    """A failed image borrows the nearest one of the same chapter (or the previous chapter)."""
    last = None
    for ci, ch in enumerate(chapters):
        paths = [f"{work}/img_{ci}_{i}.jpg" for i in range(len(ch['shots']))]
        have = [i for i, p in enumerate(paths) if os.path.exists(p)]
        for i, p in enumerate(paths):
            if os.path.exists(p):
                continue
            src = paths[min(have, key=lambda h: (abs(h - i), h > i))] if have else last
            if not src:
                raise RuntimeError(f"no images at all for chapter {ci}")
            shutil.copyfile(src, p)
        last = paths[-1]


def run_long(cid, payload):
    work = f"{COMPILES_DIR}/{cid}"
    try:
        chapters = payload['chapters']
        style = str(payload.get('style') or '').strip()
        q_first = payload.get('image_quality_first') or 'medium'
        q_rest = payload.get('image_quality') or 'low'
        n_img = sum(len(c['shots']) for c in chapters)

        # 1. images (skips files that already exist, so a retry continues where it stopped)
        done = 0
        for ci, ch in enumerate(chapters):
            for si, sh in enumerate(ch['shots']):
                p = f"{work}/img_{ci}_{si}.jpg"
                if not os.path.exists(p):
                    prompt = '. '.join(x for x in [style, str(sh.get('image_prompt') or sh.get('text'))] if x)
                    try:
                        gen_image(prompt, q_first if ci == 0 else q_rest, p)
                    except Exception as e:
                        log.error(f"[{cid}] image {ci}/{si} failed: {e}")
                    time.sleep(IMG_INTERVAL)
                done += 1
                set_status(cid, status='running', progress=f"images {done}/{n_img}")
        fill_missing_images(work, chapters)

        # 2. voiceover per chapter
        voice = payload['voice_id']
        model = payload.get('voice_model') or 'eleven_multilingual_v2'
        for ci, ch in enumerate(chapters):
            mp3, al = f"{work}/voice_{ci}.mp3", f"{work}/voice_{ci}.json"
            if not os.path.exists(mp3):
                text = ' '.join(str(s['text']).strip() for s in ch['shots'])
                gen_voice(text, voice, model, mp3, al)
            set_status(cid, progress=f"voice {ci + 1}/{len(chapters)}")

        # 3. music (optional)
        music = f"{work}/music.mp3"
        if payload.get('music_prompt') and not os.path.exists(music):
            try:
                gen_music(payload['music_prompt'], music)
            except Exception as e:
                log.error(f"[{cid}] music failed, continuing without: {e}")

        # 4. render chapters
        segments, chapters_out, t = [], [], 0.0
        for ci, ch in enumerate(chapters):
            out = f"{work}/chapter_{ci}.mp4"
            texts = [str(s['text']).strip() for s in ch['shots']]
            with open(f"{work}/voice_{ci}.json") as f:
                al = json.load(f)
            if os.path.exists(out) and os.path.getsize(out) > 0:
                d = get_duration(out)
            else:
                d = render_long_chapter(work, ci, texts, f"{work}/voice_{ci}.mp3", al, out + '.tmp.mp4')
                os.replace(out + '.tmp.mp4', out)
            segments.append(out)
            chapters_out.append({"title": ch.get('title') or f"Part {ci + 1}", "start": round(t, 2)})
            t += d
            set_status(cid, progress=f"render {ci + 1}/{len(chapters)}")

        # 5. join, add music
        concat_list = f"{work}/concat.txt"
        with open(concat_list, 'w') as f:
            for sgm in segments:
                f.write(f"file '{sgm}'\n")
        joined, final = f"{work}/joined.mp4", f"{work}/long.mp4"
        with ffmpeg_lock:
            run_ffmpeg(['-f', 'concat', '-safe', '0', '-i', concat_list, '-c', 'copy', joined], timeout=1800)
            if os.path.exists(music) and os.path.getsize(music) > 1000:
                dur = get_duration(joined)
                try:
                    run_ffmpeg([
                        '-i', joined, '-stream_loop', '-1', '-i', music,
                        '-filter_complex',
                        f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo,volume={LONG_MUSIC_VOLUME},"
                        f"afade=t=in:st=0:d=2,afade=t=out:st={max(dur - 3, 0):.2f}:d=3[m];"
                        f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[a]",
                        '-map', '0:v', '-map', '[a]', '-c:v', 'copy', '-c:a', 'aac', '-b:a', '192k',
                        '-t', f"{dur:.3f}", '-movflags', '+faststart', final], timeout=1800)
                    safe_remove(joined)
                except FFmpegError as e:
                    log.error(f"[{cid}] music mix failed: {e}")
                    os.replace(joined, final)
            else:
                os.replace(joined, final)

        # free disk: everything except the final video, the status, the timings and the thumbnail
        for sgm in segments:
            safe_remove(sgm)
        for p in glob.glob(f"{work}/ch_*.ass") + glob.glob(f"{work}/img_*.jpg"):
            safe_remove(p)

        set_status(cid, status='done', progress='done', chapters=chapters_out,
                   duration=round(get_duration(final), 2),
                   size_mb=round(os.path.getsize(final) / 1e6, 1))
        log.info(f"[{cid}] long video done: {t:.0f}s, {len(chapters)} chapters")
    except Exception as e:
        log.error(f"[{cid}] long video failed: {e}")
        set_status(cid, status='failed', error=str(e)[-1500:])


@app.route('/long/start', methods=['POST'])
def long_start():
    """Body: job_id, channel, voice_id, voice_model, style, music_prompt,
    image_quality_first, image_quality, chapters [{title, shots [{text, image_prompt}]}].
    Returns at once; poll GET /compile/<job_id>. Re-posting the same job_id resumes it."""
    payload = request.get_json(force=True)
    cid = str(payload.get('job_id') or '')
    if not valid_id(cid):
        return jsonify({"error": "bad job_id"}), 400
    chapters = payload.get('chapters') or []
    if not chapters or not all(c.get('shots') for c in chapters):
        return jsonify({"error": "chapters with shots are required"}), 400
    if not payload.get('voice_id'):
        return jsonify({"error": "voice_id is required"}), 400

    # fail before the images are generated if the voice cannot be paid for (see eleven_preflight)
    already = (COMPILES.get(cid) or {}).get('status') in ('running', 'done')
    if not already:
        bad = eleven_preflight(chapters)
        if bad:
            log.error(f"[{cid}] preflight: {bad[0]}")
            return jsonify(bad[0]), bad[1]

    with compile_lock:
        st = COMPILES.get(cid) or {}
        if st.get('status') in ('running', 'done'):
            return jsonify(st), 202
        running = running_jobs()
        if running:
            return jsonify({"error": "another job is running", "running": running}), 409
        try:
            purge_old_compiles(keep=(cid,))
            free = free_gb()
            if free < MIN_FREE_GB:
                return jsonify({"error": "not enough disk space", "free_gb": round(free, 2),
                                "need_gb": MIN_FREE_GB}), 507
            os.makedirs(f"{COMPILES_DIR}/{cid}", exist_ok=True)
            set_status(cid, status='running', kind='long', channel=payload.get('channel'),
                       language=payload.get('language'), progress='starting', error=None)
            threading.Thread(target=run_long, args=(cid, payload), daemon=True).start()
        except Exception as e:
            COMPILES[cid] = {"compile_id": cid, "status": "failed", "error": str(e)[-500:],
                             "updated": time.time()}
            return jsonify({"error": "cannot start", "detail": str(e)[-500:]}), 500
    return jsonify(COMPILES[cid]), 202


# =====================================================================
# Topic ideas queue (filled weekly by the Competitor Radar workflow)
# =====================================================================

def _ideas_file(channel, kind):
    return f"{IDEAS_DIR}/{channel}_{kind}.json"


def _ideas_load(channel, kind):
    try:
        with open(_ideas_file(channel, kind)) as f:
            d = json.load(f)
        return {"queue": list(d.get("queue") or []), "done": list(d.get("done") or [])}
    except Exception:
        return {"queue": [], "done": []}


def _ideas_save(channel, kind, d):
    os.makedirs(IDEAS_DIR, exist_ok=True)
    tmp = _ideas_file(channel, kind) + ".tmp"
    with open(tmp, "w") as f:
        json.dump(d, f, ensure_ascii=False)
    os.replace(tmp, _ideas_file(channel, kind))


@app.route('/ideas/<channel>', methods=['GET', 'POST'])
def ideas(channel):
    if not CHANNEL_RE.fullmatch(channel):
        return jsonify({"error": "bad channel"}), 400
    if request.method == 'GET':
        return jsonify({k: _ideas_load(channel, k)["queue"] for k in IDEA_KINDS})
    body = request.get_json(silent=True) or {}
    kind = body.get('kind')
    if kind not in IDEA_KINDS:
        return jsonify({"error": "kind must be short or long"}), 400
    fresh = []
    for x in body.get('ideas') or []:
        if isinstance(x, dict) and isinstance(x.get('topic'), str) and len(x['topic'].strip()) >= 20:
            fresh.append({
                "topic": x['topic'].strip()[:400],
                "format": str(x.get('format') or '')[:20],
                "why": str(x.get('why') or '')[:300],
            })
    front = bool(body.get('front'))   # owner topics jump the queue and ignore the "already used" list
    with ideas_lock:
        d = _ideas_load(channel, kind)
        seen = {q["topic"].lower() for q in d["queue"]}
        if not front:
            seen |= {t.lower() for t in d["done"]}
        take = []
        for n in fresh:
            if n["topic"].lower() not in seen:
                take.append(n)
                seen.add(n["topic"].lower())
        if front:
            d["queue"] = (take + d["queue"])[:IDEA_QUEUE_MAX]
        else:
            d["queue"] = (d["queue"] + take)[-IDEA_QUEUE_MAX:]
        _ideas_save(channel, kind, d)
    return jsonify({"ok": True, "added": len(take), "queued": len(d["queue"])})


@app.route('/ideas/<channel>/next', methods=['POST'])
def ideas_next(channel):
    if not CHANNEL_RE.fullmatch(channel):
        return jsonify({"error": "bad channel"}), 400
    kind = request.args.get('kind')
    if kind not in IDEA_KINDS:
        return jsonify({"error": "kind must be short or long"}), 400
    with ideas_lock:
        d = _ideas_load(channel, kind)
        idea = d["queue"].pop(0) if d["queue"] else None
        if idea:
            d["done"] = (d["done"] + [idea["topic"]])[-IDEA_DONE_MAX:]
            _ideas_save(channel, kind, d)
    return jsonify({"idea": idea, "left": len(d["queue"])})


# =====================================================================
# Subtitles (.srt) for a finished long video
# =====================================================================

def build_long_srt(work):
    """.srt built from the ElevenLabs timings of each chapter."""
    with open(f"{work}/status.json") as f:
        st = json.load(f)

    def ts(t):
        ms = int(round(max(t, 0) * 1000))
        return f"{ms // 3600000:02}:{ms // 60000 % 60:02}:{ms // 1000 % 60:02},{ms % 1000:03}"

    out, n = [], 0
    for ci, ch in enumerate(st.get('chapters') or []):
        ap = f"{work}/voice_{ci}.json"
        if not os.path.exists(ap):
            continue
        with open(ap) as f:
            al = json.load(f)
        off = float(ch.get('start') or 0)
        words, cur, ws, we = [], '', 0.0, 0.0
        for c, a, b in zip(al.get('characters') or [], al.get('character_start_times_seconds') or [],
                           al.get('character_end_times_seconds') or []):
            if c.isspace():
                if cur:
                    words.append((cur, ws, we))
                    cur = ''
            else:
                if not cur:
                    ws = a
                cur += c
                we = b
        if cur:
            words.append((cur, ws, we))
        line = []
        for i, wd in enumerate(words):
            line.append(wd)
            last = wd[0][-1]
            if len(line) >= 10 or (len(line) >= 4 and last in '.!?,;:') or last in '.!?' or i == len(words) - 1:
                n += 1
                out.append(f"{n}\n{ts(off + line[0][1])} --> {ts(off + line[-1][2])}\n"
                           f"{' '.join(x[0] for x in line)}\n")
                line = []
    return '\n'.join(out)


@app.route('/compile/<cid>/subtitles.srt', methods=['GET'])
def compile_srt(cid):
    if not valid_id(cid):
        return jsonify({"error": "bad compile_id"}), 400
    work = f"{COMPILES_DIR}/{cid}"
    if not os.path.exists(f"{work}/status.json"):
        return jsonify({"error": "unknown compile_id"}), 404
    srt = build_long_srt(work)
    if not srt:
        return jsonify({"error": "no timings found for this video"}), 404
    return srt, 200, {'Content-Type': 'application/x-subrip; charset=utf-8',
                      'Content-Disposition': f'attachment; filename="{cid}.srt"'}


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))
