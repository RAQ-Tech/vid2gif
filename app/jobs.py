import os
import queue
import threading
import datetime
import logging
import time

from config import LOG_DIR, VIDEO_EXTS, LIB_ROOT
from ffmpeg_utils import (
    get_duration,
    probe_video_details,
    build_segments,
    make_gif_multi_inputs,
)


jobs = {}
job_queue = queue.Queue()
lock = threading.Lock()
queue_paused = threading.Event()


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


def enqueue_job(video_path, cfg):
    if not video_path.startswith(LIB_ROOT):
        return None, "Path must be under /library"
    out_gif = os.path.join(os.path.dirname(video_path), "poster.gif")

    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    log_path = os.path.join(LOG_DIR, f"{job_id}.txt")
    logger = create_logger(job_id, log_path)
    logger.info("Job created")
    logger.info(f"Video: {video_path}")
    logger.info(f"Out  : {out_gif}")

    job = {
        "id": job_id,
        "video": video_path,
        "out_gif": out_gif,
        "status": "queued",
        "cfg": cfg,
        "log_path": log_path,
        "progress_text": "",
        "logger": logger,
    }
    with lock:
        jobs[job_id] = job
    job_queue.put(job_id)
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
            job["status"] = "running"
            job["logger"].info(f"Starting: {job['video']}")
            job["logger"].info("----- PROBE -----")
            details, err = probe_video_details(job["video"])
            if err:
                job["logger"].error(err)
            else:
                job["logger"].info(details)
            job["logger"].info("----- CONFIG ----")
            job["logger"].info(
                f"height={job['cfg']['height']} fps={job['cfg']['fps']} clip_len={job['cfg']['clip_len']}"
            )

            dur, err = get_duration(job["video"])
            if err:
                job["status"] = "failed"
                job["logger"].error(err)
            elif not dur or dur < 0.2:
                job["status"] = "failed"
                job["logger"].error("Could not read duration.")
            else:
                segs = build_segments(dur, job["cfg"])
                job["logger"].info(
                    f"{len(segs)} segments, ~{len(segs)*job['cfg']['clip_len']:.1f}s"
                )
                ok = make_gif_multi_inputs(
                    job["video"], segs, job["out_gif"], job["cfg"], job
                )
                job["status"] = "success" if ok else "failed"
                if ok:
                    job["logger"].info("GIF ready: " + job["out_gif"])
                else:
                    job["logger"].error("ffmpeg failed: " + job["out_gif"])
        except Exception as e:
            job["status"] = "failed"
            job["logger"].error(f"Exception: {e}")
        finally:
            job_queue.task_done()
            logger = job.get("logger")
            if logger:
                for h in logger.handlers:
                    h.close()


def start_worker():
    threading.Thread(target=worker, daemon=True).start()

