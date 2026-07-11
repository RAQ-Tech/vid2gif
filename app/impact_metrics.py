import copy
import datetime
import json
import os
import shutil
import threading

from .config import STATE_ROOT
from .progress import format_size, utc_iso


SCHEMA_VERSION = 1
IMPACT_ROOT = os.path.join(STATE_ROOT, "dashboard")
IMPACT_PATH = os.path.join(IMPACT_ROOT, "impact-metrics.json")
IMPACT_BACKUP_PATH = os.path.join(IMPACT_ROOT, "impact-metrics.json.bak")
RECENT_EVENT_LIMIT = 100
CATEGORY_DEFINITIONS = (
    ("duplicates", "Duplicates", "/maintenance#duplicates"),
    ("video_previews", "Video Previews", "/maintenance#video-previews"),
    ("subtitles", "Subtitles", "/maintenance#subtitles"),
    ("posters", "Landscape Posters", "/maintenance#posters"),
    ("actor_images", "Actor Images", "/maintenance#actor-images"),
)


impact_lock = threading.Lock()
_last_error = ""


def _default_category():
    return {
        "discovered_count": 0,
        "resolved_by_app_count": 0,
        "cleared_elsewhere_count": 0,
        "last_fix_at": None,
    }


def default_store(now=None):
    now = now or utc_iso()
    return {
        "schema_version": SCHEMA_VERSION,
        "tracking_started_at": now,
        "updated_at": now,
        "processed_events": {},
        "open_issues": {},
        "categories": {
            key: _default_category() for key, _title, _href in CATEGORY_DEFINITIONS
        },
        "operations": {
            "quarantined_files": 0,
            "quarantined_bytes": 0,
            "deleted_files": 0,
            "deleted_bytes": 0,
            "other_files": 0,
            "other_bytes": 0,
        },
        "daily": {},
        "recent_events": [],
        "creative_output": {
            "standard_gifs": 0,
            "test_lab_variants": 0,
            "output_bytes": 0,
            "optimization_saved_bytes": 0,
            "last_created_at": None,
        },
    }


def _normalise_store(data):
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported impact metrics schema")
    normal = default_store(data.get("tracking_started_at") or utc_iso())
    normal.update(data)
    normal["processed_events"] = dict(data.get("processed_events") or {})
    normal["open_issues"] = dict(data.get("open_issues") or {})
    normal["daily"] = dict(data.get("daily") or {})
    normal["recent_events"] = list(data.get("recent_events") or [])[:RECENT_EVENT_LIMIT]
    normal["operations"].update(data.get("operations") or {})
    normal["creative_output"].update(data.get("creative_output") or {})
    categories = {}
    for key, _title, _href in CATEGORY_DEFINITIONS:
        category = _default_category()
        category.update((data.get("categories") or {}).get(key) or {})
        categories[key] = category
    normal["categories"] = categories
    return normal


def _read_store_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        return _normalise_store(json.load(handle))


def _load_store(create=False):
    global _last_error
    if not os.path.exists(IMPACT_PATH):
        if os.path.exists(IMPACT_BACKUP_PATH):
            try:
                data = _read_store_file(IMPACT_BACKUP_PATH)
                _last_error = "Recovered impact metrics from the last-known-good backup"
                return data, True
            except Exception as exc:
                _last_error = f"Impact metrics backup could not be read: {exc}"
                raise
        if create:
            return default_store(), False
        return None, False
    try:
        data = _read_store_file(IMPACT_PATH)
        return data, False
    except Exception as primary_exc:
        try:
            data = _read_store_file(IMPACT_BACKUP_PATH)
            _last_error = "Recovered impact metrics from the last-known-good backup"
            return data, True
        except Exception:
            _last_error = f"Impact metrics could not be read: {primary_exc}"
            raise primary_exc


def _write_store(data):
    global _last_error
    os.makedirs(IMPACT_ROOT, exist_ok=True)
    data["schema_version"] = SCHEMA_VERSION
    data["updated_at"] = utc_iso()
    if os.path.isfile(IMPACT_PATH):
        try:
            _read_store_file(IMPACT_PATH)
            backup_tmp = f"{IMPACT_BACKUP_PATH}.{os.getpid()}.tmp"
            shutil.copyfile(IMPACT_PATH, backup_tmp)
            os.replace(backup_tmp, IMPACT_BACKUP_PATH)
        except Exception:
            pass
    tmp_path = f"{IMPACT_PATH}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"), sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp_path, IMPACT_PATH)
    _last_error = ""


