import json
import os
import threading

from .config import STATE_ROOT


SCHEMA_VERSION = 1
DEFAULT_TEST_LAB_PREVIEW_HEIGHT = 720
PREVIEW_HEIGHT_PRESETS = (540, 720, 1080, 1440, 2160)
SETTINGS_PATH = os.path.join(STATE_ROOT, "app_settings.json")

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
    }


def _coerce_settings(data):
    if not isinstance(data, dict):
        return default_settings()
    height, err = parse_preview_height(
        data.get("test_lab_preview_height", DEFAULT_TEST_LAB_PREVIEW_HEIGHT)
    )
    if err:
        height = DEFAULT_TEST_LAB_PREVIEW_HEIGHT
    return {
        "schema_version": SCHEMA_VERSION,
        "test_lab_preview_height": height,
    }


def load_settings(path=None):
    path = path or SETTINGS_PATH
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return default_settings()
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
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
