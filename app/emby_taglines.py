"""Copy each Emby item's title into its tagline, cleaned of episode markers.

The operator's Emby skin displays the tagline as the title on detail pages, so
every item needs its tagline filled with a cleaned copy of its name -- season
and episode markers such as ``S03E12``, ``s4e01`` or ``s03,e9`` stripped out.
Until now that meant stopping the Emby container and editing its SQLite file by
hand, which is exactly the kind of intervention that once corrupted the
database.

This does the same work over Emby's own HTTP API -- the channel the metadata
editor in Emby's web interface uses -- so Emby performs its own database writes
while running, and the container never stops. Both the tagline and the title
receive the cleaned text, with one hard rule: the title is only edited once the
original, markers and all, is safe in ``OriginalTitle``. An empty original gets
the copy written first; an original already mirroring the title is the copy; an
original holding anything else (a real original-language title, say) means the
title is left alone and only the tagline is written, flagged in review. Emby's
per-item metadata lock is set so a library refresh cannot undo any of it.

The shape follows every other workstream: a scan classifies the library, the
operator reviews exactly what would change, and a separate apply executes the
reviewed plan. Every applied change records the tagline and lock state as they
were immediately before the write, and undo restores precisely those fields
onto a fresh copy of the item -- so undoing an old run cannot clobber edits
made since.
"""

import datetime
import json
import os
import re
import threading
import time
import urllib.parse

from . import app_settings
from . import emby_client
from .config import STATE_ROOT
from .progress import utc_iso
from .utils import path_is_under


LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "emby-taglines")
LOG_INDEX = os.path.join(LOG_DIR, "index.json")
LOG_RETENTION_COUNT = 25
LOG_MAX_BYTES = 4 * 1024 * 1024
SCAN_RETENTION_COUNT = 3
RUN_RETENTION_COUNT = 10
RETENTION_MAX_AGE_SECONDS = 24 * 60 * 60
ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}

SWEEP_ITEM_TYPES = "Movie,Episode,Video"
# LockData may not be an official Fields value; servers that ignore it just
# omit the flag, in which case an already-locked item shows as ready and
# re-applying it is a harmless identical write.
SWEEP_FIELDS = "Path,Taglines,OriginalTitle,LockData,LockedFields"

# The compact marker forms the operator strips by hand: S03E12, s4e01, s03,e9,
# s03 - e12, S01E01E02 double episodes -- plus the spelled-out pair. The season
# number must follow the "s" immediately, or "Ocean's 11" would match "s 11".
_MARKER_RE = re.compile(
    r"\bs\d{1,3}\s*[.,_\-\s]?\s*e\d{1,4}(?:\s*[-,&+]?\s*e\d{1,4})*\b"
    r"|\bseason\s*\d{1,3}\s*[.,_\-\s]*episode\s*\d{1,4}\b",
    re.IGNORECASE,
)

taglines_lock = threading.Lock()
tagline_scans = {}
tagline_plans = {}
tagline_runs = {}


def clean_title(title):
    """The tagline a title becomes: markers removed, punctuation tidied."""
    text = str(title or "")
    cleaned = _MARKER_RE.sub(" ", text)
    if cleaned == text:
        return re.sub(r"\s+", " ", text).strip()
    # Removal can strand what surrounded the marker: empty brackets, doubled
    # separators, a dangling dash at either end.
    cleaned = re.sub(r"\(\s*\)|\[\s*\]|\{\s*\}", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)
    cleaned = re.sub(r"(?:\s*[-|]\s*){2,}", " - ", cleaned)
    cleaned = re.sub(r",\s*,+", ",", cleaned)
    cleaned = re.sub(r"^[\s\-_.,:;|]+", "", cleaned)
    cleaned = re.sub(r"[\s\-_.,:;|]+$", "", cleaned)
    return cleaned.strip()


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _first_tagline(raw):
    taglines = raw.get("Taglines")
    if isinstance(taglines, list) and taglines:
        return str(taglines[0] or "").strip()
    return ""


