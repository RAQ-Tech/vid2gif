import datetime
import hashlib
import json
import mimetypes
import os
import re
import threading
import time
import urllib.parse

from . import emby_client
from . import impact_metrics
from . import maintenance_scan_store
from . import poster_maintenance
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .progress import format_size, utc_iso
from .table_sort import sort_records
from .utils import path_is_under, resolve_case_insensitive


ACTOR_IMAGE_ROOT = os.path.join(STATE_ROOT, "actor-images")
EXCEPTIONS_PATH = os.path.join(ACTOR_IMAGE_ROOT, "exceptions.json")
LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "actor-images")
LOG_INDEX = os.path.join(LOG_DIR, "index.json")
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}
ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
LARGE_RESULT_COUNT = 100
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
SCAN_RETENTION_COUNT = 10
SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
APPLY_RETENTION_COUNT = 10
APPLY_MAX_AGE_SECONDS = 24 * 60 * 60
LOG_RETENTION_COUNT = 25
LOG_MAX_BYTES = 1024 * 1024
EMBY_PAGE_SIZE = 500
__test__ = False

actor_scans = {}
_actor_cache_loaded = False
actor_plans = {}
actor_apply_runs = {}
actor_lock = threading.Lock()


class ScanCancelled(Exception):
    pass


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _path_id(path, lib_root=LIB_ROOT):
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(lib_root))
    except (OSError, ValueError):
        rel = os.path.basename(path)
    return _hash_text(os.path.normcase(rel).replace(os.sep, "/"))[:20]


def _person_key(person_id="", name=""):
    person_id = str(person_id or "").strip()
    if person_id:
        return f"id:{person_id}"
    return f"name:{normalize_actor_name(name)}"


def _relative_path(path, root):
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return os.path.basename(path)


def normalize_actor_name(value):
    text = str(value or "").lower()
    text = re.sub(r"[_\-\.]+", " ", text)
    text = re.sub(r"[^a-z0-9 ]+", "", text)
    return re.sub(r"\s+", " ", text).strip()


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


def _file_identity(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "size": stat.st_size,
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
    }


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


def _settings():
    return poster_maintenance.load_settings()


def _fetch_paged(settings, api_path, params, *, opener=None, scan=None):
    return emby_client.request_paged_json(
        settings,
        api_path,
        params=params,
        page_size=EMBY_PAGE_SIZE,
        opener=opener,
        timeout=30,
        before_page=lambda: _check_cancelled(scan),
    )


def _has_primary_image(person):
    tags = person.get("ImageTags") if isinstance(person, dict) else {}
    if isinstance(tags, dict) and tags.get("Primary"):
        return True
    return bool(person.get("PrimaryImageTag")) if isinstance(person, dict) else False


def _public_person(person):
    return {
        "id": str(person.get("Id") or person.get("id") or ""),
        "name": str(person.get("Name") or person.get("name") or ""),
        "provider_ids": dict(person.get("ProviderIds") or {}),
        "has_primary_image": _has_primary_image(person),
    }


def _person_matches(person_ref, missing_by_id, missing_by_name):
    ref_id = str(person_ref.get("Id") or person_ref.get("id") or "")
    ref_name = str(person_ref.get("Name") or person_ref.get("name") or "")
    if ref_id and ref_id in missing_by_id:
        return missing_by_id[ref_id]
    normalized = normalize_actor_name(ref_name)
    if normalized and normalized in missing_by_name:
        return missing_by_name[normalized]
    return None


def _local_item_path(item, scan_path, lib_root):
    raw = str(item.get("Path") or item.get("path") or "").strip()
    if not raw:
        return ""
    real = resolve_case_insensitive(raw)
    if not real:
        return ""
    real = os.path.realpath(real)
    if not path_is_under(real, lib_root) or not path_is_under(real, scan_path):
        return ""
    if os.path.islink(real):
        return ""
    return real


def _image_stat_payload(path, lib_root):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "id": _path_id(path, lib_root),
        "path": os.path.realpath(path),
        "relative_path": _relative_path(path, lib_root),
        "name": os.path.basename(path),
        "size_bytes": stat.st_size,
        "size_label": format_size(stat.st_size),
        "modified_at": utc_iso(stat.st_mtime),
        "preview_url": f"/api/maintenance/actor-images/preview?path={urllib.parse.quote(os.path.realpath(path))}",
        "identity": _file_identity(path),
    }


