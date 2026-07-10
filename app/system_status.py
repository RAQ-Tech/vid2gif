import datetime
import json
import os
import platform
import shutil
import sys
import tempfile
import threading
import time
import zipfile

from . import dashboard
from .config import GIFSICLE_BIN, LIB_ROOT, STATE_ROOT
from .progress import format_size, utc_iso


STARTED_AT = time.time()
WORKER_THREADS = {
    "gif_worker": ("GIF worker", "vid2gif-worker"),
    "test_lab_worker": ("Test Lab worker", "vid2gif-test-lab"),
    "poster_worker": ("Poster scheduler", "vid2gif-landscape-poster-worker"),
}


def _uptime_label(seconds):
    seconds = max(0, int(seconds or 0))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    if days:
        return f"{days}d {hours}h {minutes}m"
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m {seconds}s"
    return f"{seconds}s"


def _root_status(key, label, path, require_write=True):
    real = os.path.realpath(path)
    exists = os.path.isdir(real)
    readable = exists and os.access(real, os.R_OK)
    writable = exists and os.access(real, os.W_OK)
    status = "pass" if readable and (writable or not require_write) else "fail"
    detail = "Available"
    if not exists:
        detail = "Directory not found"
    elif not readable:
        detail = "Directory is not readable"
    elif require_write and not writable:
        detail = "Directory is not writable"
    return {
        "id": key,
        "label": label,
        "status": status,
        "detail": detail,
        "path": real,
    }


def _tool_status(key, label, command, required=True):
    path = shutil.which(command)
    return {
        "id": key,
        "label": label,
        "status": "pass" if path else ("fail" if required else "warn"),
        "detail": "Available" if path else f"{command} was not found on PATH",
        "path": path or "",
    }


def _worker_status(key, label, thread_name, active_names):
    active = thread_name in active_names
    return {
        "id": key,
        "label": label,
        "status": "pass" if active else "fail",
        "detail": "Running" if active else "Worker thread is not running",
        "path": "",
    }


def _storage_status(label, path):
    real = os.path.realpath(path)
    try:
        usage = shutil.disk_usage(real)
    except OSError:
        return {
            "label": label,
            "path": real,
            "available": False,
            "total_bytes": 0,
            "used_bytes": 0,
            "free_bytes": 0,
            "total_label": "Unavailable",
            "used_label": "Unavailable",
            "free_label": "Unavailable",
            "used_percent": 0,
        }
    used = usage.total - usage.free
    return {
        "label": label,
        "path": real,
        "available": True,
        "total_bytes": usage.total,
        "used_bytes": used,
        "free_bytes": usage.free,
        "total_label": format_size(usage.total),
        "used_label": format_size(used),
        "free_label": format_size(usage.free),
        "used_percent": int(round(100 * used / usage.total)) if usage.total else 0,
    }


def _active_work_count():
    try:
        return int((dashboard.status_payload().get("health") or {}).get("active_count") or 0)
    except Exception:
        return 0


def status_payload(now=None):
    now = time.time() if now is None else float(now)
    active_names = {thread.name for thread in threading.enumerate() if thread.is_alive()}
    checks = [
        _root_status("library_root", "Library storage", LIB_ROOT),
        _root_status("state_root", "Application state", STATE_ROOT),
        _tool_status("ffmpeg", "FFmpeg", "ffmpeg"),
        _tool_status("ffprobe", "FFprobe", "ffprobe"),
        _tool_status("gifsicle", "Gifsicle", GIFSICLE_BIN, required=False),
    ]
    for key, (label, thread_name) in WORKER_THREADS.items():
        checks.append(_worker_status(key, label, thread_name, active_names))

    failed = sum(1 for check in checks if check["status"] == "fail")
    warnings = sum(1 for check in checks if check["status"] == "warn")
    overall = "unhealthy" if failed else ("attention" if warnings else "healthy")
    uptime_seconds = max(0, int(now - STARTED_AT))
    return {
        "generated_at": utc_iso(now),
        "overall": overall,
        "healthy": failed == 0,
        "failed_count": failed,
        "warning_count": warnings,
        "uptime_seconds": uptime_seconds,
        "uptime_label": _uptime_label(uptime_seconds),
        "active_work_count": _active_work_count(),
        "runtime": {
            "python": platform.python_version(),
            "platform": platform.system(),
            "process_id": os.getpid(),
            "started_at": utc_iso(STARTED_AT),
        },
        "checks": checks,
        "storage": [
            _storage_status("Library", LIB_ROOT),
            _storage_status("State", STATE_ROOT),
        ],
    }


def create_state_backup(state_root=None):
    state_root = os.path.realpath(state_root or STATE_ROOT)
    if not os.path.isdir(state_root):
        raise FileNotFoundError("State directory not found")

    fd, archive_path = tempfile.mkstemp(prefix="vid2gif-state-", suffix=".zip")
    os.close(fd)
    file_count = 0
    total_bytes = 0
    skipped = []
    try:
        with zipfile.ZipFile(
            archive_path,
            mode="w",
            compression=zipfile.ZIP_STORED,
            allowZip64=True,
        ) as archive:
            for base, dirs, files in os.walk(state_root, followlinks=False):
                dirs[:] = [
                    name for name in dirs if not os.path.islink(os.path.join(base, name))
                ]
                for name in sorted(files):
                    source = os.path.join(base, name)
                    relative = os.path.relpath(source, state_root)
                    if os.path.islink(source) or not os.path.isfile(source):
                        skipped.append(relative)
                        continue
                    try:
                        size = os.path.getsize(source)
                        archive.write(source, os.path.join("state", relative))
                    except OSError:
                        skipped.append(relative)
                        continue
                    file_count += 1
                    total_bytes += size

            manifest = {
                "schema_version": 1,
                "created_at": utc_iso(),
                "source": "/state",
                "file_count": file_count,
                "total_bytes": total_bytes,
                "skipped": skipped,
                "python": sys.version.split()[0],
            }
            archive.writestr(
                "vid2gif-backup.json",
                json.dumps(manifest, indent=2, sort_keys=True),
            )
    except Exception:
        try:
            os.remove(archive_path)
        except OSError:
            pass
        raise

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    return archive_path, {
        "download_name": f"vid2gif-state-{stamp}.zip",
        "file_count": file_count,
        "total_bytes": total_bytes,
        "total_size_label": format_size(total_bytes),
        "skipped_count": len(skipped),
    }
