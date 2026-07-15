import json
import os
import re
import subprocess


FFPROBE_TIMEOUT_SECONDS = 30
MAX_SUBTITLE_BYTES = 16 * 1024 * 1024
LIKELY_INCOMPLETE_RATIO = 0.80
LIKELY_INCOMPLETE_GAP_SECONDS = 5 * 60
REVIEW_RATIO = 0.90
REVIEW_GAP_SECONDS = 3 * 60

_TIMESTAMP_RE = re.compile(
    r"(?m)^\s*(\d{1,3}):([0-5]\d):([0-5]\d)[,.](\d{1,3})\s*-->\s*"
    r"(\d{1,3}):([0-5]\d):([0-5]\d)[,.](\d{1,3})"
)


def _seconds(hours, minutes, seconds, fraction):
    milliseconds = int(str(fraction).ljust(3, "0")[:3])
    return int(hours) * 3600 + int(minutes) * 60 + int(seconds) + milliseconds / 1000


def format_timestamp(value):
    try:
        total = max(0, int(round(float(value))))
    except (TypeError, ValueError):
        return ""
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes}:{seconds:02d}"


def analyze_srt(path, video_duration_seconds=None):
    result = {
        "status": "unreadable",
        "label": "SRT could not be read",
        "cue_count": 0,
        "last_timestamp_seconds": None,
        "last_timestamp_label": "",
        "video_duration_seconds": None,
        "video_duration_label": "",
        "coverage_ratio": None,
        "coverage_percent": None,
        "tail_gap_seconds": None,
        "tail_gap_label": "",
    }
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_SUBTITLE_BYTES + 1)
    except OSError:
        return result
    if len(raw) > MAX_SUBTITLE_BYTES:
        result.update(status="unreadable", label="SRT is too large to inspect safely")
        return result
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16", errors="replace")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    timestamps = []
    for match in _TIMESTAMP_RE.finditer(text):
        timestamps.append(_seconds(*match.groups()[4:8]))
    if not timestamps:
        result.update(status="invalid", label="No usable SRT timestamps")
        return result

    last_timestamp = max(timestamps)
    result.update(
        cue_count=len(timestamps),
        last_timestamp_seconds=round(last_timestamp, 3),
        last_timestamp_label=format_timestamp(last_timestamp),
    )
    try:
        duration = float(video_duration_seconds or 0)
    except (TypeError, ValueError):
        duration = 0
    if duration <= 0:
        result.update(
            status="duration_unknown",
            label=f"Ends at {format_timestamp(last_timestamp)}; video runtime unavailable",
        )
        return result

    ratio = last_timestamp / duration
    gap = max(0.0, duration - last_timestamp)
    coverage_percent = round(ratio * 100, 1)
    common = {
        "video_duration_seconds": round(duration, 3),
        "video_duration_label": format_timestamp(duration),
        "coverage_ratio": round(ratio, 4),
        "coverage_percent": coverage_percent,
        "tail_gap_seconds": round(gap, 3),
        "tail_gap_label": format_timestamp(gap),
    }
    if last_timestamp > duration + 120:
        status = "timing_review"
        label = f"Timestamp exceeds video runtime ({coverage_percent:.1f}%)"
    elif ratio < LIKELY_INCOMPLETE_RATIO and gap >= LIKELY_INCOMPLETE_GAP_SECONDS:
        status = "likely_incomplete"
        label = f"Likely incomplete: ends at {format_timestamp(last_timestamp)} of {format_timestamp(duration)}"
    elif ratio < REVIEW_RATIO and gap >= REVIEW_GAP_SECONDS:
        status = "coverage_review"
        label = f"Review coverage: ends at {format_timestamp(last_timestamp)} of {format_timestamp(duration)}"
    else:
        status = "complete"
        label = f"Covers {coverage_percent:.1f}% of video"
    result.update(status=status, label=label, **common)
    return result


def probe_media_duration(path, timeout=FFPROBE_TIMEOUT_SECONDS):
    if not path or not os.path.isfile(path):
        return None
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    try:
        duration = float(
            (json.loads(proc.stdout or "{}").get("format") or {}).get("duration") or 0
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return duration if duration > 0 else None


def quality_sort_key(item):
    quality = (item or {}).get("subtitle_quality") or {}
    rank = {
        "complete": 5,
        "coverage_review": 4,
        "duration_unknown": 3,
        "likely_incomplete": 2,
        "timing_review": 1,
        "invalid": 0,
        "unreadable": 0,
    }.get(quality.get("status"), 0)
    return (
        rank,
        float(quality.get("coverage_ratio") or 0),
        float(quality.get("last_timestamp_seconds") or 0),
        int(quality.get("cue_count") or 0),
        int((item or {}).get("size_bytes") or 0),
    )


def clear_quality_winner(items):
    candidates = list(items or [])
    if len(candidates) < 2:
        return candidates[0] if candidates else None
    ranked = sorted(candidates, key=quality_sort_key, reverse=True)
    best, second = ranked[0], ranked[1]
    best_quality = best.get("subtitle_quality") or {}
    second_quality = second.get("subtitle_quality") or {}
    best_status = best_quality.get("status")
    second_status = second_quality.get("status")
    if best_status == "complete" and second_status in {
        "likely_incomplete", "timing_review", "invalid", "unreadable"
    }:
        return best
    best_ratio = best_quality.get("coverage_ratio")
    second_ratio = second_quality.get("coverage_ratio")
    if best_ratio is not None and second_ratio is not None and best_ratio - second_ratio >= 0.08:
        return best
    return None
