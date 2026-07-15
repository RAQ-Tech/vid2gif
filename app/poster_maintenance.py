import datetime
import hashlib
import json
import os
import subprocess
import threading
import time

from . import app_settings
from . import emby_catalog
from . import emby_client
from . import emby_sync
from . import emby_notifications
from . import impact_metrics
from . import maintenance_scan_store
from . import task_progress
from .config import (
    LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS,
    LANDSCAPE_POSTER_INTERVAL_SECONDS,
    LANDSCAPE_POSTER_ROOT,
    LIB_ROOT,
    VIDEO_EXTS,
)
from .file_safety import atomic_install_file, regular_file_identity, target_state
from .operation_gate import coordinated_library_operation
from .progress import format_duration, utc_iso
from .table_sort import sort_records
from .utils import BACKGROUND_IMAGE_EXTS, path_is_under, resolve_case_insensitive


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


SETTINGS_SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
EMBY_STATUS_SCHEMA_VERSION = 1
SETTINGS_PATH = os.path.join(LANDSCAPE_POSTER_ROOT, "settings.json")
MANIFEST_PATH = os.path.join(LANDSCAPE_POSTER_ROOT, "manifest.json")
EMBY_STATUS_PATH = os.path.join(LANDSCAPE_POSTER_ROOT, "emby-status.json")
BACKGROUND_SUFFIX = "-background"
POSTER_SUFFIX = "-poster"
BACKUP_SUFFIX = "-poster-backup"
MIN_SCAN_INTERVAL_SECONDS = 60
MIN_FULL_SCAN_INTERVAL_SECONDS = 3600
POSTER_RUN_RETENTION_COUNT = max(1, _env_int("POSTER_RUN_RETENTION_COUNT", 25))
POSTER_RUN_ITEM_RETENTION_COUNT = max(50, _env_int("POSTER_RUN_ITEM_RETENTION_COUNT", 200))
IMAGE_PROBE_TIMEOUT_SECONDS = max(1, _env_int("POSTER_IMAGE_PROBE_TIMEOUT", 10))
__test__ = False

_settings_lock = threading.RLock()
_manifest_lock = threading.Lock()
_emby_status_lock = threading.Lock()
_run_start_lock = threading.Lock()
_run_execution_lock = threading.Lock()
_worker_start_lock = threading.Lock()
_worker_started = False
_wake_event = threading.Event()

poster_runs = {}
poster_scans = {}
poster_plans = {}
poster_apply_runs = {}
_poster_cache_loaded = False
_poster_scan_lock = threading.Lock()
_poster_apply_execution_lock = threading.Lock()
_current_run_id = ""
_scheduler_state = {
    "last_checked_at": None,
    "next_run_at": None,
    "last_error": "",
}


class PosterScanCancelled(Exception):
    pass


def _env_truthy(name, default=False):
    raw = os.getenv(name)
    if raw is None:
        return bool(default)
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _env_str(name, default=""):
    return str(os.getenv(name, default) or "").strip()


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _safe_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clamp_interval(value, minimum):
    value = _safe_int(value, minimum)
    return max(minimum, value)


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _relative_path(path, root):
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return os.path.basename(path)


def _path_key(path, root):
    rel = os.path.normcase(_relative_path(path, root)).replace(os.sep, "/")
    return _hash_text(rel)


def _file_identity(path):
    return regular_file_identity(path)


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def _write_json_atomic(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_path = f"{path}.{os.getpid()}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, separators=(",", ":"))
        os.replace(tmp_path, path)
    except Exception:
        return False
    return True


def _write_settings_atomic(path, data):
    current = _read_json(path, None)
    if current is not None and not _write_json_atomic(f"{path}.bak", current):
        return False
    return _write_json_atomic(path, data)


def default_settings():
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "enabled": _env_truthy("LANDSCAPE_POSTER_AUTO", False),
        "scan_interval_seconds": _clamp_interval(
            LANDSCAPE_POSTER_INTERVAL_SECONDS,
            MIN_SCAN_INTERVAL_SECONDS,
        ),
        "full_scan_interval_seconds": _clamp_interval(
            LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS,
            MIN_FULL_SCAN_INTERVAL_SECONDS,
        ),
    }


def _coerce_settings(data, base=None):
    base = base or default_settings()
    if not isinstance(data, dict):
        data = {}
    scan_interval = _clamp_interval(
        data.get("scan_interval_seconds", base["scan_interval_seconds"]),
        MIN_SCAN_INTERVAL_SECONDS,
    )
    full_interval = _clamp_interval(
        data.get("full_scan_interval_seconds", base["full_scan_interval_seconds"]),
        MIN_FULL_SCAN_INTERVAL_SECONDS,
    )
    full_interval = max(full_interval, scan_interval)
    return {
        "schema_version": SETTINGS_SCHEMA_VERSION,
        "enabled": bool(data.get("enabled", base["enabled"])),
        "scan_interval_seconds": scan_interval,
        "full_scan_interval_seconds": full_interval,
    }


def load_settings(path=None):
    path = path or SETTINGS_PATH
    with _settings_lock:
        data = _read_json(path, None)
        if data is None:
            data = _read_json(f"{path}.bak", None)
        settings = _coerce_settings(data) if data is not None else default_settings()
        global_emby = app_settings.load_settings()
        settings["emby_url"] = global_emby.get("emby_url", "")
        settings["emby_api_key"] = global_emby.get("emby_api_key", "")
        settings["emby_path_mappings"] = list(global_emby.get("emby_path_mappings") or [])
        settings["emby_sync_after_maintenance"] = bool(global_emby.get("emby_sync_after_maintenance", True))
        settings["emby_refresh_enabled"] = settings["emby_sync_after_maintenance"]
        return settings


def save_settings(settings, path=None):
    path = path or SETTINGS_PATH
    emby_updates = {
        key: settings[key]
        for key in ("emby_url", "emby_api_key", "emby_path_mappings", "emby_sync_after_maintenance")
        if isinstance(settings, dict) and key in settings
    }
    if emby_updates:
        _global, error = app_settings.update_settings(emby_updates)
        if error:
            return False
    settings = _coerce_settings(settings)
    with _settings_lock:
        return _write_settings_atomic(path, settings)


def update_settings(updates, path=None):
    if not isinstance(updates, dict):
        return None, "Settings are invalid"
    updates = dict(updates)
    if "emby_refresh_enabled" in updates and "emby_sync_after_maintenance" not in updates:
        updates["emby_sync_after_maintenance"] = updates.get("emby_refresh_enabled")
    global_updates = {
        key: updates[key]
        for key in ("emby_url", "emby_api_key", "emby_api_key_clear", "emby_path_mappings", "emby_sync_after_maintenance")
        if key in updates
    }
    if global_updates:
        _global, error = app_settings.update_settings(global_updates)
        if error:
            return None, error
    path = path or SETTINGS_PATH
    with _settings_lock:
        current_data = _read_json(path, None)
        if current_data is None:
            current_data = _read_json(f"{path}.bak", None)
        current = _coerce_settings(current_data) if current_data is not None else default_settings()
        merged = dict(current)
        for key in (
            "enabled",
            "scan_interval_seconds",
            "full_scan_interval_seconds",
        ):
            if key in updates:
                merged[key] = updates[key]
        settings = _coerce_settings(merged, base=current)
        if not _write_settings_atomic(path, settings):
            return None, "Settings could not be saved"
    _wake_event.set()
    return public_settings(settings), None


