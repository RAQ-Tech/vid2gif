import json
import math
import os
import statistics
import threading
import time

from . import config
from .progress import format_duration, rounded_seconds


SCHEMA_VERSION = 1
MAX_SAMPLES_PER_WORKFLOW = 20
_lock = threading.Lock()


def _path():
    return os.path.join(config.STATE_ROOT, "task_progress_history.json")


def _valid_duration(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return {"schema_version": SCHEMA_VERSION, "workflows": {}}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {"schema_version": SCHEMA_VERSION, "workflows": {}}
    workflows = data.get("workflows")
    if not isinstance(workflows, dict):
        workflows = {}
    return {"schema_version": SCHEMA_VERSION, "workflows": workflows}


def _write(data):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass


def record_duration(workflow, duration_seconds):
    workflow = str(workflow or "").strip()
    duration = _valid_duration(duration_seconds)
    if not workflow or duration is None:
        return False
    with _lock:
        data = _load()
        samples = [
            value
            for value in (
                _valid_duration(item)
                for item in data["workflows"].get(workflow, [])
            )
            if value is not None
        ]
        samples.append(round(duration, 3))
        data["workflows"][workflow] = samples[-MAX_SAMPLES_PER_WORKFLOW:]
        try:
            _write(data)
        except OSError:
            return False
    return True


def duration_estimate(workflow):
    workflow = str(workflow or "").strip()
    if not workflow:
        return {"seconds": None, "sample_count": 0, "confidence": "none"}
    with _lock:
        raw = list((_load().get("workflows") or {}).get(workflow, []))
    samples = [value for value in (_valid_duration(item) for item in raw) if value]
    if not samples:
        return {"seconds": None, "sample_count": 0, "confidence": "calibrating"}
    seconds = statistics.median(samples)
    return {
        "seconds": rounded_seconds(seconds),
        "sample_count": len(samples),
        "confidence": "history" if len(samples) >= 3 else "learning",
    }


def _active(status):
    return status in {"running", "cancelling"}


def _elapsed(task, now):
    started = task.get("_started_ts")
    if started is None:
        return None
    return rounded_seconds(max(0, now - float(started)))


def update_scan(
    task,
    workflow,
    percent,
    label,
    *,
    now=None,
    use_history=True,
    **values,
):
    """Apply honest progress semantics to a scan whose total work is unknown."""
    now = time.time() if now is None else now
    task.update(values)
    status = task.get("status") or "queued"
    task["progress_workflow"] = workflow
    task["progress_label_base"] = str(label or status.title())
    task["elapsed_seconds"] = _elapsed(task, now)

    if _active(status) and use_history:
        estimate = duration_estimate(workflow)
        estimated_total = estimate.get("seconds")
        elapsed = task.get("elapsed_seconds") or 0
        eta = None
        if estimated_total is not None and elapsed < estimated_total:
            eta = max(0, estimated_total - elapsed)
            detail = f"About {format_duration(eta)} remaining"
        elif estimated_total is not None:
            detail = "Taking longer than previous runs"
        else:
            detail = "Learning timing from this run"
        task.update(
            progress_percent=0,
            progress_indeterminate=True,
            eta_seconds=eta,
            eta_confidence=estimate.get("confidence"),
            progress_detail=detail,
            progress_label=f"{label} · {detail}",
        )
        return task

    if _active(status):
        detail = str(
            task.get("progress_detail")
            or "Remaining time varies with library and Emby response time"
        )
        task.update(
            progress_percent=0,
            progress_indeterminate=True,
            eta_seconds=None,
            eta_confidence="none",
            progress_detail=detail,
            progress_label=str(label or status.title()),
        )
        return task

    task["progress_indeterminate"] = False
    task["progress_percent"] = max(0, min(100, int(round(float(percent or 0)))))
    task["eta_seconds"] = 0 if status in {"success", "failed", "cancelled"} else None
    task["eta_confidence"] = "complete" if status == "success" else "none"
    task["progress_detail"] = ""
    task["progress_label"] = str(label or status.title())
    if use_history and status == "success" and not task.get("_progress_history_recorded"):
        elapsed = task.get("elapsed_seconds")
        if elapsed and record_duration(workflow, elapsed):
            task["_progress_history_recorded"] = True
    return task


def public_fields(task):
    task = task or {}
    return {
        "progress_percent": task.get("progress_percent", 0),
        "progress_indeterminate": bool(task.get("progress_indeterminate")),
        "progress_label": task.get("progress_label", ""),
        "progress_detail": task.get("progress_detail", ""),
        "elapsed_seconds": task.get("elapsed_seconds"),
        "eta_seconds": task.get("eta_seconds"),
        "eta_confidence": task.get("eta_confidence", "none"),
        "current_stage": task.get("current_stage", ""),
    }