def _candidate_actor_name(entry, video_stem=""):
    stem, ext = os.path.splitext(entry)
    if ext.lower() not in IMAGE_EXTS:
        return ""
    working = stem
    lower_stem = video_stem.lower()
    if video_stem and working.lower().startswith(lower_stem):
        working = working[len(video_stem):].lstrip(" -_.")
    lower = working.lower()
    for token in ("performer", "actor"):
        marker = f"{token}-"
        marker_alt = f"{token}_"
        for needle in (marker, marker_alt, f"{token} "):
            if needle in lower:
                index = lower.index(needle) + len(needle)
                name = working[index:]
                name = re.sub(r"[-_. ]*image$", "", name, flags=re.IGNORECASE).strip(" -_.")
                return name
    if lower.endswith("-image") or lower.endswith("_image") or lower.endswith(" image"):
        working = re.sub(r"[-_. ]*image$", "", working, flags=re.IGNORECASE)
    return working.strip(" -_.")


def find_actor_image_candidates(actor_name, video_paths, lib_root=LIB_ROOT):
    target = normalize_actor_name(actor_name)
    candidates = {}
    for video_path in video_paths:
        if not video_path:
            continue
        folder = os.path.dirname(video_path) if os.path.isfile(video_path) else video_path
        video_stem = os.path.splitext(os.path.basename(video_path))[0] if os.path.isfile(video_path) else ""
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            ext = os.path.splitext(entry)[1].lower()
            if ext not in IMAGE_EXTS:
                continue
            path = os.path.realpath(os.path.join(folder, entry))
            if path in candidates or os.path.islink(path) or not os.path.isfile(path):
                continue
            if not path_is_under(path, lib_root):
                continue
            candidate_name = _candidate_actor_name(entry, video_stem)
            if normalize_actor_name(candidate_name) != target:
                continue
            payload = _image_stat_payload(path, lib_root)
            if payload:
                payload["match_name"] = candidate_name
                candidates[path] = payload
    return sorted(candidates.values(), key=lambda item: item["relative_path"].lower())


def _exception_public(item):
    return {
        "status": str(item.get("status") or ""),
        "note": str(item.get("note") or ""),
        "candidate_path": str(item.get("candidate_path") or ""),
        "updated_at": item.get("updated_at"),
    }


def load_exceptions():
    data = _read_json(EXCEPTIONS_PATH, {"exceptions": {}})
    exceptions = data.get("exceptions")
    return exceptions if isinstance(exceptions, dict) else {}


def save_exceptions(exceptions):
    _write_json(EXCEPTIONS_PATH, {"exceptions": exceptions or {}})


def _apply_exception(item, exceptions):
    key = _person_key(item.get("person_id"), item.get("name"))
    exception = exceptions.get(key)
    if not exception:
        item["exception"] = None
        return item
    public = _exception_public(exception)
    item["exception"] = public
    if public["status"] in {"ignored", "manual", "blocked"}:
        item["status"] = public["status"]
        item["status_label"] = {
            "ignored": "Ignored",
            "manual": "Manual search needed",
            "blocked": "Do not import",
        }.get(public["status"], public["status"])
    return item


def update_exception(payload):
    if not isinstance(payload, dict):
        return None, "Exception payload is invalid"
    person_id = str(payload.get("person_id") or "").strip()
    name = str(payload.get("name") or "").strip()
    if not person_id and not name:
        return None, "Actor is required"
    status = str(payload.get("status") or "").strip().lower()
    if status not in {"ignored", "manual", "blocked", "clear"}:
        return None, "Exception status is invalid"
    key = _person_key(person_id, name)
    exceptions = load_exceptions()
    if status == "clear":
        exceptions.pop(key, None)
    else:
        exceptions[key] = {
            "person_id": person_id,
            "name": name,
            "status": status,
            "note": str(payload.get("note") or "").strip(),
            "candidate_path": str(payload.get("candidate_path") or "").strip(),
            "updated_at": utc_iso(),
        }
    save_exceptions(exceptions)
    with actor_lock:
        for scan in actor_scans.values():
            for item in scan.get("items") or []:
                if _person_key(item.get("person_id"), item.get("name")) == key:
                    if status == "clear":
                        item["exception"] = None
                        _set_item_candidate_status(item)
                    else:
                        _apply_exception(item, exceptions)
            scan["counts"] = _counts(scan.get("items") or [])
    return {"key": key, "exception": _exception_public(exceptions.get(key, {})) if status != "clear" else None}, None


def _scan_cancel_requested(scan):
    if not scan:
        return False
    with actor_lock:
        return bool(scan.get("cancel_requested"))


def _check_cancelled(scan):
    if scan and _scan_cancel_requested(scan):
        raise ScanCancelled()


