import datetime
import hashlib
import json
import os
import re
import threading
import time

from . import app_settings
from . import emby_catalog
from . import emby_playback
from . import emby_sync
from . import emby_notifications
from . import impact_metrics
from . import maintenance_scan_store
from . import task_progress
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .file_safety import atomic_quarantine_file
from .operation_gate import coordinated_library_operation
from .progress import format_size, utc_iso
from .table_sort import sort_records
from .utils import path_is_under, resolve_case_insensitive


ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
LARGE_RESULT_COUNT = 100
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
SCAN_RETENTION_COUNT = 10
SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
SUBTITLE_EXTS = {".srt"}
SUBTITLE_QUARANTINE_DIRNAME = ".vid2gif-subtitle-quarantine"
LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "subtitles")
LOG_INDEX = os.path.join(LOG_DIR, "index.json")
KNOWN_SKIP_DIRS = {
    ".vid2gif-duplicates",
    ".vid2gif-video-preview-repairs",
    SUBTITLE_QUARANTINE_DIRNAME,
    "_previews",
    "__pycache__",
}
LANGUAGE_TOKEN_RE = re.compile(r"^[a-z]{2,3}(?:[-_][a-z]{2,4})?$", re.IGNORECASE)
LANGUAGE_MODIFIER_TOKENS = {
    "cc",
    "default",
    "forced",
    "foreign",
    "sdh",
    "sign",
    "signs",
    "song",
    "songs",
}
__test__ = False

subtitle_scans = {}
_subtitle_cache_loaded = False
subtitle_plans = {}
subtitle_apply_runs = {}
subtitle_lock = threading.Lock()


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


def _relative_path(path, root):
    try:
        return os.path.relpath(os.path.realpath(path), os.path.realpath(root))
    except (OSError, ValueError):
        return os.path.basename(path)


def _safe_stat(path):
    try:
        return os.stat(path)
    except OSError:
        return None


