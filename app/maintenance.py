import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from .config import LIB_ROOT, VIDEO_EXTS
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


QUARANTINE_DIRNAME = ".vid2gif-duplicates"
SCAN_TERMINAL_STATUSES = {"success", "failed"}
PLAN_ACTIONS = {"move", "delete"}
__test__ = False

duplicate_scans = {}
cleanup_plans = {}
maintenance_lock = threading.Lock()

_QUALITY_PATTERNS = [
    r"\b(?:4320p|2160p|1440p|1080p|720p|576p|540p|480p|360p|4k|8k|uhd|fhd|hd)\b",
    r"\b(?:hdr10plus|hdr10|hdr|dv|dolby\s+vision|sdr)\b",
    r"\b(?:web\s*dl|web\s*rip|webrip|webdl|bluray|blu\s*ray|brrip|br\s*rip|hdtv|dvdrip|remux)\b",
    r"\b(?:x264|x265|h264|h265|hevc|avc|av1|vp9|mpeg2)\b",
    r"\b(?:aac|ac3|eac3|eac3\s*5\s*1|dts|truehd|atmos|flac|mp3)\b",
    r"\b(?:10bit|8bit|proper|repack|rerip|extended|unrated|theatrical|directors?\s*cut)\b",
]


def _now_id():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _hash_text(value):
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


def _path_id(path, lib_root):
    try:
        rel = os.path.relpath(os.path.realpath(path), os.path.realpath(lib_root))
    except (OSError, ValueError):
        rel = os.path.basename(path)
    rel = os.path.normcase(rel).replace(os.sep, "/")
    return _hash_text(rel)[:20]


def _safe_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _bitrate_label(bit_rate):
    bit_rate = _safe_int(bit_rate)
    if not bit_rate:
        return ""
    if bit_rate >= 1_000_000:
        return f"{bit_rate / 1_000_000:.1f} Mbps"
    if bit_rate >= 1_000:
        return f"{bit_rate / 1_000:.0f} Kbps"
    return f"{bit_rate} bps"


def normalize_duplicate_name(stem):
    value = str(stem or "").lower()
    value = re.sub(r"[\[\]\(\)\{\}]", " ", value)
    value = re.sub(r"[._\-]+", " ", value)
    for pattern in _QUALITY_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value or str(stem or "").strip().lower()