def classify_item(raw, lock_items=True):
    """One library row and what, if anything, needs writing.

    Two rules keep the scan honest against a library the operator has already
    worked through by hand for years:

    * Their protection scheme counts. The manual workflow locked individual
      fields (``LockedFields``); this workflow sets the whole-item lock
      (``LockData``). Either one means a metadata refresh will not undo the
      work, so either one satisfies the check.
    * A missing lock is never "work". An item whose tagline and title are
      already correct is reported as ``unlocked`` -- visible under its own
      filter, but not counted as ready and not selected for writing. The first
      version flagged the operator's entire finished library as ready purely
      to add a lock, which read as 6,886 items to do when the true backlog was
      a few hundred.
    """
    name = str(raw.get("Name") or "").strip()
    proposed = clean_title(name)
    current = _first_tagline(raw)
    original = str(raw.get("OriginalTitle") or "").strip()
    locked_fields = raw.get("LockedFields")
    locked = bool(raw.get("LockData")) or bool(isinstance(locked_fields, list) and locked_fields)

    title_differs = bool(proposed) and proposed != name
    # The title may only be edited once the original is safe. An empty
    # original-title field receives the copy; one already mirroring the title
    # is the copy; anything else is real data the edit must not displace.
    if not title_differs:
        original_backup = "not-needed"
        writes_title = False
    elif not original:
        original_backup = "will-copy"
        writes_title = True
    elif original == name:
        original_backup = "existing-copy"
        writes_title = True
    else:
        original_backup = "occupied"
        writes_title = False

    entry = {
        "id": str(raw.get("Id") or ""),
        "name": name,
        "type": str(raw.get("Type") or "").strip(),
        "path": str(raw.get("Path") or "").strip(),
        "current_tagline": current,
        "proposed_tagline": proposed,
        "original_title": original,
        "marker_removed": proposed != re.sub(r"\s+", " ", name).strip(),
        "title_differs": title_differs,
        "writes_title": writes_title,
        "original_backup": original_backup,
        "lock_data": locked,
    }
    tagline_ok = re.sub(r"\s+", " ", current).strip() == proposed
    pending_title = title_differs and writes_title
    if not proposed:
        entry["status"] = "unusable"
        entry["detail"] = "Nothing would be left after removing the markers"
    elif not tagline_ok or pending_title:
        entry["status"] = "ready"
        if pending_title and not tagline_ok:
            entry["detail"] = "Title and tagline will be written"
        elif pending_title:
            entry["detail"] = "Tagline is set; the title still needs cleaning"
        elif title_differs:
            entry["detail"] = "Tagline only: the original-title field is in use"
        else:
            entry["detail"] = "Tagline will be written"
    elif lock_items and not locked:
        entry["status"] = "unlocked"
        entry["detail"] = "Text is correct; the item is not locked against metadata refreshes"
    else:
        entry["status"] = "done"
        entry["detail"] = (
            "Title held: the original-title field is in use" if title_differs else "Tagline already matches"
        )
    return entry


def _prune_locked(store, keep, now=None):
    now = now or time.time()
    entries = sorted(store.items(), key=lambda pair: pair[1].get("_created_ts") or 0, reverse=True)
    for index, (key, value) in enumerate(entries):
        terminal = value.get("status") not in SCAN_ACTIVE_STATUSES
        expired = (now - (value.get("_created_ts") or 0)) > RETENTION_MAX_AGE_SECONDS
        if terminal and (index >= keep or expired):
            store.pop(key, None)


def public_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    return {
        "id": scan.get("id", ""),
        "status": scan.get("status", ""),
        "active": scan.get("status") in SCAN_ACTIVE_STATUSES,
        "progress_label": scan.get("progress_label", ""),
        "item_total": scan.get("item_total", 0),
        "counts": dict(counts),
        "lock_items": bool(scan.get("lock_items")),
        "error": scan.get("error", ""),
        "created_at": scan.get("created_at"),
        "finished_at": scan.get("finished_at"),
    }


