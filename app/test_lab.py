import datetime
import hashlib
import json
import os
import queue
import re
import shutil
import threading
import time

from .config import (
    GIF_OPTIMIZE,
    GIF_OPTIMIZE_LEVEL,
    LIB_ROOT,
    LOG_DIR,
    PROCESS_TMP_ROOT,
    TEST_LAB_ROOT,
    VIDEO_EXTS,
)
from .conversion_gate import conversion_lock
from .estimate_history import record_successful_job, sample_count
from .ffmpeg_utils import (
    build_segments,
    get_duration,
    make_gif_multi_inputs,
    probe_video_details,
    summarize_video_details,
)
from .gif_optimizer import normalize_optimize_level, optimize_gif
from .jobs import create_logger
from .progress import (
    TERMINAL_STATUSES,
    clamp_percent,
    format_duration,
    format_size,
    initialize_job_progress,
    mark_job_finished,
    mark_job_started,
    rounded_seconds,
    update_job_label,
    utc_iso,
)
from .utils import find_background_image, path_is_under


SCHEMA_VERSION = 1
MIN_VARIANTS = 2
MAX_VARIANTS = 4
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")
LAB_TERMINAL_STATUSES = TERMINAL_STATUSES | {"partial"}
__test__ = False

test_lab_runs = {}
test_lab_queue = queue.Queue()
test_lab_lock = threading.Lock()
_worker_start_lock = threading.Lock()
_worker_started = False


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _run_dir(run_id):
    return os.path.join(TEST_LAB_ROOT, run_id)


def _manifest_path(run_id):
    return os.path.join(_run_dir(run_id), "manifest.json")


def _file_url(run_id, filename):
    return f"/test-lab/files/{run_id}/{filename}"


def _safe_name(value):
    value = value or ""
    if SAFE_NAME_RE.match(value):
        return value
    return None


def _safe_file_path(run_id, filename):
    run_id = _safe_name(run_id)
    filename = _safe_name(filename)
    if not run_id or not filename or not filename.lower().endswith(".gif"):
        return None
    path = os.path.realpath(os.path.join(TEST_LAB_ROOT, run_id, filename))
    if not path_is_under(path, TEST_LAB_ROOT):
        return None
    return path


def _file_id(run_id, filename):
    if not run_id or not filename:
        return ""
    return f"{run_id}/{filename}"


def _url_for_file_id(file_id):
    if not file_id or "/" not in file_id:
        return ""
    run_id, filename = file_id.split("/", 1)
    if not _safe_file_path(run_id, filename):
        return ""
    return _file_url(run_id, filename)


def test_lab_file_path(run_id, filename):
    path = _safe_file_path(run_id, filename)
    if not path or not os.path.isfile(path):
        return None
    return path


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _relative_identity(path, root):
    try:
        rel = os.path.relpath(
            os.path.realpath(path),
            os.path.realpath(root),
        )
    except (OSError, ValueError):
        rel = os.path.basename(path)
    rel = os.path.normcase(rel).replace(os.sep, "/")
    return _hash_text(rel)


def _file_identity(path, root):
    if not path or not os.path.isfile(path):
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "relative_path_hash": _relative_identity(path, root),
        "size": stat.st_size,
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
    }


def normalized_cfg(cfg):
    return {
        "height": int(cfg.get("height") or 0),
        "fps": "original" if cfg.get("fps") == "original" else int(cfg.get("fps") or 0),
        "clip_len": round(float(cfg.get("clip_len") or 0), 4),
        "percent_points": [int(p) for p in (cfg.get("percent_points") or [])],
        "abs_early": round(float(cfg.get("abs_early") or 0), 4),
        "abs_late_from_end": round(float(cfg.get("abs_late_from_end") or 0), 4),
        "start_buffer": round(float(cfg.get("start_buffer") or 0), 4),
        "end_buffer": round(float(cfg.get("end_buffer") or 0), 4),
        "loop_forever": bool(cfg.get("loop_forever")),
        "smooth": bool(cfg.get("smooth")),
    }


