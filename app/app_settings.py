import json
import os
import threading

from .config import LIB_ROOT, STATE_ROOT


SCHEMA_VERSION = 3
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

_settings_lock = threading.Lock()


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
    }


def load_settings(path=None):
    path = path or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_settings()
    if not isinstance(data, dict):
        return default_settings()
    return _coerce_settings(data)


def save_settings(settings, path=None):
    settings = _coerce_settings(settings)
    path = path or SETTINGS_PATH
    with _settings_lock:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp_path = f"{path}.{os.getpid()}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(settings, f, separators=(",", ":"))
            os.replace(tmp_path, path)
        except Exception:
            return False
    return True


def warning_for_preview_height(height):
    if height is None:
        return "Original GIFs can be very heavy in the browser."
    if height >= 1440:
        return "Large previews may still be slow on some clients."
    return ""
