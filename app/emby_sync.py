import datetime
import hashlib
import json
import os
import threading
import time
import urllib.parse
from typing import Literal, TypedDict

from . import app_settings
from . import emby_catalog
from . import emby_client
from .config import STATE_ROOT
from .progress import utc_iso


ChangeType = Literal["Created", "Modified", "Deleted"]
RefreshScope = Literal["media", "metadata", "image", "thumbnail"]
SyncStatus = Literal["success", "partial", "failed", "disabled", "not_configured", "queued", "running"]


class EmbySyncChange(TypedDict, total=False):
    id: str
    workflow: str
    run_id: str
    local_path: str
    update_type: ChangeType
    destination_path: str
    emby_item_id: str
    refresh_scope: RefreshScope
    prefer_path: bool
    status: str
    method: str
    message: str


class EmbySyncTarget(TypedDict, total=False):
    method: Literal["item", "path"]
    item_id: str
    path: str
    update_type: ChangeType
    refresh_scope: RefreshScope
    changes: list[EmbySyncChange]


class EmbySyncResult(TypedDict):
    id: str
    status: SyncStatus
    message: str
    created_at: str | None
    attempted_at: str | None
    finished_at: str | None
    attempted_count: int
    succeeded_count: int
    failed_count: int
    unresolved_count: int
    item_refresh_count: int
    path_notification_count: int
    retryable: bool


SYNC_ROOT = os.path.join(STATE_ROOT, "emby-sync")
RETENTION_COUNT = 100
RETENTION_SECONDS = 30 * 24 * 60 * 60
PATH_BATCH_SIZE = 100

_lock = threading.RLock()
_active = set()


