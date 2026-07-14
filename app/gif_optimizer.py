import os
import shutil
import time

from .config import (
    GIF_OPTIMIZE,
    GIF_OPTIMIZE_LEVEL,
    GIF_OPTIMIZE_TIMEOUT,
    GIFSICLE_BIN,
)
from .progress import format_size
from .process_runner import run_streaming_process


def normalize_optimize_level(level):
    try:
        value = int(level)
    except (TypeError, ValueError):
        return 2
    if value not in (1, 2, 3):
        return 2
    return value


def optimization_label(job):
    status = job.get("gif_optimization_status")
    saved = job.get("gif_optimization_saved_bytes") or 0
    percent = job.get("gif_optimization_savings_percent") or 0

    if status == "optimized":
        return f"Saved {format_size(saved)} ({percent:.1f}%)"
    if status == "kept_original":
        return "No smaller result"
    if status == "disabled":
        return "Disabled"
    if status == "missing":
        return "Gifsicle not found"
    if status == "timeout":
        return "Timed out"
    if status == "failed":
        return "Failed"
    if status == "cancelled":
        return "Cancelled"
    return ""


def _base_metrics(before_size, status, elapsed):
    return {
        "gif_size_before_opt_bytes": before_size,
        "gif_size_after_opt_bytes": before_size,
        "gif_optimization_saved_bytes": 0,
        "gif_optimization_savings_percent": 0.0,
        "gif_optimization_status": status,
        "gif_optimization_seconds": elapsed,
    }


def _record(job, metrics):
    job.update(metrics)
    job["gif_optimization_label"] = optimization_label(job)
    return metrics


def _log(logger, message):
    if logger:
        logger.info(message)


def _log_error(logger, message):
    if logger:
        logger.error(message)


def optimization_enabled(job):
    cfg = (job or {}).get("cfg") or {}
    if "optimize" in cfg:
        value = cfg.get("optimize")
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return bool(GIF_OPTIMIZE)


def optimize_gif(gif_path, job, logger=None):
    started = time.monotonic()
    before_size = os.path.getsize(gif_path)

    if not optimization_enabled(job):
        metrics = _base_metrics(before_size, "disabled", 0)
        _log(logger, "GIF optimization skipped: disabled")
        return _record(job, metrics)

    level = normalize_optimize_level(GIF_OPTIMIZE_LEVEL)
    optimized_path = f"{gif_path}.optimized"
    if os.path.exists(optimized_path):
        os.remove(optimized_path)

    command = GIFSICLE_BIN
    if not os.path.isabs(command) and os.sep not in command and shutil.which(command) is None:
        elapsed = time.monotonic() - started
        metrics = _base_metrics(before_size, "missing", elapsed)
        _log(logger, "GIF optimization skipped: gifsicle not found")
        return _record(job, metrics)

    _log(logger, f"Optimizing GIF with Gifsicle -O{level}")
    args = [
        command,
        f"-O{level}",
        "--output",
        optimized_path,
        gif_path,
    ]

    result = run_streaming_process(
        args,
        cancel_requested=lambda: bool((job or {}).get("cancel_requested")),
        timeout=GIF_OPTIMIZE_TIMEOUT,
        tail_lines=20,
    )
    if result.launch_error:
        elapsed = time.monotonic() - started
        metrics = _base_metrics(before_size, "missing", elapsed)
        _log(logger, "GIF optimization skipped: gifsicle not found")
        return _record(job, metrics)
    if result.cancelled:
        if os.path.exists(optimized_path):
            os.remove(optimized_path)
        elapsed = time.monotonic() - started
        metrics = _base_metrics(before_size, "cancelled", elapsed)
        _log(logger, "GIF optimization cancelled; keeping original GIF")
        return _record(job, metrics)
    if result.timed_out:
        if os.path.exists(optimized_path):
            os.remove(optimized_path)
        elapsed = time.monotonic() - started
        metrics = _base_metrics(before_size, "timeout", elapsed)
        _log(logger, "GIF optimization timed out; keeping original GIF")
        return _record(job, metrics)

    elapsed = time.monotonic() - started
    if result.returncode != 0:
        if os.path.exists(optimized_path):
            os.remove(optimized_path)
        detail = result.output_tail.strip()
        metrics = _base_metrics(before_size, "failed", elapsed)
        if detail:
            _log_error(
                logger,
                f"GIF optimization failed; keeping original GIF: {detail}",
            )
        else:
            _log_error(logger, "GIF optimization failed; keeping original GIF")
        return _record(job, metrics)

    if not os.path.isfile(optimized_path):
        metrics = _base_metrics(before_size, "failed", elapsed)
        _log_error(logger, "GIF optimization failed; no optimized file was created")
        return _record(job, metrics)

    optimized_size = os.path.getsize(optimized_path)
    if optimized_size >= before_size:
        os.remove(optimized_path)
        metrics = _base_metrics(before_size, "kept_original", elapsed)
        _log(
            logger,
            "GIF optimization kept original: "
            f"{format_size(before_size)} to {format_size(optimized_size)} was not smaller",
        )
        return _record(job, metrics)

    os.replace(optimized_path, gif_path)
    saved = before_size - optimized_size
    percent = (saved / before_size * 100.0) if before_size else 0.0
    metrics = {
        "gif_size_before_opt_bytes": before_size,
        "gif_size_after_opt_bytes": optimized_size,
        "gif_optimization_saved_bytes": saved,
        "gif_optimization_savings_percent": percent,
        "gif_optimization_status": "optimized",
        "gif_optimization_seconds": elapsed,
    }
    _log(
        logger,
        "Optimized GIF: "
        f"{format_size(before_size)} to {format_size(optimized_size)}, "
        f"saved {format_size(saved)} ({percent:.1f}%)",
    )
    return _record(job, metrics)
