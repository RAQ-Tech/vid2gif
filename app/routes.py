import os
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

from .config import DEFAULTS, LIB_ROOT
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
    public_job,
    queue_status_payload,
)

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.route("/")
def home():
    return render_template(
        "index.html", defaults=DEFAULTS, ffmpeg=ffmpeg_version(), lib_root=LIB_ROOT
    )


@app.route("/queue")
def queue_page():
    limit = request.args.get("limit", 10, type=int)
    if limit not in (10, 25, 50, 100):
        limit = 10
    with lock:
        running_jobs = [j for j in jobs.values() if j.get("status") == "running"]
    with job_queue.mutex:
        queued_ids = list(job_queue.queue)
    # Display running jobs at the top and fill remaining slots with queued ones
    remaining = max(0, limit - len(running_jobs))
    with lock:
        ordered_jobs = [jobs[jid] for jid in queued_ids[:remaining] if jid in jobs]
    shown = len(running_jobs) + len(ordered_jobs)
    total = len(queued_ids) + len(running_jobs)
    return render_template(
        "queue.html",
        running_jobs=running_jobs,
        jobs=ordered_jobs,
        limit=limit,
        shown=shown,
        total=total,
        paused=queue_paused.is_set(),
    )


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
                    j["status"] = "stopped"
    return redirect(url_for("queue_page", limit=request.args.get("limit", 10)))


@app.route("/api/queue/move/<job_id>/<direction>", methods=["POST"])
def api_queue_move(job_id, direction):
    with lock:
        if jobs.get(job_id, {}).get("status") == "running":
            return redirect(url_for("queue_page", limit=request.args.get("limit", 10)))
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
    return redirect(url_for("queue_page", limit=request.args.get("limit", 10)))


@app.route("/api/queue/status")
def api_queue_status():
    return jsonify(queue_status_payload())


@app.route("/completed")
def completed_page():
    with lock:
        all_jobs = [j for j in jobs.values() if j["status"] in ("success", "failed")]
    return render_template("completed.html", jobs=all_jobs)


@app.route("/live")
def live_page():
    with lock:
        all_jobs = list(jobs.values())
    all_jobs.sort(key=lambda j: j.get("id", ""), reverse=True)
    return render_template("live.html", jobs=all_jobs)


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


@app.route("/api/add", methods=["POST"])
def api_add():
    target = (request.form.get("video", "") or "").strip()
    real_target = resolve_case_insensitive(target)
    if not real_target:
        return jsonify({"error": "Path not found"}), 400
    if not path_is_under(real_target, LIB_ROOT):
        return jsonify({"error": "Path not found"}), 400

    height = choose_numeric(
        request.form, "height_preset", "height_custom", int, DEFAULTS["height"]
    )
    fps = (
        "original"
        if request.form.get("fps_original")
        else choose_numeric(
            request.form, "fps_preset", "fps_custom", int, DEFAULTS["fps"]
        )
    )
    clip_len = choose_numeric(
        request.form,
        "clip_len_preset",
        "clip_len_custom",
        float,
        DEFAULTS["clip_len"],
    )

    cfg = {
        "height": height,
        "fps": fps,
        "clip_len": clip_len,
        "percent_points": parse_int_list(
            request.form.get("percent_points", DEFAULTS["percent_points"])
        )
        or parse_int_list(DEFAULTS["percent_points"]),
        "abs_early": parse_float(
            request.form.get("abs_early", DEFAULTS["abs_early"]), DEFAULTS["abs_early"]
        ),
        "abs_late_from_end": parse_float(
            request.form.get("abs_late_from_end", DEFAULTS["abs_late_from_end"]),
            DEFAULTS["abs_late_from_end"],
        ),
        "start_buffer": parse_float(
            request.form.get("start_buffer", DEFAULTS["start_buffer"]),
            DEFAULTS["start_buffer"],
        ),
        "end_buffer": parse_float(
            request.form.get("end_buffer", DEFAULTS["end_buffer"]),
            DEFAULTS["end_buffer"],
        ),
        "loop_forever": (request.form.get("loop_forever", "on") == "on"),
        "smooth": (request.form.get("smooth", "off") == "on"),
    }

    if os.path.isdir(real_target):
        for v in find_videos(real_target):
            enqueue_job(v, cfg)
    else:
        enqueue_job(real_target, cfg)

    return redirect(url_for("live_page"))
