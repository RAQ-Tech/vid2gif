import json
import math
import os
import statistics
import threading
import time

from . import config
from .progress import format_duration, rounded_seconds


SCHEMA_VERSION = 2
MAX_SAMPLES_PER_WORKFLOW = 20
ESTIMATE_SAMPLE_WINDOW = 7
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


def _valid_units(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value <= 0:
        return None
    return value


def _sample(value):
    if isinstance(value, dict):
        duration = _valid_duration(value.get("duration_seconds"))
        units = _valid_units(value.get("work_units"))
        recorded_at = value.get("recorded_at")
    else:
        duration = _valid_duration(value)
        units = None
        recorded_at = None
    if duration is None:
        return None
    return {
        "duration_seconds": round(duration, 3),
        "work_units": round(units, 3) if units is not None else None,
        "recorded_at": recorded_at,
    }


def _empty_history():
    return {"schema_version": SCHEMA_VERSION, "workflows": {}}


def _load():
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except Exception:
        return _empty_history()
    if not isinstance(data, dict) or data.get("schema_version") not in {1, SCHEMA_VERSION}:
        return _empty_history()
    raw_workflows = data.get("workflows")
    if not isinstance(raw_workflows, dict):
        return _empty_history()
    workflows = {}
    for workflow, raw_samples in raw_workflows.items():
        if not isinstance(raw_samples, list):
            continue
        samples = [sample for value in raw_samples if (sample := _sample(value))]
        if samples:
            workflows[str(workflow)] = samples[-MAX_SAMPLES_PER_WORKFLOW:]
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


def record_duration(workflow, duration_seconds, work_units=None):
    workflow = str(workflow or "").strip()
    duration = _valid_duration(duration_seconds)
    units = _valid_units(work_units)
    if not workflow or duration is None:
        return False
    sample = {
        "duration_seconds": round(duration, 3),
        "work_units": round(units, 3) if units is not None else None,
        "recorded_at": int(time.time()),
    }
    with _lock:
        data = _load()
        samples = list(data["workflows"].get(workflow, []))
        samples.append(sample)
        data["workflows"][workflow] = samples[-MAX_SAMPLES_PER_WORKFLOW:]
        try:
            _write(data)
        except OSError:
            return False
    return True


def duration_estimate(workflow, work_units=None):
    workflow = str(workflow or "").strip()
    target_units = _valid_units(work_units)
    if not workflow:
        return {
            "seconds": None,
            "sample_count": 0,
            "confidence": "none",
            "seconds_per_unit": None,
            "typical_units": None,
        }
    with _lock:
        raw = list((_load().get("workflows") or {}).get(workflow, []))[
            -ESTIMATE_SAMPLE_WINDOW:
        ]
    samples = [sample for value in raw if (sample := _sample(value))]
    unit_samples = [sample for sample in samples if sample.get("work_units")]
    typical_units = (
        statistics.median(sample["work_units"] for sample in unit_samples)
        if unit_samples
        else None
    )
    if target_units is not None:
        rates = [
            sample["duration_seconds"] / sample["work_units"]
            for sample in unit_samples
        ]
        if not rates:
            return {
                "seconds": None,
                "sample_count": 0,
                "confidence": "calibrating",
                "seconds_per_unit": None,
                "typical_units": typical_units,
            }
        rate = statistics.median(rates)
        return {
            "seconds": rounded_seconds(rate * target_units),
            "sample_count": len(rates),
            "confidence": "history" if len(rates) >= 3 else "learning",
            "seconds_per_unit": rate,
            "typical_units": typical_units,
        }
    if not samples:
        return {
            "seconds": None,
            "sample_count": 0,
            "confidence": "calibrating",
            "seconds_per_unit": None,
            "typical_units": None,
        }
    seconds = statistics.median(sample["duration_seconds"] for sample in samples)
    rate = (
        statistics.median(
            sample["duration_seconds"] / sample["work_units"]
            for sample in unit_samples
        )
        if unit_samples
        else None
    )
    return {
        "seconds": rounded_seconds(seconds),
        "sample_count": len(samples),
        "confidence": "history" if len(samples) >= 3 else "learning",
        "seconds_per_unit": rate,
        "typical_units": typical_units,
    }


def plan_estimate(stages):
    stages = list(stages or [])
    if not stages:
        return {"seconds": 0, "confidence": "complete", "sample_count": 0}
    total = 0
    confidences = []
    sample_count = 0
    for raw in stages:
        stage = {"workflow": raw} if isinstance(raw, str) else dict(raw or {})
        units = stage.get("total_units")
        if units is not None and not _valid_units(units):
            continue
        estimate = duration_estimate(stage.get("workflow"), units)
        if estimate.get("seconds") is None:
            return {
                "seconds": None,
                "confidence": "calibrating",
                "sample_count": sample_count,
            }
        total += estimate["seconds"]
        confidences.append(estimate.get("confidence"))
        sample_count += int(estimate.get("sample_count") or 0)
    confidence = "history" if confidences and all(value == "history" for value in confidences) else "learning"
    return {
        "seconds": rounded_seconds(total),
        "confidence": confidence,
        "sample_count": sample_count,
    }


def _active(status):
    return status in {"running", "cancelling"}


def _elapsed(task, now):
    started = task.get("_started_ts")
    if started is None:
        return None
    return rounded_seconds(max(0, now - float(started)))


def _finish_stage(task, now):
    workflow = str(task.get("_progress_stage_workflow") or "")
    started = task.get("_progress_stage_started_ts")
    if not workflow or started is None:
        return False
    duration = max(0, float(now) - float(started))
    units = task.get("_progress_stage_total") or task.get("_progress_stage_completed")
    recorded = record_duration(workflow, duration, units) if duration > 0 else False
    task["_progress_stage_workflow"] = ""
    task["_progress_stage_started_ts"] = None
    task["_progress_stage_completed"] = None
    task["_progress_stage_total"] = None
    return recorded


def _begin_or_update_stage(task, workflow, now, completed_units, total_units):
    workflow = str(workflow or "").strip()
    current = str(task.get("_progress_stage_workflow") or "")
    if current and current != workflow:
        _finish_stage(task, now)
        current = ""
    if workflow and not current:
        task["_progress_stage_workflow"] = workflow
        task["_progress_stage_started_ts"] = float(now)
    if completed_units is not None:
        task["_progress_stage_completed"] = max(0.0, float(completed_units))
    if total_units is not None:
        task["_progress_stage_total"] = max(0.0, float(total_units))


def _live_stage_estimate(task, workflow, now, completed_units, total_units):
    completed = max(0.0, float(completed_units or 0))
    total = _valid_units(total_units)
    history = duration_estimate(workflow, total)
    historical_total = history.get("seconds")
    historical_rate = history.get("seconds_per_unit")
    started = task.get("_progress_stage_started_ts")
    stage_elapsed = max(0.0, float(now) - float(started)) if started is not None else 0.0

    if total is not None and completed >= total:
        return 0, history.get("confidence"), history

    expected_units = total or history.get("typical_units")
    if total is None and expected_units and completed >= expected_units:
        return None, "calibrating", history
    live_rate = None
    if completed > 0 and stage_elapsed >= 1 and expected_units and expected_units >= completed:
        live_rate = stage_elapsed / completed

    if historical_rate is not None and live_rate is not None and expected_units is not None:
        fraction = min(1.0, completed / max(float(expected_units), 1.0))
        live_weight = min(0.9, max(0.2, fraction))
        rate = historical_rate * (1.0 - live_weight) + live_rate * live_weight
        remaining = max(0.0, float(expected_units) - completed)
        confidence = history.get("confidence")
        return rounded_seconds(remaining * rate), confidence, history
    if live_rate is not None and expected_units is not None:
        remaining = max(0.0, float(expected_units) - completed)
        confidence = "learning"
        return rounded_seconds(remaining * live_rate), confidence, history
    if historical_rate is not None and expected_units is not None:
        remaining = max(0.0, float(expected_units) - completed)
        confidence = history.get("confidence")
        return rounded_seconds(remaining * historical_rate), confidence, history
    if historical_total is not None:
        return (
            rounded_seconds(max(0, historical_total - stage_elapsed)),
            history.get("confidence"),
            history,
        )
    return None, "calibrating", history


def _instrumented_update(
    task,
    workflow,
    percent,
    label,
    now,
    stage_workflow,
    completed_units,
    total_units,
    remaining_stages,
    unit_label,
):
    stage_workflow = str(stage_workflow or workflow)
    _begin_or_update_stage(task, stage_workflow, now, completed_units, total_units)
    current_eta, current_confidence, history = _live_stage_estimate(
        task,
        stage_workflow,
        now,
        completed_units,
        total_units,
    )
    future = plan_estimate(remaining_stages)
    future_eta = future.get("seconds")
    if current_eta is None or future_eta is None:
        eta = None
        confidence = "calibrating"
    else:
        eta = rounded_seconds(current_eta + future_eta)
        confidence = (
            "history"
            if current_confidence == "history" and future.get("confidence") in {"history", "complete"}
            else "learning"
        )

    completed = None if completed_units is None else max(0, int(completed_units))
    total = None if total_units is None else max(0, int(total_units))
    has_known_total = completed is not None and total is not None
    task["progress_percent"] = max(0, min(100, int(round(float(percent or 0)))))
    task["progress_indeterminate"] = not has_known_total
    task["eta_seconds"] = eta
    task["eta_confidence"] = confidence
    task["progress_stage"] = stage_workflow
    task["current_stage"] = str(label or task.get("current_stage") or "In progress")
    task["work_completed"] = completed
    task["work_total"] = total
    task["work_unit_label"] = str(unit_label or "items")

    if eta is not None:
        prefix = "About" if confidence == "history" else "Early estimate: about"
        detail = f"{prefix} {format_duration(eta)} remaining"
    elif remaining_stages and future_eta is None:
        detail = "Learning timing for later stages"
    elif history.get("seconds") is not None and history.get("typical_units") and completed:
        detail = "Current workload is larger than recent runs; recalculating"
    else:
        detail = "Learning timing from this run"
    task["progress_detail"] = detail
    task["progress_label"] = f"{label} · {detail}"
    return task


def update_scan(
    task,
    workflow,
    percent,
    label,
    *,
    now=None,
    use_history=True,
    stage_workflow=None,
    completed_units=None,
    total_units=None,
    remaining_stages=None,
    unit_label="items",
    overall_units=None,
    **values,
):
    """Apply persistent historical and live progress semantics to a scan."""
    now = time.time() if now is None else now
    task.update(values)
    status = task.get("status") or "queued"
    task["progress_workflow"] = workflow
    task["progress_label_base"] = str(label or status.title())
    task["elapsed_seconds"] = _elapsed(task, now)
    instrumented = any(
        value is not None
        for value in (stage_workflow, completed_units, total_units, remaining_stages)
    )

    if _active(status) and use_history and instrumented:
        return _instrumented_update(
            task,
            workflow,
            percent,
            label,
            now,
            stage_workflow,
            completed_units,
            total_units,
            remaining_stages,
            unit_label,
        )

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

    if status == "success" and instrumented:
        active_stage = str(task.get("_progress_stage_workflow") or "")
        _begin_or_update_stage(
            task,
            stage_workflow or active_stage or workflow,
            now,
            completed_units,
            total_units,
        )
        _finish_stage(task, now)

    task["progress_indeterminate"] = False
    task["progress_percent"] = max(0, min(100, int(round(float(percent or 0)))))
    task["eta_seconds"] = 0 if status in {"success", "failed", "cancelled"} else None
    task["eta_confidence"] = "complete" if status == "success" else "none"
    task["progress_detail"] = ""
    task["progress_label"] = str(label or status.title())
    task["work_completed"] = None if completed_units is None else max(0, int(completed_units))
    task["work_total"] = None if total_units is None else max(0, int(total_units))
    task["work_unit_label"] = str(unit_label or "items")
    if use_history and status == "success" and not task.get("_progress_history_recorded"):
        elapsed = task.get("elapsed_seconds")
        if elapsed and record_duration(workflow, elapsed, overall_units):
            task["_progress_history_recorded"] = True
    return task


def public_fields(task):
    task = task or {}
    return {
        "progress_percent": task.get("progress_percent", 0),
        "progress_indeterminate": bool(task.get("progress_indeterminate")),
        "progress_label": task.get("progress_label", ""),
        "progress_label_base": task.get("progress_label_base", ""),
        "progress_detail": task.get("progress_detail", ""),
        "elapsed_seconds": task.get("elapsed_seconds"),
        "eta_seconds": task.get("eta_seconds"),
        "eta_confidence": task.get("eta_confidence", "none"),
        "current_stage": task.get("current_stage", ""),
        "progress_stage": task.get("progress_stage", ""),
        "work_completed": task.get("work_completed"),
        "work_total": task.get("work_total"),
        "work_unit_label": task.get("work_unit_label", ""),
    }
