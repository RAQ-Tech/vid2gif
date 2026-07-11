import json
import os
import re
import threading

from .config import LIB_ROOT, STATE_ROOT


SCHEMA_VERSION = 5
DEFAULT_TEST_LAB_PREVIEW_HEIGHT = 720
PREVIEW_HEIGHT_PRESETS = (540, 720, 1080, 1440, 2160)
SETTINGS_PATH = os.path.join(STATE_ROOT, "app_settings.json")
DEFAULT_DUPLICATE_MOVE_ROOT = os.path.join(LIB_ROOT, ".vid2gif-duplicates")
DUPLICATE_GROUPING_MODES = {
    "balanced": "Balanced",
    "strict": "Strict stem",
    "folder": "Folder-wide review",
}
DUPLICATE_KEEPER_RULES = {
    "quality": "Quality first",
    "largest": "Largest file",
    "newest": "Newest modified",
}
DUPLICATE_ACCESSORY_POLICIES = {
    "rename_unmatched": "Rename unmatched to keeper stem",
    "keep_unmatched": "Keep unmatched sidecars",
    "remove_all": "Remove all matched-stem sidecars",
}
DEFAULT_DUPLICATE_EXCLUDED_FOLDERS = ("trailer", "trailers")
DEFAULT_SUBTITLE_EXPECTED_LANGUAGES = ("eng", "en", "en-us", "en-gb")
DEFAULT_VIDEO_PREVIEW_BIF_WIDTH = 320
DEFAULT_VIDEO_PREVIEW_BIF_INTERVAL_SECONDS = 10

_settings_lock = threading.RLock()
_TABLE_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_COLUMN_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
_SETTING_KEYS = {
    "test_lab_preview_height",
    "duplicate_grouping_mode",
    "duplicate_keeper_rule",
    "duplicate_accessory_policy",
    "duplicate_move_root",
    "duplicate_excluded_folders",
    "subtitle_expected_languages",
    "subtitle_flag_missing",
    "subtitle_flag_unknown_language",
    "subtitle_subgen_detection",
    "video_preview_bif_width",
    "video_preview_bif_interval_seconds",
    "table_preferences",
}


def parse_preview_height(value):
    if value is None:
        return None, None
    text = str(value or "").strip().lower()
    if text in {"original", "none", "off", "0"}:
        return None, None
    try:
        height = int(text)
    except (TypeError, ValueError):
        return None, "Choose a positive preview height."
    if height <= 0:
        return None, "Choose a positive preview height."
    return height, None


def preview_height_label(height):
    if height is None:
        return "Original"
    return f"{height}px"


def default_settings():
    return {
        "schema_version": SCHEMA_VERSION,
        "test_lab_preview_height": DEFAULT_TEST_LAB_PREVIEW_HEIGHT,
        "duplicate_grouping_mode": "balanced",
        "duplicate_keeper_rule": "quality",
        "duplicate_accessory_policy": "rename_unmatched",
        "duplicate_move_root": DEFAULT_DUPLICATE_MOVE_ROOT,
        "duplicate_excluded_folders": list(DEFAULT_DUPLICATE_EXCLUDED_FOLDERS),
        "subtitle_expected_languages": list(DEFAULT_SUBTITLE_EXPECTED_LANGUAGES),
        "subtitle_flag_missing": True,
        "subtitle_flag_unknown_language": True,
        "subtitle_subgen_detection": True,
        "video_preview_bif_width": DEFAULT_VIDEO_PREVIEW_BIF_WIDTH,
        "video_preview_bif_interval_seconds": DEFAULT_VIDEO_PREVIEW_BIF_INTERVAL_SECONDS,
        "table_preferences": {},
    }


def _choice(value, choices, default):
    value = str(value or "").strip().lower()
    return value if value in choices else default


def parse_excluded_folders(value):
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re_split_commas(value)
    items = []
    seen = set()
    for item in raw_items:
        cleaned = str(item or "").strip().lower()
        if not cleaned or cleaned in seen or any(sep in cleaned for sep in ("/", "\\")):
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return items or list(DEFAULT_DUPLICATE_EXCLUDED_FOLDERS)


