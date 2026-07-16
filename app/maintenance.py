import copy
import datetime
import hashlib
import json
import os
import re
import subprocess
import threading
import time

from . import app_settings
from . import duplicate_review_store
from . import emby_catalog
from . import emby_playback
from . import emby_sync
from . import emby_notifications
from . import impact_metrics
from . import maintenance_scan_store
from . import media_scope
from . import subtitle_quality
from . import task_progress
from .config import LIB_ROOT, STATE_ROOT, VIDEO_EXTS
from .file_safety import atomic_quarantine_file
from .file_safety import identity_matches as safe_identity_matches
from .file_safety import regular_file_identity
from .operation_gate import coordinated_library_operation, library_operation
from .progress import format_size, utc_iso
from .utils import path_is_under, resolve_case_insensitive


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


QUARANTINE_DIRNAME = ".vid2gif-duplicates"
SCAN_ACTIVE_STATUSES = {"queued", "running", "cancelling"}
SCAN_TERMINAL_STATUSES = {"success", "failed", "cancelled"}
PLAN_ACTIONS = {"move", "delete"}
MAINTENANCE_LOG_DIR = os.path.join(STATE_ROOT, "maintenance-logs", "duplicates")
MAINTENANCE_LOG_INDEX = os.path.join(MAINTENANCE_LOG_DIR, "index.json")
MAINTENANCE_LOG_RETENTION_COUNT = 25
MAINTENANCE_LOG_MAX_BYTES = 5 * 1024 * 1024
DUPLICATE_GROUP_PAGE_DEFAULT = 10
DUPLICATE_GROUP_PAGE_MAX = 100
DUPLICATE_GROUP_LARGE_RESULT_COUNT = 100
DUPLICATE_SCAN_RETENTION_COUNT = 10
DUPLICATE_SCAN_MAX_AGE_SECONDS = 24 * 60 * 60
DUPLICATE_APPLY_RETENTION_COUNT = 10
DUPLICATE_APPLY_MAX_AGE_SECONDS = 24 * 60 * 60
DUPLICATE_APPLY_LARGE_FILE_COUNT = 100
DUPLICATE_REFRESH_RETENTION_COUNT = 10
DUPLICATE_REFRESH_MAX_AGE_SECONDS = 24 * 60 * 60
FFPROBE_TIMEOUT_SECONDS = max(1, _env_int("FFPROBE_TIMEOUT_SECONDS", 30))
DUPLICATE_DISCOVERY_WORKFLOW = "duplicate_scan.discovery"
DUPLICATE_ANALYSIS_WORKFLOW = "duplicate_scan.analysis"
DUPLICATE_EMBY_WORKFLOW = "duplicate_scan.emby"
__test__ = False

duplicate_scans = {}
_duplicate_cache_loaded = False
cleanup_plans = {}
duplicate_apply_runs = {}
duplicate_refresh_runs = {}
duplicate_restore_plans = {}
maintenance_lock = threading.Lock()


class _ScanCancelled(Exception):
    pass

_QUALITY_PATTERNS = [
    r"\b(?:4320p|2160p|1440p|1080p|720p|576p|540p|480p|360p|4k|8k|uhd|fhd|hd)\b",
    r"\b(?:hdr10plus|hdr10|hdr|dv|dolby\s+vision|sdr)\b",
    r"\b(?:web\s*dl|web\s*rip|webrip|webdl|bluray|blu\s*ray|brrip|br\s*rip|hdtv|dvdrip|remux)\b",
    r"\b(?:x264|x265|h264|h265|hevc|avc|av1|vp9|mpeg2)\b",
    r"\b(?:aac|ac3|eac3|eac3\s*5\s*1|dts|truehd|atmos|flac|mp3)\b",
    r"\b(?:10bit|8bit|proper|repack|rerip|extended|unrated|theatrical|directors?\s*cut)\b",
]
_COPY_SUFFIX_RE = re.compile(
    r"(?:\s*\(\s*\d{1,3}\s*\)|[\s._-]+(?:copy(?:[\s._-]*\d+)?|duplicate|dupe))\s*$",
    flags=re.IGNORECASE,
)
_FULL_RELEASE_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})[\s._-]+(0?[1-9]|1[0-2])[\s._-]+(0?[1-9]|[12]\d|3[01])(?!\d)"
)
_COMPACT_RELEASE_DATE_RE = re.compile(
    r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)"
)
_RELEASE_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


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
    value = _COPY_SUFFIX_RE.sub("", str(stem or "").strip()).lower()
    value = re.sub(r"[\[\]\(\)\{\}]", " ", value)
    value = re.sub(r"[._\-]+", " ", value)
    for pattern in _QUALITY_PATTERNS:
        value = re.sub(pattern, " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\b(?:copy|duplicate|dupe)\s*\d*\b", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip()
    return value or str(stem or "").strip().lower()


def _release_date_token(value):
    text = str(value or "")
    match = _FULL_RELEASE_DATE_RE.search(text)
    if match:
        return f"{match.group(1)}-{int(match.group(2)):02d}-{int(match.group(3)):02d}"
    match = _COMPACT_RELEASE_DATE_RE.search(text)
    if match:
        return f"{match.group(1)}-{match.group(2)}-{match.group(3)}"
    match = _RELEASE_YEAR_RE.search(text)
    return match.group(1) if match else ""


def _title_without_release_date(value):
    text = _FULL_RELEASE_DATE_RE.sub(" ", str(value or ""))
    text = _COMPACT_RELEASE_DATE_RE.sub(" ", text)
    return _RELEASE_YEAR_RE.sub(" ", text)


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
    return regular_file_identity(path)


def _identity_matches(path, identity):
    return safe_identity_matches(path, identity)


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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return {}
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
        _copy_name_penalty(video.get("name")),
        str(video.get("path") or "").lower(),
    )


def _copy_name_penalty(name):
    stem = os.path.splitext(str(name or ""))[0].strip()
    return int(
        bool(
            re.search(
                r"(?:\s*\(\d+\)|[\s._-]+(?:copy(?:[\s._-]*\d+)?|duplicate|dupe))$",
                stem,
                flags=re.IGNORECASE,
            )
        )
    )


def _canonical_copy_stem(name):
    stem = os.path.splitext(os.path.basename(str(name or "")))[0].strip()
    canonical = re.sub(
        r"(?:\s*\(\d+\)|[\s._-]+(?:copy(?:[\s._-]*\d+)?|duplicate|dupe))$",
        "",
        stem,
        flags=re.IGNORECASE,
    ).rstrip(" ._-")
    return canonical if canonical and canonical != stem else ""


def _canonical_keeper_renames(keep_video):
    canonical_stem = _canonical_copy_stem(keep_video.get("name"))
    if not canonical_stem:
        return []
    folder = os.path.dirname(keep_video.get("path", ""))
    renames = [
        (
            keep_video,
            os.path.realpath(os.path.join(folder, f"{canonical_stem}{keep_video.get('ext', '')}")),
            "Copy-marked keeper will be renamed to the canonical filename",
        )
    ]
    for accessory in keep_video.get("accessories") or []:
        suffix = accessory.get("suffix") or ""
        if suffix:
            renames.append(
                (
                    accessory,
                    os.path.realpath(os.path.join(folder, f"{canonical_stem}{suffix}")),
                    "Keeper sidecar will follow the canonical video filename",
                )
            )
    return renames


def _keeper_sort_key(video, rule):
    rule = str(rule or "quality")
    if rule == "largest":
        return (
            -(video.get("size_bytes") or 0),
            _copy_name_penalty(video.get("name")),
            str(video.get("path") or "").lower(),
        )
    if rule == "newest":
        identity = video.get("identity") or {}
        return (
            -(identity.get("mtime_ns") or 0),
            _copy_name_penalty(video.get("name")),
            str(video.get("path") or "").lower(),
        )
    return _quality_sort_key(video)


def _file_payload(path, kind, lib_root, parent_video_id=""):
    identity = _stat_identity(path)
    if not identity:
        return None
    created_at = ""
    created_at_source = ""
    try:
        stat = os.stat(path, follow_symlinks=False)
        created_ts = getattr(stat, "st_birthtime", None)
        if created_ts:
            created_at = utc_iso(created_ts)
            created_at_source = "filesystem_birth_time"
        elif os.name == "nt" and stat.st_ctime:
            created_at = utc_iso(stat.st_ctime)
            created_at_source = "windows_creation_time"
    except OSError:
        pass
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
        "created_at": created_at,
        "created_at_source": created_at_source,
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


def find_folder_context_files(folder, videos, lib_root):
    represented_paths = set()
    for video in videos or []:
        if video.get("path"):
            represented_paths.add(os.path.normcase(os.path.realpath(video["path"])))
        for accessory in video.get("accessories") or []:
            if accessory.get("path"):
                represented_paths.add(os.path.normcase(os.path.realpath(accessory["path"])))

    context_files = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return context_files
    for entry in entries:
        full_path = os.path.join(folder, entry)
        if os.path.islink(full_path) or not os.path.isfile(full_path):
            continue
        if os.path.normcase(os.path.realpath(full_path)) in represented_paths:
            continue
        item = _file_payload(full_path, "folder_file", lib_root)
        if not item:
            continue
        item.update(
            {
                "role": "marker" if entry.lower() == ".posters_done" else "folder_file",
                "renameable": False,
                "default_operation": "keep",
                "default_selected": False,
                "default_reason": "Folder-level context is shown but excluded from duplicate cleanup",
            }
        )
        context_files.append(item)
    context_files.sort(key=lambda item: item["name"].lower())
    return context_files


def _scan_cancel_requested(scan):
    if not scan:
        return False
    with maintenance_lock:
        return bool(scan.get("cancel_requested"))


def _check_scan_cancelled(scan):
    if _scan_cancel_requested(scan):
        raise _ScanCancelled()


def _collect_videos(root_path, settings=None, lib_root=LIB_ROOT, scan=None):
    settings = settings or duplicate_settings()
    excluded = {str(item).lower() for item in settings.get("excluded_folders") or set()}
    move_root = os.path.realpath(_effective_move_root(settings, lib_root) or "")
    videos = []
    scanned_folders = 0
    for base, dirs, files in os.walk(root_path, followlinks=False):
        _check_scan_cancelled(scan)
        scanned_folders += 1
        dirs[:] = [
            d
            for d in dirs
            if d != QUARANTINE_DIRNAME
            and d.lower() not in excluded
            and not media_scope.is_non_main_video_dir(d)
            and not os.path.islink(os.path.join(base, d))
            and not (
                move_root
                and path_is_under(os.path.join(base, d), move_root)
                and path_is_under(move_root, lib_root)
            )
        ]
        for filename in files:
            _check_scan_cancelled(scan)
            path = os.path.join(base, filename)
            if os.path.islink(path) or not os.path.isfile(path):
                continue
            if os.path.splitext(filename)[1].lower() in VIDEO_EXTS:
                if media_scope.is_main_video_filename(filename):
                    videos.append(os.path.realpath(path))
        if scan and scanned_folders % 50 == 0:
            _set_scan_progress(
                scan,
                5,
                f"Scanning folders · {len(videos)} videos found",
                stage_workflow=DUPLICATE_DISCOVERY_WORKFLOW,
                completed_units=len(videos),
                remaining_stages=[
                    {"workflow": DUPLICATE_ANALYSIS_WORKFLOW},
                    {"workflow": DUPLICATE_EMBY_WORKFLOW},
                ],
                unit_label="videos",
                scanned_video_count=len(videos),
                scanned_folder_count=scanned_folders,
            )
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


def _settings_fingerprint(settings):
    public = public_duplicate_settings(settings)
    return _hash_text(json.dumps(public, sort_keys=True, separators=(",", ":")))[:24]


def _logical_group_key(path, settings):
    settings = settings or duplicate_settings()
    mode = settings.get("grouping_mode") or "balanced"
    folder = os.path.normcase(os.path.realpath(os.path.dirname(path)))
    stem = os.path.splitext(os.path.basename(path))[0]
    if mode == "folder":
        return (folder, "folder")
    if mode == "strict":
        return (folder, "stem", normalize_duplicate_name(stem))
    return _balanced_group_key(path)


def _group_review_key(path, settings, lib_root):
    folder = os.path.realpath(os.path.dirname(path))
    try:
        relative_folder = os.path.relpath(folder, os.path.realpath(lib_root))
    except (OSError, ValueError):
        relative_folder = folder
    logical = list(_logical_group_key(path, settings))[1:]
    value = {
        "folder": os.path.normcase(relative_folder).replace(os.sep, "/"),
        "mode": str((settings or {}).get("grouping_mode") or "balanced"),
        "logical": logical,
    }
    return "duplicate-review:" + _hash_text(
        json.dumps(value, sort_keys=True, separators=(",", ":"))
    )[:24]


