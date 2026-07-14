import datetime
import math
import os
import time


TERMINAL_STATUSES = {"success", "failed", "stopped", "interrupted", "cancelled"}


def utc_iso(ts=None):
    if ts is None:
        ts = time.time()
    return datetime.datetime.fromtimestamp(
        ts, tz=datetime.timezone.utc
    ).isoformat()


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
    job.setdefault("_projected_total_seconds", job.get("expected_duration_seconds"))
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
    now = time.time() if now is None else now
    job["_started_ts"] = now
    job["started_at"] = utc_iso(now)
    job["status"] = "running"
    job["progress_stage"] = "Preparing"
    update_job_label(job, now=now)


def update_job_label(job, now=None):
    now = time.time() if now is None else now
    started = job.get("_started_ts")
    finished = job.get("_finished_ts")
    if started:
        end = finished if finished is not None else now
        job["elapsed_seconds"] = rounded_seconds(end - started)
    else:
        job["elapsed_seconds"] = None

    if job.get("status") in TERMINAL_STATUSES:
        job["eta_seconds"] = 0
    elif job.get("status") == "running":
        projected = job.get("_projected_total_seconds") or job.get("expected_duration_seconds")
        if projected is not None and job.get("elapsed_seconds") is not None:
            job["eta_seconds"] = rounded_seconds(
                max(0, float(projected) - float(job["elapsed_seconds"]))
            )

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
    percent = None

    if out_time_seconds is not None and expected_seconds and expected_seconds > 0:
        percent = 100 * float(out_time_seconds) / float(expected_seconds)
    elif frame is not None and fps and expected_seconds and expected_seconds > 0:
        expected_frames = float(fps) * float(expected_seconds)
        if expected_frames > 0:
            percent = 100 * float(frame) / expected_frames

    if percent is None:
        return update_job_label(job, now=now)

    render_ceiling = 92 if (job.get("cfg") or {}).get("optimize", True) else 97
    percent = float(percent) * render_ceiling / 100.0
    previous = clamp_percent(job.get("progress_percent", 0))
    percent = max(previous, min(render_ceiling, clamp_percent(percent)))
    job["progress_percent"] = percent
    job["progress_stage"] = "Rendering"

    started = job.get("_started_ts")
    if started and 0 < percent < 100:
        elapsed = max(0.0, now - started)
        job["elapsed_seconds"] = rounded_seconds(elapsed)
        live_total = elapsed / max(0.01, percent / 100.0)
        baseline = job.get("expected_duration_seconds")
        if baseline:
            live_weight = min(0.75, 0.2 + (percent / 100.0) * 0.55)
            target_total = float(baseline) * (1 - live_weight) + live_total * live_weight
        else:
            target_total = live_total
            job["eta_confidence"] = "learning"
        previous_total = job.get("_projected_total_seconds")
        if previous_total:
            target_total = float(previous_total) * 0.8 + target_total * 0.2
        job["_projected_total_seconds"] = max(elapsed, target_total)
        job["eta_seconds"] = rounded_seconds(
            max(0, job["_projected_total_seconds"] - elapsed)
        )

    return update_job_label(job, now=now)


def update_job_stage(job, percent, stage, now=None):
    now = time.time() if now is None else now
    job["progress_percent"] = max(
        clamp_percent(job.get("progress_percent", 0)), clamp_percent(percent)
    )
    job["progress_stage"] = str(stage or "Processing")
    return update_job_label(job, now=now)


def mark_job_finished(job, status, output_path=None, now=None):
    now = time.time() if now is None else now
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
