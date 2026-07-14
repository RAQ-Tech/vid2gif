import os


def _env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


# -------- Paths & setup --------
LIB_ROOT = os.getenv("LIB_ROOT", "/library")
STATE_ROOT = os.getenv("STATE_ROOT", "/state")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(STATE_ROOT, "logs"))
TMP_ROOT = os.getenv("TMP_ROOT", os.path.join(STATE_ROOT, "tmp"))
PROCESS_TMP_ROOT = os.getenv(
    "PROCESS_TMP_ROOT", os.path.join(STATE_ROOT, "processing", "tmp")
)
TEST_LAB_ROOT = os.getenv("TEST_LAB_ROOT", os.path.join(STATE_ROOT, "test-lab"))
LANDSCAPE_POSTER_ROOT = os.getenv(
    "LANDSCAPE_POSTER_ROOT", os.path.join(STATE_ROOT, "landscape-posters")
)
GIF_OPTIMIZE = os.getenv("GIF_OPTIMIZE", "1").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
GIF_OPTIMIZE_LEVEL = os.getenv("GIF_OPTIMIZE_LEVEL", "2")
GIFSICLE_BIN = os.getenv("GIFSICLE_BIN", "gifsicle")
GIF_OPTIMIZE_TIMEOUT = _env_int("GIF_OPTIMIZE_TIMEOUT", 600)
GIF_GENERATION_STALL_TIMEOUT = max(
    30, _env_int("GIF_GENERATION_STALL_TIMEOUT", 180)
)
LANDSCAPE_POSTER_INTERVAL_SECONDS = _env_int("LANDSCAPE_POSTER_INTERVAL_SECONDS", 900)
LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS = _env_int(
    "LANDSCAPE_POSTER_FULL_INTERVAL_SECONDS", 86400
)

for path in (LOG_DIR, TMP_ROOT, PROCESS_TMP_ROOT, TEST_LAB_ROOT, LANDSCAPE_POSTER_ROOT):
    os.makedirs(path, exist_ok=True)

VIDEO_EXTS = {".mkv",".mp4",".m4v",".mov",".avi",".wmv",".mpg",".mpeg",".webm"}

DEFAULTS = {
    "height": 480,   # using HEIGHT (scale keeps aspect via -1:HEIGHT)
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