def _stat_identity(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return {
        "real_path": os.path.normcase(os.path.realpath(path)),
        "size": stat.st_size,
        "mtime_ns": getattr(stat, "st_mtime_ns", int(stat.st_mtime * 1_000_000_000)),
    }


def _identity_matches(path, identity):
    if not identity or not os.path.isfile(path):
        return False
    current = _stat_identity(path)
    if not current:
        return False
    return (
        current.get("real_path") == identity.get("real_path")
        and current.get("size") == identity.get("size")
        and current.get("mtime_ns") == identity.get("mtime_ns")
    )


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


def probe_video_metadata(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,bit_rate,avg_frame_rate:format=duration,bit_rate",
        "-of",
        "json",
        video_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception:
        return {}
    if proc.returncode != 0:
        return {}
    try:
        info = json.loads(proc.stdout or "{}")
    except Exception:
        return {}

    stream = (info.get("streams") or [{}])[0] or {}
    fmt = info.get("format") or {}
    duration = _safe_float(fmt.get("duration"))
    bit_rate = _safe_int(stream.get("bit_rate")) or _safe_int(fmt.get("bit_rate"))
    return {
        "codec": stream.get("codec_name") or "",
        "width": _safe_int(stream.get("width")),
        "height": _safe_int(stream.get("height")),
        "duration_seconds": duration,
        "bit_rate": bit_rate,
        "avg_frame_rate": stream.get("avg_frame_rate") or "",
    }


def _metadata_label(metadata):
    parts = []
    width = metadata.get("width")
    height = metadata.get("height")
    if width and height:
        parts.append(f"{width}x{height}")
    if metadata.get("codec"):
        parts.append(metadata["codec"])
    bitrate = _bitrate_label(metadata.get("bit_rate"))
    if bitrate:
        parts.append(bitrate)
    duration = metadata.get("duration_seconds")
    if duration:
        parts.append(f"{duration:.0f}s")
    return " - ".join(parts)


def _quality_sort_key(video):
    metadata = video.get("metadata") or {}
    width = metadata.get("width") or 0
    height = metadata.get("height") or 0
    duration = metadata.get("duration_seconds") or 0
    bitrate = metadata.get("bit_rate") or 0
    if not bitrate and duration:
        bitrate = int((video.get("size_bytes") or 0) * 8 / duration)
    return (
        -(width * height),
        -bitrate,
        -(video.get("size_bytes") or 0),
        -duration,
        str(video.get("path") or "").lower(),
    )


def _file_payload(path, kind, lib_root, parent_video_id=""):
    identity = _stat_identity(path)
    if not identity:
        return None
    name = os.path.basename(path)
    stem, ext = os.path.splitext(name)
    return {
        "id": _path_id(path, lib_root),
        "kind": kind,
        "path": os.path.realpath(path),
        "name": name,
        "stem": stem,
        "ext": ext.lower(),
        "size_bytes": identity["size"],
        "size_label": format_size(identity["size"]),
        "modified_at": utc_iso(os.path.getmtime(path)),
        "identity": identity,
        "parent_video_id": parent_video_id,
    }


def _accessory_matches(entry_name, video_stem):
    lower_entry = entry_name.lower()
    lower_stem = video_stem.lower()
    if not lower_entry.startswith(lower_stem):
        return False
    if len(lower_entry) == len(lower_stem):
        return True
    return lower_entry[len(lower_stem)] in {".", "-", "_", " ", "["}


def find_accessory_files(video_path, lib_root):
    folder = os.path.dirname(video_path)
    video_name = os.path.basename(video_path)
    video_stem = os.path.splitext(video_name)[0]
    accessories = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return accessories

    for entry in entries:
        full_path = os.path.join(folder, entry)
        if entry == video_name or os.path.islink(full_path) or not os.path.isfile(full_path):
            continue
        if os.path.splitext(entry)[1].lower() in VIDEO_EXTS:
            continue
        if not _accessory_matches(entry, video_stem):
            continue
        item = _file_payload(full_path, "accessory", lib_root, parent_video_id="")
        if item:
            accessories.append(item)
    accessories.sort(key=lambda item: item["name"].lower())
    return accessories


def _collect_videos(root_path):
    videos = []
    for base, dirs, files in os.walk(root_path, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d != QUARANTINE_DIRNAME and not os.path.islink(os.path.join(base, d))
        ]
        for filename in files:
            path = os.path.join(base, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if os.path.splitext(filename)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.realpath(path))
    videos.sort(key=lambda path: path.lower())
    return videos


def _candidate_groups(videos):
    buckets = {}
    for path in videos:
        folder = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        key = (os.path.normcase(os.path.realpath(folder)), normalize_duplicate_name(stem))
        buckets.setdefault(key, []).append(path)
    return [items for items in buckets.values() if len(items) > 1]


def _set_scan_progress(scan, percent, label, **values):
    with maintenance_lock:
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


def _build_group(paths, group_index, lib_root):
    videos = []
    for path in paths:
        item = _file_payload(path, "video", lib_root)
        if not item:
            continue
        metadata = probe_video_metadata(path)
        item["metadata"] = metadata
        item["metadata_label"] = _metadata_label(metadata)
        item["accessories"] = find_accessory_files(path, lib_root)
        for accessory in item["accessories"]:
            accessory["parent_video_id"] = item["id"]
        videos.append(item)

    if len(videos) < 2:
        return None

    videos.sort(key=_quality_sort_key)
    recommended = videos[0]
    remove_ids = [video["id"] for video in videos[1:]]
    reclaimable = 0
    accessory_count = 0
    for video in videos[1:]:
        reclaimable += video.get("size_bytes") or 0
        accessory_count += len(video.get("accessories") or [])
        reclaimable += sum(item.get("size_bytes") or 0 for item in video.get("accessories") or [])

    folder = os.path.dirname(videos[0]["path"])
    normalized_name = normalize_duplicate_name(os.path.splitext(videos[0]["name"])[0])
    return {
        "id": f"group-{group_index}",
        "folder": folder,
        "normalized_name": normalized_name,
        "videos": videos,
        "recommended_keep_id": recommended["id"],
        "remove_ids": remove_ids,
        "reclaimable_bytes": reclaimable,
        "reclaimable_label": format_size(reclaimable),
        "accessory_count": accessory_count,
    }


def _public_file(item):
    public = {
        "id": item.get("id", ""),
        "kind": item.get("kind", ""),
        "path": item.get("path", ""),
        "name": item.get("name", ""),
        "stem": item.get("stem", ""),
        "ext": item.get("ext", ""),
        "size_bytes": item.get("size_bytes", 0),
        "size_label": item.get("size_label", ""),
        "modified_at": item.get("modified_at", ""),
        "parent_video_id": item.get("parent_video_id", ""),
    }
    if item.get("metadata") is not None:
        public["metadata"] = item.get("metadata") or {}
        public["metadata_label"] = item.get("metadata_label", "")
    if "accessories" in item:
        public["accessories"] = [_public_file(child) for child in item.get("accessories") or []]
    return public


def _public_group(group):
    return {
        "id": group.get("id", ""),
        "folder": group.get("folder", ""),
        "normalized_name": group.get("normalized_name", ""),
        "videos": [_public_file(video) for video in group.get("videos") or []],
        "recommended_keep_id": group.get("recommended_keep_id", ""),
        "remove_ids": list(group.get("remove_ids") or []),
        "reclaimable_bytes": group.get("reclaimable_bytes", 0),
        "reclaimable_label": group.get("reclaimable_label", ""),
        "accessory_count": group.get("accessory_count", 0),
    }


def public_scan(scan):
    if not scan:
        return None
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
        "scanned_video_count": scan.get("scanned_video_count", 0),
        "duplicate_group_count": len(scan.get("groups") or []),
        "reclaimable_bytes": scan.get("reclaimable_bytes", 0),
        "reclaimable_label": format_size(scan.get("reclaimable_bytes", 0)),
        "groups": [_public_group(group) for group in scan.get("groups") or []],
    }


def _run_scan(scan, lib_root):
    try:
        now = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning folders",
            status="running",
            _started_ts=now,
            started_at=utc_iso(now),
        )
        videos = _collect_videos(scan["path"])
        _set_scan_progress(
            scan,
            10,
            f"Found {len(videos)} video file{'s' if len(videos) != 1 else ''}",
            scanned_video_count=len(videos),
        )

        candidates = _candidate_groups(videos)
        groups = []
        total = len(candidates)
        for index, paths in enumerate(candidates, start=1):
            group = _build_group(paths, index, lib_root)
            if group:
                groups.append(group)
            percent = 10 + int(80 * index / max(total, 1))
            _set_scan_progress(
                scan,
                percent,
                f"Checked {index} of {total} duplicate candidate groups",
            )

        reclaimable = sum(group.get("reclaimable_bytes") or 0 for group in groups)
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            f"Found {len(groups)} duplicate group{'s' if len(groups) != 1 else ''}",
            status="success",
            groups=groups,
            reclaimable_bytes=reclaimable,
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


