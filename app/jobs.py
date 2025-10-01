import os
import queue
import threading
import datetime
import logging
import time
import shutil
import json
import subprocess
import shlex

from config import (
    LOG_DIR,
    VIDEO_EXTS,
    IMAGE_EXTS,
    LIB_ROOT,
    TMP_ROOT as PROCESS_TMP_ROOT,
    SKIP_LOG_PATH,
)
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
_skip_log_lock = threading.Lock()


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
    base = os.path.splitext(os.path.basename(video_path))[0]
    job_id = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    tmp_dir = os.path.join(PROCESS_TMP_ROOT, f"{base}_{job_id}")
    log_path = os.path.join(LOG_DIR, f"{job_id}.txt")
    logger = create_logger(job_id, log_path)
    logger.info("Job created")
    logger.info(f"Video: {video_path}")
    logger.info(f"Out  : {out_gif}")

    job = {
        "id": job_id,
        "video": video_path,
        "out_gif": out_gif,
        "tmp_dir": tmp_dir,
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


def _log_skip(video_path, reason):
    timestamp = datetime.datetime.now().isoformat()
    entry = f"{timestamp}\t{video_path}\t{reason}\n"
    with _skip_log_lock:
        with open(SKIP_LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(entry)


def _probe_image_dimensions(image_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height",
        "-of",
        "json",
        image_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as exc:
        return None, None, f"Command {' '.join(shlex.quote(c) for c in cmd)} failed: {exc}"
    if proc.returncode != 0:
        return None, None, (
            f"Command {' '.join(shlex.quote(c) for c in cmd)} returned {proc.returncode}:"
            f" {proc.stderr.strip()}"
        )
    try:
        payload = json.loads(proc.stdout or "{}")
        streams = payload.get("streams", [])
        if not streams:
            return None, None, "No streams found in image probe output"
        info = streams[0]
        width = info.get("width")
        height = info.get("height")
        if not width or not height:
            return None, None, "Image probe did not report dimensions"
        return int(width), int(height), ""
    except Exception as exc:
        return None, None, f"Failed to parse image dimensions: {exc}"


def _select_background_image(video_path, logger):
    directory = os.path.dirname(video_path)
    base = os.path.splitext(os.path.basename(video_path))[0]
    try:
        entries = os.listdir(directory)
    except Exception as exc:
        logger.error(f"Failed to list directory {directory}: {exc}")
        return None

    lower_map = {name.lower(): name for name in entries}
    for ext in IMAGE_EXTS:
        target = f"{base}-background{ext}"
        candidate = lower_map.get(target.lower())
        if not candidate:
            continue
        candidate_path = os.path.join(directory, candidate)
        if os.path.isfile(candidate_path):
            return candidate_path

    for name in sorted(entries):
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext not in IMAGE_EXTS:
            continue
        width, height, err = _probe_image_dimensions(path)
        if err:
            logger.warning(f"Skipping image {path}: {err}")
            continue
        if width > height:
            return path
    return None


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
            background_image = _select_background_image(job["video"], job["logger"])
            if not background_image:
                reason = "No suitable background image found"
                job["status"] = "skipped"
                job["progress_text"] = reason
                job["logger"].warning(reason)
                _log_skip(job["video"], reason)
                continue
            job["background_image"] = background_image
            job["logger"].info(f"Background: {background_image}")
            try:
                os.makedirs(job["tmp_dir"], exist_ok=True)
            except Exception as e:
                job["status"] = "failed"
                job["logger"].error(f"Failed to create tmp dir: {e}")
                continue
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
                tmp_gif = os.path.join(job["tmp_dir"], "poster.gif")
                ok, err_msg = make_gif_multi_inputs(
                    job["video"],
                    segs,
                    tmp_gif,
                    job["cfg"],
                    job,
                    background_image,
                )
                if ok:
                    try:
                        shutil.move(tmp_gif, job["out_gif"])
                    except Exception as e:
                        job["status"] = "failed"
                        job["logger"].error(f"Failed to move GIF: {e}")
                    else:
                        if os.path.isfile(job["out_gif"]):
                            job["status"] = "success"
                            job["logger"].info("GIF ready: " + job["out_gif"])
                        else:
                            job["status"] = "failed"
                            job["logger"].error("Moved GIF not found.")
                else:
                    job["status"] = "failed"
                    job["logger"].error(err_msg)
        except Exception as e:
            job["status"] = "failed"
            job["logger"].error(f"Exception: {e}")
        finally:
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


def start_worker():
    threading.Thread(target=worker, daemon=True).start()

