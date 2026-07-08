import subprocess
import json
import shutil
import pytest

from app import ffmpeg_utils


class DummyLogger:
    def __init__(self):
        self.info_msgs = []
        self.error_msgs = []

    def info(self, msg):
        self.info_msgs.append(msg)
    def warning(self, msg, *args):
        pass
        if args:
            msg = msg % args
        self.info_msgs.append(msg)
    def error(self, msg):
        self.error_msgs.append(msg)


def _patch_probe(monkeypatch, width=640, height=360, fps="24/1"):
    def fake_probe(video):
        return (
            json.dumps(
                {
                    "streams": [
                        {
                            "width": width,
                            "height": height,
                            "avg_frame_rate": fps,
                        }
                    ]
                }
            ),
            None,
        )

    monkeypatch.setattr(ffmpeg_utils, "probe_video_details", fake_probe)



def test_parse_ffmpeg_progress_line_out_time_ms():
    assert ffmpeg_utils.parse_ffmpeg_progress_line("out_time_ms=1500000") == {
        "out_time_seconds": 1.5
    }


def test_parse_ffmpeg_progress_line_out_time():
    assert ffmpeg_utils.parse_ffmpeg_progress_line("out_time=00:01:02.500000") == {
        "out_time_seconds": 62.5
    }


def test_parse_ffmpeg_progress_line_frame_and_completion():
    assert ffmpeg_utils.parse_ffmpeg_progress_line("frame=42") == {"frame": 42}
    assert ffmpeg_utils.parse_ffmpeg_progress_line("progress=end") == {
        "progress": "end"
    }


@pytest.mark.parametrize(
    "line",
    ["out_time_ms=not-a-number", "out_time=not-a-time", "frame=none", "plain text"],
)
def test_parse_ffmpeg_progress_line_ignores_malformed_values(line):
    assert ffmpeg_utils.parse_ffmpeg_progress_line(line) == {}


def test_make_gif_multi_inputs_includes_input_flag(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    cfg = {"fps": 10, "height": 320, "loop_forever": True}
    job = {"logger": DummyLogger(), "progress_text": ""}

    captured = {}
    _patch_probe(monkeypatch)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdout = []
            self.returncode = 0
        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    ffmpeg_utils.make_gif_multi_inputs(video, segs, "out.gif", cfg, job)

    args = captured["args"]
    indices = [i for i, a in enumerate(args) if a == video]
    assert len(indices) == len(segs)
    for idx in indices:
        assert args[idx - 1] == "-i"


def test_make_gif_multi_inputs_minterpolate(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}]
    cfg = {"fps": 30, "height": 320, "loop_forever": True, "smooth": True}
    job = {"logger": DummyLogger(), "progress_text": ""}

    captured = {}
    _patch_probe(monkeypatch)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(ffmpeg_utils, "_get_source_fps", lambda v: 24.0)

    ffmpeg_utils.make_gif_multi_inputs(video, segs, "out.gif", cfg, job)

    args = captured["args"]
    filt = args[args.index("-filter_complex") + 1]
    assert "minterpolate=fps=30" in filt
    assert filt.index("minterpolate=fps=30") < filt.index("fps=30")


def test_make_gif_multi_inputs_no_minterpolate_when_match(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}]
    cfg = {"fps": 24, "height": 320, "loop_forever": True, "smooth": True}
    job = {"logger": DummyLogger(), "progress_text": ""}

    captured = {}
    _patch_probe(monkeypatch)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(ffmpeg_utils, "_get_source_fps", lambda v: 24.0)

    ffmpeg_utils.make_gif_multi_inputs(video, segs, "out.gif", cfg, job)

    args = captured["args"]
    filt = args[args.index("-filter_complex") + 1]
    assert "minterpolate" not in filt


def test_make_gif_multi_inputs_logs_failure(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}]
    cfg = {"fps": 10, "height": 320, "loop_forever": True}
    logger = DummyLogger()
    job = {"logger": logger, "progress_text": ""}
    _patch_probe(monkeypatch)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            self.stdout = ["frame=10", "speed=0.1x", "last error"]
            self.returncode = 1

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    ok, msg = ffmpeg_utils.make_gif_multi_inputs(video, segs, "out.gif", cfg, job)
    assert not ok
    assert "ffmpeg exited with code 1" in msg
    assert "last error" in msg
    assert "speed=0.1x" not in msg
    assert not any("speed=" in line for line in logger.info_msgs)
    assert logger.error_msgs and msg == logger.error_msgs[0]


