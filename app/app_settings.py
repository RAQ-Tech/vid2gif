import json
import os
import re
import threading

from .config import LANDSCAPE_POSTER_ROOT, LIB_ROOT, STATE_ROOT
from .utils import path_is_under


SCHEMA_VERSION = 10
DEFAULT_TEST_LAB_PREVIEW_HEIGHT = 720
PREVIEW_HEIGHT_PRESETS = (540, 720, 1080, 1440, 2160)
SETTINGS_PATH = os.path.join(STATE_ROOT, "app_settings.json")
LEGACY_EMBY_SETTINGS_PATH = os.path.join(LANDSCAPE_POSTER_ROOT, "settings.json")
MAX_EMBY_PATH_MAPPINGS = 20
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
    "video_preview_scan_path",
    "emby_url",
    "emby_api_key",
    "emby_api_key_clear",
    "emby_path_mappings",
    "emby_sync_after_maintenance",
    "emby_playback_protection",
    "emby_admin_notifications",
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
        "video_preview_scan_path": LIB_ROOT,
        "emby_url": str(os.getenv("EMBY_URL", "") or "").strip(),
        "emby_api_key": str(os.getenv("EMBY_API_KEY", "") or "").strip(),
        "emby_path_mappings": [],
        "emby_sync_after_maintenance": _bool(os.getenv("EMBY_SYNC_AFTER_MAINTENANCE"), True),
        "emby_playback_protection": _bool(os.getenv("EMBY_PLAYBACK_PROTECTION"), True),
        "emby_admin_notifications": _choice(
            os.getenv("EMBY_ADMIN_NOTIFICATIONS", "warnings"),
            {"off": "Off", "warnings": "Warnings", "all": "All"},
            "warnings",
        ),
        "table_preferences": {},
    }


def parse_emby_path_mappings(value):
    if value is None:
        return []
    if isinstance(value, str):
        mappings = []
        for line in value.splitlines():
            line = line.strip()
            if not line:
                continue
            if "=>" not in line:
                raise ValueError("Each Emby path mapping must use 'Emby prefix => vid2gif prefix'.")
            emby_prefix, local_prefix = line.split("=>", 1)
            mappings.append({"emby_prefix": emby_prefix.strip(), "local_prefix": local_prefix.strip()})
        return mappings
    if not isinstance(value, (list, tuple)):
        raise ValueError("Emby path mappings are invalid.")
    mappings = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("Emby path mappings are invalid.")
        mappings.append(
            {
                "emby_prefix": str(item.get("emby_prefix") or "").strip(),
                "local_prefix": str(item.get("local_prefix") or "").strip(),
            }
        )
    return mappings


def emby_path_mappings_text(value):
    return "\n".join(
        f"{item.get('emby_prefix', '')} => {item.get('local_prefix', '')}"
        for item in (value or [])
    )


def _coerce_emby_path_mappings(value):
    try:
        return parse_emby_path_mappings(value)
    except ValueError:
        return []


def _looks_absolute_path(value):
    value = str(value or "").strip()
    return bool(
        value.startswith(("/", "\\\\", "//"))
        or re.match(r"^[A-Za-z]:[\\/]", value)
    )


