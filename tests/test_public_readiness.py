import logging
from pathlib import Path

from app import jobs, routes


ROOT = Path(__file__).resolve().parents[1]


def _make_job(job_id="job1", status="queued", log_path="/tmp/job.txt"):
    return {
        "id": job_id,
        "video": "/library/video<script>.mp4",
        "out_gif": "/library/poster.gif",
        "tmp_dir": "/tmp/job",
        "status": status,
        "cfg": {},
        "log_path": log_path,
        "progress_text": "<progress>",
        "logger": logging.getLogger(job_id),
    }


def _clear_jobs():
    jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()


def test_api_status_returns_public_job_payload_only():
    _clear_jobs()
    jobs.jobs["job1"] = _make_job()

    client = routes.app.test_client()
    res = client.get("/api/status")

    assert res.status_code == 200
    payload = res.get_json()
    assert payload[0]["id"] == "job1"
    assert payload[0]["video"] == "/library/video<script>.mp4"
    assert payload[0]["out_gif"] == "/library/poster.gif"
    assert payload[0]["status"] == "queued"
    assert payload[0]["progress_text"] == "Waiting"
    assert payload[0]["progress_label"] == "Waiting"
    assert payload[0]["progress_percent"] == 0
    assert payload[0]["elapsed_seconds"] is None
    assert payload[0]["eta_seconds"] is None
    assert payload[0]["output_size_bytes"] is None
    assert payload[0]["started_at"] is None
    assert payload[0]["finished_at"] is None
    assert "logger" not in payload[0]
    assert "log_path" not in payload[0]
    assert "cfg" not in payload[0]
    _clear_jobs()


def test_queue_status_returns_public_payloads():
    _clear_jobs()
    jobs.jobs["job1"] = _make_job(status="queued")
    jobs.job_queue.put("job1")

    client = routes.app.test_client()
    res = client.get("/api/queue/status")

    payload = res.get_json()
    assert payload["queued"][0]["id"] == "job1"
    assert payload["total_active_items"] == 1
    assert payload["completed_active_items"] == 0
    assert payload["queue_progress_percent"] == 0
    assert payload["queue_progress_label"] == "0% complete"
    assert payload["summary"]["total_active_items"] == 1
    assert "logger" not in payload["queued"][0]
    assert jobs.emit_queue_status() == payload
    _clear_jobs()


def test_queue_status_reports_overall_batch_progress():
    _clear_jobs()
    jobs.jobs["done"] = _make_job(job_id="done", status="success")
    jobs.jobs["done"].update(
        {
            "batch_id": "batch1",
            "progress_percent": 100,
            "elapsed_seconds": 10,
            "_started_ts": 100,
            "_finished_ts": 110,
        }
    )
    jobs.jobs["run"] = _make_job(job_id="run", status="running")
    jobs.jobs["run"].update(
        {
            "batch_id": "batch1",
            "progress_percent": 50,
            "elapsed_seconds": 5,
            "_started_ts": 110,
        }
    )
    jobs.jobs["queued"] = _make_job(job_id="queued", status="queued")
    jobs.jobs["queued"].update({"batch_id": "batch1", "_created_ts": 111})
    jobs.job_queue.put("queued")

    client = routes.app.test_client()
    res = client.get("/api/queue/status")

    payload = res.get_json()
    assert payload["total_active_items"] == 3
    assert payload["completed_active_items"] == 1
    assert payload["queue_progress_percent"] == 50
    assert payload["queue_eta_seconds"] == 15
    assert payload["summary"]["queue_progress_percent"] == 50
    _clear_jobs()


def test_listdir_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    (sibling / "nested").mkdir(parents=True)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    client = routes.app.test_client()
    res = client.get("/api/listdir", query_string={"path": str(sibling)})

    assert res.status_code == 200
    assert res.get_json() == []


def test_api_add_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    video = sibling / "video.mp4"
    video.write_text("x")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    client = routes.app.test_client()
    res = client.post("/api/add", data={"video": str(video)})

    assert res.status_code == 400