def test_make_gif_multi_inputs_normalizes_background_before_concat(monkeypatch, tmp_path):
    video = "input.mp4"
    background = tmp_path / "input-background.png"
    background.write_bytes(b"png")
    segs = [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    cfg = {"fps": 10, "height": 360, "loop_forever": True}
    job = {"logger": DummyLogger(), "progress_text": ""}
    captured = {}
    _patch_probe(monkeypatch, width=3840, height=2160)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    ok, msg = ffmpeg_utils.make_gif_multi_inputs(
        video,
        segs,
        "out.gif",
        cfg,
        job,
        background_image=str(background),
    )

    assert ok
    assert msg == ""
    args = captured["args"]
    filt = args[args.index("-filter_complex") + 1]
    video_concat_pos = filt.index("[v0][v1]concat=n=2:v=1:a=0[vcatraw]")
    final_concat_pos = filt.index("[bg][vmain]concat=n=2:v=1:a=0[vout]")
    assert filt.index("[0:v]scale=w=640:h=360") < final_concat_pos
    assert "[bg_norm]fps=10,trim=end_frame=1,setpts=PTS-STARTPTS[bg]" in filt
    assert filt.index("[1:v:0]scale=w=640:h=360") < video_concat_pos
    assert filt.index("[2:v:0]scale=w=640:h=360") < video_concat_pos
    assert video_concat_pos < final_concat_pos
    assert filt.index("[bg_norm]fps=10") < final_concat_pos
    assert "force_original_aspect_ratio=decrease" in filt
    assert "pad=640:360:(ow-iw)/2:(oh-ih)/2" in filt
    assert args.count(str(background)) == 1
    assert not any(str(background) in arg and arg != str(background) for arg in args)


def test_make_gif_multi_inputs_smooths_video_after_background(monkeypatch, tmp_path):
    video = "input.mp4"
    background = tmp_path / "input-background.png"
    background.write_bytes(b"png")
    segs = [{"start": 0.0, "end": 1.0}]
    cfg = {"fps": 30, "height": 320, "loop_forever": True, "smooth": True}
    job = {"logger": DummyLogger(), "progress_text": ""}
    captured = {}
    _patch_probe(monkeypatch)

    class DummyPopen:
        def __init__(self, args, **kwargs):
            captured["args"] = args
            self.stdout = []
            self.returncode = 0

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)
    monkeypatch.setattr(ffmpeg_utils, "_get_source_fps", lambda v: 24.0)

    ffmpeg_utils.make_gif_multi_inputs(
        video,
        segs,
        "out.gif",
        cfg,
        job,
        background_image=str(background),
    )

    filt = captured["args"][captured["args"].index("-filter_complex") + 1]
    assert "[bg_norm]fps=30,trim=end_frame=1,setpts=PTS-STARTPTS[bg]" in filt
    assert "[vcatraw]minterpolate=fps=30,fps=30,setpts=PTS-STARTPTS[vmain]" in filt
    assert filt.index("[vcatraw]minterpolate") < filt.index("[bg][vmain]concat")


def _require_ffmpeg():
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for GIF frame regression tests")


def _run_ffmpeg(args):
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args],
        check=True,
        capture_output=True,
        text=True,
    )


def _first_frame_rgb(path):
    data = subprocess.check_output(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-i",
            str(path),
            "-frames:v",
            "1",
            "-vf",
            "scale=1:1,format=rgb24",
            "-f",
            "rawvideo",
            "pipe:1",
        ]
    )
    return tuple(data[:3])


@pytest.mark.parametrize("smooth", [False, True])
def test_generated_gif_uses_background_image_as_frame_zero(tmp_path, smooth):
    _require_ffmpeg()
    video = tmp_path / "sample.mp4"
    background = tmp_path / "sample-background.png"
    out_gif = tmp_path / "poster.gif"
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=2:r=10",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ]
    )
    _run_ffmpeg(
        [
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=160x90:d=1",
            "-frames:v",
            "1",
            str(background),
        ]
    )
    job = {"logger": DummyLogger(), "progress_text": ""}

    ok, msg = ffmpeg_utils.make_gif_multi_inputs(
        str(video),
        [{"start": 0.2, "end": 1.2}],
        str(out_gif),
        {"fps": 15, "height": 90, "loop_forever": True, "smooth": smooth},
        job,
        background_image=str(background),
    )

    assert ok, msg
    red, green, blue = _first_frame_rgb(out_gif)
    assert red > 200
    assert green < 50
    assert blue < 50


def test_first_video_stream_index_skips_attached_picture(monkeypatch):
    data = {
        "streams": [
            {"index": 0, "disposition": {"attached_pic": 1}},
            {"index": 2, "disposition": {}},
        ]
    }

    def fake_check_output(cmd, text=True):
        return json.dumps(data)

    monkeypatch.setattr(ffmpeg_utils.subprocess, "check_output", fake_check_output)
    logger = DummyLogger()

    idx = ffmpeg_utils._first_video_stream_index("input.mp4", logger)

    assert idx == 1
    assert logger.info_msgs


def test_get_duration_timeout_returns_error(monkeypatch):
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs.get("timeout"))

    monkeypatch.setattr(ffmpeg_utils.subprocess, "run", fake_run)

    duration, err = ffmpeg_utils.get_duration("input.mp4")

    assert duration is None
    assert "timed out" in err
