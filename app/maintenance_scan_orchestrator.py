import copy
import json
import os
import threading
import time

from . import actor_image_maintenance
from . import config
from . import dashboard
from . import maintenance
from . import maintenance_scan_store
from . import task_progress
from . import poster_maintenance
from . import subtitle_maintenance
from . import video_preview_maintenance
from .progress import format_duration, utc_iso
from .utils import path_is_under, resolve_case_insensitive


SCAN_TASKS = (
    "overview",
    "duplicates",
    "video_previews_missing",
    "video_previews_quality",
    "subtitles_missing",
    "subtitles_coverage",
    "posters",
    "actor_images",
)
AREA_ALIASES = {
    "video_previews": ("video_previews_missing", "video_previews_quality"),
    "subtitles": ("subtitles_missing", "subtitles_coverage"),
}
ALLOWED_AREAS = (*SCAN_TASKS, *AREA_ALIASES)
AREA_LABELS = {
    "overview": "Library Overview",
    "duplicates": "Duplicates",
    "video_previews": "Video Previews",
    "video_previews_missing": "Missing Video Previews",
    "video_previews_quality": "Video Preview Quality",
    "subtitles": "Subtitles",
    "subtitles_missing": "Missing Subtitles",
    "subtitles_coverage": "Subtitle Coverage",
    "posters": "Landscape Posters",
    "actor_images": "Actor Images",
}
AREA_HREFS = {
    "overview": "/maintenance#overview",
    "duplicates": "/maintenance#duplicates",
    "video_previews": "/maintenance#video-previews",
    "video_previews_missing": "/maintenance#video-previews",
    "video_previews_quality": "/maintenance#video-previews",
    "subtitles": "/maintenance#subtitles",
    "subtitles_missing": "/maintenance#subtitles",
    "subtitles_coverage": "/maintenance#subtitles",
    "posters": "/maintenance#posters",
    "actor_images": "/maintenance#actor-images",
}
TERMINAL = {"success", "failed", "cancelled", "skipped"}
STEP_WORKFLOWS = {
    "overview": "library_overview_scan",
    "duplicates": "duplicate_scan",
    "video_previews_missing": "video_preview_missing_scan",
    "video_previews_quality": "video_preview_quality_scan",
    "subtitles_missing": "subtitle_scan",
    "subtitles_coverage": "subtitle_scan",
    "posters": "poster_scan",
    "actor_images": "actor_image_scan",
}
STEP_STAGE_WORKFLOWS = {
    "overview": ("library_overview_scan.filesystem",),
    "duplicates": (
        "duplicate_scan.discovery",
        "duplicate_scan.analysis",
        "duplicate_scan.emby",
    ),
    "video_previews_missing": (
        "video_preview_missing_scan.filesystem",
        "video_preview_missing_scan.emby",
        "video_preview_missing_scan.profile",
    ),
    "video_previews_quality": (
        "video_preview_quality_scan.catalog",
        "video_preview_quality_scan.analysis",
        "video_preview_quality_scan.emby",
    ),
    "subtitles_missing": (
        "subtitle_scan.filesystem",
        "subtitle_scan.emby",
    ),
    "subtitles_coverage": (
        "subtitle_scan.filesystem",
        "subtitle_scan.quality",
    ),
    "posters": ("poster_scan.filesystem", "poster_scan.emby"),
    "actor_images": (
        "actor_image_scan.people",
        "actor_image_scan.media",
        "actor_image_scan.candidates",
    ),
}

_lock = threading.Lock()
_current = None


def _state_path():
    return os.path.join(config.STATE_ROOT, "maintenance-scans", "dashboard-orchestrator.json")


def _read_state():
    try:
        with open(_state_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {}
    return data if isinstance(data, dict) else {}


def _write_state(data):
    path = _state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=2)
    os.replace(tmp, path)