def _folder_snapshot(folder):
    snapshot = {}
    try:
        entries = os.listdir(folder)
    except OSError:
        return snapshot
    for entry in entries:
        path = os.path.join(folder, entry)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        scope_name = entry
        non_main = False
        while scope_name:
            if media_scope.is_non_main_video_filename(scope_name):
                non_main = True
                break
            stem = os.path.splitext(scope_name)[0]
            if stem == scope_name:
                break
            scope_name = stem
        if non_main:
            continue
        try:
            value = os.stat(path, follow_symlinks=False)
        except OSError:
            continue
        snapshot[entry] = [
            int(value.st_size),
            int(getattr(value, "st_mtime_ns", value.st_mtime * 1_000_000_000)),
        ]
    return dict(sorted(snapshot.items(), key=lambda item: item[0].lower()))


def _folder_fingerprint(snapshot):
    return _hash_text(json.dumps(snapshot or {}, sort_keys=True, separators=(",", ":")))[:32]


def _folder_change_detail(original, current):
    original = original or {}
    current = current or {}
    original_names = set(original)
    current_names = set(current)
    return {
        "added": sorted(current_names - original_names, key=str.lower),
        "removed": sorted(original_names - current_names, key=str.lower),
        "modified": sorted(
            (
                name for name in original_names & current_names
                if original.get(name) != current.get(name)
            ),
            key=str.lower,
        ),
    }


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


def _protected_set_summary(paths, reason, *, title="", release_dates=None):
    video_paths = sorted(
        {os.path.realpath(path) for path in paths if path},
        key=str.lower,
    )
    if not video_paths:
        return None
    names = [os.path.basename(path) for path in video_paths]
    label = normalize_duplicate_name(title) if title else normalize_duplicate_name(
        os.path.splitext(names[0])[0]
    )
    set_id = "protected:" + hashlib.sha256(
        "|".join(os.path.normcase(path) for path in video_paths).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "id": set_id,
        "folder": os.path.dirname(video_paths[0]),
        "normalized_name": label,
        "video_count": len(video_paths),
        "video_paths": video_paths,
        "video_names": names,
        "release_dates": sorted({value for value in (release_dates or []) if value}),
        "reason": reason,
    }


def _related_release_sets(videos):
    """Find same-folder/title media with affirmative evidence of distinct releases."""
    buckets = {}
    for path in videos:
        nfo = parse_nfo_identity(path)
        stem = os.path.splitext(os.path.basename(path))[0]
        release_date = _release_date_token(nfo.get("premiered")) or _release_date_token(stem)
        title_source = nfo.get("title") or nfo.get("sorttitle") or _title_without_release_date(stem)
        title = normalize_duplicate_name(title_source)
        if not title:
            continue
        studio = normalize_duplicate_name(nfo.get("studio") or "")
        folder = os.path.normcase(os.path.realpath(os.path.dirname(path)))
        buckets.setdefault((folder, studio, title), []).append(
            {
                "path": path,
                "release_date": release_date,
                "provider_identity": "|".join(nfo.get("unique_ids") or []),
            }
        )

    protected = []
    for (_folder, _studio, title), records in buckets.items():
        if len(records) < 2:
            continue
        dates = [record["release_date"] for record in records]
        provider_ids = [record["provider_identity"] for record in records]
        distinct_dates = all(dates) and len(set(dates)) > 1
        distinct_provider_ids = all(provider_ids) and len(set(provider_ids)) > 1
        if not distinct_dates and not distinct_provider_ids:
            continue
        evidence = "different release dates" if distinct_dates else "different provider IDs"
        summary = _protected_set_summary(
            [record["path"] for record in records],
            f"Same-title videos have {evidence} and are excluded from duplicate cleanup",
            title=title,
            release_dates=dates,
        )
        if summary:
            protected.append(summary)
    return protected


def _set_scan_progress(scan, percent, label, **values):
    with maintenance_lock:
        task_progress.update_scan(scan, "duplicate_scan", percent, label, **values)


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

    _apply_subtitle_quality_defaults(videos, recommended, action, lib_root)


def _subtitle_accessory_buckets(videos):
    buckets = {}
    for video in videos:
        for accessory in video.get("accessories") or []:
            if accessory.get("role") != "subtitle" or accessory.get("ext") != ".srt":
                continue
            key = accessory.get("equivalence_key") or str(accessory.get("suffix") or "").lower()
            buckets.setdefault(key, []).append((video, accessory))
    return buckets


def _apply_subtitle_quality_defaults(videos, recommended, action, lib_root):
    for entries in _subtitle_accessory_buckets(videos).values():
        if len(entries) < 2:
            continue
        winner = subtitle_quality.clear_quality_winner([accessory for _video, accessory in entries])
        if not winner:
            continue
        winner_parent = next(
            (video for video, accessory in entries if accessory.get("id") == winner.get("id")),
            None,
        )
        if not winner_parent or winner_parent.get("id") == recommended.get("id"):
            continue
        target = _accessory_destination(winner, recommended)
        if not target or not path_is_under(target, lib_root):
            continue
        for video, accessory in entries:
            if accessory.get("id") == winner.get("id"):
                accessory.update(
                    default_operation="rename",
                    default_destination_path=target,
                    default_reason="Best subtitle coverage will be preserved under the keeper stem",
                    default_selected=True,
                )
            else:
                accessory.update(
                    default_operation=action,
                    default_destination_path="",
                    default_reason="A more complete matching subtitle will replace this file",
                    default_selected=True,
                )


def _group_default_action_counts(group):
    counts = {"keep": 0, "cleanup": 0, "rename": 0}
    for video in group.get("videos") or []:
        items = [video, *(video.get("accessories") or [])]
        for item in items:
            operation = str(item.get("default_operation") or "keep")
            if operation == "rename":
                counts["rename"] += 1
            elif operation in {"move", "delete", "cleanup"}:
                counts["cleanup"] += 1
            else:
                counts["keep"] += 1
    return counts


def _group_review_flags(group):
    flags = []
    accessory_buckets = {}
    unknown_count = 0
    for video in group.get("videos") or []:
        for accessory in video.get("accessories") or []:
            if accessory.get("role") == "unknown":
                unknown_count += 1
            key = accessory.get("equivalence_key") or (
                f"{accessory.get('role', 'accessory')}:{str(accessory.get('suffix') or '').lower()}"
            )
            accessory_buckets.setdefault(key, []).append(accessory)

    for accessories in accessory_buckets.values():
        sizes = {int(item.get("size_bytes") or 0) for item in accessories}
        if len(accessories) < 2 or len(sizes) < 2:
            continue
        role = str(accessories[0].get("role") or "accessory")
        if role == "subtitle" and subtitle_quality.clear_quality_winner(accessories):
            continue
        flags.append(
            {
                "kind": "different_size_accessories",
                "role": role,
                "file_count": len(accessories),
                "label": f"{len(accessories)} matching {role} files differ in size",
            }
        )
    if unknown_count:
        flags.append(
            {
                "kind": "unknown_accessories",
                "role": "unknown",
                "file_count": unknown_count,
                "label": f"{unknown_count} unrecognized sidecar file{'s' if unknown_count != 1 else ''}",
            }
        )
    if len(group.get("videos") or []) > 2:
        flags.append(
            {
                "kind": "multiple_video_candidates",
                "role": "video",
                "file_count": len(group.get("videos") or []),
                "label": f"{len(group.get('videos') or [])} video candidates",
            }
        )
    return flags


def _group_subtitle_signals(group):
    signals = []
    for entries in _subtitle_accessory_buckets(group.get("videos") or []).values():
        accessories = [accessory for _video, accessory in entries]
        if len(accessories) < 2:
            continue
        winner = subtitle_quality.clear_quality_winner(accessories)
        incomplete = [
            item for item in accessories
            if (item.get("subtitle_quality") or {}).get("status") == "likely_incomplete"
        ]
        if winner:
            quality = winner.get("subtitle_quality") or {}
            coverage = quality.get("coverage_percent")
            coverage_label = f" · {coverage:.1f}% coverage" if coverage is not None else ""
            replacement_label = (
                f"; {len(incomplete)} likely incomplete replacement"
                f"{'s' if len(incomplete) != 1 else ''}"
                if incomplete
                else "; automatic coverage choice"
            )
            signals.append(
                {
                    "kind": "subtitle_quality_choice",
                    "severity": "success",
                    "label": (
                        f"Best SRT: {winner.get('name', 'subtitle')}{coverage_label}"
                        f"{replacement_label}"
                    ),
                }
            )
        elif incomplete:
            signals.append(
                {
                    "kind": "subtitle_quality_review",
                    "severity": "warning",
                    "label": f"{len(incomplete)} SRT file{'s' if len(incomplete) != 1 else ''} likely incomplete",
                }
            )
    return signals


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
    return clusters


def _group_payload_from_videos(videos, group_id, lib_root, settings):
    videos.sort(key=lambda video: _keeper_sort_key(video, settings.get("keeper_rule")))
    recommended = videos[0]
    remove_ids = [video["id"] for video in videos[1:]]
    _annotate_group_defaults(videos, recommended, settings, lib_root)
    reclaimable = 0
    accessory_count = sum(len(video.get("accessories") or []) for video in videos)
    for video in videos[1:]:
        reclaimable += video.get("size_bytes") or 0
        for item in video.get("accessories") or []:
            if item.get("default_operation") in {"move", "delete"}:
                reclaimable += item.get("size_bytes") or 0

    folder = os.path.dirname(videos[0]["path"])
    folder_files = find_folder_context_files(folder, videos, lib_root)
    folder_snapshot = _folder_snapshot(folder)
    normalized_name = normalize_duplicate_name(os.path.splitext(videos[0]["name"])[0])
    impact_issue_id = "duplicate:" + hashlib.sha256(
        "|".join(sorted(video["id"] for video in videos)).encode("utf-8")
    ).hexdigest()[:24]
    return {
        "id": group_id,
        "impact_issue_id": impact_issue_id,
        "review_key": _group_review_key(videos[0]["path"], settings, lib_root),
        "settings_fingerprint": _settings_fingerprint(settings),
        "folder_snapshot": folder_snapshot,
        "folder_fingerprint": _folder_fingerprint(folder_snapshot),
        "folder": folder,
        "normalized_name": normalized_name,
        "videos": videos,
        "folder_files": folder_files,
        "recommended_keep_id": recommended["id"],
        "remove_ids": remove_ids,
        "reclaimable_bytes": reclaimable,
        "reclaimable_label": format_size(reclaimable),
        "accessory_count": accessory_count,
        "folder_file_count": len(folder_files),
        "keeper_rule": settings.get("keeper_rule") or "quality",
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
            if accessory.get("role") == "subtitle" and accessory.get("ext") == ".srt":
                accessory["subtitle_quality"] = subtitle_quality.analyze_srt(
                    accessory.get("path"), metadata.get("duration_seconds")
                )
        videos.append(item)

    if len(videos) < 2:
        return [], []

    clusters = _duration_clusters(videos) if settings.get("grouping_mode") == "balanced" else [videos]
    if settings.get("grouping_mode") == "balanced" and len(clusters) > 1:
        summary = _protected_set_summary(
            [video.get("path") for video in videos],
            "Different runtimes were treated as separate releases; this set is excluded from duplicate cleanup",
        )
        return [], [summary] if summary else []

    groups = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        groups.append(
            _group_payload_from_videos(
                cluster,
                f"group-{group_index + len(groups)}",
                lib_root,
                settings,
            )
        )
    return groups, []


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
        "created_at": item.get("created_at", ""),
        "created_at_source": item.get("created_at_source", ""),
        "parent_video_id": item.get("parent_video_id", ""),
        "role": item.get("role", ""),
        "suffix": item.get("suffix", ""),
        "equivalence_key": item.get("equivalence_key", ""),
        "renameable": bool(item.get("renameable")),
        "default_operation": item.get("default_operation", ""),
        "default_destination_path": item.get("default_destination_path", ""),
        "default_reason": item.get("default_reason", ""),
        "default_selected": bool(item.get("default_selected")),
        "emby_item_id": item.get("emby_item_id", ""),
        "emby_item_type": item.get("emby_item_type", ""),
        "emby_item_name": item.get("emby_item_name", ""),
        "emby_match_status": item.get("emby_match_status", ""),
        "emby_parent_item_id": item.get("emby_parent_item_id", ""),
        "emby_parent_item_type": item.get("emby_parent_item_type", ""),
    }
    if item.get("subtitle_quality") is not None:
        public["subtitle_quality"] = dict(item.get("subtitle_quality") or {})
    if item.get("metadata") is not None:
        public["metadata"] = item.get("metadata") or {}
        public["metadata_label"] = item.get("metadata_label", "")
    if "accessories" in item:
        public["accessories"] = [_public_file(child) for child in item.get("accessories") or []]
    return public


