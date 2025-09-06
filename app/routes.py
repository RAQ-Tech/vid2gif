import os
import time
from flask import Flask, render_template, request, redirect, url_for, jsonify, Response

from config import DEFAULTS, LIB_ROOT
from utils import parse_float, parse_int_list, choose_numeric, resolve_case_insensitive
from ffmpeg_utils import ffmpeg_version
from jobs import (
    jobs,
    job_queue,
    lock,
    queue_paused,
    enqueue_job,
    find_videos,
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
    with lock:
        running = [j for j in jobs.values() if j.get("status") == "running"]
    with job_queue.mutex:
        queued_ids = list(job_queue.queue)
    with lock:
        queued = [jobs[jid] for jid in queued_ids if jid in jobs]
    return jsonify(
        {"running": running, "queued": queued, "paused": queue_paused.is_set()}
    )


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
    text = open(j["log_path"], "r", encoding="utf-8").read()
    return "<pre style='white-space:pre-wrap;font-family:ui-monospace'>" + text + "</pre>"


def _sse_format(line: str) -> str:
    return "data: " + line.replace("\r", "") + "\n\n"


@app.route("/api/stream/<job_id>")
def api_stream(job_id):
    j = jobs.get(job_id)
    if not j or not os.path.isfile(j["log_path"]):
        def not_found():
            yield _sse_format("No log yet for this job.")

        return Response(not_found(), mimetype="text/event-stream")

    def tail():
        path = j["log_path"]
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            lines = []
        for line in lines[-200:]:
            yield _sse_format(line.rstrip("\n"))

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
                    if j.get("status") in ("success", "failed") and idle_ticks > 50:
                        time.sleep(0.5)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(tail(), headers=headers, mimetype="text/event-stream")


@app.route("/api/stream/live")
def api_stream_live():
    def tail_live():
        last_id = None
        f = None
        idle_ticks = 0
        while True:
            with lock:
                running = [j for j in jobs.values() if j.get("status") == "running"]
            running.sort(key=lambda j: j.get("id", ""))
            cur = running[0] if running else None
            if cur and cur.get("id") != last_id:
                if f:
                    f.close()
                last_id = cur.get("id")
                yield _sse_format(f"[job {last_id}]")
                try:
                    f = open(cur["log_path"], "r", encoding="utf-8", errors="replace")
                    lines = f.readlines()
                except Exception:
                    f = None
                    lines = []
                for line in lines[-200:]:
                    yield _sse_format(line.rstrip("\n"))
                if f:
                    f.seek(0, os.SEEK_END)
            if f:
                chunk = f.readline()
                if chunk:
                    idle_ticks = 0
                    yield _sse_format(chunk.rstrip("\n"))
                    continue
            idle_ticks += 1
            if idle_ticks % 10 == 0:
                yield _sse_format("[heartbeat]")
            time.sleep(0.2)

    headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    return Response(tail_live(), headers=headers, mimetype="text/event-stream")


@app.route("/api/status")
def api_status():
    with lock:
        return jsonify(list(jobs.values()))


@app.route("/api/listdir")
def api_listdir():
    path = request.args.get("path", LIB_ROOT)
    if not path.lower().startswith(LIB_ROOT.lower()):
        return jsonify([])
    real = resolve_case_insensitive(path)
    if not real or not os.path.isdir(real):
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
    if not target.lower().startswith(LIB_ROOT.lower()):
        return jsonify({"error": "Path must be under /library"}), 400
    real_target = resolve_case_insensitive(target)
    if not real_target or not real_target.startswith(LIB_ROOT):
        return jsonify({"error": "Path not found"}), 400

    height = choose_numeric(request.form, "height_preset", "height_custom", int, DEFAULTS["height"])
    fps = choose_numeric(request.form, "fps_preset", "fps_custom", int, DEFAULTS["fps"])
    clip_len = choose_numeric(
        request.form, "clip_len_preset", "clip_len_custom", float, DEFAULTS["clip_len"]
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
            request.form.get("start_buffer", DEFAULTS["start_buffer"]), DEFAULTS["start_buffer"]
        ),
        "end_buffer": parse_float(
            request.form.get("end_buffer", DEFAULTS["end_buffer"]), DEFAULTS["end_buffer"]
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