def _public(run):
    if not run:
        stored = _read_state()
        run = stored.get("last_run")
        if not run:
            return {
                "id": "", "status": "idle", "active": False, "path": stored.get("last_scope") or config.LIB_ROOT,
                "progress_percent": 0, "progress_label": "Ready", "current_area": "", "areas": {},
                "created_at": None, "started_at": None, "finished_at": None, "cancel_requested": False,
            }
        if run.get("status") in {"queued", "running", "cancelling"}:
            run = copy.deepcopy(run)
            run.update(
                status="complete_with_issues",
                active=False,
                current_area="",
                progress_label="Previous maintenance scan was interrupted by a restart",
                error="The active attempt did not complete; latest successful area results remain available.",
            )
    public = copy.deepcopy(run)
    for key in list(public):
        if key.startswith("_"):
            public.pop(key, None)
    public["active"] = public.get("status") in {"queued", "running", "cancelling"}
    return public


def _update(run, **values):
    with _lock:
        run.update(values)


def _area_update(run, area, **values):
    with _lock:
        run["areas"][area].update(values)


def _scan_result_count(scan, area):
    keys = {
        "overview": ("video_count",),
        "duplicates": ("duplicate_group_count",),
        "video_previews_missing": ("missing_count",),
        "video_previews_quality": ("bad_count", "warning_count"),
        "subtitles_missing": ("review_count",),
        "subtitles_coverage": ("review_count",),
        "posters": ("eligible_count",),
        "actor_images": ("missing_actor_count",),
    }.get(area, ())
    return next((int(scan.get(key) or 0) for key in keys if key in scan), 0)


def _step_plan(steps):
    return [
        {"workflow": workflow}
        for step in steps
        for workflow in STEP_STAGE_WORKFLOWS.get(
            step,
            (STEP_WORKFLOWS.get(step, step),),
        )
    ]


def _wait_for_scan(run, area, scan_id, status_loader, cancel_loader, completed_steps, total_steps, sublabel=""):
    run["_active_cancel"] = cancel_loader
    while True:
        if run.get("cancel_requested"):
            try:
                cancel_loader(scan_id)
            except Exception:
                pass
        payload, err = status_loader(scan_id)
        scan = (payload or {}).get("scan") or {}
        status = scan.get("status") or "failed"
        local_progress = int(scan.get("progress_percent") or 0)
        local_indeterminate = bool(scan.get("progress_indeterminate"))
        overall = int(
            100
            * (completed_steps + (0 if local_indeterminate else local_progress / 100.0))
            / max(total_steps, 1)
        )
        label = (
            scan.get("progress_label_base")
            or scan.get("current_stage")
            or sublabel
            or AREA_LABELS[area]
        )
        pending_steps = (run.get("_expanded_steps") or [])[completed_steps + 1:]
        future = task_progress.plan_estimate(_step_plan(pending_steps))
        current_eta = scan.get("eta_seconds")
        eta = (
            int(current_eta) + int(future["seconds"])
            if current_eta is not None and future.get("seconds") is not None
            else None
        )
        eta_confidence = (
            "history"
            if eta is not None
            and scan.get("eta_confidence") == "history"
            and future.get("confidence") in {"history", "complete"}
            else ("learning" if eta is not None else "calibrating")
        )
        eta_detail = (
            f"about {format_duration(eta)} remaining"
            if eta is not None
            else "learning timing for remaining checks"
        )
        _update(
            run,
            progress_percent=overall,
            progress_indeterminate=local_indeterminate,
            progress_completed_steps=completed_steps,
            progress_total_steps=total_steps,
            eta_seconds=eta,
            eta_confidence=eta_confidence,
            progress_label=(
                f"{completed_steps} of {total_steps} checks complete · "
                f"{label} · {eta_detail}"
            ),
        )
        _area_update(
            run, area, status=status, scan_id=scan.get("id") or scan_id,
            progress_percent=local_progress,
            progress_indeterminate=bool(scan.get("progress_indeterminate")),
            eta_seconds=scan.get("eta_seconds"),
            eta_confidence=scan.get("eta_confidence", "none"),
            progress_label=label, error=err or scan.get("error") or "",
            result_count=_scan_result_count(scan, area), finished_at=scan.get("finished_at"),
        )
        if status not in {"queued", "running", "cancelling"}:
            return scan, err
        time.sleep(0.2)


def _library_status(scan_id):
    return dashboard.library_scan_status(), None


def _poster_status(scan_id):
    return poster_maintenance.poster_scan_status(scan_id)


