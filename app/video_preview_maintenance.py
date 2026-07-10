import datetime
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import threading
import time

from . import app_settings
from . import emby_client
from . import poster_maintenance
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .maintenance import QUARANTINE_DIRNAME
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _truthy(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


DEFAULT_REPAIR_ROOT = os.getenv("VIDEO_PREVIEW_REPAIR_ROOT", "/library/.vid2gif-video-preview-repairs")
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
SCAN_RETENTION_COUNT = 10
SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
LARGE_RESULT_COUNT = 100
QUALITY_SAMPLE_LIMIT = max(4, _env_int("VIDEO_PREVIEW_QUALITY_SAMPLE_LIMIT", 24))
QUALITY_DECODE_SAMPLE_LIMIT = max(2, _env_int("VIDEO_PREVIEW_QUALITY_DECODE_SAMPLE_LIMIT", 8))
QUALITY_APPLY_LARGE_FILE_COUNT = 100
FFPROBE_TIMEOUT_SECONDS = max(1, _env_int("VIDEO_PREVIEW_FFPROBE_TIMEOUT", 10))
JPEG_DECODE_TIMEOUT_SECONDS = max(1, _env_int("VIDEO_PREVIEW_JPEG_DECODE_TIMEOUT", 5))
LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "video-previews")
LOG_INDEX = os.path.join(LOG_DIR, "index.json")
LOG_RETENTION_COUNT = 25
LOG_MAX_BYTES = 1024 * 1024
__test__ = False

preview_scans = {}
quality_scans = {}
quality_plans = {}
quality_apply_runs = {}
preview_lock = threading.Lock()


class ScanCancelled(Exception):
    pass


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _path_id(path, lib_root):
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(lib_root))
    except (OSError, ValueError):
        rel = os.path.basename(path)
    return _hash_text(os.path.normcase(rel).replace(os.sep, "/"))[:20]


def _relative_path(path, root):
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return os.path.basename(path)


def _validate_scan_path(path, lib_root):
    target = str(path or "").strip()
    if not target:
        return None, "Choose a folder under the library"
    real = resolve_case_insensitive(target)
    if (
        not real
        or not path_is_under(real, lib_root)
        or not os.path.isdir(real)
        or os.path.islink(real)
    ):
        return None, "Path not found"
    return os.path.realpath(real), None


def _bif_stem(name):
    stem, ext = os.path.splitext(str(name or ""))
    return stem if ext.lower() == ".bif" else ""


def bif_matches_video(bif_name, video_stem):
    stem = _bif_stem(bif_name)
    if not stem:
        return False
    lower_stem = stem.lower()
    lower_video = str(video_stem or "").lower()
    if lower_stem == lower_video:
        return True
    if not lower_stem.startswith(lower_video):
        return False
    if len(lower_stem) == len(lower_video):
        return True
    return lower_stem[len(lower_video)] in {"-", ".", "_", " ", "["}


def bif_interval_seconds(bif_name, video_stem=""):
    stem = _bif_stem(bif_name)
    if not stem:
        return None
    if video_stem and bif_matches_video(bif_name, video_stem):
        suffix = stem[len(video_stem):]
    else:
        suffix = stem
    match = re.search(r"(?:^|[-_. ])(\d{2,5})[-_.](\d{1,5})$", suffix)
    if not match:
        return None
    try:
        return int(match.group(2))
    except (TypeError, ValueError):
        return None


def _scan_cancel_requested(scan):
    if not scan:
        return False
    with preview_lock:
        return bool(scan.get("cancel_requested"))


def _check_cancelled(scan):
    if _scan_cancel_requested(scan):
        raise ScanCancelled()


def _set_scan_progress(scan, percent, label, **values):
    with preview_lock:
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


def _coerce_page(offset, limit):
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = ITEM_PAGE_DEFAULT
    return max(0, offset), max(1, min(ITEM_PAGE_MAX, limit))


def _safe_stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def _public_bif(path, video_stem):
    stat = _safe_stat(path)
    return {
        "name": os.path.basename(path),
        "path": os.path.realpath(path),
        "size_bytes": stat.st_size if stat else 0,
        "size_label": format_size(stat.st_size if stat else 0),
        "interval_seconds": bif_interval_seconds(os.path.basename(path), video_stem),
        "modified_at": utc_iso(stat.st_mtime) if stat else None,
    }


def _video_item(video_path, folder_files, lib_root):
    name = os.path.basename(video_path)
    stem = os.path.splitext(name)[0]
    bifs = []
    folder = os.path.dirname(video_path)
    for entry in folder_files:
        full_path = os.path.join(folder, entry)
        if os.path.islink(full_path) or not os.path.isfile(full_path):
            continue
        if bif_matches_video(entry, stem):
            bifs.append(_public_bif(full_path, stem))
    bifs.sort(key=lambda item: item["name"].lower())
    status = "present"
    detail = "Preview BIF found"
    if not bifs:
        status = "missing"
        detail = "No matching BIF file found beside the video"
    stat = _safe_stat(video_path)
    return {
        "id": _path_id(video_path, lib_root),
        "path": os.path.realpath(video_path),
        "relative_path": _relative_path(video_path, lib_root),
        "folder": os.path.realpath(folder),
        "name": name,
        "status": status,
        "detail": detail,
        "bif_count": len(bifs),
        "bifs": bifs,
        "size_bytes": stat.st_size if stat else 0,
        "size_label": format_size(stat.st_size if stat else 0),
        "modified_at": utc_iso(stat.st_mtime) if stat else None,
    }


def _skip_dir(base, dirname, lib_root):
    path = os.path.join(base, dirname)
    if os.path.islink(path):
        return True
    if dirname == QUARANTINE_DIRNAME:
        return True
    if dirname in {"_previews", "__pycache__"}:
        return True
    settings = app_settings.load_settings()
    move_root = os.path.realpath(settings.get("duplicate_move_root") or "")
    if move_root and path_is_under(path, move_root) and path_is_under(move_root, lib_root):
        return True
    repair_root = os.path.realpath(DEFAULT_REPAIR_ROOT)
    if repair_root and path_is_under(path, repair_root) and path_is_under(repair_root, lib_root):
        return True
    return False


