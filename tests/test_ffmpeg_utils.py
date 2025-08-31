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



def test_make_gif_multi_inputs_includes_input_flag(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}, {"start": 2.0, "end": 3.0}]
    cfg = {"fps": 10, "height": 320, "loop_forever": True}
    job = {"logger": DummyLogger(), "progress_text": ""}

    captured = {}

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


def test_make_gif_multi_inputs_logs_failure(monkeypatch):
    video = "input.mp4"
    segs = [{"start": 0.0, "end": 1.0}]
    cfg = {"fps": 10, "height": 320, "loop_forever": True}
    logger = DummyLogger()
    job = {"logger": logger, "progress_text": ""}

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
