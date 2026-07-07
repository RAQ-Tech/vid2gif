import os
import time
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

from . import estimate_history
from .config import DEFAULTS, LIB_ROOT, VIDEO_EXTS
from .utils import (
    parse_float,
    parse_int_list,
    choose_numeric,
    resolve_case_insensitive,
    path_is_under,
)
from .ffmpeg_utils import ffmpeg_version
from .jobs import (
    jobs,
    job_queue,
    lock,
    queue_paused,
    enqueue_job,
    find_videos,
    emit_queue_status,
    new_queue_batch_id,
    public_job,
    queue_status_payload,
)
from .progress import format_duration, format_size, mark_job_finished

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True

QUEUE_LIMITS = (10, 25, 50, 100)
SCAN_CACHE_TTL_SECONDS = 300
_scan_cache = {}


def _queue_limit(default=10):
    limit = request.args.get("limit", default, type=int)
    if limit not in QUEUE_LIMITS:
        return default
    return limit


def _redirect_to_gifs(anchor, **params):
    clean_params = {k: v for k, v in params.items() if v is not None}
    return redirect(url_for("gifs_page", _anchor=anchor, **clean_params))


def _truthy(value):
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _job_config_from_values(values):
    height = choose_numeric(
        values, "height_preset", "height_custom", int, DEFAULTS["height"]
    )
    fps = (
        "original"
        if _truthy(values.get("fps_original"))
        else choose_numeric(
            values, "fps_preset", "fps_custom", int, DEFAULTS["fps"]
        )
    )
    clip_len = choose_numeric(
        values,
        "clip_len_preset",
        "clip_len_custom",
        float,
        DEFAULTS["clip_len"],
    )

    return {
        "height": height,
        "fps": fps,
        "clip_len": clip_len,
        "percent_points": parse_int_list(
            values.get("percent_points", DEFAULTS["percent_points"])
        )
        or parse_int_list(DEFAULTS["percent_points"]),
        "abs_early": parse_float(
            values.get("abs_early", DEFAULTS["abs_early"]), DEFAULTS["abs_early"]
        ),
        "abs_late_from_end": parse_float(
            values.get("abs_late_from_end", DEFAULTS["abs_late_from_end"]),
            DEFAULTS["abs_late_from_end"],
        ),
        "start_buffer": parse_float(
            values.get("start_buffer", DEFAULTS["start_buffer"]),
            DEFAULTS["start_buffer"],
        ),
        "end_buffer": parse_float(
            values.get("end_buffer", DEFAULTS["end_buffer"]),
            DEFAULTS["end_buffer"],
        ),
        "loop_forever": (values.get("loop_forever", "on") == "on"),
        "smooth": (values.get("smooth", "off") == "on"),
    }


def _compatible_file_count(real_path):
    now = time.time()
    cached = _scan_cache.get(real_path)
    if cached and now - cached["scanned_at"] <= SCAN_CACHE_TTL_SECONDS:
        return cached["count"], cached["is_dir"]

    if os.path.isfile(real_path):
        count = (
            1
            if not os.path.islink(real_path)
            and os.path.splitext(real_path)[1].lower() in VIDEO_EXTS
            else 0
        )
        is_dir = False
    elif os.path.isdir(real_path):
        count = 0
        is_dir = True
        for base, dirs, files in os.walk(real_path, followlinks=False):
            dirs[:] = [
                d for d in dirs if not os.path.islink(os.path.join(base, d))
            ]
            for fn in files:
                candidate = os.path.join(base, fn)
                if os.path.islink(candidate):
                    continue
                if os.path.splitext(fn)[1].lower() in VIDEO_EXTS:
                    count += 1
    else:
        count = 0
        is_dir = False

    _scan_cache[real_path] = {
        "count": count,
        "is_dir": is_dir,
        "scanned_at": now,
    }
    return count, is_dir