def start_scan(settings=None, synchronous=False, opener=None):
    settings = settings or app_settings.load_settings()
    if not settings.get("emby_url") or not settings.get("emby_api_key"):
        return None, "Emby is not configured. Add the server URL and API key on the Settings page."
    lock_items = bool(settings.get("emby_tagline_lock_items", True))
    scan_id = _now_id()
    created = time.time()
    scan = {
        "id": scan_id,
        "status": "queued",
        "progress_label": "Waiting to start",
        "item_total": 0,
        "counts": {"ready": 0, "done": 0, "unusable": 0, "unlocked": 0, "needs_title": 0, "needs_tagline": 0},
        "items": [],
        "lock_items": lock_items,
        "error": "",
        "created_at": utc_iso(created),
        "finished_at": None,
        "_created_ts": created,
    }
    with taglines_lock:
        _prune_locked(tagline_scans, SCAN_RETENTION_COUNT)
        active = next((s for s in tagline_scans.values() if s.get("status") in SCAN_ACTIVE_STATUSES), None)
        if active:
            return active, None
        tagline_scans[scan_id] = scan

    def work():
        scan["status"] = "running"
        scan["progress_label"] = "Reading titles from Emby"
        fetched = {"count": 0}

        def before_page():
            scan["progress_label"] = f"Reading titles from Emby ({fetched['count']} so far)"

        items, outcome = emby_client.request_paged_json(
            settings,
            "/Items",
            params={"Recursive": "true", "IncludeItemTypes": SWEEP_ITEM_TYPES, "Fields": SWEEP_FIELDS},
            opener=opener,
            timeout=60,
            before_page=before_page,
        )
        if outcome.get("status") != "success" or items is None:
            scan.update(
                status="failed",
                error=outcome.get("message") or "Emby could not be read",
                progress_label="Scan failed",
                finished_at=utc_iso(),
            )
            return
        fetched["count"] = len(items)
        entries = [classify_item(raw, lock_items) for raw in items if isinstance(raw, dict) and raw.get("Id")]
        counts = {"ready": 0, "done": 0, "unusable": 0, "unlocked": 0, "needs_title": 0, "needs_tagline": 0}
        for entry in entries:
            counts[entry["status"]] += 1
            if entry["status"] == "ready":
                # The breakdown that makes a large number legible: how much is
                # the one-time title catch-up versus new taglines.
                if entry["title_differs"] and entry["writes_title"]:
                    counts["needs_title"] += 1
                if "Tagline will be written" in entry["detail"] or "Title and tagline" in entry["detail"]:
                    counts["needs_tagline"] += 1
        scan.update(
            items=entries,
            item_total=len(entries),
            counts=counts,
            status="success",
            progress_label=f"{counts['ready']} ready, {counts['done']} already done",
            finished_at=utc_iso(),
        )

    if synchronous:
        work()
    else:
        threading.Thread(target=work, daemon=True, name=f"vid2gif-emby-taglines-{scan_id}").start()
    return scan, None


def scan_status(scan_id=None):
    with taglines_lock:
        if scan_id:
            scan = tagline_scans.get(str(scan_id))
        else:
            scans = sorted(tagline_scans.values(), key=lambda s: s.get("_created_ts") or 0, reverse=True)
            scan = scans[0] if scans else None
    return {"scan": public_scan(scan)}


def items_payload(scan_id, status="ready", offset=0, limit=ITEM_PAGE_DEFAULT):
    with taglines_lock:
        scan = tagline_scans.get(str(scan_id or ""))
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    try:
        offset = max(0, int(offset or 0))
        limit = max(1, min(ITEM_PAGE_MAX, int(limit or ITEM_PAGE_DEFAULT)))
    except (TypeError, ValueError):
        offset, limit = 0, ITEM_PAGE_DEFAULT
    status = str(status or "ready")
    items = scan.get("items") or []
    if status != "all":
        items = [item for item in items if item.get("status") == status]
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "status": status,
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(page),
        "has_previous": offset > 0,
        "has_next": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "items": page,
    }, None


def build_plan(payload):
    """Resolve a selection into the exact writes an apply would perform."""
    if not isinstance(payload, dict):
        return None, "Plan payload is invalid"
    scan_id = str(payload.get("scan_id") or "")
    with taglines_lock:
        scan = tagline_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"

    eligible = {item["id"]: item for item in scan.get("items") or [] if item.get("status") == "ready"}
    selection = payload.get("selection") if isinstance(payload.get("selection"), dict) else {}
    if selection.get("mode") == "all_eligible":
        excluded = {str(v) for v in selection.get("excluded_item_ids") or []}
        chosen = [item for item_id, item in eligible.items() if item_id not in excluded]
    else:
        wanted = {str(v) for v in selection.get("item_ids") or []}
        chosen = [item for item_id, item in eligible.items() if item_id in wanted]
    if not chosen:
        return None, "Nothing is selected"

    plan_id = _now_id()
    created = time.time()
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "status": "ready",
        "lock_items": bool(scan.get("lock_items")),
        "item_count": len(chosen),
        "items": [
            {
                "id": item["id"],
                "name": item["name"],
                "current_tagline": item["current_tagline"],
                "proposed_tagline": item["proposed_tagline"],
            }
            for item in chosen
        ],
        "created_at": utc_iso(created),
        "_created_ts": created,
    }
    with taglines_lock:
        _prune_locked(tagline_plans, RUN_RETENTION_COUNT)
        tagline_plans[plan_id] = plan
    return plan, None


