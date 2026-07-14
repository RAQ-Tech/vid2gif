from app import operation_gate, routes


def test_activity_endpoint_exposes_shared_operation_state():
    operation_gate.library_gate.reset_for_tests()
    with operation_gate.library_operation(
        "scan:one",
        label="Scan library",
        state={"status": "running", "progress_percent": 37},
        href="/maintenance",
    ):
        response = routes.app.test_client().get("/api/activity")

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["active"] is True
    assert payload["current"]["label"] == "Scan library"
    assert payload["current"]["progress_percent"] == 37


def test_gif_cancel_route_returns_updated_job(monkeypatch):
    monkeypatch.setattr(
        routes,
        "cancel_job",
        lambda job_id: ({"id": job_id, "status": "cancelling"}, None),
    )

    response = routes.app.test_client().post("/api/jobs/job-7/cancel")

    assert response.status_code == 200
    assert response.get_json()["job"] == {"id": "job-7", "status": "cancelling"}


def test_test_lab_cancel_route_returns_not_found(monkeypatch):
    monkeypatch.setattr(
        routes.test_lab,
        "cancel_test_run",
        lambda run_id: (None, "Test run not found"),
    )

    response = routes.app.test_client().post("/api/test-lab/runs/missing/cancel")

    assert response.status_code == 404
    assert response.get_json()["error"] == "Test run not found"