def _hash(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _change_id(change):
    return _hash(
        "|".join(
            str(change.get(key) or "")
            for key in (
                "workflow",
                "run_id",
                "local_path",
                "update_type",
                "destination_path",
                "emby_item_id",
                "refresh_scope",
                "prefer_path",
            )
        )
    )[:24]


def normalize_change(change) -> EmbySyncChange:
    if not isinstance(change, dict):
        raise ValueError("Emby synchronization change is invalid")
    update_type = str(change.get("update_type") or "Modified").strip().title()
    if update_type not in {"Created", "Modified", "Deleted"}:
        raise ValueError("Emby synchronization change type is invalid")
    scope = str(change.get("refresh_scope") or "metadata").strip().lower()
    if scope not in {"media", "metadata", "image", "thumbnail"}:
        raise ValueError("Emby synchronization refresh scope is invalid")
    raw_local_path = str(change.get("local_path") or "").strip()
    if not raw_local_path:
        raise ValueError("Emby synchronization path is required")
    normalized: EmbySyncChange = {
        "workflow": str(change.get("workflow") or "maintenance"),
        "run_id": str(change.get("run_id") or ""),
        "local_path": os.path.realpath(raw_local_path),
        "update_type": update_type,
        "destination_path": os.path.realpath(str(change.get("destination_path") or ""))
        if change.get("destination_path")
        else "",
        "emby_item_id": str(change.get("emby_item_id") or ""),
        "refresh_scope": scope,
        "prefer_path": bool(change.get("prefer_path")),
        "status": "pending",
        "method": "",
        "message": "",
    }
    normalized["id"] = _change_id(normalized)
    return normalized


def _job_path(sync_id):
    return os.path.join(SYNC_ROOT, f"{sync_id}.json")


def _write_json_atomic(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def _read_job(sync_id):
    try:
        with open(_job_path(sync_id), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _timestamp(value):
    try:
        parsed = datetime.datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
        return parsed.timestamp()
    except (TypeError, ValueError):
        return 0


def _prune_jobs(now=None):
    now = time.time() if now is None else float(now)
    try:
        names = [name for name in os.listdir(SYNC_ROOT) if name.endswith(".json")]
    except OSError:
        return
    jobs = []
    for name in names:
        path = os.path.join(SYNC_ROOT, name)
        job = _read_job(name[:-5]) or {}
        created = _timestamp(job.get("created_at"))
        jobs.append((created, path, name[:-5]))
    jobs.sort(reverse=True)
    for index, (created, path, sync_id) in enumerate(jobs):
        if sync_id in _active:
            continue
        if index >= RETENTION_COUNT or (created and now - created > RETENTION_SECONDS):
            try:
                os.remove(path)
            except OSError:
                pass


def _save_job(job):
    with _lock:
        job["updated_at"] = utc_iso()
        _write_json_atomic(_job_path(job["id"]), job)
        _prune_jobs()


def public_sync(job) -> EmbySyncResult | None:
    if not isinstance(job, dict):
        return None
    result = job.get("result") or {}
    job_status = job.get("status") or "failed"
    status = job_status if job_status in {"queued", "running"} else result.get("status") or job_status
    return {
        "id": str(job.get("id") or ""),
        "status": status,
        "message": str(result.get("message") or job.get("message") or ""),
        "created_at": job.get("created_at"),
        "attempted_at": job.get("attempted_at"),
        "finished_at": job.get("finished_at"),
        "attempted_count": int(result.get("attempted_count") or 0),
        "succeeded_count": int(result.get("succeeded_count") or 0),
        "failed_count": int(result.get("failed_count") or 0),
        "unresolved_count": int(result.get("unresolved_count") or 0),
        "item_refresh_count": int(result.get("item_refresh_count") or 0),
        "path_notification_count": int(result.get("path_notification_count") or 0),
        "retryable": bool(result.get("retryable")),
    }


def get_sync(sync_id):
    with _lock:
        return _read_job(str(sync_id or ""))


def _same_path_prefix(path, prefix):
    path_key = emby_catalog.normalize_path(path)
    prefix_key = emby_catalog.normalize_path(prefix)
    return bool(prefix_key and (path_key == prefix_key or path_key.startswith(prefix_key.rstrip("/") + "/")))


def emby_paths_for_local(local_path, mappings):
    local_path = str(local_path or "")
    candidates = []
    for mapping in mappings or []:
        if not isinstance(mapping, dict):
            continue
        local_prefix = str(mapping.get("local_prefix") or "").rstrip("/\\")
        emby_prefix = str(mapping.get("emby_prefix") or "").rstrip("/\\")
        if not local_prefix or not emby_prefix or not _same_path_prefix(local_path, local_prefix):
            continue
        try:
            remainder = os.path.relpath(os.path.realpath(local_path), os.path.realpath(local_prefix))
        except ValueError:
            continue
        mapped = emby_prefix if remainder == "." else emby_prefix + "/" + remainder.replace("\\", "/")
        candidates.append((len(emby_catalog.normalize_path(local_prefix)), mapped))
    if not candidates:
        return [local_path] if os.path.isabs(local_path) else []
    longest = max(length for length, _path in candidates)
    return sorted({path for length, path in candidates if length == longest})


def _item_refresh(settings, item_id, scope, opener=None):
    params = {
        "Recursive": "false",
        "MetadataRefreshMode": "ValidationOnly",
        "ImageRefreshMode": "Default" if scope == "image" else "ValidationOnly",
        "ReplaceAllMetadata": "false",
        "ReplaceAllImages": "false",
    }
    return emby_client.request_no_content(
        settings,
        f"/Items/{urllib.parse.quote(str(item_id), safe='')}/Refresh",
        params=params,
        method="POST",
        json_body={"ReplaceThumbnailImages": False},
        opener=opener,
        timeout=15,
    )


def _path_batches(changes, settings, opener=None):
    accepted = 0
    failed = 0
    unresolved = 0
    pending_by_path: dict[str, EmbySyncTarget] = {}
    mappings = settings.get("emby_path_mappings") or []
    for change in changes:
        paths = emby_paths_for_local(change.get("local_path"), mappings)
        if len(paths) != 1:
            change.update(status="unresolved", method="path", message="Path mapping is ambiguous")
            unresolved += 1
            continue
        key = emby_catalog.normalize_path(paths[0])
        target = pending_by_path.get(key)
        if target is None:
            pending_by_path[key] = {
                "method": "path",
                "path": paths[0],
                "update_type": change.get("update_type") or "Modified",
                "changes": [change],
            }
            continue
        target["changes"].append(change)
        update_types = {target.get("update_type"), change.get("update_type")}
        if update_types == {"Created", "Deleted"}:
            target["update_type"] = "Modified"
        elif "Deleted" in update_types:
            target["update_type"] = "Deleted"
        elif "Created" in update_types:
            target["update_type"] = "Created"
        else:
            target["update_type"] = "Modified"
    pending = list(pending_by_path.values())
    batch_count = 0
    for start in range(0, len(pending), PATH_BATCH_SIZE):
        batch = pending[start : start + PATH_BATCH_SIZE]
        request_result = emby_client.request_no_content(
            settings,
            "/Library/Media/Updated",
            method="POST",
            json_body={
                "Updates": [
                    {"Path": target.get("path"), "UpdateType": target.get("update_type")}
                    for target in batch
                ]
            },
            opener=opener,
            timeout=15,
        )
        if request_result.get("status") == "success":
            batch_count += 1
            for target in batch:
                for change in target.get("changes") or []:
                    change.update(status="accepted", method="path", message="Emby accepted the path notification")
                    accepted += 1
        else:
            for target in batch:
                for change in target.get("changes") or []:
                    change.update(status="failed", method="path", message=request_result.get("message") or "Path notification failed")
                    failed += 1
    return accepted, failed, unresolved, batch_count


def _result_for_job(job, status, message, *, attempted_count=0, item_refresh_count=0, path_notification_count=0):
    changes = job.get("changes") or []
    succeeded = sum(1 for change in changes if change.get("status") == "accepted")
    failed = sum(1 for change in changes if change.get("status") == "failed")
    unresolved = sum(1 for change in changes if change.get("status") == "unresolved")
    return {
        "status": status,
        "message": message,
        "attempted_count": attempted_count,
        "succeeded_count": succeeded,
        "failed_count": failed,
        "unresolved_count": unresolved,
        "item_refresh_count": item_refresh_count,
        "path_notification_count": path_notification_count,
        "retryable": bool(failed or unresolved or status == "not_configured"),
    }


def _execute(job, settings=None, opener=None):
    settings = dict(settings or app_settings.load_settings())
    pending = [change for change in job.get("changes") or [] if change.get("status") != "accepted"]
    job.update(status="running", attempted_at=utc_iso(), finished_at=None)
    _save_job(job)
    if not settings.get("emby_sync_after_maintenance", True):
        job["result"] = _result_for_job(job, "disabled", "Automatic Emby synchronization is disabled")
        job.update(status="disabled", finished_at=utc_iso())
        _save_job(job)
        return public_sync(job)
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        job["result"] = _result_for_job(job, "not_configured", "Configure Emby to synchronize maintenance changes")
        job.update(status="not_configured", finished_at=utc_iso())
        _save_job(job)
        return public_sync(job)

    item_groups: dict[str, EmbySyncTarget] = {}
    path_changes = []
    attempted = 0
    item_refresh_count = 0
    for change in pending:
        if change.get("emby_item_id") and not change.get("prefer_path"):
            item_id = change["emby_item_id"]
            group = item_groups.setdefault(
                item_id,
                {
                    "method": "item",
                    "item_id": item_id,
                    "refresh_scope": change.get("refresh_scope") or "metadata",
                    "changes": [],
                },
            )
            group["changes"].append(change)
            if change.get("refresh_scope") == "image":
                group["refresh_scope"] = "image"
        else:
            path_changes.append(change)
    for item_id, target in item_groups.items():
        scope = target.get("refresh_scope") or "metadata"
        grouped = target.get("changes") or []
        attempted += len(grouped)
        request_result = _item_refresh(settings, item_id, scope, opener=opener)
        if request_result.get("status") == "success":
            item_refresh_count += 1
            for change in grouped:
                change.update(status="accepted", method="item", message="Emby accepted the targeted item refresh")
        else:
            for change in grouped:
                change.update(status="pending", method="item", message=request_result.get("message") or "Item refresh failed")
                path_changes.append(change)

    path_accepted, _path_failed, _unresolved, batch_count = _path_batches(
        path_changes, settings, opener=opener
    )
    attempted += len(path_changes)
    succeeded = sum(1 for change in job.get("changes") or [] if change.get("status") == "accepted")
    failed = sum(1 for change in job.get("changes") or [] if change.get("status") == "failed")
    unresolved = sum(1 for change in job.get("changes") or [] if change.get("status") == "unresolved")
    if failed or unresolved:
        status = "partial" if succeeded else "failed"
        message = f"Emby accepted {succeeded} change(s); {failed + unresolved} need attention"
    else:
        status = "success"
        message = f"Emby accepted {succeeded} targeted maintenance change(s)"
    job["result"] = _result_for_job(
        job,
        status,
        message,
        attempted_count=attempted,
        item_refresh_count=item_refresh_count,
        path_notification_count=path_accepted,
    )
    job.update(status=status, finished_at=utc_iso())
    if succeeded:
        emby_catalog.clear_cache()
    _save_job(job)
    return public_sync(job)


def _execute_safely(job, settings=None, opener=None):
    try:
        return _execute(job, settings=settings, opener=opener)
    except Exception:
        for change in job.get("changes") or []:
            if change.get("status") != "accepted":
                change.update(
                    status="failed",
                    message="Synchronization could not be completed",
                )
        job["result"] = _result_for_job(
            job,
            "partial" if any(change.get("status") == "accepted" for change in job.get("changes") or []) else "failed",
            "Emby synchronization could not be completed; local maintenance changes were preserved",
        )
        job.update(status=job["result"]["status"], finished_at=utc_iso())
        try:
            _save_job(job)
        except Exception:
            pass
        return public_sync(job)


def sync_changes(changes, *, workflow="maintenance", run_id="", settings=None, opener=None):
    normalized = []
    seen = set()
    for raw in changes or []:
        value = dict(raw or {})
        value.setdefault("workflow", workflow)
        value.setdefault("run_id", run_id)
        change = normalize_change(value)
        if change["id"] in seen:
            continue
        seen.add(change["id"])
        normalized.append(change)
    sync_id = f"{int(time.time() * 1000)}-{_hash(str(time.time_ns()) + workflow + str(run_id))[:10]}"
    job = {
        "id": sync_id,
        "workflow": str(workflow or "maintenance"),
        "run_id": str(run_id or ""),
        "status": "queued",
        "created_at": utc_iso(),
        "attempted_at": None,
        "finished_at": None,
        "changes": normalized,
        "result": {},
    }
    with _lock:
        _active.add(sync_id)
        try:
            _save_job(job)
        except Exception:
            _active.discard(sync_id)
            for change in job["changes"]:
                change.update(status="failed", message="Synchronization job could not be persisted")
            job["result"] = _result_for_job(
                job,
                "failed",
                "Emby synchronization could not be started; local maintenance changes were preserved",
            )
            job.update(status="failed", finished_at=utc_iso())
            return public_sync(job)
    try:
        return _execute_safely(job, settings=settings, opener=opener)
    finally:
        with _lock:
            _active.discard(sync_id)


def start_retry(sync_id, *, opener=None):
    sync_id = str(sync_id or "")
    with _lock:
        if sync_id in _active:
            return None, "Synchronization retry is already running"
        job = _read_job(sync_id)
        if not job:
            return None, "Synchronization job not found"
        retryable = [change for change in job.get("changes") or [] if change.get("status") != "accepted"]
        if not retryable:
            return None, "Synchronization job has nothing to retry"
        for change in retryable:
            change.update(status="pending", method="", message="")
        job["status"] = "queued"
        job["finished_at"] = None
        _active.add(sync_id)
        _save_job(job)

    def execute():
        try:
            _execute_safely(job, opener=opener)
        finally:
            with _lock:
                _active.discard(sync_id)

    threading.Thread(target=execute, daemon=True, name=f"vid2gif-emby-sync-{sync_id}").start()
    return public_sync(job), None
