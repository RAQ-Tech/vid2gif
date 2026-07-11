import datetime
import json
import os
import threading
import time

from . import actor_image_maintenance
from . import impact_metrics
from . import maintenance
from . import maintenance_scan_store
from . import poster_maintenance
from . import subtitle_maintenance
from . import test_lab
from . import video_preview_maintenance
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .jobs import jobs, job_queue, lock, queue_status_payload
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


DASHBOARD_ROOT = os.path.join(STATE_ROOT, "dashboard")
LIBRARY_INVENTORY_PATH = os.path.join(DASHBOARD_ROOT, "library-inventory.json")
LIBRARY_SCAN_RETENTION_SECONDS = 24 * 60 * 60
LIBRARY_FOLDER_LIMITS = (10, 25, 50, 100)
LIBRARY_FOLDER_SORT_FIELDS = {
    "name",
    "video_count",
    "video_size_bytes",
    "subtitle_count",
    "nfo_count",
    "bif_count",
    "poster_count",
    "background_count",
    "actor_image_count",
    "file_count",
}
SIDECARE_EXTS = {".srt", ".ass", ".ssa", ".vtt", ".sub"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
KNOWN_SKIP_DIRS = {
    ".vid2gif-duplicates",
    ".vid2gif-video-preview-repairs",
    "_previews",
    "__pycache__",
}
__test__ = False

dashboard_lock = threading.Lock()
library_scan = None


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


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
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _safe_status(callable_):
    try:
        payload = callable_()
    except Exception as exc:
        return {"error": str(exc)}
    if isinstance(payload, tuple):
        payload = payload[0]
    return payload or {}


def _safe_scan_payload(callable_):
    payload = _safe_status(callable_)
    return payload.get("scan") if isinstance(payload, dict) else None


def _safe_apply_payload(callable_):
    payload = _safe_status(callable_)
    return payload.get("apply") if isinstance(payload, dict) else None


def _percent(resolved, total):
    try:
        total = int(total)
        resolved = int(resolved)
    except (TypeError, ValueError):
        return 0
    if total <= 0:
        return 100 if resolved else 0
    return max(0, min(100, int(round(100 * resolved / total))))


def _workstream(
    key,
    title,
    href,
    *,
    status="not_scanned",
    found=0,
    ready=0,
    resolved=0,
    remaining=0,
    detail="",
    action_label="Open",
    needs_verification=False,
    active=False,
):
    total = max(int(found or 0), int(resolved or 0) + int(remaining or 0))
    progress = _percent(resolved, total)
    if active:
        state = "active"
    elif needs_verification:
        state = "needs_verification"
    elif status in {"success", "ok"} and not remaining and total:
        state = "clean"
    elif status in {"failed", "error"}:
        state = "attention"
    elif remaining or ready:
        state = "attention"
    elif status in {"not_scanned", "", None}:
        state = "not_scanned"
    else:
        state = "idle"
    return {
        "key": key,
        "title": title,
        "href": href,
        "state": state,
        "status": status or "not_scanned",
        "found": int(found or 0),
        "ready": int(ready or 0),
        "resolved": int(resolved or 0),
        "remaining": int(remaining or 0),
        "progress_percent": progress,
        "detail": detail,
        "action_label": action_label,
        "needs_verification": bool(needs_verification),
        "active": bool(active),
    }


def _job_summary():
    queue = queue_status_payload()
    with lock:
        all_jobs = list(jobs.values())
    with job_queue.mutex:
        queued_count = len(job_queue.queue)
    completed = [job for job in all_jobs if job.get("status") == "success"]
    failed = [job for job in all_jobs if job.get("status") == "failed"]
    stopped = [job for job in all_jobs if job.get("status") == "stopped"]
    output_size = sum(int(job.get("output_size_bytes") or 0) for job in completed)
    latest = sorted(all_jobs, key=lambda job: job.get("id", ""), reverse=True)[:5]
    return {
        "queue": queue,
        "queued_count": queued_count,
        "running_count": len(queue.get("running") or []),
        "completed_count": len(completed),
        "failed_count": len(failed),
        "stopped_count": len(stopped),
        "output_size_bytes": output_size,
        "output_size_label": format_size(output_size),
        "latest_jobs": [
            {
                "id": job.get("id", ""),
                "status": job.get("status", ""),
                "video": job.get("video", ""),
                "progress_percent": job.get("progress_percent", 0),
                "progress_label": job.get("progress_label", ""),
                "output_size_label": format_size(job.get("output_size_bytes")),
            }
            for job in latest
        ],
    }


def _test_lab_summary():
    payload = _safe_status(test_lab.status_payload)
    runs = payload.get("runs") or []
    active = payload.get("active_run") or {}
    return {
        "active_run": active,
        "run_count": len(runs),
        "file_count": payload.get("file_count", 0),
        "files_truncated": bool(payload.get("files_truncated")),
        "total_size_bytes": payload.get("total_size_bytes", 0),
        "total_size_label": payload.get("total_size_label", "0 B"),
    }


def _poster_summary():
    payload = _safe_status(poster_maintenance.status_payload)
    current = payload.get("current_run") or {}
    last = payload.get("last_run") or {}
    source = current or last
    counters = source.get("counters") or {}
    analysis = payload.get("analysis_scan") or {}
    changed = analysis.get("eligible_count", counters.get("updated", 0)) or 0
    skipped = (
        counters.get("already_matching", 0)
        + counters.get("missing_poster", 0)
        + counters.get("folders_skipped_unchanged", 0)
    )
    errors = counters.get("errors", 0) or 0
    settings = payload.get("settings") or {}
    emby = payload.get("emby_status") or {}
    active = bool(current and current.get("status") in {"queued", "running"})
    return {
        "payload": payload,
        "changed_count": changed,
        "skipped_count": skipped,
        "error_count": errors,
        "automation_enabled": bool(settings.get("enabled")),
        "emby_configured": bool(emby.get("configured")),
        "active": active,
        "has_run": bool(current or last),
        "scan": analysis,
    }


def _duplicate_summary():
    scan = _safe_scan_payload(maintenance.status_payload) or {}
    apply_run = _safe_apply_payload(maintenance.duplicate_apply_status) or {}
    result = apply_run.get("result") or {}
    active = bool(scan.get("active") or apply_run.get("status") in {"queued", "running"})
    resolved = result.get("applied_count") or apply_run.get("applied_count") or 0
    found = scan.get("duplicate_group_count") or 0
    remaining = found if not resolved else max(0, found - resolved)
    return {
        "scan": scan,
        "apply": apply_run,
        "found": found,
        "ready": found,
        "resolved": resolved,
        "remaining": remaining,
        "reclaimable_label": scan.get("reclaimable_label", "0 B"),
        "needs_verification": bool(resolved and scan.get("finished_at")),
        "active": active,
    }


def _preview_summary():
    scan = _safe_scan_payload(video_preview_maintenance.status_payload) or {}
    quality = _safe_scan_payload(video_preview_maintenance.quality_status_payload) or {}
    apply_run = _safe_apply_payload(video_preview_maintenance.quality_apply_status) or {}
    result = apply_run.get("result") or {}
    active = bool(
        scan.get("active")
        or quality.get("active")
        or apply_run.get("status") in {"queued", "running"}
    )
    missing = scan.get("missing_count") or 0
    bad = quality.get("bad_count") or 0
    warnings = quality.get("warning_count") or 0
    repaired = result.get("applied_count") or apply_run.get("applied_count") or 0
    return {
        "scan": scan,
        "quality": quality,
        "apply": apply_run,
        "missing_count": missing,
        "bad_count": bad,
        "warning_count": warnings,
        "repaired_count": repaired,
        "active": active,
        "needs_verification": bool(repaired),
    }


def _subtitle_summary():
    scan = _safe_scan_payload(subtitle_maintenance.status_payload) or {}
    active = bool(scan.get("active"))
    missing = scan.get("missing_count") or 0
    language_review = scan.get("language_review_count") or 0
    unknown = scan.get("unknown_count") or 0
    ok = scan.get("ok_count") or 0
    review = missing + language_review + unknown
    return {
        "scan": scan,
        "missing_count": missing,
        "language_review_count": language_review,
        "unknown_count": unknown,
        "ok_count": ok,
        "review_count": review,
        "active": active,
    }


def _actor_summary():
    scan = _safe_scan_payload(actor_image_maintenance.status_payload) or {}
    apply_run = _safe_apply_payload(actor_image_maintenance.apply_status) or {}
    result = apply_run.get("result") or {}
    active = bool(scan.get("active") or apply_run.get("status") in {"queued", "running"})
    missing = scan.get("missing_actor_count") or 0
    ready = scan.get("ready_count") or 0
    unresolved = scan.get("unresolved_count") or 0
    imported = result.get("imported_count") or apply_run.get("imported_count") or scan.get("imported_count") or 0
    return {
        "scan": scan,
        "apply": apply_run,
        "missing_count": missing,
        "ready_count": ready,
        "unresolved_count": unresolved,
        "imported_count": imported,
        "active": active,
        "needs_verification": bool(imported),
    }


def _recent_logs():
    entries = []
    for area, loader in (
        ("Duplicates", maintenance.list_duplicate_cleanup_logs),
        ("Video Previews", video_preview_maintenance.list_recent_logs),
        ("Actor Images", actor_image_maintenance.list_recent_logs),
    ):
        for item in _safe_status(lambda loader=loader: {"logs": loader()}).get("logs", [])[:5]:
            entries.append(
                {
                    "area": area,
                    "id": item.get("id", ""),
                    "created_at": item.get("created_at"),
                    "type": item.get("type") or item.get("action") or "log",
                    "size_label": item.get("size_label", ""),
                }
            )
    entries.sort(key=lambda item: item.get("created_at") or "", reverse=True)
    return entries[:8]


def _library_root_item(lib_root=LIB_ROOT):
    root = resolve_case_insensitive(lib_root) or lib_root
    root = os.path.realpath(root)
    return {"name": os.path.basename(root) or root, "path": root, "kind": "root"}


def _direct_library_items(lib_root=LIB_ROOT):
    root = _library_root_item(lib_root)["path"]
    libraries = []
    try:
        entries = os.listdir(root)
    except OSError:
        return libraries
    for entry in sorted(entries, key=str.lower):
        if entry.startswith(".") or entry in KNOWN_SKIP_DIRS:
            continue
        path = os.path.realpath(os.path.join(root, entry))
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        libraries.append({"name": entry, "path": path, "kind": "library"})
    return libraries


def _empty_library_stats(item):
    return {
        "name": item.get("name", ""),
        "path": item.get("path", ""),
        "kind": item.get("kind", ""),
        "video_count": 0,
        "video_size_bytes": 0,
        "video_size_label": "0 B",
        "subtitle_count": 0,
        "nfo_count": 0,
        "bif_count": 0,
        "poster_count": 0,
        "background_count": 0,
        "actor_image_count": 0,
        "other_sidecar_count": 0,
        "file_count": 0,
    }


def _normalise_library_stats(item, fallback):
    source = item if isinstance(item, dict) else {}
    stats = _empty_library_stats({**fallback, **source})
    for key in (
        "video_count",
        "video_size_bytes",
        "subtitle_count",
        "nfo_count",
        "bif_count",
        "poster_count",
        "background_count",
        "actor_image_count",
        "other_sidecar_count",
        "file_count",
    ):
        try:
            stats[key] = int(source.get(key, stats[key]) or 0)
        except (TypeError, ValueError):
            stats[key] = 0
    stats["video_size_label"] = source.get("video_size_label") or format_size(stats["video_size_bytes"])
    return stats


def _count_file(stats, path):
    _, ext = os.path.splitext(path)
    ext = ext.lower()
    name = os.path.basename(path).lower()
    stats["file_count"] += 1
    if ext in VIDEO_EXTS:
        stats["video_count"] += 1
        try:
            stats["video_size_bytes"] += os.path.getsize(path)
        except OSError:
            pass
    elif ext in SIDECARE_EXTS:
        stats["subtitle_count"] += 1
    elif ext == ".nfo":
        stats["nfo_count"] += 1
    elif ext == ".bif":
        stats["bif_count"] += 1
    elif ext in IMAGE_EXTS:
        if "-poster" in name or name.startswith("poster"):
            stats["poster_count"] += 1
        elif "-background" in name or "fanart" in name or "backdrop" in name:
            stats["background_count"] += 1
        elif "-performer-" in name or "-actor-" in name:
            stats["actor_image_count"] += 1
        else:
            stats["other_sidecar_count"] += 1
    else:
        stats["other_sidecar_count"] += 1


def _format_library_stats(stats):
    stats["video_size_label"] = format_size(stats["video_size_bytes"])
    return stats


def _inventory_from_data(data, lib_root=LIB_ROOT):
    source = data if isinstance(data, dict) else {}
    root_item = _library_root_item(lib_root)
    if isinstance(source.get("root"), dict):
        root = _normalise_library_stats(source.get("root"), root_item)
        folders = [
            _normalise_library_stats(item, {"name": item.get("name", ""), "path": item.get("path", ""), "kind": "library"})
            for item in source.get("folders") or []
            if isinstance(item, dict)
        ]
    else:
        old_libraries = [item for item in source.get("libraries") or [] if isinstance(item, dict)]
        old_root = next((item for item in old_libraries if item.get("kind") == "root"), old_libraries[0] if old_libraries else {})
        root = _normalise_library_stats(old_root, root_item)
        folders = [
            _normalise_library_stats(item, {"name": item.get("name", ""), "path": item.get("path", ""), "kind": "library"})
            for item in old_libraries
            if item is not old_root and item.get("kind") != "root"
        ]
    folders.sort(key=lambda item: (item.get("name") or "").lower())
    return {
        "schema_version": 2,
        "status": source.get("status", "not_scanned"),
        "finished_at": source.get("finished_at"),
        "library_count": len(folders) + 1,
        "folder_count": len(folders),
        "video_count": root.get("video_count", 0),
        "video_size_bytes": root.get("video_size_bytes", 0),
        "video_size_label": root.get("video_size_label", "0 B"),
        "root": root,
        "folders": folders,
    }


def _direct_child_name(root, path):
    try:
        rel = os.path.relpath(path, root)
    except ValueError:
        return ""
    if rel in {"", "."}:
        return ""
    return rel.split(os.sep, 1)[0]


def _scan_library_inventory(scan):
    root_item = _library_root_item(scan.get("path") or LIB_ROOT)
    root_path = root_item["path"]
    root_stats = _empty_library_stats(root_item)
    folders = [_empty_library_stats(item) for item in _direct_library_items(root_path)]
    folders_by_name = {item["name"]: item for item in folders}
    scanned_dirs = 0
    scanned_files = 0

    for base, dirs, files in os.walk(root_path, followlinks=False):
        if scan.get("cancel_requested"):
            break
        dirs[:] = sorted(
            [
                dirname
                for dirname in dirs
                if not dirname.startswith(".")
                and dirname not in KNOWN_SKIP_DIRS
                and not os.path.islink(os.path.join(base, dirname))
            ],
            key=str.lower,
        )
        scanned_dirs += 1
        for filename in files:
            full_path = os.path.join(base, filename)
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                continue
            _count_file(root_stats, full_path)
            child_name = _direct_child_name(root_path, full_path)
            child_stats = folders_by_name.get(child_name)
            if child_stats:
                _count_file(child_stats, full_path)
            scanned_files += 1
        if scanned_dirs % 50 == 0:
            with dashboard_lock:
                scan.update(
                    {
                        "progress_percent": 25,
                        "progress_label": f"Scanned {scanned_dirs} folders and {scanned_files} files",
                        "library_count": len(folders) + 1,
                        "folder_count": len(folders),
                        "root": _format_library_stats(dict(root_stats)),
                        "video_count": root_stats["video_count"],
                        "video_size_bytes": root_stats["video_size_bytes"],
                        "video_size_label": format_size(root_stats["video_size_bytes"]),
                    }
                )

    root_stats = _format_library_stats(root_stats)
    folders = [_format_library_stats(item) for item in folders]
    return {
        "status": "cancelled" if scan.get("cancel_requested") else "success",
        "finished_at": utc_iso(),
        "library_count": len(folders) + 1,
        "folder_count": len(folders),
        "video_count": root_stats["video_count"],
        "video_size_bytes": root_stats["video_size_bytes"],
        "video_size_label": root_stats["video_size_label"],
        "root": root_stats,
        "folders": folders,
    }


def _run_library_scan(scan_id):
    global library_scan
    with dashboard_lock:
        scan = library_scan if library_scan and library_scan.get("id") == scan_id else None
        if not scan:
            return
        scan.update({"status": "running", "progress_percent": 1, "progress_label": "Scanning libraries", "started_at": utc_iso()})
    try:
        result = _scan_library_inventory(scan)
        with dashboard_lock:
            scan.update(result)
            scan["progress_percent"] = 100
            scan["progress_label"] = "Library inventory complete" if result["status"] == "success" else "Library inventory cancelled"
            scan["status"] = result["status"]
        if result["status"] == "success":
            _write_json(LIBRARY_INVENTORY_PATH, {"schema_version": 2, **result})
            maintenance_scan_store.persist_success(
                "overview", "overview", {**scan, "status": "success"}, scan.get("path") or LIB_ROOT
            )
    except Exception as exc:
        with dashboard_lock:
            scan.update(
                {
                    "status": "failed",
                    "error": str(exc),
                    "progress_percent": 100,
                    "progress_label": "Library inventory failed",
                    "finished_at": utc_iso(),
                }
            )


def _overview_scan_metadata(public, scan=None):
    cached = maintenance_scan_store.load_latest("overview") or {}
    cached_scan = cached.get("scan") or {}
    if not public.get("id") and cached_scan.get("id"):
        public["id"] = cached_scan.get("id")
        public["path"] = cached_scan.get("path")
    public.update(maintenance_scan_store.public_cache_metadata("overview", scan or cached_scan))
    if not scan and cached_scan:
        public["restored"] = True
    return public


def _public_library_scan(scan):
    if not scan:
        cached = _inventory_from_data(_read_json(LIBRARY_INVENTORY_PATH, {}))
        if cached.get("finished_at"):
            return _overview_scan_metadata({
                "id": "",
                "status": "cached",
                "active": False,
                "progress_percent": 100,
                "progress_label": "Cached library inventory",
                "started_at": None,
                "finished_at": cached.get("finished_at"),
                "error": "",
                "library_count": cached.get("library_count", 0),
                "folder_count": cached.get("folder_count", 0),
                "scanned_library_count": cached.get("library_count", 0),
                "video_count": cached.get("video_count", 0),
                "video_size_bytes": cached.get("video_size_bytes", 0),
                "video_size_label": cached.get("video_size_label", "0 B"),
                "root": cached.get("root") or _empty_library_stats(_library_root_item()),
            })
        direct_folders = _direct_library_items()
        root = _empty_library_stats(_library_root_item())
        return _overview_scan_metadata({
            "id": "",
            "status": "not_scanned",
            "active": False,
            "progress_percent": 0,
            "progress_label": "Not scanned",
            "started_at": None,
            "finished_at": None,
            "error": "",
            "library_count": len(direct_folders) + 1,
            "folder_count": len(direct_folders),
            "scanned_library_count": 0,
            "video_count": 0,
            "video_size_bytes": 0,
            "video_size_label": "0 B",
            "root": root,
        })
    root = scan.get("root") or _empty_library_stats(_library_root_item(scan.get("path") or LIB_ROOT))
    return _overview_scan_metadata({
        "id": scan.get("id", ""),
        "status": scan.get("status", ""),
        "active": scan.get("status") in {"queued", "running", "cancelling"},
        "progress_percent": scan.get("progress_percent", 0),
        "progress_label": scan.get("progress_label", ""),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "error": scan.get("error", ""),
        "library_count": scan.get("library_count", 0),
        "folder_count": scan.get("folder_count", 0),
        "scanned_library_count": scan.get("scanned_library_count", 0),
        "video_count": scan.get("video_count", 0),
        "video_size_bytes": scan.get("video_size_bytes", 0),
        "video_size_label": scan.get("video_size_label", "0 B"),
        "root": root,
    }, scan)


def start_library_scan(path=None, synchronous=False):
    global library_scan
    target = str(path or LIB_ROOT).strip()
    real = resolve_case_insensitive(target)
    if not real or not os.path.isdir(real) or os.path.islink(real) or not path_is_under(real, LIB_ROOT):
        return None, "Path not found"
    with dashboard_lock:
        if library_scan and library_scan.get("status") in {"queued", "running", "cancelling"}:
            return _public_library_scan(library_scan), None
        scan_id = _now_id()
        library_scan = {
            "id": scan_id,
            "path": os.path.realpath(real),
            "status": "queued",
            "progress_percent": 0,
            "progress_label": "Queued",
            "created_at": utc_iso(),
            "started_at": None,
            "finished_at": None,
            "error": "",
            "cancel_requested": False,
            "root": _empty_library_stats(_library_root_item(real)),
            "folders": [],
        }
        scan = library_scan
    if synchronous:
        _run_library_scan(scan_id)
    else:
        threading.Thread(
            target=_run_library_scan,
            args=(scan_id,),
            daemon=True,
            name=f"vid2gif-dashboard-library-scan-{scan_id}",
        ).start()
    return _public_library_scan(scan), None


def library_scan_status():
    with dashboard_lock:
        scan = dict(library_scan) if library_scan else None
    return {"scan": _public_library_scan(scan)}


def cancel_library_scan():
    global library_scan
    with dashboard_lock:
        if not library_scan:
            return None, "Scan not found"
        if library_scan.get("status") not in {"success", "failed", "cancelled", "cached"}:
            library_scan["cancel_requested"] = True
            library_scan["status"] = "cancelling"
            library_scan["progress_label"] = "Cancelling library inventory"
        return _public_library_scan(library_scan), None


def _current_inventory():
    with dashboard_lock:
        scan = dict(library_scan) if library_scan else None
    if scan:
        return _inventory_from_data(scan, scan.get("path") or LIB_ROOT), _public_library_scan(scan)
    cached = _read_json(LIBRARY_INVENTORY_PATH, {})
    inventory = _inventory_from_data(cached)
    return inventory, _public_library_scan(None)


def _parse_folder_limit(value):
    try:
        limit = int(value)
    except (TypeError, ValueError):
        return 25
    return limit if limit in LIBRARY_FOLDER_LIMITS else 25


def _parse_offset(value):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def library_folders_payload(offset=0, limit=25, q="", sort="name", direction="asc"):
    inventory, scan = _current_inventory()
    limit = _parse_folder_limit(limit)
    offset = _parse_offset(offset)
    query = str(q or "").strip().lower()
    sort_key = sort if sort in LIBRARY_FOLDER_SORT_FIELDS else "name"
    direction = "desc" if str(direction or "").lower() == "desc" else "asc"
    folders = list(inventory.get("folders") or [])
    if query:
        folders = [
            item for item in folders
            if query in str(item.get("name", "")).lower()
            or query in str(item.get("path", "")).lower()
        ]
    folders.sort(key=lambda item: str(item.get("name", "")).lower())
    if sort_key == "name":
        folders.sort(key=lambda item: str(item.get("name", "")).lower(), reverse=direction == "desc")
    else:
        folders.sort(key=lambda item: int(item.get(sort_key, 0) or 0), reverse=direction == "desc")
    total = len(folders)
    page = folders[offset:offset + limit]
    count = len(page)
    return {
        "scan": scan,
        "root": inventory.get("root") or scan.get("root"),
        "folders": page,
        "total": total,
        "count": count,
        "offset": offset,
        "limit": limit,
        "q": q or "",
        "sort": sort_key,
        "direction": direction,
        "has_previous": offset > 0,
        "previous_offset": max(0, offset - limit),
        "has_next": offset + count < total,
        "next_offset": offset + limit if offset + count < total else offset,
        "large_result": total > limit,
    }


def status_payload():
    from . import maintenance_scan_orchestrator

    impact = impact_metrics.status_payload()
    gifs = _job_summary()
    test_summary = _test_lab_summary()
    posters = _poster_summary()
    duplicates = _duplicate_summary()
    previews = _preview_summary()
    subtitles = _subtitle_summary()
    actors = _actor_summary()
    library = library_scan_status()["scan"]

    preview_found = previews["missing_count"] + previews["bad_count"] + previews["warning_count"]
    preview_remaining = previews["missing_count"] + previews["bad_count"] + previews["warning_count"]
    actor_remaining = actors["missing_count"]
    workstreams = [
        _workstream(
            "gifs",
            "GIF Jobs",
            "/gifs",
            status="active" if gifs["running_count"] or gifs["queued_count"] else "ok",
            found=gifs["completed_count"] + gifs["failed_count"],
            ready=gifs["queued_count"],
            resolved=gifs["completed_count"],
            remaining=gifs["queued_count"] + gifs["running_count"] + gifs["failed_count"],
            detail=f"{gifs['running_count']} running, {gifs['queued_count']} queued, {gifs['failed_count']} failed",
            action_label="Open GIFs",
            active=bool(gifs["running_count"] or gifs["queued_count"]),
        ),
        _workstream(
            "duplicates",
            "Duplicates",
            "/maintenance#duplicates",
            status=(duplicates["scan"] or {}).get("status", "not_scanned"),
            found=duplicates["found"],
            ready=duplicates["ready"],
            resolved=0,
            remaining=duplicates["found"],
            detail=f"{duplicates['found']} groups, {duplicates['reclaimable_label']} reclaimable",
            action_label="Review duplicates",
            needs_verification=duplicates["needs_verification"],
            active=duplicates["active"],
        ),
        _workstream(
            "posters",
            "Landscape Posters",
            "/maintenance#posters",
            status=(posters.get("scan") or {}).get("status") or ("active" if posters["active"] else ("ok" if posters["has_run"] else "not_scanned")),
            found=posters["changed_count"] + posters["error_count"],
            ready=posters["changed_count"],
            resolved=posters["changed_count"],
            remaining=posters["error_count"],
            detail=f"{posters['changed_count']} updated, {posters['skipped_count']} skipped, automation {'on' if posters['automation_enabled'] else 'off'}",
            action_label="Open posters",
            active=posters["active"],
        ),
        _workstream(
            "video_previews",
            "Video Previews",
            "/maintenance#video-previews",
            status=(previews["scan"] or {}).get("status") or (previews["quality"] or {}).get("status") or "not_scanned",
            found=preview_found,
            ready=previews["bad_count"],
            resolved=0,
            remaining=preview_remaining,
            detail=f"{previews['missing_count']} missing, {previews['bad_count']} bad, {previews['warning_count']} warnings",
            action_label="Open previews",
            needs_verification=previews["needs_verification"],
            active=previews["active"],
        ),
        _workstream(
            "subtitles",
            "Subtitles",
            "/maintenance#subtitles",
            status=(subtitles["scan"] or {}).get("status", "not_scanned"),
            found=subtitles["review_count"],
            ready=subtitles["language_review_count"] + subtitles["unknown_count"],
            resolved=0,
            remaining=subtitles["review_count"],
            detail=f"{subtitles['missing_count']} missing, {subtitles['language_review_count']} language review, {subtitles['unknown_count']} unknown",
            action_label="Open subtitles",
            active=subtitles["active"],
        ),
        _workstream(
            "actor_images",
            "Actor Images",
            "/maintenance#actor-images",
            status=(actors["scan"] or {}).get("status", "not_scanned"),
            found=actors["missing_count"],
            ready=actors["ready_count"],
            resolved=0,
            remaining=actor_remaining,
            detail=f"{actors['ready_count']} ready, {actors['unresolved_count']} unresolved",
            action_label="Open actors",
            needs_verification=actors["needs_verification"],
            active=actors["active"],
        ),
    ]

    scan_sources = {
        "duplicates": duplicates.get("scan") or {},
        "video_previews": previews.get("scan") or previews.get("quality") or {},
        "subtitles": subtitles.get("scan") or {},
        "posters": posters.get("scan") or {},
        "actor_images": actors.get("scan") or {},
    }
    for item in workstreams:
        key = item.get("key")
        source = scan_sources.get(key) or {}
        item.update(
            {
                "scan_available": key in scan_sources,
                "scan_id": source.get("id", ""),
                "latest_success_at": source.get("finished_at"),
                "scan_age_seconds": source.get("scan_age_seconds"),
                "restored": bool(source.get("restored")),
                "freshness": source.get("freshness") or {"status": "unknown"},
            }
        )

    unresolved = sum(item["remaining"] for item in workstreams if item["key"] != "gifs")
    active = sum(1 for item in workstreams if item["active"])
    attention = sum(1 for item in workstreams if item["state"] in {"attention", "needs_verification"})
    maintenance_workstreams = [item for item in workstreams if item["key"] != "gifs"]
    not_scanned = sum(1 for item in maintenance_workstreams if item["state"] == "not_scanned")
    health_score = (
        0
        if not_scanned == len(maintenance_workstreams)
        else max(0, min(100, 100 - min(80, unresolved * 2) - min(20, attention * 4)))
    )
    if active:
        health_label = "Work running"
    elif unresolved:
        health_label = "Needs review"
    elif not_scanned == len(maintenance_workstreams):
        health_label = "Not scanned"
    elif not_scanned:
        health_label = "Partially scanned"
    else:
        health_label = "Clear"

    return {
        "generated_at": utc_iso(),
        "lib_root": LIB_ROOT,
        "maintenance_scope": maintenance_scan_orchestrator.last_scope(),
        "maintenance_scan": maintenance_scan_orchestrator.status(),
        "freshness_check": maintenance_scan_store.freshness_status(),
        "health": {
            "label": health_label,
            "score": health_score,
            "unresolved_count": unresolved,
            "active_count": active,
            "attention_count": attention,
            "not_scanned_count": not_scanned,
        },
        "impact": impact,
        "creative_output": impact.get("creative_output") or {},
        "gifs": gifs,
        "test_lab": test_summary,
        "posters": posters,
        "duplicates": duplicates,
        "video_previews": previews,
        "subtitles": subtitles,
        "actor_images": actors,
        "library": library,
        "workstreams": workstreams,
        "recent_activity": _recent_logs(),
    }