def _run_step(run, area, start_loader, status_loader, cancel_loader, completed_steps, total_steps, sublabel=""):
    _area_update(run, area, status="running", started_at=utc_iso(), progress_label=sublabel or f"Scanning {AREA_LABELS[area]}")
    started, err = start_loader(run["path"])
    if err:
        _area_update(run, area, status="failed", error=err, finished_at=utc_iso(), progress_percent=100)
        return None, err
    scan_id = (started or {}).get("id", "")
    return _wait_for_scan(run, area, scan_id, status_loader, cancel_loader, completed_steps, total_steps, sublabel)


def _execute(run):
    selected = run["selected_areas"]
    expanded = list(selected)
    total_steps = len(expanded)
    run["_expanded_steps"] = expanded
    run["progress_total_steps"] = total_steps
    completed = 0
    issues = False
    initial_estimate = task_progress.plan_estimate(_step_plan(expanded))
    initial_eta = initial_estimate.get("seconds")
    _update(
        run,
        status="running",
        started_at=utc_iso(),
        eta_seconds=initial_eta,
        eta_confidence=initial_estimate.get("confidence", "calibrating"),
        progress_label=(
            f"Starting maintenance scans · about {format_duration(initial_eta)} remaining"
            if initial_eta is not None
            else "Starting maintenance scans · learning timing for selected checks"
        ),
    )
    try:
        for step in expanded:
            if run.get("cancel_requested"):
                break
            area = step
            _update(run, current_area=area)
            if step == "actor_images":
                settings = actor_image_maintenance._settings()
                if not settings.get("emby_url") or not settings.get("emby_api_key"):
                    _area_update(run, area, status="skipped", error="Emby configuration is required", progress_percent=100, progress_label="Skipped: configure Emby", finished_at=utc_iso())
                    completed += 1
                    _update(
                        run,
                        progress_percent=int(100 * completed / max(total_steps, 1)),
                        progress_indeterminate=False,
                        progress_completed_steps=completed,
                    )
                    continue
            if step == "overview":
                result, err = _run_step(run, area, lambda path: dashboard.start_library_scan(path), _library_status, lambda _id: dashboard.cancel_library_scan(), completed, total_steps)
            elif step == "duplicates":
                result, err = _run_step(run, area, lambda path: maintenance.start_duplicate_scan(path), maintenance.status_payload, maintenance.cancel_duplicate_scan, completed, total_steps)
            elif step == "video_previews_missing":
                result, err = _run_step(run, area, lambda path: video_preview_maintenance.start_scan(path), video_preview_maintenance.status_payload, video_preview_maintenance.cancel_scan, completed, total_steps, "Scanning missing video previews")
                if result:
                    _area_update(run, area, missing_scan_id=result.get("id"), missing_count=result.get("missing_count", 0))
            elif step == "video_previews_quality":
                result, err = _run_step(run, area, lambda path: video_preview_maintenance.start_quality_scan(path), video_preview_maintenance.quality_status_payload, video_preview_maintenance.cancel_quality_scan, completed, total_steps, "Checking video preview quality")
                if result:
                    _area_update(run, area, quality_scan_id=result.get("id"), quality_problem_count=int(result.get("bad_count") or 0) + int(result.get("warning_count") or 0), result_count=int(run["areas"][area].get("missing_count") or 0) + int(result.get("bad_count") or 0) + int(result.get("warning_count") or 0))
            elif step == "subtitles_missing":
                result, err = _run_step(
                    run, area,
                    lambda path: subtitle_maintenance.start_scan(path, mode="missing"),
                    subtitle_maintenance.status_payload,
                    subtitle_maintenance.cancel_scan,
                    completed, total_steps,
                    "Scanning for missing subtitles",
                )
            elif step == "subtitles_coverage":
                result, err = _run_step(
                    run, area,
                    lambda path: subtitle_maintenance.start_scan(path, mode="coverage"),
                    subtitle_maintenance.status_payload,
                    subtitle_maintenance.cancel_scan,
                    completed, total_steps,
                    "Checking subtitle duration coverage",
                )
            elif step == "posters":
                result, err = _run_step(run, area, lambda path: poster_maintenance.start_poster_scan(path), _poster_status, poster_maintenance.cancel_poster_scan, completed, total_steps)
            else:
                result, err = _run_step(run, area, lambda path: actor_image_maintenance.start_scan(path), actor_image_maintenance.status_payload, actor_image_maintenance.cancel_scan, completed, total_steps)
            completed += 1
            if err or not result or result.get("status") not in {"success", "skipped"}:
                issues = True
            _update(
                run,
                progress_percent=int(100 * completed / max(total_steps, 1)),
                progress_indeterminate=False,
                progress_completed_steps=completed,
            )
        if run.get("cancel_requested"):
            status = "cancelled"
            label = "Maintenance scan cancelled"
        else:
            status = "complete_with_issues" if issues else "complete"
            label = "Maintenance scans complete with issues" if issues else "Maintenance scans complete"
        _update(run, status=status, progress_percent=100, progress_indeterminate=False, progress_completed_steps=total_steps, eta_seconds=0, progress_label=label, current_area="", finished_at=utc_iso(), _active_cancel=None)
    except Exception as exc:
        _update(run, status="complete_with_issues", error=str(exc), progress_percent=100, progress_indeterminate=False, eta_seconds=0, progress_label="Maintenance scans stopped unexpectedly", current_area="", finished_at=utc_iso(), _active_cancel=None)
    finally:
        try:
            _write_state({"last_scope": run["path"], "last_run": _public(run)})
        except OSError:
            pass


