import logging
from pathlib import Path

from app import jobs, routes, sockets


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
    assert payload == [
        {
            "id": "job1",
            "video": "/library/video<script>.mp4",
            "out_gif": "/library/poster.gif",
            "status": "queued",
            "progress_text": "<progress>",
        }
    ]
    assert "logger" not in payload[0]
    assert "log_path" not in payload[0]
    assert "cfg" not in payload[0]
    _clear_jobs()


def test_queue_status_and_socketio_emit_public_payloads(monkeypatch):
    _clear_jobs()
    jobs.jobs["job1"] = _make_job(status="queued")
    jobs.job_queue.put("job1")
    emitted = {}

    class DummySocketIO:
        server = True

        def emit(self, event, payload):
            emitted["event"] = event
            emitted["payload"] = payload

    monkeypatch.setattr(jobs, "socketio", DummySocketIO())

    client = routes.app.test_client()
    res = client.get("/api/queue/status")
    jobs.emit_queue_status()

    payload = res.get_json()
    assert payload["queued"][0]["id"] == "job1"
    assert "logger" not in payload["queued"][0]
    assert emitted["event"] == "queue_update"
    assert emitted["payload"]["queued"][0] == payload["queued"][0]
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


def test_socketio_cors_defaults_to_same_origin(monkeypatch):
    monkeypatch.delenv("SOCKETIO_CORS_ALLOWED_ORIGINS", raising=False)
    assert sockets._cors_allowed_origins() is None


def test_socketio_cors_allowlist_from_env(monkeypatch):
    monkeypatch.setenv(
        "SOCKETIO_CORS_ALLOWED_ORIGINS",
        "https://example.test, https://internal.test",
    )
    assert sockets._cors_allowed_origins() == [
        "https://example.test",
        "https://internal.test",
    ]


def test_templates_escape_dynamic_job_tables():
    queue_template = (ROOT / "app" / "templates" / "queue.html").read_text()
    completed_template = (ROOT / "app" / "templates" / "completed.html").read_text()

    assert "escapeHtml(j.video)" in queue_template
    assert "escapeHtml(j.progress_text)" in queue_template
    assert "escapeHtml(j.video)" in completed_template
    assert "escapeHtml(j.out_gif)" in completed_template


def test_live_logs_tracks_last_job_result_instead_of_forcing_running():
    live_template = (ROOT / "app" / "templates" / "live.html").read_text()

    assert "setStatus('running')" not in live_template
    assert "refreshStatus();" in live_template
    assert "let lastJob" in live_template
    assert "rememberStreamJob(e.data)" in live_template
    assert "newestFinishedJob(all)" in live_template
    assert 'class="pill idle">idle</span>' in live_template


def test_dockerfile_uses_package_module_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY app ./app" in dockerfile
    assert 'CMD ["python", "-u", "-m", "app.main"]' in dockerfile
    assert "/app/main.py" not in dockerfile


def test_runtime_requirements_exclude_dev_tools():
    requirements = (ROOT / "requirements.txt").read_text()
    dev_requirements = (ROOT / "requirements-dev.txt").read_text()
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text()

    assert "pytest" not in requirements
    assert "pip-audit" not in requirements
    assert "werkzeug==3.1.8" in requirements
    assert "pytest==8.1.1" in dev_requirements
    assert "pip-audit==2.10.1" in dev_requirements
    assert "python -m pip_audit -r requirements.txt" in workflow
