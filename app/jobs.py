import queue
import threading
import datetime
import json
import logging
import os
import re
import time
import shutil

from .config import LOG_DIR, VIDEO_EXTS, LIB_ROOT, PROCESS_TMP_ROOT
from .estimate_history import job_duration_estimate, record_successful_job
from .impact_metrics import record_creative_output
from .gif_optimizer import optimize_gif
from .operation_gate import OperationCancelled, library_operation
from .file_safety import (
    FileSafetyError,
    atomic_install_file,
    identity_matches,
    regular_file_identity,
    target_state,
)
from .ffmpeg_utils import (
    get_duration,
    probe_video_details,
    summarize_video_details,
    build_segments,
    make_gif_multi_inputs,
)
from .progress import (
    TERMINAL_STATUSES,
    format_duration,
    format_size,
    initialize_job_progress,
    mark_job_finished,
    mark_job_started,
    rounded_seconds,
    update_job_label,
    update_job_stage,
    utc_iso,
)
from .utils import find_background_image


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


JOB_RETENTION_COUNT = max(1, _env_int("JOB_RETENTION_COUNT", 500))
JOB_MAX_AGE_SECONDS = max(60, _env_int("JOB_MAX_AGE_SECONDS", 7 * 24 * 60 * 60))
JOB_LOG_RETENTION_COUNT = max(1, _env_int("JOB_LOG_RETENTION_COUNT", 500))
JOB_LOG_MAX_AGE_SECONDS = max(60, _env_int("JOB_LOG_MAX_AGE_SECONDS", 30 * 24 * 60 * 60))

jobs = {}
job_queue = queue.Queue()
lock = threading.Lock()
queue_paused = threading.Event()
_worker_start_lock = threading.Lock()
_worker_started = False
_restore_lock = threading.Lock()
_restore_complete = False
_persist_lock = threading.Lock()
JOB_STATE_SCHEMA_VERSION = 1


