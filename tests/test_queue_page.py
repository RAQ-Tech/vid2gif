import os
import sys

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)

from app.routes import app
from app import jobs


def _make_job(job_id: str, status: str):
    return {
        "id": job_id,
        "video": "/library/video.mp4",
        "out_gif": "/tmp/out.gif",
        "tmp_dir": "/tmp",
        "status": status,
        "cfg": {},
        "log_path": "/tmp/log.txt",
        "progress_text": "",
        "logger": None,
    }


def _clear_jobs():
    jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()


def test_running_job_display_and_lock():
    _clear_jobs()
    running = _make_job("run1", "running")
    queued = _make_job("queued1", "queued")
    jobs.jobs[running["id"]] = running
    jobs.jobs[queued["id"]] = queued
    jobs.job_queue.put(queued["id"])

    client = app.test_client()
    res = client.get("/queue")
    html = res.get_data(as_text=True)
    assert html.find(running["id"]) < html.find(queued["id"])
    assert f"/api/queue/move/{running['id']}/up" not in html
    assert f"/api/queue/move/{running['id']}/down" not in html

    jobs.jobs[running["id"]]["status"] = "success"
    res = client.get("/queue")
    html = res.get_data(as_text=True)
    assert running["id"] not in html

    _clear_jobs()

