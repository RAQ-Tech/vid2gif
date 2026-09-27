import datetime
import math
import os
import time

from . import time_estimate


TERMINAL_STATUSES = {"success", "failed", "stopped", "interrupted", "cancelled"}


def utc_iso(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.datetime.fromtimestamp(ts, tz=datetime.timezone.utc).isoformat()


def clamp_percent(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0
    if math.isnan(value) or math.isinf(value):
        return 0
    return max(0, min(100, int(round(value))))


def rounded_seconds(value):
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    if math.isnan(value) or math.isinf(value):
        return None
    return max(0, int(round(value)))


def format_duration(seconds):
    seconds = rounded_seconds(seconds)
    if seconds is None:
        return "unknown"
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes:02d}m"


def format_size(num_bytes):
    if num_bytes is None:
        return ""
    try:
        value = float(num_bytes)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return ""


def progress_label(status, percent, eta_seconds, elapsed_seconds, output_size_bytes, stage=""):
    status = status or "queued"
    percent = clamp_percent(percent)

    if status == "queued":
        return "Waiting"
    if status == "running":
        stage = str(stage or "Processing")
        if percent <= 0:
            return stage
        if eta_seconds is None:
            return f"{stage} · {percent}%"
        return f"{stage} · {percent}% · about {format_duration(eta_seconds)} remaining"
    if status == "success":
        parts = ["Complete"]
        size = format_size(output_size_bytes)
        if size:
            parts.append(size)
        if elapsed_seconds is not None:
            parts.append(format_duration(elapsed_seconds))
        return " · ".join(parts)
    if status == "failed":
        if elapsed_seconds is None:
            return "Failed"
        return f"Failed after {format_duration(elapsed_seconds)}"
    if status == "stopped":
        return "Stopped"
    return status.title()


def initialize_job_progress(job, now=None):
    now = time.time() if now is None else now
    job.setdefault("_created_ts", now)
    job.setdefault("created_at", utc_iso(job["_created_ts"]))
    job.setdefault("_started_ts", None)
    job.setdefault("_finished_ts", None)
    job.setdefault("started_at", None)
    job.setdefault("finished_at", None)
    job.setdefault("progress_percent", 0)
    job.setdefault("elapsed_seconds", None)
    job.setdefault("eta_seconds", None)
    job.setdefault("output_size_bytes", None)
    job.setdefault("expected_duration_seconds", None)
    job.setdefault("eta_confidence", "none")
    job.setdefault("progress_stage", "")
    job["progress_label"] = progress_label(
        job.get("status"),
        job.get("progress_percent"),
        job.get("eta_seconds"),
        job.get("elapsed_seconds"),
        job.get("output_size_bytes"),
        job.get("progress_stage"),
    )
    job["progress_text"] = job["progress_label"]


def mark_job_started(job, now=None):
    from .estimate_history import stage_duration_estimates

    now = time.time() if now is None else now
    job["_started_ts"] = now
    job["started_at"] = utc_iso(now)
    job["status"] = "running"
    job["progress_stage"] = "Preparing"
    job["_stage_started_ts"] = now
    job["_stage_durations"] = {}
    job["_stage_estimates"] = stage_duration_estimates(job.get("cfg") or {})
    update_job_label(job, now=now)


def _job_eta(job, now):
    stage = job.get("progress_stage")
    estimates = job.get("_stage_estimates") or {}
    stages = [
        "Preparing",
        "Rendering",
        "Optimizing" if (job.get("cfg") or {}).get("optimize", True) else "Finalizing",
        "Installing",
    ]
    if stage not in stages:
        return None, "calibrating"
    future = stages[stages.index(stage) + 1 :]
    elapsed = max(0, now - job.get("_stage_started_ts", now))
    confidence = "learning"
    if stage == "Rendering":
        current, confidence = time_estimate.remaining(
            job.get("_render_observations") or {}, job.get("render_duration_seconds"), now
        )
        if current is None and confidence != "recalculating":
            current = time_estimate.historical_remaining(estimates.get(stage), elapsed)
    else:
        current = time_estimate.historical_remaining(estimates.get(stage), elapsed)
    eta = time_estimate.serial_sum([current, *(estimates.get(name) for name in future)])
    if eta is not None:
        return max(1, eta), "learning" if future or confidence != "live" else "live"
    # Whole-job history is a fallback only before measurable rendering. Never let
    # an old total mask an unknown/overdue optimizer or installer.
    if stage == "Preparing" or (stage == "Rendering" and not job.get("_render_observations", {}).get("points")):
        eta = time_estimate.historical_remaining(job.get("expected_duration_seconds"), job.get("elapsed_seconds") or 0)
        return eta, "learning" if eta is not None else "recalculating"
    return None, "recalculating" if confidence == "recalculating" or stage in estimates else "calibrating"


def update_job_label(job, now=None):
    now = time.time() if now is None else now
    started = job.get("_started_ts")
    finished = job.get("_finished_ts")
    if started is not None:
        end = finished if finished is not None else now
        job["elapsed_seconds"] = rounded_seconds(end - started)
    else:
        job["elapsed_seconds"] = None

    if job.get("status") in TERMINAL_STATUSES:
        job["eta_seconds"] = 0
        job["eta_confidence"] = "complete"
    elif job.get("status") == "running":
        job["eta_seconds"], job["eta_confidence"] = _job_eta(job, now)
    elif job.get("status") == "cancelling":
        job["eta_seconds"], job["eta_confidence"] = None, "none"

    job["progress_percent"] = clamp_percent(job.get("progress_percent", 0))
    job["progress_label"] = progress_label(
        job.get("status"),
        job.get("progress_percent"),
        job.get("eta_seconds"),
        job.get("elapsed_seconds"),
        job.get("output_size_bytes"),
        job.get("progress_stage"),
    )
    job["progress_text"] = job["progress_label"]
    return job


def update_render_progress(
    job,
    expected_seconds,
    *,
    out_time_seconds=None,
    frame=None,
    fps=None,
    now=None,
):
    now = time.time() if now is None else now
    completed = None

    if out_time_seconds is not None and expected_seconds and expected_seconds > 0:
        completed = time_estimate.number(out_time_seconds)
    elif frame is not None and fps and expected_seconds and expected_seconds > 0:
        completed = time_estimate.number(float(frame) / float(fps))

    if completed is None:
        return update_job_label(job, now=now)

    if job.get("progress_stage") != "Rendering":
        update_job_stage(job, job.get("progress_percent", 0), "Rendering", now=now)
    job["render_duration_seconds"] = expected_seconds
    # The first output can follow a long palette-generation pass. Anchor the
    # slope at that output, not at process start, to avoid extrapolating startup.
    if completed > 0:
        time_estimate.observe(job.setdefault("_render_observations", {}), completed, now)

    render_ceiling = 92 if (job.get("cfg") or {}).get("optimize", True) else 97
    percent = completed / expected_seconds * render_ceiling
    previous = clamp_percent(job.get("progress_percent", 0))
    percent = max(previous, min(render_ceiling, clamp_percent(percent)))
    job["progress_percent"] = percent
    job["progress_stage"] = "Rendering"

    return update_job_label(job, now=now)


def update_job_stage(job, percent, stage, now=None):
    now = time.time() if now is None else now
    if job.get("progress_stage") != stage:
        _record_stage(job, now)
        job["_stage_started_ts"] = now
    job["progress_percent"] = max(clamp_percent(job.get("progress_percent", 0)), clamp_percent(percent))
    job["progress_stage"] = str(stage or "Processing")
    return update_job_label(job, now=now)


def _record_stage(job, now):
    stage, started = job.get("progress_stage"), job.get("_stage_started_ts")
    if stage and started is not None:
        job.setdefault("_stage_durations", {})[stage] = max(0, now - started)


def mark_job_finished(job, status, output_path=None, now=None):
    now = time.time() if now is None else now
    _record_stage(job, now)
    job["status"] = status
    job["_finished_ts"] = now
    job["finished_at"] = utc_iso(now)
    if status == "success":
        job["progress_percent"] = 100
        if output_path and os.path.isfile(output_path):
            job["output_size_bytes"] = os.path.getsize(output_path)
    job["eta_seconds"] = 0
    job["progress_stage"] = ""
    return update_job_label(job, now=now)