def public_job(job):
    update_job_label(job)
    return {
        "id": job.get("id", ""),
        "video": job.get("video", ""),
        "out_gif": job.get("out_gif", ""),
        "status": job.get("status", ""),
        "progress_text": job.get("progress_text", ""),
        "progress_label": job.get("progress_label", ""),
        "progress_percent": job.get("progress_percent", 0),
        "elapsed_seconds": job.get("elapsed_seconds"),
        "eta_seconds": job.get("eta_seconds"),
        "eta_confidence": job.get("eta_confidence", "none"),
        "expected_duration_seconds": job.get("expected_duration_seconds"),
        "progress_stage": job.get("progress_stage", ""),
        "progress_indeterminate": False,
        "output_size_bytes": job.get("output_size_bytes"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "optimize": (job.get("cfg") or {}).get("optimize"),
        "gif_size_before_opt_bytes": job.get("gif_size_before_opt_bytes"),
        "gif_size_after_opt_bytes": job.get("gif_size_after_opt_bytes"),
        "gif_optimization_saved_bytes": job.get("gif_optimization_saved_bytes"),
        "gif_optimization_savings_percent": job.get(
            "gif_optimization_savings_percent"
        ),
        "gif_optimization_status": job.get("gif_optimization_status"),
        "gif_optimization_seconds": job.get("gif_optimization_seconds"),
        "gif_optimization_label": job.get("gif_optimization_label", ""),
        "cancel_requested": bool(job.get("cancel_requested")),
        "restored": bool(job.get("restored")),
    }


def _job_sort_ts(job):
    return job.get("_finished_ts") or job.get("_created_ts") or 0


def _prune_completed_jobs_locked(now=None):
    now = now or time.time()
    active_batch_ids = {
        job.get("batch_id")
        for job in jobs.values()
        if job.get("status") not in TERMINAL_STATUSES and job.get("batch_id")
    }
    terminal = [
        (job_id, job)
        for job_id, job in jobs.items()
        if job.get("status") in TERMINAL_STATUSES
    ]
    for job_id, job in terminal:
        if job.get("batch_id") in active_batch_ids:
            continue
        finished = job.get("_finished_ts") or job.get("_created_ts") or now
        if now - finished > JOB_MAX_AGE_SECONDS:
            jobs.pop(job_id, None)

    terminal = sorted(
        (
            (job_id, job)
            for job_id, job in jobs.items()
            if job.get("status") in TERMINAL_STATUSES
            and job.get("batch_id") not in active_batch_ids
        ),
        key=lambda item: _job_sort_ts(item[1]),
        reverse=True,
    )
    for job_id, _job in terminal[JOB_RETENTION_COUNT:]:
        jobs.pop(job_id, None)


def _prune_job_logs(active_log_paths, now=None):
    now = now or time.time()
    try:
        names = os.listdir(LOG_DIR)
    except OSError:
        return
    paths = []
    for name in names:
        path = os.path.realpath(os.path.join(LOG_DIR, name))
        if path in active_log_paths or not os.path.isfile(path):
            continue
        try:
            stat = os.stat(path)
        except OSError:
            continue
        paths.append((path, stat.st_mtime))

    paths.sort(key=lambda item: item[1], reverse=True)
    keep = {path for path, _mtime in paths[:JOB_LOG_RETENTION_COUNT]}
    for path, mtime in paths:
        if path in keep and now - mtime <= JOB_LOG_MAX_AGE_SECONDS:
            continue
        try:
            os.remove(path)
        except OSError:
            pass


def prune_job_history():
    with lock:
        _prune_completed_jobs_locked()
        active_log_paths = {
            os.path.realpath(job.get("log_path", ""))
            for job in jobs.values()
            if job.get("log_path")
        }
    _prune_job_logs(active_log_paths)


def _queue_summary(all_jobs):
    active_batch_ids = {
        j.get("batch_id")
        for j in all_jobs
        if j.get("status") in ("queued", "running", "cancelling") and j.get("batch_id")
    }
    if active_batch_ids:
        relevant = [j for j in all_jobs if j.get("batch_id") in active_batch_ids]
    else:
        batch_ids = [j.get("batch_id") for j in all_jobs if j.get("batch_id")]
        latest_batch = max(batch_ids) if batch_ids else None
        relevant = [j for j in all_jobs if j.get("batch_id") == latest_batch]

    total = len(relevant)
    if total == 0:
        return {
            "total_active_items": 0,
            "completed_active_items": 0,
            "queue_progress_percent": 0,
            "queue_elapsed_seconds": None,
            "queue_eta_seconds": None,
            "queue_progress_label": "No active queue",
        }

    now = time.time()
    completed = [j for j in relevant if j.get("status") in TERMINAL_STATUSES]
    running = [j for j in relevant if j.get("status") in {"running", "cancelling"}]
    queued = [j for j in relevant if j.get("status") == "queued"]
    completed_units = float(len(completed))
    completed_units += sum(
        max(0, min(100, j.get("progress_percent") or 0)) / 100.0 for j in running
    )
    percent = max(0, min(100, int(round(100 * completed_units / total))))

    starts = [
        j.get("_started_ts") or j.get("_created_ts")
        for j in relevant
        if j.get("_started_ts") or j.get("_created_ts")
    ]
    finishes = [j.get("_finished_ts") for j in relevant if j.get("_finished_ts")]
    elapsed = None
    if starts:
        end = max(finishes) if len(completed) == total and finishes else now
        elapsed = rounded_seconds(end - min(starts))

    eta = None
    confidence = "none"
    if len(completed) < total:
        remaining = [j.get("eta_seconds") for j in running]
        remaining.extend(j.get("expected_duration_seconds") for j in queued)
        if remaining and all(value is not None for value in remaining):
            eta = rounded_seconds(sum(float(value) for value in remaining))
            confidence = "history" if all(
                j.get("eta_confidence") == "history" for j in running + queued
            ) else "learning"
        elif running or queued:
            confidence = "calibrating"

    if percent >= 100:
        label = f"Complete · {total} item{'s' if total != 1 else ''}"
    elif eta is not None:
        label = f"{percent}% complete · about {format_duration(eta)} remaining"
    else:
        label = f"{percent}% complete · learning timing" if confidence == "calibrating" else f"{percent}% complete"

    return {
        "total_active_items": total,
        "completed_active_items": len(completed),
        "queue_progress_percent": percent,
        "queue_elapsed_seconds": elapsed,
        "queue_eta_seconds": eta,
        "queue_eta_confidence": confidence,
        "queue_progress_label": label,
    }


def queue_status_payload():
    prune_job_history()
    with lock:
        running = [
            public_job(j)
            for j in jobs.values()
            if j.get("status") in {"running", "cancelling"}
        ]
        all_jobs = list(jobs.values())
    with job_queue.mutex:
        queued_ids = list(job_queue.queue)
    with lock:
        queued = [public_job(jobs[jid]) for jid in queued_ids if jid in jobs]
    summary = _queue_summary(all_jobs)
    payload = {"running": running, "queued": queued, "paused": queue_paused.is_set()}
    payload.update(summary)
    payload["summary"] = dict(summary)
    return payload


def emit_queue_status():
    _persist_job_state()
    return queue_status_payload()


class JobFileHandler(logging.FileHandler):
    """File handler that fsyncs after each log record."""

    def emit(self, record):
        super().emit(record)
        if self.stream and hasattr(self.stream, "fileno"):
            os.fsync(self.stream.fileno())


def create_logger(job_id, log_path):
    logger = logging.getLogger(job_id)
    logger.setLevel(logging.INFO)
    for existing in list(logger.handlers):
        existing.close()
        logger.removeHandler(existing)
    handler = JobFileHandler(log_path, encoding="utf-8")
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def close_job_logger(job):
    logger = job.get("logger") if isinstance(job, dict) else None
    if not logger:
        return
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)
    job["logger"] = None


