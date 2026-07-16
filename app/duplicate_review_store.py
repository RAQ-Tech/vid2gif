import copy
import json
import os
import threading

from . import config
from .progress import utc_iso


SCHEMA_VERSION = 1
_lock = threading.Lock()


def _path():
    return os.path.join(config.STATE_ROOT, "maintenance-duplicates", "review-drafts.json")


def _empty():
    return {"schema_version": SCHEMA_VERSION, "scopes": {}}


def _read_locked():
    try:
        with open(_path(), "r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return _empty()
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        return _empty()
    if not isinstance(value.get("scopes"), dict):
        value["scopes"] = {}
    return value


def _write_locked(value):
    path = _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    temporary = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def _scope_key(scan):
    path = os.path.normcase(os.path.realpath(str((scan or {}).get("path") or "")))
    return str((scan or {}).get("review_scope_key") or path)


def _scope_locked(value, scan, create=False):
    key = _scope_key(scan)
    scopes = value.setdefault("scopes", {})
    scope = scopes.get(key)
    if scope is None and create:
        scope = {
            "path": os.path.realpath(str((scan or {}).get("path") or "")),
            "selection_mode": "all_eligible",
            "excluded_review_keys": [],
            "selected_review_keys": [],
            "groups": {},
            "updated_at": utc_iso(),
        }
        scopes[key] = scope
    return scope


def _group_maps(scan):
    groups = list((scan or {}).get("groups") or [])
    by_id = {str(group.get("id") or ""): group for group in groups if group.get("id")}
    by_key = {
        str(group.get("review_key") or ""): group
        for group in groups
        if group.get("review_key")
    }
    return by_id, by_key


def _folder_key(value):
    return os.path.normcase(os.path.realpath(str(value or "")))


def _review_state(group, record):
    record = copy.deepcopy(record) if isinstance(record, dict) else None
    if not record:
        return {
            "saved": False,
            "requires_review": False,
            "status": "default",
            "reason": "",
        }
    current_fingerprint = str(group.get("folder_fingerprint") or "")
    current_settings = str(group.get("settings_fingerprint") or "")
    acknowledged = str(record.get("acknowledged_fingerprint") or "")
    saved_settings = str(record.get("settings_fingerprint") or "")
    folder_changed = bool(acknowledged and current_fingerprint != acknowledged)
    settings_changed = bool(saved_settings and current_settings != saved_settings)
    forced_review = bool(record.get("review_required"))
    requires_review = folder_changed or settings_changed or forced_review
    if settings_changed:
        reason = "Duplicate cleanup settings changed after this review"
    elif folder_changed:
        reason = "Files in this folder changed after this review"
    elif forced_review:
        reason = str(record.get("review_reason") or "The duplicate groups in this folder changed")
    else:
        reason = ""
    record.update(
        {
            "saved": True,
            "requires_review": requires_review,
            "status": "review_required" if requires_review else "saved",
            "reason": reason,
        }
    )
    return record


def mapped_payload(scan):
    with _lock:
        value = _read_locked()
        scope = copy.deepcopy(_scope_locked(value, scan) or {})
    by_id, by_key = _group_maps(scan)
    records = scope.get("groups") if isinstance(scope.get("groups"), dict) else {}
    mapped_groups = {}
    for review_key, record in records.items():
        group = by_key.get(str(review_key or ""))
        if not group:
            continue
        mapped_groups[group["id"]] = _review_state(group, record)
    unmatched_folders = {
        _folder_key(record.get("folder"))
        for review_key, record in records.items()
        if isinstance(record, dict)
        and review_key not in by_key
        and record.get("folder")
    }
    for group in by_id.values():
        if group["id"] in mapped_groups or _folder_key(group.get("folder")) not in unmatched_folders:
            continue
        mapped_groups[group["id"]] = {
            "saved": True,
            "requires_review": True,
            "status": "review_required",
            "reason": "Duplicate groups in this folder changed after the saved review",
            "enabled": True,
            "keep_video_id": "",
            "include_file_ids": [],
            "known_file_ids": [],
            "file_operations": [],
        }
    scan_settings_fingerprint = str((scan or {}).get("settings_fingerprint") or "")
    if scan_settings_fingerprint:
        for group in by_id.values():
            if str(group.get("settings_fingerprint") or "") == scan_settings_fingerprint:
                continue
            state = mapped_groups.setdefault(
                group["id"],
                {
                    "saved": False,
                    "enabled": True,
                    "keep_video_id": "",
                    "include_file_ids": [],
                    "known_file_ids": [],
                    "file_operations": [],
                },
            )
            state.update(
                {
                    "requires_review": True,
                    "status": "review_required",
                    "reason": "Duplicate cleanup settings changed after this group was scanned",
                }
            )

    excluded_keys = set(scope.get("excluded_review_keys") or [])
    selected_keys = set(scope.get("selected_review_keys") or [])
    return {
        "scan_id": str((scan or {}).get("id") or ""),
        "saved": bool(scope),
        "updated_at": scope.get("updated_at"),
        "selection": {
            "mode": "explicit" if scope.get("selection_mode") == "explicit" else "all_eligible",
            "excluded_group_ids": [
                group["id"] for key, group in by_key.items() if key in excluded_keys
            ],
            "group_ids": [
                group["id"] for key, group in by_key.items() if key in selected_keys
            ],
        },
        "groups": mapped_groups,
        "saved_group_count": sum(1 for state in mapped_groups.values() if state.get("saved")),
        "review_required_count": sum(
            1 for state in mapped_groups.values() if state.get("requires_review")
        ),
    }


def group_state(scan, group, mapped=None):
    mapped = mapped if isinstance(mapped, dict) else mapped_payload(scan)
    return copy.deepcopy((mapped.get("groups") or {}).get(group.get("id")) or {
        "saved": False,
        "requires_review": False,
        "status": "default",
        "reason": "",
    })


def patch(scan, payload):
    payload = payload if isinstance(payload, dict) else {}
    by_id, by_key = _group_maps(scan)
    with _lock:
        value = _read_locked()
        scope = _scope_locked(value, scan, create=True)

        selection = payload.get("selection")
        if isinstance(selection, dict):
            mode = "explicit" if selection.get("mode") == "explicit" else "all_eligible"
            scope["selection_mode"] = mode
            excluded = []
            for group_id in selection.get("excluded_group_ids") or []:
                group = by_id.get(str(group_id or ""))
                if group and group.get("review_key"):
                    excluded.append(group["review_key"])
            selected = []
            for group_id in selection.get("group_ids") or []:
                group = by_id.get(str(group_id or ""))
                if group and group.get("review_key"):
                    selected.append(group["review_key"])
            scope["excluded_review_keys"] = sorted(set(excluded))
            scope["selected_review_keys"] = sorted(set(selected))

        records = scope.setdefault("groups", {})
        for submitted in payload.get("groups") or []:
            if not isinstance(submitted, dict):
                continue
            group = by_id.get(str(submitted.get("id") or ""))
            if not group:
                group = by_key.get(str(submitted.get("review_key") or ""))
            if not group or not group.get("review_key"):
                continue
            review_key = group["review_key"]
            existing = records.get(review_key) if isinstance(records.get(review_key), dict) else {}
            known_file_ids = {
                str(item)
                for item in submitted.get("known_file_ids") or existing.get("known_file_ids") or []
                if str(item or "")
            }
            record = {
                "folder": _folder_key(group.get("folder")),
                "enabled": bool(submitted.get("enabled", existing.get("enabled", True))),
                "keep_video_id": str(
                    submitted.get("keep_video_id") or existing.get("keep_video_id") or ""
                ),
                "include_file_ids": sorted(
                    {
                        str(item)
                        for item in submitted.get("include_file_ids", existing.get("include_file_ids") or [])
                        if str(item or "")
                    }
                ),
                "known_file_ids": sorted(known_file_ids),
                "file_operations": [
                    {
                        "file_id": str(item.get("file_id") or ""),
                        "operation": str(item.get("operation") or "default"),
                    }
                    for item in submitted.get("file_operations", existing.get("file_operations") or [])
                    if isinstance(item, dict) and item.get("file_id")
                ],
                "acknowledged_fingerprint": str(
                    group.get("folder_fingerprint")
                    if submitted.get("accept_current") or not existing
                    else existing.get("acknowledged_fingerprint") or ""
                ),
                "settings_fingerprint": str(
                    group.get("settings_fingerprint")
                    if submitted.get("accept_current") or not existing
                    else existing.get("settings_fingerprint") or ""
                ),
                "review_required": (
                    False
                    if submitted.get("accept_current")
                    else bool(submitted.get("require_review", existing.get("review_required", False)))
                ),
                "review_reason": str(
                    submitted.get("review_reason") or existing.get("review_reason") or ""
                ),
                "updated_at": utc_iso(),
            }
            records[review_key] = record

        scope["updated_at"] = utc_iso()
        _write_locked(value)
    return mapped_payload(scan)


def ensure_groups(scan, group_ids, overrides=None, require_review=None, review_reason=""):
    by_id, _by_key = _group_maps(scan)
    overrides = overrides if isinstance(overrides, dict) else {}
    mapped = mapped_payload(scan)
    submitted = []
    for group_id in group_ids or []:
        group = by_id.get(str(group_id or ""))
        if not group:
            continue
        saved = (mapped.get("groups") or {}).get(group["id"]) or {}
        override = dict(saved)
        override.update(overrides.get(group["id"], {}) if isinstance(overrides, dict) else {})
        videos = list(group.get("videos") or [])
        keep_id = str(override.get("keep_video_id") or group.get("recommended_keep_id") or "")
        candidates = []
        for video in videos:
            if video.get("id") != keep_id:
                candidates.append(video)
            candidates.extend(video.get("accessories") or [])
        known_ids = [item.get("id") for item in candidates if item.get("id")]
        included = override.get("include_file_ids")
        if not isinstance(included, list):
            included = [
                item.get("id") for item in candidates
                if item.get("default_selected") is not False
                and item.get("default_operation") != "keep"
            ]
        elif saved:
            previous_known = {str(item) for item in saved.get("known_file_ids") or []}
            included = set(included)
            included.update(
                item.get("id") for item in candidates
                if item.get("id") not in previous_known
                and item.get("default_selected") is not False
                and item.get("default_operation") != "keep"
            )
            included = sorted(item for item in included if item)
        item = {
            "id": group["id"],
            "enabled": bool(override.get("enabled", True)),
            "keep_video_id": keep_id,
            "include_file_ids": included,
            "known_file_ids": known_ids,
            "file_operations": override.get("file_operations") or [],
        }
        if require_review is not None:
            item["require_review"] = bool(require_review)
            item["review_reason"] = review_reason
        submitted.append(item)
    if submitted:
        patch(scan, {"groups": submitted})


def remove_review_keys(scan, review_keys):
    keys = {str(item or "") for item in review_keys or [] if str(item or "")}
    if not keys:
        return
    with _lock:
        value = _read_locked()
        scope = _scope_locked(value, scan)
        if not scope:
            return
        records = scope.get("groups") if isinstance(scope.get("groups"), dict) else {}
        for key in keys:
            records.pop(key, None)
        scope["excluded_review_keys"] = [
            key for key in scope.get("excluded_review_keys") or [] if key not in keys
        ]
        scope["selected_review_keys"] = [
            key for key in scope.get("selected_review_keys") or [] if key not in keys
        ]
        scope["updated_at"] = utc_iso()
        _write_locked(value)


def delete(scan):
    with _lock:
        value = _read_locked()
        removed = value.setdefault("scopes", {}).pop(_scope_key(scan), None)
        if removed is not None:
            _write_locked(value)
    return bool(removed)
