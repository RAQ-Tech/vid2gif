import os
import re


# Media stored in these conventional folders belongs to a movie or episode, but
# is not the main playable item that maintenance scans are intended to assess.
NON_MAIN_VIDEO_DIR_NAMES = frozenset(
    {
        "trailer",
        "trailers",
        "extra",
        "extras",
        "featurette",
        "featurettes",
        "behind the scenes",
        "behind-the-scenes",
        "behind_the_scenes",
        "behindthescenes",
        "deleted scene",
        "deleted scenes",
        "deleted-scene",
        "deleted-scenes",
        "deleted_scene",
        "deleted_scenes",
        "interview",
        "interviews",
        "scene",
        "scenes",
        "short",
        "shorts",
        "sample",
        "samples",
    }
)

_NON_MAIN_FILENAME_RE = re.compile(
    r"(?:^|[\s._-])(?:trailer|sample|featurette|interview|extras?|"
    r"behind[\s._-]+the[\s._-]+scenes?|deleted[\s._-]+scenes?)(?:[\s._-]*\d+)?"
    r"(?:[\s._-]+(?:4320p|2160p|1440p|1080p|720p|576p|540p|480p|360p|4k|8k|uhd|fhd|hd|webdl|webrip|bluray|x264|x265|h264|h265|hevc))*$",
    re.IGNORECASE,
)


def normalized_dir_name(value):
    return " ".join(str(value or "").strip().lower().split())


def is_non_main_video_dir(dirname):
    return normalized_dir_name(dirname) in NON_MAIN_VIDEO_DIR_NAMES


def is_non_main_video_filename(filename):
    stem = os.path.splitext(os.path.basename(str(filename or "")))[0].strip()
    stem = re.sub(r"[\[\](){}]", " ", stem)
    return bool(_NON_MAIN_FILENAME_RE.search(stem))


def is_main_video_filename(filename):
    return not is_non_main_video_filename(filename)


# Quarantine destinations are configurable, so scans cannot recognise them by
# name alone. Matching on the configured paths keeps a renamed or relocated
# folder from being walked back into the very scan that emptied it.
QUARANTINE_ROOT_SETTINGS = (
    "duplicate_move_root",
    "damaged_move_root",
    "video_preview_repair_root",
    "subtitle_quarantine_root",
)


def configured_quarantine_roots(settings=None):
    from . import app_settings

    settings = settings if settings is not None else app_settings.load_settings()
    roots = []
    for key in QUARANTINE_ROOT_SETTINGS:
        value = str((settings or {}).get(key) or "").strip()
        if value:
            roots.append(os.path.realpath(value))
    return roots


def is_quarantine_path(path, settings=None):
    from .utils import path_is_under

    real = os.path.realpath(str(path or ""))
    if not real:
        return False
    return any(path_is_under(real, root) for root in configured_quarantine_roots(settings))