def _job_state_path():
    state_root = os.path.dirname(os.path.realpath(LOG_DIR))
    return os.path.join(state_root, "gif-jobs", "queue.json")


def _serializable_job(job):
    keys = (
        "id",
        "batch_id",
        "video",
        "out_gif",
        "tmp_dir",
        "status",
        "cfg",
        "log_path",
        "source_identity",
        "output_state",
        "progress_text",
        "progress_label",
        "progress_percent",
        "progress_stage",
        "progress_indeterminate",
        "expected_duration_seconds",
        "eta_seconds",
        "eta_confidence",
        "elapsed_seconds",
        "output_size_bytes",
        "created_at",
        "started_at",
        "finished_at",
        "gif_size_before_opt_bytes",
        "gif_size_after_opt_bytes",
        "gif_optimization_saved_bytes",
        "gif_optimization_savings_percent",
        "gif_optimization_status",
        "gif_optimization_seconds",
        "gif_optimization_label",
        "cancel_requested",
        "restored",
        "_created_ts",
        "_started_ts",
        "_finished_ts",
    )
    return {key: job.get(key) for key in keys if key in job}


def _persist_job_state():
    try:
        with _persist_lock:
            with lock:
                saved_jobs = [_serializable_job(job) for job in jobs.values()]
            with job_queue.mutex:
                order = [job_id for job_id in job_queue.queue if job_id is not None]
            payload = {
                "schema_version": JOB_STATE_SCHEMA_VERSION,
                "saved_at": utc_iso(),
                "paused": queue_paused.is_set(),
                "queue": order,
                "jobs": saved_jobs,
            }
            path = _job_state_path()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            temp = f"{path}.{os.getpid()}.tmp"
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _cleanup_restored_tmp(job):
    path = os.path.realpath(str(job.get("tmp_dir") or ""))
    root = os.path.realpath(PROCESS_TMP_ROOT)
    try:
        safe = os.path.commonpath([path, root]) == root and path != root
    except (OSError, ValueError):
        safe = False
    if safe and os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)


