import os


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

def resolve_case_insensitive(path: str):
    """Return the actual filesystem path matching the given path, ignoring case.

    If any segment does not exist, return None.
    """
    if not os.path.isabs(path):
        return None
    parts = [p for p in path.split('/') if p]
    if path.startswith('/'):
        cur = '/'
    else:
        cur = ''
    for part in parts:
        try:
            entries = os.listdir(cur or '/')
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

