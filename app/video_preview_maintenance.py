import datetime
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import threading
import time

from . import app_settings
from . import emby_catalog
from . import emby_playback
from . import emby_client
from . import emby_operations
from . import emby_notifications
from . import emby_sync
from . import impact_metrics
from . import maintenance_scan_store
from . import poster_maintenance
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .file_safety import (
    FileSafetyError,
    atomic_install_file,
    atomic_quarantine_file,
    identity_matches as safe_identity_matches,
    regular_file_identity,
    target_state,
)
from .maintenance import QUARANTINE_DIRNAME
from .progress import format_size, utc_iso
from .table_sort import sort_records
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
GENERATION_ROOT = os.path.join(STATE_ROOT, "video-preview-generation")
GENERATION_MANIFEST_PATH = os.path.join(GENERATION_ROOT, "manifest.json")
__test__ = False

preview_scans = {}
quality_scans = {}
_preview_cache_loaded = False
_quality_cache_loaded = False
quality_plans = {}
quality_apply_runs = {}
generation_plans = {}
generation_runs = {}
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
    settings = app_settings.load_settings()
    configured_profile = {
        "width": settings.get("video_preview_bif_width", 320),
        "interval_seconds": settings.get("video_preview_bif_interval_seconds", 10),
    }
    recommendation = scan.get("recommended_profile") or None
    public = {
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
        "configured_profile": configured_profile,
        "recommended_profile": recommendation,
        "profile_mismatch": bool(
            recommendation
            and (
                recommendation.get("width") != configured_profile["width"]
                or recommendation.get("interval_seconds") != configured_profile["interval_seconds"]
            )
        ),
        "emby_mapping": emby_catalog.public_summary(
            scan.get("emby_mapping"), app_settings.load_settings()
        ),
    }
    public.update(maintenance_scan_store.public_cache_metadata("video_previews_missing", scan))
    return public


def _ensure_preview_cache_loaded():
    global _preview_cache_loaded
    if _preview_cache_loaded:
        return
    restored = maintenance_scan_store.restore_scan("video_previews_missing")
    with preview_lock:
        if restored and restored.get("id") not in preview_scans:
            preview_scans[restored["id"]] = restored
        _preview_cache_loaded = True


def _prune_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(preview_scans):
        scan = preview_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if not scan.get("_persisted_latest") and now - finished > SCAN_MAX_AGE_SECONDS:
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
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[SCAN_RETENTION_COUNT:]:
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
        emby_mapping = emby_catalog.enrich_records(
            items,
            app_settings.load_settings(),
            lambda item: item.get("path"),
            before_page=lambda: _check_cancelled(scan),
        )
        for item in items:
            for bif in item.get("bifs") or []:
                bif["emby_parent_item_id"] = item.get("emby_item_id", "")
                bif["emby_parent_item_type"] = item.get("emby_item_type", "")
        recommendation = _recommended_bif_profile(items)
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
            recommended_profile=recommendation,
            emby_mapping=emby_mapping,
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        impact_metrics.record_scan(
            scan["id"],
            "video_previews",
            "missing",
            scan["path"],
            [
                {
                    "issue_id": f"video-preview:{item.get('id')}",
                    "finding_ids": ["missing"],
                    "label": item.get("name") or "Missing video preview",
                    "path": item.get("path") or scan["path"],
                }
                for item in items
                if item.get("status") == "missing"
            ],
            timestamp=utc_iso(finished),
        )
        _write_log(
            "scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "counts": counts,
            },
        )
        persisted = maintenance_scan_store.persist_success(
            "video_previews_missing", "video_previews", scan, lib_root
        )
        if persisted:
            with preview_lock:
                for candidate in preview_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
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
    _ensure_preview_cache_loaded()
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
        "lib_root": os.path.realpath(lib_root),
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
    _ensure_preview_cache_loaded()
    with preview_lock:
        _prune_scans_locked()
        if scan_id:
            scan = preview_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif preview_scans:
            active = _active_scan_locked()
            successful = [item for item in preview_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("_finished_ts") or 0) if successful else max(preview_scans.values(), key=lambda item: item.get("_created_ts") or 0))
        else:
            scan = None
    return {"scan": public_scan(scan)}, None


def items_payload(scan_id, status="missing", offset=0, limit=ITEM_PAGE_DEFAULT, sort="video", direction="asc"):
    _ensure_preview_cache_loaded()
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
    items, sort, direction = sort_records(
        items, sort, direction,
        {
            "status": lambda item: item.get("status"),
            "video": lambda item: item.get("relative_path") or item.get("name"),
            "size": lambda item: item.get("size_bytes"),
            "detail": lambda item: item.get("detail"),
            "bifs": lambda item: len(item.get("bifs") or []),
        },
        "video",
    )
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "status": status,
        "sort": sort,
        "direction": direction,
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
    return regular_file_identity(path)


def _identity_matches(path, identity):
    return safe_identity_matches(path, identity)


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
        "repairable_count": bad + warning,
    }


def _expected_bif_frame_count(duration_seconds, interval_seconds):
    try:
        duration = float(duration_seconds)
        interval = float(interval_seconds)
    except (TypeError, ValueError):
        return None
    if duration <= 0 or interval <= 0:
        return None
    return max(1, int(math.ceil(duration / interval)))


