import os
import sys
from urllib.parse import parse_qs, urlparse

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)

from app import jobs
from app.routes import app


def _clear_queue_state():
    jobs.jobs.clear()
    jobs.queue_paused.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()


def _location_parts(response):
    assert response.status_code == 302
    return urlparse(response.headers["Location"])


def test_gifs_workspace_renders_sections():
    client = app.test_client()

    res = client.get("/gifs")
    html = res.get_data(as_text=True)

    assert res.status_code == 200
    assert 'id="pane-new"' in html
    assert 'id="pane-test"' in html
    assert 'id="pane-queue"' in html
    assert 'id="pane-completed"' in html
    assert 'id="pane-logs"' in html
    assert 'src="/static/gifs.js"' in html


def test_legacy_page_routes_redirect_to_workspace_tabs():
    client = app.test_client()

    redirects = {
        "/": "new",
        "/queue": "queue",
        "/completed": "completed",
        "/live": "logs",
    }
    for path, fragment in redirects.items():
        parts = _location_parts(client.get(path))
        assert parts.path == "/gifs"
        assert parts.fragment == fragment


def test_queue_legacy_redirect_preserves_limit():
    client = app.test_client()

    parts = _location_parts(client.get("/queue?limit=25"))

    assert parts.path == "/gifs"
    assert parts.fragment == "queue"
    assert parse_qs(parts.query)["limit"] == ["25"]


def test_queue_actions_redirect_to_queue_workspace_tab():
    _clear_queue_state()
    client = app.test_client()

    try:
        for action in ("start", "pause", "stop"):
            parts = _location_parts(client.post(f"/api/queue/{action}?limit=25"))
            assert parts.path == "/gifs"
            assert parts.fragment == "queue"
            assert parse_qs(parts.query)["limit"] == ["25"]

        parts = _location_parts(client.post("/api/queue/move/missing/up?limit=50"))
        assert parts.path == "/gifs"
        assert parts.fragment == "queue"
        assert parse_qs(parts.query)["limit"] == ["50"]
    finally:
        _clear_queue_state()
