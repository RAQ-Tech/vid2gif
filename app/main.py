import os, threading, queue, subprocess, shlex, datetime, time
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

# -------- Paths & setup --------
LIB_ROOT   = "/library"
STATE_ROOT = "/state"
LOG_DIR    = os.path.join(STATE_ROOT, "logs")
TMP_ROOT   = os.path.join(STATE_ROOT, "tmp")
os.makedirs(LOG_DIR, exist_ok=True)
os.makedirs(TMP_ROOT, exist_ok=True)

VIDEO_EXTS = {".mkv",".mp4",".m4v",".mov",".avi",".wmv",".mpg",".mpeg",".webm"}

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True  # helps while iterating UI

# -------- In-memory job state --------
jobs = {}
job_queue = queue.Queue()
lock = threading.Lock()

DEFAULTS = {
    "height": 480,   # using HEIGHT (scale keeps aspect via -1:HEIGHT)
    "fps": 15,
    "clip_len": 2.0,
    "percent_points": "10,20,30,40,50,60,70,80,90",
    "abs_early": 15.0,
    "abs_late_from_end": 10.0,
    "start_buffer": 5.0,
    "end_buffer": 5.0,
    "loop_forever": True
}

def ts(): return datetime.datetime.now().strftime("%H:%M:%S")

