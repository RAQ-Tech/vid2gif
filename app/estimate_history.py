import json
import math
import os
import statistics
import threading
import time

from .config import DEFAULTS, STATE_ROOT


SCHEMA_VERSION = 1
MAX_SAMPLES = 500
HISTORY_PATH = os.path.join(STATE_ROOT, "estimate_history.json")
ORIGINAL_FPS_ESTIMATE = 24.0

_history_lock = threading.Lock()


def _to_float(value, default):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(value) or math.isinf(value):
        return default
    return value


def _to_bool(value, default=False):
    if value is None:
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def sample_count(cfg):
    points = cfg.get("percent_points") or []
    count = len(points)
    if _to_float(cfg.get("abs_early"), DEFAULTS["abs_early"]) > 0:
        count += 1
    if _to_float(cfg.get("abs_late_from_end"), DEFAULTS["abs_late_from_end"]) > 0:
        count += 1
    return max(1, count)


def effective_fps(cfg):
    fps = cfg.get("fps", DEFAULTS["fps"])
    if fps == "original":
        return ORIGINAL_FPS_ESTIMATE
    return max(1.0, _to_float(fps, float(DEFAULTS["fps"])))


def settings_unit(cfg):
    height = max(1.0, _to_float(cfg.get("height"), float(DEFAULTS["height"])))
    fps = effective_fps(cfg)
    clip_len = max(0.1, _to_float(cfg.get("clip_len"), float(DEFAULTS["clip_len"])))
    clips = sample_count(cfg)
    return (height / 480.0) ** 2 * (fps / 15.0) * ((clip_len * clips) / 22.0)


def optimize_enabled(cfg):
    return _to_bool((cfg or {}).get("optimize"), DEFAULTS.get("optimize", True))


def _coerce_sample(raw):
    if not isinstance(raw, dict):
        return None
    unit = _to_float(raw.get("settings_unit"), 0)
    elapsed = _to_float(raw.get("elapsed_seconds"), 0)
    size = _to_float(raw.get("output_size_bytes"), 0)
    if unit <= 0 or elapsed <= 0 or size <= 0:
        return None
    return {
        "settings_unit": unit,
        "elapsed_seconds": elapsed,
        "output_size_bytes": int(round(size)),
        "optimize": _to_bool(raw.get("optimize"), DEFAULTS.get("optimize", True)),
        "created_at": _to_float(raw.get("created_at"), time.time()),
    }


def sample_from_job(job):
    if not job or job.get("status") != "success":
        return None
    elapsed = job.get("elapsed_seconds")
    size = job.get("output_size_bytes")
    if not elapsed or not size:
        return None
    sample = {
        "settings_unit": settings_unit(job.get("cfg") or {}),
        "elapsed_seconds": elapsed,
        "output_size_bytes": size,
        "optimize": optimize_enabled(job.get("cfg") or {}),
        "created_at": job.get("_finished_ts") or time.time(),
    }
    return _coerce_sample(sample)


def samples_from_jobs(all_jobs):
    samples = []
    for job in all_jobs:
        sample = sample_from_job(job)
        if sample:
            samples.append(sample)
    return samples


def load_history(path=None):
    path = path or HISTORY_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []

    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return []

    samples = []
    for raw in data.get("samples") or []:
        sample = _coerce_sample(raw)
        if sample:
            samples.append(sample)
    return samples[-MAX_SAMPLES:]


def save_sample(sample, path=None):
    sample = _coerce_sample(sample)
    if not sample:
        return False
    path = path or HISTORY_PATH
    with _history_lock:
        samples = load_history(path)
        samples.append(sample)
        samples = samples[-MAX_SAMPLES:]
        data = {"schema_version": SCHEMA_VERSION, "samples": samples}
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(data, f, separators=(",", ":"))
            os.replace(tmp_path, path)
        except Exception:
            return False
    return True


def record_successful_job(job):
    sample = sample_from_job(job)
    if not sample:
        return False
    return save_sample(sample)


def _plain_count_label(count):
    return f"{count} compatible file" if count == 1 else f"{count} compatible files"


def format_estimate_duration(seconds):
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{seconds} second" if seconds == 1 else f"{seconds} seconds"
    minutes = max(1, int(round(seconds / 60.0)))
    if minutes < 60:
        return f"{minutes} minute" if minutes == 1 else f"{minutes} minutes"
    hours, mins = divmod(minutes, 60)
    if mins == 0:
        return f"{hours} hour" if hours == 1 else f"{hours} hours"
    hour_label = f"{hours} hour" if hours == 1 else f"{hours} hours"
    minute_label = f"{mins} minute" if mins == 1 else f"{mins} minutes"
    return f"{hour_label} {minute_label}"


def format_estimate_size(num_bytes):
    value = float(max(0, num_bytes))
    units = ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if value < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(round(value))} {unit}"
            if unit == "GB" or unit == "TB":
                return f"{value:.2f} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024
    return "0 B"


def estimate_payload(compatible_count, cfg, in_memory_samples=None):
    compatible_count = max(0, int(compatible_count or 0))
    if compatible_count == 0:
        return {
            "compatible_count": 0,
            "estimated_seconds": None,
            "estimated_size_bytes": None,
            "time_label": "",
            "size_label": "",
            "confidence": "none",
            "low_confidence": False,
            "detail": "",
            "message": "No compatible files found",
        }

    samples = []
    samples.extend(load_history())
    samples.extend(in_memory_samples or [])

    seconds_per_unit = []
    bytes_per_unit = []
    target_optimize = optimize_enabled(cfg)
    for sample in samples:
        sample = _coerce_sample(sample)
        if not sample:
            continue
        if sample["optimize"] != target_optimize:
            continue
        seconds_per_unit.append(sample["elapsed_seconds"] / sample["settings_unit"])
        bytes_per_unit.append(sample["output_size_bytes"] / sample["settings_unit"])

    count_label = _plain_count_label(compatible_count)
    if not seconds_per_unit or not bytes_per_unit:
        return {
            "compatible_count": compatible_count,
            "estimated_seconds": None,
            "estimated_size_bytes": None,
            "time_label": "",
            "size_label": "",
            "confidence": "calibration",
            "low_confidence": cfg.get("fps") == "original",
            "detail": "",
            "message": f"{count_label}. Run one GIF to calibrate time and size estimates.",
        }

    unit = settings_unit(cfg)
    estimated_seconds = statistics.median(seconds_per_unit) * unit * compatible_count
    estimated_size = statistics.median(bytes_per_unit) * unit * compatible_count
    time_label = format_estimate_duration(estimated_seconds)
    size_label = format_estimate_size(estimated_size)
    low_confidence = cfg.get("fps") == "original"
    detail = "Lower confidence because original FPS varies." if low_confidence else ""
    return {
        "compatible_count": compatible_count,
        "estimated_seconds": int(round(estimated_seconds)),
        "estimated_size_bytes": int(round(estimated_size)),
        "time_label": time_label,
        "size_label": size_label,
        "confidence": "low" if low_confidence else "history",
        "low_confidence": low_confidence,
        "detail": detail,
        "message": (
            f"{count_label}, estimated time {time_label}, "
            f"estimated total size {size_label}"
        ),
    }
