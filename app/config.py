import os


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# -------- Paths & setup --------
LIB_ROOT = os.getenv("LIB_ROOT", "/library")
# Whether STATE_ROOT was chosen deliberately or fell back to the container
# default. The distinction decides whether creating it is safe.
STATE_ROOT_IS_DEFAULT = os.getenv("STATE_ROOT") is None
STATE_ROOT = os.getenv("STATE_ROOT", "/state")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(STATE_ROOT, "logs"))
TMP_ROOT = os.getenv("TMP_ROOT", os.path.join(STATE_ROOT, "tmp"))
PROCESS_TMP_ROOT = os.getenv("PROCESS_TMP_ROOT", os.path.join(STATE_ROOT, "processing", "tmp"))
TEST_LAB_ROOT = os.getenv("TEST_LAB_ROOT", os.path.join(STATE_ROOT, "test-lab"))
LANDSCAPE_POSTER_ROOT = os.getenv("LANDSCAPE_POSTER_ROOT", os.path.join(STATE_ROOT, "landscape-posters"))
GIF_OPTIMIZE = os.getenv("GIF_OPTIMIZE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GIF_OPTIMIZE_LEVEL = os.getenv("GIF_OPTIMIZE_LEVEL", "2")
GIFSICLE_BIN = os.getenv("GIFSICLE_BIN", "gifsicle")
GIF_OPTIMIZE_TIMEOUT = _env_int("GIF_OPTIMIZE_TIMEOUT", 600)
GIF_GENERATION_STALL_TIMEOUT = max(30, _env_int("GIF_GENERATION_STALL_TIMEOUT", 180))
LANDSCAPE_POSTER_INTERVAL_SECONDS = _env_int("LANDSCAPE_POSTER_INTERVAL_SECONDS", 900)
LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS = _env_int("LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS", 86400)


class StateRootError(RuntimeError):
    """STATE_ROOT is unusable, and guessing would write somewhere unwanted."""


def _ensure_state_directories():
    """Create the state subdirectories, but never invent the root itself.

    Importing this module has always had a filesystem side effect. That is fine
    in the container: the image creates /state and a volume is mounted over it.
    Off the container the default is a path nobody asked for -- a stray folder
    at the drive root on Windows, a permission error on Linux -- and anything
    importing `app` without setting STATE_ROOT quietly filled it with state.

    So an explicit STATE_ROOT is honoured and created. The default is used only
    when it already exists, which is exactly the container case. Otherwise fail
    with a message that says what to do, rather than littering.
    """
    if STATE_ROOT_IS_DEFAULT and not os.path.isdir(STATE_ROOT):
        raise StateRootError(
            f"STATE_ROOT is not set and the default {STATE_ROOT!r} does not exist,"
            " so there is nowhere safe to put application state."
            "\n  - Running locally: export STATE_ROOT to a scratch directory first,"
            " for example STATE_ROOT=/tmp/vid2gif-state."
            "\n  - Running tests: use pytest, which sets it for you via tests/conftest.py."
            "\n  - In Docker: /state is created by the image, so this should not happen"
            " -- check the volume mount."
        )
    for path in (LOG_DIR, TMP_ROOT, PROCESS_TMP_ROOT, TEST_LAB_ROOT, LANDSCAPE_POSTER_ROOT):
        os.makedirs(path, exist_ok=True)


_ensure_state_directories()

VIDEO_EXTS = {".mkv", ".mp4", ".m4v", ".mov", ".avi", ".wmv", ".mpg", ".mpeg", ".webm"}

DEFAULTS = {
    "height": 480,  # using HEIGHT (scale keeps aspect via -1:HEIGHT)
    "fps": 15,
    "clip_len": 2.0,
    "percent_points": "10,20,30,40,50,60,70,80,90",
    "abs_early": 15.0,
    "abs_late_from_end": 10.0,
    "start_buffer": 5.0,
    "end_buffer": 5.0,
    "loop_forever": True,
    "smooth": False,
    "optimize": GIF_OPTIMIZE,
}