def _gifs_workspace_context(limit):
    with lock:
        running_jobs = [
            public_job(j) for j in jobs.values() if j.get("status") == "running"
        ]

    with job_queue.mutex:
        queued_ids = list(job_queue.queue)

    remaining = max(0, limit - len(running_jobs))
    with lock:
        queued_jobs = [
            public_job(jobs[jid]) for jid in queued_ids[:remaining] if jid in jobs
        ]
        completed_jobs = [
            public_job(j)
            for j in jobs.values()
            if j.get("status") in ("success", "failed", "stopped")
        ]
        all_jobs = [public_job(j) for j in jobs.values()]

    completed_jobs.sort(key=lambda j: j.get("id", ""), reverse=True)
    all_jobs.sort(key=lambda j: j.get("id", ""), reverse=True)
    shown = len(running_jobs) + len(queued_jobs)
    total = len(queued_ids) + len(running_jobs)
    latest_optimization_label = next(
        (
            j.get("gif_optimization_label")
            for j in completed_jobs
            if j.get("gif_optimization_label")
        ),
        "",
    )

    return {
        "defaults": DEFAULTS,
        "ffmpeg": ffmpeg_version(),
        "lib_root": LIB_ROOT,
        "queue_limits": QUEUE_LIMITS,
        "running_jobs": running_jobs,
        "queued_jobs": queued_jobs,
        "completed_jobs": completed_jobs,
        "all_jobs": all_jobs,
        "limit": limit,
        "shown": shown,
        "total": total,
        "paused": queue_paused.is_set(),
        "queue_summary": queue_status_payload(),
        "latest_optimization_label": latest_optimization_label,
        "format_duration": format_duration,
        "format_size": format_size,
    }


@app.route("/")
def home():
    return _redirect_to_gifs("new")


@app.route("/gifs")
def gifs_page():
    return render_template("gifs.html", **_gifs_workspace_context(_queue_limit()))


@app.route("/queue")
def queue_page():
    limit = _queue_limit() if "limit" in request.args else None
    return _redirect_to_gifs("queue", limit=limit)


@app.route("/api/queue/<action>", methods=["POST"])
def api_queue_control(action):
    if action == "start":
        queue_paused.clear()
    elif action == "pause":
        queue_paused.set()
    elif action == "stop":
        queue_paused.set()
        with job_queue.mutex:
            ids = list(job_queue.queue)
            job_queue.queue.clear()
        with lock:
            for jid in ids:
                j = jobs.get(jid)
                if j and j.get("status") == "queued":
                    mark_job_finished(j, "stopped")
    return _redirect_to_gifs("queue", limit=_queue_limit())


@app.route("/api/queue/move/<job_id>/<direction>", methods=["POST"])
def api_queue_move(job_id, direction):
    with lock:
        if jobs.get(job_id, {}).get("status") == "running":
            return _redirect_to_gifs("queue", limit=_queue_limit())
    with job_queue.mutex:
        q = list(job_queue.queue)
        try:
            idx = q.index(job_id)
        except ValueError:
            pass
        else:
            if direction == "up" and idx > 0:
                q[idx - 1], q[idx] = q[idx], q[idx - 1]
            elif direction == "down" and idx < len(q) - 1:
                q[idx + 1], q[idx] = q[idx], q[idx + 1]
            job_queue.queue.clear()
            job_queue.queue.extend(q)
    return _redirect_to_gifs("queue", limit=_queue_limit())


@app.route("/api/queue/status")
def api_queue_status():
    return jsonify(queue_status_payload())


@app.route("/completed")
def completed_page():
    return _redirect_to_gifs("completed")


@app.route("/live")
def live_page():
    return _redirect_to_gifs("logs")