def start_duplicate_scan(path, lib_root=LIB_ROOT, synchronous=False):
    real_path, err = _validate_scan_path(path, lib_root)
    if err:
        return None, err

    scan_id = _now_id()
    created = time.time()
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
        "scanned_video_count": 0,
        "groups": [],
        "reclaimable_bytes": 0,
        "lib_root": os.path.realpath(lib_root),
    }
    with maintenance_lock:
        duplicate_scans[scan_id] = scan

    if synchronous:
        _run_scan(scan, lib_root)
    else:
        thread = threading.Thread(
            target=_run_scan,
            args=(scan, lib_root),
            daemon=True,
            name=f"vid2gif-maintenance-scan-{scan_id}",
        )
        thread.start()
    return scan, None


def status_payload(scan_id=None):
    with maintenance_lock:
        if scan_id:
            scan = duplicate_scans.get(scan_id)
            if not scan:
                return None, "Scan not found"
        elif duplicate_scans:
            scan = max(duplicate_scans.values(), key=lambda item: item.get("_created_ts") or 0)
        else:
            scan = None
    return {"scan": public_scan(scan)}, None


def _group_overrides(payload):
    groups = payload.get("groups") or []
    if not isinstance(groups, list):
        return None
    overrides = {}
    for group in groups:
        if not isinstance(group, dict):
            return None
        group_id = str(group.get("id") or "")
        if group_id:
            overrides[group_id] = group
    return overrides