def _set_scan_progress(scan, percent, label, **values):
    with actor_lock:
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


def _set_item_candidate_status(item):
    candidates = item.get("candidates") or []
    if len(candidates) == 1:
        item["status"] = "ready"
        item["status_label"] = "Ready to import"
        item["recommended_candidate"] = candidates[0]
    elif len(candidates) > 1:
        item["status"] = "ambiguous"
        item["status_label"] = "Multiple local images"
        item["recommended_candidate"] = None
    else:
        item["status"] = "no_candidate"
        item["status_label"] = "No local image"
        item["recommended_candidate"] = None


def _counts(items):
    counts = {
        "missing_actor_count": len(items),
        "ready_count": 0,
        "ambiguous_count": 0,
        "no_candidate_count": 0,
        "ignored_count": 0,
        "manual_count": 0,
        "blocked_count": 0,
        "imported_count": 0,
        "failed_count": 0,
        "candidate_count": 0,
        "unresolved_count": 0,
    }
    for item in items:
        status = item.get("status")
        counts["candidate_count"] += len(item.get("candidates") or [])
        key = f"{status}_count"
        if key in counts:
            counts[key] += 1
        if status in {"ambiguous", "no_candidate", "manual", "blocked", "failed"}:
            counts["unresolved_count"] += 1
    return counts


def _public_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    total = counts.get("missing_actor_count", 0)
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
        "checked_person_count": scan.get("checked_person_count", 0),
        "checked_item_count": scan.get("checked_item_count", 0),
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": total >= LARGE_RESULT_COUNT,
        "recent_logs": list_recent_logs(),
        **counts,
    }


def public_scan(scan):
    public = _public_scan(scan)
    if public:
        public.update(maintenance_scan_store.public_cache_metadata("actor_images", scan))
    return public


def _ensure_cache_loaded():
    global _actor_cache_loaded
    if _actor_cache_loaded:
        return
    restored = maintenance_scan_store.restore_scan("actor_images")
    with actor_lock:
        if restored and restored.get("id") not in actor_scans:
            actor_scans[restored["id"]] = restored
        _actor_cache_loaded = True


def _public_item(item):
    public = {
        key: item.get(key)
        for key in (
            "id",
            "person_id",
            "name",
            "status",
            "status_label",
            "has_primary_image",
            "related_video_count",
            "related_videos",
            "candidates",
            "recommended_candidate",
            "exception",
            "provider_ids",
        )
    }
    return public


