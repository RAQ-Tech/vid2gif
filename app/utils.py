import os
from pathlib import Path


def parse_float(s, fb):
    try:
        return float(s)
    except Exception:
        return fb

def parse_int_list(s):
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            out.append(int(tok))
        except Exception:
            pass
    return out

def choose_numeric(form, preset_key, custom_key, caster, default_val):
    """Handle preset/custom numeric inputs from forms."""
    if preset_key in form or custom_key in form:
        preset = (form.get(preset_key, "") or "").strip().lower()
        if preset and preset != "custom":
            try:
                return caster(preset)
            except Exception:
                return default_val
        custom = (form.get(custom_key, "") or "").strip()
        if custom:
            try:
                return caster(custom)
            except Exception:
                return default_val
        return default_val

    legacy_key = preset_key.replace("_preset", "")
    val = (form.get(legacy_key, "") or "").strip()
    if val:
        try:
            return caster(val)
        except Exception:
            return default_val
    return default_val

def path_is_under(path: str, root: str) -> bool:
    """Return True when path is contained by root after normalization."""
    if not path or not root:
        return False
    try:
        path_abs = os.path.normcase(os.path.realpath(os.path.abspath(path)))
        root_abs = os.path.normcase(os.path.realpath(os.path.abspath(root)))
        return os.path.commonpath([path_abs, root_abs]) == root_abs
    except (OSError, ValueError):
        return False

def resolve_case_insensitive(path: str):
    """Return the actual filesystem path matching the given path, ignoring case.

    If any segment does not exist, return None.
    """
    if not path or not os.path.isabs(path):
        return None

    parts = Path(os.path.abspath(path)).parts
    if not parts:
        return None

    cur = parts[0]
    if not os.path.exists(cur):
        return None

    for part in parts[1:]:
        try:
            entries = os.listdir(cur)
        except Exception:
            return None
        match = None
        for name in entries:
            if name.lower() == part.lower():
                match = name
                break
        if match is None:
            return None
        cur = os.path.join(cur, match)
    return cur


BACKGROUND_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".tbn", ".bmp"}
BACKGROUND_IMAGE_TYPES = ("background", "backdrop", "fanart", "art")


def _background_variant_number(name: str, image_type: str):
    if name == image_type:
        return 0

    suffix = name[len(image_type):]
    if suffix.isdigit():
        return int(suffix)
    if suffix.startswith("-") and suffix[1:].isdigit():
        return int(suffix[1:])
    return None


def _background_image_score(name: str, video_base: str):
    lower_name = name.lower()
    lower_base = video_base.lower()
    prefixed = f"{lower_base}-"

    if lower_name.startswith(prefixed):
        suffix = lower_name[len(prefixed):]
        for type_index, image_type in enumerate(BACKGROUND_IMAGE_TYPES):
            variant = _background_variant_number(suffix, image_type)
            if variant is not None:
                return (0, type_index, variant)

    for type_index, image_type in enumerate(BACKGROUND_IMAGE_TYPES):
        variant = _background_variant_number(lower_name, image_type)
        if variant is not None:
            return (1, type_index, variant)

    return None


def find_background_image(video_path: str):
    """Return a companion background image for ``video_path`` if present.

    Supports common Emby/Jellyfin/Plex backdrop naming patterns.  Matching is
    case-insensitive and prefers video-specific names over folder-level names.
    """

    if not video_path:
        return None

    directory = os.path.dirname(video_path)
    base_name = os.path.splitext(os.path.basename(video_path))[0]
    if not directory or not base_name:
        return None

    try:
        entries = os.listdir(directory)
    except OSError:
        return None

    candidates = []

    for entry in entries:
        name, ext = os.path.splitext(entry)
        if ext.lower() not in BACKGROUND_IMAGE_EXTS:
            continue
        score = _background_image_score(name, base_name)
        if score is not None:
            candidates.append((score, entry.lower(), entry))

    if candidates:
        _, _, entry = min(candidates)
        return os.path.join(directory, entry)

    return None