def ensure_store():
    global _last_error
    with impact_lock:
        try:
            data, recovered = _load_store(create=True)
            if not os.path.exists(IMPACT_PATH) or recovered:
                _write_store(data)
            return True
        except Exception as exc:
            _last_error = str(exc)
            return False


def _event_seen(data, event_id):
    return bool(event_id and event_id in data.get("processed_events", {}))


def _mark_event(data, event_id, timestamp):
    data.setdefault("processed_events", {})[event_id] = timestamp


def _path_is_within(path, scope):
    try:
        path = os.path.normcase(os.path.realpath(path))
        scope = os.path.normcase(os.path.realpath(scope))
        return os.path.commonpath([path, scope]) == scope
    except (OSError, ValueError, TypeError):
        return False


def _category(data, key):
    return data.setdefault("categories", {}).setdefault(key, _default_category())


def _day_bucket(data, timestamp):
    day = str(timestamp or utc_iso())[:10]
    bucket = data.setdefault("daily", {}).setdefault(
        day,
        {"fixes": 0, "discovered": 0, "cleared_elsewhere": 0, "gifs_created": 0},
    )
    return bucket


def _close_issue(data, issue_id, resolution, timestamp):
    issue = data.setdefault("open_issues", {}).pop(issue_id, None)
    if not issue:
        return False
    category = _category(data, issue.get("category"))
    if resolution == "app":
        category["resolved_by_app_count"] += 1
        category["last_fix_at"] = timestamp
        _day_bucket(data, timestamp)["fixes"] += 1
    else:
        category["cleared_elsewhere_count"] += 1
        _day_bucket(data, timestamp)["cleared_elsewhere"] += 1
    return True


def _issue_sources(issue):
    return issue.setdefault("sources", {})


def _open_issue(data, category, issue, stream, timestamp):
    issue_id = str(issue.get("issue_id") or "")
    if not issue_id:
        return None, False
    open_issues = data.setdefault("open_issues", {})
    current = open_issues.get(issue_id)
    discovered = current is None
    if current is None:
        current = {
            "issue_id": issue_id,
            "category": category,
            "label": str(issue.get("label") or ""),
            "path": str(issue.get("path") or ""),
            "opened_at": timestamp,
            "last_seen_at": timestamp,
            "sources": {},
        }
        open_issues[issue_id] = current
        _category(data, category)["discovered_count"] += 1
        _day_bucket(data, timestamp)["discovered"] += 1
    else:
        current["last_seen_at"] = timestamp
        current["label"] = str(issue.get("label") or current.get("label") or "")
        current["path"] = str(issue.get("path") or current.get("path") or "")
    finding_ids = [str(value) for value in issue.get("finding_ids") or [] if str(value)]
    if not finding_ids:
        finding_ids = [issue_id]
    _issue_sources(current)[stream] = {
        "finding_ids": sorted(set(finding_ids)),
        "scope": str(issue.get("scope") or ""),
        "last_seen_at": timestamp,
    }
    return current, discovered


def record_scan(event_id, category, stream, scope, issues, timestamp=None):
    global _last_error
    timestamp = timestamp or utc_iso()
    event_id = f"scan:{category}:{stream}:{event_id}"
    with impact_lock:
        try:
            data, _recovered = _load_store(create=True)
            if _event_seen(data, event_id):
                return False
            found = {}
            for issue in issues or []:
                issue_id = str(issue.get("issue_id") or "")
                if issue_id:
                    if issue_id not in found:
                        found[issue_id] = dict(issue)
                    else:
                        merged = found[issue_id]
                        merged["finding_ids"] = list(merged.get("finding_ids") or []) + list(
                            issue.get("finding_ids") or []
                        )

            for issue_id, current in list(data.get("open_issues", {}).items()):
                if current.get("category") != category or issue_id in found:
                    continue
                source = (_issue_sources(current).get(stream) or {})
                issue_path = current.get("path") or source.get("scope") or ""
                if source and _path_is_within(issue_path, scope):
                    current["sources"].pop(stream, None)
                    if not current["sources"]:
                        _close_issue(data, issue_id, "external", timestamp)

            discovered_count = 0
            for issue in found.values():
                issue.setdefault("scope", scope)
                _current, discovered = _open_issue(data, category, issue, stream, timestamp)
                discovered_count += int(discovered)

            _mark_event(data, event_id, timestamp)
            if discovered_count:
                _add_recent_event(
                    data,
                    {
                        "id": event_id,
                        "kind": "scan",
                        "category": category,
                        "timestamp": timestamp,
                        "discovered_count": discovered_count,
                        "fix_count": 0,
                    },
                )
            _write_store(data)
            return True
        except Exception as exc:
            _last_error = str(exc)
            return False


