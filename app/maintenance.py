import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time

from . import app_settings
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


QUARANTINE_DIRNAME = ".vid2gif-duplicates"
SCAN_TERMINAL_STATUSES = {"success", "failed"}
PLAN_ACTIONS = {"move", "delete"}
MAINTENANCE_LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "duplicates")
MAINTENANCE_LOG_INDEX = os.path.join(MAINTENANCE_LOG_DIR, "index.json")
MAINTENANCE_LOG_RETENTION_COUNT = 25
MAINTENANCE_LOG_MAX_BYTES = 5 * 1024 * 1024
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


def duplicate_settings():
    settings = app_settings.load_settings()
    return {
        "grouping_mode": settings.get("duplicate_grouping_mode", "balanced"),
        "keeper_rule": settings.get("duplicate_keeper_rule", "quality"),
        "accessory_policy": settings.get("duplicate_accessory_policy", "rename_unmatched"),
        "move_root": settings.get("duplicate_move_root") or app_settings.DEFAULT_DUPLICATE_MOVE_ROOT,
        "excluded_folders": set(settings.get("duplicate_excluded_folders") or []),
    }


def public_duplicate_settings(settings):
    settings = settings or {}
    return {
        "grouping_mode": str(settings.get("grouping_mode") or "balanced"),
        "keeper_rule": str(settings.get("keeper_rule") or "quality"),
        "accessory_policy": str(
            settings.get("accessory_policy") or "rename_unmatched"
        ),
        "move_root": str(
            settings.get("move_root") or app_settings.DEFAULT_DUPLICATE_MOVE_ROOT
        ),
        "excluded_folders": sorted(
            str(item) for item in (settings.get("excluded_folders") or [])
        ),
    }


def _effective_move_root(settings, lib_root):
    configured = settings.get("move_root") or app_settings.DEFAULT_DUPLICATE_MOVE_ROOT
    try:
        if (
            os.path.realpath(configured) == os.path.realpath(app_settings.DEFAULT_DUPLICATE_MOVE_ROOT)
            and os.path.realpath(lib_root) != os.path.realpath(LIB_ROOT)
        ):
            return os.path.join(os.path.realpath(lib_root), QUARANTINE_DIRNAME)
    except (OSError, ValueError):
        pass
    return configured


def normalize_duplicate_name(stem):
    value = str(stem or "").lower()
    value = re.sub(r"[\[\]\(\)\{\}]", " ", value)
    value = re.sub(r"[._\-]+", " ", value)
    for pattern in _QUALITY_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:copy|duplicate|dupe)\s*\d*\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b\d+\b\s*$", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value or str(stem or "").strip().lower()


def _duration_close(first, second):
    first = _safe_float(first)
    second = _safe_float(second)
    if first is None or second is None or not first or not second:
        return True
    return abs(first - second) <= max(120, min(first, second) * 0.10)


def _nfo_path_for_video(video_path):
    folder = os.path.dirname(video_path)
    stem = os.path.splitext(os.path.basename(video_path))[0]
    direct = os.path.join(folder, f"{stem}.nfo")
    if os.path.isfile(direct):
        return direct
    target = f"{stem}.nfo".lower()
    try:
        for entry in os.listdir(folder):
            if entry.lower() == target and os.path.isfile(os.path.join(folder, entry)):
                return os.path.join(folder, entry)
    except OSError:
        return ""
    return ""