def _truthy(value, default=True):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def quarantine_destination(path, lib_root, scan_id, used_destinations=None):
    used_destinations = used_destinations if used_destinations is not None else set()
    lib_real = os.path.realpath(lib_root)
    rel = os.path.relpath(os.path.realpath(path), lib_real)
    base_dest = os.path.realpath(os.path.join(lib_real, QUARANTINE_DIRNAME, scan_id, rel))
    directory = os.path.dirname(base_dest)
    stem, ext = os.path.splitext(os.path.basename(base_dest))
    candidate = base_dest
    index = 1
    while candidate in used_destinations or os.path.exists(candidate):
        candidate = os.path.join(directory, f"{stem}.{index}{ext}")
        index += 1
    used_destinations.add(candidate)
    return candidate


def _build_plan_item(item, group_id, action, lib_root, scan_id, used_destinations):
    source = item.get("path", "")
    dest = ""
    if action == "move":
        dest = quarantine_destination(source, lib_root, scan_id, used_destinations)
    return {
        "file_id": item.get("id", ""),
        "group_id": group_id,
        "kind": item.get("kind", ""),
        "source_path": source,
        "relative_path": os.path.relpath(os.path.realpath(source), os.path.realpath(lib_root)),
        "destination_path": dest,
        "size_bytes": item.get("size_bytes", 0),
        "size_label": item.get("size_label", ""),
        "identity": dict(item.get("identity") or {}),
    }


def build_duplicate_cleanup_plan(payload, lib_root=LIB_ROOT):
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    action = str(payload.get("action") or "move").strip().lower()
    if action not in PLAN_ACTIONS:
        return None, "Choose move or delete"
    overrides = _group_overrides(payload)
    if overrides is None:
        return None, "Group overrides are invalid"

    with maintenance_lock:
        scan = duplicate_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"

    plan_id = _now_id()
    used_destinations = set()
    files = []
    skipped_groups = []
    lib_real = os.path.realpath(lib_root)

    for group in scan.get("groups") or []:
        override = overrides.get(group["id"], {})
        if not _truthy(override.get("enabled"), default=True):
            skipped_groups.append(group["id"])
            continue

        videos = {video["id"]: video for video in group.get("videos") or []}
        keep_id = str(override.get("keep_video_id") or group.get("recommended_keep_id") or "")
        if keep_id not in videos:
            keep_id = group.get("recommended_keep_id")

        removable_video_ids = [video_id for video_id in videos if video_id != keep_id]
        raw_remove_ids = override.get("remove_video_ids")
        if isinstance(raw_remove_ids, list):
            allowed = {str(item) for item in raw_remove_ids}
            removable_video_ids = [video_id for video_id in removable_video_ids if video_id in allowed]

        candidate_files = {}
        for video_id in removable_video_ids:
            video = videos[video_id]
            candidate_files[video_id] = video
            for accessory in video.get("accessories") or []:
                candidate_files[accessory["id"]] = accessory

        include_file_ids = override.get("include_file_ids")
        if isinstance(include_file_ids, list):
            selected_ids = [str(file_id) for file_id in include_file_ids if str(file_id) in candidate_files]
        else:
            selected_ids = list(candidate_files)

        for file_id in selected_ids:
            item = candidate_files[file_id]
            source = item.get("path", "")
            if not path_is_under(source, lib_real):
                continue
            files.append(
                _build_plan_item(
                    item,
                    group["id"],
                    action,
                    lib_real,
                    scan_id,
                    used_destinations,
                )
            )

    total_size = sum(item.get("size_bytes") or 0 for item in files)
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "action": action,
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": lib_real,
        "move_root": os.path.join(lib_real, QUARANTINE_DIRNAME, scan_id) if action == "move" else "",
        "files": files,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "skipped_groups": skipped_groups,
    }
    with maintenance_lock:
        cleanup_plans[plan_id] = plan
    return public_plan(plan), None