def _public_group(group, review_state=None):
    review_flags = _group_review_flags(group)
    review_state = copy.deepcopy(review_state or {
        "saved": False,
        "requires_review": False,
        "status": "default",
        "reason": "",
    })
    return {
        "id": group.get("id", ""),
        "review_key": group.get("review_key", ""),
        "folder_fingerprint": group.get("folder_fingerprint", ""),
        "review_state": review_state,
        "folder": group.get("folder", ""),
        "normalized_name": group.get("normalized_name", ""),
        "videos": [_public_file(video) for video in group.get("videos") or []],
        "folder_files": [_public_file(item) for item in group.get("folder_files") or []],
        "recommended_keep_id": group.get("recommended_keep_id", ""),
        "remove_ids": list(group.get("remove_ids") or []),
        "reclaimable_bytes": group.get("reclaimable_bytes", 0),
        "reclaimable_label": group.get("reclaimable_label", ""),
        "accessory_count": group.get("accessory_count", 0),
        "folder_file_count": group.get("folder_file_count", 0),
        "default_action_counts": _group_default_action_counts(group),
        "needs_review": bool(review_flags or review_state.get("requires_review")),
        "review_flags": review_flags,
        "subtitle_signals": _group_subtitle_signals(group),
    }


def _public_group_summary(group, review_state=None):
    videos = group.get("videos") or []
    recommended_id = group.get("recommended_keep_id", "")
    recommended = next((video for video in videos if video.get("id") == recommended_id), {})
    review_flags = _group_review_flags(group)
    review_state = copy.deepcopy(review_state or {
        "saved": False,
        "requires_review": False,
        "status": "default",
        "reason": "",
    })
    has_noncopy = any(not _copy_name_penalty(video.get("name")) for video in videos)
    recommended_is_copy = bool(_copy_name_penalty(recommended.get("name")))
    if recommended_is_copy and has_noncopy:
        recommended_reason = "Higher media quality outweighed the copy-number filename"
    elif has_noncopy and not recommended_is_copy:
        recommended_reason = "Preferred the original filename when media quality was otherwise tied"
    else:
        recommended_reason = "Best match under the configured keeper rule"
    return {
        "id": group.get("id", ""),
        "review_key": group.get("review_key", ""),
        "folder_fingerprint": group.get("folder_fingerprint", ""),
        "review_state": review_state,
        "folder": group.get("folder", ""),
        "normalized_name": group.get("normalized_name", ""),
        "recommended_keep_id": recommended_id,
        "recommended_keep_name": recommended.get("name", ""),
        "recommended_keep_reason": recommended_reason,
        "keeper_options": [
            {
                "id": video.get("id", ""),
                "name": video.get("name", ""),
                "metadata_label": video.get("metadata_label", ""),
                "size_label": video.get("size_label", ""),
                "copy_marked": bool(_copy_name_penalty(video.get("name"))),
            }
            for video in videos
        ],
        "video_count": len(videos),
        "accessory_count": group.get("accessory_count", 0),
        "folder_file_count": group.get("folder_file_count", 0),
        "reclaimable_bytes": group.get("reclaimable_bytes", 0),
        "reclaimable_label": group.get("reclaimable_label", ""),
        "default_action_counts": _group_default_action_counts(group),
        "needs_review": bool(review_flags or review_state.get("requires_review")),
        "review_flags": review_flags,
        "subtitle_signals": _group_subtitle_signals(group),
    }


def public_scan(scan, include_groups=False):
    if not scan:
        return None
    groups = list(scan.get("groups") or [])
    default_action_counts = {"keep": 0, "cleanup": 0, "rename": 0}
    for group in groups:
        for key, value in _group_default_action_counts(group).items():
            default_action_counts[key] += value
    protected_sets = list(scan.get("protected_distinct_sets") or [])
    protected_video_paths = {
        path
        for item in protected_sets
        for path in (item.get("video_paths") or [])
        if path
    }
    review_draft = duplicate_review_store.mapped_payload(scan) if scan.get("status") == "success" else {}
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
        "scanned_video_count": scan.get("scanned_video_count", 0),
        "duplicate_group_count": len(scan.get("groups") or []),
        "reclaimable_bytes": scan.get("reclaimable_bytes", 0),
        "reclaimable_label": format_size(scan.get("reclaimable_bytes", 0)),
        "settings": public_duplicate_settings(scan.get("settings")),
        "results_page_size": DUPLICATE_GROUP_PAGE_DEFAULT,
        "large_result": len(scan.get("groups") or []) >= DUPLICATE_GROUP_LARGE_RESULT_COUNT,
        "protected_distinct_set_count": len(protected_sets),
        "protected_distinct_video_count": len(protected_video_paths),
        "default_action_counts": default_action_counts,
        "review_group_count": sum(1 for group in groups if _group_review_flags(group)),
        "saved_review_group_count": review_draft.get("saved_group_count", 0),
        "review_required_count": review_draft.get("review_required_count", 0),
        "attention_group_count": sum(
            1
            for group in groups
            if _group_review_flags(group)
            or ((review_draft.get("groups") or {}).get(group.get("id")) or {}).get("requires_review")
        ),
        "emby_mapping": emby_catalog.public_summary(
            scan.get("emby_mapping"), app_settings.load_settings()
        ),
    }
    if include_groups:
        public["groups"] = [
            _public_group(group, duplicate_review_store.group_state(scan, group, review_draft))
            for group in scan.get("groups") or []
        ]
    public.update(maintenance_scan_store.public_cache_metadata("duplicates", scan))
    return public


def _upgrade_duplicate_scan_groups(scan):
    if not isinstance(scan, dict):
        return scan
    settings = scan.get("settings") or duplicate_settings()
    if not scan.get("settings_fingerprint"):
        scan["settings_fingerprint"] = _settings_fingerprint(settings)
    lib_root = os.path.realpath(scan.get("lib_root") or LIB_ROOT)
    for group in scan.get("groups") or []:
        videos = list(group.get("videos") or [])
        if not videos:
            continue
        folder = group.get("folder") or os.path.dirname(videos[0].get("path", ""))
        if not group.get("review_key"):
            group["review_key"] = _group_review_key(videos[0].get("path", ""), settings, lib_root)
        if not group.get("settings_fingerprint"):
            group["settings_fingerprint"] = _settings_fingerprint(settings)
        if not isinstance(group.get("folder_snapshot"), dict):
            # A pre-fingerprint cached scan has no trustworthy folder baseline.
            # Force one targeted refresh instead of silently blessing current files.
            group["folder_snapshot"] = {}
        if not group.get("folder_fingerprint"):
            group["folder_fingerprint"] = "legacy-scan-needs-refresh"
    return scan


def _ensure_duplicate_cache_loaded():
    global _duplicate_cache_loaded
    if _duplicate_cache_loaded:
        return
    restored = _upgrade_duplicate_scan_groups(
        maintenance_scan_store.restore_scan("duplicates")
    )
    with maintenance_lock:
        if restored and restored.get("id") not in duplicate_scans:
            duplicate_scans[restored["id"]] = restored
        _duplicate_cache_loaded = True


def _coerce_page(offset, limit):
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(limit)
    except (TypeError, ValueError):
        limit = DUPLICATE_GROUP_PAGE_DEFAULT
    offset = max(0, offset)
    limit = max(1, min(DUPLICATE_GROUP_PAGE_MAX, limit))
    return offset, limit