def analyze_bif_quality(bif_path, video_path, lib_root):
    parsed = parse_bif(bif_path)
    stat = _safe_stat(bif_path)
    identity = _stat_identity(bif_path) or {}
    interval_from_name = bif_interval_seconds(os.path.basename(bif_path), os.path.splitext(os.path.basename(video_path))[0])
    timestamp_multiplier_ms = parsed.get("timestamp_multiplier_ms") or 0
    interval_from_header = max(1, int(round(timestamp_multiplier_ms / 1000))) if timestamp_multiplier_ms else None
    interval_seconds = interval_from_name or interval_from_header
    duration = _probe_video_duration(video_path)
    frame_count = parsed.get("image_count") or 0
    expected_frame_count = _expected_bif_frame_count(duration, interval_seconds)
    frame_count_ratio = (
        round(frame_count / expected_frame_count, 3)
        if expected_frame_count
        else None
    )
    frame_count_detail = (
        f"{frame_count} / {expected_frame_count}"
        if expected_frame_count
        else str(frame_count)
    )
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
        if expected_frame_count and expected_frame_count >= 6:
            missing_frames = max(0, expected_frame_count - frame_count)
            severe_missing = max(3, int(math.ceil(expected_frame_count * 0.25)))
            warning_missing = max(4, int(math.ceil(expected_frame_count * 0.10)))
            if frame_count_ratio is not None and frame_count_ratio <= 0.60 and missing_frames >= severe_missing:
                reasons.append(
                    f"BIF has fewer frames than expected for this video ({frame_count} of {expected_frame_count})"
                )
                confidence = max(confidence, 90)
            elif frame_count_ratio is not None and frame_count_ratio <= 0.85 and missing_frames >= warning_missing:
                reasons.append(
                    f"BIF frame count is lower than expected for this video ({frame_count} of {expected_frame_count})"
                )
                confidence = max(confidence, 65)

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
        "repairable": status in {"bad", "warning"},
        "confidence": confidence,
        "reason": "; ".join(reasons),
        "reasons": reasons,
        "path": os.path.realpath(bif_path),
        "relative_path": _relative_path(bif_path, lib_root),
        "name": os.path.basename(bif_path),
        "video_path": os.path.realpath(video_path),
        "video_relative_path": _relative_path(video_path, lib_root),
        "video_name": os.path.basename(video_path),
        "frame_count": frame_count,
        "expected_frame_count": expected_frame_count,
        "frame_count_ratio": frame_count_ratio,
        "frame_count_detail": frame_count_detail,
        "duration_seconds": duration,
        "interval_seconds": interval_seconds,
        "timestamp_multiplier_ms": timestamp_multiplier_ms,
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
    public = {
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
        "emby_mapping": emby_catalog.public_summary(
            scan.get("emby_mapping"), app_settings.load_settings()
        ),
    }
    public.update(maintenance_scan_store.public_cache_metadata("video_previews_quality", scan))
    return public


def _ensure_quality_cache_loaded():
    global _quality_cache_loaded
    if _quality_cache_loaded:
        return
    restored = maintenance_scan_store.restore_scan("video_previews_quality")
    with preview_lock:
        if restored and restored.get("id") not in quality_scans:
            quality_scans[restored["id"]] = restored
        _quality_cache_loaded = True


def _prune_quality_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(quality_scans):
        scan = quality_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if not scan.get("_persisted_latest") and now - finished > SCAN_MAX_AGE_SECONDS:
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
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[SCAN_RETENTION_COUNT:]:
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
        emby_mapping = emby_catalog.enrich_records(
            items,
            app_settings.load_settings(),
            lambda item: item.get("video_path"),
            before_page=lambda: _check_quality_cancelled(scan),
        )
        finished = time.time()
        _set_quality_progress(
            scan,
            100,
            f"{counts['bad_count']} bad, {counts['warning_count']} warnings",
            status="success",
            items=items,
            counts=counts,
            checked_bif_count=counts["checked_bif_count"],
            emby_mapping=emby_mapping,
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        impact_metrics.record_scan(
            scan["id"],
            "video_previews",
            "quality",
            scan["path"],
            [
                {
                    "issue_id": f"video-preview:{_path_id(item.get('video_path'), lib_root)}",
                    "finding_ids": [item.get("id")],
                    "label": item.get("video_name") or "Video preview quality",
                    "path": item.get("video_path") or item.get("path") or scan["path"],
                }
                for item in items
                if item.get("status") in {"bad", "warning"}
            ],
            timestamp=utc_iso(finished),
        )
        _write_log("quality-scan", {"scan_id": scan["id"], "path": scan["path"], "counts": counts})
        persisted = maintenance_scan_store.persist_success(
            "video_previews_quality", "video_previews", scan, lib_root
        )
        if persisted:
            with preview_lock:
                for candidate in quality_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
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
    _ensure_quality_cache_loaded()
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
    _ensure_quality_cache_loaded()
    with preview_lock:
        _prune_quality_scans_locked()
        if scan_id:
            scan = quality_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif quality_scans:
            active = _active_quality_scan_locked()
            successful = [item for item in quality_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("_finished_ts") or 0) if successful else max(quality_scans.values(), key=lambda item: item.get("_created_ts") or 0))
        else:
            scan = None
    return {"scan": public_quality_scan(scan)}, None


