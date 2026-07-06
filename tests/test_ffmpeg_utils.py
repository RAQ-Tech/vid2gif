import subprocess
import json

from app import ffmpeg_utils


class DummyLogger:
    def __init__(self):
        self.info_msgs = []
        self.error_msgs = []

    def info(self, msg):
        pass
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
            self.stdout = ["line1", "last error"]
            self.returncode = 1

        def wait(self):
            return self.returncode

    monkeypatch.setattr(subprocess, "Popen", DummyPopen)

    ok, msg = ffmpeg_utils.make_gif_multi_inputs(video, segs, "out.gif", cfg, job)
    assert not ok
    assert "ffmpeg exited with code 1" in msg
    assert "last error" in msg
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
    concat_pos = filt.index("concat=n=3")
    assert filt.index("[0:v]scale=w=640:h=360") < concat_pos
    assert filt.index("[1:v:0]scale=w=640:h=360") < concat_pos
    assert filt.index("[2:v:0]scale=w=640:h=360") < concat_pos
    assert "force_original_aspect_ratio=decrease" in filt
    assert "pad=640:360:(ow-iw)/2:(oh-ih)/2" in filt
    assert args.count(str(background)) == 1
    assert not any(str(background) in arg and arg != str(background) for arg in args)


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
