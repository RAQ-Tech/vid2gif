
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