def quality_items_payload(scan_id, status="problem", offset=0, limit=ITEM_PAGE_DEFAULT, sort="bif", direction="asc"):
    _ensure_quality_cache_loaded()
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
    items, sort, direction = sort_records(
        items, sort, direction,
        {
            "status": lambda item: item.get("status"),
            "bif": lambda item: item.get("relative_path") or item.get("name"),
            "video": lambda item: item.get("video_relative_path") or item.get("video_name"),
            "confidence": lambda item: item.get("confidence"),
            "frames": lambda item: item.get("frame_count"),
            "interval": lambda item: item.get("interval_seconds"),
            "reason": lambda item: item.get("reason"),
            "size": lambda item: item.get("size_bytes"),
        },
        "bif",
    )
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_quality_scan(scan),
        "status": status,
        "sort": sort,
        "direction": direction,
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
            "expected_frame_count",
            "frame_count_ratio",
            "frame_count_detail",
            "duration_seconds",
            "interval_seconds",
            "timestamp_multiplier_ms",
            "sample_summary",
            "size_bytes",
            "size_label",
            "modified_at",
            "emby_item_id",
            "emby_item_type",
            "emby_item_name",
            "emby_match_status",
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
    _ensure_quality_cache_loaded()
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    allowed, freshness_error = maintenance_scan_store.action_allowed(
        "video_previews_quality", scan_id, lib_root
    )
    if not allowed:
        return None, freshness_error
    item_ids = payload.get("item_ids")
    selected_ids = {str(item_id) for item_id in item_ids if str(item_id)} if isinstance(item_ids, list) else None
    selected_statuses = {
        str(status).lower()
        for status in (payload.get("statuses") or ["bad", "warning"])
        if str(status).lower() in {"bad", "warning"}
    }
    excluded_ids = {
        str(item_id) for item_id in (payload.get("excluded_item_ids") or []) if str(item_id)
    }
    included_ids = {
        str(item_id) for item_id in (payload.get("included_item_ids") or []) if str(item_id)
    }
    operation = str(payload.get("operation") or "quarantine").strip().lower()
    if operation not in {"quarantine", "delete"}:
        return None, "Choose quarantine or delete"
    with preview_lock:
        _prune_quality_scans_locked()
        scan = quality_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    lib_real = os.path.realpath(lib_root)
    move_root, move_err = _validate_repair_root(payload.get("move_root"), lib_real)
    if operation == "quarantine" and move_err:
        return None, move_err

    files = []
    manual_review = []
    for item in scan.get("items") or []:
        if selected_ids is not None and item.get("id") not in selected_ids:
            continue
        if selected_ids is None and not (
            (item.get("status") in selected_statuses and item.get("id") not in excluded_ids)
            or item.get("id") in included_ids
        ):
            continue
        if item.get("status") not in {"bad", "warning"}:
            if selected_ids is not None:
                manual_review.append(
                    {
                        "file_id": item.get("id", ""),
                        "path": item.get("path", ""),
                        "reason": "Only bad and warning BIFs can be cleaned up",
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
        destination = _repair_destination(source, lib_real, move_root) if operation == "quarantine" else ""
        files.append(
            {
                "file_id": item.get("id", ""),
                "impact_issue_id": f"video-preview:{_path_id(item.get('video_path'), lib_real)}",
                "video_path": item.get("video_path", ""),
                "video_name": item.get("video_name", ""),
                "operation": operation,
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
                "emby_item_id": item.get("emby_item_id", ""),
                "emby_item_type": item.get("emby_item_type", ""),
            }
        )
    plan_id = _now_id()
    total_size = sum(item.get("size_bytes") or 0 for item in files)
    playback_targets = [
        {
            "id": item["file_id"],
            "group_id": item.get("emby_item_id") or item.get("video_path") or item["file_id"],
            "local_path": item.get("video_path", ""),
            "emby_item_id": item.get("emby_item_id", ""),
        }
        for item in files
    ]
    playback = emby_playback.check_targets(playback_targets, force=True)
    for item in files:
        item["emby_playback_status"] = emby_playback.target_status(playback, item["file_id"])
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "action": operation,
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": lib_real,
        "move_root": move_root if operation == "quarantine" else "",
        "files": files,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "manual_review": manual_review,
        "playback_targets": playback_targets,
        "emby_playback": playback,
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
        "emby_playback": emby_playback.public_result(plan.get("emby_playback")),
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
                "emby_item_id": item.get("emby_item_id", ""),
                "emby_item_type": item.get("emby_item_type", ""),
                "emby_playback_status": item.get("emby_playback_status", "not_checked"),
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
        "action": plan.get("action", "quarantine"),
        "applied_count": result.get("applied_count", 0),
        "missing_count": result.get("missing_count", 0),
        "refused_count": result.get("refused_count", 0),
        "deferred_count": result.get("deferred_count", 0),
        "deferred_bytes": result.get("deferred_bytes", 0),
        "total_applied_bytes": result.get("total_applied_bytes", 0),
        "move_root": plan.get("move_root", ""),
        "emby_sync": result.get("emby_sync") or result.get("emby") or {},
        "emby": result.get("emby_sync") or result.get("emby") or {},
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
        "deferred_count": header["deferred_count"],
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
        "emby_playback": emby_playback.public_result(result.get("emby_playback")),
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
        "deferred_count": run.get("deferred_count", 0),
        "current_path": run.get("current_path", ""),
        "current_name": run.get("current_name", ""),
        "error": run.get("error", ""),
        "large_operation": bool(run.get("large_operation")),
        "emby_playback": emby_playback.public_result(result.get("emby_playback")) if result else None,
        "emby_notification": emby_notifications.public_result(run.get("emby_notification") or result.get("emby_notification")) if (run.get("emby_notification") or result) else None,
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
            "action": plan.get("action", "quarantine"),
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
        notification = emby_notifications.notify_maintenance(
            "BIF cleanup",
            run["id"],
            status="failed",
            attempted_count=run.get("file_count", 0),
            succeeded_count=run.get("applied_count", 0),
            failed_count=1,
            refused_count=run.get("refused_count", 0),
            deferred_count=run.get("deferred_count", 0),
        )
        _set_quality_apply_progress(
            run,
            status="failed",
            error=err,
            progress_label="BIF repair failed",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
            emby_notification=notification,
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
    action = plan.get("action") or "quarantine"
    files = list(plan.get("files") or [])
    file_count = len(files)
    applied = []
    missing = []
    refused = []
    deferred = []
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
            deferred_count=0,
        )

    playback_targets = list(plan.get("playback_targets") or [])
    playback = emby_playback.check_targets(playback_targets, opener=opener, force=True)
    playback_checked = time.monotonic()
    group_decisions = {}

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
            deferred_count=len(deferred),
            progress_percent=pct,
            progress_label=f"Processed {index} of {file_count} BIF files",
            current_path=source if index < file_count else "",
            current_name=os.path.basename(source) if source and index < file_count else "",
        )

    for index, item in enumerate(files, start=1):
        source = item.get("source_path", "")
        dest = item.get("destination_path", "")
        file_id = item.get("file_id", "")
        group_id = item.get("emby_item_id") or item.get("video_path") or file_id
        if group_id not in group_decisions:
            if time.monotonic() - playback_checked >= emby_playback.RUN_REFRESH_SECONDS:
                playback = emby_playback.check_targets(
                    playback_targets, opener=opener, force=True
                )
                playback_checked = time.monotonic()
            group_decisions[group_id] = emby_playback.group_status(
                playback, playback_targets, group_id
            )
        playback_status = group_decisions[group_id]
        if apply_run:
            _set_quality_apply_progress(
                apply_run,
                current_path=source,
                current_name=os.path.basename(source),
                progress_label=f"Processing {index} of {file_count} BIF files",
            )
        if playback_status in {"active", "unverified"}:
            reason = (
                "Parent video is actively playing in Emby"
                if playback_status == "active"
                else "Parent video playback could not be verified"
            )
            value = {
                "file_id": file_id,
                "path": source,
                "reason": reason,
                "playback_status": playback_status,
                "size_bytes": item.get("size_bytes", 0),
            }
            deferred.append(value)
            log_records.append({"type": "file", "result": "deferred", **value})
            _finish_item(index, source)
            continue
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
                    "operation": action,
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
        if action == "quarantine" and (
            not dest or not path_is_under(dest, move_root) or not path_is_under(dest, lib_root)
        ):
            refusal = _quality_refusal(file_id, source, "Destination is outside repair quarantine")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if action == "quarantine" and os.path.exists(dest):
            refusal = _quality_refusal(file_id, source, "Destination already exists")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        try:
            if action == "delete":
                os.remove(source)
            else:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                atomic_quarantine_file(
                    source,
                    dest,
                    root=lib_root,
                    expected_source=item.get("identity"),
                )
        except Exception as exc:
            refusal = _quality_refusal(file_id, source, str(exc))
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        applied_item = {
            "file_id": file_id,
            "operation": action,
            "source_path": source,
            "destination_path": dest,
            "source_name": item.get("source_name") or os.path.basename(source),
            "destination_name": item.get("destination_name") or os.path.basename(dest),
            "size_bytes": item.get("size_bytes", 0),
            "size_label": item.get("size_label", ""),
            "emby_item_id": item.get("emby_item_id", ""),
            "emby_item_type": item.get("emby_item_type", ""),
        }
        applied.append(applied_item)
        log_records.append(
            {
                "type": "file",
                "timestamp": utc_iso(),
                "result": "applied",
                "file_id": file_id,
                "operation": action,
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

    if applied and apply_run:
        _set_quality_apply_progress(apply_run, progress_label="Synchronizing BIF cleanup with Emby")
    emby = emby_sync.sync_changes(
        [
            {
                "local_path": item.get("source_path"),
                "update_type": "Deleted",
                "emby_item_id": item.get("emby_item_id", ""),
                "refresh_scope": "thumbnail",
            }
            for item in applied
        ],
        workflow="video_previews_quality",
        run_id=(apply_run or {}).get("id") or plan.get("id"),
        opener=opener,
    ) if applied else None
    result = {
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": action,
        "applied": applied,
        "missing": missing,
        "refused": refused,
        "deferred": deferred,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "refused_count": len(refused),
        "deferred_count": len(deferred),
        "deferred_bytes": sum(int(item.get("size_bytes") or 0) for item in deferred),
        "total_applied_bytes": total,
        "total_applied_label": format_size(total),
        "emby_sync": emby,
        "emby": emby,
        "emby_playback": playback,
    }
    log_entry = _write_quality_repair_log(plan, result, log_records)
    result["log"] = {key: value for key, value in log_entry.items() if key != "path"}
    applied_ids = {item.get("file_id") for item in applied}
    affected_videos = {}
    for item in plan.get("files") or []:
        if item.get("file_id") in applied_ids and item.get("video_path"):
            affected_videos[item.get("impact_issue_id")] = item
    for issue_id, item in affected_videos.items():
        video_path = item.get("video_path")
        usable = False
        for bif_path in _matching_bifs_for_video(video_path):
            try:
                if analyze_bif_quality(bif_path, video_path, plan.get("lib_root") or LIB_ROOT).get("status") == "ok":
                    usable = True
                    break
            except Exception:
                continue
        impact_metrics.record_scan(
            f"{plan.get('id')}:{issue_id}:post-cleanup",
            "video_previews",
            "missing",
            video_path,
            [] if usable else [
                {
                    "issue_id": issue_id,
                    "finding_ids": ["missing"],
                    "label": item.get("video_name") or "Missing video preview",
                    "path": video_path,
                }
            ],
            timestamp=utc_iso(),
        )
    resolutions = [
        {
            "issue_id": item.get("impact_issue_id"),
            "stream": "quality",
            "finding_ids": [item.get("file_id")],
            "ensure_issue": True,
            "label": item.get("source_name") or "Video preview quality",
            "path": item.get("source_path") or plan.get("lib_root"),
        }
        for item in plan.get("files") or []
        if item.get("file_id") in applied_ids
    ]
    operation = plan.get("action")
    impact_metrics.record_maintenance_action(
        plan.get("id"),
        "video_previews",
        resolutions=resolutions,
        operations={
            "quarantined_files": len(applied) if operation == "quarantine" else 0,
            "quarantined_bytes": total if operation == "quarantine" else 0,
            "deleted_files": len(applied) if operation == "delete" else 0,
            "deleted_bytes": total if operation == "delete" else 0,
        },
        timestamp=utc_iso(),
        label="Video preview cleanup",
    )
    result["emby_notification"] = emby_notifications.notify_maintenance(
        "BIF cleanup",
        (apply_run or {}).get("id") or plan.get("id"),
        status="success",
        attempted_count=file_count,
        succeeded_count=len(applied),
        refused_count=len(refused),
        deferred_count=len(deferred),
        unresolved_count=len(missing),
        reclaimed_bytes=total,
        emby_sync=emby,
        opener=opener,
    )
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
            deferred_count=len(deferred),
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


def recent_log_payload(log_id):
    log_id = str(log_id or "")
    entry = next((item for item in _read_json(LOG_INDEX, {"logs": []}).get("logs") or [] if item.get("id") == log_id), None)
    if not entry:
        return None, "Log not found"
    path = os.path.realpath(entry.get("path") or "")
    if not path_is_under(path, LOG_DIR) or not os.path.isfile(path):
        return None, "Log not found"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            content = handle.read(LOG_MAX_BYTES)
    except OSError:
        return None, "Log not found"
    return {"log": {**{key: value for key, value in entry.items() if key != "path"}, "content": content}}, None


def _jpeg_dimensions(data):
    data = bytes(data or b"")
    if len(data) < 4 or data[:2] != b"\xff\xd8":
        return None, None
    position = 2
    sof_markers = {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
    while position + 4 <= len(data):
        if data[position] != 0xFF:
            position += 1
            continue
        while position < len(data) and data[position] == 0xFF:
            position += 1
        if position >= len(data):
            break
        marker = data[position]
        position += 1
        if marker in {0xD8, 0xD9}:
            continue
        if position + 2 > len(data):
            break
        length = int.from_bytes(data[position : position + 2], "big")
        if length < 2 or position + length > len(data):
            break
        if marker in sof_markers and length >= 7:
            height = int.from_bytes(data[position + 3 : position + 5], "big")
            width = int.from_bytes(data[position + 5 : position + 7], "big")
            return width or None, height or None
        position += length
    return None, None


def _bif_width_from_name(name):
    stem = _bif_stem(name)
    match = re.search(r"(?:^|[-_. ])(\d{2,5})[-_.](\d{1,5})$", stem)
    if not match:
        return None
    try:
        return int(match.group(1))
    except (TypeError, ValueError):
        return None


def _generation_manifest():
    data = _read_json(GENERATION_MANIFEST_PATH, {"schema_version": 1, "records": {}})
    if data.get("schema_version") != 1:
        return {"schema_version": 1, "records": {}}
    data.setdefault("records", {})
    return data


def _manifest_generated_identity(path, manifest=None):
    manifest = manifest or _generation_manifest()
    key = os.path.normcase(os.path.realpath(path))
    record = manifest.get("records", {}).get(key) or {}
    return record.get("identity") or None


def _record_generated_bif(path, width, interval_seconds):
    manifest = _generation_manifest()
    key = os.path.normcase(os.path.realpath(path))
    manifest.setdefault("records", {})[key] = {
        "path": os.path.realpath(path),
        "identity": _stat_identity(path) or {},
        "width": int(width),
        "interval_seconds": int(interval_seconds),
        "generated_at": utc_iso(),
    }
    _write_json(GENERATION_MANIFEST_PATH, manifest)


def _recommended_bif_profile(items):
    manifest = _generation_manifest()
    candidates = []
    for item in items or []:
        for bif in item.get("bifs") or []:
            path = bif.get("path") or ""
            identity = _stat_identity(path)
            if not identity or identity == _manifest_generated_identity(path, manifest):
                continue
            parsed = parse_bif(path, sample_limit=1)
            if not parsed.get("valid") or not parsed.get("samples"):
                continue
            interval = bif.get("interval_seconds") or max(
                1, int(round((parsed.get("timestamp_multiplier_ms") or 0) / 1000))
            )
            width = _bif_width_from_name(bif.get("name"))
            if not width:
                width, _height = _jpeg_dimensions(parsed["samples"][0].get("bytes"))
            stat = _safe_stat(path)
            if width and interval and stat:
                candidates.append((stat.st_mtime_ns, path, int(width), int(interval)))
    if not candidates:
        return None
    _mtime, path, width, interval = max(candidates, key=lambda value: value[0])
    return {
        "width": width,
        "interval_seconds": interval,
        "source_path": _relative_path(path, LIB_ROOT),
        "source_name": os.path.basename(path),
    }


def save_generation_settings(payload):
    if not isinstance(payload, dict):
        return None, "Invalid settings"
    try:
        width = int(payload.get("width"))
        interval = int(payload.get("interval_seconds"))
    except (TypeError, ValueError):
        return None, "BIF width and interval must be whole numbers"
    if not 64 <= width <= 1920:
        return None, "BIF width must be between 64 and 1920"
    if not 1 <= interval <= 3600:
        return None, "BIF interval must be between 1 and 3600 seconds"
    _settings, err = app_settings.update_settings(
        {
            "video_preview_bif_width": width,
            "video_preview_bif_interval_seconds": interval,
        }
    )
    if err:
        return None, "BIF settings could not be saved"
    return {"width": width, "interval_seconds": interval}, None


def build_generation_plan(payload, lib_root=LIB_ROOT):
    _ensure_preview_cache_loaded()
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    allowed, freshness_error = maintenance_scan_store.action_allowed(
        "video_previews_missing", scan_id, lib_root
    )
    if not allowed:
        return None, freshness_error
    raw_ids = payload.get("item_ids")
    if not isinstance(raw_ids, list) or not raw_ids:
        return None, "Select at least one missing video"
    item_ids = []
    for value in raw_ids:
        item_id = str(value or "")
        if item_id and item_id not in item_ids:
            item_ids.append(item_id)
    with preview_lock:
        scan = preview_scans.get(scan_id)
    if not scan or scan.get("status") != "success":
        return None, "Missing-BIF scan is not complete"
    settings = app_settings.load_settings()
    width = int(settings.get("video_preview_bif_width") or 320)
    interval = int(settings.get("video_preview_bif_interval_seconds") or 10)
    recommendation = scan.get("recommended_profile") or None
    mismatch = bool(
        recommendation
        and (recommendation.get("width") != width or recommendation.get("interval_seconds") != interval)
    )
    if mismatch and not _truthy(payload.get("confirm_profile_mismatch"), default=False):
        return None, "BIF generation settings differ from the latest observed Emby BIF"
    items_by_id = {item.get("id"): item for item in scan.get("items") or []}
    root = os.path.realpath(lib_root)
    files = []
    for item_id in item_ids:
        item = items_by_id.get(item_id)
        if not item or item.get("status") != "missing":
            return None, "Generation accepts only videos still marked missing"
        video_path = item.get("path") or ""
        if not path_is_under(video_path, root) or os.path.islink(video_path):
            return None, "Video path is unsafe"
        stem = os.path.splitext(os.path.basename(video_path))[0]
        output_path = os.path.join(os.path.dirname(video_path), f"{stem}-{width}-{interval}.bif")
        try:
            output_state = target_state(output_path, root=root)
        except FileSafetyError as exc:
            return None, f"BIF destination is unsafe: {exc}"
        files.append({
            "item_id": item_id,
            "video_path": video_path,
            "video_relative_path": _relative_path(video_path, root),
            "video_identity": _stat_identity(video_path) or {},
            "output_path": os.path.realpath(output_path),
            "output_state": output_state,
            "output_relative_path": _relative_path(output_path, root),
            "emby_item_id": item.get("emby_item_id", ""),
            "emby_item_type": item.get("emby_item_type", ""),
        })
    plan = {
        "id": _now_id(),
        "scan_id": scan_id,
        "scan_path": scan.get("path") or root,
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": root,
        "width": width,
        "interval_seconds": interval,
        "profile_mismatch": mismatch,
        "recommended_profile": recommendation,
        "files": files,
        "file_count": len(files),
    }
    with preview_lock:
        generation_plans[plan["id"]] = plan
    return plan, None


def _matching_bifs_for_video(video_path):
    folder = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [name for name in names if bif_matches_video(name, stem) and os.path.isfile(os.path.join(folder, name))]


def _write_bif_from_jpegs(jpeg_paths, output_path, interval_seconds):
    if not jpeg_paths:
        raise ValueError("FFmpeg did not generate any preview frames")
    sizes = []
    for path in jpeg_paths:
        with open(path, "rb") as handle:
            data = handle.read()
        if not data.startswith(b"\xff\xd8") or not data.rstrip().endswith(b"\xff\xd9"):
            raise ValueError(f"Generated frame is not a valid JPEG: {os.path.basename(path)}")
        sizes.append(len(data))
    offset = 64 + (len(jpeg_paths) + 1) * 8
    header = bytearray(64)
    header[:8] = BIF_MAGIC
    struct.pack_into("<III", header, 8, 0, len(jpeg_paths), int(interval_seconds) * 1000)
    with open(output_path, "wb") as output:
        output.write(header)
        current = offset
        for index, size in enumerate(sizes):
            output.write(struct.pack("<II", index, current))
            current += size
        output.write(struct.pack("<II", 0xFFFFFFFF, current))
        for path in jpeg_paths:
            with open(path, "rb") as frame:
                shutil.copyfileobj(frame, output, length=1024 * 1024)


def _run_frame_extraction(video_path, output_pattern, width, interval_seconds, run):
    command = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-i", video_path,
        "-vf", (
            f"select='eq(n,0)+gte(t-prev_selected_t,{int(interval_seconds)})',"
            f"scale={int(width)}:-2:flags=lanczos"
        ),
        "-fps_mode", "vfr", "-pix_fmt", "yuvj420p", "-q:v", "2", output_pattern,
    ]
    process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    while process.poll() is None:
        if run.get("cancel_requested"):
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
            raise ScanCancelled()
        time.sleep(0.1)
    stderr = (process.stderr.read() if process.stderr else b"").decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        raise RuntimeError(stderr or "FFmpeg frame extraction failed")


def _install_generated_bif(
    work_bif,
    target,
    video_path,
    width,
    interval_seconds,
    *,
    lib_root=LIB_ROOT,
    expected_target=None,
):
    if _matching_bifs_for_video(video_path):
        raise FileExistsError("A matching BIF appeared after the generation plan was created")
    parsed = parse_bif(work_bif)
    if not parsed.get("valid") or parsed.get("timestamp_multiplier_ms") != int(interval_seconds) * 1000:
        raise ValueError("Generated BIF failed structural validation")
    if not parsed.get("image_count"):
        raise ValueError("Generated BIF contains no frames")
    duration = _probe_video_duration(video_path)
    expected_count = _expected_bif_frame_count(duration, interval_seconds)
    if expected_count is not None and abs(parsed.get("image_count", 0) - expected_count) > 1:
        raise ValueError(
            f"Generated BIF frame count is unexpected ({parsed.get('image_count', 0)} / {expected_count})"
        )
    staged_identity = regular_file_identity(work_bif)
    atomic_install_file(
        work_bif,
        target,
        root=lib_root,
        expected_source=staged_identity,
        expected_target=expected_target,
    )
    _record_generated_bif(target, width, interval_seconds)
    return parsed


def public_generation_run(run):
    if not run:
        return None
    return {key: value for key, value in run.items() if not key.startswith("_")}


def _execute_generation(run, plan):
    run.update({"status": "running", "started_at": utc_iso(), "progress_label": "Generating BIF previews"})
    results = []
    generated = 0
    refused = 0
    work_root = os.path.join(GENERATION_ROOT, "work", run["id"])
    try:
        os.makedirs(work_root, exist_ok=False)
        for index, item in enumerate(plan.get("files") or [], 1):
            if run.get("cancel_requested"):
                raise ScanCancelled()
            video = item["video_path"]
            result = {"item_id": item["item_id"], "video": item["video_relative_path"], "output": item["output_relative_path"]}
            try:
                if not _identity_matches(video, item.get("video_identity")):
                    raise RuntimeError("Video changed after the missing-BIF scan")
                item_work = os.path.join(work_root, item["item_id"])
                os.makedirs(item_work)
                pattern = os.path.join(item_work, "%08d.jpg")
                _run_frame_extraction(video, pattern, plan["width"], plan["interval_seconds"], run)
                frames = sorted(
                    (os.path.join(item_work, name) for name in os.listdir(item_work) if name.lower().endswith(".jpg")),
                    key=str.lower,
                )
                work_bif = os.path.join(item_work, "preview.bif")
                _write_bif_from_jpegs(frames, work_bif, plan["interval_seconds"])
                if not _identity_matches(video, item.get("video_identity")):
                    raise RuntimeError("Video changed during BIF generation")
                parsed = _install_generated_bif(
                    work_bif,
                    item["output_path"],
                    video,
                    plan["width"],
                    plan["interval_seconds"],
                    lib_root=plan["lib_root"],
                    expected_target=item.get("output_state"),
                )
                result.update({
                    "status": "generated",
                    "frame_count": parsed.get("image_count", 0),
                    "output_size_bytes": os.path.getsize(item["output_path"]),
                })
                generated += 1
            except Exception as exc:
                result.update({"status": "refused", "reason": str(exc)})
                refused += 1
            results.append(result)
            run.update({
                "processed_count": index,
                "generated_count": generated,
                "refused_count": refused,
                "progress_percent": int(100 * index / max(1, plan["file_count"])),
                "progress_label": f"Processed {index} of {plan['file_count']} videos",
            })
        generated_ids = {item.get("item_id") for item in results if item.get("status") == "generated"}
        if generated:
            run.update(progress_label="Synchronizing generated BIF files with Emby")
        emby = emby_sync.sync_changes(
            [
                {
                    "local_path": item.get("output_path"),
                    "update_type": "Created",
                    "emby_item_id": item.get("emby_item_id", ""),
                    "refresh_scope": "thumbnail",
                }
                for item in plan.get("files") or []
                if item.get("item_id") in generated_ids
            ],
            workflow="video_previews_generation",
            run_id=run["id"],
        ) if generated else None
        run.update({
            "status": "success",
            "finished_at": utc_iso(),
            "progress_percent": 100,
            "progress_label": "BIF generation complete",
            "emby_sync": emby,
            "result": {"items": results, "generated_count": generated, "refused_count": refused, "emby_sync": emby, "emby": emby, "scan_path": plan["scan_path"]},
        })
        notification = emby_notifications.notify_maintenance(
            "BIF generation",
            run["id"],
            status="success",
            attempted_count=plan.get("file_count", 0),
            succeeded_count=generated,
            refused_count=refused,
            emby_sync=emby,
        )
        run["emby_notification"] = notification
        run["result"]["emby_notification"] = notification
        _write_log("bif-generation", {"plan_id": plan["id"], "generated_count": generated, "refused_count": refused, "items": results})
        impact_metrics.record_maintenance_action(
            plan.get("id"),
            "video_previews",
            resolutions=[
                {
                    "issue_id": f"video-preview:{item.get('item_id')}",
                    "stream": "missing",
                    "finding_ids": ["missing"],
                    "ensure_issue": True,
                    "label": os.path.basename(item.get("video_path") or "Video preview"),
                    "path": item.get("video_path") or plan.get("scan_path"),
                }
                for item in plan.get("files") or []
                if item.get("item_id") in generated_ids
            ],
            operations={
                "other_files": generated,
                "other_bytes": sum(int(item.get("output_size_bytes") or 0) for item in results),
            },
            timestamp=utc_iso(),
            label="Video preview generation",
        )
    except ScanCancelled:
        run.update({"status": "cancelled", "finished_at": utc_iso(), "progress_label": "BIF generation cancelled", "result": {"items": results}})
    except Exception as exc:
        run.update({"status": "failed", "finished_at": utc_iso(), "progress_label": "BIF generation failed", "error": str(exc), "result": {"items": results}})
        notification = emby_notifications.notify_maintenance(
            "BIF generation",
            run["id"],
            status="failed",
            attempted_count=plan.get("file_count", 0),
            succeeded_count=generated,
            failed_count=1,
            refused_count=refused,
        )
        run["emby_notification"] = notification
        run["result"]["emby_notification"] = notification
    finally:
        shutil.rmtree(work_root, ignore_errors=True)


def start_generation(plan_id, synchronous=False):
    with preview_lock:
        plan = generation_plans.get(str(plan_id or ""))
        if not plan:
            return None, "Generation plan not found"
        active = next((item for item in generation_runs.values() if item.get("status") in {"queued", "running", "cancelling"}), None)
        if active:
            return active, None
        run = {
            "id": _now_id(), "plan_id": plan["id"], "status": "queued", "created_at": utc_iso(),
            "file_count": plan["file_count"], "processed_count": 0, "generated_count": 0,
            "refused_count": 0, "progress_percent": 0, "progress_label": "Queued", "cancel_requested": False,
        }
        generation_runs[run["id"]] = run
    if synchronous:
        _execute_generation(run, plan)
    else:
        threading.Thread(target=_execute_generation, args=(run, plan), daemon=True, name=f"vid2gif-bif-generation-{run['id']}").start()
    return run, None


def generation_status(run_id=None):
    with preview_lock:
        if run_id:
            run = generation_runs.get(str(run_id or ""))
        elif generation_runs:
            run = max(generation_runs.values(), key=lambda item: item.get("created_at") or "")
        else:
            run = None
    if run_id and not run:
        return None, "Generation run not found"
    return {"run": public_generation_run(run)}, None


def cancel_generation(run_id):
    with preview_lock:
        run = generation_runs.get(str(run_id or ""))
        if not run:
            return None, "Generation run not found"
        if run.get("status") in {"queued", "running"}:
            run["cancel_requested"] = True
            run["status"] = "cancelling"
            run["progress_label"] = "Cancelling BIF generation"
    return public_generation_run(run), None


def _settings():
    settings = dict(app_settings.load_settings())
    settings.update(poster_maintenance.load_settings())
    return settings


def discover_thumbnail_tasks(settings=None, opener=None):
    settings = settings or _settings()
    inventory = emby_operations.load_tasks(settings, opener=opener, force=True)
    tasks = inventory.get("tasks") or []
    task_id = inventory.get("thumbnail_task_id") or ""
    task = next((item for item in tasks if item.get("id") == task_id), None)
    if inventory.get("status") == "ready":
        result = emby_client.result("success", inventory.get("message") or "Emby scheduled tasks loaded")
    else:
        status = "skipped" if inventory.get("status") == "not_configured" else "failed"
        result = emby_client.result(status, inventory.get("message") or "Emby scheduled tasks are unavailable")
    return {
        "configured": bool(inventory.get("configured")),
        "result": result,
        "tasks": tasks,
        "thumbnail_task": task,
        "operations": inventory,
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
    operation, operation_error = emby_operations.start_task(task_id, settings, opener=opener)
    result = emby_client.result(
        "success" if not operation_error else "failed",
        operation.get("message") or "Thumbnail extraction request failed",
    )
    log = _write_log("emby-task", {"result": result, "task": task})
    return {"result": result, "task": task, "log": log, "tasks": tasks}, None