def public_settings(settings=None):
    settings = _emby_settings(settings or load_settings())
    return {
        "enabled": bool(settings.get("enabled")),
        "scan_interval_seconds": settings.get("scan_interval_seconds"),
        "scan_interval_label": format_duration(settings.get("scan_interval_seconds")),
        "full_scan_interval_seconds": settings.get("full_scan_interval_seconds"),
        "full_scan_interval_label": format_duration(
            settings.get("full_scan_interval_seconds")
        ),
        "emby_refresh_enabled": bool(settings.get("emby_sync_after_maintenance", True)),
        "emby_api_key_configured": bool(settings.get("emby_api_key")),
    }


def _emby_settings(settings=None):
    provided = dict(settings or {})
    combined = dict(app_settings.load_settings())
    combined.update(provided)
    if "emby_refresh_enabled" in provided and "emby_sync_after_maintenance" not in provided:
        combined["emby_sync_after_maintenance"] = bool(provided.get("emby_refresh_enabled"))
    return combined


def default_manifest():
    return {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "folders": {},
        "records": {},
        "last_run": None,
        "last_full_run_at": None,
    }


def load_manifest(path=None):
    path = path or MANIFEST_PATH
    data = _read_json(path, {})
    if not data or data.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        return default_manifest()
    data.setdefault("folders", {})
    data.setdefault("records", {})
    data.setdefault("last_run", None)
    data.setdefault("last_full_run_at", None)
    return data


def save_manifest(manifest, path=None):
    path = path or MANIFEST_PATH
    manifest = dict(manifest or {})
    manifest["schema_version"] = MANIFEST_SCHEMA_VERSION
    with _manifest_lock:
        return _write_json_atomic(path, manifest)


def default_emby_status():
    return {
        "schema_version": EMBY_STATUS_SCHEMA_VERSION,
        "last_test": None,
        "last_refresh": None,
    }


def load_emby_status(path=None):
    path = path or EMBY_STATUS_PATH
    data = _read_json(path, {})
    if not data or data.get("schema_version") != EMBY_STATUS_SCHEMA_VERSION:
        return default_emby_status()
    data.setdefault("last_test", None)
    data.setdefault("last_refresh", None)
    return data


def save_emby_status(status, path=None):
    path = path or EMBY_STATUS_PATH
    status = dict(status or {})
    status["schema_version"] = EMBY_STATUS_SCHEMA_VERSION
    with _emby_status_lock:
        return _write_json_atomic(path, status)


def _save_emby_status_value(key, result):
    status = load_emby_status()
    status[key] = _public_emby_result(result)
    save_emby_status(status)
    return status[key]


def _candidate_from_background(path):
    directory = os.path.dirname(path)
    filename = os.path.basename(path)
    stem, ext = os.path.splitext(filename)
    if ext.lower() not in BACKGROUND_IMAGE_EXTS:
        return None
    if not stem.lower().endswith(BACKGROUND_SUFFIX):
        return None
    base = stem[: -len(BACKGROUND_SUFFIX)]
    if not base:
        return None
    return {
        "background_path": os.path.realpath(path),
        "poster_path": os.path.realpath(os.path.join(directory, f"{base}{POSTER_SUFFIX}{ext}")),
        "backup_path": os.path.realpath(os.path.join(directory, f"{base}{BACKUP_SUFFIX}{ext}")),
        "name": filename,
        "base": base,
        "ext": ext,
    }


def _background_candidate_groups(directory, files):
    """Group artwork by video stem so a folder may safely contain many videos."""
    groups = {}
    for name in sorted(files, key=str.lower):
        candidate = _candidate_from_background(os.path.join(directory, name))
        if not candidate:
            continue
        groups.setdefault(candidate["base"].casefold(), []).append(candidate)
    return [groups[key] for key in sorted(groups)]


def _relevant_file(name):
    stem, ext = os.path.splitext(name)
    if ext.lower() not in BACKGROUND_IMAGE_EXTS:
        return False
    lower = stem.lower()
    return (
        lower.endswith(BACKGROUND_SUFFIX)
        or lower.endswith(POSTER_SUFFIX)
        or lower.endswith(BACKUP_SUFFIX)
    )


def _folder_signature(base, files):
    parts = []
    for name in sorted(files, key=str.lower):
        if not _relevant_file(name):
            continue
        path = os.path.join(base, name)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        identity = _file_identity(path)
        if not identity:
            continue
        parts.append(
            f"{name.lower()}:{identity['size']}:{identity['mtime_ns']}"
        )
    encoded = "|".join(parts)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest() if encoded else ""


def _same_file_bytes(first, second):
    first_identity = _file_identity(first)
    second_identity = _file_identity(second)
    if not first_identity or not second_identity:
        return False
    if first_identity["size"] != second_identity["size"]:
        return False
    first_hash = hashlib.sha256()
    second_hash = hashlib.sha256()
    try:
        with open(first, "rb") as f1, open(second, "rb") as f2:
            while True:
                chunk1 = f1.read(1024 * 1024)
                chunk2 = f2.read(1024 * 1024)
                if chunk1 != chunk2:
                    return False
                if not chunk1:
                    break
                first_hash.update(chunk1)
                second_hash.update(chunk2)
    except OSError:
        return False
    return first_hash.digest() == second_hash.digest()


def _copy_file_atomic(source, target, *, root, expected_target=None):
    return atomic_install_file(
        source,
        target,
        root=root,
        expected_source=regular_file_identity(source),
        expected_target=(
            expected_target
            if expected_target is not None
            else target_state(target, root=root)
        ),
    )