def public_plan(plan):
    if not plan:
        return None
    return {
        "id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "status": plan.get("status", ""),
        "item_count": plan.get("item_count", 0),
        "lock_items": bool(plan.get("lock_items")),
        "preview": [
            {"name": item["name"], "proposed_tagline": item["proposed_tagline"]} for item in plan.get("items", [])[:5]
        ],
        "created_at": plan.get("created_at"),
    }


def _item_endpoint(settings, item_id):
    user_id = str(settings.get("emby_user_id") or "").strip()
    quoted = urllib.parse.quote(str(item_id))
    # The user-scoped endpoint returns the fuller item the metadata editor
    # itself round-trips, so prefer it when an account is configured.
    return f"/Users/{urllib.parse.quote(user_id)}/Items/{quoted}" if user_id else f"/Items/{quoted}"


def _item_state(raw):
    """Exactly what an apply may change, captured for the undo log."""
    return {
        "name": str(raw.get("Name") or ""),
        "original_title": str(raw.get("OriginalTitle") or ""),
        "taglines": list(raw.get("Taglines") or []),
        "lock_data": bool(raw.get("LockData")),
    }


def _write_item(settings, item_id, body, opener=None):
    return emby_client.request_no_content(
        settings,
        f"/Items/{urllib.parse.quote(str(item_id))}",
        json_body=body,
        opener=opener,
        timeout=30,
    )


def public_run(run):
    if not run:
        return None
    return {
        "id": run.get("id", ""),
        "plan_id": run.get("plan_id", ""),
        "kind": run.get("kind", "apply"),
        "status": run.get("status", ""),
        "active": run.get("status") in SCAN_ACTIVE_STATUSES,
        "progress_label": run.get("progress_label", ""),
        "item_count": run.get("item_count", 0),
        "processed_count": run.get("processed_count", 0),
        "applied_count": run.get("applied_count", 0),
        "refused_count": run.get("refused_count", 0),
        "failed_count": run.get("failed_count", 0),
        "current_name": run.get("current_name", ""),
        "log_id": run.get("log_id", ""),
        "error": run.get("error", ""),
        "created_at": run.get("created_at"),
        "finished_at": run.get("finished_at"),
    }


def start_apply(plan_id, settings=None, synchronous=False, opener=None):
    settings = settings or app_settings.load_settings()
    with taglines_lock:
        plan = tagline_plans.get(str(plan_id or ""))
        if not plan:
            return None, "Plan not found"
        if plan.get("status") == "applied":
            return None, "Plan was already applied"
        active = next((r for r in tagline_runs.values() if r.get("status") in SCAN_ACTIVE_STATUSES), None)
        if active:
            return active, None
        run_id = _now_id()
        created = time.time()
        run = {
            "id": run_id,
            "plan_id": plan["id"],
            "kind": "apply",
            "status": "queued",
            "progress_label": "Waiting to start",
            "item_count": plan["item_count"],
            "processed_count": 0,
            "applied_count": 0,
            "refused_count": 0,
            "failed_count": 0,
            "current_name": "",
            "log_id": "",
            "error": "",
            "cancel_requested": False,
            "created_at": utc_iso(created),
            "finished_at": None,
            "_created_ts": created,
        }
        _prune_locked(tagline_runs, RUN_RETENTION_COUNT)
        tagline_runs[run_id] = run
        plan["status"] = "applying"

    lock_items = bool(plan.get("lock_items"))

    def work():
        run["status"] = "running"
        records = []
        for item in plan.get("items") or []:
            if run.get("cancel_requested"):
                run["status"] = "cancelled"
                break
            run["current_name"] = item["name"]
            run["progress_label"] = f"Updating {item['name']}"
            record = {
                "type": "item",
                "item_id": item["id"],
                "name": item["name"],
                "timestamp": utc_iso(),
            }
            raw, outcome = emby_client.request_json(
                settings, _item_endpoint(settings, item["id"]), opener=opener, timeout=30
            )
            if outcome.get("status") != "success" or not isinstance(raw, dict):
                record.update(status="failed", detail=outcome.get("message") or "Emby item could not be read")
                run["failed_count"] += 1
            elif not raw.get("Id") or not raw.get("Name"):
                # A partial item posted back would blank what the GET omitted.
                record.update(status="refused", detail="Emby returned an incomplete item")
                run["refused_count"] += 1
            elif clean_title(raw.get("Name")) != item["proposed_tagline"]:
                record.update(status="refused", detail="Title changed after the scan")
                run["refused_count"] += 1
            else:
                record["before"] = _item_state(raw)
                proposed = item["proposed_tagline"]
                body = dict(raw)
                body["Taglines"] = [proposed]
                if lock_items:
                    body["LockData"] = True
                detail = ""
                name_now = str(raw.get("Name") or "")
                if name_now != proposed:
                    original = str(raw.get("OriginalTitle") or "").strip()
                    if not original or original == name_now:
                        # Preserve the marker-bearing title before replacing it;
                        # the copy and the edit land in the same write.
                        body["OriginalTitle"] = name_now
                        body["Name"] = proposed
                    else:
                        detail = "Title left unchanged: the original-title field already holds something else"
                write = _write_item(settings, item["id"], body, opener=opener)
                if write.get("status") == "success":
                    record["after"] = _item_state(body)
                    record.update(status="applied", detail=detail)
                    run["applied_count"] += 1
                else:
                    record.update(status="failed", detail=write.get("message") or "Emby rejected the update")
                    run["failed_count"] += 1
            records.append(record)
            run["processed_count"] += 1
        else:
            run["status"] = "success"
        run["current_name"] = ""
        header = {
            "type": "summary",
            "kind": "apply",
            "timestamp": utc_iso(),
            "run_id": run["id"],
            "plan_id": plan["id"],
            "lock_items": lock_items,
            "item_count": run["item_count"],
            "applied_count": run["applied_count"],
            "refused_count": run["refused_count"],
            "failed_count": run["failed_count"],
        }
        run["log_id"] = _write_log("apply", header, records)
        run["progress_label"] = (
            f"{run['applied_count']} updated, {run['refused_count']} refused, {run['failed_count']} failed"
        )
        run["finished_at"] = utc_iso()
        with taglines_lock:
            plan["status"] = "applied" if run["status"] == "success" else plan["status"]

    if synchronous:
        work()
    else:
        threading.Thread(target=work, daemon=True, name=f"vid2gif-emby-taglines-apply-{run['id']}").start()
    return run, None