def _restore_jobs_once():
    global _restore_complete
    with _restore_lock:
        if _restore_complete:
            return
        _restore_complete = True
        try:
            with open(_job_state_path(), "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            return
        if not isinstance(payload, dict) or payload.get("schema_version") != JOB_STATE_SCHEMA_VERSION:
            return

        restored = {}
        for raw in payload.get("jobs") or []:
            if not isinstance(raw, dict) or not raw.get("id"):
                continue
            job = dict(raw)
            job_id = str(job.get("id") or "")
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", job_id):
                continue
            job["id"] = job_id
            job["restored"] = True
            status = str(job.get("status") or "").lower()
            if status in {"running", "cancelling"}:
                now = time.time()
                job.update(
                    {
                        "status": "interrupted",
                        "progress_label": "Interrupted by container restart",
                        "progress_text": "No media output was installed from the interrupted job",
                        "cancel_requested": False,
                        "_finished_ts": now,
                        "finished_at": utc_iso(now),
                    }
                )
                _cleanup_restored_tmp(job)
            elif status == "queued":
                _cleanup_restored_tmp(job)
                base = os.path.splitext(os.path.basename(str(job.get("video") or "video")))[0]
                job["tmp_dir"] = os.path.join(PROCESS_TMP_ROOT, f"{base}_{job_id}")
                job["log_path"] = os.path.join(LOG_DIR, f"{job_id}.txt")
                job["cancel_requested"] = False
                job["logger"] = create_logger(job_id, job["log_path"])
                job["logger"].info("Queued job restored after container restart")
            restored[job_id] = job

        order = [
            job_id
            for job_id in payload.get("queue") or []
            if job_id in restored and restored[job_id].get("status") == "queued"
        ]
        order.extend(
            job_id
            for job_id, job in sorted(
                restored.items(), key=lambda item: item[1].get("_created_ts") or 0
            )
            if job.get("status") == "queued" and job_id not in order
        )
        with lock:
            for job_id, job in restored.items():
                jobs.setdefault(job_id, job)
        with job_queue.mutex:
            existing = set(job_queue.queue)
            for job_id in order:
                if job_id not in existing:
                    job_queue.queue.append(job_id)
                    job_queue.unfinished_tasks += 1
            if order:
                job_queue.not_empty.notify_all()
        if payload.get("paused"):
            queue_paused.set()
        _persist_job_state()


def new_queue_batch_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_video_identity(video_path):
    return regular_file_identity(
        video_path, root=LIB_ROOT, allowed_extensions=VIDEO_EXTS
    )


def _valid_staged_gif(path):
    try:
        if os.path.getsize(path) < 6:
            return False
        with open(path, "rb") as handle:
            return handle.read(6) in {b"GIF87a", b"GIF89a"}
    except OSError:
        return False


def _ensure_job_safety_state(job):
    source_identity = job.get("source_identity") or _safe_video_identity(job.get("video"))
    if not source_identity:
        raise FileSafetyError("Source is not a safe, compatible video file")
    job["source_identity"] = source_identity
    if "output_state" not in job:
        job["output_state"] = target_state(job.get("out_gif"), root=LIB_ROOT)
    return source_identity, job["output_state"]


def enqueue_job(video_path, cfg, batch_id=None):
    source_identity = _safe_video_identity(video_path)
    if not source_identity:
        return None, "Choose a regular, non-symlink video under /library"
    video_path = os.path.realpath(video_path)
    out_gif = os.path.join(os.path.dirname(video_path), "poster.gif")
    try:
        output_state = target_state(out_gif, root=LIB_ROOT)
    except FileSafetyError as exc:
        return None, str(exc)
    base = os.path.splitext(os.path.basename(video_path))[0]
    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_id = batch_id or job_id
    tmp_dir = os.path.join(PROCESS_TMP_ROOT, f"{base}_{job_id}")
    log_path = os.path.join(LOG_DIR, f"{job_id}.txt")
    logger = create_logger(job_id, log_path)
    duration_estimate = job_duration_estimate(cfg)
    logger.info("Job created")
    logger.info(f"Source: {video_path}")
    logger.info(f"Output: {out_gif}")

    job = {
        "id": job_id,
        "batch_id": batch_id,
        "video": video_path,
        "out_gif": out_gif,
        "tmp_dir": tmp_dir,
        "status": "queued",
        "cfg": cfg,
        "log_path": log_path,
        "progress_text": "",
        "logger": logger,
        "source_identity": source_identity,
        "output_state": output_state,
        "expected_duration_seconds": duration_estimate.get("seconds"),
        "eta_confidence": duration_estimate.get("confidence"),
    }
    initialize_job_progress(job)
    with lock:
        jobs[job_id] = job
    job_queue.put(job_id)
    emit_queue_status()
    return job_id, None


def find_videos(root_path):
    vids = []
    for base, dirs, files in os.walk(root_path, followlinks=False):
        dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            path = os.path.join(base, fn)
            if ext in VIDEO_EXTS and _safe_video_identity(path):
                vids.append(os.path.realpath(path))
    return vids


def cancel_job(job_id):
    job_id = str(job_id or "")
    with lock:
        job = jobs.get(job_id)
    if not job:
        return None, "Job not found"
    status = str(job.get("status") or "").lower()
    if status in TERMINAL_STATUSES:
        return public_job(job), None

    removed = False
    with job_queue.mutex:
        try:
            job_queue.queue.remove(job_id)
            removed = True
            if job_queue.unfinished_tasks > 0:
                job_queue.unfinished_tasks -= 1
            if job_queue.unfinished_tasks == 0:
                job_queue.all_tasks_done.notify_all()
        except ValueError:
            pass

    job["cancel_requested"] = True
    if removed:
        mark_job_finished(job, "stopped")
        job["logger"].info("Queued job cancelled")
        close_job_logger(job)
    else:
        job["status"] = "cancelling"
        job["progress_label"] = "Cancelling GIF generation"
        job["logger"].info("Cancellation requested")
    emit_queue_status()
    return public_job(job), None


def _process_job(job):
    mark_job_started(job)
    job["logger"].info("Job started")
    emit_queue_status()
    try:
        source_identity, output_state = _ensure_job_safety_state(job)
    except FileSafetyError as exc:
        mark_job_finished(job, "failed")
        job["logger"].error(str(exc))
        return
    if not identity_matches(
        job["video"],
        source_identity,
        root=LIB_ROOT,
        allowed_extensions=VIDEO_EXTS,
    ):
        mark_job_finished(job, "failed")
        job["logger"].error("Source video changed while the job was queued")
        return
    if job.get("cancel_requested"):
        mark_job_finished(job, "stopped")
        job["logger"].info("Job cancelled before conversion started")
        return
    try:
        os.makedirs(job["tmp_dir"], exist_ok=True)
    except Exception as exc:
        mark_job_finished(job, "failed")
        job["logger"].error(f"Failed to create tmp dir: {exc}")
        return

    details, err = probe_video_details(job["video"])
    if err:
        job["logger"].info("Video details unavailable")
    else:
        job["logger"].info(f"Video details: {summarize_video_details(details)}")
    cfg = job["cfg"]
    fps_label = "original FPS" if cfg.get("fps") == "original" else f"{cfg.get('fps')} FPS"
    optimize_label = "on" if cfg.get("optimize", True) else "off"
    job["logger"].info(
        "Settings: "
        f"{cfg['height']}px high, {fps_label}, {cfg['clip_len']}s clips, "
        f"optimization {optimize_label}"
    )

    dur, err = get_duration(job["video"])
    if err:
        mark_job_finished(job, "failed")
        job["logger"].error(err)
        return
    if not dur or dur < 0.2:
        mark_job_finished(job, "failed")
        job["logger"].error("Could not read duration.")
        return

    job["logger"].info(f"Duration: {format_duration(dur)}")
    segs = build_segments(dur, job["cfg"])
    bg_image = find_background_image(job["video"])
    job["logger"].info(
        f"Background frame: {bg_image}" if bg_image else "Background frame: not found"
    )
    job["logger"].info(
        f"Segments: {len(segs)} clips, about "
        f"{format_duration(len(segs)*job['cfg']['clip_len'])}"
    )
    tmp_gif = os.path.join(job["tmp_dir"], "poster.gif")
    ok, err_msg = make_gif_multi_inputs(
        job["video"],
        segs,
        tmp_gif,
        job["cfg"],
        job,
        background_image=bg_image,
    )
    if not ok:
        if job.get("cancel_requested"):
            mark_job_finished(job, "stopped")
            job["logger"].info("GIF generation cancelled")
        else:
            mark_job_finished(job, "failed")
            if not job.get("_ffmpeg_error_logged"):
                job["logger"].error(err_msg)
        return

    update_job_stage(
        job,
        92 if job["cfg"].get("optimize", True) else 97,
        "Optimizing" if job["cfg"].get("optimize", True) else "Finalizing",
    )
    optimize_gif(tmp_gif, job, job["logger"])
    if job.get("cancel_requested"):
        mark_job_finished(job, "stopped")
        job["logger"].info("Job cancelled before installation")
        return

    try:
        if not _valid_staged_gif(tmp_gif):
            raise FileSafetyError("Generated GIF failed validation")
        if not identity_matches(
            job["video"],
            source_identity,
            root=LIB_ROOT,
            allowed_extensions=VIDEO_EXTS,
        ):
            raise FileSafetyError("Source video changed during GIF generation")
        staged_identity = regular_file_identity(tmp_gif)
        update_job_stage(job, 98, "Installing")
        job["logger"].info("Installing GIF atomically")
        atomic_install_file(
            tmp_gif,
            job["out_gif"],
            root=LIB_ROOT,
            expected_source=staged_identity,
            expected_target=output_state,
        )
    except Exception as exc:
        mark_job_finished(job, "failed")
        job["logger"].error(f"Failed to install GIF safely: {exc}")
        return

    if not os.path.isfile(job["out_gif"]):
        mark_job_finished(job, "failed")
        job["logger"].error("Installed GIF was not found")
        return

    mark_job_finished(job, "success", job["out_gif"])
    record_successful_job(job)
    record_creative_output(
        job.get("id"),
        "standard",
        output_bytes=job.get("output_size_bytes") or 0,
        saved_bytes=job.get("gif_optimization_saved_bytes") or 0,
        timestamp=job.get("finished_at"),
    )
    size = format_size(job.get("output_size_bytes"))
    elapsed = format_duration(job.get("elapsed_seconds"))
    job["logger"].info(f"GIF ready: {job['out_gif']} ({size}, {elapsed})")


def worker():
    while True:
        if queue_paused.is_set():
            time.sleep(0.2)
            continue
        try:
            job_id = job_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if job_id is None:
            break
        job = jobs.get(job_id)
        if not job:
            job_queue.task_done()
            continue
        try:
            try:
                with library_operation(
                    f"gif:{job_id}",
                    label="Generate GIF",
                    kind="conversion",
                    state=job,
                    href="/gifs#logs",
                    cancel_url=f"/api/jobs/{job_id}/cancel",
                    cancel_requested=lambda: bool(job.get("cancel_requested")),
                ) as activity:
                    _process_job(job)
                    activity.set_outcome(job.get("status"))
            except OperationCancelled:
                mark_job_finished(job, "stopped")
                job["logger"].info("Job cancelled while waiting for library access")
        except Exception as exc:
            mark_job_finished(job, "failed")
            job["logger"].error(f"Exception: {exc}")
        finally:
            if job.get("status") in {"running", "cancelling", "queued"}:
                mark_job_finished(job, "failed")
            try:
                tmp_dir = job.get("tmp_dir")
                if tmp_dir and os.path.isdir(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=False)
            except Exception as exc:
                job["logger"].error(f"Failed to remove tmp dir: {exc}")
            job_queue.task_done()
            close_job_logger(job)
            emit_queue_status()


def start_worker():
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        _restore_jobs_once()
        threading.Thread(target=worker, daemon=True, name="vid2gif-worker").start()
        _worker_started = True
