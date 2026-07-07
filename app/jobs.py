import queue
import threading
import datetime
import logging
import os
import time
import shutil

from .config import LOG_DIR, VIDEO_EXTS, LIB_ROOT, PROCESS_TMP_ROOT
from .conversion_gate import conversion_lock
from .estimate_history import record_successful_job
from .gif_optimizer import optimize_gif
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
)
from .utils import find_background_image, path_is_under


jobs = {}
job_queue = queue.Queue()
lock = threading.Lock()
queue_paused = threading.Event()
_worker_start_lock = threading.Lock()
_worker_started = False


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
        "output_size_bytes": job.get("output_size_bytes"),
        "started_at": job.get("started_at"),
        "finished_at": job.get("finished_at"),
        "gif_size_before_opt_bytes": job.get("gif_size_before_opt_bytes"),
        "gif_size_after_opt_bytes": job.get("gif_size_after_opt_bytes"),
        "gif_optimization_saved_bytes": job.get("gif_optimization_saved_bytes"),
        "gif_optimization_savings_percent": job.get(
            "gif_optimization_savings_percent"
        ),
        "gif_optimization_status": job.get("gif_optimization_status"),
        "gif_optimization_seconds": job.get("gif_optimization_seconds"),
        "gif_optimization_label": job.get("gif_optimization_label", ""),
    }


def _queue_summary(all_jobs):
    active_batch_ids = {
        j.get("batch_id")
        for j in all_jobs
        if j.get("status") in ("queued", "running") and j.get("batch_id")
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
    running = [j for j in relevant if j.get("status") == "running"]
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

    finished_durations = [
        j.get("elapsed_seconds")
        for j in completed
        if j.get("elapsed_seconds") and j.get("elapsed_seconds") > 0
    ]
    eta = None
    if len(completed) < total:
        running_remaining = sum(
            (100 - max(0, min(100, j.get("progress_percent") or 0))) / 100.0
            for j in running
        )
        remaining_units = len(queued) + running_remaining
        if finished_durations:
            eta = rounded_seconds(
                (sum(finished_durations) / len(finished_durations)) * remaining_units
            )
        elif elapsed and percent > 0:
            eta = rounded_seconds(elapsed * (100 - percent) / percent)

    if percent >= 100:
        label = f"Complete · {total} item{'s' if total != 1 else ''}"
    elif eta is not None:
        label = f"{percent}% complete · {format_duration(eta)} remaining"
    else:
        label = f"{percent}% complete"

    return {
        "total_active_items": total,
        "completed_active_items": len(completed),
        "queue_progress_percent": percent,
        "queue_elapsed_seconds": elapsed,
        "queue_eta_seconds": eta,
        "queue_progress_label": label,
    }


def queue_status_payload():
    with lock:
        running = [
            public_job(j) for j in jobs.values() if j.get("status") == "running"
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
    logger.handlers.clear()
    handler = JobFileHandler(log_path, encoding="utf-8")
    fmt = logging.Formatter("[%(asctime)s] %(message)s", datefmt="%H:%M:%S")
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    logger.propagate = False
    return logger


def new_queue_batch_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def enqueue_job(video_path, cfg, batch_id=None):
    if not path_is_under(video_path, LIB_ROOT):
        return None, "Path must be under /library"
    out_gif = os.path.join(os.path.dirname(video_path), "poster.gif")
    base = os.path.splitext(os.path.basename(video_path))[0]
    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    batch_id = batch_id or job_id
    tmp_dir = os.path.join(PROCESS_TMP_ROOT, f"{base}_{job_id}")
    log_path = os.path.join(LOG_DIR, f"{job_id}.txt")
    logger = create_logger(job_id, log_path)
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
    }
    initialize_job_progress(job)
    with lock:
        jobs[job_id] = job
    job_queue.put(job_id)
    emit_queue_status()
    return job_id, None


def find_videos(root_path):
    vids = []
    for base, _, files in os.walk(root_path):
        for fn in files:
            ext = os.path.splitext(fn)[1].lower()
            if ext in VIDEO_EXTS:
                vids.append(os.path.join(base, fn))
    return vids


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
            mark_job_started(job)
            job["logger"].info("Job started")
            emit_queue_status()
            try:
                os.makedirs(job["tmp_dir"], exist_ok=True)
            except Exception as e:
                mark_job_finished(job, "failed")
                job["logger"].error(f"Failed to create tmp dir: {e}")
                continue
            details, err = probe_video_details(job["video"])
            if err:
                job["logger"].info("Video details unavailable")
            else:
                job["logger"].info(f"Video details: {summarize_video_details(details)}")
            job["logger"].info(
                "Settings: "
                f"{job['cfg']['height']}px high, "
                f"{job['cfg']['fps']} FPS, "
                f"{job['cfg']['clip_len']}s clips"
            )

            dur, err = get_duration(job["video"])
            if err:
                mark_job_finished(job, "failed")
                job["logger"].error(err)
            elif not dur or dur < 0.2:
                mark_job_finished(job, "failed")
                job["logger"].error("Could not read duration.")
            else:
                job["logger"].info(f"Duration: {format_duration(dur)}")
                segs = build_segments(dur, job["cfg"])
                bg_image = find_background_image(job["video"])
                if bg_image:
                    job["logger"].info(f"Background frame: {bg_image}")
                else:
                    job["logger"].info("Background frame: not found")
                job["logger"].info(
                    f"Segments: {len(segs)} clips, about {format_duration(len(segs)*job['cfg']['clip_len'])}"
                )
                tmp_gif = os.path.join(job["tmp_dir"], "poster.gif")
                with conversion_lock:
                    ok, err_msg = make_gif_multi_inputs(
                        job["video"],
                        segs,
                        tmp_gif,
                        job["cfg"],
                        job,
                        background_image=bg_image,
                    )
                    if ok:
                        optimize_gif(tmp_gif, job, job["logger"])
                        try:
                            job["logger"].info("Moving GIF into place")
                            shutil.move(tmp_gif, job["out_gif"])
                        except Exception as e:
                            mark_job_finished(job, "failed")
                            job["logger"].error(f"Failed to move GIF: {e}")
                        else:
                            if os.path.isfile(job["out_gif"]):
                                mark_job_finished(job, "success", job["out_gif"])
                                record_successful_job(job)
                                size = format_size(job.get("output_size_bytes"))
                                elapsed = format_duration(job.get("elapsed_seconds"))
                                job["logger"].info(
                                    f"GIF ready: {job['out_gif']} ({size}, {elapsed})"
                                )
                            else:
                                mark_job_finished(job, "failed")
                                job["logger"].error("Moved GIF not found.")
                    else:
                        mark_job_finished(job, "failed")
                        if not job.get("_ffmpeg_error_logged"):
                            job["logger"].error(err_msg)
        except Exception as e:
            mark_job_finished(job, "failed")
            job["logger"].error(f"Exception: {e}")
        finally:
            if job.get("status") == "running":
                mark_job_finished(job, "failed")
            try:
                tmp_dir = job.get("tmp_dir")
                if tmp_dir and os.path.isdir(tmp_dir):
                    shutil.rmtree(tmp_dir, ignore_errors=False)
            except Exception as e:
                job["logger"].error(f"Failed to remove tmp dir: {e}")
            job_queue.task_done()
            logger = job.get("logger")
            if logger:
                for h in logger.handlers:
                    h.close()
            emit_queue_status()


def start_worker():
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        threading.Thread(target=worker, daemon=True, name="vid2gif-worker").start()
        _worker_started = True