def _scan_videos(scan, lib_root):
    items = []
    path = scan["path"]
    for base, dirs, files in os.walk(path, followlinks=False):
        _check_cancelled(scan)
        dirs[:] = [d for d in dirs if not _skip_dir(base, d, lib_root)]
        video_files = [
            filename
            for filename in files
            if os.path.splitext(filename)[1].lower() in VIDEO_EXTS
        ]
        for filename in sorted(video_files, key=str.lower):
            _check_cancelled(scan)
            video_path = os.path.join(base, filename)
            if os.path.islink(video_path) or not os.path.isfile(video_path):
                continue
            item = _video_item(video_path, files, lib_root)
            items.append(item)
            if len(items) % 25 == 0:
                _set_scan_progress(
                    scan,
                    min(95, 5 + len(items) // 10),
                    f"Scanned {len(items)} videos",
                    scanned_video_count=len(items),
                )
    items.sort(key=lambda item: item["relative_path"].lower())
    return items


def _counts(items):
    missing = sum(1 for item in items if item.get("status") == "missing")
    present = sum(1 for item in items if item.get("status") != "missing")
    return {
        "scanned_video_count": len(items),
        "present_count": present,
        "missing_count": missing,
    }


def public_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    missing_count = counts.get("missing_count", 0)
    return {
        "id": scan.get("id", ""),
        "path": scan.get("path", ""),
        "status": scan.get("status", ""),
        "progress_percent": scan.get("progress_percent", 0),
        "progress_label": scan.get("progress_label", ""),
        "error": scan.get("error", ""),
        "created_at": scan.get("created_at"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "active": scan.get("status") in SCAN_ACTIVE_STATUSES,
        "cancel_requested": bool(scan.get("cancel_requested")),
        "scanned_video_count": counts.get("scanned_video_count", scan.get("scanned_video_count", 0)),
        "present_count": counts.get("present_count", 0),
        "missing_count": missing_count,
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": missing_count >= LARGE_RESULT_COUNT,
        "recent_logs": list_recent_logs(),
    }


def _prune_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(preview_scans):
        scan = preview_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if now - finished > SCAN_MAX_AGE_SECONDS:
            preview_scans.pop(scan_id, None)
    terminal = sorted(
        (
            (scan_id, scan)
            for scan_id, scan in preview_scans.items()
            if scan.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    for scan_id, _scan in terminal[SCAN_RETENTION_COUNT:]:
        preview_scans.pop(scan_id, None)


def _active_scan_locked():
    active = [
        scan
        for scan in preview_scans.values()
        if scan.get("status") in SCAN_ACTIVE_STATUSES
    ]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


def _run_scan(scan, lib_root):
    try:
        started = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning video preview sidecars",
            status="running",
            _started_ts=started,
            started_at=utc_iso(started),
        )
        items = _scan_videos(scan, lib_root)
        counts = _counts(items)
        finished = time.time()
        label = f"{counts['missing_count']} missing, {counts['present_count']} present"
        _set_scan_progress(
            scan,
            100,
            label,
            status="success",
            items=items,
            counts=counts,
            scanned_video_count=counts["scanned_video_count"],
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log(
            "scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "counts": counts,
            },
        )
    except ScanCancelled:
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            "Scan cancelled",
            status="cancelled",
            error="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log(
            "scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "status": "cancelled",
                "scanned_video_count": scan.get("scanned_video_count", 0),
            },
        )
    except Exception as exc:
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            "Scan failed",
            status="failed",
            error=str(exc),
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log(
            "scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "status": "failed",
                "error": str(exc),
                "scanned_video_count": scan.get("scanned_video_count", 0),
            },
        )


def start_scan(path, lib_root=LIB_ROOT, synchronous=False):
    real_path, err = _validate_scan_path(path, lib_root)
    if err:
        return None, err
    scan_id = _now_id()
    created = time.time()
    scan = {
        "id": scan_id,
        "path": real_path,
        "status": "queued",
        "progress_percent": 0,
        "progress_label": "Queued",
        "error": "",
        "_created_ts": created,
        "_started_ts": None,
        "_finished_ts": None,
        "created_at": utc_iso(created),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "scanned_video_count": 0,
        "items": [],
        "counts": {},
    }
    with preview_lock:
        _prune_scans_locked()
        active = _active_scan_locked()
        if active:
            return active, None
        preview_scans[scan_id] = scan
    if synchronous:
        _run_scan(scan, lib_root)
    else:
        threading.Thread(
            target=_run_scan,
            args=(scan, lib_root),
            daemon=True,
            name=f"vid2gif-video-preview-scan-{scan_id}",
        ).start()
    return scan, None


def cancel_scan(scan_id=None):
    target_id = str(scan_id or "")
    now = time.time()
    with preview_lock:
        _prune_scans_locked(now)
        scan = preview_scans.get(target_id) if target_id else _active_scan_locked()
        if not scan:
            return None, "Scan not found"
        if scan.get("status") in SCAN_TERMINAL_STATUSES:
            return scan, None
        scan["cancel_requested"] = True
        if scan.get("status") == "queued":
            scan.update(
                {
                    "status": "cancelled",
                    "progress_percent": 100,
                    "progress_label": "Scan cancelled",
                    "_finished_ts": now,
                    "finished_at": utc_iso(now),
                }
            )
        else:
            scan.update({"status": "cancelling", "progress_label": "Cancelling scan"})
    return scan, None


def status_payload(scan_id=None):
    with preview_lock:
        _prune_scans_locked()
        if scan_id:
            scan = preview_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif preview_scans:
            scan = max(preview_scans.values(), key=lambda item: item.get("_created_ts") or 0)
        else:
            scan = None
    return {"scan": public_scan(scan)}, None


def items_payload(scan_id, status="missing", offset=0, limit=ITEM_PAGE_DEFAULT):
    offset, limit = _coerce_page(offset, limit)
    status = str(status or "missing").lower()
    if status not in {"missing", "present", "all"}:
        status = "missing"
    with preview_lock:
        _prune_scans_locked()
        scan = preview_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        items = list(scan.get("items") or [])
    if status != "all":
        if status == "present":
            items = [item for item in items if item.get("status") != "missing"]
        else:
            items = [item for item in items if item.get("status") == status]
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "status": status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(page),
        "has_previous": offset > 0,
        "has_next": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "large_result": total >= LARGE_RESULT_COUNT,
        "items": page,
    }, None


BIF_MAGIC = b"\x89BIF\r\n\x1a\n"


def _stat_identity(path):
    stat = _safe_stat(path)
    if not stat:
        return None
    return {
        "real_path": os.path.realpath(path),
        "size": stat.st_size,
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
    }


def _identity_matches(path, identity):
    if not identity or not os.path.isfile(path):
        return False
    current = _stat_identity(path)
    if not current:
        return False
    return (
        current.get("real_path") == identity.get("real_path")
        and current.get("size") == identity.get("size")
        and current.get("mtime_ns") == identity.get("mtime_ns")
    )


