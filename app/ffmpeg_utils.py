import subprocess
import shlex
import json
from collections import deque



def ffmpeg_version():
    try:
        return subprocess.check_output(["ffmpeg", "-version"]).decode().splitlines()[0]
    except Exception:
        return "ffmpeg not found"


def get_duration(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, f"Command {cmd_str} failed: {e}"

    if proc.returncode != 0:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, (
            f"Command {cmd_str} returned {proc.returncode}: {proc.stderr.strip()}"
        )

    try:
        return float(proc.stdout.strip()), None
    except Exception:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, (
            f"Command {cmd_str} produced unexpected output: {proc.stdout.strip()}"
        )


def probe_video_details(video_path):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,avg_frame_rate",
        "-of",
        "json",
        video_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    except Exception as e:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, f"Command {cmd_str} failed: {e}"

    if proc.returncode != 0:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, (
            f"Command {cmd_str} returned {proc.returncode}: {proc.stderr.strip()}"
        )

    return proc.stdout, None


def build_segments(dur, cfg):
    clip = cfg["clip_len"]
    start_buf = cfg["start_buffer"]
    end_buf = cfg["end_buffer"]
    min_start = max(0.0, start_buf)
    max_start = max(0.0, dur - end_buf - clip)
    points = []
    if cfg["abs_early"] > 0:
        points.append(cfg["abs_early"])
    for p in cfg["percent_points"]:
        points.append(dur * p / 100.0)
    if cfg["abs_late_from_end"] > 0:
        points.append(max(0.0, dur - cfg["abs_late_from_end"]))
    points.sort()
    valid = []
    for t in points:
        st = min(max(t, min_start), max_start)
        if st < 0:
            continue
        if not valid or abs(st - valid[-1]) >= (clip / 2.0):
            valid.append(st)
    if not valid:
        n = max(int(dur // (clip + 1)), 1)
        for i in range(n):
            st = min(min_start + i * ((dur - min_start) / n), max_start)
            valid.append(st)
    return [{"start": st, "end": min(st + clip, dur)} for st in valid]


def _first_video_stream_index(video, logger):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v",
        "-show_entries",
        "stream=index,disposition",
        "-of",
        "json",
        video,
    ]
    try:
        info = json.loads(subprocess.check_output(cmd, text=True))
        streams = info.get("streams", [])
        idx = 0
        for i, s in enumerate(streams):
            if s.get("disposition", {}).get("attached_pic") != 1:
                idx = i
                break
        if streams and streams[0].get("disposition", {}).get("attached_pic") == 1 and idx != 0:
            logger.warning(
                "Discarding attached picture stream; using video stream index %s",
                idx,
            )
        return idx
    except Exception:
        return 0


def _get_source_fps(video):
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=avg_frame_rate",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        video,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            return None
        rate = proc.stdout.strip()
        if not rate or rate == "0/0" or "/" not in rate:
            return None
        num, den = rate.split("/")
        den = float(den)
        if den == 0:
            return None
        return float(num) / den
    except Exception:
        return None


def _fps_to_float(value):
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        if "/" in value:
            num, den = value.split("/", 1)
            try:
                den = float(den)
                if den == 0:
                    return None
                return float(num) / den
            except (ValueError, ZeroDivisionError):
                return None
        try:
            return float(value)
        except ValueError:
            return None
    return None


def make_gif_multi_inputs(video, segs, out_gif, cfg, job):
    fps_cfg = cfg["fps"]
    if fps_cfg == "original":
        details, err = probe_video_details(video)
        if err:
            return False, err
        try:
            info = json.loads(details or "{}")
            stream = info.get("streams", [{}])[0]
            fps = stream.get("avg_frame_rate", "0")
            if not fps or fps == "0/0":
                fps = "15"
        except Exception as e:
            return False, str(e)
    else:
        fps = str(int(fps_cfg))
    height = int(cfg["height"])
    loop = "0" if cfg["loop_forever"] else "1"

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-v",
        "info",
        "-nostats",
        "-progress",
        "pipe:2",
        "-fflags",
        "+genpts",
        "-an",
        "-sn",
    ]

    for s in segs:
        dur = max(0.01, s["end"] - s["start"])
        args += ["-ss", f"{s['start']:.3f}", "-t", f"{dur:.3f}", "-i", video]

    n = len(segs)
    stream_idx = _first_video_stream_index(video, job["logger"])
    concat_inputs = "".join(f"[{i}:v:{stream_idx}]" for i in range(n))

    main_filters = ""
    if cfg.get("smooth"):
        src_fps = _get_source_fps(video)
        target_fps = _fps_to_float(fps)
        if src_fps is not None and target_fps is not None and abs(src_fps - target_fps) > 0.1:
            main_filters += f"minterpolate=fps={fps},"
    main_filters += (
        f"fps={fps},scale=-1:{height}:flags=lanczos,"
        "format=rgb24,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    )
    filter_graph = (
        f"{concat_inputs}concat=n={n}:v=1:a=0[vcat];" f"[vcat]{main_filters}"
    )

    args += ["-filter_complex", filter_graph, "-loop", loop, out_gif]

    job["logger"].info("----- FFMPEG CMD -----")
    job["logger"].info(" ".join(shlex.quote(a) for a in args))

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_lines = deque(maxlen=20)
    for raw in proc.stdout:
        line = (raw or "").rstrip("\n")
        if not line:
            continue
        job["logger"].info(line)
        last_lines.append(line)
        if (
            "frame=" in line
            or "time=" in line
            or "speed=" in line
            or line.startswith("out_time=")
        ):
            job["progress_text"] = line.strip()

    proc.wait()
    if proc.returncode != 0:
        tail = "\n".join(last_lines)
        msg = f"ffmpeg exited with code {proc.returncode}\n{tail}".rstrip()
        job["logger"].error(msg)
        return False, msg

    return True, ""

