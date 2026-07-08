import datetime
import hashlib
import json
import os
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .config import (
    LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS,
    LANDSCAPE_POSTER_INTERVAL_SECONDS,
    LANDSCAPE_POSTER_ROOT,
    LIB_ROOT,
)
from .progress import format_duration, utc_iso
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
__test__ = False

_settings_lock = threading.Lock()
_manifest_lock = threading.Lock()
_emby_status_lock = threading.Lock()
_run_start_lock = threading.Lock()
_run_execution_lock = threading.Lock()
_worker_start_lock = threading.Lock()
_worker_started = False
_wake_event = threading.Event()

poster_runs = {}
_current_run_id = ""
_scheduler_state = {
    "last_checked_at": None,
    "next_run_at": None,
    "last_error": "",
}


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
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "size": stat.st_size,
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
    }


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
        "emby_refresh_enabled": _env_truthy("EMBY_REFRESH_ENABLED", False),
        "emby_url": _env_str("EMBY_URL"),
        "emby_api_key": _env_str("EMBY_API_KEY"),
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
        "emby_refresh_enabled": bool(
            data.get("emby_refresh_enabled", base["emby_refresh_enabled"])
        ),
        "emby_url": str(data.get("emby_url", base["emby_url"]) or "").strip(),
        "emby_api_key": str(data.get("emby_api_key", base["emby_api_key"]) or "").strip(),
    }


def load_settings(path=None):
    path = path or SETTINGS_PATH
    data = _read_json(path, {})
    if not data or data.get("schema_version") != SETTINGS_SCHEMA_VERSION:
        return default_settings()
    return _coerce_settings(data)


def save_settings(settings, path=None):
    path = path or SETTINGS_PATH
    settings = _coerce_settings(settings)
    with _settings_lock:
        return _write_json_atomic(path, settings)


def update_settings(updates, path=None):
    if not isinstance(updates, dict):
        return None, "Settings are invalid"
    current = load_settings(path)
    merged = dict(current)
    for key in (
        "enabled",
        "scan_interval_seconds",
        "full_scan_interval_seconds",
        "emby_refresh_enabled",
        "emby_url",
    ):
        if key in updates:
            merged[key] = updates[key]
    if "emby_api_key" in updates:
        merged["emby_api_key"] = updates.get("emby_api_key") or ""
    settings = _coerce_settings(merged, base=current)
    if settings["emby_refresh_enabled"] and (
        not settings["emby_url"] or not settings["emby_api_key"]
    ):
        return None, "Emby URL and API key are required when refresh is enabled"
    if not save_settings(settings, path):
        return None, "Settings could not be saved"
    _wake_event.set()
    return public_settings(settings), None


def public_settings(settings=None):
    settings = settings or load_settings()
    return {
        "enabled": bool(settings.get("enabled")),
        "scan_interval_seconds": settings.get("scan_interval_seconds"),
        "scan_interval_label": format_duration(settings.get("scan_interval_seconds")),
        "full_scan_interval_seconds": settings.get("full_scan_interval_seconds"),
        "full_scan_interval_label": format_duration(
            settings.get("full_scan_interval_seconds")
        ),
        "emby_refresh_enabled": bool(settings.get("emby_refresh_enabled")),
        "emby_url": settings.get("emby_url", ""),
        "emby_api_key_configured": bool(settings.get("emby_api_key")),
    }


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


def _copy_file_atomic(source, target):
    tmp_path = f"{target}.{os.getpid()}.tmp"
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        shutil.copy2(source, tmp_path)
        os.replace(tmp_path, target)
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


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
    if not path_is_under(background, root) or not path_is_under(poster, root):
        return "error", "Path is outside the library"
    if os.path.islink(background) or os.path.islink(poster) or os.path.islink(backup):
        return "error", "Symlinked artwork is not modified"
    if not os.path.isfile(poster):
        return "missing_poster", "Matching poster does not exist"
    if _same_file_bytes(background, poster):
        return "already_matching", "Poster already matches background"
    try:
        if not os.path.exists(backup):
            shutil.copy2(poster, backup)
            backup_created = True
        else:
            backup_created = False
        _copy_file_atomic(background, poster)
    except Exception as exc:
        return "error", str(exc)
    return (
        "updated",
        "Poster replaced; backup created" if backup_created else "Poster replaced",
    )


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
        "emby_refresh": dict(run.get("emby_refresh") or {}),
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
        "errors": 0,
    }