def log_write(job, msg):
    line = f"[{ts()}] {msg}"
    with open(job["log_path"], "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()  # make sure it hits disk for the tailer
        os.fsync(f.fileno())
    job["last_log"] = line

def parse_float(s, fb):
    try: return float(s)
    except: return fb

def parse_int_list(s):
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok: continue
        try: out.append(int(tok))
        except: pass
    return out

def choose_numeric(form, preset_key, custom_key, caster, default_val):
    # New UI: preset/custom
    if preset_key in form or custom_key in form:
        preset = (form.get(preset_key, "") or "").strip().lower()
        if preset and preset != "custom":
            try: return caster(preset)
            except: return default_val
        custom = (form.get(custom_key, "") or "").strip()
        if custom:
            try: return caster(custom)
            except: return default_val
        return default_val
    # Legacy fallback: single numeric field
    legacy_key = preset_key.replace("_preset","")
    val = (form.get(legacy_key,"") or "").strip()
    if val:
        try: return caster(val)
        except: return default_val
    return default_val

def get_duration(video_path):
    cmd = ["ffprobe","-v","error","-show_entries","format=duration",
           "-of","default=noprint_wrappers=1:nokey=1", video_path]
    try:
        return float(subprocess.check_output(cmd).decode().strip())
    except:
        return None

def probe_video_details(video_path):
    cmd = [
        "ffprobe","-v","error","-select_streams","v:0",
        "-show_entries","stream=codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,avg_frame_rate",
        "-of","json", video_path
    ]
    try:
        return subprocess.check_output(cmd, text=True)
    except:
        return "ffprobe stream query failed"

def build_segments(dur, cfg):
    clip = cfg["clip_len"]; start_buf = cfg["start_buffer"]; end_buf = cfg["end_buffer"]
    min_start = max(0.0, start_buf); max_start = max(0.0, dur - end_buf - clip)
    points = []
    if cfg["abs_early"] > 0: points.append(cfg["abs_early"])
    for p in cfg["percent_points"]: points.append(dur * p / 100.0)
    if cfg["abs_late_from_end"] > 0: points.append(max(0.0, dur - cfg["abs_late_from_end"]))
    points.sort()
    valid = []
    for t in points:
        st = min(max(t, min_start), max_start)
        if st < 0: continue
        if not valid or abs(st - valid[-1]) >= (clip/2.0): valid.append(st)
    if not valid:
        n = max(int(dur // (clip + 1)), 1)
        for i in range(n):
            st = min(min_start + i * ((dur - min_start)/n), max_start)
            valid.append(st)
    return [{"start": st, "end": min(st + clip, dur)} for st in valid]

def ffmpeg_version():
    try:
        return subprocess.check_output(["ffmpeg","-version"]).decode().splitlines()[0]
    except:
        return "ffmpeg not found"

def make_gif_multi_inputs(video, segs, out_gif, cfg, job):
    fps = int(cfg["fps"])
    height = int(cfg["height"])
    loop = "0" if cfg["loop_forever"] else "1"

    # Be chatty for Live Logs: -v info ; -progress pipe:2 emits a machine-readable line every N ms
    args = ["ffmpeg","-y","-hide_banner","-v","info","-nostats","-progress","pipe:2","-fflags","+genpts","-an","-sn"]

    # Trim at demuxer for each segment
    for s in segs:
        dur = max(0.01, s["end"] - s["start"])
        args += ["-ss", f"{s['start']:.3f}", "-t", f"{dur:.3f}", "-i", video]

    n = len(segs)
    concat_inputs = "".join(f"[{i}:v]" for i in range(n))
    filter_graph = (
        f"{concat_inputs}concat=n={n}:v=1:a=0[vcat];"
        f"[vcat]fps={fps},scale=-1:{height}:flags=lanczos,format=rgb24,split[s0][s1];"
        f"[s0]palettegen[p];[s1][p]paletteuse"
    )

    args += ["-filter_complex", filter_graph, "-loop", loop, out_gif]

    # Log the full command
    log_write(job, "----- FFMPEG CMD -----")
    log_write(job, " ".join(shlex.quote(a) for a in args))

    # Run & tee output into the log (stderr merged)
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    for raw in proc.stdout:
        line = (raw or "").rstrip("\n")
        if not line: continue
        log_write(job, line)
        if "frame=" in line or "time=" in line or "speed=" in line or line.startswith("out_time="):
            job["progress_text"] = line.strip()
    proc.wait()
    return proc.returncode == 0

def enqueue_job(video_path, cfg):
    if not video_path.startswith(LIB_ROOT):
        return None, "Path must be under /library"
    out_gif = os.path.join(os.path.dirname(video_path), "poster.gif")

    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = os.path.join(LOG_DIR, f"{job_id}.txt")
    # Create log immediately so Live tab can tail it
    with open(log_path, "w", encoding="utf-8") as f:
        f.write(f"[{ts()}] Job created\n")
        f.write(f"[{ts()}] Video: {video_path}\n")
        f.write(f"[{ts()}] Out  : {out_gif}\n")

    job = {
        "id": job_id,
        "video": video_path,
        "out_gif": out_gif,
        "status": "queued",
        "cfg": cfg,
        "log_path": log_path,
        "progress_text": ""
    }
    with lock:
        jobs[job_id] = job
    job_queue.put(job_id)
    return job_id, None

def find_videos(root_path):
    vids = []
    for base, _, files in os.walk(root_path):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in VIDEO_EXTS:
                vids.append(os.path.join(base, fn))
    return vids

# -------- Worker thread --------
def worker():
    while True:
        job_id = job_queue.get()
        # A ``None`` job_id is a sentinel indicating shutdown.
        # Make sure to mark the task as done so ``join`` calls do not block.
        if job_id is None:
            job_queue.task_done()
            break
        job = jobs.get(job_id)
        if not job:
            job_queue.task_done()
            continue
        try:
            job["status"] = "running"
            log_write(job, f"Starting: {job['video']}")
            log_write(job, "----- PROBE -----")
            log_write(job, probe_video_details(job["video"]))
            log_write(job, "----- CONFIG ----")
            log_write(job, f"height={job['cfg']['height']} fps={job['cfg']['fps']} clip_len={job['cfg']['clip_len']}")

            dur = get_duration(job["video"])
            if not dur or dur < 0.2:
                job["status"] = "failed"
                log_write(job, "Could not read duration.")
            else:
                segs = build_segments(dur, job["cfg"])
                log_write(job, f"{len(segs)} segments, ~{len(segs)*job['cfg']['clip_len']:.1f}s")
                ok = make_gif_multi_inputs(job["video"], segs, job["out_gif"], job["cfg"], job)
                job["status"] = "success" if ok else "failed"
                log_write(job, ("GIF ready: " if ok else "ffmpeg failed: ") + job["out_gif"])
        except Exception as e:
            job["status"] = "failed"
            log_write(job, f"Exception: {e}")
        finally:
            job_queue.task_done()

# Start the background worker thread and expose a helper to shut it down
# gracefully.  The thread watches for a ``None`` sentinel on the queue to
# terminate.
worker_thread = threading.Thread(target=worker, daemon=True)
worker_thread.start()

def stop_worker():
    """Signal the worker thread to exit and wait for it to finish."""
    job_queue.put(None)
    worker_thread.join()

# -------- Web routes --------
@app.route("/")
def home():
    return render_template("index.html", defaults=DEFAULTS, ffmpeg=ffmpeg_version())

@app.route("/queue")
def queue_page():
    with lock:
        all_jobs = list(jobs.values())
    return render_template("queue.html", jobs=all_jobs)

@app.route("/completed")
def completed_page():
    with lock:
        all_jobs = [j for j in jobs.values() if j["status"] in ("success","failed")]
    return render_template("completed.html", jobs=all_jobs)

@app.route("/live")
def live_page():
    with lock:
        all_jobs = list(jobs.values())
    all_jobs.sort(key=lambda j: j.get("id",""), reverse=True)
    return render_template("live.html", jobs=all_jobs)

@app.route("/logs/<job_id>")
def logs(job_id):
    j = jobs.get(job_id)
    if not j: return "Not found", 404
    if not os.path.isfile(j["log_path"]): return "No log", 404
    text = open(j["log_path"], "r", encoding="utf-8").read()
    return "<pre style='white-space:pre-wrap;font-family:ui-monospace'>" + text + "</pre>"

def _sse_format(line: str) -> str:
    return "data: " + line.replace("\r","") + "\n\n"

@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    j = jobs.get(job_id)
    if not j or not os.path.isfile(j["log_path"]):
        def not_found():
            yield _sse_format("No log yet for this job.")
        return Response(not_found(), mimetype="text/event-stream")

    def tail():
        path = j["log_path"]
        # send last ~200 lines first
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except:
            lines = []
        for line in lines[-200:]:
            yield _sse_format(line.rstrip("\n"))

        # follow file; keep alive with heartbeats
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            f.seek(0, os.SEEK_END)
            idle_ticks = 0
            while True:
                chunk = f.readline()
                if chunk:
                    idle_ticks = 0
                    yield _sse_format(chunk.rstrip("\n"))
                else:
                    idle_ticks += 1
                    if idle_ticks % 10 == 0:
                        yield _sse_format("[heartbeat]")
                    time.sleep(0.2)
                    if j.get("status") in ("success","failed") and idle_ticks > 50:
                        time.sleep(0.5)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(tail(), headers=headers, mimetype="text/event-stream")

@app.route("/api/status")
def api_status():
    with lock:
        return jsonify(list(jobs.values()))

@app.route("/api/add", methods=["POST"])
def api_add():
    target = (request.form.get("video","") or "").strip()
    if not target.startswith(LIB_ROOT):
        return jsonify({"error":"Path must be under /library"}), 400

    height   = choose_numeric(request.form, "height_preset", "height_custom", int,   DEFAULTS["height"])
    fps      = choose_numeric(request.form, "fps_preset",    "fps_custom",    int,   DEFAULTS["fps"])
    clip_len = choose_numeric(request.form, "clip_len_preset","clip_len_custom",float,DEFAULTS["clip_len"])

    cfg = {
        "height": height,
        "fps": fps,
        "clip_len": clip_len,
        "percent_points": parse_int_list(request.form.get("percent_points", DEFAULTS["percent_points"])) or parse_int_list(DEFAULTS["percent_points"]),
        "abs_early": parse_float(request.form.get("abs_early", DEFAULTS["abs_early"]), DEFAULTS["abs_early"]),
        "abs_late_from_end": parse_float(request.form.get("abs_late_from_end", DEFAULTS["abs_late_from_end"]), DEFAULTS["abs_late_from_end"]),
        "start_buffer": parse_float(request.form.get("start_buffer", DEFAULTS["start_buffer"]), DEFAULTS["start_buffer"]),
        "end_buffer": parse_float(request.form.get("end_buffer", DEFAULTS["end_buffer"]), DEFAULTS["end_buffer"]),
        "loop_forever": (request.form.get("loop_forever","on") == "on")
    }

    if os.path.isdir(target):
        for v in find_videos(target):
            enqueue_job(v, cfg)
    else:
        enqueue_job(target, cfg)

    # Go to Live Logs to watch it
    return redirect(url_for("live_page"))

if __name__ == "__main__":
    # threaded=True so SSE + worker don’t block each other
    app.run(host="0.0.0.0", port=904, debug=False, threaded=True)