def _add_recent_event(data, event):
    events = [item for item in data.setdefault("recent_events", []) if item.get("id") != event.get("id")]
    events.insert(0, event)
    data["recent_events"] = events[:RECENT_EVENT_LIMIT]


def record_maintenance_action(
    event_id,
    category,
    resolutions=None,
    operations=None,
    timestamp=None,
    label="",
):
    global _last_error
    timestamp = timestamp or utc_iso()
    event_id = f"action:{category}:{event_id}"
    operations = operations or {}
    with impact_lock:
        try:
            data, _recovered = _load_store(create=True)
            if _event_seen(data, event_id):
                return False
            fix_count = 0
            for resolution in resolutions or []:
                issue_id = str(resolution.get("issue_id") or "")
                if not issue_id:
                    continue
                current = data.get("open_issues", {}).get(issue_id)
                if current is None and resolution.get("ensure_issue"):
                    current, _discovered = _open_issue(
                        data,
                        category,
                        resolution,
                        str(resolution.get("stream") or category),
                        timestamp,
                    )
                if not current:
                    continue
                stream = str(resolution.get("stream") or "")
                if resolution.get("resolve_all"):
                    current["sources"] = {}
                elif stream in current.get("sources", {}):
                    finding_ids = {
                        str(value) for value in resolution.get("finding_ids") or [] if str(value)
                    }
                    if not finding_ids:
                        current["sources"].pop(stream, None)
                    else:
                        source = current["sources"][stream]
                        remaining = [
                            value for value in source.get("finding_ids") or [] if value not in finding_ids
                        ]
                        if remaining:
                            source["finding_ids"] = remaining
                        else:
                            current["sources"].pop(stream, None)
                if not current.get("sources"):
                    fix_count += int(_close_issue(data, issue_id, "app", timestamp))

            op_totals = data.setdefault("operations", {})
            for key in (
                "quarantined_files",
                "quarantined_bytes",
                "deleted_files",
                "deleted_bytes",
                "other_files",
                "other_bytes",
            ):
                op_totals[key] = int(op_totals.get(key) or 0) + max(0, int(operations.get(key) or 0))
            _mark_event(data, event_id, timestamp)
            if fix_count or any(int(value or 0) for value in operations.values()):
                _add_recent_event(
                    data,
                    {
                        "id": event_id,
                        "kind": "maintenance",
                        "category": category,
                        "timestamp": timestamp,
                        "label": str(label or ""),
                        "fix_count": fix_count,
                        "file_count": sum(
                            int(operations.get(key) or 0)
                            for key in ("quarantined_files", "deleted_files", "other_files")
                        ),
                    },
                )
            _write_store(data)
            return True
        except Exception as exc:
            _last_error = str(exc)
            return False


def record_creative_output(event_id, kind, output_bytes=0, saved_bytes=0, timestamp=None):
    global _last_error
    timestamp = timestamp or utc_iso()
    event_id = f"creative:{kind}:{event_id}"
    if kind not in {"standard", "test_lab"}:
        return False
    with impact_lock:
        try:
            data, _recovered = _load_store(create=True)
            if _event_seen(data, event_id):
                return False
            creative = data.setdefault("creative_output", {})
            key = "standard_gifs" if kind == "standard" else "test_lab_variants"
            creative[key] = int(creative.get(key) or 0) + 1
            creative["output_bytes"] = int(creative.get("output_bytes") or 0) + max(0, int(output_bytes or 0))
            creative["optimization_saved_bytes"] = int(creative.get("optimization_saved_bytes") or 0) + max(0, int(saved_bytes or 0))
            creative["last_created_at"] = timestamp
            _day_bucket(data, timestamp)["gifs_created"] += 1
            _mark_event(data, event_id, timestamp)
            _write_store(data)
            return True
        except Exception as exc:
            _last_error = str(exc)
            return False


def _resolution_percent(resolved, discovered):
    if not discovered:
        return 0
    return max(0, min(100, int(round(100 * resolved / discovered))))


