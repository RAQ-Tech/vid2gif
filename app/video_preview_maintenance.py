import datetime
import hashlib
import json
import os
import re
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


EXPECTED_INTERVAL_SECONDS = max(1, _env_int("VIDEO_PREVIEW_INTERVAL_SECONDS", 180))
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
SCAN_RETENTION_COUNT = 10
SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
LARGE_RESULT_COUNT = 100
LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "video-previews")
LOG_INDEX = os.path.join(LOG_DIR, "index.json")
LOG_RETENTION_COUNT = 25
LOG_MAX_BYTES = 1024 * 1024
__test__ = False

preview_scans = {}
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
    intervals = [
        item.get("interval_seconds")
        for item in bifs
        if item.get("interval_seconds") is not None
    ]
    status = "present"
    detail = "Preview BIF found"
    if not bifs:
        status = "missing"
        detail = "No matching BIF file found beside the video"
    elif intervals and EXPECTED_INTERVAL_SECONDS not in intervals:
        status = "stale"
        detail = f"BIF interval does not match {EXPECTED_INTERVAL_SECONDS} seconds"
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
    stale = sum(1 for item in items if item.get("status") == "stale")
    present = sum(1 for item in items if item.get("status") != "missing")
    return {
        "scanned_video_count": len(items),
        "present_count": present,
        "missing_count": missing,
        "stale_count": stale,
    }


def public_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    missing_count = counts.get("missing_count", 0)
    stale_count = counts.get("stale_count", 0)
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
        "stale_count": stale_count,
        "expected_interval_seconds": scan.get("expected_interval_seconds", EXPECTED_INTERVAL_SECONDS),
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": missing_count + stale_count >= LARGE_RESULT_COUNT,
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
        label = (
            f"{counts['missing_count']} missing, "
            f"{counts['stale_count']} interval mismatches"
        )
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
                "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
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
                "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
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
                "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
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
        "expected_interval_seconds": EXPECTED_INTERVAL_SECONDS,
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
    if status not in {"missing", "stale", "present", "all"}:
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
