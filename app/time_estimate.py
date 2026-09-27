"""Progress-based timing, independent of presentation percentages.

Observations are bounded and serializable. Reading an estimate never adds samples:
poll frequency must not affect the prediction. Unknown work stays unknown.
"""

import math


WINDOW_SECONDS = 30
MIN_OBSERVATION_SECONDS = 2


def number(value):
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) and value >= 0 else None


def observe(state, completed, now):
    completed, now = number(completed), number(now)
    if completed is None or now is None:
        return
    points = state.setdefault("points", [])
    if points and (now < points[-1][0] or completed < points[-1][1]):
        return
    if points and completed == points[-1][1]:
        return
    state["last_advance"] = now
    # Coalesce subsecond FFmpeg fields/pages without giving fast reporters more weight.
    if len(points) > 1 and int(now) == int(points[-1][0]):
        points[-1] = [now, completed]
    else:
        points.append([now, completed])
    while len(points) > 2 and points[1][0] <= now - WINDOW_SECONDS:
        points.pop(0)
    del points[:-64]


def remaining(state, total, now, historical_rate=None):
    total = number(total)
    points = state.get("points") or []
    if total is None or not points:
        return None, "calibrating"
    completed = points[-1][1]
    if completed >= total:
        return 0, "live"
    span = points[-1][0] - points[0][0]
    delta = completed - points[0][1]
    rate = number(historical_rate)
    confidence = "learning"
    if span >= MIN_OBSERVATION_SECONDS and delta > 0:
        rate = span / delta
        confidence = "live"
    if not rate:
        return None, "calibrating"
    idle = max(0, now - state.get("last_advance", points[-1][0]))
    # A blocked filesystem/network call is not evidence of continued progress.
    if idle > max(10, 3 * rate):
        return None, "recalculating"
    return max(1, math.ceil((total - completed) * rate)), confidence


def historical_remaining(duration, elapsed):
    duration = number(duration)
    if duration is None:
        return None
    remaining_seconds = duration - max(0, elapsed)
    return max(1, math.ceil(remaining_seconds)) if remaining_seconds > 0 else None


def serial_sum(values):
    """An unknown component makes the whole finish time unknown."""
    values = list(values)
    if any(number(value) is None for value in values):
        return None
    return math.ceil(sum(values))