def normalize_language_code(value):
    return str(value or "").strip().lower().replace("_", "-")


def parse_subtitle_languages(value):
    if isinstance(value, (list, tuple)):
        raw_items = value
    else:
        raw_items = re_split_commas(value)
    items = []
    seen = set()
    for item in raw_items:
        cleaned = normalize_language_code(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        items.append(cleaned)
    return items or list(DEFAULT_SUBTITLE_EXPECTED_LANGUAGES)


def re_split_commas(value):
    return str(value or "").replace("\n", ",").split(",")


def _bool(value, default=False):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _bounded_int(value, default, minimum, maximum):
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if minimum <= parsed <= maximum else default


def _coerce_table_preferences(value):
    if not isinstance(value, dict):
        return {}
    tables = {}
    for table_id, raw in value.items():
        table_id = str(table_id or "").strip().lower()
        if not _TABLE_KEY_RE.fullmatch(table_id) or not isinstance(raw, dict):
            continue
        widths = {}
        for column_id, width in (raw.get("widths") or {}).items():
            column_id = str(column_id or "").strip().lower()
            try:
                width = int(width)
            except (TypeError, ValueError):
                continue
            if _COLUMN_KEY_RE.fullmatch(column_id) and 48 <= width <= 4096:
                widths[column_id] = width
        preference = {"widths": widths}
        sort = raw.get("sort")
        if isinstance(sort, dict):
            column = str(sort.get("column") or "").strip().lower()
            direction = str(sort.get("direction") or "asc").strip().lower()
            if _COLUMN_KEY_RE.fullmatch(column) and direction in {"asc", "desc"}:
                preference["sort"] = {"column": column, "direction": direction}
        tables[table_id] = preference
    return tables


def _coerce_settings(data):
    if not isinstance(data, dict):
        return default_settings()
    defaults = default_settings()
    height, err = parse_preview_height(
        data.get("test_lab_preview_height", DEFAULT_TEST_LAB_PREVIEW_HEIGHT)
    )
    if err:
        height = DEFAULT_TEST_LAB_PREVIEW_HEIGHT
    move_root = str(data.get("duplicate_move_root", defaults["duplicate_move_root"]) or "").strip()
    return {
        "schema_version": SCHEMA_VERSION,
        "test_lab_preview_height": height,
        "duplicate_grouping_mode": _choice(
            data.get("duplicate_grouping_mode", defaults["duplicate_grouping_mode"]),
            DUPLICATE_GROUPING_MODES,
            defaults["duplicate_grouping_mode"],
        ),
        "duplicate_keeper_rule": _choice(
            data.get("duplicate_keeper_rule", defaults["duplicate_keeper_rule"]),
            DUPLICATE_KEEPER_RULES,
            defaults["duplicate_keeper_rule"],
        ),
        "duplicate_accessory_policy": _choice(
            data.get("duplicate_accessory_policy", defaults["duplicate_accessory_policy"]),
            DUPLICATE_ACCESSORY_POLICIES,
            defaults["duplicate_accessory_policy"],
        ),
        "duplicate_move_root": move_root or defaults["duplicate_move_root"],
        "duplicate_excluded_folders": parse_excluded_folders(
            data.get("duplicate_excluded_folders", defaults["duplicate_excluded_folders"])
        ),
        "subtitle_expected_languages": parse_subtitle_languages(
            data.get("subtitle_expected_languages", defaults["subtitle_expected_languages"])
        ),
        "subtitle_flag_missing": _bool(
            data.get("subtitle_flag_missing", defaults["subtitle_flag_missing"]),
            defaults["subtitle_flag_missing"],
        ),
        "subtitle_flag_unknown_language": _bool(
            data.get("subtitle_flag_unknown_language", defaults["subtitle_flag_unknown_language"]),
            defaults["subtitle_flag_unknown_language"],
        ),
        "subtitle_subgen_detection": _bool(
            data.get("subtitle_subgen_detection", defaults["subtitle_subgen_detection"]),
            defaults["subtitle_subgen_detection"],
        ),
        "video_preview_bif_width": _bounded_int(
            data.get("video_preview_bif_width", defaults["video_preview_bif_width"]),
            defaults["video_preview_bif_width"],
            64,
            1920,
        ),
        "video_preview_bif_interval_seconds": _bounded_int(
            data.get("video_preview_bif_interval_seconds", defaults["video_preview_bif_interval_seconds"]),
            defaults["video_preview_bif_interval_seconds"],
            1,
            3600,
        ),
        "table_preferences": _coerce_table_preferences(
            data.get("table_preferences", defaults["table_preferences"])
        ),
    }


def _read_settings_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _load_settings_unlocked(path):
    data = _read_settings_file(path)
    if data is None:
        data = _read_settings_file(f"{path}.bak")
    return _coerce_settings(data) if data is not None else default_settings()


def load_settings(path=None):
    path = path or SETTINGS_PATH
    with _settings_lock:
        return _load_settings_unlocked(path)


def _write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


def _write_settings_unlocked(path, settings):
    try:
        current = _read_settings_file(path)
        if current is not None:
            _write_json_atomic(f"{path}.bak", current)
        _write_json_atomic(path, settings)
    except Exception:
        return False
    return True


def save_settings(settings, path=None):
    settings = _coerce_settings(settings)
    path = path or SETTINGS_PATH
    with _settings_lock:
        return _write_settings_unlocked(path, settings)


def _validation_error(updates):
    unknown = set(updates) - _SETTING_KEYS
    if unknown:
        return f"Unknown setting: {sorted(unknown)[0]}"
    if "test_lab_preview_height" in updates:
        _height, err = parse_preview_height(updates.get("test_lab_preview_height"))
        if err:
            return err
    for key, choices in (
        ("duplicate_grouping_mode", DUPLICATE_GROUPING_MODES),
        ("duplicate_keeper_rule", DUPLICATE_KEEPER_RULES),
        ("duplicate_accessory_policy", DUPLICATE_ACCESSORY_POLICIES),
    ):
        if key in updates and str(updates.get(key) or "").strip().lower() not in choices:
            return f"Invalid {key.replace('_', ' ')}"
    for key, minimum, maximum, label in (
        ("video_preview_bif_width", 64, 1920, "BIF width"),
        ("video_preview_bif_interval_seconds", 1, 3600, "BIF interval"),
    ):
        if key not in updates:
            continue
        try:
            value = int(updates.get(key))
        except (TypeError, ValueError):
            return f"{label} must be a whole number"
        if not minimum <= value <= maximum:
            return f"{label} must be between {minimum} and {maximum}"
    if "table_preferences" in updates and not isinstance(updates.get("table_preferences"), dict):
        return "Table preferences are invalid"
    return ""


def update_settings(updates, path=None):
    if not isinstance(updates, dict):
        return None, "Settings are invalid"
    error = _validation_error(updates)
    if error:
        return None, error
    path = path or SETTINGS_PATH
    with _settings_lock:
        current = _load_settings_unlocked(path)
        merged = dict(current)
        for key, value in updates.items():
            if key != "table_preferences":
                merged[key] = value
                continue
            tables = dict(current.get("table_preferences") or {})
            for table_id, preference in value.items():
                table_id = str(table_id or "").strip().lower()
                if preference is None:
                    tables.pop(table_id, None)
                else:
                    tables[table_id] = preference
            merged["table_preferences"] = tables
        settings = _coerce_settings(merged)
        if not _write_settings_unlocked(path, settings):
            return None, "Settings could not be saved"
    return settings, None


def warning_for_preview_height(height):
    if height is None:
        return "Original GIFs can be very heavy in the browser."
    if height >= 1440:
        return "Large previews may still be slow on some clients."
    return ""
