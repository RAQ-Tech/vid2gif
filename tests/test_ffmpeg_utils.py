import subprocess

from app import ffmpeg_utils

class DummyLogger:
    def info(self, msg):
        pass


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
