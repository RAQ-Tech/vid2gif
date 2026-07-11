import copy
import datetime
import gzip
import json
import os
import threading
import time

from . import config
from .progress import utc_iso
from .utils import path_is_under


SCHEMA_VERSION = 1
FRESHNESS_THROTTLE_SECONDS = 15 * 60
AREA_CACHE_KEYS = {
    "overview": ("overview",),
    "duplicates": ("duplicates",),
    "video_previews": ("video_previews_missing", "video_previews_quality"),
    "subtitles": ("subtitles",),
    "posters": ("posters",),
    "actor_images": ("actor_images",),
}
VIDEO_EXTS = set(config.VIDEO_EXTS)
SUBTITLE_EXTS = {".srt"}
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
DUPLICATE_ACCESSORY_EXTS = {
    ".srt", ".ass", ".ssa", ".vtt", ".sub", ".nfo", ".jpg", ".jpeg",
    ".png", ".webp", ".bif",
}
OVERVIEW_EXTS = VIDEO_EXTS | DUPLICATE_ACCESSORY_EXTS
SKIP_DIRS = {
    ".vid2gif-duplicates",
    ".vid2gif-video-preview-repairs",
    ".vid2gif-subtitle-quarantine",
    "_previews",
    "__pycache__",
}

_lock = threading.Lock()
_file_lock = threading.Lock()
_freshness_run = {
    "status": "idle",
    "active": False,
    "started_at": None,
    "finished_at": None,
    "areas": {},
}


def _root():
    return os.path.join(config.STATE_ROOT, "maintenance-scans")


def _path(cache_key):
    return os.path.join(_root(), f"{cache_key}.json.gz")


def _backup_path(cache_key):
    return f"{_path(cache_key)}.bak"


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _atomic_write_gzip(path, payload):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with gzip.open(tmp, "wt", encoding="utf-8") as handle:
            json.dump(payload, handle, separators=(",", ":"))
        valid_primary = False
        if os.path.isfile(path):
            try:
                valid_primary = _read_gzip(path).get("schema_version") == SCHEMA_VERSION
            except Exception:
                valid_primary = False
        if valid_primary:
            backup = f"{path}.bak"
            backup_tmp = f"{backup}.{os.getpid()}.tmp"
            try:
                with open(path, "rb") as source, open(backup_tmp, "wb") as target:
                    while True:
                        chunk = source.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                os.replace(backup_tmp, backup)
            finally:
                if os.path.exists(backup_tmp):
                    os.remove(backup_tmp)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _read_gzip(path):
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported maintenance scan cache")
    return payload


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, set):
        return [_json_safe(item) for item in sorted(value, key=str)]
    return value


def load_latest(cache_key):
    for candidate in (_path(cache_key), _backup_path(cache_key)):
        try:
            payload = _read_gzip(candidate)
        except (OSError, ValueError, TypeError, json.JSONDecodeError, gzip.BadGzipFile):
            continue
        payload["restored_from_backup"] = candidate.endswith(".bak")
        return payload
    return None


def _is_relevant(area, filename):
    stem, ext = os.path.splitext(filename)
    ext = ext.lower()
    lower = stem.lower()
    if area == "overview":
        return ext in OVERVIEW_EXTS
    if area == "duplicates":
        return ext in VIDEO_EXTS or ext in DUPLICATE_ACCESSORY_EXTS
    if area == "video_previews":
        return ext in VIDEO_EXTS or ext == ".bif"
    if area == "subtitles":
        return ext in VIDEO_EXTS or ext in SUBTITLE_EXTS
    if area == "posters":
        return ext in IMAGE_EXTS and (
            lower.endswith("-background")
            or lower.endswith("-poster")
            or lower.endswith("-poster-backup")
        )
    if area == "actor_images":
        return ext in VIDEO_EXTS or ext in IMAGE_EXTS
    return False


def capture_manifest(area, path, lib_root=None):
    root = os.path.realpath(path)
    library = os.path.realpath(lib_root or config.LIB_ROOT)
    if not os.path.isdir(root) or os.path.islink(root) or not path_is_under(root, library):
        raise ValueError("Scan path is no longer available")
    files = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = [
            name for name in dirs
            if name not in SKIP_DIRS and not os.path.islink(os.path.join(base, name))
        ]
        for name in names:
            if not _is_relevant(area, name):
                continue
            full_path = os.path.join(base, name)
            if os.path.islink(full_path) or not os.path.isfile(full_path):
                continue
            try:
                stat = os.stat(full_path)
            except OSError:
                continue
            relative = os.path.relpath(full_path, root).replace(os.sep, "/")
            files[relative] = [
                int(stat.st_size),
                int(getattr(stat, "st_mtime_ns", stat.st_mtime * 1_000_000_000)),
            ]
    return files