def request_fingerprint(video_path, cfg, lib_root=LIB_ROOT, background_image=None):
    if background_image is None:
        background_image = find_background_image(video_path)
    payload = {
        "schema": 1,
        "source": _file_identity(video_path, lib_root),
        "background": _file_identity(background_image, lib_root),
        "settings": normalized_cfg(cfg),
        "optimization": {
            "enabled": bool(GIF_OPTIMIZE),
            "level": normalize_optimize_level(GIF_OPTIMIZE_LEVEL),
        },
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def settings_label(cfg):
    fps = "original FPS" if cfg.get("fps") == "original" else f"{cfg.get('fps')} FPS"
    clip_len = cfg.get("clip_len")
    try:
        clip_label = f"{float(clip_len):g}s clips"
    except (TypeError, ValueError):
        clip_label = f"{clip_len}s clips"
    smooth = "smooth" if cfg.get("smooth") else "standard"
    return (
        f"{cfg.get('height')}px high, {fps}, {clip_label}, "
        f"{sample_count(cfg)} samples, {smooth}"
    )


def _manifest_variant(variant):
    return {
        "id": variant.get("id", ""),
        "name": variant.get("name", ""),
        "filename": variant.get("filename", ""),
        "request_fingerprint": variant.get("request_fingerprint", ""),
        "reused": bool(variant.get("reused")),
        "reused_file_id": variant.get("reused_file_id", ""),
        "reuse_variant_id": variant.get("reuse_variant_id", ""),
        "settings_label": variant.get("settings_label", ""),
        "cfg": variant.get("cfg") or {},
        "status": variant.get("status", ""),
        "output_size_bytes": variant.get("output_size_bytes"),
        "elapsed_seconds": variant.get("elapsed_seconds"),
        "gif_size_before_opt_bytes": variant.get("gif_size_before_opt_bytes"),
        "gif_size_after_opt_bytes": variant.get("gif_size_after_opt_bytes"),
        "gif_optimization_saved_bytes": variant.get("gif_optimization_saved_bytes"),
        "gif_optimization_savings_percent": variant.get(
            "gif_optimization_savings_percent"
        ),
        "gif_optimization_status": variant.get("gif_optimization_status"),
        "gif_optimization_seconds": variant.get("gif_optimization_seconds"),
        "gif_optimization_label": variant.get("gif_optimization_label", ""),
    }


def _write_manifest(run):
    data = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run.get("id", ""),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
        "source_name": run.get("source_name", ""),
        "variants": [_manifest_variant(v) for v in run.get("variants") or []],
    }
    path = _manifest_path(run["id"])
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        return False
    return True