def _probe_image_dimensions(path, timeout=IMAGE_PROBE_TIMEOUT_SECONDS):
    try:
        process = subprocess.run(
            [
                "ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height", "-of", "json", path,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        data = json.loads(process.stdout or "{}") if process.returncode == 0 else {}
        stream = (data.get("streams") or [{}])[0]
        width = int(stream.get("width") or 0)
        height = int(stream.get("height") or 0)
    except (OSError, ValueError, TypeError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return None
    if width <= 0 or height <= 0:
        return None
    return {"width": width, "height": height, "landscape": width > height}


def _matching_artwork(directory, base, suffix):
    matches = []
    try:
        names = os.listdir(directory)
    except OSError:
        return matches
    expected_stem = f"{base}{suffix}".lower()
    for name in names:
        stem, ext = os.path.splitext(name)
        if stem.lower() == expected_stem and ext.lower() in BACKGROUND_IMAGE_EXTS:
            matches.append(os.path.realpath(os.path.join(directory, name)))
    return sorted(matches, key=str.lower)


def _record_for(candidate, root, status, message=""):
    background_identity = _file_identity(candidate["background_path"])
    poster_identity = (
        _file_identity(candidate["poster_path"])
        if os.path.isfile(candidate["poster_path"])
        else None
    )
    backup_identity = (
        _file_identity(candidate["backup_path"])
        if os.path.isfile(candidate["backup_path"])
        else None
    )
    return {
        "source": _relative_path(candidate["background_path"], root),
        "poster": _relative_path(candidate["poster_path"], root),
        "backup": _relative_path(candidate["backup_path"], root),
        "background": background_identity,
        "poster_identity": poster_identity,
        "backup_identity": backup_identity,
        "status": status,
        "message": message,
        "updated_at": utc_iso(),
    }


def _public_item(candidate, root, status, message=""):
    return {
        "source": _relative_path(candidate["background_path"], root),
        "poster": _relative_path(candidate["poster_path"], root),
        "backup": _relative_path(candidate["backup_path"], root),
        "status": status,
        "message": message,
    }


def _process_candidate(candidate, root):
    background = candidate["background_path"]
    poster = candidate["poster_path"]
    backup = candidate["backup_path"]
    if not all(path_is_under(path, root) for path in (background, poster, backup)):
        return "error", "Path is outside the library"
    if os.path.islink(background) or os.path.islink(poster) or os.path.islink(backup):
        return "error", "Symlinked artwork is not modified"
    poster_matches = _matching_artwork(os.path.dirname(poster), candidate["base"], POSTER_SUFFIX)
    if len(poster_matches) > 1:
        return "error", "Multiple poster candidates are ambiguous"
    if not os.path.isfile(poster):
        if poster_matches:
            return "error", "Poster extension does not match the landscape background"
        return "missing_poster", "Matching poster does not exist"
    background_identity = _file_identity(background)
    poster_identity = _file_identity(poster)
    background_dimensions = _probe_image_dimensions(background)
    poster_dimensions = _probe_image_dimensions(poster)
    if not background_identity or not background_dimensions:
        return "skipped", "Background image is unreadable"
    if not background_dimensions["landscape"]:
        return "skipped", "Background image is not landscape"
    if not poster_identity or not poster_dimensions:
        return "error", "Poster image is unreadable"
    if poster_dimensions["landscape"]:
        return "already_matching", "Poster is already landscape"
    backup_exists = os.path.lexists(backup)
    backup_matches = _matching_artwork(os.path.dirname(backup), candidate["base"], BACKUP_SUFFIX)
    if len(backup_matches) > 1:
        return "error", "Multiple poster backup candidates are ambiguous"
    if backup_matches and os.path.realpath(backup) not in backup_matches:
        return "error", "Poster backup extension does not match the landscape background"
    if backup_exists:
        if not os.path.isfile(backup):
            return "error", "Existing backup is not a regular file"
        backup_dimensions = _probe_image_dimensions(backup)
        if not backup_dimensions:
            return "error", "Existing backup is unreadable"
        if backup_dimensions["landscape"]:
            return "error", "Existing backup is landscape; poster was not changed"
        backup_identity = _file_identity(backup)
    else:
        backup_identity = None
    if _file_identity(background) != background_identity or _file_identity(poster) != poster_identity:
        return "error", "Artwork changed during preflight"
    try:
        if not backup_exists:
            _copy_file_atomic(
                poster,
                backup,
                root=root,
                expected_target={"exists": False, "identity": None},
            )
            backup_created = True
            if _file_identity(backup).get("size") != poster_identity.get("size") or not _probe_image_dimensions(backup):
                raise RuntimeError("Backup verification failed")
        else:
            backup_created = False
            if _file_identity(backup) != backup_identity:
                raise RuntimeError("Backup changed during preflight")
        if _file_identity(background) != background_identity or _file_identity(poster) != poster_identity:
            raise RuntimeError("Artwork changed before replacement")
        _copy_file_atomic(
            background,
            poster,
            root=root,
            expected_target=target_state(poster, root=root),
        )
        installed = _probe_image_dimensions(poster)
        if not installed or not installed["landscape"] or installed["width"] != background_dimensions["width"] or installed["height"] != background_dimensions["height"]:
            _copy_file_atomic(
                backup,
                poster,
                root=root,
                expected_target=target_state(poster, root=root),
            )
            raise RuntimeError("Poster verification failed; original was restored")
    except Exception as exc:
        return "error", str(exc)
    return (
        "updated",
        "Poster replaced; backup created" if backup_created else "Poster replaced",
    )


def _poster_item_id(candidate, root):
    return f"poster-{_path_key(candidate['background_path'], root)[:24]}"


def _analysis_item(candidate, root, status, message):
    return {
        "id": _poster_item_id(candidate, root),
        "source": _relative_path(candidate["background_path"], root),
        "poster": _relative_path(candidate["poster_path"], root),
        "backup": _relative_path(candidate["backup_path"], root),
        "status": status,
        "message": message,
        "eligible": status == "eligible",
        "candidate": dict(candidate),
        "identities": {
            "background": _file_identity(candidate["background_path"]),
            "poster": _file_identity(candidate["poster_path"]),
            "backup": _file_identity(candidate["backup_path"]),
        },
    }


def _public_analysis_item(item):
    return {
        key: item.get(key)
        for key in (
            "id", "source", "poster", "backup", "status", "message", "eligible",
            "emby_item_id", "emby_item_type", "emby_item_name", "emby_match_status",
        )
    }


def _poster_video_paths(item):
    candidate = item.get("candidate") or {}
    folder = os.path.dirname(candidate.get("background_path") or "")
    base = str(candidate.get("base") or "").casefold()
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    return [
        os.path.realpath(os.path.join(folder, name))
        for name in names
        if os.path.splitext(name)[0].casefold() == base
        and os.path.splitext(name)[1].lower() in VIDEO_EXTS
        and os.path.isfile(os.path.join(folder, name))
        and not os.path.islink(os.path.join(folder, name))
    ]


def _enrich_poster_items(items, scan):
    settings = app_settings.load_settings()
    catalog, summary = emby_catalog.load_catalog(
        settings,
        before_page=lambda: _check_poster_cancelled(scan),
    )
    mappings = settings.get("emby_path_mappings") or []
    counts = {"matched": 0, "unmatched": 0, "ambiguous": 0}
    for item in items:
        if scan.get("cancel_requested"):
            task_progress.update_scan(
                scan,
                "poster_scan",
                100,
                "Poster analysis cancelled",
                status="cancelled",
                finished_at=utc_iso(),
            )
            return None
        video_paths = _poster_video_paths(item)
        match = emby_catalog.match_paths(catalog, video_paths, mappings)
        if match.get("emby_match_status") == "unmatched":
            folder = os.path.dirname((item.get("candidate") or {}).get("background_path") or "")
            match = emby_catalog.match_path(catalog, folder, mappings)
        item.update(match)
        counts[match["emby_match_status"]] += 1
    summary = dict(summary)
    summary.update(
        total_count=len(items),
        matched_count=counts["matched"],
        unmatched_count=counts["unmatched"],
        ambiguous_count=counts["ambiguous"],
    )
    if summary.get("status") == "complete" and items:
        summary["status"] = "complete" if counts["matched"] == len(items) else "partial"
        summary["message"] = f"Matched {counts['matched']} of {len(items)} poster records to Emby."
    return summary


def _check_poster_cancelled(scan):
    if scan.get("cancel_requested"):
        raise PosterScanCancelled()


def _analyze_candidate(candidate, root):
    background = candidate["background_path"]
    poster = candidate["poster_path"]
    backup = candidate["backup_path"]
    if not all(path_is_under(path, root) for path in (background, poster, backup)):
        return "unsafe", "Artwork path is outside the library"
    if any(os.path.islink(path) for path in (background, poster, backup)):
        return "unsafe", "Symlinked artwork is not eligible"
    poster_matches = _matching_artwork(os.path.dirname(poster), candidate["base"], POSTER_SUFFIX)
    if len(poster_matches) > 1:
        return "ambiguous", "Multiple poster candidates are ambiguous"
    if not os.path.isfile(poster):
        if poster_matches:
            return "ambiguous", "Poster extension does not match the landscape background"
        return "missing", "Matching poster does not exist"
    background_dimensions = _probe_image_dimensions(background)
    poster_dimensions = _probe_image_dimensions(poster)
    if not _file_identity(background) or not background_dimensions:
        return "unreadable", "Background image is unreadable"
    if not background_dimensions["landscape"]:
        return "unreadable", "Background image is not landscape"
    if not _file_identity(poster) or not poster_dimensions:
        return "unreadable", "Poster image is unreadable"
    if poster_dimensions["landscape"]:
        return "already_landscape", "Poster is already landscape"
    backup_matches = _matching_artwork(os.path.dirname(backup), candidate["base"], BACKUP_SUFFIX)
    if len(backup_matches) > 1:
        return "ambiguous", "Multiple poster backup candidates are ambiguous"
    if backup_matches and os.path.realpath(backup) not in backup_matches:
        return "ambiguous", "Poster backup extension does not match the landscape background"
    if os.path.lexists(backup):
        if not os.path.isfile(backup):
            return "unsafe", "Existing backup is not a regular file"
        backup_dimensions = _probe_image_dimensions(backup)
        if not backup_dimensions:
            return "unreadable", "Existing backup is unreadable"
        if backup_dimensions["landscape"]:
            return "unsafe", "Existing backup is landscape"
    return "eligible", "Portrait poster can be replaced safely"


def _poster_counts(items):
    counts = {
        "candidate_count": len(items),
        "eligible_count": 0,
        "already_landscape_count": 0,
        "missing_count": 0,
        "ambiguous_count": 0,
        "unreadable_count": 0,
        "unsafe_count": 0,
    }
    for item in items:
        key = f"{item.get('status')}_count"
        if key in counts:
            counts[key] += 1
    return counts


def public_poster_scan(scan):
    if not scan:
        return None
    public = {
        "id": scan.get("id", ""),
        "path": scan.get("path", ""),
        "status": scan.get("status", ""),
        **task_progress.public_fields(scan),
        "error": scan.get("error", ""),
        "created_at": scan.get("created_at"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "active": scan.get("status") in {"queued", "running", "cancelling"},
        "cancel_requested": bool(scan.get("cancel_requested")),
        "results_page_size": 10,
        "emby_mapping": emby_catalog.public_summary(
            scan.get("emby_mapping"), app_settings.load_settings()
        ),
        **(scan.get("counts") or {}),
    }
    public.update(maintenance_scan_store.public_cache_metadata("posters", scan))
    return public


def _ensure_poster_cache_loaded():
    global _poster_cache_loaded
    if _poster_cache_loaded:
        return
    restored = maintenance_scan_store.restore_scan("posters")
    with _poster_scan_lock:
        if restored and restored.get("id") not in poster_scans:
            poster_scans[restored["id"]] = restored
        _poster_cache_loaded = True


def _prune_poster_analysis_locked():
    terminal = sorted(
        ((scan_id, scan) for scan_id, scan in poster_scans.items() if scan.get("status") in {"success", "failed", "cancelled"}),
        key=lambda item: item[1].get("finished_at") or item[1].get("created_at") or "",
        reverse=True,
    )
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[10:]:
        poster_scans.pop(scan_id, None)
    valid_scan_ids = set(poster_scans)
    for plan_id, plan in list(poster_plans.items()):
        if plan.get("scan_id") not in valid_scan_ids:
            poster_plans.pop(plan_id, None)
    completed_apply = sorted(
        ((apply_id, run) for apply_id, run in poster_apply_runs.items() if run.get("status") not in {"queued", "running"}),
        key=lambda item: item[1].get("finished_at") or item[1].get("created_at") or "",
        reverse=True,
    )
    for apply_id, _run in completed_apply[25:]:
        poster_apply_runs.pop(apply_id, None)


@coordinated_library_operation(
    "Scan landscape posters", kind="scan", href="/maintenance#posters"
)
def _run_poster_scan(scan, lib_root):
    try:
        started = time.time()
        task_progress.update_scan(
            scan,
            "poster_scan",
            1,
            "Analyzing poster artwork",
            status="running",
            _started_ts=started,
            started_at=utc_iso(started),
        )
        root = os.path.realpath(lib_root)
        items = []
        folders = 0
        for base, dirs, files in os.walk(scan["path"], followlinks=False):
            if scan.get("cancel_requested"):
                task_progress.update_scan(
                    scan,
                    "poster_scan",
                    100,
                    "Poster analysis cancelled",
                    status="cancelled",
                    finished_at=utc_iso(),
                )
                return
            dirs[:] = [name for name in dirs if not os.path.islink(os.path.join(base, name))]
            folders += 1
            for candidates in _background_candidate_groups(base, files):
                if len(candidates) > 1:
                    for candidate in candidates:
                        items.append(_analysis_item(candidate, root, "ambiguous", "Multiple landscape backgrounds for this video stem are ambiguous"))
                else:
                    for candidate in candidates:
                        status, message = _analyze_candidate(candidate, root)
                        items.append(_analysis_item(candidate, root, status, message))
            if folders % 50 == 0:
                task_progress.update_scan(
                    scan,
                    "poster_scan",
                    25,
                    f"Analyzed {folders} folders",
                )
        counts = _poster_counts(items)
        emby_mapping = _enrich_poster_items(items, scan)
        if emby_mapping is None:
            return
        task_progress.update_scan(
            scan,
            "poster_scan",
            100,
            f"{counts['eligible_count']} poster updates ready",
            status="success",
            finished_at=utc_iso(),
            items=items,
            counts=counts,
            emby_mapping=emby_mapping,
        )
        persisted = maintenance_scan_store.persist_success("posters", "posters", scan, lib_root)
        if persisted:
            with _poster_scan_lock:
                for candidate in poster_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
        impact_metrics.record_scan(
            scan["id"], "posters", "posters", scan["path"],
            [{"issue_id": f"poster:{item['id']}", "finding_ids": [item["id"]], "label": os.path.basename(item["poster"]), "path": item["poster"]} for item in items if item.get("eligible")],
            timestamp=scan["finished_at"],
        )
    except PosterScanCancelled:
        task_progress.update_scan(scan, "poster_scan", 100, "Poster analysis cancelled", status="cancelled", error="", finished_at=utc_iso())
    except Exception as exc:
        task_progress.update_scan(scan, "poster_scan", 100, "Poster analysis failed", status="failed", error=str(exc), finished_at=utc_iso())


def start_poster_scan(path=None, *, synchronous=False, lib_root=LIB_ROOT):
    _ensure_poster_cache_loaded()
    real_path, err = _normalize_scan_path(path or lib_root, lib_root)
    if err:
        return None, err
    with _poster_scan_lock:
        _prune_poster_analysis_locked()
        active = next((item for item in poster_scans.values() if item.get("status") in {"queued", "running", "cancelling"}), None)
        if active:
            return active, None
        scan_id = _now_id()
        scan = {
            "id": scan_id, "path": real_path, "lib_root": os.path.realpath(lib_root),
            "status": "queued", "created_at": utc_iso(), "started_at": None,
            "finished_at": None, "progress_percent": 0, "progress_label": "Queued",
            "error": "", "cancel_requested": False, "items": [], "counts": {},
        }
        poster_scans[scan_id] = scan
    if synchronous:
        _run_poster_scan(scan, lib_root)
    else:
        threading.Thread(target=_run_poster_scan, args=(scan, lib_root), daemon=True, name=f"vid2gif-poster-analysis-{scan_id}").start()
    return scan, None


def cancel_poster_scan(scan_id=None):
    _ensure_poster_cache_loaded()
    with _poster_scan_lock:
        scan = poster_scans.get(str(scan_id or "")) if scan_id else next((item for item in poster_scans.values() if item.get("status") in {"queued", "running", "cancelling"}), None)
        if not scan:
            return None, "Scan not found"
        if scan.get("status") not in {"success", "failed", "cancelled"}:
            scan["cancel_requested"] = True
            scan["status"] = "cancelling"
            scan["progress_label"] = "Cancelling poster analysis"
    return scan, None


def poster_scan_status(scan_id=None):
    _ensure_poster_cache_loaded()
    with _poster_scan_lock:
        _prune_poster_analysis_locked()
        if scan_id:
            scan = poster_scans.get(str(scan_id))
            if not scan:
                return None, "Scan not found"
        else:
            active = next((item for item in poster_scans.values() if item.get("status") in {"queued", "running", "cancelling"}), None)
            successful = [item for item in poster_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("finished_at") or "") if successful else (max(poster_scans.values(), key=lambda item: item.get("created_at") or "") if poster_scans else None))
    return {"scan": public_poster_scan(scan)}, None


def poster_items_payload(scan_id, offset=0, limit=10, status="all", sort="background", direction="asc"):
    _ensure_poster_cache_loaded()
    try:
        offset = max(0, int(offset or 0)); limit = max(1, min(100, int(limit or 10)))
    except (TypeError, ValueError):
        offset, limit = 0, 10
    with _poster_scan_lock:
        scan = poster_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        items = list(scan.get("items") or [])
    if status != "all":
        items = [item for item in items if item.get("status") == status]
    items, sort, direction = sort_records(
        items, sort, direction,
        {
            "status": lambda item: item.get("status"),
            "background": lambda item: item.get("source"),
            "poster": lambda item: item.get("poster"),
            "detail": lambda item: item.get("message"),
        },
        "background",
    )
    total = len(items); page = items[offset:offset + limit]
    return {
        "scan": public_poster_scan(scan), "offset": offset, "limit": limit,
        "sort": sort, "direction": direction,
        "total": total, "count": len(page), "has_previous": offset > 0,
        "has_next": offset + limit < total, "items": [_public_analysis_item(item) for item in page],
    }, None


def build_poster_plan(payload, lib_root=LIB_ROOT):
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    allowed, error = maintenance_scan_store.action_allowed("posters", scan_id, lib_root)
    if not allowed:
        return None, error
    selected = {str(value) for value in payload.get("item_ids") or [] if str(value)}
    visible = {str(value) for value in payload.get("visible_item_ids") or [] if str(value)}
    if not selected or not visible or not selected.issubset(visible):
        return None, "Selected posters must belong to the visible page"
    with _poster_scan_lock:
        scan = poster_scans.get(scan_id)
        if not scan or scan.get("status") != "success":
            return None, "Poster analysis is not complete"
        by_id = {item.get("id"): item for item in scan.get("items") or []}
        if not visible.issubset(by_id):
            return None, "Visible poster results are stale"
        items = [by_id[item_id] for item_id in selected if by_id[item_id].get("eligible")]
        for item in items:
            candidate = item.get("candidate") or {}
            identities = item.get("identities") or {}
            for key, path_key in (("background", "background_path"), ("poster", "poster_path"), ("backup", "backup_path")):
                path = candidate.get(path_key)
                if _file_identity(path) != identities.get(key):
                    return None, "Artwork changed after analysis. Rescan before applying updates."
    if not items:
        return None, "Select at least one eligible poster"
    plan_id = _now_id()
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "path": scan["path"],
        "created_at": utc_iso(),
        "items": items,
        "file_count": len(items),
        "emby_item_ids": sorted({item.get("emby_item_id") for item in items if item.get("emby_item_id")}),
    }
    with _poster_scan_lock:
        poster_plans[plan_id] = plan
    return {key: plan.get(key) for key in ("id", "scan_id", "path", "created_at", "file_count", "emby_item_ids")}, None


def public_poster_apply(run):
    if not run:
        return None
    return {key: run.get(key) for key in ("id", "plan_id", "status", "created_at", "started_at", "finished_at", "progress_percent", "progress_label", "error", "updated_count", "failed_count", "results", "emby_sync", "emby_notification")}


@coordinated_library_operation(
    "Apply landscape poster maintenance",
    kind="mutation",
    href="/maintenance#posters",
)
def _run_poster_apply(run, plan, lib_root):
    if not _poster_apply_execution_lock.acquire(blocking=False):
        run.update(status="failed", error="Another poster apply is already active", finished_at=utc_iso(), progress_percent=100, progress_label="Poster apply failed")
        return
    results = []
    try:
        run.update(status="running", started_at=utc_iso(), progress_percent=1, progress_label="Applying poster updates")
        for index, item in enumerate(plan.get("items") or [], start=1):
            candidate = item.get("candidate") or {}
            identities = item.get("identities") or {}
            stale = any(
                _file_identity(candidate.get(path_key)) != identities.get(key)
                for key, path_key in (("background", "background_path"), ("poster", "poster_path"), ("backup", "backup_path"))
            )
            if stale:
                status, message = "error", "Artwork changed after review; update skipped"
            else:
                status, message = _process_candidate(candidate, os.path.realpath(lib_root))
            results.append({"id": item["id"], "status": status, "message": message, "poster": item["poster"], "emby_item_id": item.get("emby_item_id", "")})
            run.update(progress_percent=int(100 * index / len(plan["items"])), progress_label=f"Applied {index} of {len(plan['items'])} poster updates")
        updated = sum(1 for item in results if item["status"] == "updated")
        failed = len(results) - updated
        sync_result = None
        if updated:
            run.update(progress_label="Synchronizing poster changes with Emby")
            changed_ids = {result["id"] for result in results if result.get("status") == "updated"}
            sync_result = emby_sync.sync_changes(
                [
                    {
                        "local_path": (item.get("candidate") or {}).get("poster_path") or item.get("poster"),
                        "update_type": "Modified",
                        "emby_item_id": item.get("emby_item_id", ""),
                        "refresh_scope": "image",
                    }
                    for item in plan.get("items") or []
                    if item.get("id") in changed_ids
                ],
                workflow="posters",
                run_id=run["id"],
            )
            impact_metrics.record_maintenance_action(run["id"], "posters", resolutions=[{"issue_id": f"poster:{item['id']}", "stream": "posters", "resolve_all": True, "ensure_issue": True, "label": os.path.basename(item["poster"]), "path": item["poster"]} for item in plan["items"] if any(result["id"] == item["id"] and result["status"] == "updated" for result in results)], operations={"other_files": updated}, timestamp=utc_iso(), label="Landscape poster updates")
        terminal_status = "success" if not failed else "complete_with_issues"
        notification = emby_notifications.notify_maintenance(
            "Poster updates",
            run["id"],
            status=terminal_status,
            attempted_count=len(plan.get("items") or []),
            succeeded_count=updated,
            failed_count=failed,
            emby_sync=sync_result,
        )
        run.update(status=terminal_status, finished_at=utc_iso(), progress_percent=100, progress_label=f"{updated} posters updated", updated_count=updated, failed_count=failed, results=results, emby_sync=sync_result, emby_notification=notification)
    except Exception as exc:
        notification = emby_notifications.notify_maintenance(
            "Poster updates",
            run["id"],
            status="failed",
            attempted_count=len(plan.get("items") or []),
            succeeded_count=sum(1 for item in results if item.get("status") == "updated"),
            failed_count=1,
        )
        run.update(status="failed", error=str(exc), finished_at=utc_iso(), progress_percent=100, progress_label="Poster apply failed", results=results, emby_notification=notification)
    finally:
        _poster_apply_execution_lock.release()


def start_poster_apply(plan_id, *, synchronous=False, lib_root=LIB_ROOT):
    with _poster_scan_lock:
        if any(item.get("status") in {"queued", "running"} for item in poster_apply_runs.values()):
            return None, "Another poster apply is already active"
        plan = poster_plans.get(str(plan_id or ""))
        if not plan:
            return None, "Plan not found"
        run_id = _now_id()
        run = {"id": run_id, "plan_id": plan["id"], "status": "queued", "created_at": utc_iso(), "started_at": None, "finished_at": None, "progress_percent": 0, "progress_label": "Queued", "error": "", "updated_count": 0, "failed_count": 0, "results": []}
        poster_apply_runs[run_id] = run
    def apply_and_refresh():
        _run_poster_apply(run, plan, lib_root)
        try:
            # The rescan deliberately starts after the apply releases the
            # shared library-operation lease, avoiding a nested-gate deadlock.
            start_poster_scan(plan["path"], synchronous=True, lib_root=lib_root)
        except Exception:
            pass

    if synchronous:
        apply_and_refresh()
    else:
        threading.Thread(target=apply_and_refresh, daemon=True, name=f"vid2gif-poster-apply-{run_id}").start()
    return run, None


def poster_apply_status(apply_id=None):
    with _poster_scan_lock:
        run = poster_apply_runs.get(str(apply_id or "")) if apply_id else (max(poster_apply_runs.values(), key=lambda item: item.get("created_at") or "") if poster_apply_runs else None)
    if apply_id and not run:
        return None, "Apply run not found"
    return {"apply": public_poster_apply(run)}, None


def _run_summary(run):
    counters = run.get("counters") or {}
    return {
        "id": run.get("id", ""),
        "status": run.get("status", ""),
        "mode": run.get("mode", ""),
        "path": run.get("path", ""),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "progress_label": run.get("progress_label", ""),
        "error": run.get("error", ""),
        "counters": dict(counters),
        "items": list(run.get("items") or [])[-50:],
        "emby_sync": dict(run.get("emby_sync") or {}),
        "emby_refresh": dict(run.get("emby_sync") or run.get("emby_refresh") or {}),
        "emby_notification": emby_notifications.public_result(run.get("emby_notification")),
    }


def public_run(run):
    return _run_summary(run) if run else None


def _prune_poster_runs_locked():
    current_id = _current_run_id
    terminal = sorted(
        (
            (run_id, run)
            for run_id, run in poster_runs.items()
            if run_id != current_id and run.get("status") in {"success", "failed"}
        ),
        key=lambda item: item[1].get("finished_at") or item[1].get("created_at") or "",
        reverse=True,
    )
    for run_id, _run in terminal[POSTER_RUN_RETENTION_COUNT:]:
        poster_runs.pop(run_id, None)


def _set_run_state(run, **values):
    run.update(values)
    with _run_start_lock:
        poster_runs[run["id"]] = run
        _prune_poster_runs_locked()


def _normalize_scan_path(path, lib_root):
    target = str(path or lib_root or "").strip()
    real = resolve_case_insensitive(target)
    if (
        not real
        or not path_is_under(real, lib_root)
        or not os.path.isdir(real)
        or os.path.islink(real)
    ):
        return None, "Path not found"
    return os.path.realpath(real), None


def _empty_counters():
    return {
        "folders_scanned": 0,
        "folders_skipped_unchanged": 0,
        "candidates": 0,
        "updated": 0,
        "already_matching": 0,
        "missing_poster": 0,
        "skipped": 0,
        "errors": 0,
    }


def _scan_and_apply(run, lib_root, settings):
    manifest = load_manifest()
    folders = manifest.setdefault("folders", {})
    records = manifest.setdefault("records", {})
    counters = _empty_counters()
    items = []
    impact_issues = []
    impact_resolutions = []
    impact_bytes = 0
    sync_candidates = []
    mode = run.get("mode") or "incremental"
    root = os.path.realpath(lib_root)
    scan_path = run["path"]

    for base, dirs, files in os.walk(scan_path, followlinks=False):
        dirs[:] = [d for d in dirs if not os.path.islink(os.path.join(base, d))]
        rel_folder = os.path.normcase(_relative_path(base, root)).replace(os.sep, "/")
        signature = _folder_signature(base, files)
        if (
            mode != "full"
            and signature
            and folders.get(rel_folder, {}).get("signature") == signature
        ):
            counters["folders_skipped_unchanged"] += 1
            continue
        if not signature:
            folders[rel_folder] = {"signature": signature, "checked_at": utc_iso()}
            continue

        counters["folders_scanned"] += 1
        for candidates in _background_candidate_groups(base, files):
            if len(candidates) > 1:
                candidate = candidates[0]
                counters["candidates"] += 1
                counters["skipped"] += 1
                status, message = "skipped", "Multiple landscape backgrounds for this video stem are ambiguous"
                key = _path_key(candidate["background_path"], root)
                records[key] = _record_for(candidate, root, status, message)
                items.append(_public_item(candidate, root, status, message))
                continue
            for candidate in candidates:
                if os.path.islink(candidate["background_path"]):
                    continue
                counters["candidates"] += 1
                status, message = _process_candidate(candidate, root)
                if status == "updated":
                    counters["updated"] += 1
                elif status == "already_matching":
                    counters["already_matching"] += 1
                elif status == "missing_poster":
                    counters["missing_poster"] += 1
                elif status == "skipped":
                    counters["skipped"] += 1
                else:
                    counters["errors"] += 1
                key = _path_key(candidate["background_path"], root)
                records[key] = _record_for(candidate, root, status, message)
                items.append(_public_item(candidate, root, status, message))
                if status in {"updated", "error"}:
                    issue_id = f"poster:{hashlib.sha256(key.encode('utf-8')).hexdigest()[:24]}"
                    impact_issues.append(
                        {
                            "issue_id": issue_id,
                            "finding_ids": [issue_id],
                            "label": os.path.basename(candidate["poster_path"]),
                            "path": candidate["poster_path"],
                        }
                    )
                    if status == "updated":
                        sync_candidates.append(dict(candidate))
                        impact_resolutions.append(
                            {
                                "issue_id": issue_id,
                                "stream": "posters",
                                "resolve_all": True,
                                "ensure_issue": True,
                                "label": os.path.basename(candidate["poster_path"]),
                                "path": candidate["poster_path"],
                            }
                        )
                        try:
                            impact_bytes += os.path.getsize(candidate["poster_path"])
                        except OSError:
                            pass
                if len(items) > POSTER_RUN_ITEM_RETENTION_COUNT:
                    del items[: len(items) - POSTER_RUN_ITEM_RETENTION_COUNT]
        try:
            current_files = os.listdir(base)
        except OSError:
            current_files = files
        folders[rel_folder] = {
            "signature": _folder_signature(base, current_files),
            "checked_at": utc_iso(),
        }

    run["counters"] = counters
    run["items"] = items
    run["_impact_issues"] = impact_issues
    run["_impact_resolutions"] = impact_resolutions
    run["_impact_bytes"] = impact_bytes
    run["_sync_candidates"] = sync_candidates
    if mode == "full":
        manifest["last_full_run_at"] = utc_iso()
    manifest["last_run"] = _run_summary(run)
    save_manifest(manifest)
    return counters


def _public_emby_result(result):
    return emby_client.public_result(result)


def _settings_for_emby_test(updates):
    if not isinstance(updates, dict):
        return None, "Settings are invalid"
    settings = _emby_settings()
    if "emby_url" in updates:
        settings["emby_url"] = str(updates.get("emby_url") or "").strip()
    if updates.get("emby_api_key"):
        settings["emby_api_key"] = str(updates.get("emby_api_key") or "").strip()
    return settings, None


def test_emby_connection(updates=None, opener=None, persist=True):
    settings, err = _settings_for_emby_test(updates or {})
    if err:
        return None, err
    api_key = str(settings.get("emby_api_key") or "")
    data, request_result = emby_client.request_json(
        settings,
        "/System/Info",
        opener=opener,
        timeout=15,
    )
    if request_result.get("error_code") == "missing_config":
        result = emby_client.result(
            "skipped",
            "Emby URL and API key are required to test the connection",
            api_key=api_key,
            error_code="missing_config",
        )
    elif request_result.get("status") == "success":
        if isinstance(data, dict):
            server_name = data.get("ServerName") or data.get("Name") or ""
            version = data.get("Version") or ""
            message = (
                f"Connected to {server_name}"
                if server_name
                else "Connected to Emby"
            )
            result = emby_client.result(
                "success",
                message,
                api_key=api_key,
                http_status=request_result.get("http_status"),
                server_name=server_name,
                version=version,
            )
        else:
            result = emby_client.result(
                "failed",
                "Emby returned an invalid system information response",
                api_key=api_key,
                http_status=request_result.get("http_status"),
                error_code="invalid_response",
            )
    else:
        result = request_result

    if persist:
        _save_emby_status_value("last_test", result)
    return result, None


@coordinated_library_operation(
    "Run landscape poster automation",
    kind="mutation",
    href="/maintenance#posters",
)
def _execute_run(run, lib_root, settings):
    global _current_run_id
    if not _run_execution_lock.acquire(blocking=False):
        _set_run_state(
            run,
            status="failed",
            error="Another landscape poster run is already active",
            finished_at=utc_iso(),
            progress_label="Failed",
        )
        return run
    try:
        _set_run_state(
            run,
            status="running",
            started_at=utc_iso(),
            progress_label="Scanning artwork",
        )
        counters = _scan_and_apply(run, lib_root, settings)
        impact_issues = run.get("_impact_issues") or []
        if run.get("mode") == "full":
            impact_metrics.record_scan(
                run["id"],
                "posters",
                "posters",
                run["path"],
                impact_issues,
                timestamp=utc_iso(),
            )
        else:
            for issue in impact_issues:
                impact_metrics.record_scan(
                    f"{run['id']}:{issue.get('issue_id')}",
                    "posters",
                    "posters",
                    issue.get("path") or run["path"],
                    [issue],
                    timestamp=utc_iso(),
                )
        impact_metrics.record_maintenance_action(
            run["id"],
            "posters",
            resolutions=run.get("_impact_resolutions") or [],
            operations={
                "other_files": counters.get("updated", 0),
                "other_bytes": run.get("_impact_bytes", 0),
            },
            timestamp=utc_iso(),
            label="Landscape poster updates",
        )
        sync_result = None
        if counters.get("updated"):
            _set_run_state(run, progress_label="Synchronizing poster changes with Emby")
            candidates = run.get("_sync_candidates") or []
            emby_settings = _emby_settings(settings)
            changes = [
                {
                    "local_path": candidate.get("poster_path"),
                    "update_type": "Modified",
                    "emby_item_id": "",
                    "refresh_scope": "image",
                }
                for candidate in candidates
            ]
            if (
                emby_settings.get("emby_sync_after_maintenance", True)
                and emby_settings.get("emby_url")
                and emby_settings.get("emby_api_key")
            ):
                catalog, _summary = emby_catalog.load_catalog(emby_settings)
                mappings = emby_settings.get("emby_path_mappings") or []
                for change, candidate in zip(changes, candidates):
                    match = emby_catalog.match_paths(catalog, _poster_video_paths({"candidate": candidate}), mappings)
                    if match.get("emby_match_status") == "unmatched":
                        match = emby_catalog.match_path(catalog, os.path.dirname(candidate.get("poster_path") or ""), mappings)
                    change["emby_item_id"] = match.get("emby_item_id", "")
            sync_result = emby_sync.sync_changes(
                changes,
                workflow="posters",
                run_id=run["id"],
                settings=emby_settings,
            )
        notification = emby_notifications.notify_maintenance(
            "Automatic poster maintenance",
            run["id"],
            status="success" if not counters.get("errors") else "complete_with_issues",
            attempted_count=counters.get("updated", 0) + counters.get("errors", 0),
            succeeded_count=counters.get("updated", 0),
            failed_count=counters.get("errors", 0),
            emby_sync=sync_result,
            settings=emby_settings if counters.get("updated") else None,
        )
        _set_run_state(
            run,
            status="success",
            finished_at=utc_iso(),
            progress_label=(
                f"{counters.get('updated', 0)} updated, "
                f"{counters.get('already_matching', 0)} already matching"
            ),
            emby_sync=sync_result,
            emby_refresh=sync_result or {},
            emby_notification=notification,
        )
        manifest = load_manifest()
        manifest["last_run"] = _run_summary(run)
        if run.get("mode") == "full":
            manifest["last_full_run_at"] = run.get("finished_at")
        save_manifest(manifest)
        return run
    except Exception as exc:
        notification = emby_notifications.notify_maintenance(
            "Automatic poster maintenance",
            run["id"],
            status="failed",
            attempted_count=max(1, int((run.get("counters") or {}).get("candidates") or 0)),
            failed_count=1,
        )
        _set_run_state(
            run,
            status="failed",
            error=str(exc),
            finished_at=utc_iso(),
            progress_label="Failed",
            emby_notification=notification,
        )
        return run
    finally:
        with _run_start_lock:
            _current_run_id = ""
        _run_execution_lock.release()


def start_landscape_poster_run(
    path=None,
    mode="full",
    *,
    synchronous=False,
    lib_root=LIB_ROOT,
    settings=None,
):
    global _current_run_id
    mode = "full" if str(mode or "").lower() == "full" else "incremental"
    real_path, err = _normalize_scan_path(path or lib_root, lib_root)
    if err:
        return None, err
    settings = settings or load_settings()
    with _run_start_lock:
        if _current_run_id:
            return None, "Another landscape poster run is already active"
        run_id = _now_id()
        run = {
            "id": run_id,
            "status": "queued",
            "mode": mode,
            "path": real_path,
            "created_at": utc_iso(),
            "started_at": None,
            "finished_at": None,
            "progress_label": "Queued",
            "error": "",
            "counters": _empty_counters(),
            "items": [],
            "emby_refresh": {},
        }
        poster_runs[run_id] = run
        _current_run_id = run_id

    if synchronous:
        _execute_run(run, lib_root, settings)
    else:
        thread = threading.Thread(
            target=_execute_run,
            args=(run, lib_root, settings),
            daemon=True,
            name=f"vid2gif-landscape-posters-{run_id}",
        )
        thread.start()
    return run, None


def _parse_iso_ts(value):
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _latest_run():
    manifest = load_manifest()
    last_run = manifest.get("last_run")
    with _run_start_lock:
        current = poster_runs.get(_current_run_id) if _current_run_id else None
        memory_latest = (
            max(poster_runs.values(), key=lambda run: run.get("created_at") or "")
            if poster_runs
            else None
        )
    return current, memory_latest or last_run, manifest


def _next_run_timestamp(settings, manifest=None, now=None):
    if not settings.get("enabled"):
        return None
    now = time.time() if now is None else now
    manifest = manifest or load_manifest()
    last_run = manifest.get("last_run") or {}
    last_finished = _parse_iso_ts(last_run.get("finished_at"))
    if last_finished is None:
        return now
    return last_finished + int(settings.get("scan_interval_seconds") or 0)


def emby_status_payload(settings=None, latest_run=None):
    poster_settings = settings or load_settings()
    settings = _emby_settings(poster_settings)
    stored = load_emby_status()
    last_refresh = stored.get("last_refresh")
    if not last_refresh and latest_run:
        last_refresh = latest_run.get("emby_sync") or latest_run.get("emby_refresh") or None
    if isinstance(last_refresh, dict) and last_refresh.get("id"):
        public_last_refresh = dict(last_refresh)
    else:
        public_last_refresh = _public_emby_result(last_refresh)
    return {
        "configured": bool(settings.get("emby_url") and settings.get("emby_api_key")),
        "url_configured": bool(settings.get("emby_url")),
        "api_key_configured": bool(settings.get("emby_api_key")),
        "refresh_enabled": bool(settings.get("emby_sync_after_maintenance", True)),
        "last_test": _public_emby_result(stored.get("last_test")),
        "last_refresh": public_last_refresh,
    }


def status_payload():
    settings = load_settings()
    current, latest, manifest = _latest_run()
    with _run_start_lock:
        _prune_poster_runs_locked()
    next_ts = _next_run_timestamp(settings, manifest)
    _scheduler_state["next_run_at"] = utc_iso(next_ts) if next_ts else None
    scan_payload, _ = poster_scan_status()
    apply_payload, _ = poster_apply_status()
    return {
        "settings": public_settings(settings),
        "current_run": _run_summary(current) if current else None,
        "last_run": _run_summary(latest) if latest else None,
        "worker_started": _worker_started,
        "scheduler": dict(_scheduler_state),
        "emby_status": emby_status_payload(settings=settings, latest_run=latest),
        "manifest_path": MANIFEST_PATH,
        "analysis_scan": (scan_payload or {}).get("scan"),
        "analysis_apply": (apply_payload or {}).get("apply"),
    }


def _auto_mode(manifest, settings, now):
    last_full = _parse_iso_ts(manifest.get("last_full_run_at"))
    if last_full is None or now - last_full >= settings["full_scan_interval_seconds"]:
        return "full"
    return "incremental"


def worker():
    while True:
        try:
            settings = load_settings()
            manifest = load_manifest()
            now = time.time()
            _scheduler_state["last_checked_at"] = utc_iso(now)
            next_ts = _next_run_timestamp(settings, manifest, now=now)
            _scheduler_state["next_run_at"] = utc_iso(next_ts) if next_ts else None
            if settings.get("enabled") and next_ts is not None and now >= next_ts:
                mode = _auto_mode(manifest, settings, now)
                run, err = start_landscape_poster_run(
                    LIB_ROOT,
                    mode=mode,
                    synchronous=True,
                    settings=settings,
                )
                _scheduler_state["last_error"] = err or ""
                if run:
                    _scheduler_state["last_run_id"] = run.get("id")
        except Exception as exc:
            _scheduler_state["last_error"] = str(exc)
        wait_seconds = max(5, min(60, int(load_settings().get("scan_interval_seconds") or 60)))
        _wake_event.wait(wait_seconds)
        _wake_event.clear()


def start_landscape_poster_worker():
    global _worker_started
    with _worker_start_lock:
        if _worker_started:
            return
        threading.Thread(
            target=worker,
            daemon=True,
            name="vid2gif-landscape-poster-worker",
        ).start()
        _worker_started = True