def persist_success(cache_key, area, scan, lib_root=None, manifest=None):
    if not isinstance(scan, dict) or scan.get("status") != "success":
        return False
    path = os.path.realpath(scan.get("path") or lib_root or config.LIB_ROOT)
    try:
        identities = manifest if manifest is not None else capture_manifest(area, path, lib_root)
        payload = {
            "schema_version": SCHEMA_VERSION,
            "cache_key": cache_key,
            "area": area,
            "saved_at": utc_iso(),
            "path": path,
            "scan": _json_safe(copy.deepcopy(scan)),
            "manifest": identities,
            "freshness": {
                "status": "unchanged",
                "checked_at": utc_iso(),
                "added": 0,
                "removed": 0,
                "changed": 0,
            },
        }
        with _file_lock:
            _atomic_write_gzip(_path(cache_key), payload)
        return True
    except (OSError, TypeError, ValueError):
        return False


def freshness_for(cache_key):
    payload = load_latest(cache_key)
    if not payload:
        return {"status": "unknown", "checked_at": None, "added": 0, "removed": 0, "changed": 0}
    return dict(payload.get("freshness") or {"status": "unknown"})


def public_cache_metadata(cache_key, scan=None):
    payload = load_latest(cache_key)
    freshness = dict((payload or {}).get("freshness") or {"status": "unknown"})
    finished_at = (scan or {}).get("finished_at") or ((payload or {}).get("scan") or {}).get("finished_at")
    finished_ts = _parse_iso(finished_at)
    return {
        "restored": bool((scan or {}).get("_restored")),
        "scan_age_seconds": max(0, int(time.time() - finished_ts)) if finished_ts else None,
        "freshness": freshness,
    }


def restore_scan(cache_key):
    payload = load_latest(cache_key)
    scan = copy.deepcopy((payload or {}).get("scan"))
    if not isinstance(scan, dict) or scan.get("status") != "success" or not scan.get("id"):
        return None
    scan["_restored"] = True
    scan["_persisted_latest"] = True
    return scan


def action_allowed(cache_key, scan_id):
    payload = load_latest(cache_key)
    if not payload or str((payload.get("scan") or {}).get("id") or "") != str(scan_id or ""):
        return True, ""
    freshness = payload.get("freshness") or {}
    if freshness.get("status") == "changed":
        return False, "Library files changed after this scan. Rescan before creating an action plan."
    return True, ""


def _check_cache(cache_key, force=False):
    with _file_lock:
        return _check_cache_locked(cache_key, force=force)


def _check_cache_locked(cache_key, force=False):
    payload = load_latest(cache_key)
    if not payload:
        return {"status": "unknown", "checked_at": utc_iso(), "added": 0, "removed": 0, "changed": 0}
    previous = payload.get("freshness") or {}
    checked_ts = _parse_iso(previous.get("checked_at"))
    if not force and checked_ts and time.time() - checked_ts < FRESHNESS_THROTTLE_SECONDS:
        return dict(previous)
    try:
        current = capture_manifest(payload.get("area"), payload.get("path"))
        original = payload.get("manifest") or {}
        added = len(set(current) - set(original))
        removed = len(set(original) - set(current))
        changed = sum(1 for key in set(current) & set(original) if current[key] != original[key])
        freshness = {
            "status": "changed" if added or removed or changed else "unchanged",
            "checked_at": utc_iso(),
            "added": added,
            "removed": removed,
            "changed": changed,
        }
    except Exception as exc:
        freshness = {
            "status": "unknown",
            "checked_at": utc_iso(),
            "added": 0,
            "removed": 0,
            "changed": 0,
            "error": str(exc),
        }
    payload["freshness"] = freshness
    try:
        _atomic_write_gzip(_path(cache_key), payload)
    except OSError:
        pass
    return freshness


def _run_freshness(areas, force):
    global _freshness_run
    results = {}
    try:
        for area in areas:
            keys = AREA_CACHE_KEYS.get(area, ())
            key_results = [_check_cache(key, force=force) for key in keys]
            if not key_results:
                results[area] = {"status": "unknown"}
                continue
            statuses = {item.get("status") for item in key_results}
            status = "changed" if "changed" in statuses else ("unknown" if "unknown" in statuses else "unchanged")
            results[area] = {
                "status": status,
                "checked_at": max((item.get("checked_at") or "" for item in key_results), default="") or None,
                "added": sum(int(item.get("added") or 0) for item in key_results),
                "removed": sum(int(item.get("removed") or 0) for item in key_results),
                "changed": sum(int(item.get("changed") or 0) for item in key_results),
            }
    finally:
        with _lock:
            _freshness_run = {
                "status": "complete",
                "active": False,
                "started_at": _freshness_run.get("started_at"),
                "finished_at": utc_iso(),
                "areas": results,
            }


def start_freshness(areas=None, force=False, synchronous=False):
    global _freshness_run
    selected = [area for area in (areas or AREA_CACHE_KEYS) if area in AREA_CACHE_KEYS]
    with _lock:
        if _freshness_run.get("active"):
            return copy.deepcopy(_freshness_run)
        _freshness_run = {
            "status": "checking",
            "active": True,
            "started_at": utc_iso(),
            "finished_at": None,
            "areas": {area: {"status": "checking"} for area in selected},
        }
    if synchronous:
        _run_freshness(selected, bool(force))
    else:
        threading.Thread(
            target=_run_freshness,
            args=(selected, bool(force)),
            daemon=True,
            name="vid2gif-maintenance-freshness",
        ).start()
    return freshness_status()


def freshness_status():
    with _lock:
        return copy.deepcopy(_freshness_run)
