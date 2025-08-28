import os
import time
from flask import render_template, request, redirect, url_for, jsonify, Response

from jobs import (
    ffmpeg_version,
    choose_numeric,
    parse_int_list,
    parse_float,
    enqueue_job,
    find_videos,
    LIB_ROOT,
)


def register_routes(app, state):
    @app.route("/")
    def home():
        return render_template("index.html", defaults=state.defaults, ffmpeg=ffmpeg_version())

    @app.route("/queue")
    def queue_page():
        with state.lock:
            all_jobs = list(state.jobs.values())
        return render_template("queue.html", jobs=all_jobs)

    @app.route("/completed")
    def completed_page():
        with state.lock:
            all_jobs = [j for j in state.jobs.values() if j["status"] in ("success", "failed")]
        return render_template("completed.html", jobs=all_jobs)

    @app.route("/live")
    def live_page():
        with state.lock:
            all_jobs = list(state.jobs.values())
        all_jobs.sort(key=lambda j: j.get("id", ""), reverse=True)
        return render_template("live.html", jobs=all_jobs)

    @app.route("/logs/<job_id>")
    def logs(job_id):
        j = state.jobs.get(job_id)
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
        j = state.jobs.get(job_id)
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
            except Exception:
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
                        if j.get("status") in ("success", "failed") and idle_ticks > 50:
                            time.sleep(0.5)

        headers = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
        return Response(tail(), headers=headers, mimetype="text/event-stream")

    @app.route("/api/status")
    def api_status():
        with state.lock:
            return jsonify(list(state.jobs.values()))

    @app.route("/api/add", methods=["POST"])
    def api_add():
        target = (request.form.get("video", "") or "").strip()
        if not target.startswith(LIB_ROOT):
            return jsonify({"error": "Path must be under /library"}), 400

        height = choose_numeric(request.form, "height_preset", "height_custom", int, state.defaults["height"])
        fps = choose_numeric(request.form, "fps_preset", "fps_custom", int, state.defaults["fps"])
        clip_len = choose_numeric(request.form, "clip_len_preset", "clip_len_custom", float, state.defaults["clip_len"])

        cfg = {
            "height": height,
            "fps": fps,
            "clip_len": clip_len,
            "percent_points": parse_int_list(request.form.get("percent_points", state.defaults["percent_points"]))
            or parse_int_list(state.defaults["percent_points"]),
            "abs_early": parse_float(request.form.get("abs_early", state.defaults["abs_early"]), state.defaults["abs_early"]),
            "abs_late_from_end": parse_float(
                request.form.get("abs_late_from_end", state.defaults["abs_late_from_end"]),
                state.defaults["abs_late_from_end"],
            ),
            "start_buffer": parse_float(
                request.form.get("start_buffer", state.defaults["start_buffer"]), state.defaults["start_buffer"]
            ),
            "end_buffer": parse_float(
                request.form.get("end_buffer", state.defaults["end_buffer"]), state.defaults["end_buffer"]
            ),
            "loop_forever": request.form.get("loop_forever", "on") == "on",
        }

        if os.path.isdir(target):
            for v in find_videos(target):
                enqueue_job(state, v, cfg)
        else:
            enqueue_job(state, target, cfg)

        # Go to Live Logs to watch it
        return redirect(url_for("live_page"))