def parse_bif(path, sample_limit=QUALITY_SAMPLE_LIMIT):
    errors = []
    path = os.path.realpath(path)
    stat = _safe_stat(path)
    if not stat:
        return {"valid": False, "errors": ["BIF file is missing"], "path": path}
    try:
        with open(path, "rb") as f:
            header = f.read(64)
            if len(header) < 64:
                return {
                    "valid": False,
                    "errors": ["BIF header is incomplete"],
                    "path": path,
                    "file_size": stat.st_size,
                }
            if header[:8] != BIF_MAGIC:
                errors.append("BIF magic bytes are invalid")
            version, image_count, multiplier = struct.unpack_from("<III", header, 8)
            if version != 0:
                errors.append(f"BIF version {version} is not supported")
            if image_count <= 0:
                errors.append("BIF contains no images")
            index_size = (image_count + 1) * 8
            if image_count > 250000 or 64 + index_size > stat.st_size:
                errors.append("BIF index is outside the file bounds")
                return {
                    "valid": False,
                    "errors": errors,
                    "path": path,
                    "file_size": stat.st_size,
                    "version": version,
                    "image_count": image_count,
                    "timestamp_multiplier_ms": multiplier or 1000,
                }
            index_raw = f.read(index_size)
            if len(index_raw) != index_size:
                errors.append("BIF index is incomplete")
                return {
                    "valid": False,
                    "errors": errors,
                    "path": path,
                    "file_size": stat.st_size,
                    "version": version,
                    "image_count": image_count,
                    "timestamp_multiplier_ms": multiplier or 1000,
                }
            entries = [
                struct.unpack_from("<II", index_raw, index * 8)
                for index in range(image_count + 1)
            ]
            if entries[-1][0] != 0xFFFFFFFF:
                errors.append("BIF end marker is invalid")
            offsets = [entry[1] for entry in entries]
            if offsets[-1] > stat.st_size:
                errors.append("BIF end offset is outside the file")
            for index, offset in enumerate(offsets):
                if offset < 64 + index_size or offset > stat.st_size:
                    errors.append(f"BIF offset {index} is outside the data section")
                    break
                if index and offset < offsets[index - 1]:
                    errors.append("BIF offsets are not ordered")
                    break
            frames = []
            for index in range(image_count):
                start = offsets[index]
                end = offsets[index + 1]
                size = end - start
                frames.append(
                    {
                        "index": index,
                        "timestamp_ms": entries[index][0] * (multiplier or 1000),
                        "offset": start,
                        "size": size,
                    }
                )
                if size <= 0:
                    errors.append(f"BIF frame {index} is empty")
                    break

            if errors:
                return {
                    "valid": False,
                    "errors": errors,
                    "path": path,
                    "file_size": stat.st_size,
                    "version": version,
                    "image_count": image_count,
                    "timestamp_multiplier_ms": multiplier or 1000,
                    "frames": frames,
                    "samples": [],
                }

            sample_indexes = _sample_indexes(image_count, sample_limit)
            samples = []
            for index in sample_indexes:
                frame = frames[index]
                f.seek(frame["offset"])
                data = f.read(frame["size"])
                if len(data) != frame["size"]:
                    errors.append(f"BIF frame {index} could not be read")
                    continue
                has_markers = data.startswith(b"\xff\xd8") and data.rstrip().endswith(b"\xff\xd9")
                if not has_markers:
                    errors.append(f"BIF frame {index} does not look like a JPEG")
                samples.append(
                    {
                        **frame,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "jpeg_markers": has_markers,
                        "bytes": data,
                    }
                )
    except OSError as exc:
        return {"valid": False, "errors": [str(exc)], "path": path, "file_size": stat.st_size}

    return {
        "valid": not errors,
        "errors": errors,
        "path": path,
        "file_size": stat.st_size,
        "version": version,
        "image_count": image_count,
        "timestamp_multiplier_ms": multiplier or 1000,
        "frames": frames,
        "samples": samples,
    }


def _sample_indexes(count, limit=QUALITY_SAMPLE_LIMIT):
    count = max(0, int(count or 0))
    limit = max(1, int(limit or 1))
    if count <= limit:
        return list(range(count))
    if limit == 1:
        return [0]
    indexes = {
        int(round(index * (count - 1) / (limit - 1)))
        for index in range(limit)
    }
    return sorted(indexes)