def _prune_duplicate_scans_locked(now=None):
    now = now or time.time()
    terminal_ids = [
        scan_id
        for scan_id, scan in duplicate_scans.items()
        if scan.get("status") in SCAN_TERMINAL_STATUSES
    ]
    for scan_id in terminal_ids:
        scan = duplicate_scans.get(scan_id) or {}
        finished = scan.get("_finished_ts") or scan.get("_created_ts") or now
        if not scan.get("_persisted_latest") and now - finished > DUPLICATE_SCAN_MAX_AGE_SECONDS:
            duplicate_scans.pop(scan_id, None)

    terminal = sorted(
        (
            (scan_id, scan)
            for scan_id, scan in duplicate_scans.items()
            if scan.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    removable = [item for item in terminal if not item[1].get("_persisted_latest")]
    for scan_id, _scan in removable[DUPLICATE_SCAN_RETENTION_COUNT:]:
        duplicate_scans.pop(scan_id, None)


def _active_duplicate_scan_locked():
    active = [
        scan
        for scan in duplicate_scans.values()
        if scan.get("status") in SCAN_ACTIVE_STATUSES
    ]
    if not active:
        return None
    return max(active, key=lambda item: item.get("_created_ts") or 0)


@coordinated_library_operation(
    "Scan duplicate videos", kind="scan", href="/maintenance#duplicates"
)
def _run_scan(scan, lib_root):
    try:
        settings = scan.get("settings") or duplicate_settings()
        _check_scan_cancelled(scan)
        now = time.time()
        _set_scan_progress(
            scan,
            1,
            "Scanning folders",
            stage_workflow=DUPLICATE_DISCOVERY_WORKFLOW,
            completed_units=0,
            remaining_stages=[
                {"workflow": DUPLICATE_ANALYSIS_WORKFLOW},
                {"workflow": DUPLICATE_EMBY_WORKFLOW},
            ],
            unit_label="videos",
            status="running",
            _started_ts=now,
            started_at=utc_iso(now),
        )
        videos = _collect_videos(
            scan["path"],
            settings=settings,
            lib_root=lib_root,
            scan=scan,
        )
        _check_scan_cancelled(scan)
        _set_scan_progress(
            scan,
            8,
            f"Found {len(videos)} video file{'s' if len(videos) != 1 else ''}",
            stage_workflow=DUPLICATE_DISCOVERY_WORKFLOW,
            completed_units=len(videos),
            total_units=len(videos),
            remaining_stages=[
                {"workflow": DUPLICATE_ANALYSIS_WORKFLOW},
                {"workflow": DUPLICATE_EMBY_WORKFLOW},
            ],
            unit_label="videos",
            scanned_video_count=len(videos),
        )

        protected_sets = {
            item["id"]: item
            for item in _related_release_sets(videos)
            if item.get("id")
        }
        protected_paths = {
            os.path.normcase(os.path.realpath(path))
            for item in protected_sets.values()
            for path in item.get("video_paths") or []
        }
        candidates = _candidate_groups(videos, settings=settings)
        groups = []
        total = len(candidates)
        group_index = 1
        _set_scan_progress(
            scan,
            10,
            f"Preparing {total} duplicate candidate group{'s' if total != 1 else ''}",
            stage_workflow=DUPLICATE_ANALYSIS_WORKFLOW,
            completed_units=0,
            total_units=total,
            remaining_stages=[{"workflow": DUPLICATE_EMBY_WORKFLOW}],
            unit_label="groups",
        )
        for index, paths in enumerate(candidates, start=1):
            _check_scan_cancelled(scan)
            actionable_paths = [
                path
                for path in paths
                if os.path.normcase(os.path.realpath(path)) not in protected_paths
            ]
            built_groups, built_protected = _build_groups(
                actionable_paths, group_index, lib_root, settings
            )
            if built_groups:
                groups.extend(built_groups)
                group_index += len(built_groups)
            for item in built_protected:
                if item.get("id"):
                    protected_sets[item["id"]] = item
            percent = 10 + int(80 * index / max(total, 1))
            _set_scan_progress(
                scan,
                percent,
                f"Checked {index} of {total} duplicate candidate groups",
                stage_workflow=DUPLICATE_ANALYSIS_WORKFLOW,
                completed_units=index,
                total_units=total,
                remaining_stages=[{"workflow": DUPLICATE_EMBY_WORKFLOW}],
                unit_label="groups",
            )

        _check_scan_cancelled(scan)
        reclaimable = sum(group.get("reclaimable_bytes") or 0 for group in groups)
        group_videos = [video for group in groups for video in group.get("videos") or []]
        _set_scan_progress(
            scan,
            92,
            f"Matching {len(group_videos)} duplicate video records with Emby",
            stage_workflow=DUPLICATE_EMBY_WORKFLOW,
            completed_units=0,
            total_units=len(group_videos),
            remaining_stages=[],
            unit_label="videos",
        )
        emby_mapping = emby_catalog.enrich_records(
            group_videos,
            app_settings.load_settings(),
            lambda video: video.get("path"),
            before_page=lambda: _check_scan_cancelled(scan),
        )
        for video in group_videos:
            for accessory in video.get("accessories") or []:
                accessory["emby_parent_item_id"] = video.get("emby_item_id", "")
                accessory["emby_parent_item_type"] = video.get("emby_item_type", "")
        finished = time.time()
        _set_scan_progress(
            scan,
            100,
            (
                f"Found {len(groups)} duplicate group{'s' if len(groups) != 1 else ''}; "
                f"protected {len(protected_sets)} distinct set{'s' if len(protected_sets) != 1 else ''}"
                if protected_sets
                else f"Found {len(groups)} duplicate group{'s' if len(groups) != 1 else ''}"
            ),
            status="success",
            groups=groups,
            reclaimable_bytes=reclaimable,
            protected_distinct_sets=list(protected_sets.values()),
            emby_mapping=emby_mapping,
            stage_workflow=DUPLICATE_EMBY_WORKFLOW,
            completed_units=len(group_videos),
            total_units=len(group_videos),
            remaining_stages=[],
            unit_label="videos",
            overall_units=len(videos),
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
        impact_metrics.record_scan(
            scan["id"],
            "duplicates",
            "duplicates",
            scan["path"],
            [
                {
                    "issue_id": group.get("impact_issue_id"),
                    "finding_ids": [group.get("impact_issue_id")],
                    "label": group.get("normalized_name") or "Duplicate group",
                    "path": group.get("folder") or scan["path"],
                }
                for group in groups
            ],
            timestamp=utc_iso(finished),
        )
        persisted = maintenance_scan_store.persist_success(
            "duplicates", "duplicates", scan, lib_root
        )
        if persisted:
            with maintenance_lock:
                for candidate in duplicate_scans.values():
                    candidate["_persisted_latest"] = candidate is scan
    except _ScanCancelled:
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


def start_duplicate_scan(path, lib_root=LIB_ROOT, synchronous=False):
    _ensure_duplicate_cache_loaded()
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
        "cancel_requested": False,
        "scanned_video_count": 0,
        "groups": [],
        "protected_distinct_sets": [],
        "reclaimable_bytes": 0,
        "lib_root": os.path.realpath(lib_root),
        "settings": settings,
        "settings_fingerprint": _settings_fingerprint(settings),
    }
    with maintenance_lock:
        _prune_duplicate_scans_locked()
        active_scan = _active_duplicate_scan_locked()
        if active_scan:
            return active_scan, None
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


def cancel_duplicate_scan(scan_id=None):
    target_id = str(scan_id or "")
    now = time.time()
    with maintenance_lock:
        _prune_duplicate_scans_locked(now)
        if target_id:
            scan = duplicate_scans.get(target_id)
        else:
            scan = _active_duplicate_scan_locked()
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
            scan.update(
                {
                    "status": "cancelling",
                    "progress_label": "Cancelling scan",
                }
            )
    return scan, None


def status_payload(scan_id=None):
    _ensure_duplicate_cache_loaded()
    with maintenance_lock:
        _prune_duplicate_scans_locked()
        if scan_id:
            scan = duplicate_scans.get(scan_id)
            if not scan:
                return None, "Scan not found"
        elif duplicate_scans:
            active = _active_duplicate_scan_locked()
            successful = [item for item in duplicate_scans.values() if item.get("status") == "success"]
            scan = active or (max(successful, key=lambda item: item.get("_finished_ts") or 0) if successful else max(duplicate_scans.values(), key=lambda item: item.get("_created_ts") or 0))
        else:
            scan = None
    return {"scan": public_scan(scan)}, None


def groups_payload(scan_id, offset=0, limit=DUPLICATE_GROUP_PAGE_DEFAULT, review="all"):
    _ensure_duplicate_cache_loaded()
    offset, limit = _coerce_page(offset, limit)
    with maintenance_lock:
        _prune_duplicate_scans_locked()
        scan = duplicate_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        groups = list(scan.get("groups") or [])

    review_draft = duplicate_review_store.mapped_payload(scan)
    review_states = review_draft.get("groups") or {}
    review = str(review or "all").strip().lower()
    if review == "attention":
        groups = [
            group for group in groups
            if _group_review_flags(group)
            or (review_states.get(group.get("id")) or {}).get("requires_review")
        ]
    elif review == "ready":
        groups = [
            group for group in groups
            if not _group_review_flags(group)
            and not (review_states.get(group.get("id")) or {}).get("requires_review")
        ]
    else:
        review = "all"

    total = len(groups)
    page = groups[offset : offset + limit]
    return {
        "scan": public_scan(scan),
        "offset": offset,
        "limit": limit,
        "total": total,
        "count": len(page),
        "has_previous": offset > 0,
        "has_next": offset + limit < total,
        "next_offset": offset + limit if offset + limit < total else None,
        "previous_offset": max(0, offset - limit) if offset > 0 else None,
        "large_result": total >= DUPLICATE_GROUP_LARGE_RESULT_COUNT,
        "review": review,
        "groups": [
            _public_group_summary(group, review_states.get(group.get("id")))
            for group in page
        ],
    }, None


def group_payload(scan_id, group_id, keep_video_id=""):
    _ensure_duplicate_cache_loaded()
    with maintenance_lock:
        _prune_duplicate_scans_locked()
        scan = duplicate_scans.get(str(scan_id or ""))
        if not scan:
            return None, "Scan not found"
        if scan.get("status") != "success":
            return None, "Scan is not complete"
        group = next(
            (item for item in scan.get("groups") or [] if item.get("id") == str(group_id or "")),
            None,
        )
    if not group:
        return None, "Group not found"
    projected = group
    keep_video_id = str(keep_video_id or "")
    if keep_video_id:
        projected = copy.deepcopy(group)
        keep_video = next(
            (video for video in projected.get("videos") or [] if video.get("id") == keep_video_id),
            None,
        )
        if not keep_video:
            return None, "Keeper not found"
        settings = scan.get("settings") or duplicate_settings()
        _annotate_group_defaults(
            projected.get("videos") or [],
            keep_video,
            settings,
            os.path.realpath(scan.get("lib_root") or LIB_ROOT),
        )
        projected["recommended_keep_id"] = keep_video_id
        projected["remove_ids"] = [
            video.get("id")
            for video in projected.get("videos") or []
            if video.get("id") != keep_video_id
        ]
    review_draft = duplicate_review_store.mapped_payload(scan)
    return {
        "group": _public_group(
            projected,
            (review_draft.get("groups") or {}).get(group.get("id")),
        )
    }, None


def review_draft_payload(scan_id):
    _ensure_duplicate_cache_loaded()
    with maintenance_lock:
        scan = duplicate_scans.get(str(scan_id or ""))
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    return {"review_draft": duplicate_review_store.mapped_payload(scan)}, None


def patch_review_draft(payload):
    payload = payload if isinstance(payload, dict) else {}
    scan_id = str(payload.get("scan_id") or "")
    _ensure_duplicate_cache_loaded()
    with maintenance_lock:
        scan = duplicate_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    return {"review_draft": duplicate_review_store.patch(scan, payload)}, None


def delete_review_draft(scan_id):
    _ensure_duplicate_cache_loaded()
    with maintenance_lock:
        scan = duplicate_scans.get(str(scan_id or ""))
    if not scan:
        return None, "Scan not found"
    duplicate_review_store.delete(scan)
    return {"review_draft": duplicate_review_store.mapped_payload(scan)}, None


def _prune_duplicate_refresh_runs_locked(now=None):
    now = now or time.time()
    terminal = sorted(
        (
            run for run in duplicate_refresh_runs.values()
            if run.get("status") in SCAN_TERMINAL_STATUSES
        ),
        key=lambda item: item.get("_finished_ts") or item.get("_created_ts") or 0,
        reverse=True,
    )
    for run in terminal[DUPLICATE_REFRESH_RETENTION_COUNT:]:
        duplicate_refresh_runs.pop(run.get("id"), None)
    for run in terminal:
        finished = run.get("_finished_ts") or run.get("_created_ts") or now
        if now - finished > DUPLICATE_REFRESH_MAX_AGE_SECONDS:
            duplicate_refresh_runs.pop(run.get("id"), None)


def _set_refresh_progress(run, percent, label, **values):
    with maintenance_lock:
        task_progress.update_scan(run, "duplicate_refresh", percent, label, **values)


def _public_refresh_run(run):
    if not run:
        return None
    return {
        "id": run.get("id", ""),
        "scan_id": run.get("scan_id", ""),
        "status": run.get("status", ""),
        "active": run.get("status") in SCAN_ACTIVE_STATUSES,
        **task_progress.public_fields(run),
        "error": run.get("error", ""),
        "folder_count": run.get("folder_count", 0),
        "processed_folder_count": run.get("processed_folder_count", 0),
        "refreshed_group_count": run.get("refreshed_group_count", 0),
        "removed_group_count": run.get("removed_group_count", 0),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "result": copy.deepcopy(run.get("result")) if run.get("status") == "success" else None,
    }


def _direct_main_videos(folder):
    videos = []
    try:
        entries = os.listdir(folder)
    except OSError:
        return videos
    for entry in entries:
        path = os.path.join(folder, entry)
        if os.path.islink(path) or not os.path.isfile(path):
            continue
        if os.path.splitext(entry)[1].lower() not in VIDEO_EXTS:
            continue
        if media_scope.is_main_video_filename(entry):
            videos.append(os.path.realpath(path))
    return sorted(videos, key=str.lower)


def _rebuilt_folder_groups(scan, folder, lib_root):
    settings = scan.get("settings") or duplicate_settings()
    videos = _direct_main_videos(folder)
    protected_sets = {
        item["id"]: item for item in _related_release_sets(videos) if item.get("id")
    }
    protected_paths = {
        os.path.normcase(os.path.realpath(path))
        for item in protected_sets.values()
        for path in item.get("video_paths") or []
    }
    groups = []
    group_index = 1
    for paths in _candidate_groups(videos, settings=settings):
        actionable_paths = [
            path for path in paths
            if os.path.normcase(os.path.realpath(path)) not in protected_paths
        ]
        built, built_protected = _build_groups(
            actionable_paths, group_index, lib_root, settings
        )
        groups.extend(built)
        group_index += len(built)
        for item in built_protected:
            if item.get("id"):
                protected_sets[item["id"]] = item

    group_videos = [video for group in groups for video in group.get("videos") or []]
    emby_catalog.enrich_records(
        group_videos,
        app_settings.load_settings(),
        lambda video: video.get("path"),
    )
    for video in group_videos:
        for accessory in video.get("accessories") or []:
            accessory["emby_parent_item_id"] = video.get("emby_item_id", "")
            accessory["emby_parent_item_type"] = video.get("emby_item_type", "")
    return groups, list(protected_sets.values())


def _run_duplicate_refresh(run):
    started = time.time()
    _set_refresh_progress(
        run,
        0,
        "Preparing changed folders",
        status="running",
        _started_ts=started,
        started_at=utc_iso(started),
    )
    try:
        scan_id = run.get("scan_id")
        with maintenance_lock:
            live_scan = duplicate_scans.get(scan_id)
            scan = copy.deepcopy(live_scan) if live_scan else None
        if not scan or scan.get("status") != "success":
            raise RuntimeError("Scan not found")
        lib_root = os.path.realpath(scan.get("lib_root") or LIB_ROOT)
        refresh_scan = copy.deepcopy(scan)
        refresh_scan["settings"] = copy.deepcopy(run.get("settings") or duplicate_settings())
        refresh_scan["settings_fingerprint"] = str(
            run.get("settings_fingerprint") or _settings_fingerprint(refresh_scan["settings"])
        )
        folders = list(run.get("folders") or [])
        original_groups = list(scan.get("groups") or [])
        original_by_folder = {}
        for group in original_groups:
            original_by_folder.setdefault(
                os.path.normcase(os.path.realpath(group.get("folder") or "")), []
            ).append(group)

        replacements = {}
        replacement_protected = {}
        refreshed_ids = []
        removed_ids = []
        removed_review_keys = []
        used_ids = {str(group.get("id") or "") for group in original_groups}
        for index, folder in enumerate(folders, start=1):
            folder_key = os.path.normcase(os.path.realpath(folder))
            _set_refresh_progress(
                run,
                int(90 * (index - 1) / max(len(folders), 1)),
                f"Refreshing folder {index} of {len(folders)}",
                processed_folder_count=index - 1,
                current_path=folder,
            )
            rebuilt, protected = _rebuilt_folder_groups(refresh_scan, folder, lib_root)
            old_groups = original_by_folder.get(folder_key, [])
            old_by_key = {group.get("review_key"): group for group in old_groups}
            matched_old_ids = set()
            for group in rebuilt:
                old = old_by_key.get(group.get("review_key"))
                if old:
                    group["id"] = old.get("id")
                    matched_old_ids.add(old.get("id"))
                else:
                    base = f"group-refresh-{_hash_text(group.get('review_key'))[:12]}"
                    candidate = base
                    suffix = 2
                    while candidate in used_ids:
                        candidate = f"{base}-{suffix}"
                        suffix += 1
                    group["id"] = candidate
                    used_ids.add(candidate)
                refreshed_ids.append(group.get("id"))
            removed_ids.extend(
                group.get("id") for group in old_groups
                if group.get("id") not in matched_old_ids
            )
            removed_review_keys.extend(
                group.get("review_key") for group in old_groups
                if group.get("id") not in matched_old_ids and group.get("review_key")
            )
            replacements[folder_key] = rebuilt
            replacement_protected[folder_key] = protected
            _set_refresh_progress(
                run,
                int(90 * index / max(len(folders), 1)),
                f"Refreshed folder {index} of {len(folders)}",
                processed_folder_count=index,
                current_path="",
            )

        refreshed_folder_keys = set(replacements)
        with maintenance_lock:
            live_scan = duplicate_scans.get(scan_id)
            if not live_scan:
                raise RuntimeError("Scan not found")
            updated_groups = [
                group for group in live_scan.get("groups") or []
                if os.path.normcase(os.path.realpath(group.get("folder") or "")) not in refreshed_folder_keys
            ]
            for folder in folders:
                updated_groups.extend(
                    replacements.get(os.path.normcase(os.path.realpath(folder)), [])
                )
            updated_groups.sort(
                key=lambda group: (
                    str(group.get("folder") or "").lower(),
                    str(group.get("normalized_name") or "").lower(),
                )
            )
            protected_sets = [
                item for item in live_scan.get("protected_distinct_sets") or []
                if os.path.normcase(os.path.realpath(item.get("folder") or "")) not in refreshed_folder_keys
            ]
            for items in replacement_protected.values():
                protected_sets.extend(items)
            live_scan["groups"] = updated_groups
            live_scan["protected_distinct_sets"] = protected_sets
            live_scan["settings"] = copy.deepcopy(refresh_scan["settings"])
            live_scan["settings_fingerprint"] = refresh_scan["settings_fingerprint"]
            live_scan["reclaimable_bytes"] = sum(
                int(group.get("reclaimable_bytes") or 0) for group in updated_groups
            )
            scan_for_persistence = live_scan
        duplicate_review_store.ensure_groups(
            scan_for_persistence,
            refreshed_ids,
            require_review=True,
            review_reason="This duplicate group was rebuilt from changed folder contents",
        )
        maintenance_scan_store.update_persisted_scan(
            "duplicates", scan_for_persistence, lib_root
        )
        duplicate_review_store.remove_review_keys(scan_for_persistence, removed_review_keys)
        finished = time.time()
        result = {
            "scan": public_scan(scan_for_persistence),
            "refreshed_group_ids": refreshed_ids,
            "removed_group_ids": removed_ids,
            "refreshed_group_count": len(refreshed_ids),
            "removed_group_count": len(removed_ids),
        }
        _set_refresh_progress(
            run,
            100,
            "Changed folders refreshed",
            status="success",
            result=result,
            refreshed_group_count=len(refreshed_ids),
            removed_group_count=len(removed_ids),
            processed_folder_count=len(folders),
            current_path="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
    except Exception as exc:
        finished = time.time()
        _set_refresh_progress(
            run,
            100,
            "Changed-folder refresh failed",
            status="failed",
            error=str(exc),
            current_path="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )


def start_duplicate_refresh(scan_id, group_ids, synchronous=False):
    scan_id = str(scan_id or "")
    with maintenance_lock:
        scan = duplicate_scans.get(scan_id)
        if not scan:
            return None, "Scan not found"
        groups_by_id = {group.get("id"): group for group in scan.get("groups") or []}
        folders = sorted(
            {
                os.path.realpath(groups_by_id[group_id].get("folder"))
                for group_id in group_ids or []
                if group_id in groups_by_id and groups_by_id[group_id].get("folder")
            },
            key=str.lower,
        )
        if not folders:
            return None, "No changed folders to refresh"
        for existing in duplicate_refresh_runs.values():
            if (
                existing.get("scan_id") == scan_id
                and existing.get("status") in SCAN_ACTIVE_STATUSES
                and set(existing.get("folders") or []) == set(folders)
            ):
                return existing, None
        _prune_duplicate_refresh_runs_locked()
        created = time.time()
        settings = duplicate_settings()
        run = {
            "id": _now_id(),
            "scan_id": scan_id,
            "status": "queued",
            "progress_percent": 0,
            "progress_label": "Queued changed-folder refresh",
            "error": "",
            "folders": folders,
            "folder_count": len(folders),
            "processed_folder_count": 0,
            "refreshed_group_count": 0,
            "removed_group_count": 0,
            "settings": settings,
            "settings_fingerprint": _settings_fingerprint(settings),
            "_created_ts": created,
            "created_at": utc_iso(created),
            "started_at": None,
            "finished_at": None,
        }
        duplicate_refresh_runs[run["id"]] = run
    if synchronous:
        _run_duplicate_refresh(run)
    else:
        threading.Thread(
            target=_run_duplicate_refresh,
            args=(run,),
            daemon=True,
            name=f"vid2gif-duplicate-refresh-{run['id']}",
        ).start()
    return run, None


def duplicate_refresh_status(refresh_id=None):
    with maintenance_lock:
        _prune_duplicate_refresh_runs_locked()
        if refresh_id:
            run = duplicate_refresh_runs.get(str(refresh_id or ""))
        elif duplicate_refresh_runs:
            run = max(
                duplicate_refresh_runs.values(),
                key=lambda item: item.get("_created_ts") or 0,
            )
        else:
            run = None
    if not run:
        return None, "Refresh run not found"
    return {"refresh": _public_refresh_run(run)}, None


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
        "emby_item_id": item.get("emby_item_id") or item.get("emby_parent_item_id") or "",
        "emby_item_type": item.get("emby_item_type") or item.get("emby_parent_item_type") or "",
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
    annotated_operation = str(item.get("default_operation") or "")
    if annotated_operation == "rename":
        default_operation = "rename"
        default_target = item.get("default_destination_path") or _accessory_destination(
            item, keep_video
        )
        reason = item.get("default_reason") or "Sidecar will be renamed to the keeper stem"
    elif annotated_operation == "keep":
        default_operation, default_target = "keep", ""
        reason = item.get("default_reason") or "Kept by the scan recommendation"
    elif annotated_operation in {"move", "delete", "cleanup"}:
        default_operation, default_target = action, ""
        reason = item.get("default_reason") or "Cleanup recommended by the scan"
    else:
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
    _ensure_duplicate_cache_loaded()
    if not isinstance(payload, dict):
        return None, "Invalid request"
    scan_id = str(payload.get("scan_id") or "")
    allowed, freshness_error = maintenance_scan_store.library_root_allowed(
        "duplicates", scan_id, lib_root
    )
    if not allowed:
        return None, freshness_error
    action = str(payload.get("action") or "move").strip().lower()
    if action not in PLAN_ACTIONS:
        return None, "Choose move or delete"
    overrides = _group_overrides(payload)
    if overrides is None:
        return None, "Group overrides are invalid"

    with maintenance_lock:
        _prune_duplicate_scans_locked()
        scan = duplicate_scans.get(scan_id)
    if not scan:
        return None, "Scan not found"
    if scan.get("status") != "success":
        return None, "Scan is not complete"
    groups_by_id = {group.get("id"): group for group in scan.get("groups") or []}
    settings = duplicate_settings()
    current_settings_fingerprint = _settings_fingerprint(settings)

    def unique_group_ids(values):
        result = []
        for value in values if isinstance(values, list) else []:
            group_id = str(value or "")
            if group_id and group_id not in result:
                result.append(group_id)
        return result

    selection = payload.get("selection")
    selection_mode = "visible_page"
    if isinstance(selection, dict) and selection.get("mode") == "all_eligible":
        selection_mode = "all_eligible"
        excluded_group_ids = set(unique_group_ids(selection.get("excluded_group_ids")))
        selected_group_ids = [
            group_id for group_id in groups_by_id if group_id not in excluded_group_ids
        ]
    elif isinstance(selection, dict) and selection.get("mode") == "explicit":
        selection_mode = "explicit"
        selected_group_ids = unique_group_ids(selection.get("group_ids"))
    else:
        selected_group_ids = unique_group_ids(payload.get("visible_group_ids"))
    if not selected_group_ids:
        return None, "Select at least one duplicate group"

    unknown_ids = [group_id for group_id in selected_group_ids if group_id not in groups_by_id]
    if unknown_ids:
        return None, "Selected duplicate groups are stale"
    if any(group_id not in groups_by_id for group_id in overrides):
        return None, "Group overrides are stale"

    requested_selected_group_ids = list(selected_group_ids)
    review_draft = duplicate_review_store.mapped_payload(scan)
    review_states = review_draft.get("groups") or {}
    for group_id, saved_state in review_states.items():
        if not saved_state.get("saved"):
            continue
        group = groups_by_id.get(group_id) or {}
        keep_video_id = str(
            saved_state.get("keep_video_id") or group.get("recommended_keep_id") or ""
        )
        known_file_ids = {
            str(item) for item in saved_state.get("known_file_ids") or [] if str(item or "")
        }
        included_file_ids = {
            str(item) for item in saved_state.get("include_file_ids") or [] if str(item or "")
        }
        for video in group.get("videos") or []:
            candidates = [*(video.get("accessories") or [])]
            if video.get("id") != keep_video_id:
                candidates.insert(0, video)
            for item in candidates:
                file_id = str(item.get("id") or "")
                if (
                    file_id
                    and file_id not in known_file_ids
                    and item.get("default_selected") is not False
                    and item.get("default_operation") != "keep"
                ):
                    included_file_ids.add(file_id)
        saved_override = {
            "id": group_id,
            "keep_video_id": keep_video_id,
            "include_file_ids": sorted(included_file_ids),
            "file_operations": copy.deepcopy(saved_state.get("file_operations") or []),
        }
        saved_override.update(overrides.get(group_id) or {})
        overrides[group_id] = saved_override
    current_snapshots = {}
    changed_groups = []
    stale_group_ids = []
    ready_group_ids = []
    for group_id in requested_selected_group_ids:
        group = groups_by_id[group_id]
        folder = os.path.realpath(group.get("folder") or "")
        folder_key = os.path.normcase(folder)
        if folder_key not in current_snapshots:
            current_snapshots[folder_key] = _folder_snapshot(folder)
        current_snapshot = current_snapshots[folder_key]
        current_fingerprint = _folder_fingerprint(current_snapshot)
        scanned_fingerprint = str(group.get("folder_fingerprint") or "")
        review_state = review_states.get(group_id) or {}
        folder_changed = current_fingerprint != scanned_fingerprint
        settings_changed = str(group.get("settings_fingerprint") or "") != current_settings_fingerprint
        review_required = bool(review_state.get("requires_review"))
        if folder_changed or settings_changed or review_required:
            detail = _folder_change_detail(group.get("folder_snapshot"), current_snapshot)
            reason = (
                "Files in this folder changed after the duplicate scan"
                if folder_changed
                else (
                    "Duplicate cleanup settings changed after this group was scanned"
                    if settings_changed
                    else review_state.get("reason") or "This saved review must be confirmed again"
                )
            )
            changed_groups.append(
                {
                    "group_id": group_id,
                    "review_key": group.get("review_key", ""),
                    "folder": folder,
                    "label": group.get("normalized_name") or "Duplicate group",
                    "reason": reason,
                    "added": detail["added"],
                    "removed": detail["removed"],
                    "modified": detail["modified"],
                    "refresh_required": folder_changed or settings_changed,
                    "review_required": review_required,
                    "settings_changed": settings_changed,
                }
            )
            if folder_changed or settings_changed:
                stale_group_ids.append(group_id)
            continue
        ready_group_ids.append(group_id)
    selected_group_ids = ready_group_ids

    move_root, move_err = _validate_move_root(_effective_move_root(settings, lib_root), lib_root)
    if action == "move" and move_err:
        return None, move_err

    plan_id = _now_id()
    used_destinations = set()
    files = []
    selected_group_set = set(requested_selected_group_ids)
    skipped_groups = [group_id for group_id in groups_by_id if group_id not in selected_group_set]
    skipped_groups.extend(
        group_id for group_id in requested_selected_group_ids
        if group_id not in ready_group_ids and group_id not in skipped_groups
    )
    manual_review = []
    impact_groups = []
    lib_real = os.path.realpath(lib_root)

    for group_id in selected_group_ids:
        group = groups_by_id[group_id]
        override = overrides.get(group["id"], {})
        if not _truthy(override.get("enabled"), default=True):
            skipped_groups.append(group["id"])
            continue

        videos = {video["id"]: video for video in group.get("videos") or []}
        keep_id = str(override.get("keep_video_id") or group.get("recommended_keep_id") or "")
        if keep_id not in videos:
            keep_id = group.get("recommended_keep_id")

        removable_video_ids = [video_id for video_id in videos if video_id != keep_id]
        impact_groups.append(
            {
                "issue_id": group.get("impact_issue_id", ""),
                "group_id": group["id"],
                "required_video_ids": list(removable_video_ids),
                "label": group.get("normalized_name") or "Duplicate group",
                "path": group.get("folder") or scan.get("path") or lib_real,
            }
        )
        raw_remove_ids = override.get("remove_video_ids")
        if isinstance(raw_remove_ids, list):
            allowed = {str(item) for item in raw_remove_ids}
            removable_video_ids = [video_id for video_id in removable_video_ids if video_id in allowed]

        candidate_files = {}
        removable_video_id_set = set(removable_video_ids)
        for video_id, video in videos.items():
            if video_id in removable_video_id_set:
                candidate_files[video_id] = video
            if video_id == keep_id or video_id in removable_video_id_set:
                for accessory in video.get("accessories") or []:
                    candidate_files[accessory["id"]] = accessory

        include_file_ids = override.get("include_file_ids")
        if isinstance(include_file_ids, list):
            selected_ids = [str(file_id) for file_id in include_file_ids if str(file_id) in candidate_files]
        else:
            selected_ids = [
                file_id
                for file_id, item in candidate_files.items()
                if item.get("default_selected") is not False
                and item.get("default_operation") != "keep"
            ]
        operation_overrides = _file_operation_overrides(override)

        planned_items = []
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
            planned_items.append((file_id, item, operation, destination_path, reason))

        keep_video = videos.get(keep_id) or {}
        canonical_stem = _canonical_copy_stem(keep_video.get("name"))
        if canonical_stem:
            canonicalized_items = []
            for file_id, item, operation, destination_path, reason in planned_items:
                expected_keeper_target = _accessory_destination(item, keep_video)
                if (
                    operation == "rename"
                    and item.get("kind") != "video"
                    and item.get("suffix")
                    and destination_path
                    and os.path.normcase(os.path.realpath(destination_path))
                    == os.path.normcase(os.path.realpath(expected_keeper_target))
                ):
                    destination_path = os.path.realpath(
                        os.path.join(
                            os.path.dirname(keep_video.get("path", "")),
                            f"{canonical_stem}{item.get('suffix')}",
                        )
                    )
                    reason = "Preserved sidecar will follow the canonical keeper filename"
                    if os.path.normcase(os.path.realpath(item.get("path", ""))) == os.path.normcase(destination_path):
                        continue
                canonicalized_items.append(
                    (file_id, item, operation, destination_path, reason)
                )
            planned_items = canonicalized_items
        already_planned = {file_id for file_id, *_rest in planned_items}
        cleanup_paths_before_renames = {
            os.path.normcase(os.path.realpath(item.get("path", "")))
            for _file_id, item, operation, _destination_path, _reason in planned_items
            if operation in {"move", "delete"} and item.get("path")
        }
        for item, destination_path, reason in _canonical_keeper_renames(keep_video):
            if item.get("id") in already_planned:
                continue
            if (
                os.path.lexists(destination_path)
                and os.path.normcase(destination_path) not in cleanup_paths_before_renames
            ):
                continue
            planned_items.append(
                (item.get("id", ""), item, "rename", destination_path, reason)
            )

        scheduled_cleanup_paths = {
            os.path.normcase(os.path.realpath(item.get("path", "")))
            for _file_id, item, operation, _destination_path, _reason in planned_items
            if operation in {"move", "delete"} and item.get("path")
        }
        for file_id, item, operation, destination_path, reason in planned_items:
            source = item.get("path", "")
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
                destination_is_scheduled_for_cleanup = (
                    os.path.normcase(os.path.realpath(destination_path)) in scheduled_cleanup_paths
                )
                if os.path.lexists(destination_path) and not destination_is_scheduled_for_cleanup:
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

    files.sort(
        key=lambda item: (
            str(item.get("group_id") or ""),
            1 if item.get("operation") == "rename" else 0,
            str(item.get("source_path") or "").lower(),
        )
    )

    total_size = sum(
        item.get("size_bytes") or 0
        for item in files
        if item.get("operation") in {"move", "delete"}
    )
    playback_group_ids = {item.get("group_id") for item in files}
    playback_targets = []
    for group_id in playback_group_ids:
        group = groups_by_id.get(group_id) or {}
        for video in group.get("videos") or []:
            playback_targets.append(
                {
                    "id": f"{group_id}:{video.get('id')}",
                    "group_id": group_id,
                    "local_path": video.get("path", ""),
                    "emby_item_id": video.get("emby_item_id", ""),
                    "ambiguous": video.get("emby_match_status") == "ambiguous",
                }
            )
    playback = emby_playback.check_targets(playback_targets, force=True)
    for item in files:
        item["emby_playback_status"] = emby_playback.group_status(
            playback, playback_targets, item.get("group_id")
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
        "visible_group_ids": requested_selected_group_ids,
        "visible_group_count": len(requested_selected_group_ids),
        "selection_mode": selection_mode,
        "selected_group_ids": requested_selected_group_ids,
        "selected_group_count": len(requested_selected_group_ids),
        "ready_group_ids": ready_group_ids,
        "ready_group_count": len(ready_group_ids),
        "changed_groups": changed_groups,
        "changed_group_count": len(changed_groups),
        "revalidation_checked_count": len(requested_selected_group_ids),
        "group_snapshots": [
            {
                "group_id": group_id,
                "folder": groups_by_id[group_id].get("folder", ""),
                "folder_fingerprint": groups_by_id[group_id].get("folder_fingerprint", ""),
            }
            for group_id in ready_group_ids
        ],
        "total_group_count": len(groups_by_id),
        "files": files,
        "file_count": len(files),
        "total_size_bytes": total_size,
        "total_size_label": format_size(total_size),
        "skipped_groups": skipped_groups,
        "manual_review": manual_review,
        "impact_groups": impact_groups,
        "settings": settings,
        "playback_targets": playback_targets,
        "emby_playback": playback,
        "refresh_run_id": "",
    }
    with maintenance_lock:
        cleanup_plans[plan_id] = plan
    if stale_group_ids:
        duplicate_review_store.ensure_groups(
            scan,
            stale_group_ids,
            overrides,
            require_review=True,
            review_reason="Files in this folder changed after the duplicate scan",
        )
        refresh_run, _refresh_error = start_duplicate_refresh(scan_id, stale_group_ids)
        if refresh_run:
            plan["refresh_run_id"] = refresh_run.get("id", "")
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
        "visible_group_ids": list(plan.get("visible_group_ids") or []),
        "visible_group_count": plan.get("visible_group_count", 0),
        "selection_mode": plan.get("selection_mode", "visible_page"),
        "selected_group_ids": list(plan.get("selected_group_ids") or []),
        "selected_group_count": plan.get("selected_group_count", plan.get("visible_group_count", 0)),
        "ready_group_ids": list(plan.get("ready_group_ids") or []),
        "ready_group_count": plan.get("ready_group_count", 0),
        "changed_groups": copy.deepcopy(plan.get("changed_groups") or []),
        "changed_group_count": plan.get("changed_group_count", 0),
        "revalidation_checked_count": plan.get("revalidation_checked_count", 0),
        "refresh_run_id": plan.get("refresh_run_id", ""),
        "total_group_count": plan.get("total_group_count", plan.get("visible_group_count", 0)),
        "file_count": plan.get("file_count", 0),
        "total_size_bytes": plan.get("total_size_bytes", 0),
        "total_size_label": plan.get("total_size_label", ""),
        "skipped_groups": list(plan.get("skipped_groups") or []),
        "manual_review": list(plan.get("manual_review") or []),
        "emby_playback": emby_playback.public_result(plan.get("emby_playback")),
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
                "emby_item_id": item.get("emby_item_id", ""),
                "emby_item_type": item.get("emby_item_type", ""),
                "emby_playback_status": item.get("emby_playback_status", "not_checked"),
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
        "deferred_count": result.get("deferred_count", 0),
        "skipped_changed_group_count": result.get("skipped_changed_group_count", 0),
        "deferred_bytes": result.get("deferred_bytes", 0),
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
        "deferred_count": header["deferred_count"],
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
        public["reversible"] = bool(
            public.get("action") == "move"
            and int(public.get("applied_count") or 0) > 0
        )
        public["restore_available"] = bool(
            public["reversible"] and not public.get("restored_at")
        )
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


def _cleanup_log_records(log_id):
    clean_id = os.path.basename(str(log_id or ""))
    index = _read_json(MAINTENANCE_LOG_INDEX, {"logs": []})
    match = next((item for item in index.get("logs") or [] if item.get("id") == clean_id), None)
    if not match:
        return None, None, "Log not found"
    path = match.get("path", "")
    if not path_is_under(path, MAINTENANCE_LOG_DIR) or not os.path.isfile(path):
        return None, None, "Log not found"
    records = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except (TypeError, ValueError):
                    continue
                if value.get("type") == "file" and value.get("result") == "applied":
                    records.append(value)
    except OSError:
        return None, None, "Log not found"
    return match, records, None


def _unique_restore_destination(path, reserved=None):
    reserved = reserved or set()
    path = os.path.realpath(path)
    stem, ext = os.path.splitext(path)
    candidate = path
    index = 1
    while os.path.lexists(candidate) or os.path.normcase(candidate) in reserved:
        candidate = f"{stem} (restored {index}){ext}"
        index += 1
    return candidate


def build_duplicate_restore_plan(log_id, lib_root=LIB_ROOT):
    entry, records, err = _cleanup_log_records(log_id)
    if err:
        return None, err
    if entry.get("action") != "move":
        return None, "Permanent deletions cannot be restored"
    if entry.get("restored_at"):
        return None, "This cleanup log has already been restored"
    root = os.path.realpath(lib_root)
    files = []
    unavailable = []
    reserved = set()
    vacated = set()
    for record in reversed(records):
        operation = str(record.get("operation") or "")
        if operation not in {"move", "rename"}:
            continue
        raw_source = str(record.get("new_path") or "").strip()
        raw_requested = str(record.get("old_path") or "").strip()
        source = os.path.realpath(raw_source) if raw_source else ""
        requested = os.path.realpath(raw_requested) if raw_requested else ""
        if (
            not source
            or not requested
            or not path_is_under(source, root)
            or not path_is_under(requested, root)
        ):
            unavailable.append({"source_path": source, "reason": "Restore path is outside the library"})
            continue
        if not os.path.isfile(source) or os.path.islink(source):
            unavailable.append({"source_path": source, "reason": "Restorable file is no longer present"})
            continue
        requested_key = os.path.normcase(requested)
        collision = os.path.lexists(requested) and requested_key not in vacated
        destination = (
            _unique_restore_destination(requested, reserved)
            if collision or requested_key in reserved
            else requested
        )
        files.append(
            {
                "file_id": record.get("file_id", ""),
                "source_path": source,
                "destination_path": destination,
                "requested_destination_path": requested,
                "source_name": os.path.basename(source),
                "destination_name": os.path.basename(destination),
                "original_operation": operation,
                "collision_adjusted": destination != requested,
                "size_bytes": int(record.get("size_bytes") or 0),
                "size_label": format_size(record.get("size_bytes") or 0),
                "identity": _stat_identity(source) or {},
            }
        )
        reserved.add(os.path.normcase(destination))
        vacated.add(os.path.normcase(source))
    if not files:
        return None, "No moved files from this cleanup are currently restorable"
    plan = {
        "id": _now_id(),
        "log_id": entry.get("id", ""),
        "status": "ready",
        "created_at": utc_iso(),
        "lib_root": root,
        "files": files,
        "file_count": len(files),
        "collision_adjusted_count": sum(bool(item["collision_adjusted"]) for item in files),
        "unavailable": unavailable,
        "unavailable_count": len(unavailable),
    }
    with maintenance_lock:
        duplicate_restore_plans[plan["id"]] = plan
    return copy.deepcopy(plan), None


def apply_duplicate_restore_plan(plan_id):
    with maintenance_lock:
        plan = duplicate_restore_plans.get(str(plan_id or ""))
    if not plan:
        return None, "Restore plan not found"
    if plan.get("status") != "ready":
        return None, "Restore plan has already been applied"
    root = os.path.realpath(plan.get("lib_root") or LIB_ROOT)
    applied = []
    refused = []
    reserved = set()
    for item in plan.get("files") or []:
        source = item.get("source_path", "")
        destination = item.get("destination_path", "")
        if not path_is_under(source, root) or not path_is_under(destination, root):
            refused.append({"source_path": source, "reason": "Restore path is outside the library"})
            continue
        if os.path.islink(source) or not os.path.isfile(source):
            refused.append({"source_path": source, "reason": "Restorable file is no longer present"})
            continue
        if not _identity_matches(source, item.get("identity")):
            refused.append({"source_path": source, "reason": "File changed after the restore preview"})
            continue
        if os.path.lexists(destination) or os.path.normcase(destination) in reserved:
            destination = _unique_restore_destination(
                item.get("requested_destination_path") or destination,
                reserved,
            )
        try:
            os.makedirs(os.path.dirname(destination), exist_ok=True)
            atomic_quarantine_file(
                source,
                destination,
                root=root,
                expected_source=item.get("identity"),
            )
        except Exception as exc:
            refused.append({"source_path": source, "reason": str(exc)})
            continue
        reserved.add(os.path.normcase(destination))
        applied.append(
            {
                **item,
                "destination_path": destination,
                "destination_name": os.path.basename(destination),
                "collision_adjusted": destination != item.get("requested_destination_path"),
            }
        )

    finished_at = utc_iso()
    index = _read_json(MAINTENANCE_LOG_INDEX, {"logs": []})
    for entry in index.get("logs") or []:
        if entry.get("id") == plan.get("log_id"):
            entry["last_restore_attempt_at"] = finished_at
            entry["restored_count"] = int(entry.get("restored_count") or 0) + len(applied)
            entry["restore_refused_count"] = len(refused)
            if not refused:
                entry["restored_at"] = finished_at
            break
    _write_json(MAINTENANCE_LOG_INDEX, index)
    with maintenance_lock:
        plan["status"] = "applied"
        plan["applied_at"] = finished_at
    sync_changes = [
        {"local_path": item.get("source_path"), "update_type": "Deleted", "refresh_scope": "metadata"}
        for item in applied
    ] + [
        {"local_path": item.get("destination_path"), "update_type": "Created", "refresh_scope": "metadata", "prefer_path": True}
        for item in applied
    ]
    sync_result = emby_sync.sync_changes(
        sync_changes,
        workflow="duplicates_restore",
        run_id=plan.get("id"),
    ) if sync_changes else None
    return {
        "plan_id": plan.get("id"),
        "log_id": plan.get("log_id"),
        "status": "success" if not refused else "complete_with_issues",
        "applied": applied,
        "applied_count": len(applied),
        "refused": refused,
        "refused_count": len(refused),
        "collision_adjusted_count": sum(bool(item.get("collision_adjusted")) for item in applied),
        "emby_sync": sync_result,
        "finished_at": finished_at,
    }, None


def _prune_duplicate_apply_runs_locked(now=None):
    now = now or time.time()
    terminal = [
        (apply_id, run)
        for apply_id, run in duplicate_apply_runs.items()
        if run.get("status") in {"success", "failed"}
    ]
    for apply_id, run in terminal:
        finished = run.get("_finished_ts") or run.get("_created_ts") or now
        if now - finished > DUPLICATE_APPLY_MAX_AGE_SECONDS:
            duplicate_apply_runs.pop(apply_id, None)

    terminal = sorted(
        (
            (apply_id, run)
            for apply_id, run in duplicate_apply_runs.items()
            if run.get("status") in {"success", "failed"}
        ),
        key=lambda item: item[1].get("_finished_ts") or item[1].get("_created_ts") or 0,
        reverse=True,
    )
    for apply_id, _run in terminal[DUPLICATE_APPLY_RETENTION_COUNT:]:
        duplicate_apply_runs.pop(apply_id, None)


def _set_apply_progress(run, **values):
    if not run:
        return
    with maintenance_lock:
        run.update(values)


def _public_apply_result(result):
    result = result or {}
    log = result.get("log") or {}
    return {
        "plan_id": result.get("plan_id", ""),
        "scan_id": result.get("scan_id", ""),
        "action": result.get("action", ""),
        "applied_count": result.get("applied_count", 0),
        "missing_count": result.get("missing_count", 0),
        "refused_count": result.get("refused_count", 0),
        "deferred_count": result.get("deferred_count", 0),
        "skipped_changed_groups": copy.deepcopy(result.get("skipped_changed_groups") or []),
        "skipped_changed_group_count": result.get("skipped_changed_group_count", 0),
        "refresh_run_id": result.get("refresh_run_id", ""),
        "deferred_bytes": result.get("deferred_bytes", 0),
        "total_applied_bytes": result.get("total_applied_bytes", 0),
        "total_applied_label": result.get("total_applied_label", "0 B"),
        "resolved_group_ids": list(result.get("resolved_group_ids") or []),
        "resolved_group_count": result.get("resolved_group_count", 0),
        "scan_reconciled": bool(result.get("scan_reconciled")),
        "scan": result.get("scan"),
        "emby_sync": result.get("emby_sync"),
        "emby_playback": emby_playback.public_result(result.get("emby_playback")),
        "emby_notification": emby_notifications.public_result(result.get("emby_notification")),
        "log": {key: value for key, value in log.items() if key != "path"},
    }


def public_apply_run(run):
    if not run:
        return None
    result = run.get("result") or {}
    return {
        "id": run.get("id", ""),
        "plan_id": run.get("plan_id", ""),
        "scan_id": run.get("scan_id", ""),
        "action": run.get("action", ""),
        "status": run.get("status", ""),
        "created_at": run.get("created_at"),
        "started_at": run.get("started_at"),
        "finished_at": run.get("finished_at"),
        "progress_percent": run.get("progress_percent", 0),
        "progress_label": run.get("progress_label", ""),
        "file_count": run.get("file_count", 0),
        "processed_count": run.get("processed_count", 0),
        "applied_count": run.get("applied_count", 0),
        "missing_count": run.get("missing_count", 0),
        "refused_count": run.get("refused_count", 0),
        "deferred_count": run.get("deferred_count", 0),
        "skipped_changed_group_count": run.get("skipped_changed_group_count", 0),
        "current_path": run.get("current_path", ""),
        "current_name": run.get("current_name", ""),
        "error": run.get("error", ""),
        "large_operation": bool(run.get("large_operation")),
        "emby_sync": result.get("emby_sync") if result else None,
        "emby_playback": emby_playback.public_result(result.get("emby_playback")) if result else None,
        "emby_notification": emby_notifications.public_result(run.get("emby_notification") or result.get("emby_notification")) if (run.get("emby_notification") or result) else None,
        "result": _public_apply_result(result) if result else None,
        "log": (result.get("log") or None) if result else None,
    }


def _active_apply_for_plan_locked(plan_id):
    for run in duplicate_apply_runs.values():
        if run.get("plan_id") == plan_id and run.get("status") in {"queued", "running"}:
            return run
    return None


def start_duplicate_apply(plan_id):
    plan_id = str(plan_id or "")
    with maintenance_lock:
        _prune_duplicate_apply_runs_locked()
        plan = cleanup_plans.get(plan_id)
        if not plan:
            return None, "Plan not found"
        active = _active_apply_for_plan_locked(plan_id)
        if active:
            return active, None
        if plan.get("status") == "applied":
            return None, "Plan already applied"
        if plan.get("status") == "applying":
            return None, "Plan is already applying"
        run_id = _now_id()
        created = time.time()
        run = {
            "id": run_id,
            "plan_id": plan_id,
            "scan_id": plan.get("scan_id", ""),
            "action": plan.get("action", ""),
            "status": "queued",
            "created_at": utc_iso(created),
            "started_at": None,
            "finished_at": None,
            "_created_ts": created,
            "_started_ts": None,
            "_finished_ts": None,
            "progress_percent": 0,
            "progress_label": "Queued",
            "file_count": len(plan.get("files") or []),
            "processed_count": 0,
            "applied_count": 0,
            "missing_count": 0,
            "refused_count": 0,
            "current_path": "",
            "current_name": "",
            "error": "",
            "result": None,
            "large_operation": len(plan.get("files") or []) >= DUPLICATE_APPLY_LARGE_FILE_COUNT,
        }
        duplicate_apply_runs[run_id] = run
        plan["status"] = "applying"

    thread = threading.Thread(
        target=_execute_duplicate_apply,
        args=(run_id,),
        daemon=True,
        name=f"vid2gif-maintenance-apply-{run_id}",
    )
    thread.start()
    return run, None


def _execute_duplicate_apply(apply_id):
    with maintenance_lock:
        run = duplicate_apply_runs.get(apply_id)
    if not run:
        return
    with library_operation(
        f"mutation:duplicates:{apply_id}",
        label="Apply duplicate cleanup",
        kind="mutation",
        state=run,
        href="/maintenance#duplicates",
    ) as activity:
        result, err = apply_duplicate_cleanup_plan(run.get("plan_id"), apply_run=run)
        activity.set_outcome(run.get("status"))
    if err:
        finished = time.time()
        notification = emby_notifications.notify_maintenance(
            "Duplicate cleanup",
            run["id"],
            status="failed",
            attempted_count=run.get("file_count", 0),
            succeeded_count=run.get("applied_count", 0),
            failed_count=1,
            refused_count=run.get("refused_count", 0),
            deferred_count=run.get("deferred_count", 0),
        )
        _set_apply_progress(
            run,
            status="failed",
            error=err,
            progress_label="Cleanup failed",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
            emby_notification=notification,
        )


def duplicate_apply_status(apply_id=None):
    with maintenance_lock:
        _prune_duplicate_apply_runs_locked()
        if apply_id:
            run = duplicate_apply_runs.get(str(apply_id or ""))
            if not run:
                return None, "Apply run not found"
        elif duplicate_apply_runs:
            run = max(
                duplicate_apply_runs.values(),
                key=lambda item: item.get("_created_ts") or 0,
            )
        else:
            run = None
    return {"apply": public_apply_run(run)}, None


def apply_duplicate_cleanup_plan(plan_id, apply_run=None):
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
    deferred = []
    log_records = []
    total = 0
    files = list(plan.get("files") or [])
    current_folder_snapshots = {}
    skipped_changed_groups = []
    skipped_group_ids = set()
    refresh_group_ids = set()
    for expected in plan.get("group_snapshots") or []:
        group_id = str(expected.get("group_id") or "")
        folder = os.path.realpath(expected.get("folder") or "")
        folder_key = os.path.normcase(folder)
        if folder_key not in current_folder_snapshots:
            current_folder_snapshots[folder_key] = _folder_snapshot(folder)
        current = current_folder_snapshots[folder_key]
        current_fingerprint = _folder_fingerprint(current)
        if current_fingerprint == str(expected.get("folder_fingerprint") or ""):
            continue
        detail = _folder_change_detail({}, current)
        with maintenance_lock:
            scan = duplicate_scans.get(str(plan.get("scan_id") or ""))
            group = next(
                (
                    item for item in (scan or {}).get("groups") or []
                    if item.get("id") == group_id
                ),
                {},
            )
        if group:
            detail = _folder_change_detail(group.get("folder_snapshot"), current)
        skipped_group_ids.add(group_id)
        refresh_group_ids.add(group_id)
        skipped_changed_groups.append(
            {
                "group_id": group_id,
                "folder": folder,
                "reason": "Files in this folder changed after the cleanup preview",
                "added": detail["added"],
                "removed": detail["removed"],
                "modified": detail["modified"],
            }
        )
    files_by_group = {}
    for item in files:
        files_by_group.setdefault(str(item.get("group_id") or ""), []).append(item)
    for group_id, group_files in files_by_group.items():
        if group_id in skipped_group_ids:
            continue
        cleanup_sources = {
            os.path.normcase(os.path.realpath(item.get("source_path") or ""))
            for item in group_files
            if item.get("operation") in {"move", "delete"} and item.get("source_path")
        }
        preflight_reason = ""
        for item in group_files:
            source = item.get("source_path") or ""
            if not source or not _identity_matches(source, item.get("identity")):
                preflight_reason = "A planned source file changed after the cleanup preview"
                break
            destination = item.get("destination_path") or ""
            if item.get("operation") == "move" and destination and os.path.lexists(destination):
                preflight_reason = "A quarantine destination is no longer available"
                break
            if (
                item.get("operation") == "rename"
                and destination
                and os.path.lexists(destination)
                and os.path.normcase(os.path.realpath(destination)) not in cleanup_sources
            ):
                preflight_reason = "A rename destination is no longer available"
                break
        if preflight_reason:
            skipped_group_ids.add(group_id)
            if "source file changed" in preflight_reason:
                refresh_group_ids.add(group_id)
            skipped_changed_groups.append(
                {
                    "group_id": group_id,
                    "folder": os.path.dirname(group_files[0].get("source_path") or ""),
                    "reason": preflight_reason,
                    "added": [],
                    "removed": [],
                    "modified": [],
                }
            )
    if skipped_group_ids:
        files = [item for item in files if item.get("group_id") not in skipped_group_ids]
    log_records.extend(
        {"type": "group", "result": "skipped_changed", **item}
        for item in skipped_changed_groups
    )
    file_count = len(files)
    if apply_run:
        started = time.time()
        _set_apply_progress(
            apply_run,
            status="running",
            started_at=utc_iso(started),
            _started_ts=started,
            progress_percent=0,
            progress_label=f"Processing 0 of {file_count} files",
            file_count=file_count,
            deferred_count=0,
            skipped_changed_group_count=len(skipped_changed_groups),
        )

    playback_targets = list(plan.get("playback_targets") or [])
    playback = emby_playback.check_targets(playback_targets, force=True)
    playback_checked = time.monotonic()
    group_decisions = {}

    def _finish_item(index, source):
        if not apply_run:
            return
        pct = int(100 * index / max(file_count, 1))
        _set_apply_progress(
            apply_run,
            processed_count=index,
            applied_count=len(applied),
            missing_count=len(missing),
            refused_count=len(refused),
            deferred_count=len(deferred),
            progress_percent=pct,
            progress_label=f"Processed {index} of {file_count} files",
            current_path=source if index < file_count else "",
            current_name=os.path.basename(source) if source and index < file_count else "",
        )

    for index, item in enumerate(files, start=1):
        source = item.get("source_path", "")
        file_id = item.get("file_id", "")
        operation = item.get("operation") or action
        group_id = item.get("group_id", "")
        if group_id not in group_decisions:
            if time.monotonic() - playback_checked >= emby_playback.RUN_REFRESH_SECONDS:
                playback = emby_playback.check_targets(playback_targets, force=True)
                playback_checked = time.monotonic()
            group_decisions[group_id] = emby_playback.group_status(
                playback, playback_targets, group_id
            )
        playback_status = group_decisions[group_id]
        if apply_run:
            _set_apply_progress(
                apply_run,
                current_path=source,
                current_name=os.path.basename(source),
                progress_label=f"Processing {index} of {file_count} files",
            )
        if playback_status in {"active", "unverified"}:
            reason = (
                "Duplicate group is actively playing in Emby"
                if playback_status == "active"
                else "Duplicate group playback could not be verified"
            )
            value = {
                "file_id": file_id,
                "group_id": group_id,
                "path": source,
                "reason": reason,
                "playback_status": playback_status,
                "size_bytes": item.get("size_bytes", 0),
            }
            deferred.append(value)
            log_records.append({"type": "file", "result": "deferred", **value})
            _finish_item(index, source)
            continue
        if not source or not path_is_under(source, lib_root):
            refusal = _refusal(file_id, source, "Source is outside the library")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue
        if os.path.islink(source):
            refusal = _refusal(file_id, source, "Symlinks are not cleaned")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
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
            _finish_item(index, source)
            continue
        if not _identity_matches(source, item.get("identity")):
            refusal = _refusal(file_id, source, "File changed after scan")
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
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
                    _finish_item(index, source)
                    continue
                if os.path.exists(dest):
                    refusal = _refusal(file_id, source, "Destination already exists")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    _finish_item(index, source)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                atomic_quarantine_file(
                    source,
                    dest,
                    root=lib_root,
                    expected_source=item.get("identity"),
                )
            elif operation == "rename":
                if not dest or not path_is_under(dest, lib_root):
                    refusal = _refusal(file_id, source, "Rename target is outside the library")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    _finish_item(index, source)
                    continue
                if os.path.exists(dest):
                    refusal = _refusal(file_id, source, "Rename target already exists")
                    refused.append(refusal)
                    log_records.append({"type": "file", "result": "refused", **refusal})
                    _finish_item(index, source)
                    continue
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                atomic_quarantine_file(
                    source,
                    dest,
                    root=lib_root,
                    expected_source=item.get("identity"),
                )
            else:
                refusal = _refusal(file_id, source, "Unsupported operation")
                refused.append(refusal)
                log_records.append({"type": "file", "result": "refused", **refusal})
                _finish_item(index, source)
                continue
        except Exception as exc:
            refusal = _refusal(file_id, source, str(exc))
            refused.append(refusal)
            log_records.append({"type": "file", "result": "refused", **refusal})
            _finish_item(index, source)
            continue

        applied_item = {
            "file_id": file_id,
            "group_id": item.get("group_id", ""),
            "kind": item.get("kind", ""),
            "operation": operation,
            "source_path": source,
            "destination_path": item.get("destination_path", ""),
            "source_name": item.get("source_name") or os.path.basename(source),
            "destination_name": item.get("destination_name", ""),
            "size_bytes": item.get("size_bytes", 0),
            "size_label": item.get("size_label", ""),
            "emby_item_id": item.get("emby_item_id", ""),
            "emby_item_type": item.get("emby_item_type", ""),
        }
        applied.append(applied_item)
        log_records.append(
            {
                "type": "file",
                "timestamp": utc_iso(),
                "result": "applied",
                "file_id": file_id,
                "group_id": group_id,
                "kind": item.get("kind", ""),
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
        _finish_item(index, source)

    sync_changes = []
    for item in applied:
        is_video = item.get("kind") == "video"
        sync_changes.append(
            {
                "local_path": item.get("source_path"),
                "update_type": "Deleted",
                "emby_item_id": item.get("emby_item_id", ""),
                "refresh_scope": "media" if is_video else "metadata",
            }
        )
        if item.get("operation") == "rename" and item.get("destination_path"):
            sync_changes.append(
                {
                    "local_path": item.get("destination_path"),
                    "update_type": "Created",
                    "refresh_scope": "metadata",
                    "prefer_path": True,
                }
            )
    if apply_run and sync_changes:
        _set_apply_progress(apply_run, progress_label="Synchronizing cleanup changes with Emby")
    emby_sync_result = emby_sync.sync_changes(
        sync_changes,
        workflow="duplicates",
        run_id=(apply_run or {}).get("id") or plan.get("id"),
    ) if sync_changes else None
    reconciled_scan, resolved_group_ids, scan_reconciled = _reconcile_duplicate_scan_after_apply(
        plan, applied
    )
    refresh_run = None
    if refresh_group_ids:
        refresh_run, _refresh_error = start_duplicate_refresh(
            plan.get("scan_id"), list(refresh_group_ids)
        )
    result = {
        "plan_id": plan.get("id", ""),
        "scan_id": plan.get("scan_id", ""),
        "action": action,
        "applied": applied,
        "missing": missing,
        "refused": refused,
        "deferred": deferred,
        "applied_count": len(applied),
        "missing_count": len(missing),
        "refused_count": len(refused),
        "deferred_count": len(deferred),
        "skipped_changed_groups": skipped_changed_groups,
        "skipped_changed_group_count": len(skipped_changed_groups),
        "refresh_run_id": (refresh_run or {}).get("id", ""),
        "deferred_bytes": sum(int(item.get("size_bytes") or 0) for item in deferred),
        "total_applied_bytes": total,
        "total_applied_label": format_size(total),
        "emby_sync": emby_sync_result,
        "emby_playback": playback,
        "resolved_group_ids": resolved_group_ids,
        "resolved_group_count": len(resolved_group_ids),
        "scan_reconciled": scan_reconciled,
        "scan": reconciled_scan,
    }
    log_entry = _write_cleanup_log(plan, result, log_records)
    result["log"] = {key: value for key, value in log_entry.items() if key != "path"}
    applied_video_ids = {
        item.get("file_id")
        for item in applied
        if item.get("kind") == "video" and item.get("operation") in {"move", "delete"}
    }
    resolutions = []
    for group in plan.get("impact_groups") or []:
        required = set(group.get("required_video_ids") or [])
        if required and required.issubset(applied_video_ids):
            resolutions.append(
                {
                    "issue_id": group.get("issue_id"),
                    "stream": "duplicates",
                    "resolve_all": True,
                    "ensure_issue": True,
                    "label": group.get("label"),
                    "path": group.get("path"),
                }
            )
    operation_counts = {
        "quarantined_files": sum(1 for item in applied if item.get("operation") == "move"),
        "quarantined_bytes": sum(
            int(item.get("size_bytes") or 0) for item in applied if item.get("operation") == "move"
        ),
        "deleted_files": sum(1 for item in applied if item.get("operation") == "delete"),
        "deleted_bytes": sum(
            int(item.get("size_bytes") or 0) for item in applied if item.get("operation") == "delete"
        ),
        "other_files": sum(1 for item in applied if item.get("operation") == "rename"),
        "other_bytes": 0,
    }
    impact_metrics.record_maintenance_action(
        plan.get("id"),
        "duplicates",
        resolutions=resolutions,
        operations=operation_counts,
        timestamp=utc_iso(),
        label="Duplicate cleanup",
    )
    result["emby_notification"] = emby_notifications.notify_maintenance(
        "Duplicate cleanup",
        (apply_run or {}).get("id") or plan.get("id"),
        status="success",
        attempted_count=file_count,
        succeeded_count=len(applied),
        refused_count=len(refused),
        deferred_count=len(deferred),
        unresolved_count=len(missing),
        reclaimed_bytes=total,
        emby_sync=emby_sync_result,
    )
    with maintenance_lock:
        plan["status"] = "applied"
        plan["applied_at"] = utc_iso()
        plan["last_result"] = result
    if apply_run:
        finished = time.time()
        _set_apply_progress(
            apply_run,
            status="success",
            result=result,
            progress_percent=100,
            progress_label="Cleanup complete",
            processed_count=file_count,
            applied_count=len(applied),
            missing_count=len(missing),
            refused_count=len(refused),
            deferred_count=len(deferred),
            skipped_changed_group_count=len(skipped_changed_groups),
            current_path="",
            current_name="",
            _finished_ts=finished,
            finished_at=utc_iso(finished),
        )
    return result, None


def _reconcile_duplicate_scan_after_apply(plan, applied):
    affected_group_ids = {
        item.get("group_id")
        for item in applied or []
        if item.get("group_id")
    }
    scan_id = str((plan or {}).get("scan_id") or "")
    if not affected_group_ids or not scan_id:
        return None, [], False
    with maintenance_lock:
        scan = duplicate_scans.get(scan_id)
        if not scan or scan.get("status") != "success":
            return None, [], False
        original_groups = copy.deepcopy(scan.get("groups") or [])
        settings = copy.deepcopy(scan.get("settings") or duplicate_settings())
        lib_root = os.path.realpath(scan.get("lib_root") or plan.get("lib_root") or LIB_ROOT)

    updated_groups = []
    resolved_group_ids = []
    resolved_review_keys = []
    for group in original_groups:
        group_id = group.get("id")
        if group_id not in affected_group_ids:
            updated_groups.append(group)
            continue
        existing_video_paths = [
            video.get("path")
            for video in group.get("videos") or []
            if video.get("path")
            and path_is_under(video.get("path"), lib_root)
            and os.path.isfile(video.get("path"))
            and not os.path.islink(video.get("path"))
        ]
        if len(existing_video_paths) < 2:
            resolved_group_ids.append(group_id)
            if group.get("review_key"):
                resolved_review_keys.append(group.get("review_key"))
            continue
        rebuilt, _protected = _build_groups(existing_video_paths, 0, lib_root, settings)
        if not rebuilt:
            resolved_group_ids.append(group_id)
            if group.get("review_key"):
                resolved_review_keys.append(group.get("review_key"))
            continue
        replacement = rebuilt[0]
        replacement["id"] = group_id
        replacement["impact_issue_id"] = group.get("impact_issue_id") or replacement.get(
            "impact_issue_id", ""
        )
        updated_groups.append(replacement)

    with maintenance_lock:
        live_scan = duplicate_scans.get(scan_id)
        if not live_scan:
            return None, resolved_group_ids, False
        live_scan["groups"] = updated_groups
        live_scan["reclaimable_bytes"] = sum(
            int(group.get("reclaimable_bytes") or 0) for group in updated_groups
        )
        scan_for_persistence = live_scan
    maintenance_scan_store.update_persisted_scan(
        "duplicates",
        scan_for_persistence,
        lib_root,
        removed_paths=[item.get("source_path") for item in applied if item.get("source_path")],
        accepted_paths=[
            item.get("destination_path") for item in applied
            if item.get("operation") == "rename" and item.get("destination_path")
        ],
    )
    duplicate_review_store.remove_review_keys(scan_for_persistence, resolved_review_keys)
    return public_scan(scan_for_persistence), resolved_group_ids, True
