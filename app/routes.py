import mimetypes
import os
import time
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from . import app_settings
from . import actor_image_maintenance
from . import dashboard
from . import estimate_history
from . import maintenance
from . import poster_maintenance
from . import subtitle_maintenance
from . import system_status
from . import test_lab
from . import video_preview_maintenance
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
    prune_job_history,
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


def _optional_truthy(values, key, default):
    if key not in values:
        return bool(default)
    return _truthy(values.get(key))


def _job_config_from_values(values):
    height = choose_numeric(
        values, "height_preset", "height_custom", int, DEFAULTS["height"]
    )
    fps_preset = str(values.get("fps_preset") or values.get("fps") or "").strip().lower()
    if _truthy(values.get("fps_original")) or fps_preset == "original":
        fps = "original"
    else:
        fps = choose_numeric(
            values, "fps_preset", "fps_custom", int, DEFAULTS["fps"]
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
        "loop_forever": _truthy(values.get("loop_forever", "on")),
        "smooth": _truthy(values.get("smooth", "off")),
        "optimize": _optional_truthy(values, "optimize", DEFAULTS["optimize"]),
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
    prune_job_history()
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
    return render_template("dashboard.html", lib_root=LIB_ROOT)


@app.route("/dashboard")
def dashboard_page():
    return render_template("dashboard.html", lib_root=LIB_ROOT)


@app.route("/gifs")
def gifs_page():
    return render_template("gifs.html", **_gifs_workspace_context(_queue_limit()))


def _settings_context(error="", saved=False, form_values=None):
    settings = app_settings.load_settings()
    preview_height = settings["test_lab_preview_height"]
    selected = "original" if preview_height is None else str(preview_height)
    custom = ""
    presets = [str(value) for value in app_settings.PREVIEW_HEIGHT_PRESETS]
    if selected not in presets and selected != "original":
        custom = selected
        selected = "custom"
    if form_values:
        selected = form_values.get("preview_height_preset", selected)
        custom = form_values.get("preview_height_custom", custom)
        settings = dict(settings)
        for key in (
            "duplicate_grouping_mode",
            "duplicate_keeper_rule",
            "duplicate_accessory_policy",
            "duplicate_move_root",
            "subtitle_expected_languages",
            "video_preview_bif_width",
            "video_preview_bif_interval_seconds",
        ):
            if key in form_values:
                settings[key] = form_values.get(key)
        if "duplicate_excluded_folders" in form_values:
            settings["duplicate_excluded_folders"] = app_settings.parse_excluded_folders(
                form_values.get("duplicate_excluded_folders")
            )
        settings["subtitle_flag_missing"] = _truthy(form_values.get("subtitle_flag_missing"))
        settings["subtitle_flag_unknown_language"] = _truthy(
            form_values.get("subtitle_flag_unknown_language")
        )
        settings["subtitle_subgen_detection"] = _truthy(
            form_values.get("subtitle_subgen_detection")
        )
    return {
        "settings": settings,
        "preview_height_presets": app_settings.PREVIEW_HEIGHT_PRESETS,
        "preview_height_selected": selected,
        "preview_height_custom": custom,
        "preview_height_label": app_settings.preview_height_label(preview_height),
        "preview_height_warning": app_settings.warning_for_preview_height(
            preview_height
        ),
        "duplicate_grouping_modes": app_settings.DUPLICATE_GROUPING_MODES,
        "duplicate_keeper_rules": app_settings.DUPLICATE_KEEPER_RULES,
        "duplicate_accessory_policies": app_settings.DUPLICATE_ACCESSORY_POLICIES,
        "duplicate_excluded_folders_text": ", ".join(
            settings.get("duplicate_excluded_folders") or []
        ),
        "subtitle_expected_languages_text": ", ".join(
            settings.get("subtitle_expected_languages") or []
        ),
        "error": error,
        "saved": saved,
    }


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        preset = (request.form.get("preview_height_preset") or "").strip().lower()
        raw_height = (
            request.form.get("preview_height_custom")
            if preset == "custom"
            else preset
        )
        height, err = app_settings.parse_preview_height(raw_height)
        if err:
            return (
                render_template(
                    "settings.html",
                    **_settings_context(error=err, form_values=request.form),
                ),
                400,
            )
        settings = app_settings.load_settings()
        settings.update(
            {
                "test_lab_preview_height": height,
                "duplicate_grouping_mode": request.form.get("duplicate_grouping_mode"),
                "duplicate_keeper_rule": request.form.get("duplicate_keeper_rule"),
                "duplicate_accessory_policy": request.form.get("duplicate_accessory_policy"),
                "duplicate_move_root": request.form.get("duplicate_move_root"),
                "duplicate_excluded_folders": app_settings.parse_excluded_folders(
                    request.form.get("duplicate_excluded_folders")
                ),
                "subtitle_expected_languages": app_settings.parse_subtitle_languages(
                    request.form.get("subtitle_expected_languages")
                ),
                "subtitle_flag_missing": _truthy(
                    request.form.get("subtitle_flag_missing")
                ),
                "subtitle_flag_unknown_language": _truthy(
                    request.form.get("subtitle_flag_unknown_language")
                ),
                "subtitle_subgen_detection": _truthy(
                    request.form.get("subtitle_subgen_detection")
                ),
                "video_preview_bif_width": request.form.get("video_preview_bif_width"),
                "video_preview_bif_interval_seconds": request.form.get("video_preview_bif_interval_seconds"),
            }
        )
        if not app_settings.save_settings(settings):
            return (
                render_template(
                    "settings.html",
                    **_settings_context(
                        error="Settings could not be saved.",
                        form_values=request.form,
                    ),
                ),
                500,
            )
        return redirect(url_for("settings_page", saved="1"))

    return render_template(
        "settings.html",
        **_settings_context(saved=request.args.get("saved") == "1"),
    )


@app.route("/maintenance")
def maintenance_page():
    return render_template(
        "maintenance.html",
        lib_root=LIB_ROOT,
        bif_settings=app_settings.load_settings(),
    )


@app.route("/system")
def system_page():
    return render_template("system.html")


@app.route("/api/system/status")
def api_system_status():
    return jsonify(system_status.status_payload())


@app.route("/healthz")
def healthz():
    payload = system_status.status_payload()
    status_code = 200 if payload.get("healthy") else 503
    return jsonify(
        {
            "status": payload.get("overall", "unhealthy"),
            "healthy": bool(payload.get("healthy")),
            "generated_at": payload.get("generated_at"),
        }
    ), status_code


@app.route("/system/backup", methods=["POST"])
def system_backup():
    try:
        archive_path, backup = system_status.create_state_backup()
    except Exception as exc:
        return Response(f"State backup failed: {exc}\n", status=500, mimetype="text/plain")

    archive_size = os.path.getsize(archive_path)

    def stream_archive():
        try:
            with open(archive_path, "rb") as archive:
                while True:
                    chunk = archive.read(1024 * 1024)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(archive_path)
            except OSError:
                pass

    response = Response(stream_archive(), mimetype="application/zip")
    response.content_length = archive_size
    response.headers.set(
        "Content-Disposition", "attachment", filename=backup["download_name"]
    )
    response.headers["X-vid2gif-Backup-Files"] = str(backup["file_count"])
    response.headers["X-vid2gif-Backup-Bytes"] = str(backup["total_bytes"])
    return response


@app.route("/api/dashboard/status")
def api_dashboard_status():
    return jsonify(dashboard.status_payload())


@app.route("/api/dashboard/library-scan", methods=["POST"])
def api_dashboard_library_scan():
    data = request.get_json(silent=True) or {}
    scan, err = dashboard.start_library_scan(
        data.get("path") or LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": scan})


@app.route("/api/dashboard/library-scan/status")
def api_dashboard_library_scan_status():
    return jsonify(dashboard.library_scan_status())


@app.route("/api/dashboard/library-scan/folders")
def api_dashboard_library_scan_folders():
    return jsonify(
        dashboard.library_folders_payload(
            offset=request.args.get("offset"),
            limit=request.args.get("limit"),
            q=request.args.get("q"),
            sort=request.args.get("sort") or "name",
            direction=request.args.get("direction") or "asc",
        )
    )


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
    prune_job_history()
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


@app.route("/api/media-browser")
def api_media_browser():
    path = (request.args.get("path") or LIB_ROOT).strip()
    real = resolve_case_insensitive(path)
    if real and os.path.isfile(real):
        real = os.path.dirname(real)

    if not real or not path_is_under(real, LIB_ROOT) or not os.path.isdir(real):
        return (
            jsonify(
                {
                    "error": "Path not found",
                    "path": "",
                    "parent": "",
                    "folders": [],
                    "files": [],
                }
            ),
            400,
        )

    folders = []
    files = []
    try:
        for entry in os.listdir(real):
            full_path = os.path.join(real, entry)
            if os.path.islink(full_path):
                continue
            if os.path.isdir(full_path):
                folders.append({"name": entry, "path": full_path})
            elif os.path.isfile(full_path) and os.path.splitext(entry)[1].lower() in VIDEO_EXTS:
                files.append({"name": entry, "path": full_path})
    except Exception:
        folders = []
        files = []

    folders.sort(key=lambda item: item["name"].lower())
    files.sort(key=lambda item: item["name"].lower())
    parent = ""
    lib_real = resolve_case_insensitive(LIB_ROOT) or LIB_ROOT
    real_parent = os.path.dirname(real)
    if real_parent and path_is_under(real_parent, lib_real) and os.path.normcase(os.path.realpath(real)) != os.path.normcase(os.path.realpath(lib_real)):
        parent = real_parent

    return jsonify(
        {
            "path": real,
            "parent": parent,
            "folders": folders,
            "files": files,
        }
    )


def _json_or_form_data():
    if request.is_json:
        data = request.get_json(silent=True) or {}
    elif request.form:
        data = request.form.to_dict(flat=True)
    else:
        data = {}
    return data if isinstance(data, dict) else {}


@app.route("/api/maintenance/duplicates/scan", methods=["POST"])
def api_maintenance_duplicates_scan():
    data = _json_or_form_data()
    scan, err = maintenance.start_duplicate_scan(
        data.get("path"),
        lib_root=LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": maintenance.public_scan(scan)})


@app.route("/api/maintenance/duplicates/status")
def api_maintenance_duplicates_status():
    payload, err = maintenance.status_payload(request.args.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/duplicates/cancel", methods=["POST"])
def api_maintenance_duplicates_cancel():
    data = request.get_json(silent=True) or {}
    scan, err = maintenance.cancel_duplicate_scan(data.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"scan": maintenance.public_scan(scan)})


@app.route("/api/maintenance/duplicates/groups")
def api_maintenance_duplicates_groups():
    payload, err = maintenance.groups_payload(
        request.args.get("scan_id"),
        offset=request.args.get("offset"),
        limit=request.args.get("limit"),
    )
    if err:
        status = 404 if err == "Scan not found" else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/duplicates/groups/<group_id>")
def api_maintenance_duplicates_group(group_id):
    payload, err = maintenance.group_payload(
        request.args.get("scan_id"),
        group_id,
        keep_video_id=request.args.get("keep_video_id"),
    )
    if err:
        status = 404 if err in {"Scan not found", "Group not found"} else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/duplicates/plan", methods=["POST"])
def api_maintenance_duplicates_plan():
    plan, err = maintenance.build_duplicate_cleanup_plan(
        request.get_json(silent=True) or {},
        lib_root=LIB_ROOT,
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"plan": plan})


@app.route("/api/maintenance/duplicates/apply", methods=["POST"])
def api_maintenance_duplicates_apply():
    data = request.get_json(silent=True) or {}
    run, err = maintenance.start_duplicate_apply(data.get("plan_id"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"apply": maintenance.public_apply_run(run)})


@app.route("/api/maintenance/duplicates/apply/status")
def api_maintenance_duplicates_apply_status():
    payload, err = maintenance.duplicate_apply_status(request.args.get("apply_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/duplicates/logs")
def api_maintenance_duplicates_logs():
    return jsonify({"logs": maintenance.list_duplicate_cleanup_logs()})


@app.route("/api/maintenance/duplicates/logs/<log_id>")
def api_maintenance_duplicates_log(log_id):
    log, err = maintenance.read_duplicate_cleanup_log(log_id)
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"log": log})


@app.route("/api/maintenance/landscape-posters/status")
def api_maintenance_landscape_posters_status():
    return jsonify(poster_maintenance.status_payload())


@app.route("/api/maintenance/landscape-posters/run", methods=["POST"])
def api_maintenance_landscape_posters_run():
    data = request.get_json(silent=True) or {}
    run, err = poster_maintenance.start_landscape_poster_run(
        data.get("path") or LIB_ROOT,
        mode=data.get("mode") or "full",
        synchronous=_truthy(data.get("synchronous")),
        lib_root=LIB_ROOT,
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"run": poster_maintenance.public_run(run)})