def _stat_identity(path):
    stat = _safe_stat(path)
    if not stat:
        return None
    return {
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "inode": getattr(stat, "st_ino", 0),
        "device": getattr(stat, "st_dev", 0),
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


def normalize_language_code(value):
    return app_settings.normalize_language_code(value)


def expected_language_set(settings=None):
    settings = settings or app_settings.load_settings()
    return set(app_settings.parse_subtitle_languages(settings.get("subtitle_expected_languages")))


def subtitle_matches_video(subtitle_name, video_stem):
    stem, ext = os.path.splitext(str(subtitle_name or ""))
    if ext.lower() not in SUBTITLE_EXTS:
        return False
    lower_stem = stem.lower()
    lower_video = str(video_stem or "").lower()
    if not lower_video:
        return False
    if lower_stem == lower_video:
        return True
    if not lower_stem.startswith(lower_video):
        return False
    return lower_stem[len(lower_video)] in {".", "-", "_", " ", "["}


def subtitle_language_code(subtitle_name, video_stem, allow_subgen=True):
    stem, ext = os.path.splitext(str(subtitle_name or ""))
    if ext.lower() not in SUBTITLE_EXTS or not subtitle_matches_video(subtitle_name, video_stem):
        return None
    suffix = stem[len(str(video_stem or "")) :].strip(" .-_[]")
    if not suffix:
        return None
    tokens = [token for token in re.split(r"[.\s]+", suffix) if token]
    if not allow_subgen and any(token.lower() == "subgen" for token in tokens):
        return None
    for token in reversed(tokens):
        normalized = normalize_language_code(token.strip(" -_[]()"))
        if not normalized or normalized in LANGUAGE_MODIFIER_TOKENS:
            continue
        if LANGUAGE_TOKEN_RE.match(normalized):
            return normalized
        return None
    return None


def _public_subtitle(path, video_stem, lib_root, settings=None):
    stat = _safe_stat(path)
    settings = settings or app_settings.load_settings()
    code = subtitle_language_code(
        os.path.basename(path),
        video_stem,
        allow_subgen=bool(settings.get("subtitle_subgen_detection", True)),
    )
    return {
        "id": _path_id(path, lib_root),
        "name": os.path.basename(path),
        "path": os.path.realpath(path),
        "relative_path": _relative_path(path, lib_root),
        "language_code": code or "",
        "language_status": "unknown" if not code else "detected",
        "size_bytes": stat.st_size if stat else 0,
        "size_label": format_size(stat.st_size if stat else 0),
        "modified_at": utc_iso(stat.st_mtime) if stat else None,
        "identity": _stat_identity(path) or {},
        "actionable": False,
        "action_reason": "",
    }


def _classify_video(video_path, folder_files, settings, lib_root):
    name = os.path.basename(video_path)
    stem = os.path.splitext(name)[0]
    folder = os.path.dirname(video_path)
    expected = expected_language_set(settings)
    subtitles = []
    for entry in folder_files:
        full_path = os.path.join(folder, entry)
        if os.path.islink(full_path) or not os.path.isfile(full_path):
            continue
        if subtitle_matches_video(entry, stem):
            subtitles.append(_public_subtitle(full_path, stem, lib_root, settings))
    subtitles.sort(key=lambda item: item["name"].lower())

    flag_missing = bool(settings.get("subtitle_flag_missing", True))
    flag_unknown = bool(settings.get("subtitle_flag_unknown_language", True))
    non_expected = [
        item for item in subtitles
        if item.get("language_code") and item.get("language_code") not in expected
    ]
    unknown = [item for item in subtitles if not item.get("language_code")]
    expected_matches = [
        item for item in subtitles
        if item.get("language_code") and item.get("language_code") in expected
    ]
    for subtitle in subtitles:
        code = subtitle.get("language_code")
        if code and code not in expected:
            subtitle["actionable"] = True
            subtitle["action_reason"] = f"Unexpected language: {code}"
        elif not code and flag_unknown:
            subtitle["actionable"] = True
            subtitle["action_reason"] = "Language could not be identified"

    status = "ok"
    detail = "Expected subtitle language found"
    if not subtitles:
        if flag_missing:
            status = "missing"
            detail = "No matching SRT file found beside the video"
        else:
            detail = "No matching SRT found, but missing subtitle checks are disabled"
    elif non_expected:
        status = "language_review"
        codes = ", ".join(sorted({item["language_code"] for item in non_expected}))
        detail = f"Subtitle language code needs review: {codes}"
    elif unknown and flag_unknown:
        status = "unknown"
        detail = "Subtitle file found without a clear language code"
    elif expected_matches:
        detail = "Expected subtitle language found"
    else:
        detail = "Subtitle file found"

    stat = _safe_stat(video_path)
    return {
        "id": _path_id(video_path, lib_root),
        "path": os.path.realpath(video_path),
        "relative_path": _relative_path(video_path, lib_root),
        "folder": os.path.realpath(folder),
        "name": name,
        "status": status,
        "detail": detail,
        "subtitle_count": len(subtitles),
        "srt_files": subtitles,
        "language_codes": sorted(
            {item.get("language_code") or "unknown" for item in subtitles}
        ),
        "size_bytes": stat.st_size if stat else 0,
        "size_label": format_size(stat.st_size if stat else 0),
        "modified_at": utc_iso(stat.st_mtime) if stat else None,
    }


def _skip_dir(base, dirname, lib_root):
    path = os.path.join(base, dirname)
    if os.path.islink(path):
        return True
    if dirname in KNOWN_SKIP_DIRS:
        return True
    settings = app_settings.load_settings()
    move_root = os.path.realpath(settings.get("duplicate_move_root") or "")
    if move_root and path_is_under(path, move_root) and path_is_under(move_root, lib_root):
        return True
    return False


def _scan_cancel_requested(scan):
    if not scan:
        return False
    with subtitle_lock:
        return bool(scan.get("cancel_requested"))


def _check_cancelled(scan):
    if _scan_cancel_requested(scan):
        raise ScanCancelled()


def _set_scan_progress(scan, percent, label, **values):
    with subtitle_lock:
        task_progress.update_scan(scan, "subtitle_scan", percent, label, **values)


def _scan_videos(scan, settings, lib_root):
    items = []
    scanned = 0
    path = scan["path"]
    for base, dirs, files in os.walk(path, followlinks=False):
        _check_cancelled(scan)
        dirs[:] = [dirname for dirname in dirs if not _skip_dir(base, dirname, lib_root)]
        videos = [
            filename
            for filename in files
            if os.path.splitext(filename)[1].lower() in VIDEO_EXTS
        ]
        for filename in sorted(videos, key=str.lower):
            _check_cancelled(scan)
            video_path = os.path.join(base, filename)
            if os.path.islink(video_path) or not os.path.isfile(video_path):
                continue
            items.append(_classify_video(video_path, files, settings, lib_root))
            scanned += 1
            if scanned % 25 == 0:
                _set_scan_progress(
                    scan,
                    min(95, 5 + scanned // 10),
                    f"Scanned {scanned} videos",
                    scanned_video_count=scanned,
                )
    items.sort(key=lambda item: item["relative_path"].lower())
    return items


def _counts(items):
    counts = {
        "scanned_video_count": len(items),
        "missing_count": 0,
        "language_review_count": 0,
        "unknown_count": 0,
        "ok_count": 0,
        "subtitle_file_count": 0,
        "review_count": 0,
    }
    for item in items:
        status = item.get("status")
        counts["subtitle_file_count"] += len(item.get("srt_files") or [])
        key = f"{status}_count"
        if key in counts:
            counts[key] += 1
    counts["review_count"] = (
        counts["missing_count"]
        + counts["language_review_count"]
        + counts["unknown_count"]
    )
    return counts


def _stream_summary(status="not_checked", message="Emby subtitle streams were not checked; rescan to add stream details.", settings=None):
    return {
        "status": status,
        "checked_at": None,
        "server_id": "",
        "catalog_item_count": 0,
        "checked_video_count": 0,
        "stream_count": 0,
        "embedded_count": 0,
        "external_count": 0,
        "forced_count": 0,
        "hearing_impaired_count": 0,
        "index_mismatch_count": 0,
        "message": message,
        "_configuration_fingerprint": emby_catalog.configuration_fingerprint(settings or {}),
    }


def public_stream_summary(summary, settings=None):
    summary = summary if isinstance(summary, dict) else _stream_summary(settings=settings)
    public = {key: value for key, value in summary.items() if not str(key).startswith("_")}
    fingerprint = summary.get("_configuration_fingerprint")
    if settings is not None and fingerprint and fingerprint != emby_catalog.configuration_fingerprint(settings):
        public["status"] = "stale"
        public["message"] = "Emby settings changed after this scan; rescan to refresh subtitle streams."
    return public


def _stream_local_candidates(stream, mappings):
    raw = str((stream or {}).get("_path") or "")
    values = [raw, *emby_catalog.mapped_local_paths(raw, mappings)] if raw else []
    return {emby_catalog.normalize_path(value) for value in values if emby_catalog.normalize_path(value)}


def _classify_with_emby(item, settings):
    expected = expected_language_set(settings)
    sidecars = item.get("srt_files") or []
    streams = item.get("emby_subtitle_streams") or []
    actionable_non_expected = any(
        sidecar.get("actionable") and sidecar.get("language_code")
        for sidecar in sidecars
    )
    actionable_unknown = any(
        sidecar.get("actionable") and not sidecar.get("language_code")
        for sidecar in sidecars
    )
    known_languages = {
        str(value or "")
        for value in [
            *(sidecar.get("language_code") for sidecar in sidecars),
            *(stream.get("language_code") for stream in streams),
        ]
        if value
    }
    has_any = bool(sidecars or streams)
    if actionable_non_expected:
        item.update(status="language_review", detail="Filesystem subtitle language needs review")
    elif actionable_unknown:
        item.update(status="unknown", detail="Filesystem subtitle language could not be identified")
    elif known_languages & expected:
        source = "Emby stream" if not any(sidecar.get("language_code") in expected for sidecar in sidecars) else "subtitle"
        item.update(status="ok", detail=f"Expected {source} language found")
    elif known_languages:
        item.update(
            status="language_review",
            detail=f"Indexed subtitle language needs review: {', '.join(sorted(known_languages))}",
        )
    elif has_any and bool(settings.get("subtitle_flag_unknown_language", True)):
        item.update(status="unknown", detail="Subtitle found without a clear language code")
    elif not has_any and bool(settings.get("subtitle_flag_missing", True)):
        item.update(status="missing", detail="No filesystem or indexed subtitle found")
    else:
        item.update(status="ok", detail="No actionable subtitle issue")
    item["language_codes"] = sorted(known_languages or {"unknown"} if has_any else set())


def _enrich_subtitle_streams(items, settings, catalog, catalog_summary):
    mappings = settings.get("emby_path_mappings") or []
    summary = _stream_summary(
        catalog_summary.get("status") or "unavailable",
        catalog_summary.get("message") or "Emby subtitle streams are unavailable.",
        settings,
    )
    summary.update(
        checked_at=catalog_summary.get("checked_at"),
        server_id=catalog_summary.get("server_id", ""),
        catalog_item_count=catalog_summary.get("catalog_item_count", 0),
    )
    incomplete = False
    for item in items:
        for sidecar in item.get("srt_files") or []:
            sidecar.update(
                emby_stream_index=None,
                emby_stream_flags=[],
                emby_stream_match_status="not_checked",
            )
        item.update(
            emby_subtitle_streams=[],
            emby_subtitle_stream_count=0,
            embedded_subtitle_count=0,
            external_subtitle_count=0,
            emby_index_status="not_checked",
        )
        if item.get("emby_match_status") != "matched" or not catalog:
            incomplete = True
            continue
        source = emby_catalog.subtitle_streams_for_path(
            catalog,
            item.get("emby_item_id"),
            item.get("path"),
            mappings,
        )
        source_status = source.get("status")
        if source_status != "complete":
            incomplete = True
        raw_streams = source.get("streams") or []
        public_streams = []
        matched_sidecars = set()
        mismatch = source_status == "ambiguous"
        sidecars = item.get("srt_files") or []
        sidecar_paths = {
            emby_catalog.normalize_path(sidecar.get("path")): sidecar
            for sidecar in sidecars
            if emby_catalog.normalize_path(sidecar.get("path"))
        }
        for stream in raw_streams:
            public_stream = emby_catalog.public_subtitle_stream(stream)
            match_status = "not_applicable"
            if stream.get("is_external"):
                candidates = _stream_local_candidates(stream, mappings)
                matches = [sidecar_paths[path] for path in candidates if path in sidecar_paths]
                unique_matches = {sidecar.get("id"): sidecar for sidecar in matches}
                if len(unique_matches) == 1:
                    sidecar = next(iter(unique_matches.values()))
                    match_status = "matched"
                    matched_sidecars.add(sidecar.get("id"))
                    flags = [
                        label
                        for enabled, label in (
                            (stream.get("is_forced"), "forced"),
                            (stream.get("is_hearing_impaired"), "hearing_impaired"),
                            (stream.get("is_default"), "default"),
                        )
                        if enabled
                    ]
                    sidecar.update(
                        emby_stream_index=stream.get("index"),
                        emby_stream_flags=flags,
                        emby_stream_match_status="matched",
                    )
                elif len(unique_matches) > 1:
                    match_status = "ambiguous"
                    mismatch = True
                else:
                    match_status = "unmatched"
                    mismatch = True
            public_stream["path_match_status"] = match_status
            public_streams.append(public_stream)
        if source_status == "complete":
            for sidecar in sidecars:
                if sidecar.get("id") not in matched_sidecars:
                    sidecar["emby_stream_match_status"] = "unmatched"
                    mismatch = True
        item.update(
            emby_subtitle_streams=public_streams,
            emby_subtitle_stream_count=len(public_streams),
            embedded_subtitle_count=sum(not stream.get("is_external") for stream in public_streams),
            external_subtitle_count=sum(bool(stream.get("is_external")) for stream in public_streams),
            emby_index_status="mismatch" if mismatch else source_status,
        )
        if source_status == "complete":
            summary["checked_video_count"] += 1
        summary["stream_count"] += len(public_streams)
        summary["embedded_count"] += item["embedded_subtitle_count"]
        summary["external_count"] += item["external_subtitle_count"]
        summary["forced_count"] += sum(bool(stream.get("is_forced")) for stream in public_streams)
        summary["hearing_impaired_count"] += sum(bool(stream.get("is_hearing_impaired")) for stream in public_streams)
        summary["index_mismatch_count"] += int(mismatch)
        _classify_with_emby(item, settings)
    if catalog_summary.get("status") in {"not_configured", "unavailable"}:
        summary["status"] = catalog_summary.get("status")
    else:
        summary["status"] = "partial" if incomplete else "complete"
        summary["message"] = (
            f"Checked Emby subtitle streams for {summary['checked_video_count']} video(s); "
            f"{summary['index_mismatch_count']} index mismatch(es)."
        )
    return summary


def public_settings(settings=None):
    settings = settings or app_settings.load_settings()
    return {
        "expected_languages": app_settings.parse_subtitle_languages(
            settings.get("subtitle_expected_languages")
        ),
        "flag_missing": bool(settings.get("subtitle_flag_missing", True)),
        "flag_unknown_language": bool(settings.get("subtitle_flag_unknown_language", True)),
        "subgen_detection": bool(settings.get("subtitle_subgen_detection", True)),
    }


def public_scan(scan):
    if not scan:
        return None
    counts = scan.get("counts") or {}
    review_count = counts.get("review_count", 0)
    public = {
        "id": scan.get("id", ""),
        "path": scan.get("path", ""),
        "status": scan.get("status", ""),
        **task_progress.public_fields(scan),
        "error": scan.get("error", ""),
        "created_at": scan.get("created_at"),
        "started_at": scan.get("started_at"),
        "finished_at": scan.get("finished_at"),
        "active": scan.get("status") in SCAN_ACTIVE_STATUSES,
        "cancel_requested": bool(scan.get("cancel_requested")),
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": review_count >= LARGE_RESULT_COUNT,
        "settings": public_settings(scan.get("settings") or app_settings.load_settings()),
        **counts,
        "emby_mapping": emby_catalog.public_summary(
            scan.get("emby_mapping"), app_settings.load_settings()
        ),
        "emby_streams": public_stream_summary(
            scan.get("emby_streams"), app_settings.load_settings()
        ),
    }
    public.update(maintenance_scan_store.public_cache_metadata("subtitles", scan))
    return public


def _ensure_cache_loaded():
    global _subtitle_cache_loaded
    if _subtitle_cache_loaded:
        return
    restored = maintenance_scan_store.restore_scan("subtitles")
    with subtitle_lock:
        if restored and restored.get("id") not in subtitle_scans:
            subtitle_scans[restored["id"]] = restored
        _subtitle_cache_loaded = True


def _prune_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(subtitle_scans):
        scan = subtitle_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if not scan.get("_persisted_latest") and now - finished > SCAN_MAX_AGE_SECONDS:
            subtitle_scans.pop(scan_id, None)
    terminal = sorted(
        (
            (scan_id, scan)
            for scan_id, scan in subtitle_scans.items()
            if scan.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[SCAN_RETENTION_COUNT:]:
        subtitle_scans.pop(scan_id, None)


def _active_scan_locked():
    active = [
        scan
        for scan in subtitle_scans.values()
        if scan.get("status") in SCAN_ACTIVE_STATUSES
    ]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


@coordinated_library_operation(
    "Scan subtitle health", kind="scan", href="/maintenance#subtitles"
)
def _run_scan(scan, settings, lib_root):
    try:
        started = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning subtitle sidecars",
            status="running",
            _started_ts=started,
            started_at=utc_iso(started),
        )
        items = _scan_videos(scan, settings, lib_root)
        emby_mapping = emby_catalog.enrich_records(
            items,
            app_settings.load_settings(),
            lambda item: item.get("path"),
            before_page=lambda: _check_cancelled(scan),
        )
        catalog, catalog_summary = emby_catalog.load_catalog(
            app_settings.load_settings(),
            before_page=lambda: _check_cancelled(scan),
        )
        emby_streams = _enrich_subtitle_streams(
            items,
            settings,
            catalog,
            catalog_summary,
        )
        counts = _counts(items)
        for item in items:
            for subtitle in item.get("srt_files") or []:
                subtitle["emby_parent_item_id"] = item.get("emby_item_id", "")
                subtitle["emby_parent_item_type"] = item.get("emby_item_type", "")
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            f"{counts['review_count']} review items, {counts['ok_count']} OK",
            status="success",
            items=items,
            counts=counts,
            scanned_video_count=counts["scanned_video_count"],
            emby_mapping=emby_mapping,
            emby_streams=emby_streams,
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        impact_metrics.record_scan(
            scan["id"],
            "subtitles",
            "subtitles",
            scan["path"],
            [
                {
                    "issue_id": f"subtitle:{subtitle.get('id')}",
                    "finding_ids": [subtitle.get("id")],
                    "label": subtitle.get("name") or "Flagged subtitle",
                    "path": subtitle.get("path") or scan["path"],
                }
                for item in items
                for subtitle in item.get("srt_files") or []
                if subtitle.get("actionable")
            ],
            timestamp=utc_iso(finished),
        )
        persisted = maintenance_scan_store.persist_success(
            "subtitles", "subtitles", scan, lib_root
        )
        if persisted:
            with subtitle_lock:
                for candidate in subtitle_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
    except ScanCancelled:
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            "Scan cancelled",
            status="cancelled",
            error="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
    except Exception as exc:
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            "Scan failed",
            status="failed",
            error=str(exc),
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )


def start_scan(path, lib_root=LIB_ROOT, synchronous=False):
    _ensure_cache_loaded()
    real_path, err = _validate_scan_path(path, lib_root)
    if err:
        return None, err
    scan_id = _now_id()
    created = time.time()
    settings = app_settings.load_settings()
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
        "scanned_video_count": 0,
        "items": [],
        "counts": {},
        "settings": settings,
        "lib_root": os.path.realpath(lib_root),
    }
    with subtitle_lock:
        _prune_scans_locked()
        active = _active_scan_locked()
        if active:
            return active, None
        subtitle_scans[scan_id] = scan
    if synchronous:
        _run_scan(scan, settings, lib_root)
    else:
        threading.Thread(
            target=_run_scan,
            args=(scan, settings, lib_root),
            daemon=True,
            name=f"vid2gif-subtitle-scan-{scan_id}",
        ).start()
    return scan, None


def cancel_scan(scan_id=None):
    target_id = str(scan_id or "")
    now = time.time()
    with subtitle_lock:
        _prune_scans_locked(now)
        scan = subtitle_scans.get(target_id) if target_id else _active_scan_locked()
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
                    "progress_label": "Scan cancelled",
                    "_finished_ts": now,
                    "finished_at": utc_iso(now),
                }
            )
        else:
            scan.update({"status": "cancelling", "progress_label": "Cancelling scan"})
    return scan, None


