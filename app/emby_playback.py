import time
import threading
from typing import Literal, TypedDict

from . import app_settings
from . import emby_catalog
from . import emby_client
from .progress import utc_iso


PlaybackStatus = Literal["clear", "active", "not_configured", "unavailable", "disabled"]
TargetStatus = Literal["clear", "active", "unverified", "not_checked"]


class PlaybackTarget(TypedDict, total=False):
    id: str
    group_id: str
    local_path: str
    emby_item_id: str
    emby_item_ids: list[str]
    ambiguous: bool


class ActivePlaybackItem(TypedDict):
    key: str
    emby_item_id: str
    paths: list[str]


class PlaybackSnapshot(TypedDict, total=False):
    status: PlaybackStatus
    checked_at: str | None
    active_session_count: int
    active_item_count: int
    message: str
    items: list[ActivePlaybackItem]


class PlaybackResult(TypedDict, total=False):
    status: PlaybackStatus
    checked_at: str | None
    active_session_count: int
    active_item_count: int
    target_count: int
    clear_count: int
    active_count: int
    unverified_count: int
    deferred_count: int
    message: str
    _target_statuses: dict[str, TargetStatus]


SNAPSHOT_CACHE_SECONDS = 2
RUN_REFRESH_SECONDS = 5

_cache = {}
_cache_lock = threading.Lock()


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _summary(status, message, *, checked_at=None, sessions=0, items=0, active_items=None):
    return {
        "status": status,
        "checked_at": checked_at,
        "active_session_count": int(sessions or 0),
        "active_item_count": int(items or 0),
        "message": str(message or ""),
        "items": list(active_items or []),
    }


def public_snapshot(snapshot):
    snapshot = snapshot or {}
    return {
        "status": snapshot.get("status") or "unavailable",
        "checked_at": snapshot.get("checked_at"),
        "active_session_count": int(snapshot.get("active_session_count") or 0),
        "active_item_count": int(snapshot.get("active_item_count") or 0),
        "message": str(snapshot.get("message") or ""),
    }


def public_result(result):
    result = result or {}
    public = public_snapshot(result)
    public.update(
        {
            "target_count": int(result.get("target_count") or 0),
            "clear_count": int(result.get("clear_count") or 0),
            "active_count": int(result.get("active_count") or 0),
            "unverified_count": int(result.get("unverified_count") or 0),
            "deferred_count": int(result.get("deferred_count") or 0),
        }
    )
    return public


def _session_paths(session, now_playing):
    paths = [now_playing.get("Path")]
    for source in now_playing.get("MediaSources") or []:
        if isinstance(source, dict):
            paths.append(source.get("Path"))
    play_state = session.get("PlayState") or {}
    source = play_state.get("MediaSource") or {}
    if isinstance(source, dict):
        paths.append(source.get("Path"))
    return sorted({emby_catalog.normalize_path(path) for path in paths if emby_catalog.normalize_path(path)})


def _parse_sessions(data):
    if not isinstance(data, list):
        return None
    active_sessions = 0
    by_key = {}
    for session in data:
        if not isinstance(session, dict):
            continue
        now_playing = session.get("NowPlayingItem")
        if not isinstance(now_playing, dict):
            continue
        active_sessions += 1
        item_id = str(now_playing.get("Id") or "")
        paths = _session_paths(session, now_playing)
        key = f"id:{item_id}" if item_id else "paths:" + "|".join(paths)
        if not key or key == "paths:":
            key = f"unknown:{active_sessions}"
        entry = by_key.setdefault(
            key,
            {"key": key, "emby_item_id": item_id, "paths": []},
        )
        entry["paths"] = sorted(set(entry["paths"]) | set(paths))
    return active_sessions, list(by_key.values())


