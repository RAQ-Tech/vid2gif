import hashlib
import threading
import time
import urllib.parse
from typing import Literal, TypedDict

from . import app_settings
from . import emby_client
from .progress import utc_iso


OperationsStatus = Literal["ready", "not_configured", "unavailable", "forbidden"]


class EmbyTask(TypedDict, total=False):
    id: str
    name: str
    key: str
    category: str
    description: str
    state: str
    progress_percent: float
    triggers: list[dict]
    last_result: dict | None
    can_start: bool
    can_cancel: bool


class TaskInventory(TypedDict, total=False):
    status: OperationsStatus
    checked_at: str | None
    configured: bool
    tasks: list[EmbyTask]
    thumbnail_task_id: str
    thumbnail_match_count: int
    running_count: int
    failed_count: int
    message: str


SUCCESS_CACHE_SECONDS = 2
ACTIVITY_FETCH_LIMIT = 100
ACTIVITY_PUBLIC_LIMIT = 20
ERROR_LIMIT = 300

_cache_lock = threading.RLock()
_task_cache: dict[str, tuple[float, TaskInventory]] = {}


def _settings(settings=None):
    return dict(settings or app_settings.load_settings())


def _fingerprint(settings):
    value = "|".join(
        (str(settings.get("emby_url") or "").strip().rstrip("/"), str(settings.get("emby_api_key") or ""))
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def clear_cache():
    with _cache_lock:
        _task_cache.clear()


def _clean_text(value, settings, limit=500):
    text = emby_client.sanitize_secret_text(value, settings.get("emby_api_key", ""))
    for private in [settings.get("emby_url"), *(m.get("local_prefix") for m in settings.get("emby_path_mappings") or [] if isinstance(m, dict))]:
        private = str(private or "").strip()
        if private:
            text = text.replace(private, "[redacted]")
    return " ".join(text.split())[:limit]


def _progress(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, value))


def _trigger(raw):
    raw = raw if isinstance(raw, dict) else {}
    return {
        key: raw.get(key)
        for key in ("Type", "TimeOfDayTicks", "IntervalTicks", "SystemEvent", "DayOfWeek", "MaxRuntimeTicks")
        if raw.get(key) is not None
    }


def _last_result(raw, settings):
    raw = raw if isinstance(raw, dict) else {}
    if not raw:
        return None
    return {
        "status": str(raw.get("Status") or raw.get("status") or ""),
        "start_time": raw.get("StartTimeUtc") or raw.get("start_time"),
        "end_time": raw.get("EndTimeUtc") or raw.get("end_time"),
        "error_message": _clean_text(raw.get("ErrorMessage") or raw.get("error_message") or "", settings, ERROR_LIMIT),
    }


def _task_text(raw):
    return " ".join(
        str(raw.get(key) or "")
        for key in ("Name", "name", "Key", "key", "Description", "description")
    ).casefold()


def _is_thumbnail_task(raw):
    text = _task_text(raw)
    return (
        "thumbnail image extraction" in text
        or "thumbnail images extraction" in text
        or "video preview thumbnail" in text
        or ("thumbnail" in text and "extract" in text)
        or ("chapter" in text and "image" in text and "extract" in text)
    )


def _public_task(raw, settings, *, controllable=False):
    raw = raw if isinstance(raw, dict) else {}
    state = str(raw.get("State") or raw.get("state") or "")
    task = {
        "id": str(raw.get("Id") or raw.get("id") or ""),
        "name": _clean_text(raw.get("Name") or raw.get("name") or "", settings, 200),
        "key": _clean_text(raw.get("Key") or raw.get("key") or "", settings, 200),
        "category": _clean_text(raw.get("Category") or raw.get("category") or "", settings, 200),
        "description": _clean_text(raw.get("Description") or raw.get("description") or "", settings, 500),
        "state": state,
        "progress_percent": _progress(raw.get("CurrentProgressPercentage", raw.get("progress_percent", 0))),
        "triggers": [_trigger(item) for item in (raw.get("Triggers") or raw.get("triggers") or []) if isinstance(item, dict)],
        "last_result": _last_result(raw.get("LastExecutionResult") or raw.get("last_result"), settings),
        "can_start": bool(controllable and state.casefold() == "idle"),
        "can_cancel": bool(controllable and state.casefold() == "running"),
    }
    return task


def _base(status, message, settings, **values):
    return {
        "status": status,
        "checked_at": values.pop("checked_at", utc_iso()),
        "configured": bool(settings.get("emby_url") and settings.get("emby_api_key")),
        "tasks": [],
        "thumbnail_task_id": "",
        "thumbnail_match_count": 0,
        "running_count": 0,
        "failed_count": 0,
        "message": _clean_text(message, settings, 500),
        **values,
    }