def status_payload(scan_id=None):
    _ensure_cache_loaded()
    with subtitle_lock:
        _prune_scans_locked()
        if scan_id:
            scan = subtitle_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif subtitle_scans:
            active = _active_scan_locked()
            successful = [item for item in subtitle_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("_finished_ts") or 0) if successful else max(subtitle_scans.values(), key=lambda item: item.get("_created_ts") or 0))
        else:
            scan = None
    return {"scan": public_scan(scan)}, None


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


def _filter_items(items, status, query):
    status = str(status or "language_review").lower()
    if status not in {"missing", "language_review", "unknown", "ok", "index_mismatch", "all"}:
        status = "language_review"
    filtered = list(items)
    if status == "index_mismatch":
        filtered = [item for item in filtered if item.get("emby_index_status") == "mismatch"]
    elif status != "all":
        filtered = [item for item in filtered if item.get("status") == status]
    query = str(query or "").strip().lower()
    if query:
        filtered = [
            item
            for item in filtered
            if query in str(item.get("name", "")).lower()
            or query in str(item.get("path", "")).lower()
            or query in str(item.get("relative_path", "")).lower()
            or any(query in str(srt.get("name", "")).lower() for srt in item.get("srt_files") or [])
            or any(
                query in " ".join(
                    str(stream.get(key) or "").lower()
                    for key in ("language_code", "display_language", "display_title", "codec")
                )
                for stream in item.get("emby_subtitle_streams") or []
            )
        ]
    return status, filtered


