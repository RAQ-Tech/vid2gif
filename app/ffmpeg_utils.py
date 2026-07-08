import subprocess
import shlex
import json
import os
import time
from collections import deque

from .progress import update_render_progress


FFMPEG_PROGRESS_KEYS = {
    "bitrate",
    "drop_frames",
    "dup_frames",
    "fps",
    "frame",
    "out_time",
    "out_time_ms",
    "out_time_us",
    "progress",
    "speed",
    "stream_0_0_q",
    "total_size",
}


def _env_int(name, default):
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


FFPROBE_TIMEOUT_SECONDS = max(1, _env_int("FFPROBE_TIMEOUT_SECONDS", 30))


def _check_output_with_timeout(cmd, **kwargs):
    try:
        return subprocess.check_output(cmd, timeout=FFPROBE_TIMEOUT_SECONDS, **kwargs)
    except TypeError:
        return subprocess.check_output(cmd, **kwargs)


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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, f"Command {cmd_str} timed out after {FFPROBE_TIMEOUT_SECONDS}s"
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, f"Command {cmd_str} timed out after {FFPROBE_TIMEOUT_SECONDS}s"
    except Exception as e:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, f"Command {cmd_str} failed: {e}"

    if proc.returncode != 0:
        cmd_str = " ".join(shlex.quote(c) for c in cmd)
        return None, (
            f"Command {cmd_str} returned {proc.returncode}: {proc.stderr.strip()}"
        )

    return proc.stdout, None


def parse_ffmpeg_time(value):
    value = (value or "").strip()
    if not value or value.upper() == "N/A":
        return None
    try:
        if ":" not in value:
            return float(value)
        parts = value.split(":")
        if len(parts) != 3:
            return None
        hours = int(parts[0])
        minutes = int(parts[1])
        seconds = float(parts[2])
        return hours * 3600 + minutes * 60 + seconds
    except (TypeError, ValueError):
        return None


def parse_ffmpeg_progress_line(line):
    if not line or "=" not in line:
        return {}
    key, value = line.split("=", 1)
    key = key.strip()
    value = value.strip()

    if key in ("out_time_ms", "out_time_us"):
        try:
            return {"out_time_seconds": int(value) / 1_000_000}
        except (TypeError, ValueError):
            return {}
    if key == "out_time":
        seconds = parse_ffmpeg_time(value)
        if seconds is None:
            return {}
        return {"out_time_seconds": seconds}
    if key == "frame":
        try:
            return {"frame": int(value)}
        except (TypeError, ValueError):
            return {}
    if key == "progress":
        return {"progress": value}
    return {}


def is_ffmpeg_progress_line(line):
    if not line or "=" not in line:
        return False
    key = line.split("=", 1)[0].strip()
    return key in FFMPEG_PROGRESS_KEYS


def summarize_video_details(details):
    try:
        info = json.loads(details or "{}")
        stream = info.get("streams", [{}])[0]
    except Exception:
        return "Video details unavailable"

    codec = stream.get("codec_name") or "unknown codec"
    width = stream.get("width")
    height = stream.get("height")
    fps = _fps_to_float(stream.get("avg_frame_rate"))
    parts = [str(codec)]
    if width and height:
        parts.append(f"{width}x{height}")
    if fps:
        parts.append(f"{fps:.2f} fps")
    return " · ".join(parts)


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
        info = json.loads(_check_output_with_timeout(cmd, text=True))
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
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=FFPROBE_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return None
    try:
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


def _source_video_info(video):
    details, err = probe_video_details(video)
    if err:
        return None, err
    try:
        info = json.loads(details or "{}")
        stream = info.get("streams", [{}])[0]
        return stream, None
    except Exception as e:
        return None, str(e)


def _target_width_for_height(stream, height):
    try:
        width = int(stream.get("width") or 0)
        source_height = int(stream.get("height") or 0)
    except (TypeError, ValueError):
        width = 0
        source_height = 0
    if width <= 0 or source_height <= 0:
        return None
    target_width = round(width * height / source_height)
    return max(1, target_width)


def _normalize_filter(ref, label, width, height):
    scale = (
        f"{ref}scale=w={width}:h={height}:force_original_aspect_ratio=decrease:"
        f"flags=lanczos,"
    )
    pad = (
        f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,"
        "setsar=1"
    )
    return f"{scale}{pad}[{label}]"