def load_tasks(settings=None, *, opener=None, force=False, now=None):
    settings = _settings(settings)
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return _base("not_configured", "Configure Emby to monitor scheduled tasks", settings)
    now = time.monotonic() if now is None else float(now)
    fingerprint = _fingerprint(settings)
    if not force:
        with _cache_lock:
            cached = _task_cache.get(fingerprint)
        if cached and cached[0] > now:
            return dict(cached[1])
    data, result = emby_client.request_json(
        settings,
        "/ScheduledTasks",
        params={"IsHidden": "false"},
        opener=opener,
        timeout=15,
    )
    if result.get("status") != "success":
        status = "forbidden" if result.get("http_status") == 403 else "unavailable"
        return _base(status, result.get("message") or "Emby scheduled tasks are unavailable", settings, checked_at=result.get("checked_at"))
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        return _base("unavailable", "Emby returned an invalid scheduled-task response", settings, checked_at=result.get("checked_at"))
    matches = [item for item in data if _is_thumbnail_task(item)]
    match_id = str((matches[0] if len(matches) == 1 else {}).get("Id") or (matches[0] if len(matches) == 1 else {}).get("id") or "")
    tasks = [_public_task(item, settings, controllable=bool(match_id and str(item.get("Id") or item.get("id") or "") == match_id)) for item in data]
    payload = _base(
        "ready",
        f"Emby reports {len(tasks)} non-hidden scheduled task(s)",
        settings,
        checked_at=result.get("checked_at"),
        tasks=tasks,
        thumbnail_task_id=match_id,
        thumbnail_match_count=len(matches),
        running_count=sum(1 for task in tasks if task["state"].casefold() in {"running", "cancelling"}),
        failed_count=sum(1 for task in tasks if (task.get("last_result") or {}).get("status", "").casefold() in {"failed", "aborted"}),
    )
    with _cache_lock:
        _task_cache[fingerprint] = (now + SUCCESS_CACHE_SECONDS, payload)
    return dict(payload)


def get_task(task_id, settings=None, *, opener=None, force=False):
    inventory = load_tasks(settings, opener=opener, force=force)
    task = next((item for item in inventory.get("tasks") or [] if item.get("id") == str(task_id or "")), None)
    return {**inventory, "task": task, "tasks": []}


def _control(task_id, action, settings=None, *, opener=None):
    settings = _settings(settings)
    inventory = load_tasks(settings, opener=opener, force=True)
    if inventory.get("status") != "ready":
        return {"status": "failed", "message": inventory.get("message"), "task": None, "result": None}, "upstream"
    task = next((item for item in inventory.get("tasks") or [] if item.get("id") == str(task_id or "")), None)
    if not task:
        return {"status": "failed", "message": "Scheduled task was not found", "task": None, "result": None}, "not_found"
    if task.get("id") != inventory.get("thumbnail_task_id"):
        return {"status": "failed", "message": "vid2gif can only control Emby thumbnail extraction", "task": task, "result": None}, "forbidden"
    state = str(task.get("state") or "").casefold()
    if action == "start" and state in {"running", "cancelling"}:
        return {"status": "failed", "message": "Thumbnail extraction is already active", "task": task, "result": None}, "conflict"
    if action == "start" and state != "idle":
        return {"status": "failed", "message": "Thumbnail extraction is not ready to start", "task": task, "result": None}, "conflict"
    if action == "cancel" and state != "running":
        return {"status": "failed", "message": "Thumbnail extraction is not running", "task": task, "result": None}, "conflict"
    task_id_path = urllib.parse.quote(str(task.get("id") or ""), safe="")
    path = f"/ScheduledTasks/Running/{task_id_path}" if action == "start" else f"/ScheduledTasks/Running/{task_id_path}/Delete"
    result = emby_client.request_no_content(settings, path, method="POST", body=b"", opener=opener, timeout=15)
    if result.get("status") != "success":
        return {"status": "failed", "message": result.get("message"), "task": task, "result": emby_client.public_result(result)}, "upstream"
    clear_cache()
    verb = "start" if action == "start" else "cancellation"
    return {
        "status": "accepted",
        "message": f"Emby accepted the thumbnail extraction {verb} request",
        "checked_at": result.get("checked_at"),
        "task": task,
        "result": emby_client.public_result(result),
    }, None


def start_task(task_id, settings=None, *, opener=None):
    return _control(task_id, "start", settings, opener=opener)


def cancel_task(task_id, settings=None, *, opener=None):
    return _control(task_id, "cancel", settings, opener=opener)


def load_activity(settings=None, *, opener=None, limit=ACTIVITY_PUBLIC_LIMIT):
    settings = _settings(settings)
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return {"status": "not_configured", "checked_at": utc_iso(), "entries": [], "message": "Configure Emby to view task activity"}
    try:
        limit = max(1, min(ACTIVITY_PUBLIC_LIMIT, int(limit)))
    except (TypeError, ValueError):
        limit = ACTIVITY_PUBLIC_LIMIT
    data, result = emby_client.request_json(
        settings,
        "/System/ActivityLog/Entries",
        params={"StartIndex": 0, "Limit": ACTIVITY_FETCH_LIMIT},
        opener=opener,
        timeout=15,
    )
    if result.get("status") != "success":
        status = "forbidden" if result.get("http_status") == 403 else "unavailable"
        return {"status": status, "checked_at": result.get("checked_at"), "entries": [], "message": _clean_text(result.get("message"), settings)}
    if not isinstance(data, dict) or not isinstance(data.get("Items"), list):
        return {"status": "unavailable", "checked_at": result.get("checked_at"), "entries": [], "message": "Emby returned an invalid activity response"}
    inventory = load_tasks(settings, opener=opener)
    terms = {str(value).casefold() for task in inventory.get("tasks") or [] for value in (task.get("name"), task.get("key")) if value}
    entries = []
    for raw in data.get("Items"):
        if not isinstance(raw, dict):
            continue
        name = _clean_text(raw.get("Name") or "", settings, 200)
        entry_type = _clean_text(raw.get("Type") or "", settings, 200)
        haystack = f"{name} {entry_type}".casefold()
        if "task" not in haystack and "scheduled" not in haystack and not any(term in haystack for term in terms):
            continue
        entries.append({
            "name": name,
            "type": entry_type,
            "date": raw.get("Date"),
            "severity": str(raw.get("Severity") or ""),
        })
        if len(entries) >= limit:
            break
    return {"status": "ready", "checked_at": result.get("checked_at"), "entries": entries, "message": f"Showing {len(entries)} recent task-related activity entries"}