def _read_manifest(run_id):
    try:
        with open(_manifest_path(run_id), "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return {}
    return data


def _reuse_match(fingerprint):
    if not fingerprint:
        return None
    try:
        run_ids = sorted(os.listdir(TEST_LAB_ROOT), reverse=True)
    except Exception:
        return None

    for run_id in run_ids:
        if not _safe_name(run_id):
            continue
        manifest = _read_manifest(run_id)
        for variant in manifest.get("variants") or []:
            if not isinstance(variant, dict):
                continue
            if variant.get("request_fingerprint") != fingerprint:
                continue
            filename = variant.get("filename", "")
            path = _safe_file_path(run_id, filename)
            if not path or not os.path.isfile(path):
                continue
            file_id = _file_id(run_id, filename)
            return {
                "file_id": file_id,
                "path": path,
                "url": _file_url(run_id, filename),
                "meta": variant,
            }
    return None


def _path_for_file_id(file_id):
    if not file_id or "/" not in file_id:
        return None
    run_id, filename = file_id.split("/", 1)
    return test_lab_file_path(run_id, filename)


def _close_logger(logger):
    if not logger:
        return
    for handler in list(logger.handlers):
        try:
            handler.close()
        finally:
            logger.removeHandler(handler)


def _public_variant(variant):
    update_job_label(variant)
    filename = variant.get("filename", "")
    run_id = variant.get("run_id", "")
    own_file_id = _file_id(run_id, filename)
    reused_file_id = variant.get("reused_file_id", "")
    own_exists = bool(filename and test_lab_file_path(run_id, filename))
    reused_url = _url_for_file_id(reused_file_id)
    file_id = own_file_id if own_exists else reused_file_id
    url = _file_url(run_id, filename) if own_exists else reused_url
    return {
        "id": variant.get("id", ""),
        "name": variant.get("name", ""),
        "status": variant.get("status", ""),
        "reused": bool(variant.get("reused")),
        "progress_label": variant.get("progress_label", ""),
        "progress_percent": variant.get("progress_percent", 0),
        "elapsed_seconds": variant.get("elapsed_seconds"),
        "eta_seconds": variant.get("eta_seconds"),
        "output_size_bytes": variant.get("output_size_bytes"),
        "started_at": variant.get("started_at"),
        "finished_at": variant.get("finished_at"),
        "settings_label": variant.get("settings_label", ""),
        "request_fingerprint": variant.get("request_fingerprint", ""),
        "file_id": file_id if url else "",
        "url": url,
        "gif_size_before_opt_bytes": variant.get("gif_size_before_opt_bytes"),
        "gif_size_after_opt_bytes": variant.get("gif_size_after_opt_bytes"),
        "gif_optimization_saved_bytes": variant.get("gif_optimization_saved_bytes"),
        "gif_optimization_savings_percent": variant.get(
            "gif_optimization_savings_percent"
        ),
        "gif_optimization_status": variant.get("gif_optimization_status"),
        "gif_optimization_seconds": variant.get("gif_optimization_seconds"),
        "gif_optimization_label": variant.get("gif_optimization_label", ""),
        "error": variant.get("error", ""),
    }


def _variant_public_file_id(variant):
    run_id = variant.get("run_id", "")
    filename = variant.get("filename", "")
    own_file_id = _file_id(run_id, filename)
    if filename and test_lab_file_path(run_id, filename):
        return own_file_id
    reused_file_id = variant.get("reused_file_id", "")
    if _path_for_file_id(reused_file_id):
        return reused_file_id
    return ""


def _run_progress(run, now=None):
    now = time.time() if now is None else now
    variants = run.get("variants") or []
    total = max(1, len(variants))
    completed = [v for v in variants if v.get("status") in TERMINAL_STATUSES]
    running = [v for v in variants if v.get("status") == "running"]
    units = float(len(completed))
    units += sum(clamp_percent(v.get("progress_percent")) / 100.0 for v in running)
    percent = clamp_percent(100 * units / total)

    started = run.get("_started_ts")
    finished = run.get("_finished_ts")
    elapsed = None
    if started:
        elapsed = rounded_seconds((finished or now) - started)

    eta = None
    if started and 0 < percent < 100:
        eta = rounded_seconds((now - started) * (100 - percent) / percent)
    elif run.get("status") in LAB_TERMINAL_STATUSES:
        eta = 0

    if run.get("status") == "queued":
        label = "Waiting"
    elif run.get("status") == "running":
        if eta is None:
            label = f"{percent}% complete"
        else:
            label = f"{percent}% complete · {format_duration(eta)} remaining"
    elif run.get("status") == "partial":
        label = f"Complete with issues · {len(completed)} of {len(variants)} variants"
    elif run.get("status") == "success":
        label = f"Complete · {len(variants)} variants"
    elif run.get("status") == "failed":
        label = "Failed"
    else:
        label = run.get("status", "")

    run["progress_percent"] = percent
    run["progress_label"] = label
    run["elapsed_seconds"] = elapsed
    run["eta_seconds"] = eta
    return run


def _public_run(run):
    _run_progress(run)
    return {
        "id": run.get("id", ""),
        "video": run.get("video", ""),
        "source_name": run.get("source_name", ""),
        "status": run.get("status", ""),
        "progress_label": run.get("progress_label", ""),
        "progress_percent": run.get("progress_percent", 0),
        "elapsed_seconds": run.get("elapsed_seconds"),
        "eta_seconds": run.get("eta_seconds"),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "variants": [_public_variant(v) for v in run.get("variants") or []],
    }


def enqueue_test_run(video_path, variants, lib_root=LIB_ROOT):
    if not path_is_under(video_path, lib_root):
        return None, "Path not found"
    if os.path.islink(video_path) or not os.path.isfile(video_path):
        return None, "Choose one compatible video file"
    if os.path.splitext(video_path)[1].lower() not in VIDEO_EXTS:
        return None, "Choose one compatible video file"
    if not isinstance(variants, list) or not (MIN_VARIANTS <= len(variants) <= MAX_VARIANTS):
        return None, "Choose 2 to 4 variants"

    run_id = _now_id()
    run_dir = _run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    created_ts = time.time()
    run = {
        "id": run_id,
        "video": video_path,
        "source_name": os.path.basename(video_path),
        "status": "queued",
        "_created_ts": created_ts,
        "_started_ts": None,
        "_finished_ts": None,
        "created_at": utc_iso(created_ts),
        "started_at": None,
        "finished_at": None,
        "variants": [],
    }

    background_image = find_background_image(video_path)
    first_variant_for_fingerprint = {}
    for index, raw in enumerate(variants, start=1):
        cfg = raw.get("cfg") or {}
        name = (raw.get("name") or f"Variant {index}").strip()[:80]
        filename = f"variant-{index}.gif"
        variant_id = f"variant-{index}"
        fingerprint = request_fingerprint(
            video_path,
            cfg,
            lib_root=lib_root,
            background_image=background_image,
        )
        reuse_match = _reuse_match(fingerprint)
        reuse_variant_id = ""
        reused_file_id = ""
        if fingerprint in first_variant_for_fingerprint:
            reuse_variant_id = first_variant_for_fingerprint[fingerprint]
        elif reuse_match:
            reused_file_id = reuse_match["file_id"]
            first_variant_for_fingerprint[fingerprint] = variant_id
        else:
            first_variant_for_fingerprint[fingerprint] = variant_id

        variant = {
            "id": variant_id,
            "run_id": run_id,
            "name": name or f"Variant {index}",
            "video": video_path,
            "out_gif": os.path.join(run_dir, filename),
            "filename": filename,
            "request_fingerprint": fingerprint,
            "reused_file_id": reused_file_id,
            "reuse_variant_id": reuse_variant_id,
            "reused": False,
            "tmp_dir": os.path.join(PROCESS_TMP_ROOT, "test-lab", run_id, variant_id),
            "status": "queued",
            "cfg": cfg,
            "log_path": os.path.join(LOG_DIR, f"test_lab_{run_id}_{variant_id}.txt"),
            "progress_text": "",
            "settings_label": settings_label(cfg),
            "logger": None,
        }
        initialize_job_progress(variant, now=created_ts)
        run["variants"].append(variant)

    _write_manifest(run)
    with test_lab_lock:
        test_lab_runs[run_id] = run
    start_test_lab_worker()
    test_lab_queue.put(run_id)
    return run_id, None


def _copy_reuse_metrics(variant, meta):
    for key in (
        "gif_size_before_opt_bytes",
        "gif_size_after_opt_bytes",
        "gif_optimization_saved_bytes",
        "gif_optimization_savings_percent",
        "gif_optimization_status",
        "gif_optimization_seconds",
        "gif_optimization_label",
    ):
        if meta.get(key) is not None:
            variant[key] = meta.get(key)


def _complete_reused_file(variant, file_id, logger, meta=None):
    path = _path_for_file_id(file_id)
    if not path:
        return False
    variant["reused"] = True
    variant["reused_file_id"] = file_id
    if meta:
        _copy_reuse_metrics(variant, meta)
    mark_job_finished(variant, "success", path)
    logger.info(f"Reused existing test GIF: {file_id}")
    return True


def _complete_same_run_reuse(run, variant, logger):
    reuse_variant_id = variant.get("reuse_variant_id")
    if not reuse_variant_id:
        return False
    source = next(
        (
            candidate
            for candidate in run.get("variants") or []
            if candidate.get("id") == reuse_variant_id
        ),
        None,
    )
    if not source:
        mark_job_finished(variant, "failed")
        variant["error"] = "Matching variant was not found."
        logger.error(variant["error"])
        return True
    if source.get("status") != "success":
        mark_job_finished(variant, "failed")
        variant["error"] = "Matching variant did not complete."
        logger.error(variant["error"])
        return True

    file_id = _variant_public_file_id(source)
    if not file_id:
        mark_job_finished(variant, "failed")
        variant["error"] = "Matching variant output was not found."
        logger.error(variant["error"])
        return True
    _complete_reused_file(variant, file_id, logger, meta=source)
    return True


def _process_variant(run, variant):
    logger_name = f"test_lab_{run['id']}_{variant['id']}"
    logger = create_logger(logger_name, variant["log_path"])
    variant["logger"] = logger
    mark_job_started(variant)
    logger.info(f"Test variant started: {variant['name']}")
    logger.info(f"Source: {run['video']}")
    logger.info(f"Output: {variant['out_gif']}")
    logger.info(f"Settings: {variant['settings_label']}")

    if _complete_same_run_reuse(run, variant, logger):
        return

    reused_file_id = variant.get("reused_file_id")
    if reused_file_id:
        match_path = _path_for_file_id(reused_file_id)
        if match_path:
            meta = _reuse_match(variant.get("request_fingerprint", ""))
            _complete_reused_file(
                variant,
                reused_file_id,
                logger,
                meta=(meta or {}).get("meta") if meta else None,
            )
            return
        logger.info("Reusable test GIF was missing; regenerating")
        variant["reused_file_id"] = ""

    try:
        os.makedirs(variant["tmp_dir"], exist_ok=True)
    except Exception as e:
        mark_job_finished(variant, "failed")
        variant["error"] = f"Failed to create tmp dir: {e}"
        logger.error(variant["error"])
        return

    details, err = probe_video_details(run["video"])
    if err:
        logger.info("Video details unavailable")
    else:
        logger.info(f"Video details: {summarize_video_details(details)}")

    dur, err = get_duration(run["video"])
    if err:
        mark_job_finished(variant, "failed")
        variant["error"] = err
        logger.error(err)
        return
    if not dur or dur < 0.2:
        mark_job_finished(variant, "failed")
        variant["error"] = "Could not read duration."
        logger.error(variant["error"])
        return

    logger.info(f"Duration: {format_duration(dur)}")
    segs = build_segments(dur, variant["cfg"])
    bg_image = find_background_image(run["video"])
    if bg_image:
        logger.info(f"Background frame: {bg_image}")
    else:
        logger.info("Background frame: not found")
    logger.info(
        f"Segments: {len(segs)} clips, about "
        f"{format_duration(len(segs)*variant['cfg']['clip_len'])}"
    )
    tmp_gif = os.path.join(variant["tmp_dir"], "poster.gif")

    with conversion_lock:
        ok, err_msg = make_gif_multi_inputs(
            run["video"],
            segs,
            tmp_gif,
            variant["cfg"],
            variant,
            background_image=bg_image,
        )
        if not ok:
            mark_job_finished(variant, "failed")
            variant["error"] = err_msg
            if not variant.get("_ffmpeg_error_logged"):
                logger.error(err_msg)
            return

        optimize_gif(tmp_gif, variant, logger)
        try:
            logger.info("Moving test GIF into place")
            shutil.move(tmp_gif, variant["out_gif"])
        except Exception as e:
            mark_job_finished(variant, "failed")
            variant["error"] = f"Failed to move GIF: {e}"
            logger.error(variant["error"])
            return

    if os.path.isfile(variant["out_gif"]):
        mark_job_finished(variant, "success", variant["out_gif"])
        record_successful_job(variant)
        size = format_size(variant.get("output_size_bytes"))
        elapsed = format_duration(variant.get("elapsed_seconds"))
        logger.info(f"Test GIF ready: {variant['out_gif']} ({size}, {elapsed})")
    else:
        mark_job_finished(variant, "failed")
        variant["error"] = "Moved GIF not found."
        logger.error(variant["error"])


def _finish_run(run):
    statuses = [v.get("status") for v in run.get("variants") or []]
    if statuses and all(status == "success" for status in statuses):
        status = "success"
    elif statuses and any(status == "success" for status in statuses):
        status = "partial"
    else:
        status = "failed"
    now = time.time()
    run["status"] = status
    run["_finished_ts"] = now
    run["finished_at"] = utc_iso(now)
    _run_progress(run, now=now)


def worker():
    while True:
        try:
            run_id = test_lab_queue.get(timeout=0.2)
        except queue.Empty:
            continue
        if run_id is None:
            break

        with test_lab_lock:
            run = test_lab_runs.get(run_id)
            if run:
                now = time.time()
                run["status"] = "running"
                run["_started_ts"] = now
                run["started_at"] = utc_iso(now)
                _run_progress(run, now=now)
        if not run:
            test_lab_queue.task_done()
            continue

        try:
            for variant in run.get("variants") or []:
                try:
                    _process_variant(run, variant)
                except Exception as e:
                    mark_job_finished(variant, "failed")
                    variant["error"] = str(e)
                    if variant.get("logger"):
                        variant["logger"].error(f"Exception: {e}")
                finally:
                    try:
                        tmp_dir = variant.get("tmp_dir")
                        if tmp_dir and os.path.isdir(tmp_dir):
                            shutil.rmtree(tmp_dir, ignore_errors=False)
                    except Exception as e:
                        if variant.get("logger"):
                            variant["logger"].error(f"Failed to remove tmp dir: {e}")
                    _close_logger(variant.get("logger"))
                    variant["logger"] = None
                    with test_lab_lock:
                        _run_progress(run)
                        _write_manifest(run)
            with test_lab_lock:
                _finish_run(run)
                _write_manifest(run)
        finally:
            test_lab_queue.task_done()


def start_test_lab_worker():
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        threading.Thread(target=worker, daemon=True, name="vid2gif-test-lab").start()
        _worker_started = True


def _inventory_items():
    items = []
    total_size = 0
    try:
        run_ids = sorted(os.listdir(TEST_LAB_ROOT), reverse=True)
    except Exception:
        return [], 0

    for run_id in run_ids:
        if not _safe_name(run_id):
            continue
        run_dir = _run_dir(run_id)
        if not os.path.isdir(run_dir):
            continue
        manifest = _read_manifest(run_id)
        source_name = manifest.get("source_name", "")
        variants = {
            v.get("filename"): v
            for v in manifest.get("variants") or []
            if isinstance(v, dict)
        }
        try:
            filenames = sorted(os.listdir(run_dir))
        except Exception:
            continue
        for filename in filenames:
            if not filename.lower().endswith(".gif"):
                continue
            path = _safe_file_path(run_id, filename)
            if not path or not os.path.isfile(path):
                continue
            stat = os.stat(path)
            size = stat.st_size
            total_size += size
            meta = variants.get(filename) or {}
            items.append(
                {
                    "id": f"{run_id}/{filename}",
                    "run_id": run_id,
                    "filename": filename,
                    "name": meta.get("name") or filename,
                    "source_name": source_name,
                    "settings_label": meta.get("settings_label", ""),
                    "request_fingerprint": meta.get("request_fingerprint", ""),
                    "size_bytes": size,
                    "size_label": format_size(size),
                    "modified_at": utc_iso(stat.st_mtime),
                    "url": _file_url(run_id, filename),
                    "gif_optimization_label": meta.get("gif_optimization_label", ""),
                }
            )
    return items, total_size


def status_payload():
    with test_lab_lock:
        runs = [_public_run(run) for run in test_lab_runs.values()]
    runs.sort(key=lambda run: run.get("id", ""), reverse=True)
    items, total_size = _inventory_items()
    return {
        "runs": runs,
        "active_run": next(
            (
                run
                for run in runs
                if run.get("status") in {"queued", "running"}
            ),
            runs[0] if runs else None,
        ),
        "files": items,
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "test_lab_root": TEST_LAB_ROOT,
    }


def _active_file_ids():
    active = set()
    with test_lab_lock:
        runs = list(test_lab_runs.values())
    for run in runs:
        if run.get("status") not in LAB_TERMINAL_STATUSES:
            for variant in run.get("variants") or []:
                filename = variant.get("filename", "")
                if filename:
                    active.add(f"{run.get('id')}/{filename}")
                reused_file_id = variant.get("reused_file_id", "")
                if reused_file_id:
                    active.add(reused_file_id)
    return active


def _cleanup_run_dir(run_id):
    run_dir = _run_dir(run_id)
    if not os.path.isdir(run_dir):
        return
    try:
        has_gifs = any(
            name.lower().endswith(".gif") for name in os.listdir(run_dir)
        )
    except Exception:
        return
    if has_gifs:
        return
    shutil.rmtree(run_dir, ignore_errors=True)


def delete_files(file_ids):
    active = _active_file_ids()
    deleted = []
    refused = []
    missing = []
    touched_runs = set()

    for file_id in file_ids or []:
        file_id = str(file_id or "")
        if "/" not in file_id:
            refused.append(file_id)
            continue
        run_id, filename = file_id.split("/", 1)
        path = _safe_file_path(run_id, filename)
        if not path:
            refused.append(file_id)
            continue
        if file_id in active:
            refused.append(file_id)
            continue
        if not os.path.isfile(path):
            missing.append(file_id)
            continue
        try:
            os.remove(path)
            deleted.append(file_id)
            touched_runs.add(run_id)
        except Exception:
            refused.append(file_id)

    for run_id in touched_runs:
        if run_id not in {fid.split("/", 1)[0] for fid in active if "/" in fid}:
            _cleanup_run_dir(run_id)

    payload = status_payload()
    payload.update({"deleted": deleted, "refused": refused, "missing": missing})
    return payload