def load_snapshot(settings=None, *, opener=None, force=False, now=None):
    settings = dict(settings or app_settings.load_settings())
    if not settings.get("emby_playback_protection", True):
        return _summary("disabled", "Emby playback protection is disabled")
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return _summary("not_configured", "Configure Emby to check active playback")
    now = time.monotonic() if now is None else float(now)
    fingerprint = emby_catalog.configuration_fingerprint(settings)
    with _cache_lock:
        cached = _cache.get(fingerprint)
    if not force and cached and cached[0] > now:
        return dict(cached[1])
    data, request_result = emby_client.request_json(
        settings,
        "/Sessions",
        opener=opener,
        timeout=15,
    )
    parsed = _parse_sessions(data) if request_result.get("status") == "success" else None
    checked_at = request_result.get("checked_at") or utc_iso()
    if parsed is None:
        snapshot = _summary(
            "unavailable",
            request_result.get("message") or "Emby playback sessions are unavailable",
            checked_at=checked_at,
        )
    else:
        session_count, active_items = parsed
        status = "active" if session_count else "clear"
        message = (
            f"Emby reports {session_count} active playback session(s)"
            if session_count
            else "Emby reports no active playback"
        )
        snapshot = _summary(
            status,
            message,
            checked_at=checked_at,
            sessions=session_count,
            items=len(active_items),
            active_items=active_items,
        )
    with _cache_lock:
        _cache[fingerprint] = (now + SNAPSHOT_CACHE_SECONDS, snapshot)
    return dict(snapshot)


def _normalize_target(raw, index):
    raw = dict(raw or {})
    item_ids = {str(raw.get("emby_item_id") or "")}
    item_ids.update(str(value or "") for value in raw.get("emby_item_ids") or [])
    item_ids.discard("")
    return {
        "id": str(raw.get("id") or f"target-{index}"),
        "group_id": str(raw.get("group_id") or ""),
        "local_path": str(raw.get("local_path") or ""),
        "emby_item_ids": sorted(item_ids),
        "ambiguous": bool(raw.get("ambiguous")),
    }


def check_targets(targets, settings=None, *, opener=None, force=False, now=None):
    settings = dict(settings or app_settings.load_settings())
    normalized = [_normalize_target(target, index) for index, target in enumerate(targets or [])]
    snapshot = load_snapshot(settings, opener=opener, force=force, now=now)
    statuses = {}
    active_items = snapshot.get("items") or []
    active_ids = {item.get("emby_item_id") for item in active_items if item.get("emby_item_id")}
    active_paths = {path for item in active_items for path in item.get("paths") or []}
    mappings = settings.get("emby_path_mappings") or []
    for target in normalized:
        target_id = target["id"]
        if snapshot.get("status") in {"disabled", "not_configured"}:
            statuses[target_id] = "not_checked"
            continue
        if snapshot.get("status") == "unavailable":
            statuses[target_id] = "unverified"
            continue
        if not active_items:
            statuses[target_id] = "clear"
            continue
        if set(target["emby_item_ids"]) & active_ids:
            statuses[target_id] = "active"
            continue
        if target["emby_item_ids"] and active_items and all(item.get("emby_item_id") for item in active_items):
            statuses[target_id] = "clear"
            continue
        local = emby_catalog.normalize_path(target.get("local_path"))
        mapped = emby_catalog.mapped_emby_paths(target.get("local_path"), mappings)
        candidates = {value for value in [local, *mapped] if value}
        if candidates & active_paths:
            statuses[target_id] = "active"
        elif target.get("ambiguous") or len(mapped) > 1:
            statuses[target_id] = "unverified"
        else:
            statuses[target_id] = "clear"
    active_count = sum(value == "active" for value in statuses.values())
    unverified_count = sum(value == "unverified" for value in statuses.values())
    clear_count = sum(value in {"clear", "not_checked"} for value in statuses.values())
    result = {
        **snapshot,
        "target_count": len(normalized),
        "clear_count": clear_count,
        "active_count": active_count,
        "unverified_count": unverified_count,
        "deferred_count": active_count + unverified_count,
        "_target_statuses": statuses,
    }
    if active_count or unverified_count:
        result["message"] = (
            f"Playback protection will defer {active_count + unverified_count} of {len(normalized)} target(s)"
        )
    return result


def target_status(result, target_id):
    return str(((result or {}).get("_target_statuses") or {}).get(str(target_id or "")) or "not_checked")


def group_status(result, targets, group_id):
    statuses = {
        target_status(result, target.get("id"))
        for target in targets or []
        if str(target.get("group_id") or "") == str(group_id or "")
    }
    if "active" in statuses:
        return "active"
    if "unverified" in statuses:
        return "unverified"
    if "clear" in statuses:
        return "clear"
    return "not_checked"