@app.route("/api/maintenance/landscape-posters/settings", methods=["POST"])
def api_maintenance_landscape_posters_settings():
    settings, err = poster_maintenance.update_settings(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    return jsonify(
        {
            "settings": settings,
            "status": poster_maintenance.status_payload(),
        }
    )


@app.route("/api/maintenance/landscape-posters/emby/test", methods=["POST"])
def api_maintenance_landscape_posters_emby_test():
    data = request.get_json(silent=True)
    if data is None:
        data = {}
    result, err = poster_maintenance.test_emby_connection(data)
    if err:
        return jsonify({"error": err}), 400
    return jsonify(
        {
            "result": result,
            "status": poster_maintenance.status_payload(),
        }
    )


@app.route("/api/maintenance/video-previews/scan", methods=["POST"])
def api_maintenance_video_previews_scan():
    data = _json_or_form_data()
    scan, err = video_preview_maintenance.start_scan(
        data.get("path"),
        lib_root=LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": video_preview_maintenance.public_scan(scan)})


@app.route("/api/maintenance/video-previews/status")
def api_maintenance_video_previews_status():
    payload, err = video_preview_maintenance.status_payload(request.args.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/cancel", methods=["POST"])
def api_maintenance_video_previews_cancel():
    data = request.get_json(silent=True) or {}
    scan, err = video_preview_maintenance.cancel_scan(data.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"scan": video_preview_maintenance.public_scan(scan)})


@app.route("/api/maintenance/video-previews/items")
def api_maintenance_video_previews_items():
    payload, err = video_preview_maintenance.items_payload(
        request.args.get("scan_id"),
        status=request.args.get("status"),
        offset=request.args.get("offset"),
        limit=request.args.get("limit"),
    )
    if err:
        status = 404 if err == "Scan not found" else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/generation/settings", methods=["POST"])
def api_maintenance_video_previews_generation_settings():
    settings, err = video_preview_maintenance.save_generation_settings(
        request.get_json(silent=True) or {}
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"settings": settings})


@app.route("/api/maintenance/video-previews/generation/plan", methods=["POST"])
def api_maintenance_video_previews_generation_plan():
    plan, err = video_preview_maintenance.build_generation_plan(
        request.get_json(silent=True) or {},
        lib_root=LIB_ROOT,
    )
    if err:
        status = 409 if "differ from the latest observed" in err else 400
        return jsonify({"error": err, "profile_mismatch": status == 409}), status
    return jsonify({"plan": plan})


@app.route("/api/maintenance/video-previews/generation/start", methods=["POST"])
def api_maintenance_video_previews_generation_start():
    data = request.get_json(silent=True) or {}
    run, err = video_preview_maintenance.start_generation(
        data.get("plan_id"),
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"run": video_preview_maintenance.public_generation_run(run)})