def items_payload(scan_id, status="language_review", offset=0, limit=ITEM_PAGE_DEFAULT, q="", sort="video", direction="asc"):
    _ensure_cache_loaded()
    offset, limit = _coerce_page(offset, limit)
    with subtitle_lock:
        _prune_scans_locked()
        scan = subtitle_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        items = list(scan.get("items") or [])
    status, items = _filter_items(items, status, q)
    items, sort, direction = sort_records(
        items, sort, direction,
        {
            "status": lambda item: item.get("status"),
            "video": lambda item: item.get("relative_path") or item.get("name"),
            "subtitles": lambda item: len(item.get("srt_files") or []),
            "language": lambda item: item.get("language_codes") or [],
            "reason": lambda item: item.get("detail"),
            "streams": lambda item: item.get("emby_subtitle_stream_count", 0),
        },
        "video",
    )
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "status": status,
        "q": q or "",
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
        "items": page,
    }, None


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, separators=(",", ":"))
    os.replace(tmp, path)


def _all_subtitles(scan):
    result = {}
    for video in scan.get("items") or []:
        for subtitle in video.get("srt_files") or []:
            value = dict(subtitle)
            value["_video_path"] = video.get("path", "")
            value["_video_id"] = video.get("id", "")
            result[subtitle.get("id")] = value
    return result