def _milestones(total_fixes, categories):
    thresholds = (1, 10, 25, 50, 100, 250, 500, 1000)
    earned = [
        {"key": f"fixes-{target}", "label": "First Fix" if target == 1 else f"{target:,} Fixes", "target": target}
        for target in thresholds
        if total_fixes >= target
    ]
    for category in categories:
        if category["resolved_count"]:
            earned.append(
                {
                    "key": f"category-{category['key']}",
                    "label": f"First {category['title']} Fix",
                    "target": 1,
                    "category": category["key"],
                }
            )
    next_target = next((target for target in thresholds if total_fixes < target), None)
    next_item = None
    if next_target:
        previous = max((target for target in thresholds if target <= total_fixes), default=0)
        span = max(1, next_target - previous)
        next_item = {
            "label": "First Fix" if next_target == 1 else f"{next_target:,} Fixes",
            "target": next_target,
            "current": total_fixes,
            "progress_percent": max(0, min(100, int(round(100 * (total_fixes - previous) / span)))),
        }
    return {"earned": earned, "latest": earned[-1] if earned else None, "next": next_item}


def _daily_series(data, days=30, now=None):
    now = now or datetime.datetime.now(datetime.timezone.utc)
    series = []
    for offset in range(days - 1, -1, -1):
        date = (now - datetime.timedelta(days=offset)).date().isoformat()
        bucket = (data.get("daily") or {}).get(date) or {}
        series.append(
            {
                "date": date,
                "fixes": int(bucket.get("fixes") or 0),
                "discovered": int(bucket.get("discovered") or 0),
                "gifs_created": int(bucket.get("gifs_created") or 0),
            }
        )
    return series


def status_payload(now=None):
    global _last_error
    with impact_lock:
        try:
            data, recovered = _load_store(create=True)
            if not os.path.exists(IMPACT_PATH) or recovered:
                recovery_warning = _last_error if recovered else ""
                _write_store(data)
                if recovery_warning:
                    _last_error = recovery_warning
            error = _last_error
        except Exception as exc:
            return {
                "status": "error",
                "error": str(exc),
                "tracking_started_at": None,
                "total_fixes": 0,
                "discovered_count": 0,
                "resolved_count": 0,
                "cleared_elsewhere_count": 0,
                "open_count": 0,
                "resolution_percent": 0,
                "categories": [],
                "operations": {},
                "daily": [],
                "milestones": {"earned": [], "latest": None, "next": None},
                "recent_events": [],
                "creative_output": {},
            }

    open_counts = {}
    for issue in data.get("open_issues", {}).values():
        key = issue.get("category")
        open_counts[key] = open_counts.get(key, 0) + 1
    categories = []
    for key, title, href in CATEGORY_DEFINITIONS:
        values = data.get("categories", {}).get(key) or {}
        discovered = int(values.get("discovered_count") or 0)
        resolved = int(values.get("resolved_by_app_count") or 0)
        cleared = int(values.get("cleared_elsewhere_count") or 0)
        categories.append(
            {
                "key": key,
                "title": title,
                "href": href,
                "discovered_count": discovered,
                "resolved_count": resolved,
                "cleared_elsewhere_count": cleared,
                "open_count": int(open_counts.get(key) or 0),
                "resolution_percent": _resolution_percent(resolved, discovered),
                "last_fix_at": values.get("last_fix_at"),
            }
        )
    total_fixes = sum(item["resolved_count"] for item in categories)
    discovered = sum(item["discovered_count"] for item in categories)
    cleared = sum(item["cleared_elsewhere_count"] for item in categories)
    open_count = sum(item["open_count"] for item in categories)
    operations = copy.deepcopy(data.get("operations") or {})
    operations.update(
        {
            "quarantined_size_label": format_size(operations.get("quarantined_bytes")),
            "deleted_size_label": format_size(operations.get("deleted_bytes")),
        }
    )
    creative = copy.deepcopy(data.get("creative_output") or {})
    creative.update(
        {
            "output_size_label": format_size(creative.get("output_bytes")),
            "optimization_saved_label": format_size(creative.get("optimization_saved_bytes")),
        }
    )
    return {
        "status": "warning" if error else "ok",
        "error": error,
        "tracking_started_at": data.get("tracking_started_at"),
        "updated_at": data.get("updated_at"),
        "total_fixes": total_fixes,
        "discovered_count": discovered,
        "resolved_count": total_fixes,
        "cleared_elsewhere_count": cleared,
        "open_count": open_count,
        "resolution_percent": _resolution_percent(total_fixes, discovered),
        "categories": categories,
        "operations": operations,
        "daily": _daily_series(data, now=now),
        "milestones": _milestones(total_fixes, categories),
        "recent_events": copy.deepcopy(data.get("recent_events") or []),
        "creative_output": creative,
    }
