import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)

from app import routes

app = routes.app


def test_api_add_accepts_original_fps(monkeypatch):
    captured = {}

    def fake_resolve(path):
        return path

    def fake_enqueue(video, cfg):
        captured["video"] = video
        captured["cfg"] = cfg

    monkeypatch.setattr(routes, "resolve_case_insensitive", fake_resolve)
    monkeypatch.setattr(routes, "enqueue_job", fake_enqueue)
    monkeypatch.setattr(os.path, "isdir", lambda p: False)

    client = app.test_client()
    data = {
        "video": "/library/video.mp4",
        "height_preset": "480",
        "fps_preset": "15",
        "fps_original": "on",
        "clip_len_preset": "2",
    }

    res = client.post("/api/add", data=data)
    assert res.status_code == 302
    assert captured["cfg"]["fps"] == "original"