def build_action_plan(payload, lib_root=LIB_ROOT):
    _ensure_cache_loaded()
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    allowed, freshness_error = maintenance_scan_store.action_allowed(
        "subtitles", scan_id, lib_root
    )
    if not allowed:
        return None, freshness_error
    operation = str(payload.get("operation") or "quarantine").lower()
    if operation not in {"quarantine", "delete"}:
        return None, "Choose quarantine or delete"
    visible_ids = payload.get("visible_file_ids")
    selected_ids = payload.get("selected_file_ids")
    if not isinstance(visible_ids, list) or not visible_ids:
        return None, "Visible subtitle files are required"
    if not isinstance(selected_ids, list) or not selected_ids:
        return None, "Select at least one subtitle file"
    visible = {str(value or "") for value in visible_ids if str(value or "")}
    selected = []
    for value in selected_ids:
        file_id = str(value or "")
        if file_id and file_id not in selected:
            selected.append(file_id)
    if any(file_id not in visible for file_id in selected):
        return None, "Selected subtitles must be visible on the current page"
    with subtitle_lock:
        scan = subtitle_scans.get(scan_id)
    if not scan or scan.get("status") != "success":
        return None, "Scan is not complete"
    files_by_id = _all_subtitles(scan)
    if any(file_id not in files_by_id for file_id in visible):
        return None, "Visible subtitle results are stale"
    root = os.path.realpath(lib_root)
    quarantine_root = os.path.realpath(os.path.join(root, SUBTITLE_QUARANTINE_DIRNAME))
    files = []
    for file_id in selected:
        subtitle = files_by_id.get(file_id)
        if not subtitle or not subtitle.get("actionable"):
            return None, "Only flagged subtitle files can be changed"
        source = subtitle.get("path") or ""
        if not path_is_under(source, root) or os.path.islink(source):
            return None, "Subtitle path is unsafe"
        relative = os.path.relpath(os.path.realpath(source), root)
        destination = os.path.realpath(os.path.join(quarantine_root, relative)) if operation == "quarantine" else ""
        if destination and not path_is_under(destination, quarantine_root):
            return None, "Subtitle quarantine path is unsafe"
        files.append({
            "file_id": file_id,
            "source_path": source,
            "relative_path": relative,
            "destination_path": destination,
            "size_bytes": subtitle.get("size_bytes") or 0,
            "size_label": subtitle.get("size_label") or "",
            "language_code": subtitle.get("language_code") or "unknown",
            "identity": dict(subtitle.get("identity") or {}),
            "emby_item_id": subtitle.get("emby_parent_item_id", ""),
            "emby_item_type": subtitle.get("emby_parent_item_type", ""),
            "video_path": subtitle.get("_video_path", ""),
            "video_id": subtitle.get("_video_id", ""),
        })
    playback_targets = [
        {
            "id": item["file_id"],
            "group_id": item.get("emby_item_id") or item.get("video_id") or item["file_id"],
            "local_path": item.get("video_path", ""),
            "emby_item_id": item.get("emby_item_id", ""),
        }
        for item in files
    ]
    playback = emby_playback.check_targets(playback_targets, force=True)
    for item in files:
        item["emby_playback_status"] = emby_playback.target_status(playback, item["file_id"])
    plan = {
        "id": _now_id(),
        "scan_id": scan_id,
        "scan_path": scan.get("path") or root,
        "operation": operation,
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": root,
        "quarantine_root": quarantine_root if operation == "quarantine" else "",
        "visible_file_ids": sorted(visible),
        "files": files,
        "file_count": len(files),
        "total_size_bytes": sum(item["size_bytes"] for item in files),
        "playback_targets": playback_targets,
        "emby_playback": playback,
    }
    plan["total_size_label"] = format_size(plan["total_size_bytes"])
    with subtitle_lock:
        subtitle_plans[plan["id"]] = plan
    public = dict(plan)
    public.pop("playback_targets", None)
    public["emby_playback"] = emby_playback.public_result(playback)
    return public, None


