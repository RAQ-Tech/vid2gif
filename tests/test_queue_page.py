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
    jobs.queue_paused.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()


def _queue_body(html: str) -> str:
    start = html.index('id="queue-body"')
    end = html.index("</tbody>", start)
    return html[start:end]


def test_paused_queue_does_not_promise_a_finish_time():
    _clear_jobs()
    jobs.queue_paused.set()
    try:
        result = jobs._queue_summary([{"status": "queued", "expected_duration_seconds": 10}])
        assert result["queue_eta_seconds"] is None
        assert result["queue_eta_confidence"] == "paused"
    finally:
        _clear_jobs()


def test_queued_jobs_relearn_after_first_completion(monkeypatch):
    _clear_jobs()
    monkeypatch.setattr(jobs, "job_duration_estimate", lambda cfg: {"seconds": 25, "confidence": "learning"})
    result = jobs._queue_summary(
        [
            {"status": "success"},
            {"status": "queued", "cfg": {"height": 480}, "expected_duration_seconds": None},
            {"status": "queued", "cfg": {"height": 480}, "expected_duration_seconds": None},
        ]
    )
    assert result["queue_eta_seconds"] == 50


def test_rounding_progress_cannot_claim_queue_complete():
    _clear_jobs()
    result = jobs._queue_summary(
        [
            *({"status": "success"} for _ in range(200)),
            {"status": "running", "progress_percent": 99, "eta_seconds": None},
        ]
    )
    assert result["queue_progress_percent"] == 99
    assert not result["queue_progress_label"].startswith("Complete")


def test_running_job_display_and_lock():
    _clear_jobs()
    running = _make_job("run1", "running")
    queued = _make_job("queued1", "queued")
    jobs.jobs[running["id"]] = running
    jobs.jobs[queued["id"]] = queued
    jobs.job_queue.put(queued["id"])

    client = app.test_client()
    res = client.get("/gifs?limit=10")
    html = res.get_data(as_text=True)
    assert html.find(running["id"]) < html.find(queued["id"])
    assert f"/api/queue/move/{running['id']}/up" not in html
    assert f"/api/queue/move/{running['id']}/down" not in html

    jobs.jobs[running["id"]]["status"] = "success"
    res = client.get("/gifs?limit=10")
    html = _queue_body(res.get_data(as_text=True))
    assert running["id"] not in html

    _clear_jobs()