def start(path=None, areas=None, synchronous=False):
    global _current
    real = resolve_case_insensitive(str(path or config.LIB_ROOT).strip())
    if not real or not os.path.isdir(real) or os.path.islink(real) or not path_is_under(real, config.LIB_ROOT):
        return None, "Path not found"
    if areas is None:
        selected = list(SCAN_TASKS)
    elif not isinstance(areas, list) or not areas:
        return None, "Select at least one maintenance area"
    else:
        selected = []
        for value in areas:
            key = str(value or "")
            if key not in ALLOWED_AREAS:
                return None, f"Unknown maintenance area: {key}"
            for task in AREA_ALIASES.get(key, (key,)):
                if task not in selected:
                    selected.append(task)
    with _lock:
        if _current and _current.get("status") in {"queued", "running", "cancelling"}:
            return _public(_current), None
        run_id = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time_ns() % 1000000):06d}"
        run = {
            "id": run_id, "status": "queued", "path": os.path.realpath(real), "selected_areas": selected,
            "created_at": utc_iso(), "started_at": None, "finished_at": None, "progress_percent": 0,
            "progress_indeterminate": False, "eta_seconds": None, "eta_confidence": "none",
            "progress_completed_steps": 0, "progress_total_steps": 0,
            "progress_label": "Queued", "current_area": "", "error": "", "cancel_requested": False,
            "areas": {area: {"key": area, "label": AREA_LABELS[area], "href": AREA_HREFS[area], "status": "queued", "scan_id": "", "progress_percent": 0, "progress_label": "Queued", "error": "", "result_count": 0, "started_at": None, "finished_at": None} for area in selected},
        }
        _current = run
    try:
        stored = _read_state()
        _write_state({"last_scope": run["path"], "last_run": stored.get("last_run")})
    except OSError:
        pass
    if synchronous:
        _execute(run)
    else:
        threading.Thread(target=_execute, args=(run,), daemon=True, name=f"vid2gif-maintenance-orchestrator-{run_id}").start()
    return _public(run), None


def cancel():
    with _lock:
        run = _current
        if not run:
            return None, "Maintenance scan not found"
        if run.get("status") not in {"queued", "running", "cancelling"}:
            return _public(run), None
        run["cancel_requested"] = True
        run["status"] = "cancelling"
        run["progress_label"] = "Cancelling maintenance scans"
        cancel_loader = run.get("_active_cancel")
    if cancel_loader:
        try:
            cancel_loader(None)
        except Exception:
            pass
    return _public(run), None


def status():
    with _lock:
        return _public(_current)


def last_scope():
    with _lock:
        if _current:
            return _current.get("path") or config.LIB_ROOT
    return _read_state().get("last_scope") or config.LIB_ROOT