def make_gif_multi_inputs(video, segs, out_gif, cfg, job, background_image=None):
    stream, err = _source_video_info(video)
    if err:
        return False, err

    fps_cfg = cfg["fps"]
    if fps_cfg == "original":
        fps = stream.get("avg_frame_rate", "0")
        if not fps or fps == "0/0":
            fps = "15"
    else:
        fps = str(int(fps_cfg))
    height = int(cfg["height"])
    width = _target_width_for_height(stream, height)
    if width is None:
        return False, "Could not read source video dimensions."
    loop = "0" if cfg["loop_forever"] else "1"

    args = [
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-v",
        "warning",
        "-nostats",
        "-progress",
        "pipe:2",
        "-fflags",
        "+genpts",
        "-an",
        "-sn",
    ]

    background_ref = None
    video_refs = []
    input_filters = []
    target_fps_float = _fps_to_float(fps)
    if target_fps_float is None or target_fps_float <= 0:
        target_fps_float = 15.0

    if background_image:
        still_duration = max(1.0 / target_fps_float, 1 / 30.0)
        args += [
            "-loop",
            "1",
            "-t",
            f"{still_duration:.3f}",
            "-i",
            background_image,
        ]
        input_filters.append(_normalize_filter("[0:v]", "bg_norm", width, height))
        input_filters.append(
            f"[bg_norm]fps={fps},trim=end_frame=1,setpts=PTS-STARTPTS[bg]"
        )
        background_ref = "[bg]"

    stream_idx = _first_video_stream_index(video, job["logger"])
    video_offset = 1 if background_image else 0

    for i, s in enumerate(segs):
        dur = max(0.01, s["end"] - s["start"])
        args += ["-ss", f"{s['start']:.3f}", "-t", f"{dur:.3f}", "-i", video]
        label = f"v{len(video_refs)}"
        input_filters.append(
            _normalize_filter(
                f"[{i + video_offset}:v:{stream_idx}]",
                label,
                width,
                height,
            )
        )
        video_refs.append(f"[{label}]")

    video_concat_inputs = "".join(video_refs)

    video_filters = ""
    if cfg.get("smooth"):
        src_fps = _get_source_fps(video)
        target_fps = _fps_to_float(fps)
        if src_fps is not None and target_fps is not None and abs(src_fps - target_fps) > 0.1:
            video_filters += f"minterpolate=fps={fps},"
    video_filters += f"fps={fps},setpts=PTS-STARTPTS[vmain]"

    filter_parts = list(input_filters)
    filter_parts.append(
        f"{video_concat_inputs}concat=n={len(video_refs)}:v=1:a=0[vcatraw]"
    )
    filter_parts.append(f"[vcatraw]{video_filters}")

    if background_ref:
        filter_parts.append(f"{background_ref}[vmain]concat=n=2:v=1:a=0[vout]")
        final_ref = "[vout]"
    else:
        final_ref = "[vmain]"

    filter_parts.append(
        f"{final_ref}format=rgb24,split[s0][s1];[s0]palettegen[p];[s1][p]paletteuse"
    )
    filter_graph = ";".join(filter_parts)

    args += ["-filter_complex", filter_graph, "-loop", loop, out_gif]

    expected_seconds = sum(max(0.0, s["end"] - s["start"]) for s in segs)
    if background_image:
        expected_seconds += 1.0 / target_fps_float
    job["expected_duration_seconds"] = expected_seconds
    job["logger"].info("GIF generation started")

    proc = subprocess.Popen(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    last_error_lines = deque(maxlen=12)
    last_logged_percent = -5
    last_logged_at = 0
    for raw in proc.stdout:
        line = (raw or "").rstrip("\n")
        if not line:
            continue
        parsed = parse_ffmpeg_progress_line(line)
        if parsed:
            update_render_progress(
                job,
                expected_seconds,
                out_time_seconds=parsed.get("out_time_seconds"),
                frame=parsed.get("frame"),
                fps=target_fps_float,
            )
            if parsed.get("progress") == "end":
                update_render_progress(
                    job,
                    expected_seconds,
                    out_time_seconds=expected_seconds,
                    fps=target_fps_float,
                )
            percent = job.get("progress_percent", 0)
            now = time.time()
            should_log = (
                percent > 0
                and (
                    percent >= 100
                    or percent - last_logged_percent >= 5
                    or now - last_logged_at >= 15
                )
            )
            if should_log:
                job["logger"].info(f"Progress: {job.get('progress_label', '')}")
                last_logged_percent = percent
                last_logged_at = now
        elif not is_ffmpeg_progress_line(line):
            last_error_lines.append(line)

    proc.wait()
    if proc.returncode != 0:
        tail = "\n".join(last_error_lines)
        msg = f"ffmpeg exited with code {proc.returncode}"
        if tail:
            msg = f"{msg}\n{tail}"
        job["logger"].error(msg)
        job["_ffmpeg_error_logged"] = True
        return False, msg

    update_render_progress(
        job,
        expected_seconds,
        out_time_seconds=expected_seconds,
        fps=target_fps_float,
    )
    job["logger"].info("GIF generation finished")
    return True, ""