def _probe_video_duration(video_path, timeout=FFPROBE_TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                video_path,
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
        duration = float((proc.stdout or "").strip())
    except (TypeError, ValueError):
        return None
    return duration if duration > 0 else None


def _decode_jpeg_fingerprint(data, timeout=JPEG_DECODE_TIMEOUT_SECONDS):
    try:
        proc = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "image2pipe",
                "-vcodec",
                "mjpeg",
                "-i",
                "pipe:0",
                "-frames:v",
                "1",
                "-vf",
                "scale=16:9:force_original_aspect_ratio=decrease,pad=16:9:(ow-iw)/2:(oh-ih)/2:color=black,format=gray",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "pipe:1",
            ],
            input=data,
            capture_output=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    raw = proc.stdout or b""
    if proc.returncode != 0 or not raw:
        return None
    average = sum(raw) / len(raw)
    quantized = bytes(min(15, max(0, value // 16)) for value in raw)
    return {
        "hash": hashlib.sha256(quantized).hexdigest(),
        "average_luma": average,
    }


def _max_equal_run(values):
    best = 0
    current = 0
    previous = object()
    for value in values:
        if value == previous:
            current += 1
        else:
            current = 1
            previous = value
        best = max(best, current)
    return best


def _quality_counts(items):
    bad = sum(1 for item in items if item.get("status") == "bad")
    warning = sum(1 for item in items if item.get("status") == "warning")
    ok = sum(1 for item in items if item.get("status") == "ok")
    return {
        "checked_bif_count": len(items),
        "bad_count": bad,
        "warning_count": warning,
        "ok_count": ok,
        "repairable_count": bad,
    }


def analyze_bif_quality(bif_path, video_path, lib_root):
    parsed = parse_bif(bif_path)
    stat = _safe_stat(bif_path)
    identity = _stat_identity(bif_path) or {}
    interval_from_name = bif_interval_seconds(os.path.basename(bif_path), os.path.splitext(os.path.basename(video_path))[0])
    interval_seconds = interval_from_name or max(1, int(round((parsed.get("timestamp_multiplier_ms") or 1000) / 1000)))
    duration = _probe_video_duration(video_path)
    reasons = []
    confidence = 0
    sample_summary = {
        "sampled_frames": 0,
        "unique_raw_frames": 0,
        "unique_decoded_frames": 0,
        "max_repeated_run": 0,
        "blank_frames": 0,
        "decode_available": False,
    }

    if not parsed.get("valid"):
        reasons.extend(parsed.get("errors") or ["BIF could not be parsed"])
        confidence = 100
    else:
        samples = parsed.get("samples") or []
        raw_hashes = [sample.get("sha256") for sample in samples if sample.get("sha256")]
        decoded = []
        decode_indexes = set(_sample_indexes(len(samples), min(QUALITY_DECODE_SAMPLE_LIMIT, len(samples))))
        for sample_index, sample in enumerate(samples):
            if sample_index not in decode_indexes:
                continue
            result = _decode_jpeg_fingerprint(sample.get("bytes") or b"")
            if result:
                decoded.append(result)
        frame_keys = [item["hash"] for item in decoded] or raw_hashes
        blank_count = sum(1 for item in decoded if item.get("average_luma", 255) < 8 or item.get("average_luma", 0) > 247)
        unique_raw = len(set(raw_hashes))
        unique_decoded = len(set(item["hash"] for item in decoded))
        max_run = _max_equal_run(frame_keys)
        sample_summary = {
            "sampled_frames": len(samples),
            "unique_raw_frames": unique_raw,
            "unique_decoded_frames": unique_decoded,
            "max_repeated_run": max_run,
            "blank_frames": blank_count,
            "decode_available": bool(decoded),
        }
        if len(samples) >= 5 and unique_raw <= max(2, int(len(samples) * 0.20)):
            reasons.append("Most sampled BIF frames are byte-identical")
            confidence = max(confidence, 95)
        if decoded and len(decoded) >= 5 and unique_decoded <= max(2, int(len(decoded) * 0.20)):
            reasons.append("Most sampled BIF frames decode to the same image")
            confidence = max(confidence, 95)
        if len(frame_keys) >= 6 and max_run >= max(5, int(len(frame_keys) * 0.60)):
            reasons.append("Sampled BIF frames contain a long repeated-image run")
            confidence = max(confidence, 90)
        if decoded and blank_count >= max(3, int(len(decoded) * 0.80)):
            reasons.append("Most sampled BIF frames are blank")
            confidence = max(confidence, 90)

    status = "ok"
    if confidence >= 90:
        status = "bad"
    elif confidence >= 60 or reasons:
        status = "warning"
    if not reasons:
        reasons.append("BIF passed quality checks")

    return {
        "id": _path_id(bif_path, lib_root),
        "status": status,
        "repairable": status == "bad",
        "confidence": confidence,
        "reason": "; ".join(reasons),
        "reasons": reasons,
        "path": os.path.realpath(bif_path),
        "relative_path": _relative_path(bif_path, lib_root),
        "name": os.path.basename(bif_path),
        "video_path": os.path.realpath(video_path),
        "video_relative_path": _relative_path(video_path, lib_root),
        "video_name": os.path.basename(video_path),
        "frame_count": parsed.get("image_count") or 0,
        "duration_seconds": duration,
        "interval_seconds": interval_seconds,
        "timestamp_multiplier_ms": parsed.get("timestamp_multiplier_ms") or 0,
        "sample_summary": sample_summary,
        "size_bytes": stat.st_size if stat else 0,
        "size_label": format_size(stat.st_size if stat else 0),
        "modified_at": utc_iso(stat.st_mtime) if stat else None,
        "identity": identity,
    }


def _find_matching_video_for_bif(bif_path, folder_files):
    bif_name = os.path.basename(bif_path)
    for filename in sorted(folder_files, key=str.lower):
        if os.path.splitext(filename)[1].lower() not in VIDEO_EXTS:
            continue
        stem = os.path.splitext(filename)[0]
        if bif_matches_video(bif_name, stem):
            return os.path.join(os.path.dirname(bif_path), filename)
    return ""


def _scan_quality_items(scan, lib_root):
    items = []
    seen = set()
    path = scan["path"]
    for base, dirs, files in os.walk(path, followlinks=False):
        _check_quality_cancelled(scan)
        dirs[:] = [d for d in dirs if not _skip_dir(base, d, lib_root)]
        for filename in sorted(files, key=str.lower):
            if os.path.splitext(filename)[1].lower() != ".bif":
                continue
            bif_path = os.path.realpath(os.path.join(base, filename))
            if bif_path in seen or os.path.islink(bif_path) or not os.path.isfile(bif_path):
                continue
            seen.add(bif_path)
            video_path = _find_matching_video_for_bif(bif_path, files)
            if not video_path:
                continue
            items.append(analyze_bif_quality(bif_path, video_path, lib_root))
            if len(items) % 10 == 0:
                _set_quality_progress(
                    scan,
                    min(95, 5 + len(items)),
                    f"Checked {len(items)} BIF files",
                    checked_bif_count=len(items),
                )
    items.sort(key=lambda item: item["relative_path"].lower())
    return items


def _quality_cancel_requested(scan):
    if not scan:
        return False
    with preview_lock:
        return bool(scan.get("cancel_requested"))


def _check_quality_cancelled(scan):
    if _quality_cancel_requested(scan):
        raise ScanCancelled()


def _set_quality_progress(scan, percent, label, **values):
    with preview_lock:
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


def public_quality_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    return {
        "id": scan.get("id", ""),
        "path": scan.get("path", ""),
        "status": scan.get("status", ""),
        "progress_percent": scan.get("progress_percent", 0),
        "progress_label": scan.get("progress_label", ""),
        "error": scan.get("error", ""),
        "created_at": scan.get("created_at"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "active": scan.get("status") in SCAN_ACTIVE_STATUSES,
        "cancel_requested": bool(scan.get("cancel_requested")),
        "checked_bif_count": counts.get("checked_bif_count", scan.get("checked_bif_count", 0)),
        "bad_count": counts.get("bad_count", 0),
        "warning_count": counts.get("warning_count", 0),
        "ok_count": counts.get("ok_count", 0),
        "repairable_count": counts.get("repairable_count", 0),
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": (counts.get("bad_count", 0) + counts.get("warning_count", 0)) >= LARGE_RESULT_COUNT,
        "default_repair_root": DEFAULT_REPAIR_ROOT,
        "recent_logs": list_recent_logs(),
    }


def _prune_quality_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(quality_scans):
        scan = quality_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if now - finished > SCAN_MAX_AGE_SECONDS:
            quality_scans.pop(scan_id, None)
    terminal = sorted(
        (
            (scan_id, scan)
            for scan_id, scan in quality_scans.items()
            if scan.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    for scan_id, _scan in terminal[SCAN_RETENTION_COUNT:]:
        quality_scans.pop(scan_id, None)


def _active_quality_scan_locked():
    active = [
        scan
        for scan in quality_scans.values()
        if scan.get("status") in SCAN_ACTIVE_STATUSES
    ]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


def _run_quality_scan(scan, lib_root):
    try:
        started = time.time()
        _set_quality_progress(
            scan,
            1,
            "Checking BIF quality",
            status="running",
            _started_ts=started,
            started_at=utc_iso(started),
        )
        items = _scan_quality_items(scan, lib_root)
        counts = _quality_counts(items)
        finished = time.time()
        _set_quality_progress(
            scan,
            100,
            f"{counts['bad_count']} bad, {counts['warning_count']} warnings",
            status="success",
            items=items,
            counts=counts,
            checked_bif_count=counts["checked_bif_count"],
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log("quality-scan", {"scan_id": scan["id"], "path": scan["path"], "counts": counts})
    except ScanCancelled:
        finished = time.time()
        _set_quality_progress(
            scan,
            100,
            "Quality scan cancelled",
            status="cancelled",
            error="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log(
            "quality-scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "status": "cancelled",
                "checked_bif_count": scan.get("checked_bif_count", 0),
            },
        )
    except Exception as exc:
        finished = time.time()
        _set_quality_progress(
            scan,
            100,
            "Quality scan failed",
            status="failed",
            error=str(exc),
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log(
            "quality-scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "status": "failed",
                "error": str(exc),
                "checked_bif_count": scan.get("checked_bif_count", 0),
            },
        )


def start_quality_scan(path, lib_root=LIB_ROOT, synchronous=False):
    real_path, err = _validate_scan_path(path, lib_root)
    if err:
        return None, err
    scan_id = _now_id()
    created = time.time()
    scan = {
        "id": scan_id,
        "path": real_path,
        "status": "queued",
        "progress_percent": 0,
        "progress_label": "Queued",
        "error": "",
        "_created_ts": created,
        "_started_ts": None,
        "_finished_ts": None,
        "created_at": utc_iso(created),
        "started_at": None,
        "finished_at": None,
        "cancel_requested": False,
        "checked_bif_count": 0,
        "items": [],
        "counts": {},
        "lib_root": os.path.realpath(lib_root),
    }
    with preview_lock:
        _prune_quality_scans_locked()
        active = _active_quality_scan_locked()
        if active:
            return active, None
        quality_scans[scan_id] = scan
    if synchronous:
        _run_quality_scan(scan, lib_root)
    else:
        threading.Thread(
            target=_run_quality_scan,
            args=(scan, lib_root),
            daemon=True,
            name=f"vid2gif-bif-quality-scan-{scan_id}",
        ).start()
    return scan, None


def cancel_quality_scan(scan_id=None):
    target_id = str(scan_id or "")
    now = time.time()
    with preview_lock:
        _prune_quality_scans_locked(now)
        scan = quality_scans.get(target_id) if target_id else _active_quality_scan_locked()
        if not scan:
            return None, "Scan not found"
        if scan.get("status") in SCAN_TERMINAL_STATUSES:
            return scan, None
        scan["cancel_requested"] = True
        if scan.get("status") == "queued":
            scan.update(
                {
                    "status": "cancelled",
                    "progress_percent": 100,
                    "progress_label": "Quality scan cancelled",
                    "_finished_ts": now,
                    "finished_at": utc_iso(now),
                }
            )
        else:
            scan.update({"status": "cancelling", "progress_label": "Cancelling quality scan"})
    return scan, None


def quality_status_payload(scan_id=None):
    with preview_lock:
        _prune_quality_scans_locked()
        if scan_id:
            scan = quality_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif quality_scans:
            scan = max(quality_scans.values(), key=lambda item: item.get("_created_ts") or 0)
        else:
            scan = None
    return {"scan": public_quality_scan(scan)}, None


def quality_items_payload(scan_id, status="problem", offset=0, limit=ITEM_PAGE_DEFAULT):
    offset, limit = _coerce_page(offset, limit)
    status = str(status or "problem").lower()
    if status not in {"problem", "bad", "warning", "ok", "all"}:
        status = "problem"
    with preview_lock:
        _prune_quality_scans_locked()
        scan = quality_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        items = list(scan.get("items") or [])
    if status == "problem":
        items = [item for item in items if item.get("status") in {"bad", "warning"}]
    elif status != "all":
        items = [item for item in items if item.get("status") == status]
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_quality_scan(scan),
        "status": status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(page),
        "has_previous": offset > 0,
        "has_next": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "large_result": total >= LARGE_RESULT_COUNT,
        "items": [_public_quality_item(item) for item in page],
    }, None


def _public_quality_item(item):
    public = {
        key: item.get(key)
        for key in (
            "id",
            "status",
            "repairable",
            "confidence",
            "reason",
            "reasons",
            "path",
            "relative_path",
            "name",
            "video_path",
            "video_relative_path",
            "video_name",
            "frame_count",
            "duration_seconds",
            "interval_seconds",
            "timestamp_multiplier_ms",
            "sample_summary",
            "size_bytes",
            "size_label",
            "modified_at",
        )
    }
    return public


def _default_repair_root(lib_root):
    configured = str(DEFAULT_REPAIR_ROOT or "").strip()
    lib_real = os.path.realpath(lib_root)
    if not configured:
        return os.path.join(lib_real, ".vid2gif-video-preview-repairs")
    configured_real = os.path.realpath(configured)
    library_real = os.path.realpath("/library")
    if path_is_under(configured_real, library_real):
        rel = os.path.relpath(configured_real, library_real)
        return os.path.realpath(os.path.join(lib_real, rel))
    return configured_real


def _validate_repair_root(move_root, lib_root):
    move_root = str(move_root or "").strip()
    if not move_root:
        move_root = _default_repair_root(lib_root)
    real = os.path.realpath(move_root)
    if os.path.islink(real) or not path_is_under(real, lib_root):
        return None, "Repair destination must be inside the mounted library root"
    return real, None


def _repair_destination(path, lib_root, move_root):
    lib_real = os.path.realpath(lib_root)
    move_real = os.path.realpath(move_root)
    rel = os.path.relpath(os.path.realpath(path), lib_real)
    return os.path.realpath(os.path.join(move_real, rel))


def build_quality_repair_plan(payload, lib_root=LIB_ROOT):
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    item_ids = payload.get("item_ids")
    selected_ids = {str(item_id) for item_id in item_ids if str(item_id)} if isinstance(item_ids, list) else None
    trigger_emby = _truthy(payload.get("trigger_emby"), default=True)
    with preview_lock:
        _prune_quality_scans_locked()
        scan = quality_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    lib_real = os.path.realpath(lib_root)
    move_root, move_err = _validate_repair_root(payload.get("move_root"), lib_real)
    if move_err:
        return None, move_err

    files = []
    manual_review = []
    for item in scan.get("items") or []:
        if selected_ids is not None and item.get("id") not in selected_ids:
            continue
        if not item.get("repairable"):
            if selected_ids is not None:
                manual_review.append(
                    {
                        "file_id": item.get("id", ""),
                        "path": item.get("path", ""),
                        "reason": "Item is not high-confidence repairable",
                    }
                )
            continue
        source = item.get("path", "")
        if not source or not path_is_under(source, lib_real):
            manual_review.append(
                {
                    "file_id": item.get("id", ""),
                    "path": source,
                    "reason": "Source is outside the library",
                }
            )
            continue
        destination = _repair_destination(source, lib_real, move_root)
        files.append(
            {
                "file_id": item.get("id", ""),
                "operation": "move",
                "source_path": source,
                "relative_path": _relative_path(source, lib_real),
                "destination_path": destination,
                "source_name": os.path.basename(source),
                "destination_name": os.path.basename(destination),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
                "reason": item.get("reason", ""),
                "confidence": item.get("confidence", 0),
                "identity": dict(item.get("identity") or {}),
            }
        )
    plan_id = _now_id()
    total_size = sum(item.get("size_bytes") or 0 for item in files)
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "action": "move",
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": lib_real,
        "move_root": move_root,
        "files": files,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "manual_review": manual_review,
        "trigger_emby": trigger_emby,
    }
    with preview_lock:
        quality_plans[plan_id] = plan
    return public_quality_plan(plan), None


def public_quality_plan(plan):
    if not plan:
        return None
    return {
        "id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": plan.get("action", ""),
        "status": plan.get("status", ""),
        "created_at": plan.get("created_at", ""),
        "move_root": plan.get("move_root", ""),
        "file_count": plan.get("file_count", 0),
        "total_size_bytes": plan.get("total_size_bytes", 0),
        "total_size_label": plan.get("total_size_label", ""),
        "manual_review": list(plan.get("manual_review") or []),
        "trigger_emby": bool(plan.get("trigger_emby")),
        "files": [
            {
                "file_id": item.get("file_id", ""),
                "operation": item.get("operation", ""),
                "source_path": item.get("source_path", ""),
                "relative_path": item.get("relative_path", ""),
                "destination_path": item.get("destination_path", ""),
                "source_name": item.get("source_name", ""),
                "destination_name": item.get("destination_name", ""),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
                "reason": item.get("reason", ""),
                "confidence": item.get("confidence", 0),
            }
            for item in plan.get("files") or []
        ],
    }


def _quality_refusal(file_id, path, reason):
    return {"file_id": file_id, "path": path, "reason": reason}


def _write_quality_repair_log(plan, result, records):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_id = f"{plan.get('id', _now_id())}-quality-repair.jsonl"
    path = os.path.join(LOG_DIR, log_id)
    header = {
        "type": "quality-repair-summary",
        "timestamp": utc_iso(),
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": plan.get("action", "move"),
        "applied_count": result.get("applied_count", 0),
        "missing_count": result.get("missing_count", 0),
        "refused_count": result.get("refused_count", 0),
        "total_applied_bytes": result.get("total_applied_bytes", 0),
        "move_root": plan.get("move_root", ""),
        "trigger_emby": bool(plan.get("trigger_emby")),
        "emby": result.get("emby") or {},
    }
    written = 0
    truncated = False
    with open(path, "w", encoding="utf-8") as f:
        for record in [header, *records]:
            line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
            size = len(line.encode("utf-8"))
            if written + size > LOG_MAX_BYTES:
                truncated = True
                break
            f.write(line)
            written += size
        if truncated:
            line = json.dumps(
                {
                    "type": "truncated",
                    "timestamp": utc_iso(),
                    "message": "Log reached maximum size; remaining records were omitted.",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
            f.write(line)
    index = _read_json(LOG_INDEX, {"logs": []})
    logs = [item for item in index.get("logs", []) if item.get("id") != log_id]
    entry = {
        "id": log_id,
        "path": path,
        "created_at": header["timestamp"],
        "type": "quality-repair",
        "plan_id": header["plan_id"],
        "scan_id": header["scan_id"],
        "action": header["action"],
        "applied_count": header["applied_count"],
        "missing_count": header["missing_count"],
        "refused_count": header["refused_count"],
        "size_bytes": os.path.getsize(path),
        "size_label": format_size(os.path.getsize(path)),
        "truncated": truncated,
    }
    logs.insert(0, entry)
    for old in logs[LOG_RETENTION_COUNT:]:
        try:
            os.remove(old.get("path", ""))
        except OSError:
            pass
    _write_json(LOG_INDEX, {"logs": logs[:LOG_RETENTION_COUNT]})
    public = dict(entry)
    public.pop("path", None)
    return public


def _refresh_emby_library(settings=None, opener=None):
    settings = settings or _settings()
    _data, result = emby_client.request_json(
        settings,
        "/Library/Refresh",
        method="POST",
        body=b"",
        opener=opener,
        accept="*/*",
    )
    if result.get("status") == "success":
        result = {**result, "message": "Emby library refresh started"}
    return result


def _run_quality_emby_sequence(settings=None, opener=None):
    settings = settings or _settings()
    refresh = _refresh_emby_library(settings=settings, opener=opener)
    extraction, _err = run_thumbnail_extraction(settings=settings, opener=opener)
    return {
        "refresh": refresh,
        "extraction": (extraction or {}).get("result") or {},
        "task": (extraction or {}).get("task"),
        "log": (extraction or {}).get("log"),
    }


def _public_quality_apply_result(result):
    result = result or {}
    log = result.get("log") or {}
    return {
        "plan_id": result.get("plan_id", ""),
        "scan_id": result.get("scan_id", ""),
        "action": result.get("action", ""),
        "applied_count": result.get("applied_count", 0),
        "missing_count": result.get("missing_count", 0),
        "refused_count": result.get("refused_count", 0),
        "total_applied_bytes": result.get("total_applied_bytes", 0),
        "total_applied_label": result.get("total_applied_label", "0 B"),
        "emby": result.get("emby") or {},
        "log": {key: value for key, value in log.items() if key != "path"},
    }


def public_quality_apply_run(run):
    if not run:
        return None
    result = run.get("result") or {}
    return {
        "id": run.get("id", ""),
        "plan_id": run.get("plan_id", ""),
        "scan_id": run.get("scan_id", ""),
        "action": run.get("action", ""),
        "status": run.get("status", ""),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "progress_percent": run.get("progress_percent", 0),
        "progress_label": run.get("progress_label", ""),
        "file_count": run.get("file_count", 0),
        "processed_count": run.get("processed_count", 0),
        "applied_count": run.get("applied_count", 0),
        "missing_count": run.get("missing_count", 0),
        "refused_count": run.get("refused_count", 0),
        "current_path": run.get("current_path", ""),
        "current_name": run.get("current_name", ""),
        "error": run.get("error", ""),
        "large_operation": bool(run.get("large_operation")),
        "result": _public_quality_apply_result(result) if result else None,
    }


def _set_quality_apply_progress(run, **values):
    if not run:
        return
    with preview_lock:
        run.update(values)


def _active_quality_apply_for_plan_locked(plan_id):
    for run in quality_apply_runs.values():
        if run.get("plan_id") == plan_id and run.get("status") in {"queued", "running"}:
            return run
    return None


def _prune_quality_apply_runs_locked(now=None):
    now = now or time.time()
    terminal = [
        (apply_id, run)
        for apply_id, run in quality_apply_runs.items()
        if run.get("status") in {"success", "failed"}
    ]
    for apply_id, run in terminal:
        finished = run.get("_finished_ts") or run.get("_created_ts") or now
        if now - finished > SCAN_MAX_AGE_SECONDS:
            quality_apply_runs.pop(apply_id, None)
    terminal = sorted(
        (
            (apply_id, run)
            for apply_id, run in quality_apply_runs.items()
            if run.get("status") in {"success", "failed"}
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    for apply_id, _run in terminal[SCAN_RETENTION_COUNT:]:
        quality_apply_runs.pop(apply_id, None)


def start_quality_repair_apply(plan_id):
    plan_id = str(plan_id or "")
    with preview_lock:
        _prune_quality_apply_runs_locked()
        plan = quality_plans.get(plan_id)
        if not plan:
            return None, "Plan not found"
        active = _active_quality_apply_for_plan_locked(plan_id)
        if active:
            return active, None
        if plan.get("status") == "applied":
            return None, "Plan already applied"
        if plan.get("status") == "applying":
            return None, "Plan is already applying"
        run_id = _now_id()
        created = time.time()
        run = {
            "id": run_id,
            "plan_id": plan_id,
            "scan_id": plan.get("scan_id", ""),
            "action": plan.get("action", "move"),
            "status": "queued",
            "created_at": utc_iso(created),
            "started_at": None,
            "finished_at": None,
            "_created_ts": created,
            "_started_ts": None,
            "_finished_ts": None,
            "progress_percent": 0,
            "progress_label": "Queued",
            "file_count": len(plan.get("files") or []),
            "processed_count": 0,
            "applied_count": 0,
            "missing_count": 0,
            "refused_count": 0,
            "current_path": "",
            "current_name": "",
            "error": "",
            "result": None,
            "large_operation": len(plan.get("files") or []) >= QUALITY_APPLY_LARGE_FILE_COUNT,
        }
        quality_apply_runs[run_id] = run
        plan["status"] = "applying"
    threading.Thread(
        target=_execute_quality_repair_apply,
        args=(run_id,),
        daemon=True,
        name=f"vid2gif-bif-quality-apply-{run_id}",
    ).start()
    return run, None


def _execute_quality_repair_apply(apply_id):
    with preview_lock:
        run = quality_apply_runs.get(apply_id)
    if not run:
        return
    result, err = apply_quality_repair_plan(run.get("plan_id"), apply_run=run)
    if err:
        finished = time.time()
        _set_quality_apply_progress(
            run,
            status="failed",
            error=err,
            progress_label="BIF repair failed",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )


def quality_apply_status(apply_id=None):
    with preview_lock:
        _prune_quality_apply_runs_locked()
        if apply_id:
            run = quality_apply_runs.get(str(apply_id or ""))
            if not run:
                return None, "Apply run not found"
        elif quality_apply_runs:
            run = max(quality_apply_runs.values(), key=lambda item: item.get("_created_ts") or 0)
        else:
            run = None
    return {"apply": public_quality_apply_run(run)}, None


def apply_quality_repair_plan(plan_id, apply_run=None, opener=None):
    with preview_lock:
        plan = quality_plans.get(str(plan_id or ""))
    if not plan:
        return None, "Plan not found"
    lib_root = plan.get("lib_root") or LIB_ROOT
    move_root = plan.get("move_root") or ""
    files = list(plan.get("files") or [])
    file_count = len(files)
    applied = []
    missing = []
    refused = []
    log_records = []
    total = 0
    if apply_run:
        started = time.time()
        _set_quality_apply_progress(
            apply_run,
            status="running",
            started_at=utc_iso(started),
            _started_ts=started,
            progress_percent=0,
            progress_label=f"Processing 0 of {file_count} BIF files",
            file_count=file_count,
        )

    def _finish_item(index, source):
        if not apply_run:
            return
        pct = int(100 * index / max(file_count, 1))
        _set_quality_apply_progress(
            apply_run,
            processed_count=index,
            applied_count=len(applied),
            missing_count=len(missing),
            refused_count=len(refused),
            progress_percent=pct,
            progress_label=f"Processed {index} of {file_count} BIF files",
            current_path=source if index < file_count else "",
            current_name=os.path.basename(source) if source and index < file_count else "",
        )

    for index, item in enumerate(files, start=1):
        source = item.get("source_path", "")
        dest = item.get("destination_path", "")
        file_id = item.get("file_id", "")
        if apply_run:
            _set_quality_apply_progress(
                apply_run,
                current_path=source,
                current_name=os.path.basename(source),
                progress_label=f"Processing {index} of {file_count} BIF files",
            )
        if not source or not path_is_under(source, lib_root):
            refusal = _quality_refusal(file_id, source, "Source is outside the library")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if os.path.islink(source):
            refusal = _quality_refusal(file_id, source, "Symlinks are not repaired")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if not os.path.exists(source):
            missing.append(file_id)
            log_records.append(
                {
                    "type": "file",
                    "result": "missing",
                    "file_id": file_id,
                    "old_path": source,
                    "old_name": os.path.basename(source),
                    "operation": "move",
                }
            )
            _finish_item(index, source)
            continue
        if not _identity_matches(source, item.get("identity")):
            refusal = _quality_refusal(file_id, source, "File changed after scan")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if not dest or not path_is_under(dest, move_root) or not path_is_under(dest, lib_root):
            refusal = _quality_refusal(file_id, source, "Destination is outside repair quarantine")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if os.path.exists(dest):
            refusal = _quality_refusal(file_id, source, "Destination already exists")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        try:
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            shutil.move(source, dest)
        except Exception as exc:
            refusal = _quality_refusal(file_id, source, str(exc))
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        applied_item = {
            "file_id": file_id,
            "operation": "move",
            "source_path": source,
            "destination_path": dest,
            "source_name": item.get("source_name") or os.path.basename(source),
            "destination_name": item.get("destination_name") or os.path.basename(dest),
            "size_bytes": item.get("size_bytes", 0),
            "size_label": item.get("size_label", ""),
        }
        applied.append(applied_item)
        log_records.append(
            {
                "type": "file",
                "timestamp": utc_iso(),
                "result": "applied",
                "file_id": file_id,
                "operation": "move",
                "old_path": source,
                "old_name": applied_item["source_name"],
                "new_path": dest,
                "new_name": applied_item["destination_name"],
                "size_bytes": item.get("size_bytes", 0),
                "reason": item.get("reason", ""),
                "confidence": item.get("confidence", 0),
                "identity": item.get("identity") or {},
            }
        )
        total += item.get("size_bytes") or 0
        _finish_item(index, source)

    emby = {}
    if plan.get("trigger_emby") and applied:
        if apply_run:
            _set_quality_apply_progress(apply_run, progress_label="Triggering Emby preview extraction")
        emby = _run_quality_emby_sequence(opener=opener)
        log_records.append({"type": "emby", "timestamp": utc_iso(), "result": emby})
    result = {
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": "move",
        "applied": applied,
        "missing": missing,
        "refused": refused,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "refused_count": len(refused),
        "total_applied_bytes": total,
        "total_applied_label": format_size(total),
        "emby": emby,
    }
    log_entry = _write_quality_repair_log(plan, result, log_records)
    result["log"] = {key: value for key, value in log_entry.items() if key != "path"}
    with preview_lock:
        plan["status"] = "applied"
        plan["applied_at"] = utc_iso()
        plan["last_result"] = result
    if apply_run:
        finished = time.time()
        _set_quality_apply_progress(
            apply_run,
            status="success",
            result=result,
            progress_percent=100,
            progress_label="BIF repair complete",
            processed_count=file_count,
            applied_count=len(applied),
            missing_count=len(missing),
            refused_count=len(refused),
            current_path="",
            current_name="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
    return result, None


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp_path, path)


def _write_log(kind, payload):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_id = f"{_now_id()}-{kind}.jsonl"
    path = os.path.join(LOG_DIR, log_id)
    summary = {
        "type": kind,
        "timestamp": utc_iso(),
        **(payload or {}),
    }
    line = json.dumps(summary, ensure_ascii=False, separators=(",", ":")) + "\n"
    encoded = line.encode("utf-8")
    truncated = len(encoded) > LOG_MAX_BYTES
    with open(path, "w", encoding="utf-8") as f:
        f.write(encoded[:LOG_MAX_BYTES].decode("utf-8", errors="ignore") if truncated else line)
    entry = {
        "id": log_id,
        "path": path,
        "created_at": summary["timestamp"],
        "type": kind,
        "size_bytes": os.path.getsize(path),
        "size_label": format_size(os.path.getsize(path)),
        "truncated": truncated,
    }
    index = _read_json(LOG_INDEX, {"logs": []})
    logs = [item for item in index.get("logs", []) if item.get("id") != log_id]
    logs.insert(0, entry)
    for old in logs[LOG_RETENTION_COUNT:]:
        try:
            os.remove(old.get("path", ""))
        except OSError:
            pass
    _write_json(LOG_INDEX, {"logs": logs[:LOG_RETENTION_COUNT]})
    public = dict(entry)
    public.pop("path", None)
    return public


def list_recent_logs():
    index = _read_json(LOG_INDEX, {"logs": []})
    logs = []
    for item in index.get("logs") or []:
        public = dict(item)
        public.pop("path", None)
        logs.append(public)
    return logs


def _settings():
    return poster_maintenance.load_settings()


def _public_task(task):
    task = task or {}
    return {
        "id": str(task.get("Id") or task.get("id") or ""),
        "name": str(task.get("Name") or task.get("name") or ""),
        "key": str(task.get("Key") or task.get("key") or ""),
        "state": str(task.get("State") or task.get("state") or ""),
        "description": str(task.get("Description") or task.get("description") or ""),
    }


def _task_search_text(task):
    public = _public_task(task)
    return " ".join(public.values()).lower()


def _is_thumbnail_task(task):
    text = _task_search_text(task)
    return (
        "thumbnail image extraction" in text
        or "thumbnail images extraction" in text
        or "video preview thumbnail" in text
        or ("thumbnail" in text and "extract" in text)
        or ("chapter" in text and "image" in text and "extract" in text)
    )


def discover_thumbnail_tasks(settings=None, opener=None):
    settings = settings or _settings()
    data, result = emby_client.request_json(
        settings,
        "/ScheduledTasks",
        opener=opener,
        accept="application/json",
    )
    configured = bool(settings.get("emby_url") and settings.get("emby_api_key"))
    if result.get("status") != "success":
        return {
            "configured": configured,
            "result": result,
            "tasks": [],
            "thumbnail_task": None,
        }
    tasks = data if isinstance(data, list) else []
    public_tasks = [_public_task(task) for task in tasks]
    matches = [task for task in tasks if _is_thumbnail_task(task)]
    return {
        "configured": configured,
        "result": {
            **result,
            "message": f"Found {len(matches)} thumbnail extraction task{'s' if len(matches) != 1 else ''}",
        },
        "tasks": public_tasks,
        "thumbnail_task": _public_task(matches[0]) if matches else None,
    }


def run_thumbnail_extraction(settings=None, opener=None):
    settings = settings or _settings()
    tasks = discover_thumbnail_tasks(settings=settings, opener=opener)
    task = tasks.get("thumbnail_task") or {}
    task_id = task.get("id") or ""
    if not task_id:
        result = emby_client.result(
            "failed",
            "Thumbnail extraction task was not found",
            api_key=settings.get("emby_api_key", ""),
        )
        log = _write_log("emby-task", {"result": result, "task": None})
        return {"result": result, "task": None, "log": log, "tasks": tasks}, None
    _data, result = emby_client.request_json(
        settings,
        f"/ScheduledTasks/Running/{task_id}",
        method="POST",
        body=b"",
        opener=opener,
        accept="*/*",
    )
    if result.get("status") == "success":
        result = {
            **result,
            "message": f"Thumbnail extraction task started: {task.get('name') or task_id}",
        }
    log = _write_log("emby-task", {"result": result, "task": task})
    return {"result": result, "task": task, "log": log, "tasks": tasks}, None