def test_logs_route_serves_plain_text(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("<script>alert(1)</script>", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/logs/job1")

    assert res.status_code == 200
    assert res.mimetype == "text/plain"
    assert res.get_data(as_text=True) == "<script>alert(1)</script>"
    _clear_jobs()


def test_api_logs_returns_initial_and_offset_chunks(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("first\nsecond\n", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/api/logs/job1")

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["lines"] == ["first", "second"]
    assert payload["reset"] is False
    assert payload["job"]["id"] == "job1"
    offset = payload["offset"]

    with log_path.open("a", encoding="utf-8") as f:
        f.write("third\n")

    res = client.get("/api/logs/job1", query_string={"offset": offset})
    payload = res.get_json()
    assert payload["lines"] == ["third"]
    assert payload["offset"] > offset
    _clear_jobs()


def test_api_logs_missing_job_returns_404():
    _clear_jobs()

    client = routes.app.test_client()
    res = client.get("/api/logs/missing")

    assert res.status_code == 404
    assert res.get_json()["error"] == "Not found"


def test_api_logs_resets_when_offset_exceeds_file_size(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("after-rotate\n", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/api/logs/job1", query_string={"offset": 9999})

    payload = res.get_json()
    assert payload["lines"] == ["after-rotate"]
    assert payload["reset"] is True
    assert payload["offset"] == log_path.stat().st_size
    _clear_jobs()


def test_templates_escape_dynamic_job_tables():
    queue_template = (ROOT / "app" / "templates" / "queue.html").read_text()
    completed_template = (ROOT / "app" / "templates" / "completed.html").read_text()

    assert "escapeHtml(j.video)" in queue_template
    assert "escapeHtml(j.progress_label" in queue_template
    assert "escapeHtml(j.video)" in completed_template
    assert "escapeHtml(j.out_gif)" in completed_template
    assert "escapeHtml(formatDuration(j.elapsed_seconds))" in completed_template
    assert "escapeHtml(formatSize(j.output_size_bytes))" in completed_template


def test_queue_page_uses_polling_instead_of_socketio():
    queue_template = (ROOT / "app" / "templates" / "queue.html").read_text()

    assert "socket.io" not in queue_template
    assert "const socket = io()" not in queue_template
    assert "queue_update" not in queue_template
    assert "fetch('/api/queue/status')" in queue_template
    assert "setInterval(refreshQueue, 1000)" in queue_template


def test_live_logs_tracks_last_job_result_instead_of_forcing_running():
    live_template = (ROOT / "app" / "templates" / "live.html").read_text()

    assert "setStatus('running')" not in live_template
    assert "EventSource" not in live_template
    assert "/api/stream" not in live_template
    assert "pollTimer" in live_template
    assert "fetch(`/api/logs/${encodeURIComponent(currentJob)}" in live_template
    assert "let lastJob" in live_template
    assert "newestFinishedJob(all)" in live_template
    assert "clearInterval(pollTimer)" in live_template
    assert 'class="pill idle">idle</span>' in live_template
    assert "jobProgressBar" in live_template
    assert "queueProgressBar" in live_template
    assert "speed=" not in live_template


def test_dockerfile_uses_gunicorn_wsgi_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY app ./app" in dockerfile
    assert '"gunicorn"' in dockerfile
    assert '"--threads", "8"' in dockerfile
    assert '"--graceful-timeout", "10"' in dockerfile
    assert '"app.wsgi:app"' in dockerfile
    assert "/app/main.py" not in dockerfile


def test_entrypoint_chowns_library_only_when_requested():
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()

    assert "CHOWN_LIBRARY" in entrypoint
    assert 'chown -R app:app /state' in entrypoint
    assert '[ "${CHOWN_LIBRARY:-0}" = "1" ]' in entrypoint
    assert "for dir in /library /state" not in entrypoint


def test_runtime_requirements_exclude_dev_tools():
    requirements = (ROOT / "requirements.txt").read_text()
    dev_requirements = (ROOT / "requirements-dev.txt").read_text()
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()

    assert "pytest" not in requirements
    assert "pip-audit" not in requirements
    assert "eventlet" not in requirements
    assert "flask-socketio" not in requirements
    assert "gunicorn==26.0.0" in requirements
    assert "werkzeug==3.1.8" in requirements
    assert "pytest==8.1.1" in dev_requirements
    assert "pip-audit==2.10.1" in dev_requirements
    assert "python -m pip_audit -r requirements.txt" in workflow