def _build_items(settings, scan, lib_root, opener=None):
    people, people_result = _fetch_paged(
        settings,
        "/Persons",
        {"Fields": "ImageTags,ProviderIds"},
        opener=opener,
        scan=scan,
    )
    if people_result.get("status") != "success":
        raise RuntimeError(people_result.get("message") or "Emby people scan failed")
    missing_people = [_public_person(person) for person in people if not _has_primary_image(person)]
    missing_by_id = {person["id"]: person for person in missing_people if person.get("id")}
    missing_by_name = {
        normalize_actor_name(person["name"]): person
        for person in missing_people
        if normalize_actor_name(person.get("name"))
    }
    _set_scan_progress(
        scan,
        20,
        f"Found {len(missing_people)} people missing images",
        checked_person_count=len(people),
    )
    media_items, item_result = _fetch_paged(
        settings,
        "/Items",
        {
            "Recursive": "true",
            "IncludeItemTypes": "Movie,Episode,Video",
            "Fields": "People,Path,ProviderIds",
        },
        opener=opener,
        scan=scan,
    )
    if item_result.get("status") != "success":
        raise RuntimeError(item_result.get("message") or "Emby media item scan failed")

    related = {}
    scan_path = scan["path"]
    for index, media in enumerate(media_items, start=1):
        _check_cancelled(scan)
        local_path = _local_item_path(media, scan_path, lib_root)
        if not local_path:
            continue
        for person_ref in media.get("People") or []:
            if str(person_ref.get("Type") or "").lower() not in {"", "actor", "person"}:
                continue
            person = _person_matches(person_ref, missing_by_id, missing_by_name)
            if not person:
                continue
            key = _person_key(person.get("id"), person.get("name"))
            related.setdefault(key, {"person": person, "videos": []})
            related[key]["videos"].append(
                {
                    "item_id": str(media.get("Id") or ""),
                    "name": str(media.get("Name") or os.path.basename(local_path)),
                    "path": local_path,
                    "relative_path": _relative_path(local_path, lib_root),
                }
            )
        if index % 50 == 0:
            _set_scan_progress(
                scan,
                min(85, 20 + index // 10),
                f"Checked {index} Emby media items",
                checked_item_count=index,
            )

    exceptions = load_exceptions()
    items = []
    for key, entry in related.items():
        _check_cancelled(scan)
        person = entry["person"]
        video_paths = [video["path"] for video in entry.get("videos") or []]
        candidates = find_actor_image_candidates(person.get("name"), video_paths, lib_root=lib_root)
        item = {
            "id": _hash_text(key)[:20],
            "person_id": person.get("id", ""),
            "name": person.get("name", ""),
            "provider_ids": person.get("provider_ids") or {},
            "has_primary_image": False,
            "related_video_count": len(entry.get("videos") or []),
            "related_videos": sorted(entry.get("videos") or [], key=lambda value: value["relative_path"].lower())[:10],
            "candidates": candidates,
        }
        _set_item_candidate_status(item)
        _apply_exception(item, exceptions)
        items.append(item)
    items.sort(key=lambda item: normalize_actor_name(item.get("name")))
    return items, len(people), len(media_items)


def _run_scan(scan, lib_root, opener=None):
    try:
        started = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning Emby actor images",
            status="running",
            _started_ts=started,
            started_at=utc_iso(started),
        )
        settings = _settings()
        if not settings.get("emby_url") or not settings.get("emby_api_key"):
            raise RuntimeError("Emby URL and API key are required")
        items, checked_people, checked_media = _build_items(settings, scan, lib_root, opener=opener)
        counts = _counts(items)
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            f"{counts['missing_actor_count']} missing actor images, {counts['ready_count']} ready",
            status="success",
            items=items,
            counts=counts,
            checked_person_count=checked_people,
            checked_item_count=checked_media,
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        impact_metrics.record_scan(
            scan["id"],
            "actor_images",
            "actor_images",
            scan["path"],
            [
                {
                    "issue_id": f"actor-image:{item.get('id')}",
                    "finding_ids": [item.get("id")],
                    "label": item.get("name") or "Missing actor image",
                    "path": scan["path"],
                }
                for item in items
                if item.get("status") == "ready"
            ],
            timestamp=utc_iso(finished),
        )
        _write_log(
            "scan",
            {
                "scan_id": scan["id"],
                "path": scan["path"],
                "counts": counts,
                "checked_person_count": checked_people,
                "checked_item_count": checked_media,
            },
        )
        persisted = maintenance_scan_store.persist_success(
            "actor_images", "actor_images", scan, lib_root
        )
        if persisted:
            with actor_lock:
                for candidate in actor_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
    except ScanCancelled:
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            "Actor image scan cancelled",
            status="cancelled",
            error="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log("scan", {"scan_id": scan["id"], "path": scan["path"], "status": "cancelled"})
    except Exception as exc:
        finished = time.time()
        message = emby_client.sanitize_secret_text(exc, (_settings() or {}).get("emby_api_key", ""))
        _set_scan_progress(
            scan,
            100,
            "Actor image scan failed",
            status="failed",
            error=message,
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        _write_log("scan", {"scan_id": scan["id"], "path": scan["path"], "status": "failed", "error": message})


def _prune_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(actor_scans):
        scan = actor_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if not scan.get("_persisted_latest") and now - finished > SCAN_MAX_AGE_SECONDS:
            actor_scans.pop(scan_id, None)
    terminal = sorted(
        (
            (scan_id, scan)
            for scan_id, scan in actor_scans.items()
            if scan.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[SCAN_RETENTION_COUNT:]:
        actor_scans.pop(scan_id, None)


def _active_scan_locked():
    active = [scan for scan in actor_scans.values() if scan.get("status") in SCAN_ACTIVE_STATUSES]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


def start_scan(path, lib_root=LIB_ROOT, synchronous=False, opener=None):
    _ensure_cache_loaded()
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
        "checked_person_count": 0,
        "checked_item_count": 0,
        "items": [],
        "counts": {},
        "lib_root": os.path.realpath(lib_root),
    }
    with actor_lock:
        _prune_scans_locked()
        active = _active_scan_locked()
        if active:
            return active, None
        actor_scans[scan_id] = scan
    if synchronous:
        _run_scan(scan, lib_root, opener=opener)
    else:
        threading.Thread(
            target=_run_scan,
            args=(scan, lib_root, opener),
            daemon=True,
            name=f"vid2gif-actor-image-scan-{scan_id}",
        ).start()
    return scan, None


def cancel_scan(scan_id=None):
    target_id = str(scan_id or "")
    now = time.time()
    with actor_lock:
        _prune_scans_locked(now)
        scan = actor_scans.get(target_id) if target_id else _active_scan_locked()
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
                    "progress_label": "Actor image scan cancelled",
                    "_finished_ts": now,
                    "finished_at": utc_iso(now),
                }
            )
        else:
            scan.update({"status": "cancelling", "progress_label": "Cancelling actor image scan"})
    return scan, None


