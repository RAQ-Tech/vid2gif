import datetime
import hashlib
import os
import re
import threading
import time

from . import app_settings
from .config import LIB_ROOT, VIDEO_EXTS
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


ITEM_PAGE_DEFAULT = 25
ITEM_PAGE_MAX = 100
LARGE_RESULT_COUNT = 100
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
SCAN_RETENTION_COUNT = 10
SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
SUBTITLE_EXTS = {".srt"}
KNOWN_SKIP_DIRS = {
    ".vid2gif-duplicates",
    ".vid2gif-video-preview-repairs",
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
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


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
        "results_page_size": ITEM_PAGE_DEFAULT,
        "large_result": review_count >= LARGE_RESULT_COUNT,
        "settings": public_settings(scan.get("settings") or app_settings.load_settings()),
        **counts,
    }


def _prune_scans_locked(now=None):
    now = now or time.time()
    for scan_id in list(subtitle_scans):
        scan = subtitle_scans.get(scan_id) or {}
        if scan.get("status") not in SCAN_TERMINAL_STATUSES:
            continue
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if now - finished > SCAN_MAX_AGE_SECONDS:
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
    for scan_id, _scan in terminal[SCAN_RETENTION_COUNT:]:
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
        counts = _counts(items)
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            f"{counts['review_count']} review items, {counts['ok_count']} OK",
            status="success",
            items=items,
            counts=counts,
            scanned_video_count=counts["scanned_video_count"],
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
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
    with subtitle_lock:
        _prune_scans_locked()
        if scan_id:
            scan = subtitle_scans.get(str(scan_id or ""))
            if not scan:
                return None, "Scan not found"
        elif subtitle_scans:
            scan = max(subtitle_scans.values(), key=lambda item: item.get("_created_ts") or 0)
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
    if status not in {"missing", "language_review", "unknown", "ok", "all"}:
        status = "language_review"
    filtered = list(items)
    if status != "all":
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
        ]
    return status, filtered


def items_payload(scan_id, status="language_review", offset=0, limit=ITEM_PAGE_DEFAULT, q=""):
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
    total = len(items)
    page = items[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "status": status,
        "q": q or "",
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