def _scan_and_apply(run, lib_root, settings):
    manifest = load_manifest()
    folders = manifest.setdefault("folders", {})
    records = manifest.setdefault("records", {})
    counters = _empty_counters()
    items = []
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
        for name in sorted(files, key=str.lower):
            candidate = _candidate_from_background(os.path.join(base, name))
            if not candidate:
                continue
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
            else:
                counters["errors"] += 1
            key = _path_key(candidate["background_path"], root)
            records[key] = _record_for(candidate, root, status, message)
            items.append(_public_item(candidate, root, status, message))
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
    if mode == "full":
        manifest["last_full_run_at"] = utc_iso()
    manifest["last_run"] = _run_summary(run)
    save_manifest(manifest)
    return counters


def _sanitize_secret_text(value, api_key=""):
    text = str(value or "")
    secret = str(api_key or "")
    if not secret:
        return text
    encoded = urllib.parse.quote_plus(secret)
    return text.replace(secret, "[redacted]").replace(encoded, "[redacted]")


def _public_emby_result(result):
    result = result or {}
    return {
        "status": str(result.get("status") or ""),
        "message": str(result.get("message") or ""),
        "checked_at": result.get("checked_at"),
        "http_status": result.get("http_status"),
        "server_name": str(result.get("server_name") or ""),
        "version": str(result.get("version") or ""),
    }


def _emby_result(
    status,
    message,
    *,
    api_key="",
    http_status=None,
    server_name="",
    version="",
):
    return _public_emby_result(
        {
            "status": status,
            "message": _sanitize_secret_text(message, api_key),
            "checked_at": utc_iso(),
            "http_status": http_status,
            "server_name": server_name,
            "version": version,
        }
    )


def _emby_endpoint(settings, api_path):
    base = str(settings.get("emby_url") or "").strip().rstrip("/")
    api_key = str(settings.get("emby_api_key") or "").strip()
    if not base or not api_key:
        return ""
    api_path = "/" + str(api_path or "").strip("/")
    if base.lower().endswith("/emby"):
        url = f"{base}{api_path}"
    else:
        url = f"{base}/emby{api_path}"
    return f"{url}?{urllib.parse.urlencode({'api_key': api_key})}"


def _emby_refresh_endpoint(settings):
    return _emby_endpoint(settings, "/Library/Refresh")


def _read_response_json(response):
    if not hasattr(response, "read"):
        return {}
    raw = response.read()
    if not raw:
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _settings_for_emby_test(updates):
    if not isinstance(updates, dict):
        return None, "Settings are invalid"
    settings = load_settings()
    if "emby_url" in updates:
        settings["emby_url"] = str(updates.get("emby_url") or "").strip()
    if updates.get("emby_api_key"):
        settings["emby_api_key"] = str(updates.get("emby_api_key") or "").strip()
    return _coerce_settings(settings), None


def test_emby_connection(updates=None, opener=None, persist=True):
    settings, err = _settings_for_emby_test(updates or {})
    if err:
        return None, err
    api_key = str(settings.get("emby_api_key") or "")
    endpoint = _emby_endpoint(settings, "/System/Info")
    if not endpoint:
        result = _emby_result(
            "skipped",
            "Emby URL and API key are required to test the connection",
            api_key=api_key,
        )
        if persist:
            _save_emby_status_value("last_test", result)
        return result, None

    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(endpoint, method="GET", headers={"accept": "application/json"})
    try:
        with opener(request, timeout=15) as response:
            code = getattr(response, "status", None) or getattr(response, "code", 0)
            data = _read_response_json(response)
    except urllib.error.HTTPError as exc:
        result = _emby_result(
            "failed",
            f"Emby rejected the request ({getattr(exc, 'code', 'unknown')})",
            api_key=api_key,
            http_status=getattr(exc, "code", None),
        )
    except Exception as exc:
        result = _emby_result(
            "failed",
            f"Emby connection failed: {_sanitize_secret_text(exc, api_key)}",
            api_key=api_key,
        )
    else:
        server_name = data.get("ServerName") or data.get("Name") or ""
        version = data.get("Version") or ""
        message = (
            f"Connected to {server_name}"
            if server_name
            else "Connected to Emby"
        )
        result = _emby_result(
            "success",
            message,
            api_key=api_key,
            http_status=code,
            server_name=server_name,
            version=version,
        )

    if persist:
        _save_emby_status_value("last_test", result)
    return result, None