@app.route("/api/maintenance/video-previews/generation/status")
def api_maintenance_video_previews_generation_status():
    payload, err = video_preview_maintenance.generation_status(request.args.get("run_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/generation/cancel", methods=["POST"])
def api_maintenance_video_previews_generation_cancel():
    data = request.get_json(silent=True) or {}
    run, err = video_preview_maintenance.cancel_generation(data.get("run_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"run": run})


@app.route("/api/maintenance/video-previews/logs")
def api_maintenance_video_previews_logs():
    return jsonify({"logs": video_preview_maintenance.list_recent_logs()})


@app.route("/api/maintenance/video-previews/logs/<log_id>")
def api_maintenance_video_previews_log(log_id):
    payload, err = video_preview_maintenance.recent_log_payload(log_id)
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/emby/tasks")
def api_maintenance_video_previews_emby_tasks():
    return jsonify(video_preview_maintenance.discover_thumbnail_tasks())


@app.route("/api/maintenance/video-previews/emby/run-extraction", methods=["POST"])
def api_maintenance_video_previews_emby_run_extraction():
    payload, err = video_preview_maintenance.run_thumbnail_extraction()
    if err:
        return jsonify({"error": err}), 400
    status = 200 if (payload.get("result") or {}).get("status") == "success" else 400
    return jsonify(payload), status


@app.route("/api/maintenance/video-previews/quality/scan", methods=["POST"])
def api_maintenance_video_previews_quality_scan():
    data = _json_or_form_data()
    scan, err = video_preview_maintenance.start_quality_scan(
        data.get("path"),
        lib_root=LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": video_preview_maintenance.public_quality_scan(scan)})


@app.route("/api/maintenance/video-previews/quality/status")
def api_maintenance_video_previews_quality_status():
    payload, err = video_preview_maintenance.quality_status_payload(request.args.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/quality/cancel", methods=["POST"])
def api_maintenance_video_previews_quality_cancel():
    data = request.get_json(silent=True) or {}
    scan, err = video_preview_maintenance.cancel_quality_scan(data.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"scan": video_preview_maintenance.public_quality_scan(scan)})


@app.route("/api/maintenance/video-previews/quality/items")
def api_maintenance_video_previews_quality_items():
    payload, err = video_preview_maintenance.quality_items_payload(
        request.args.get("scan_id"),
        status=request.args.get("status"),
        offset=request.args.get("offset"),
        limit=request.args.get("limit"),
    )
    if err:
        status = 404 if err == "Scan not found" else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/video-previews/quality/plan", methods=["POST"])
def api_maintenance_video_previews_quality_plan():
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        request.get_json(silent=True) or {},
        lib_root=LIB_ROOT,
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"plan": plan})


@app.route("/api/maintenance/video-previews/quality/apply", methods=["POST"])
def api_maintenance_video_previews_quality_apply():
    data = request.get_json(silent=True) or {}
    run, err = video_preview_maintenance.start_quality_repair_apply(data.get("plan_id"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"apply": video_preview_maintenance.public_quality_apply_run(run)})


@app.route("/api/maintenance/video-previews/quality/apply/status")
def api_maintenance_video_previews_quality_apply_status():
    payload, err = video_preview_maintenance.quality_apply_status(request.args.get("apply_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/subtitles/scan", methods=["POST"])
def api_maintenance_subtitles_scan():
    data = _json_or_form_data()
    scan, err = subtitle_maintenance.start_scan(
        data.get("path"),
        lib_root=LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": subtitle_maintenance.public_scan(scan)})


@app.route("/api/maintenance/subtitles/status")
def api_maintenance_subtitles_status():
    payload, err = subtitle_maintenance.status_payload(request.args.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/subtitles/cancel", methods=["POST"])
def api_maintenance_subtitles_cancel():
    data = request.get_json(silent=True) or {}
    scan, err = subtitle_maintenance.cancel_scan(data.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"scan": subtitle_maintenance.public_scan(scan)})


@app.route("/api/maintenance/subtitles/items")
def api_maintenance_subtitles_items():
    payload, err = subtitle_maintenance.items_payload(
        request.args.get("scan_id"),
        status=request.args.get("status"),
        offset=request.args.get("offset"),
        limit=request.args.get("limit"),
        q=request.args.get("q"),
    )
    if err:
        status = 404 if err == "Scan not found" else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/subtitles/plan", methods=["POST"])
def api_maintenance_subtitles_plan():
    plan, err = subtitle_maintenance.build_action_plan(
        request.get_json(silent=True) or {},
        lib_root=LIB_ROOT,
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"plan": plan})


@app.route("/api/maintenance/subtitles/apply", methods=["POST"])
def api_maintenance_subtitles_apply():
    data = request.get_json(silent=True) or {}
    run, err = subtitle_maintenance.start_action_apply(
        data.get("plan_id"),
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"apply": subtitle_maintenance.public_apply_run(run)})


@app.route("/api/maintenance/subtitles/apply/status")
def api_maintenance_subtitles_apply_status():
    payload, err = subtitle_maintenance.apply_status(request.args.get("apply_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/subtitles/logs")
def api_maintenance_subtitles_logs():
    return jsonify({"logs": subtitle_maintenance.list_action_logs()})


@app.route("/api/maintenance/subtitles/logs/<log_id>")
def api_maintenance_subtitles_log(log_id):
    payload, err = subtitle_maintenance.action_log(log_id)
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/actor-images/scan", methods=["POST"])
def api_maintenance_actor_images_scan():
    data = _json_or_form_data()
    scan, err = actor_image_maintenance.start_scan(
        data.get("path"),
        lib_root=LIB_ROOT,
        synchronous=_truthy(data.get("synchronous")),
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"scan": actor_image_maintenance.public_scan(scan)})


@app.route("/api/maintenance/actor-images/status")
def api_maintenance_actor_images_status():
    payload, err = actor_image_maintenance.status_payload(request.args.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/actor-images/cancel", methods=["POST"])
def api_maintenance_actor_images_cancel():
    data = request.get_json(silent=True) or {}
    scan, err = actor_image_maintenance.cancel_scan(data.get("scan_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"scan": actor_image_maintenance.public_scan(scan)})


@app.route("/api/maintenance/actor-images/items")
def api_maintenance_actor_images_items():
    payload, err = actor_image_maintenance.items_payload(
        request.args.get("scan_id"),
        status=request.args.get("status"),
        offset=request.args.get("offset"),
        limit=request.args.get("limit"),
    )
    if err:
        status = 404 if err == "Scan not found" else 400
        return jsonify({"error": err}), status
    return jsonify(payload)


@app.route("/api/maintenance/actor-images/plan", methods=["POST"])
def api_maintenance_actor_images_plan():
    plan, err = actor_image_maintenance.build_import_plan(
        request.get_json(silent=True) or {},
        lib_root=LIB_ROOT,
    )
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"plan": plan})


@app.route("/api/maintenance/actor-images/apply", methods=["POST"])
def api_maintenance_actor_images_apply():
    data = request.get_json(silent=True) or {}
    run, err = actor_image_maintenance.start_import_apply(data.get("plan_id"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"apply": actor_image_maintenance.public_apply_run(run)})


@app.route("/api/maintenance/actor-images/apply/status")
def api_maintenance_actor_images_apply_status():
    payload, err = actor_image_maintenance.apply_status(request.args.get("apply_id"))
    if err:
        return jsonify({"error": err}), 404
    return jsonify(payload)


@app.route("/api/maintenance/actor-images/exceptions", methods=["POST"])
def api_maintenance_actor_images_exceptions():
    payload, err = actor_image_maintenance.update_exception(request.get_json(silent=True) or {})
    if err:
        return jsonify({"error": err}), 400
    return jsonify(payload)


@app.route("/api/maintenance/actor-images/logs")
def api_maintenance_actor_images_logs():
    return jsonify({"logs": actor_image_maintenance.list_recent_logs()})


@app.route("/api/maintenance/actor-images/logs/<log_id>")
def api_maintenance_actor_images_log(log_id):
    log, err = actor_image_maintenance.read_log(log_id)
    if err:
        return jsonify({"error": err}), 404
    return jsonify({"log": log})


@app.route("/api/maintenance/actor-images/preview")
def api_maintenance_actor_images_preview():
    path, err = actor_image_maintenance.preview_image_path(request.args.get("path"), lib_root=LIB_ROOT)
    if err:
        return jsonify({"error": err}), 404
    return send_file(path, mimetype=mimetypes.guess_type(path)[0] or "application/octet-stream", conditional=True)


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


def _variant_values(raw):
    values = raw.get("settings") if isinstance(raw.get("settings"), dict) else raw
    return values if isinstance(values, dict) else {}


@app.route("/api/test-lab/run", methods=["POST"])
def api_test_lab_run():
    data = request.get_json(silent=True) or {}
    target = (data.get("video") or "").strip()
    real_target = resolve_case_insensitive(target)
    if (
        not real_target
        or not path_is_under(real_target, LIB_ROOT)
        or not os.path.isfile(real_target)
        or os.path.islink(real_target)
        or os.path.splitext(real_target)[1].lower() not in VIDEO_EXTS
    ):
        return jsonify({"error": "Choose one compatible video file"}), 400

    raw_variants = data.get("variants") or []
    if not isinstance(raw_variants, list) or not (
        test_lab.MIN_VARIANTS <= len(raw_variants) <= test_lab.MAX_VARIANTS
    ):
        return jsonify({"error": "Choose 1 to 4 variants"}), 400

    variants = []
    for index, raw in enumerate(raw_variants, start=1):
        if not isinstance(raw, dict):
            return jsonify({"error": "Variant settings are invalid"}), 400
        values = _variant_values(raw)
        cfg = _job_config_from_values(values)
        name = (raw.get("name") or raw.get("label") or f"Variant {index}").strip()
        variants.append({"name": name, "cfg": cfg})

    run_id, err = test_lab.enqueue_test_run(real_target, variants, lib_root=LIB_ROOT)
    if err:
        return jsonify({"error": err}), 400
    return jsonify({"run_id": run_id, "status": test_lab.run_status_payload()})


@app.route("/api/test-lab/status")
def api_test_lab_status():
    return jsonify(test_lab.status_payload())


@app.route("/api/test-lab/run-status")
def api_test_lab_run_status():
    return jsonify(test_lab.run_status_payload())


@app.route("/api/test-lab/files")
def api_test_lab_files():
    return jsonify(test_lab.inventory_payload())


@app.route("/api/test-lab/delete", methods=["POST"])
def api_test_lab_delete():
    data = request.get_json(silent=True) or {}
    file_ids = data.get("file_ids") or data.get("ids") or []
    if not isinstance(file_ids, list):
        return jsonify({"error": "Choose test GIFs to delete"}), 400
    return jsonify(test_lab.delete_files(file_ids))


@app.route("/api/test-lab/rename", methods=["POST"])
def api_test_lab_rename():
    data = request.get_json(silent=True) or {}
    payload, err = test_lab.rename_file(data.get("file_id"), data.get("name"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify(payload)


@app.route("/api/test-lab/preview", methods=["POST"])
def api_test_lab_preview():
    data = request.get_json(silent=True) or {}
    payload, err = test_lab.request_preview(data.get("file_id"))
    if err:
        return jsonify({"error": err}), 400
    return jsonify(payload)


@app.route("/test-lab/files/<run_id>/<filename>")
def test_lab_file(run_id, filename):
    path = test_lab.test_lab_file_path(run_id, filename)
    if not path:
        return "Not found", 404
    return send_file(path, mimetype="image/gif", conditional=True)


@app.route("/test-lab/download/<run_id>/<filename>")
def test_lab_download(run_id, filename):
    path = test_lab.test_lab_file_path(run_id, filename)
    if not path:
        return "Not found", 404
    return send_file(
        path,
        mimetype="image/gif",
        conditional=True,
        as_attachment=True,
        download_name=filename,
    )


@app.route("/test-lab/previews/<run_id>/<filename>")
def test_lab_preview_file(run_id, filename):
    path = test_lab.test_lab_preview_file_path(run_id, filename)
    if not path:
        return "Not found", 404
    return send_file(path, mimetype="image/gif", conditional=True)


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
