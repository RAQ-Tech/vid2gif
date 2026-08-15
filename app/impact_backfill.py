"""Recover lifetime impact totals from the maintenance audit logs.

The dashboard's lifetime figures only ever counted from the first launch after
impact tracking shipped, so an installation that had been cleaning up for months
showed a total that understated its real work. The audit logs under
`/state/maintenance-logs/` already record what every applied run did, so the part
of that history they preserve can be recovered.

What they preserve is not everything, and the difference matters:

* **Recoverable.** Every applied run wrote a summary naming its action
  (quarantine or delete), how many files it touched, and -- for most workstreams
  -- how many bytes. Those become the `operations` totals.
* **Not recoverable.** The per-issue graph (which finding was opened by which
  scan and closed by which action) was never written to the logs, so historical
  discovered/resolved counts cannot be rebuilt. Subtitle runs recorded their
  size only as a human-readable label, so their byte total is gone. Actor image
  imports write to Emby rather than the library, so they moved no files at all.
  GIF creation was never logged.

Nothing here estimates. A number that cannot be read out of a log is reported as
unrecovered rather than guessed at, and the dashboard says so instead of folding
a fabricated figure into the total.

Backfill is idempotent: every event carries a deterministic id derived from its
log, and `impact_metrics` refuses an id it has already processed. Running it
twice changes nothing.
"""

import json
import os

from . import impact_metrics
from .config import STATE_ROOT
from .progress import utc_iso


MAINTENANCE_LOG_ROOT = os.path.join(STATE_ROOT, "maintenance-logs")

# Which action words mean the file was moved somewhere recoverable, and which
# mean it is gone. Anything else counts as "other" so it is still represented.
QUARANTINE_ACTIONS = {"move", "quarantine", "rename"}
DELETE_ACTIONS = {"delete", "permanent_delete", "remove"}


def _iter_jsonl_headers(directory, suffix=".jsonl"):
    """Yield (log_id, first_record) for every log in a directory.

    The first line of these logs is always the summary header; the rest are
    per-file records that the backfill does not need.
    """
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for name in names:
        if not name.endswith(suffix) or name == "index.json":
            continue
        path = os.path.join(directory, name)
        try:
            with open(path, "r", encoding="utf-8") as handle:
                first = handle.readline()
        except OSError:
            continue
        if not first.strip():
            continue
        try:
            yield name, json.loads(first)
        except ValueError:
            continue


def _iter_json_entries(directory):
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return
    for name in names:
        if not name.endswith(".json") or name == "index.json":
            continue
        try:
            with open(os.path.join(directory, name), "r", encoding="utf-8") as handle:
                yield name, json.load(handle)
        except (OSError, ValueError):
            continue


def _operations_for(action, file_count, byte_count):
    action = str(action or "").strip().lower()
    files = max(0, int(file_count or 0))
    size = max(0, int(byte_count or 0))
    if not files:
        return {}
    if action in DELETE_ACTIONS:
        return {"deleted_files": files, "deleted_bytes": size}
    if action in QUARANTINE_ACTIONS:
        return {"quarantined_files": files, "quarantined_bytes": size}
    return {"other_files": files, "other_bytes": size}


def _collect_duplicates():
    directory = os.path.join(MAINTENANCE_LOG_ROOT, "duplicates")
    for log_id, header in _iter_jsonl_headers(directory):
        if header.get("type") != "summary":
            continue
        operations = _operations_for(
            header.get("action"),
            header.get("applied_count"),
            header.get("total_applied_bytes"),
        )
        if operations:
            yield {
                "category": "duplicates",
                "event_id": f"backfill:duplicates:{log_id}",
                "timestamp": header.get("timestamp"),
                "operations": operations,
                "label": f"Duplicate cleanup ({header.get('action') or 'applied'})",
            }


def _collect_video_previews():
    directory = os.path.join(MAINTENANCE_LOG_ROOT, "video-previews")
    for log_id, header in _iter_jsonl_headers(directory):
        # Quality repair is the only video-preview run that moves files;
        # generation writes new previews rather than touching the library.
        if header.get("type") != "quality-repair-summary":
            continue
        operations = _operations_for(
            header.get("action"),
            header.get("applied_count"),
            header.get("total_applied_bytes"),
        )
        if operations:
            yield {
                "category": "video_previews",
                "event_id": f"backfill:video_previews:{log_id}",
                "timestamp": header.get("timestamp"),
                "operations": operations,
                "label": f"BIF repair ({header.get('action') or 'applied'})",
            }