def refresh_emby(settings, opener=None, persist=False):
    if not settings.get("emby_refresh_enabled"):
        result = _emby_result("disabled", "Emby refresh is disabled")
        if persist:
            _save_emby_status_value("last_refresh", result)
        return result
    endpoint = _emby_refresh_endpoint(settings)
    api_key = str(settings.get("emby_api_key") or "")
    if not endpoint:
        result = _emby_result(
            "skipped",
            "Emby refresh is not configured",
            api_key=api_key,
        )
        if persist:
            _save_emby_status_value("last_refresh", result)
        return result
    opener = opener or urllib.request.urlopen
    request = urllib.request.Request(
        endpoint,
        data=b"",
        method="POST",
        headers={"accept": "*/*"},
    )
    try:
        with opener(request, timeout=15) as response:
            code = getattr(response, "status", None) or getattr(response, "code", 0)
    except urllib.error.HTTPError as exc:
        result = _emby_result(
            "failed",
            f"Emby refresh rejected ({getattr(exc, 'code', 'unknown')})",
            api_key=api_key,
            http_status=getattr(exc, "code", None),
        )
    except Exception as exc:
        result = _emby_result(
            "failed",
            f"Emby refresh failed: {_sanitize_secret_text(exc, api_key)}",
            api_key=api_key,
        )
    else:
        result = _emby_result(
            "success",
            f"Emby refresh requested ({code})",
            api_key=api_key,
            http_status=code,
        )
    if persist:
        _save_emby_status_value("last_refresh", result)
    return result


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
        emby_result = {"status": "skipped", "message": "No poster changes"}
        if counters.get("updated"):
            emby_result = refresh_emby(settings, persist=True)
        _set_run_state(
            run,
            status="success",
            finished_at=utc_iso(),
            progress_label=(
                f"{counters.get('updated', 0)} updated, "
                f"{counters.get('already_matching', 0)} already matching"
            ),
            emby_refresh=emby_result,
        )
        manifest = load_manifest()
        manifest["last_run"] = _run_summary(run)
        if run.get("mode") == "full":
            manifest["last_full_run_at"] = run.get("finished_at")
        save_manifest(manifest)
        return run
    except Exception as exc:
        _set_run_state(
            run,
            status="failed",
            error=str(exc),
            finished_at=utc_iso(),
            progress_label="Failed",
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
    settings = settings or load_settings()
    stored = load_emby_status()
    last_refresh = stored.get("last_refresh")
    if not last_refresh and latest_run:
        last_refresh = (latest_run.get("emby_refresh") or None)
    return {
        "configured": bool(settings.get("emby_url") and settings.get("emby_api_key")),
        "url_configured": bool(settings.get("emby_url")),
        "api_key_configured": bool(settings.get("emby_api_key")),
        "refresh_enabled": bool(settings.get("emby_refresh_enabled")),
        "last_test": _public_emby_result(stored.get("last_test")),
        "last_refresh": _public_emby_result(last_refresh),
    }


def status_payload():
    settings = load_settings()
    current, latest, manifest = _latest_run()
    with _run_start_lock:
        _prune_poster_runs_locked()
    next_ts = _next_run_timestamp(settings, manifest)
    _scheduler_state["next_run_at"] = utc_iso(next_ts) if next_ts else None
    return {
        "settings": public_settings(settings),
        "current_run": _run_summary(current) if current else None,
        "last_run": _run_summary(latest) if latest else None,
        "worker_started": _worker_started,
        "scheduler": dict(_scheduler_state),
        "emby_status": emby_status_payload(settings=settings, latest_run=latest),
        "manifest_path": MANIFEST_PATH,
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
