import json
import os
import zipfile

from app import routes, system_status


class _Thread:
    def __init__(self, name):
        self.name = name

    def is_alive(self):
        return True


def test_status_payload_reports_runtime_storage_tools_and_workers(monkeypatch, tmp_path):
    library = tmp_path / "library"
    state = tmp_path / "state"
    library.mkdir()
    state.mkdir()
    monkeypatch.setattr(system_status, "LIB_ROOT", str(library))
    monkeypatch.setattr(system_status, "STATE_ROOT", str(state))
    monkeypatch.setattr(system_status, "STARTED_AT", 100.0)
    monkeypatch.setattr(
        system_status.threading,
        "enumerate",
        lambda: [_Thread(thread_name) for _label, thread_name in system_status.WORKER_THREADS.values()],
    )
    monkeypatch.setattr(system_status.shutil, "which", lambda command: f"/usr/bin/{command}")
    monkeypatch.setattr(
        system_status.dashboard,
        "status_payload",
        lambda: {"health": {"active_count": 2}},
    )

    payload = system_status.status_payload(now=160.0)

    assert payload["overall"] == "healthy"
    assert payload["healthy"] is True
    assert payload["uptime_label"] == "1m 0s"
    assert payload["active_work_count"] == 2
    assert {check["status"] for check in payload["checks"]} == {"pass"}
    assert {item["label"] for item in payload["storage"]} == {"Library", "State"}


def test_status_payload_marks_missing_requirements_unhealthy(monkeypatch, tmp_path):
    monkeypatch.setattr(system_status, "LIB_ROOT", str(tmp_path / "missing-library"))
    monkeypatch.setattr(system_status, "STATE_ROOT", str(tmp_path / "missing-state"))
    monkeypatch.setattr(system_status.threading, "enumerate", lambda: [])
    monkeypatch.setattr(system_status.shutil, "which", lambda _command: None)
    monkeypatch.setattr(system_status.dashboard, "status_payload", lambda: {})

    payload = system_status.status_payload()

    assert payload["overall"] == "unhealthy"
    assert payload["healthy"] is False
    assert payload["failed_count"] >= 7
    gifsicle = next(check for check in payload["checks"] if check["id"] == "gifsicle")
    assert gifsicle["status"] == "warn"


def test_create_state_backup_archives_files_and_manifest(tmp_path):
    state = tmp_path / "state"
    nested = state / "maintenance-logs"
    nested.mkdir(parents=True)
    (state / "app_settings.json").write_text('{"test":true}', encoding="utf-8")
    (nested / "run.jsonl").write_text('{"status":"success"}\n', encoding="utf-8")

    archive_path, backup = system_status.create_state_backup(str(state))
    try:
        assert backup["file_count"] == 2
        assert backup["total_bytes"] > 0
        with zipfile.ZipFile(archive_path) as archive:
            assert set(archive.namelist()) == {
                "state/app_settings.json",
                "state/maintenance-logs/run.jsonl",
                "vid2gif-backup.json",
            }
            manifest = json.loads(archive.read("vid2gif-backup.json"))
            assert manifest["source"] == "/state"
            assert manifest["file_count"] == 2
    finally:
        os.remove(archive_path)


def test_system_routes_render_status_health_and_backup(monkeypatch, tmp_path):
    payload = {
        "overall": "healthy",
        "healthy": True,
        "generated_at": "2026-07-10T12:00:00+00:00",
    }
    monkeypatch.setattr(routes.system_status, "status_payload", lambda: payload)
    archive = tmp_path / "backup.zip"
    archive.write_bytes(b"PK test backup")
    monkeypatch.setattr(
        routes.system_status,
        "create_state_backup",
        lambda: (
            str(archive),
            {
                "download_name": "vid2gif-state-test.zip",
                "file_count": 3,
                "total_bytes": 14,
            },
        ),
    )

    client = routes.app.test_client()
    page = client.get("/system")
    status = client.get("/api/system/status")
    health = client.get("/healthz")
    backup = client.post("/system/backup", buffered=True)

    assert page.status_code == 200
    assert "Runtime Checks" in page.get_data(as_text=True)
    assert "Download Backup" in page.get_data(as_text=True)
    assert status.get_json() == payload
    assert health.status_code == 200
    assert health.get_json()["status"] == "healthy"
    assert backup.status_code == 200
    assert backup.mimetype == "application/zip"
    assert "vid2gif-state-test.zip" in backup.headers["Content-Disposition"]
    assert backup.headers["X-vid2gif-Backup-Files"] == "3"
    backup.close()
    assert not archive.exists()


def test_health_route_returns_503_when_unhealthy(monkeypatch):
    monkeypatch.setattr(
        routes.system_status,
        "status_payload",
        lambda: {"overall": "unhealthy", "healthy": False, "generated_at": "now"},
    )

    response = routes.app.test_client().get("/healthz")

    assert response.status_code == 503
    assert response.get_json()["healthy"] is False


def test_maintenance_script_exposes_detailed_change_previews():
    script_path = os.path.join(os.path.dirname(routes.app.root_path), "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()

    assert "function renderChangePreview(options)" in script
    assert "File changes (" in script
    assert "This cannot be undone by vid2gif." in script
    assert "Local candidate files will remain unchanged." in script
