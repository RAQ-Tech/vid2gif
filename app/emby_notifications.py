import datetime
import hashlib
import json
import os
import threading
import time
from typing import Literal, TypedDict

from . import app_settings
from . import emby_client
from .config import STATE_ROOT
from .progress import utc_iso


NotificationStatus = Literal["success", "failed", "disabled", "not_configured", "skipped", "pending"]


class NotificationResult(TypedDict, total=False):
    id: str
    status: NotificationStatus
    message: str
    created_at: str | None
    sent_at: str | None


NOTIFICATION_ROOT = os.path.join(STATE_ROOT, "emby-notifications")
RETENTION_COUNT = 100
RETENTION_SECONDS = 30 * 24 * 60 * 60
DESCRIPTION_LIMIT = 500

_lock = threading.RLock()


def _settings(settings=None):
    return dict(settings or app_settings.load_settings())


def _id(event_key):
    return hashlib.sha256(str(event_key or "").encode("utf-8")).hexdigest()[:24]


def _path(notification_id):
    return os.path.join(NOTIFICATION_ROOT, f"{notification_id}.json")


def _read(notification_id):
    try:
        with open(_path(notification_id), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _write(value):
    os.makedirs(NOTIFICATION_ROOT, exist_ok=True)
    path = _path(value["id"])
    temp = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(value, handle, separators=(",", ":"))
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


def _timestamp(value):
    try:
        return datetime.datetime.fromisoformat(str(value or "").replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return 0


def _prune(now=None):
    now = time.time() if now is None else float(now)
    try:
        names = [name for name in os.listdir(NOTIFICATION_ROOT) if name.endswith(".json")]
    except OSError:
        return
    values = []
    for name in names:
        value = _read(name[:-5]) or {}
        values.append((_timestamp(value.get("created_at")), os.path.join(NOTIFICATION_ROOT, name)))
    values.sort(reverse=True)
    for index, (created, path) in enumerate(values):
        if index >= RETENTION_COUNT or (created and now - created > RETENTION_SECONDS):
            try:
                os.remove(path)
            except OSError:
                pass


def public_result(value):
    if not isinstance(value, dict):
        return None
    return {
        "id": str(value.get("id") or ""),
        "status": str(value.get("status") or "failed"),
        "message": str(value.get("message") or ""),
        "created_at": value.get("created_at"),
        "sent_at": value.get("sent_at"),
    }


def _clean(value, settings, limit):
    text = emby_client.sanitize_secret_text(value, settings.get("emby_api_key", ""))
    for private in (settings.get("emby_url"),):
        if private:
            text = text.replace(str(private), "[redacted]")
    return " ".join(str(text or "").split())[:limit]


def _send(name, description, *, event_key, settings=None, opener=None, force=False):
    settings = _settings(settings)
    notification_id = _id(event_key)
    with _lock:
        existing = _read(notification_id)
        if existing:
            return public_result(existing)
        policy = str(settings.get("emby_admin_notifications") or "warnings").casefold()
        created_at = utc_iso()
        if policy == "off" and not force:
            return public_result({"id": notification_id, "status": "disabled", "message": "Emby administrator notifications are disabled", "created_at": created_at, "sent_at": None})
        if not settings.get("emby_url") or not settings.get("emby_api_key"):
            return public_result({"id": notification_id, "status": "not_configured", "message": "Configure Emby to send administrator notifications", "created_at": created_at, "sent_at": None})
        record = {
            "id": notification_id,
            "event_key": str(event_key or ""),
            "status": "pending",
            "message": "Administrator notification is pending",
            "name": _clean(name, settings, 120),
            "description": _clean(description, settings, DESCRIPTION_LIMIT),
            "created_at": created_at,
            "sent_at": None,
        }
        _write(record)
    result = emby_client.request_no_content(
        settings,
        "/Notifications/Admin",
        params={"Name": record["name"], "Description": record["description"]},
        json_body={"DisplayDateTime": True},
        opener=opener,
        timeout=15,
    )
    with _lock:
        if result.get("status") == "success":
            record.update(status="success", message="Emby accepted the administrator notification", sent_at=utc_iso())
        else:
            record.update(status="failed", message=_clean(result.get("message") or "Administrator notification failed", settings, 300))
        _write(record)
        _prune()
    return public_result(record)


def send(name, description, *, event_key, settings=None, opener=None, force=False):
    try:
        return _send(
            name,
            description,
            event_key=event_key,
            settings=settings,
            opener=opener,
            force=force,
        )
    except Exception as exc:
        current = _settings(settings)
        return public_result(
            {
                "id": _id(event_key),
                "status": "failed",
                "message": _clean(
                    f"Administrator notification failed: {exc}", current, 300
                ),
                "created_at": utc_iso(),
                "sent_at": None,
            }
        )


def notify_maintenance(
    workflow,
    run_id,
    *,
    status,
    attempted_count=0,
    succeeded_count=0,
    failed_count=0,
    refused_count=0,
    deferred_count=0,
    unresolved_count=0,
    reclaimed_bytes=0,
    emby_sync=None,
    settings=None,
    opener=None,
):
    settings = _settings(settings)
    policy = str(settings.get("emby_admin_notifications") or "warnings").casefold()
    notification_id = _id(f"maintenance:{workflow}:{run_id}")
    base = {"id": notification_id, "created_at": utc_iso(), "sent_at": None}
    if policy == "off":
        return public_result({**base, "status": "disabled", "message": "Emby administrator notifications are disabled"})
    status_key = str(status or "").casefold()
    if status_key in {"cancelled", "canceled", "cancelling"} or int(attempted_count or 0) <= 0:
        return public_result({**base, "status": "skipped", "message": "This maintenance outcome does not require an administrator notification"})
    sync_status = str((emby_sync or {}).get("status") or "")
    warning = (
        status_key in {"failed", "complete_with_issues", "partial"}
        or any(int(value or 0) > 0 for value in (failed_count, refused_count, deferred_count, unresolved_count))
        or sync_status in {"partial", "failed"}
    )
    if policy == "warnings" and not warning:
        return public_result({**base, "status": "skipped", "message": "Successful maintenance does not require a warning notification"})
    title = f"vid2gif: {workflow} needs attention" if warning else f"vid2gif: {workflow} completed"
    parts = [
        f"Outcome {status_key or 'complete'}",
        f"attempted {int(attempted_count or 0)}",
        f"succeeded {int(succeeded_count or 0)}",
    ]
    for label, value in (("failed", failed_count), ("refused", refused_count), ("deferred", deferred_count), ("unresolved", unresolved_count)):
        if int(value or 0):
            parts.append(f"{label} {int(value)}")
    if int(reclaimed_bytes or 0):
        parts.append(f"reclaimed bytes {int(reclaimed_bytes)}")
    if sync_status:
        parts.append(f"Emby sync {sync_status}")
    return send(title, "; ".join(parts), event_key=f"maintenance:{workflow}:{run_id}", settings=settings, opener=opener)


def send_test(settings=None, *, opener=None):
    return send(
        "vid2gif notification test",
        "vid2gif successfully reached the Emby administrator notification endpoint.",
        event_key=f"test:{time.time_ns()}",
        settings=settings,
        opener=opener,
        force=True,
    )