def public_apply_run(run):
    if not run:
        return None
    public = {key: value for key, value in run.items() if not key.startswith("_")}
    if run.get("emby_playback"):
        public["emby_playback"] = emby_playback.public_result(run.get("emby_playback"))
    if isinstance(public.get("result"), dict):
        public["result"] = dict(public["result"])
        if public["result"].get("emby_playback"):
            public["result"]["emby_playback"] = emby_playback.public_result(
                public["result"].get("emby_playback")
            )
    return public


def _identity_matches(path, expected):
    current = _stat_identity(path)
    return bool(current and expected and all(current.get(key) == expected.get(key) for key in ("size", "mtime_ns", "inode", "device")))


def _save_action_log(plan, run, records):
    entry = {
        "id": run["id"],
        "created_at": run.get("finished_at") or utc_iso(),
        "operation": plan.get("operation"),
        "applied_count": run.get("applied_count", 0),
        "refused_count": run.get("refused_count", 0),
        "deferred_count": run.get("deferred_count", 0),
        "deferred_bytes": run.get("deferred_bytes", 0),
        "size_label": format_size(run.get("applied_bytes", 0)),
        "records": records,
    }
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        _write_json_atomic(os.path.join(LOG_DIR, f"{entry['id']}.json"), entry)
        try:
            with open(LOG_INDEX, "r", encoding="utf-8") as handle:
                index = json.load(handle)
        except Exception:
            index = []
        index = [entry_item for entry_item in index if entry_item.get("id") != entry["id"]]
        index.insert(0, {key: value for key, value in entry.items() if key != "records"})
        _write_json_atomic(LOG_INDEX, index[:25])
    except OSError:
        pass