@app.route("/logs/<job_id>")
def logs(job_id):
    j = jobs.get(job_id)
    if not j:
        return "Not found", 404
    if not os.path.isfile(j["log_path"]):
        return "No log", 404
    with open(j["log_path"], "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return Response(text, mimetype="text/plain; charset=utf-8")


@app.route("/api/logs/<job_id>")
def api_logs(job_id):
    j = jobs.get(job_id)
    if not j:
        return jsonify({"error": "Not found"}), 404

    try:
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        offset = 0
    offset = max(0, offset)

    path = j["log_path"]
    if not os.path.isfile(path):
        return jsonify(
            {"job": public_job(j), "lines": [], "offset": 0, "reset": offset > 0}
        )

    try:
        size = os.path.getsize(path)
        reset = offset > size
        if reset:
            offset = 0
        with open(path, "rb") as f:
            f.seek(offset)
            chunk = f.read()
            next_offset = f.tell()
    except Exception as e:
        return (
            jsonify(
                {
                    "error": str(e),
                    "job": public_job(j),
                    "lines": [],
                    "offset": offset,
                    "reset": False,
                }
            ),
            500,
        )

    text = chunk.decode("utf-8", errors="replace")
    return jsonify(
        {
            "job": public_job(j),
            "lines": text.splitlines(),
            "offset": next_offset,
            "reset": reset,
        }
    )


@app.route("/api/status")
def api_status():
    with lock:
        return jsonify([public_job(j) for j in jobs.values()])


@app.route("/api/listdir")
def api_listdir():
    path = request.args.get("path", LIB_ROOT)
    real = resolve_case_insensitive(path)
    if not real or not path_is_under(real, LIB_ROOT) or not os.path.isdir(real):
        return jsonify([])
    try:
        entries = [
            d
            for d in os.listdir(real)
            if os.path.isdir(os.path.join(real, d))
        ]
        entries.sort()
    except Exception:
        entries = []
    return jsonify(entries)


@app.route("/api/scan-estimate")
def api_scan_estimate():
    target = (request.args.get("path") or request.args.get("video") or "").strip()
    if not target:
        return jsonify(
            {
                "status": "choose_folder",
                "scan_status": "idle",
                "compatible_count": 0,
                "estimated_seconds": None,
                "estimated_size_bytes": None,
                "time_label": "",
                "size_label": "",
                "confidence": "none",
                "low_confidence": False,
                "detail": "",
                "message": "Choose a folder",
            }
        )

    real_target = resolve_case_insensitive(target)
    if (
        not real_target
        or not path_is_under(real_target, LIB_ROOT)
        or not (os.path.isdir(real_target) or os.path.isfile(real_target))
    ):
        return (
            jsonify(
                {
                    "status": "invalid_path",
                    "scan_status": "idle",
                    "error": "Path not found",
                    "compatible_count": 0,
                    "estimated_seconds": None,
                    "estimated_size_bytes": None,
                    "time_label": "",
                    "size_label": "",
                    "confidence": "none",
                    "low_confidence": False,
                    "detail": "",
                    "message": "Choose a folder",
                }
            ),
            400,
        )

    cfg = _job_config_from_values(request.args)
    compatible_count, is_dir = _compatible_file_count(real_target)
    with lock:
        in_memory_samples = estimate_history.samples_from_jobs(jobs.values())
    payload = estimate_history.estimate_payload(
        compatible_count, cfg, in_memory_samples=in_memory_samples
    )
    payload.update(
        {
            "status": "ready",
            "scan_status": "complete",
            "is_dir": is_dir,
        }
    )
    return jsonify(payload)


@app.route("/api/add", methods=["POST"])
def api_add():
    target = (request.form.get("video", "") or "").strip()
    real_target = resolve_case_insensitive(target)
    if not real_target:
        return jsonify({"error": "Path not found"}), 400
    if not path_is_under(real_target, LIB_ROOT):
        return jsonify({"error": "Path not found"}), 400

    cfg = _job_config_from_values(request.form)

    batch_id = new_queue_batch_id()
    if os.path.isdir(real_target):
        for v in find_videos(real_target):
            enqueue_job(v, cfg, batch_id=batch_id)
    else:
        enqueue_job(real_target, cfg, batch_id=batch_id)

    return _redirect_to_gifs("logs")