def _collect_subtitles():
    """Subtitle runs written before the log carried `applied_bytes`.

    Those older entries stored only a formatted size label, and reversing
    "12.0 KB" into a byte count would be a guess presented as a measurement.
    Their file counts are exact, so they are recovered with a zero byte total
    and reported as missing it. Logs written since carry the raw count and are
    recovered in full.
    """
    directory = os.path.join(MAINTENANCE_LOG_ROOT, "subtitles")
    for log_id, entry in _iter_json_entries(directory):
        has_bytes = "applied_bytes" in entry
        operations = _operations_for(
            entry.get("operation"),
            entry.get("applied_count"),
            entry.get("applied_bytes") if has_bytes else 0,
        )
        if operations:
            yield {
                "category": "subtitles",
                "event_id": f"backfill:subtitles:{log_id}",
                "timestamp": entry.get("created_at"),
                "operations": operations,
                "label": f"Subtitle cleanup ({entry.get('operation') or 'applied'})",
                "bytes_unrecovered": not has_bytes,
            }


def collect_events():
    """Every backfillable event the logs can support, oldest first."""
    events = [*_collect_duplicates(), *_collect_video_previews(), *_collect_subtitles()]
    events.sort(key=lambda event: str(event.get("timestamp") or ""))
    return events


def _count_logs(directory, suffix):
    try:
        return sum(1 for name in os.listdir(directory) if name.endswith(suffix) and name != "index.json")
    except OSError:
        return 0


def survey():
    """Describe what is on disk without changing anything."""
    return {
        "duplicate_logs": _count_logs(os.path.join(MAINTENANCE_LOG_ROOT, "duplicates"), ".jsonl"),
        "video_preview_logs": _count_logs(os.path.join(MAINTENANCE_LOG_ROOT, "video-previews"), ".jsonl"),
        "subtitle_logs": _count_logs(os.path.join(MAINTENANCE_LOG_ROOT, "subtitles"), ".json"),
        "actor_image_logs": _count_logs(os.path.join(MAINTENANCE_LOG_ROOT, "actor-images"), ".jsonl"),
    }


def run(now=None):
    """Replay the audit logs into the impact store.

    Returns a report naming what was recovered and what the logs could not
    support, so the dashboard can say so rather than implying the total is
    complete.
    """
    now = now or utc_iso()
    survey_counts = survey()
    events = collect_events()
    applied = 0
    already_recorded = 0
    files = 0
    bytes_recovered = 0
    bytes_unrecovered_runs = 0

    for event in events:
        recorded = impact_metrics.record_maintenance_action(
            event["event_id"],
            event["category"],
            operations=event["operations"],
            timestamp=event.get("timestamp") or now,
            label=event.get("label", ""),
        )
        operations = event["operations"]
        if recorded:
            applied += 1
            files += sum(value for key, value in operations.items() if key.endswith("_files"))
            bytes_recovered += sum(value for key, value in operations.items() if key.endswith("_bytes"))
            if event.get("bytes_unrecovered"):
                bytes_unrecovered_runs += 1
        else:
            already_recorded += 1

    return {
        "ran_at": now,
        "events_found": len(events),
        "events_applied": applied,
        "events_already_recorded": already_recorded,
        "files_recovered": files,
        "bytes_recovered": bytes_recovered,
        "runs_missing_byte_totals": bytes_unrecovered_runs,
        "survey": survey_counts,
        # Stated plainly so the dashboard never implies the total is complete.
        # Only the gaps that actually apply here, so the list stays a real
        # description of this install rather than boilerplate people learn to
        # scroll past.
        "not_recoverable": _gaps(bytes_unrecovered_runs, survey_counts),
    }


def _gaps(runs_missing_bytes, survey_counts):
    gaps = [
        "Issue discovery and resolution history: the logs record what each run"
        " did, not which finding it closed, so historical discovered and"
        " resolved counts cannot be rebuilt.",
        "GIF creation before tracking began: it was never written to an audit log.",
        "Anything older than the log retention limits, which keep the most"
        " recent runs per workstream and drop the rest.",
    ]
    if runs_missing_bytes:
        gaps.insert(
            1,
            f"Subtitle byte totals for {runs_missing_bytes} earlier"
            f" run{'' if runs_missing_bytes == 1 else 's'}: those logs recorded a"
            " formatted size label rather than a byte count. Runs since then record"
            " the count and are recovered in full.",
        )
    if survey_counts.get("actor_image_logs"):
        gaps.append(
            "Actor image imports: they upload to Emby and move no library files,"
            " so there is no file operation to recover."
        )
    return gaps


def ensure_backfilled(now=None):
    """Replay the logs once, the first time this install starts with the feature.

    Called from the entry points rather than from the dashboard read path: a
    page load should not trigger a filesystem sweep, and doing it at startup
    means the first dashboard someone opens already shows the recovered total.

    The stored report is what makes this once-only. The individual events are
    idempotent anyway, so a repeat would be harmless -- this just avoids
    re-reading every log on every boot.
    """
    if impact_metrics.get_backfill_report() is not None:
        return None
    report = run(now=now)
    impact_metrics.set_backfill_report(report)
    return report