@coordinated_library_operation(
    "Apply subtitle maintenance",
    kind="mutation",
    href="/maintenance#subtitles",
    state_index=1,
)
def _run_action(plan, run):
    records = []
    applied_bytes = 0
    deferred_bytes = 0
    playback_targets = list(plan.get("playback_targets") or [])
    playback = emby_playback.check_targets(playback_targets, force=True)
    playback_checked = time.monotonic()
    group_decisions = {}
    for index, item in enumerate(plan.get("files") or [], 1):
        source = item.get("source_path") or ""
        reason = ""
        status = "refused"
        group_id = item.get("emby_item_id") or item.get("video_id") or item.get("file_id")
        if group_id not in group_decisions:
            if time.monotonic() - playback_checked >= emby_playback.RUN_REFRESH_SECONDS:
                playback = emby_playback.check_targets(playback_targets, force=True)
                playback_checked = time.monotonic()
            group_decisions[group_id] = emby_playback.group_status(playback, playback_targets, group_id)
        playback_status = group_decisions[group_id]
        if playback_status in {"active", "unverified"}:
            status = "deferred"
            reason = (
                "Parent video is actively playing in Emby"
                if playback_status == "active"
                else "Parent video playback could not be verified"
            )
            deferred_bytes += item.get("size_bytes") or 0
        elif not path_is_under(source, plan["lib_root"]) or os.path.islink(source):
            reason = "Source path is unsafe"
        elif not os.path.isfile(source):
            reason = "Source file is missing"
        elif not _identity_matches(source, item.get("identity")):
            reason = "Source file changed after the scan"
        else:
            try:
                if plan["operation"] == "delete":
                    os.remove(source)
                else:
                    destination = item.get("destination_path") or ""
                    if os.path.lexists(destination):
                        raise FileExistsError("Quarantine destination already exists")
                    os.makedirs(os.path.dirname(destination), exist_ok=True)
                    atomic_quarantine_file(
                        source,
                        destination,
                        root=plan["lib_root"],
                        expected_source=item.get("identity"),
                    )
                status = "applied"
                applied_bytes += item.get("size_bytes") or 0
            except Exception as exc:
                reason = str(exc)
        records.append({"file_id": item.get("file_id"), "path": item.get("relative_path"), "status": status, "reason": reason})
        run.update({
            "processed_count": index,
            "applied_count": sum(1 for record in records if record["status"] == "applied"),
            "refused_count": sum(1 for record in records if record["status"] == "refused"),
            "deferred_count": sum(1 for record in records if record["status"] == "deferred"),
            "progress_label": f"Processed {index} of {len(plan.get('files') or [])} subtitles",
        })
    run.update({
        "progress_label": "Subtitle files processed",
        "applied_bytes": applied_bytes,
        "deferred_bytes": deferred_bytes,
        "result": {"records": records, "scan_path": plan.get("scan_path"), "applied_bytes": applied_bytes, "deferred_bytes": deferred_bytes},
    })
    _save_action_log(plan, run, records)
    applied_ids = {record.get("file_id") for record in records if record.get("status") == "applied"}
    applied_files = [item for item in plan.get("files") or [] if item.get("file_id") in applied_ids]
    if applied_files:
        run.update(progress_label="Synchronizing subtitle changes with Emby")
    sync_result = emby_sync.sync_changes(
        [
            {
                "local_path": item.get("source_path"),
                "update_type": "Deleted",
                "emby_item_id": item.get("emby_item_id", ""),
                "refresh_scope": "metadata",
            }
            for item in applied_files
        ],
        workflow="subtitles",
        run_id=run["id"],
    ) if applied_files else None
    run["result"]["emby_sync"] = sync_result
    run["result"]["emby_playback"] = playback
    operation = plan.get("operation")
    impact_metrics.record_maintenance_action(
        plan.get("id"),
        "subtitles",
        resolutions=[
            {
                "issue_id": f"subtitle:{item.get('file_id')}",
                "stream": "subtitles",
                "finding_ids": [item.get("file_id")],
                "ensure_issue": True,
                "label": os.path.basename(item.get("source_path") or "Flagged subtitle"),
                "path": item.get("source_path") or plan.get("scan_path"),
            }
            for item in applied_files
        ],
        operations={
            "quarantined_files": len(applied_files) if operation == "quarantine" else 0,
            "quarantined_bytes": applied_bytes if operation == "quarantine" else 0,
            "deleted_files": len(applied_files) if operation == "delete" else 0,
            "deleted_bytes": applied_bytes if operation == "delete" else 0,
        },
        timestamp=run.get("finished_at") or utc_iso(),
        label="Subtitle cleanup",
    )
    run.update(
        status="success",
        finished_at=utc_iso(),
        progress_label="Subtitle cleanup complete",
        emby_sync=sync_result,
        emby_playback=playback,
    )
    notification = emby_notifications.notify_maintenance(
        "Subtitle cleanup",
        run["id"],
        status="success",
        attempted_count=len(plan.get("files") or []),
        succeeded_count=run.get("applied_count", 0),
        refused_count=run.get("refused_count", 0),
        deferred_count=run.get("deferred_count", 0),
        reclaimed_bytes=applied_bytes,
        emby_sync=sync_result,
    )
    run["emby_notification"] = notification
    run["result"]["emby_notification"] = notification