def start_undo(log_id, settings=None, synchronous=False, opener=None):
    """Put back the taglines and locks a logged apply changed.

    Restoration is surgical: the recorded before-state is written onto a fresh
    copy of each item, so anything edited in Emby since the apply survives.
    """
    settings = settings or app_settings.load_settings()
    entry, records, err = read_log(str(log_id or ""))
    if err:
        return None, err
    applied = [record for record in records if record.get("status") == "applied" and record.get("before")]
    if not applied:
        return None, "This log has nothing to undo"
    with taglines_lock:
        active = next((r for r in tagline_runs.values() if r.get("status") in SCAN_ACTIVE_STATUSES), None)
        if active:
            return active, None
        run_id = _now_id()
        created = time.time()
        run = {
            "id": run_id,
            "plan_id": entry.get("plan_id", ""),
            "kind": "undo",
            "status": "queued",
            "progress_label": "Waiting to start",
            "item_count": len(applied),
            "processed_count": 0,
            "applied_count": 0,
            "refused_count": 0,
            "failed_count": 0,
            "current_name": "",
            "log_id": "",
            "error": "",
            "cancel_requested": False,
            "created_at": utc_iso(created),
            "finished_at": None,
            "_created_ts": created,
        }
        _prune_locked(tagline_runs, RUN_RETENTION_COUNT)
        tagline_runs[run_id] = run

    def work():
        run["status"] = "running"
        undo_records = []
        for record in applied:
            if run.get("cancel_requested"):
                run["status"] = "cancelled"
                break
            run["current_name"] = record.get("name", "")
            run["progress_label"] = f"Restoring {record.get('name', '')}"
            item_id = record.get("item_id", "")
            undo_record = {
                "type": "item",
                "item_id": item_id,
                "name": record.get("name", ""),
                "timestamp": utc_iso(),
            }
            raw, outcome = emby_client.request_json(
                settings, _item_endpoint(settings, item_id), opener=opener, timeout=30
            )
            if outcome.get("status") != "success" or not isinstance(raw, dict) or not raw.get("Id"):
                undo_record.update(status="failed", detail=outcome.get("message") or "Emby item could not be read")
                run["failed_count"] += 1
            else:
                before = record.get("before") or {}
                body = dict(raw)
                body["Name"] = before.get("name") or raw.get("Name")
                body["OriginalTitle"] = before.get("original_title", "")
                body["Taglines"] = list(before.get("taglines") or [])
                body["LockData"] = bool(before.get("lock_data"))
                write = _write_item(settings, item_id, body, opener=opener)
                if write.get("status") == "success":
                    undo_record.update(status="applied", detail="", restored=before)
                    run["applied_count"] += 1
                else:
                    undo_record.update(status="failed", detail=write.get("message") or "Emby rejected the update")
                    run["failed_count"] += 1
            undo_records.append(undo_record)
            run["processed_count"] += 1
        else:
            run["status"] = "success"
        run["current_name"] = ""
        header = {
            "type": "summary",
            "kind": "undo",
            "timestamp": utc_iso(),
            "run_id": run["id"],
            "undoes_log_id": str(log_id),
            "item_count": run["item_count"],
            "applied_count": run["applied_count"],
            "failed_count": run["failed_count"],
        }
        run["log_id"] = _write_log("undo", header, undo_records)
        run["progress_label"] = f"{run['applied_count']} restored, {run['failed_count']} failed"
        run["finished_at"] = utc_iso()

    if synchronous:
        work()
    else:
        threading.Thread(target=work, daemon=True, name=f"vid2gif-emby-taglines-undo-{run['id']}").start()
    return run, None


