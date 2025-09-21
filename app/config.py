import os

# -------- Paths & setup --------
LIB_ROOT = os.getenv("LIB_ROOT", "/library")
STATE_ROOT = os.getenv("STATE_ROOT", "/state")
LOG_DIR = os.getenv("LOG_DIR", os.path.join(STATE_ROOT, "logs"))
TMP_ROOT = os.getenv("TMP_ROOT", os.path.join(STATE_ROOT, "tmp"))
PROCESS_TMP_ROOT = os.getenv(
    "PROCESS_TMP_ROOT", os.path.join(STATE_ROOT, "processing", "tmp")
)

for path in (LOG_DIR, TMP_ROOT, PROCESS_TMP_ROOT):
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
}