def start_action_apply(plan_id, synchronous=False):
    with subtitle_lock:
        plan = subtitle_plans.get(str(plan_id or ""))
        if not plan:
            return None, "Plan not found"
        run = {
            "id": _now_id(),
            "plan_id": plan["id"],
            "status": "queued",
            "operation": plan["operation"],
            "file_count": plan["file_count"],
            "processed_count": 0,
            "applied_count": 0,
            "refused_count": 0,
            "deferred_count": 0,
            "progress_label": "Queued",
            "created_at": utc_iso(),
        }
        subtitle_apply_runs[run["id"]] = run
    def execute():
        run.update({"status": "running", "started_at": utc_iso(), "progress_label": "Applying subtitle cleanup"})
        try:
            _run_action(plan, run)
        except Exception as exc:
            run.update({"status": "failed", "error": str(exc), "finished_at": utc_iso(), "progress_label": "Subtitle cleanup failed"})
            run["emby_notification"] = emby_notifications.notify_maintenance(
                "Subtitle cleanup",
                run["id"],
                status="failed",
                attempted_count=run.get("file_count", 0),
                succeeded_count=run.get("applied_count", 0),
                failed_count=1,
                refused_count=run.get("refused_count", 0),
                deferred_count=run.get("deferred_count", 0),
            )
    if synchronous:
        execute()
    else:
        threading.Thread(target=execute, daemon=True, name=f"vid2gif-subtitle-apply-{run['id']}").start()
    return run, None


def apply_status(apply_id):
    with subtitle_lock:
        run = subtitle_apply_runs.get(str(apply_id or ""))
    if not run:
        return None, "Apply run not found"
    return {"apply": public_apply_run(run)}, None


def list_action_logs():
    try:
        with open(LOG_INDEX, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def action_log(log_id):
    try:
        with open(os.path.join(LOG_DIR, f"{str(log_id or '')}.json"), "r", encoding="utf-8") as handle:
            return json.load(handle), None
    except Exception:
        return None, "Log not found"