def run_status(run_id=None):
    with taglines_lock:
        if run_id:
            run = tagline_runs.get(str(run_id))
        else:
            runs = sorted(tagline_runs.values(), key=lambda r: r.get("_created_ts") or 0, reverse=True)
            run = runs[0] if runs else None
    return {"run": public_run(run)}


def cancel_run(run_id=None):
    with taglines_lock:
        if run_id:
            run = tagline_runs.get(str(run_id))
        else:
            runs = sorted(tagline_runs.values(), key=lambda r: r.get("_created_ts") or 0, reverse=True)
            run = runs[0] if runs else None
        if not run:
            return None, "Run not found"
        if run.get("status") in SCAN_ACTIVE_STATUSES:
            run["cancel_requested"] = True
            run["status"] = "cancelling"
    return public_run(run), None


# --- logs -------------------------------------------------------------------


def _write_log(kind, header, records):
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        log_id = f"{_now_id()}-{kind}.jsonl"
        path = os.path.join(LOG_DIR, log_id)
        written = 0
        with open(path, "w", encoding="utf-8") as handle:
            for record in [header, *records]:
                line = json.dumps(record, ensure_ascii=False) + "\n"
                size = len(line.encode("utf-8"))
                if written + size > LOG_MAX_BYTES:
                    handle.write(json.dumps({"type": "truncated", "timestamp": utc_iso()}) + "\n")
                    break
                handle.write(line)
                written += size
        index = _read_index()
        entry = {
            "id": log_id,
            "path": path,
            "kind": kind,
            "created_at": header.get("timestamp"),
            "plan_id": header.get("plan_id", ""),
            "item_count": header.get("item_count", 0),
            "applied_count": header.get("applied_count", 0),
            "refused_count": header.get("refused_count", 0),
            "failed_count": header.get("failed_count", 0),
        }
        logs = [item for item in index.get("logs", []) if item.get("id") != log_id]
        logs.insert(0, entry)
        for old in logs[LOG_RETENTION_COUNT:]:
            try:
                os.remove(old.get("path", ""))
            except OSError:
                pass
        _write_index({"logs": logs[:LOG_RETENTION_COUNT]})
        return log_id
    except OSError:
        # The Emby writes already happened; a missing log only loses the undo
        # for this run, which the run payload reports via an empty log_id.
        return ""


def _read_index():
    try:
        with open(LOG_INDEX, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"logs": []}
    return data if isinstance(data, dict) and isinstance(data.get("logs"), list) else {"logs": []}


def _write_index(data):
    tmp = f"{LOG_INDEX}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, indent=1)
    os.replace(tmp, LOG_INDEX)


def list_logs():
    logs = []
    for entry in _read_index().get("logs", []):
        public = {key: value for key, value in entry.items() if key != "path"}
        logs.append(public)
    return {"logs": logs}


def read_log(log_id):
    index = _read_index()
    entry = next((item for item in index.get("logs", []) if item.get("id") == str(log_id)), None)
    if not entry:
        return None, None, "Log not found"
    path = str(entry.get("path") or "")
    if not path or not path_is_under(path, LOG_DIR) or not os.path.isfile(path):
        return None, None, "Log file is missing"
    records = []
    header = {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "summary":
                    header = record
                elif record.get("type") == "item":
                    records.append(record)
    except OSError:
        return None, None, "Log could not be read"
    return {**{k: v for k, v in entry.items() if k != "path"}, **header}, records, None