def public_plan(plan):
    if not plan:
        return None
    return {
        "id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": plan.get("action", ""),
        "status": plan.get("status", ""),
        "created_at": plan.get("created_at", ""),
        "move_root": plan.get("move_root", ""),
        "file_count": plan.get("file_count", 0),
        "total_size_bytes": plan.get("total_size_bytes", 0),
        "total_size_label": plan.get("total_size_label", ""),
        "skipped_groups": list(plan.get("skipped_groups") or []),
        "files": [
            {
                "file_id": item.get("file_id", ""),
                "group_id": item.get("group_id", ""),
                "kind": item.get("kind", ""),
                "source_path": item.get("source_path", ""),
                "relative_path": item.get("relative_path", ""),
                "destination_path": item.get("destination_path", ""),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
            }
            for item in plan.get("files") or []
        ],
    }


def _refusal(file_id, path, reason):
    return {"file_id": file_id, "path": path, "reason": reason}


def apply_duplicate_cleanup_plan(plan_id):
    with maintenance_lock:
        plan = cleanup_plans.get(str(plan_id or ""))
    if not plan:
        return None, "Plan not found"

    action = plan.get("action")
    lib_root = plan.get("lib_root") or LIB_ROOT
    move_root = plan.get("move_root") or ""
    applied = []
    missing = []
    refused = []
    total = 0

    for item in plan.get("files") or []:
        source = item.get("source_path", "")
        file_id = item.get("file_id", "")
        if not source or not path_is_under(source, lib_root):
            refused.append(_refusal(file_id, source, "Source is outside the library"))
            continue
        if os.path.islink(source):
            refused.append(_refusal(file_id, source, "Symlinks are not cleaned"))
            continue
        if not os.path.exists(source):
            missing.append(file_id)
            continue
        if not _identity_matches(source, item.get("identity")):
            refused.append(_refusal(file_id, source, "File changed after scan"))
            continue

        try:
            if action == "delete":
                os.remove(source)
            elif action == "move":
                dest = item.get("destination_path", "")
                if not dest or not path_is_under(dest, move_root) or not path_is_under(dest, lib_root):
                    refused.append(_refusal(file_id, source, "Destination is outside quarantine"))
                    continue
                if os.path.exists(dest):
                    refused.append(_refusal(file_id, source, "Destination already exists"))
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(source, dest)
            else:
                refused.append(_refusal(file_id, source, "Unsupported action"))
                continue
        except Exception as exc:
            refused.append(_refusal(file_id, source, str(exc)))
            continue

        applied.append(
            {
                "file_id": file_id,
                "source_path": source,
                "destination_path": item.get("destination_path", ""),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
            }
        )
        total += item.get("size_bytes") or 0

    result = {
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": action,
        "applied": applied,
        "missing": missing,
        "refused": refused,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "refused_count": len(refused),
        "total_applied_bytes": total,
        "total_applied_label": format_size(total),
    }
    with maintenance_lock:
        plan["status"] = "applied"
        plan["applied_at"] = utc_iso()
        plan["last_result"] = result
    return result, None