def status_payload(scan_id=None):
    _ensure_cache_loaded()
    with actor_lock:
        _prune_scans_locked()
        if scan_id:
            scan = actor_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif actor_scans:
            active = _active_scan_locked()
            successful = [item for item in actor_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("_finished_ts") or 0) if successful else max(actor_scans.values(), key=lambda item: item.get("_created_ts") or 0))
        else:
            scan = None
    return {
        "scan": public_scan(scan),
        "emby_status": poster_maintenance.emby_status_payload(settings=_settings()),
    }, None


def items_payload(scan_id, status="all", offset=0, limit=ITEM_PAGE_DEFAULT, sort="actor", direction="asc"):
    _ensure_cache_loaded()
    offset, limit = _coerce_page(offset, limit)
    status = str(status or "all").lower()
    allowed = {"all", "ready", "ambiguous", "no_candidate", "ignored", "manual", "blocked", "imported", "failed", "unresolved"}
    if status not in allowed:
        status = "all"
    with actor_lock:
        _prune_scans_locked()
        scan = actor_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        items = list(scan.get("items") or [])
    if status == "unresolved":
        items = [item for item in items if item.get("status") in {"ambiguous", "no_candidate", "manual", "blocked", "failed"}]
    elif status != "all":
        items = [item for item in items if item.get("status") == status]
    items, sort, direction = sort_records(
        items, sort, direction,
        {
            "status": lambda item: item.get("status"),
            "actor": lambda item: item.get("name"),
            "candidate": lambda item: (item.get("recommended_candidate") or {}).get("relative_path"),
            "video": lambda item: ((item.get("related_videos") or [{}])[0]).get("relative_path"),
            "exception": lambda item: item.get("exception"),
        },
        "actor",
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
        "items": [_public_item(item) for item in page],
    }, None


def _find_scan_item(scan, item_id):
    for item in scan.get("items") or []:
        if item.get("id") == item_id:
            return item
    return None


def _candidate_by_id(item, candidate_id=""):
    candidates = item.get("candidates") or []
    if candidate_id:
        return next((candidate for candidate in candidates if candidate.get("id") == candidate_id), None)
    return item.get("recommended_candidate")


def build_import_plan(payload, lib_root=LIB_ROOT):
    _ensure_cache_loaded()
    if not isinstance(payload, dict):
        return None, "Plan payload is invalid"
    scan_id = str(payload.get("scan_id") or "")
    allowed, freshness_error = maintenance_scan_store.action_allowed("actor_images", scan_id)
    if not allowed:
        return None, freshness_error
    with actor_lock:
        scan = actor_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    requested = payload.get("items")
    selected = {}
    if requested is None:
        for item in scan.get("items") or []:
            if item.get("status") == "ready":
                selected[item["id"]] = ""
    elif isinstance(requested, list):
        for entry in requested:
            if isinstance(entry, dict):
                selected[str(entry.get("item_id") or "")] = str(entry.get("candidate_id") or "")
            else:
                selected[str(entry or "")] = ""
    else:
        return None, "Selected actors are invalid"

    files = []
    skipped = []
    for item_id, candidate_id in selected.items():
        item = _find_scan_item(scan, item_id)
        if not item:
            skipped.append({"item_id": item_id, "reason": "Actor not found in scan"})
            continue
        if item.get("status") != "ready" and not candidate_id:
            skipped.append({"item_id": item_id, "name": item.get("name", ""), "reason": "Actor needs review"})
            continue
        candidate = _candidate_by_id(item, candidate_id)
        if not candidate:
            skipped.append({"item_id": item_id, "name": item.get("name", ""), "reason": "Candidate image not found"})
            continue
        files.append(
            {
                "item_id": item["id"],
                "person_id": item.get("person_id", ""),
                "person_name": item.get("name", ""),
                "candidate_id": candidate.get("id", ""),
                "candidate_path": candidate.get("path", ""),
                "candidate_name": candidate.get("name", ""),
                "candidate_relative_path": candidate.get("relative_path", ""),
                "size_bytes": candidate.get("size_bytes", 0),
                "size_label": candidate.get("size_label", ""),
                "identity": candidate.get("identity") or {},
            }
        )
    plan_id = _now_id()
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "status": "ready",
        "created_at": utc_iso(),
        "file_count": len(files),
        "files": files,
        "skipped": skipped,
        "lib_root": os.path.realpath(lib_root),
    }
    with actor_lock:
        actor_plans[plan_id] = plan
    return public_plan(plan), None