def parse_nfo_identity(video_path):
    nfo_path = _nfo_path_for_video(video_path)
    if not nfo_path:
        return {}
    try:
        with open(nfo_path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read(256 * 1024)
    except OSError:
        return {}
    ids = []
    for match in re.finditer(
        r"<uniqueid\b([^>]*)>(.*?)</uniqueid>",
        text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        attrs = match.group(1) or ""
        provider_match = re.search(r'type=["\']?([^"\'\s>]+)', attrs, flags=re.IGNORECASE)
        provider = provider_match.group(1).lower() if provider_match else "uniqueid"
        value = re.sub(r"\s+", " ", match.group(2) or "").strip()
        if value:
            ids.append(f"{provider}:{value.lower()}")

    def tag_value(name):
        match = re.search(
            rf"<{name}\b[^>]*>(.*?)</{name}>",
            text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        if not match:
            return ""
        value = re.sub(r"<!\[CDATA\[(.*?)\]\]>", r"\1", match.group(1), flags=re.DOTALL)
        return re.sub(r"\s+", " ", value).strip()

    return {
        "path": nfo_path,
        "unique_ids": sorted(set(ids)),
        "title": tag_value("title"),
        "sorttitle": tag_value("sorttitle"),
        "premiered": tag_value("premiered"),
        "studio": tag_value("studio"),
    }


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


def _keeper_sort_key(video, rule):
    rule = str(rule or "quality")
    if rule == "largest":
        return (-(video.get("size_bytes") or 0), str(video.get("path") or "").lower())
    if rule == "newest":
        identity = video.get("identity") or {}
        return (-(identity.get("mtime_ns") or 0), str(video.get("path") or "").lower())
    return _quality_sort_key(video)


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


def _accessory_suffix(entry_name, video_stem):
    if not _accessory_matches(entry_name, video_stem):
        return ""
    return entry_name[len(video_stem):]


def _classify_accessory(entry_name, video_stem):
    suffix = _accessory_suffix(entry_name, video_stem)
    _, ext = os.path.splitext(entry_name)
    lower_suffix = suffix.lower()
    ext = ext.lower()
    role = "unknown"
    if ext == ".nfo":
        role = "nfo"
    elif ext in {".srt", ".ass", ".ssa", ".vtt", ".sub"}:
        role = "subtitle"
    elif ext == ".bif":
        role = "bif"
    elif "-thumb" in lower_suffix:
        role = "thumb"
    elif "-poster" in lower_suffix:
        role = "poster"
    elif "-background" in lower_suffix:
        role = "background"
    elif "-clearlogo" in lower_suffix:
        role = "clearlogo"
    elif "-performer-" in lower_suffix and lower_suffix.endswith(f"-image{ext}"):
        role = "performer"
    return {
        "role": role,
        "suffix": suffix,
        "equivalence_key": f"{role}:{lower_suffix}",
        "renameable": role != "unknown",
    }


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
            item.update(_classify_accessory(entry, video_stem))
            accessories.append(item)
    accessories.sort(key=lambda item: item["name"].lower())
    return accessories


def _collect_videos(root_path, settings=None, lib_root=LIB_ROOT):
    settings = settings or duplicate_settings()
    excluded = {str(item).lower() for item in settings.get("excluded_folders") or set()}
    move_root = os.path.realpath(_effective_move_root(settings, lib_root) or "")
    videos = []
    for base, dirs, files in os.walk(root_path, followlinks=False):
        dirs[:] = [
            d
            for d in dirs
            if d != QUARANTINE_DIRNAME
            and d.lower() not in excluded
            and not os.path.islink(os.path.join(base, d))
            and not (
                move_root
                and path_is_under(os.path.join(base, d), move_root)
                and path_is_under(move_root, lib_root)
            )
        ]
        for filename in files:
            path = os.path.join(base, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if os.path.splitext(filename)[1].lower() in VIDEO_EXTS:
                videos.append(os.path.realpath(path))
    videos.sort(key=lambda path: path.lower())
    return videos


def _balanced_group_key(path):
    folder = os.path.normcase(os.path.realpath(os.path.dirname(path)))
    nfo = parse_nfo_identity(path)
    if nfo.get("unique_ids"):
        return (folder, "nfo-id", "|".join(nfo["unique_ids"]))
    title = normalize_duplicate_name(nfo.get("title") or nfo.get("sorttitle") or "")
    premiered = str(nfo.get("premiered") or "").strip().lower()
    studio = normalize_duplicate_name(nfo.get("studio") or "")
    if title and premiered:
        return (folder, "nfo-meta", studio, title, premiered)
    stem = os.path.splitext(os.path.basename(path))[0]
    return (folder, "stem", normalize_duplicate_name(stem))


def _candidate_groups(videos, settings=None):
    settings = settings or duplicate_settings()
    mode = settings.get("grouping_mode") or "balanced"
    buckets = {}
    for path in videos:
        folder = os.path.dirname(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        folder_key = os.path.normcase(os.path.realpath(folder))
        if mode == "folder":
            key = (folder_key, "folder")
        elif mode == "strict":
            key = (folder_key, "stem", normalize_duplicate_name(stem))
        else:
            key = _balanced_group_key(path)
        buckets.setdefault(key, []).append(path)
    return [items for items in buckets.values() if len(items) > 1]


def _set_scan_progress(scan, percent, label, **values):
    with maintenance_lock:
        scan["progress_percent"] = max(0, min(100, int(percent)))
        scan["progress_label"] = label
        scan.update(values)


def _accessory_destination(accessory, keep_video):
    suffix = accessory.get("suffix") or ""
    if not suffix:
        return ""
    return os.path.realpath(
        os.path.join(
            os.path.dirname(keep_video.get("path", "")),
            f"{keep_video.get('stem', '')}{suffix}",
        )
    )


def _folder_equivalent_exists(accessory):
    if accessory.get("role") != "clearlogo":
        return False
    folder = os.path.dirname(accessory.get("path", ""))
    ext = accessory.get("ext") or ""
    return os.path.isfile(os.path.join(folder, f"clearlogo{ext}"))


def _equivalent_accessory_exists(accessory, keep_video):
    if _folder_equivalent_exists(accessory):
        return True
    target = _accessory_destination(accessory, keep_video)
    return bool(target and os.path.isfile(target))


def _default_accessory_operation(accessory, keep_video, action, settings, lib_root):
    policy = settings.get("accessory_policy") or "rename_unmatched"
    if policy == "remove_all" or _equivalent_accessory_exists(accessory, keep_video):
        return action, "", ""
    if policy == "keep_unmatched":
        return "keep", "", "No keeper-side equivalent exists"
    if not accessory.get("renameable"):
        return "keep", "", "Unknown sidecar type; review manually"
    target = _accessory_destination(accessory, keep_video)
    if not target or not path_is_under(target, lib_root):
        return "keep", "", "Rename target is outside the library"
    if os.path.exists(target):
        return "keep", target, "Rename target already exists"
    return "rename", target, "Unmatched sidecar will be renamed to the keeper stem"


def _annotate_group_defaults(videos, recommended, settings, lib_root, action="move"):
    for video in videos:
        if video["id"] == recommended["id"]:
            video["default_operation"] = "keep"
            video["default_selected"] = False
        else:
            video["default_operation"] = action
            video["default_selected"] = True
        for accessory in video.get("accessories") or []:
            if video["id"] == recommended["id"]:
                accessory["default_operation"] = "keep"
                accessory["default_selected"] = False
                continue
            operation, target, reason = _default_accessory_operation(
                accessory,
                recommended,
                action,
                settings,
                lib_root,
            )
            accessory["default_operation"] = operation
            accessory["default_destination_path"] = target
            accessory["default_reason"] = reason
            accessory["default_selected"] = operation != "keep"


def _duration_clusters(videos):
    clusters = []
    for video in videos:
        duration = (video.get("metadata") or {}).get("duration_seconds")
        placed = False
        for cluster in clusters:
            reference = (cluster[0].get("metadata") or {}).get("duration_seconds")
            if _duration_close(duration, reference):
                cluster.append(video)
                placed = True
                break
        if not placed:
            clusters.append([video])
    return [cluster for cluster in clusters if len(cluster) > 1]


def _group_payload_from_videos(videos, group_id, lib_root, settings):
    videos.sort(key=lambda video: _keeper_sort_key(video, settings.get("keeper_rule")))
    recommended = videos[0]
    remove_ids = [video["id"] for video in videos[1:]]
    _annotate_group_defaults(videos, recommended, settings, lib_root)
    reclaimable = 0
    accessory_count = 0
    for video in videos[1:]:
        reclaimable += video.get("size_bytes") or 0
        for item in video.get("accessories") or []:
            accessory_count += 1
            if item.get("default_operation") in {"move", "delete"}:
                reclaimable += item.get("size_bytes") or 0

    folder = os.path.dirname(videos[0]["path"])
    normalized_name = normalize_duplicate_name(os.path.splitext(videos[0]["name"])[0])
    return {
        "id": group_id,
        "folder": folder,
        "normalized_name": normalized_name,
        "videos": videos,
        "recommended_keep_id": recommended["id"],
        "remove_ids": remove_ids,
        "reclaimable_bytes": reclaimable,
        "reclaimable_label": format_size(reclaimable),
        "accessory_count": accessory_count,
    }


def _build_groups(paths, group_index, lib_root, settings):
    videos = []
    for path in paths:
        item = _file_payload(path, "video", lib_root)
        if not item:
            continue
        metadata = probe_video_metadata(path)
        item["metadata"] = metadata
        item["metadata_label"] = _metadata_label(metadata)
        item["nfo_identity"] = parse_nfo_identity(path)
        item["accessories"] = find_accessory_files(path, lib_root)
        for accessory in item["accessories"]:
            accessory["parent_video_id"] = item["id"]
        videos.append(item)

    if len(videos) < 2:
        return []

    clusters = _duration_clusters(videos) if settings.get("grouping_mode") == "balanced" else [videos]
    groups = []
    for offset, cluster in enumerate(clusters):
        groups.append(
            _group_payload_from_videos(
                cluster,
                f"group-{group_index + offset}",
                lib_root,
                settings,
            )
        )
    return groups


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
        "role": item.get("role", ""),
        "suffix": item.get("suffix", ""),
        "default_operation": item.get("default_operation", ""),
        "default_destination_path": item.get("default_destination_path", ""),
        "default_reason": item.get("default_reason", ""),
        "default_selected": bool(item.get("default_selected")),
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
        "settings": public_duplicate_settings(scan.get("settings")),
        "groups": [_public_group(group) for group in scan.get("groups") or []],
    }


def _run_scan(scan, lib_root):
    try:
        settings = scan.get("settings") or duplicate_settings()
        now = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning folders",
            status="running",
            _started_ts=now,
            started_at=utc_iso(now),
        )
        videos = _collect_videos(scan["path"], settings=settings, lib_root=lib_root)
        _set_scan_progress(
            scan,
            10,
            f"Found {len(videos)} video file{'s' if len(videos) != 1 else ''}",
            scanned_video_count=len(videos),
        )

        candidates = _candidate_groups(videos, settings=settings)
        groups = []
        total = len(candidates)
        group_index = 1
        for index, paths in enumerate(candidates, start=1):
            built_groups = _build_groups(paths, group_index, lib_root, settings)
            if built_groups:
                groups.extend(built_groups)
                group_index += len(built_groups)
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
    settings = duplicate_settings()
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
        "settings": settings,
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


def _validate_move_root(move_root, lib_root):
    move_root = str(move_root or "").strip()
    if not move_root:
        move_root = os.path.join(os.path.realpath(lib_root), QUARANTINE_DIRNAME)
    real = os.path.realpath(move_root)
    if os.path.islink(real) or not path_is_under(real, lib_root):
        return None, "Move destination must be inside the mounted library root"
    return real, None


def quarantine_destination(path, lib_root, scan_id, used_destinations=None, move_root=None):
    lib_real = os.path.realpath(lib_root)
    move_real = os.path.realpath(move_root or os.path.join(lib_real, QUARANTINE_DIRNAME))
    rel = os.path.relpath(os.path.realpath(path), lib_real)
    return os.path.realpath(os.path.join(move_real, rel))


def _build_plan_item(
    item,
    group_id,
    operation,
    action,
    lib_root,
    scan_id,
    used_destinations,
    move_root,
    destination_path="",
):
    source = item.get("path", "")
    dest = destination_path or ""
    if operation == "move":
        dest = quarantine_destination(source, lib_root, scan_id, used_destinations, move_root)
    return {
        "file_id": item.get("id", ""),
        "group_id": group_id,
        "kind": item.get("kind", ""),
        "operation": operation,
        "action": action,
        "source_path": source,
        "relative_path": os.path.relpath(os.path.realpath(source), os.path.realpath(lib_root)),
        "destination_path": dest,
        "source_name": os.path.basename(source),
        "destination_name": os.path.basename(dest) if dest else "",
        "size_bytes": item.get("size_bytes", 0),
        "size_label": item.get("size_label", ""),
        "identity": dict(item.get("identity") or {}),
    }


def _file_operation_overrides(override):
    raw = override.get("file_operations") if isinstance(override, dict) else None
    operations = {}
    if not isinstance(raw, list):
        return operations
    for item in raw:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("file_id") or "")
        operation = str(item.get("operation") or "").strip().lower()
        if file_id and operation in {"default", "keep", "rename", "cleanup", "move", "delete"}:
            operations[file_id] = operation
    return operations


def _planned_operation(item, keep_video, action, settings, lib_root, override_operation=""):
    if override_operation == "keep":
        return "keep", "", "Manually excluded"
    if item.get("kind") == "video":
        if override_operation in {"move", "delete"}:
            return override_operation, "", ""
        return action, "", ""
    default_operation, default_target, reason = _default_accessory_operation(
        item,
        keep_video,
        action,
        settings,
        lib_root,
    )
    if override_operation in {"move", "delete"}:
        return override_operation, "", "Manual cleanup override"
    if override_operation == "cleanup":
        return action, "", "Manual cleanup override"
    if override_operation == "rename":
        target = default_target or _accessory_destination(item, keep_video)
        return "rename", target, "Manual rename override"
    return default_operation, default_target, reason


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

    settings = scan.get("settings") or duplicate_settings()
    move_root, move_err = _validate_move_root(_effective_move_root(settings, lib_root), lib_root)
    if action == "move" and move_err:
        return None, move_err

    plan_id = _now_id()
    used_destinations = set()
    files = []
    skipped_groups = []
    manual_review = []
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
        operation_overrides = _file_operation_overrides(override)

        for file_id in selected_ids:
            item = candidate_files[file_id]
            source = item.get("path", "")
            if not path_is_under(source, lib_real):
                continue
            keep_video = videos.get(keep_id)
            operation, destination_path, reason = _planned_operation(
                item,
                keep_video,
                action,
                settings,
                lib_real,
                operation_overrides.get(file_id, ""),
            )
            if operation == "keep":
                manual_review.append(
                    {
                        "file_id": file_id,
                        "group_id": group["id"],
                        "path": source,
                        "reason": reason or "Kept for manual review",
                    }
                )
                continue
            if operation == "rename":
                if not destination_path or not path_is_under(destination_path, lib_real):
                    manual_review.append(
                        {
                            "file_id": file_id,
                            "group_id": group["id"],
                            "path": source,
                            "reason": "Rename target is outside the library",
                        }
                    )
                    continue
                if os.path.exists(destination_path):
                    manual_review.append(
                        {
                            "file_id": file_id,
                            "group_id": group["id"],
                            "path": source,
                            "reason": "Rename target already exists",
                        }
                    )
                    continue
            files.append(
                _build_plan_item(
                    item,
                    group["id"],
                    operation,
                    action,
                    lib_real,
                    scan_id,
                    used_destinations,
                    move_root,
                    destination_path=destination_path,
                )
            )

    total_size = sum(
        item.get("size_bytes") or 0
        for item in files
        if item.get("operation") in {"move", "delete"}
    )
    plan = {
        "id": plan_id,
        "scan_id": scan_id,
        "action": action,
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": lib_real,
        "move_root": move_root if action == "move" else "",
        "configured_move_root": move_root,
        "files": files,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "skipped_groups": skipped_groups,
        "manual_review": manual_review,
        "settings": settings,
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
        "configured_move_root": plan.get("configured_move_root", ""),
        "file_count": plan.get("file_count", 0),
        "total_size_bytes": plan.get("total_size_bytes", 0),
        "total_size_label": plan.get("total_size_label", ""),
        "skipped_groups": list(plan.get("skipped_groups") or []),
        "manual_review": list(plan.get("manual_review") or []),
        "files": [
            {
                "file_id": item.get("file_id", ""),
                "group_id": item.get("group_id", ""),
                "kind": item.get("kind", ""),
                "operation": item.get("operation", ""),
                "source_path": item.get("source_path", ""),
                "source_name": item.get("source_name", ""),
                "relative_path": item.get("relative_path", ""),
                "destination_path": item.get("destination_path", ""),
                "destination_name": item.get("destination_name", ""),
                "size_bytes": item.get("size_bytes", 0),
                "size_label": item.get("size_label", ""),
            }
            for item in plan.get("files") or []
        ],
    }


def _refusal(file_id, path, reason):
    return {"file_id": file_id, "path": path, "reason": reason}


def _read_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default
    return data if isinstance(data, dict) else default


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def _log_record(record):
    return json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"


def _write_cleanup_log(plan, result, records):
    os.makedirs(MAINTENANCE_LOG_DIR, exist_ok=True)
    log_id = f"{plan.get('id', _now_id())}.jsonl"
    path = os.path.join(MAINTENANCE_LOG_DIR, log_id)
    header = {
        "type": "summary",
        "timestamp": utc_iso(),
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": plan.get("action", ""),
        "applied_count": result.get("applied_count", 0),
        "missing_count": result.get("missing_count", 0),
        "refused_count": result.get("refused_count", 0),
        "total_applied_bytes": result.get("total_applied_bytes", 0),
        "move_root": plan.get("move_root", ""),
    }
    written = 0
    truncated = False
    with open(path, "w", encoding="utf-8") as f:
        line = _log_record(header)
        f.write(line)
        written += len(line.encode("utf-8"))
        for record in records:
            line = _log_record(record)
            size = len(line.encode("utf-8"))
            if written + size > MAINTENANCE_LOG_MAX_BYTES:
                truncated = True
                break
            f.write(line)
            written += size
        if truncated:
            trunc = _log_record(
                {
                    "type": "truncated",
                    "timestamp": utc_iso(),
                    "message": "Log reached maximum size; remaining records were omitted.",
                }
            )
            f.write(trunc)
            written += len(trunc.encode("utf-8"))

    index = _read_json(MAINTENANCE_LOG_INDEX, {"logs": []})
    logs = [item for item in index.get("logs", []) if item.get("id") != log_id]
    entry = {
        "id": log_id,
        "path": path,
        "created_at": header["timestamp"],
        "plan_id": header["plan_id"],
        "scan_id": header["scan_id"],
        "action": header["action"],
        "applied_count": header["applied_count"],
        "missing_count": header["missing_count"],
        "refused_count": header["refused_count"],
        "size_bytes": os.path.getsize(path),
        "size_label": format_size(os.path.getsize(path)),
        "truncated": truncated,
    }
    logs.insert(0, entry)
    for old in logs[MAINTENANCE_LOG_RETENTION_COUNT:]:
        try:
            os.remove(old.get("path", ""))
        except OSError:
            pass
    _write_json(MAINTENANCE_LOG_INDEX, {"logs": logs[:MAINTENANCE_LOG_RETENTION_COUNT]})
    return entry


def list_duplicate_cleanup_logs():
    index = _read_json(MAINTENANCE_LOG_INDEX, {"logs": []})
    logs = []
    for item in index.get("logs") or []:
        public = dict(item)
        public.pop("path", None)
        logs.append(public)
    return logs


def read_duplicate_cleanup_log(log_id):
    clean_id = os.path.basename(str(log_id or ""))
    index = _read_json(MAINTENANCE_LOG_INDEX, {"logs": []})
    match = next((item for item in index.get("logs") or [] if item.get("id") == clean_id), None)
    if not match:
        return None, "Log not found"
    path = match.get("path", "")
    if not path_is_under(path, MAINTENANCE_LOG_DIR) or not os.path.isfile(path):
        return None, "Log not found"
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return {
            "id": clean_id,
            "content": f.read(MAINTENANCE_LOG_MAX_BYTES),
            "size_label": match.get("size_label", ""),
            "truncated": bool(match.get("truncated")),
        }, None


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
    log_records = []
    total = 0

    for item in plan.get("files") or []:
        source = item.get("source_path", "")
        file_id = item.get("file_id", "")
        operation = item.get("operation") or action
        if not source or not path_is_under(source, lib_root):
            refusal = _refusal(file_id, source, "Source is outside the library")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            continue
        if os.path.islink(source):
            refusal = _refusal(file_id, source, "Symlinks are not cleaned")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            continue
        if not os.path.exists(source):
            missing.append(file_id)
            log_records.append(
                {
                    "type": "file",
                    "result": "missing",
                    "file_id": file_id,
                    "old_path": source,
                    "old_name": os.path.basename(source),
                    "operation": operation,
                }
            )
            continue
        if not _identity_matches(source, item.get("identity")):
            refusal = _refusal(file_id, source, "File changed after scan")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            continue

        try:
            dest = item.get("destination_path", "")
            if operation == "delete":
                os.remove(source)
            elif operation == "move":
                if not dest or not path_is_under(dest, move_root) or not path_is_under(dest, lib_root):
                    refusal = _refusal(file_id, source, "Destination is outside quarantine")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    continue
                if os.path.exists(dest):
                    refusal = _refusal(file_id, source, "Destination already exists")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.move(source, dest)
            elif operation == "rename":
                if not dest or not path_is_under(dest, lib_root):
                    refusal = _refusal(file_id, source, "Rename target is outside the library")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    continue
                if os.path.exists(dest):
                    refusal = _refusal(file_id, source, "Rename target already exists")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    continue
                os.rename(source, dest)
            else:
                refusal = _refusal(file_id, source, "Unsupported operation")
                refused.append(refusal)
                log_records.append({"type": "file", "result": "refused", **refusal})
                continue
        except Exception as exc:
            refusal = _refusal(file_id, source, str(exc))
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            continue

        applied_item = {
            "file_id": file_id,
            "operation": operation,
            "source_path": source,
            "destination_path": item.get("destination_path", ""),
            "source_name": item.get("source_name") or os.path.basename(source),
            "destination_name": item.get("destination_name", ""),
            "size_bytes": item.get("size_bytes", 0),
            "size_label": item.get("size_label", ""),
        }
        applied.append(applied_item)
        log_records.append(
            {
                "type": "file",
                "timestamp": utc_iso(),
                "result": "applied",
                "file_id": file_id,
                "operation": operation,
                "old_path": source,
                "old_name": applied_item["source_name"],
                "new_path": item.get("destination_path", ""),
                "new_name": applied_item["destination_name"],
                "size_bytes": item.get("size_bytes", 0),
                "identity": item.get("identity") or {},
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
    log_entry = _write_cleanup_log(plan, result, log_records)
    result["log"] = {key: value for key, value in log_entry.items() if key != "path"}
    with maintenance_lock:
        plan["status"] = "applied"
        plan["applied_at"] = utc_iso()
        plan["last_result"] = result
    return result, None