def validate_emby_path_mappings(value, lib_root=None):
    try:
        mappings = parse_emby_path_mappings(value)
    except ValueError as exc:
        return None, str(exc)
    if len(mappings) > MAX_EMBY_PATH_MAPPINGS:
        return None, f"At most {MAX_EMBY_PATH_MAPPINGS} Emby path mappings are allowed."
    root = os.path.realpath(lib_root or LIB_ROOT)
    normalized = []
    seen = set()
    for mapping in mappings:
        emby_prefix = mapping["emby_prefix"]
        local_prefix = mapping["local_prefix"]
        if ".." in emby_prefix.replace("\\", "/").split("/"):
            return None, "Emby prefixes cannot contain parent-directory segments."
        if not _looks_absolute_path(emby_prefix):
            return None, "Each Emby prefix must be an absolute POSIX, Windows, or UNC path."
        if not os.path.isabs(local_prefix):
            return None, "Each vid2gif prefix must be an absolute path."
        local_real = os.path.realpath(local_prefix)
        if not path_is_under(local_real, root):
            return None, "Each vid2gif prefix must stay inside the library root."
        if not os.path.isdir(local_real):
            return None, "Each vid2gif prefix must be an existing directory."
        key = emby_prefix.replace("\\", "/").rstrip("/").casefold()
        if key in seen:
            return None, "Emby prefixes must be unique."
        seen.add(key)
        normalized.append({"emby_prefix": emby_prefix, "local_prefix": local_real})
    return normalized, ""


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
        "video_preview_scan_path": str(
            data.get("video_preview_scan_path", defaults["video_preview_scan_path"])
            or defaults["video_preview_scan_path"]
        ).strip(),
        "emby_url": str(data.get("emby_url", defaults["emby_url"]) or "").strip(),
        "emby_api_key": str(data.get("emby_api_key", defaults["emby_api_key"]) or "").strip(),
        "emby_path_mappings": _coerce_emby_path_mappings(data.get("emby_path_mappings", [])),
        "emby_sync_after_maintenance": _bool(
            data.get("emby_sync_after_maintenance", defaults["emby_sync_after_maintenance"]),
            defaults["emby_sync_after_maintenance"],
        ),
        "emby_playback_protection": _bool(
            data.get("emby_playback_protection", defaults["emby_playback_protection"]),
            defaults["emby_playback_protection"],
        ),
        "emby_admin_notifications": _choice(
            data.get("emby_admin_notifications", defaults["emby_admin_notifications"]),
            {"off": "Off", "warnings": "Warnings", "all": "All"},
            defaults["emby_admin_notifications"],
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
    should_migrate = os.path.normcase(os.path.abspath(path)) == os.path.normcase(os.path.abspath(SETTINGS_PATH))
    if should_migrate and (
        data is None
        or "emby_url" not in data
        or "emby_api_key" not in data
        or "emby_sync_after_maintenance" not in data
        or "emby_playback_protection" not in data
        or "emby_admin_notifications" not in data
    ):
        legacy = _read_settings_file(LEGACY_EMBY_SETTINGS_PATH) or {}
        data = dict(data or {})
        migrated = False
        if legacy.get("emby_url") or legacy.get("emby_api_key"):
            data.setdefault("emby_url", legacy.get("emby_url") or "")
            data.setdefault("emby_api_key", legacy.get("emby_api_key") or "")
            migrated = True
        if "emby_sync_after_maintenance" not in data:
            data["emby_sync_after_maintenance"] = bool(
                legacy.get("emby_refresh_enabled", default_settings()["emby_sync_after_maintenance"])
            )
            migrated = True
        if "emby_playback_protection" not in data:
            data["emby_playback_protection"] = default_settings()["emby_playback_protection"]
            migrated = True
        if "emby_admin_notifications" not in data:
            data["emby_admin_notifications"] = default_settings()["emby_admin_notifications"]
            migrated = True
        if migrated:
            settings = _coerce_settings(data)
            _write_settings_unlocked(path, settings)
            return settings
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
        ("emby_admin_notifications", {"off": "Off", "warnings": "Warnings", "all": "All"}),
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
    if "video_preview_scan_path" in updates:
        raw_scan_path = str(updates.get("video_preview_scan_path") or "").strip()
        scan_path = os.path.realpath(raw_scan_path)
        if (
            not raw_scan_path
            or not path_is_under(scan_path, LIB_ROOT)
            or not os.path.isdir(scan_path)
            or os.path.islink(scan_path)
        ):
            return "Video preview scan path must be an existing folder under the library root"
    if "table_preferences" in updates and not isinstance(updates.get("table_preferences"), dict):
        return "Table preferences are invalid"
    if "emby_path_mappings" in updates:
        _mappings, error = validate_emby_path_mappings(updates.get("emby_path_mappings"))
        if error:
            return error
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
            if key == "emby_api_key_clear":
                if _bool(value):
                    merged["emby_api_key"] = ""
                continue
            if key == "emby_api_key":
                if str(value or "").strip():
                    merged[key] = str(value).strip()
                continue
            if key == "emby_path_mappings":
                mappings, _error = validate_emby_path_mappings(value)
                merged[key] = mappings
                continue
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
    if any(key in updates for key in ("emby_url", "emby_api_key", "emby_api_key_clear", "emby_path_mappings", "emby_playback_protection")):
        try:
            from . import emby_catalog

            emby_catalog.clear_cache()
        except ImportError:
            pass
        try:
            from . import emby_operations

            emby_operations.clear_cache()
        except ImportError:
            pass
        try:
            from . import emby_playback

            emby_playback.clear_cache()
        except ImportError:
            pass
    return settings, None


def public_settings(settings=None):
    public = dict(load_settings() if settings is None else settings)
    configured = bool(public.get("emby_api_key"))
    public.pop("emby_api_key", None)
    public["emby_api_key_configured"] = configured
    return public


def warning_for_preview_height(height):
    if height is None:
        return "Original GIFs can be very heavy in the browser."
    if height >= 1440:
        return "Large previews may still be slow on some clients."
    return ""