def public_plan(plan):
    if not plan:
        return None
    return {
        "id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "status": plan.get("status", ""),
        "created_at": plan.get("created_at"),
        "file_count": plan.get("file_count", 0),
        "skipped": list(plan.get("skipped") or []),
        "files": [
            {
                "item_id": item.get("item_id", ""),
                "person_id": item.get("person_id", ""),
                "person_name": item.get("person_name", ""),
                "candidate_id": item.get("candidate_id", ""),
                "candidate_path": item.get("candidate_path", ""),
                "candidate_name": item.get("candidate_name", ""),
                "candidate_relative_path": item.get("candidate_relative_path", ""),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
            }
            for item in plan.get("files") or []
        ],
    }


def _prune_apply_runs_locked(now=None):
    now = now or time.time()
    terminal = [
        (apply_id, run)
        for apply_id, run in actor_apply_runs.items()
        if run.get("status") in {"success", "failed"}
    ]
    for apply_id, run in terminal:
        finished = run.get("_finished_ts") or run.get("_created_ts") or now
        if now - finished > APPLY_MAX_AGE_SECONDS:
            actor_apply_runs.pop(apply_id, None)
    terminal = sorted(
        (
            (apply_id, run)
            for apply_id, run in actor_apply_runs.items()
            if run.get("status") in {"success", "failed"}
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    for apply_id, _run in terminal[APPLY_RETENTION_COUNT:]:
        actor_apply_runs.pop(apply_id, None)


def _active_apply_locked():
    active = [run for run in actor_apply_runs.values() if run.get("status") in {"queued", "running"}]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


def _set_apply_progress(run, **values):
    if not run:
        return
    with actor_lock:
        run.update(values)


def start_import_apply(plan_id, opener=None):
    plan_id = str(plan_id or "")
    with actor_lock:
        _prune_apply_runs_locked()
        plan = actor_plans.get(plan_id)
        if not plan:
            return None, "Plan not found"
        active = _active_apply_locked()
        if active:
            return active, None
        if plan.get("status") == "applied":
            return None, "Plan already applied"
        run_id = _now_id()
        created = time.time()
        run = {
            "id": run_id,
            "plan_id": plan_id,
            "scan_id": plan.get("scan_id", ""),
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
            "imported_count": 0,
            "refused_count": 0,
            "failed_count": 0,
            "current_name": "",
            "current_path": "",
            "error": "",
            "result": None,
            "large_operation": len(plan.get("files") or []) >= LARGE_RESULT_COUNT,
        }
        actor_apply_runs[run_id] = run
        plan["status"] = "applying"
    if opener is not None:
        _execute_import_apply(run_id, opener=opener)
    else:
        threading.Thread(
            target=_execute_import_apply,
            args=(run_id,),
            daemon=True,
            name=f"vid2gif-actor-image-apply-{run_id}",
        ).start()
    return run, None


def _fetch_person_item(settings, person_id, opener=None):
    data, result = emby_client.request_json(
        settings,
        f"/Items/{urllib.parse.quote(str(person_id or ''))}",
        params={"Fields": "ImageTags,ProviderIds"},
        opener=opener,
        timeout=30,
    )
    return data, result


def _upload_person_image(settings, person_id, image_path, opener=None):
    api_key = str((settings or {}).get("emby_api_key") or "")
    mime = mimetypes.guess_type(image_path)[0] or "application/octet-stream"
    try:
        with open(image_path, "rb") as f:
            body = f.read()
    except OSError as exc:
        return emby_client.result(
            "failed",
            f"Emby image upload failed: {emby_client.sanitize_secret_text(exc, api_key)}",
            api_key=api_key,
            error_code="connection_error",
        )
    request_result = emby_client.request_no_content(
        settings,
        f"/Items/{urllib.parse.quote(str(person_id or ''))}/Images/Primary",
        method="POST",
        body=body,
        content_type=mime,
        opener=opener,
        timeout=30,
        accept="application/json",
    )
    if request_result.get("error_code") == "missing_config":
        return emby_client.result(
            "skipped",
            "Emby URL and API key are required",
            api_key=api_key,
            error_code="missing_config",
        )
    if request_result.get("error_code") == "http_error":
        return emby_client.result(
            "failed",
            f"Emby rejected the image upload ({request_result.get('http_status') or 'unknown'})",
            api_key=api_key,
            http_status=request_result.get("http_status"),
            error_code="http_error",
        )
    if request_result.get("status") != "success":
        return request_result
    return emby_client.result(
        "success",
        f"Actor image uploaded ({request_result.get('http_status')})",
        api_key=api_key,
        http_status=request_result.get("http_status"),
    )


def _identity_matches(path, identity):
    current = _file_identity(path)
    return bool(current and identity and current == identity)


def _execute_import_apply(apply_id, opener=None):
    with actor_lock:
        run = actor_apply_runs.get(apply_id)
        plan = actor_plans.get(run.get("plan_id")) if run else None
    if not run or not plan:
        return
    settings = _settings()
    files = list(plan.get("files") or [])
    imported = []
    refused = []
    failed = []
    records = []
    started = time.time()
    _set_apply_progress(
        run,
        status="running",
        started_at=utc_iso(started),
        _started_ts=started,
        progress_label=f"Processing 0 of {len(files)} actors",
    )

    def finish_item(index, item):
        pct = int(100 * index / max(len(files), 1))
        _set_apply_progress(
            run,
            processed_count=index,
            imported_count=len(imported),
            refused_count=len(refused),
            failed_count=len(failed),
            progress_percent=pct,
            progress_label=f"Processed {index} of {len(files)} actors",
            current_name=item.get("person_name", "") if index < len(files) else "",
            current_path=item.get("candidate_path", "") if index < len(files) else "",
        )

    for index, item in enumerate(files, start=1):
        person_id = item.get("person_id", "")
        path = item.get("candidate_path", "")
        record = {
            "type": "import",
            "timestamp": utc_iso(),
            "plan_id": plan.get("id", ""),
            "scan_id": plan.get("scan_id", ""),
            "person_id": person_id,
            "person_name": item.get("person_name", ""),
            "candidate_path": path,
            "candidate_name": item.get("candidate_name", ""),
            "result": "",
            "reason": "",
        }
        _set_apply_progress(run, current_name=item.get("person_name", ""), current_path=path)
        real = resolve_case_insensitive(path)
        if not person_id:
            record.update({"result": "refused", "reason": "Missing Emby person id"})
            refused.append(record)
        elif not real or not os.path.isfile(real):
            record.update({"result": "refused", "reason": "Candidate image is missing"})
            refused.append(record)
        elif os.path.islink(real) or not path_is_under(real, plan.get("lib_root") or LIB_ROOT):
            record.update({"result": "refused", "reason": "Candidate image is outside the library or is a symlink"})
            refused.append(record)
        elif not _identity_matches(real, item.get("identity") or {}):
            record.update({"result": "refused", "reason": "Candidate image changed since review"})
            refused.append(record)
        else:
            person, check_result = _fetch_person_item(settings, person_id, opener=opener)
            record["check"] = check_result
            if check_result.get("status") != "success":
                record.update({"result": "failed", "reason": check_result.get("message", "")})
                failed.append(record)
            elif _has_primary_image(person or {}):
                record.update({"result": "refused", "reason": "Actor already has an image in Emby"})
                refused.append(record)
            else:
                upload_result = _upload_person_image(settings, person_id, real, opener=opener)
                record["upload"] = upload_result
                if upload_result.get("status") == "success":
                    record.update({"result": "imported", "reason": upload_result.get("message", "")})
                    imported.append(record)
                else:
                    record.update({"result": "failed", "reason": upload_result.get("message", "")})
                    failed.append(record)
        records.append(record)
        finish_item(index, item)

    result = {
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "imported_count": len(imported),
        "refused_count": len(refused),
        "failed_count": len(failed),
    }
    log = _write_log("apply", {"summary": result}, records=records)
    result["log"] = log
    imported_ids = {record.get("person_id") for record in imported}
    imported_files = [item for item in plan.get("files") or [] if item.get("person_id") in imported_ids]
    impact_metrics.record_maintenance_action(
        plan.get("id"),
        "actor_images",
        resolutions=[
            {
                "issue_id": f"actor-image:{item.get('item_id')}",
                "stream": "actor_images",
                "finding_ids": [item.get("item_id")],
                "ensure_issue": True,
                "label": item.get("person_name") or "Missing actor image",
                "path": plan.get("lib_root"),
            }
            for item in imported_files
        ],
        operations={
            "other_files": len(imported_files),
            "other_bytes": sum(int(item.get("size_bytes") or 0) for item in imported_files),
        },
        timestamp=utc_iso(),
        label="Actor image import",
    )
    finished = time.time()
    status = "success" if not failed else "failed"
    with actor_lock:
        plan["status"] = "applied" if status == "success" else "failed"
        scan = actor_scans.get(plan.get("scan_id"))
        if scan:
            imported_ids = {record["person_id"] for record in imported}
            failed_ids = {record["person_id"] for record in failed}
            for scan_item in scan.get("items") or []:
                if scan_item.get("person_id") in imported_ids:
                    scan_item["status"] = "imported"
                    scan_item["status_label"] = "Imported"
                elif scan_item.get("person_id") in failed_ids:
                    scan_item["status"] = "failed"
                    scan_item["status_label"] = "Import failed"
            scan["counts"] = _counts(scan.get("items") or [])
    _set_apply_progress(
        run,
        status=status,
        result=result,
        progress_percent=100,
        progress_label="Actor image import complete" if status == "success" else "Actor image import finished with errors",
        _finished_ts=finished,
        finished_at=utc_iso(finished),
        current_name="",
        current_path="",
    )


def public_apply_run(run):
    if not run:
        return None
    result = run.get("result") or {}
    return {
        "id": run.get("id", ""),
        "plan_id": run.get("plan_id", ""),
        "scan_id": run.get("scan_id", ""),
        "status": run.get("status", ""),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "progress_percent": run.get("progress_percent", 0),
        "progress_label": run.get("progress_label", ""),
        "file_count": run.get("file_count", 0),
        "processed_count": run.get("processed_count", 0),
        "imported_count": run.get("imported_count", 0),
        "refused_count": run.get("refused_count", 0),
        "failed_count": run.get("failed_count", 0),
        "current_name": run.get("current_name", ""),
        "current_path": run.get("current_path", ""),
        "error": run.get("error", ""),
        "large_operation": bool(run.get("large_operation")),
        "result": {
            **{key: value for key, value in result.items() if key != "log"},
            "log": {key: value for key, value in (result.get("log") or {}).items() if key != "path"},
        } if result else None,
    }


def apply_status(apply_id=None):
    with actor_lock:
        _prune_apply_runs_locked()
        if apply_id:
            run = actor_apply_runs.get(str(apply_id or ""))
            if not run:
                return None, "Apply run not found"
        elif actor_apply_runs:
            run = max(actor_apply_runs.values(), key=lambda item: item.get("_created_ts") or 0)
        else:
            run = None
    return {"apply": public_apply_run(run)}, None


def _log_record(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _write_log(kind, payload, records=None):
    os.makedirs(LOG_DIR, exist_ok=True)
    log_id = f"{_now_id()}-{kind}.jsonl"
    path = os.path.join(LOG_DIR, log_id)
    header = {"type": kind, "timestamp": utc_iso(), **(payload or {})}
    written = 0
    truncated = False
    with open(path, "w", encoding="utf-8") as f:
        line = _log_record(header)
        f.write(line)
        written += len(line.encode("utf-8"))
        for record in records or []:
            line = _log_record(record)
            size = len(line.encode("utf-8"))
            if written + size > LOG_MAX_BYTES:
                truncated = True
                break
            f.write(line)
            written += size
        if truncated:
            f.write(_log_record({"type": "truncated", "timestamp": utc_iso(), "message": "Log reached maximum size."}))
    entry = {
        "id": log_id,
        "path": path,
        "created_at": header["timestamp"],
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


def read_log(log_id):
    clean_id = os.path.basename(str(log_id or ""))
    index = _read_json(LOG_INDEX, {"logs": []})
    match = next((item for item in index.get("logs") or [] if item.get("id") == clean_id), None)
    if not match:
        return None, "Log not found"
    path = match.get("path", "")
    if not path_is_under(path, LOG_DIR) or not os.path.isfile(path):
        return None, "Log not found"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return {
            "id": clean_id,
            "content": f.read(LOG_MAX_BYTES),
            "size_label": match.get("size_label", ""),
            "truncated": bool(match.get("truncated")),
        }, None


def preview_image_path(path, lib_root=LIB_ROOT):
    real = resolve_case_insensitive(str(path or "").strip())
    if (
        not real
        or os.path.islink(real)
        or not os.path.isfile(real)
        or not path_is_under(real, lib_root)
        or os.path.splitext(real)[1].lower() not in IMAGE_EXTS
    ):
        return None, "Image not found"
    return os.path.realpath(real), None
