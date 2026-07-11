import io
import json
import os
import urllib.error

import pytest

from app import emby_operations, routes


class FakeResponse:
    def __init__(self, data=None, status=200):
        self.data = data
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        if self.data is None:
            return b""
        return json.dumps(self.data).encode("utf-8")


def settings(**updates):
    value = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret-token",
        "emby_path_mappings": [],
    }
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def reset_cache():
    emby_operations.clear_cache()
    yield
    emby_operations.clear_cache()


def task_data(state="Idle"):
    return [
        {
            "Id": "other",
            "Name": "Refresh Guide",
            "Category": "Live TV",
            "State": "Idle",
            "CurrentProgressPercentage": -5,
        },
        {
            "Id": "thumb/id",
            "Name": "Thumbnail Image Extraction",
            "Key": "ExtractChapterImages",
            "Description": "Creates thumbnails",
            "Category": "Library",
            "State": state,
            "CurrentProgressPercentage": 140,
            "Triggers": [{"Type": "DailyTrigger", "TimeOfDayTicks": 123, "Private": "drop"}],
            "LastExecutionResult": {
                "Status": "Failed",
                "StartTimeUtc": "2026-01-01T00:00:00Z",
                "EndTimeUtc": "2026-01-01T00:01:00Z",
                "ErrorMessage": "failed with secret-token at http://emby:8096/private " + ("x" * 500),
                "LongErrorMessage": "private stack and path",
            },
        },
    ]


def test_inventory_is_sanitized_cached_and_marks_only_thumbnail_controllable():
    captured = []

    def opener(request, timeout):
        captured.append((request, timeout))
        return FakeResponse(task_data())

    first = emby_operations.load_tasks(settings(), opener=opener, now=10)
    second = emby_operations.load_tasks(settings(), opener=opener, now=11)

    assert first == second
    assert len(captured) == 1
    request = captured[0][0]
    assert request.full_url == "http://emby:8096/emby/ScheduledTasks?IsHidden=false"
    assert request.get_header("X-emby-token") == "secret-token"
    assert first["status"] == "ready"
    assert first["thumbnail_task_id"] == "thumb/id"
    assert first["running_count"] == 0
    assert first["failed_count"] == 1
    assert first["tasks"][0]["progress_percent"] == 0
    thumb = first["tasks"][1]
    assert thumb["progress_percent"] == 100
    assert thumb["can_start"] is True
    assert thumb["can_cancel"] is False
    assert thumb["triggers"] == [{"Type": "DailyTrigger", "TimeOfDayTicks": 123}]
    assert "secret-token" not in str(first)
    assert "http://emby:8096" not in str(first)
    assert "LongErrorMessage" not in str(first)
    assert len(thumb["last_result"]["error_message"]) <= emby_operations.ERROR_LIMIT


def test_ambiguous_thumbnail_matches_are_read_only():
    data = task_data() + [{"Id": "second", "Name": "Video Preview Thumbnail Extraction", "State": "Idle"}]
    result = emby_operations.load_tasks(settings(), opener=lambda *_args, **_kwargs: FakeResponse(data), force=True)
    assert result["thumbnail_match_count"] == 2
    assert result["thumbnail_task_id"] == ""
    assert not any(task["can_start"] or task["can_cancel"] for task in result["tasks"])


def test_start_and_cancel_require_fresh_exact_thumbnail_state_and_encode_id():
    captured = []

    def start_opener(request, timeout):
        captured.append(request)
        return FakeResponse(task_data("Idle") if request.method == "GET" else None, status=204)

    accepted, error = emby_operations.start_task("thumb/id", settings(), opener=start_opener)
    assert error is None
    assert accepted["status"] == "accepted"
    assert captured[-1].full_url == "http://emby:8096/emby/ScheduledTasks/Running/thumb%2Fid"
    assert captured[-1].method == "POST"

    captured.clear()

    def cancel_opener(request, timeout):
        captured.append(request)
        return FakeResponse(task_data("Running") if request.method == "GET" else None, status=204)

    accepted, error = emby_operations.cancel_task("thumb/id", settings(), opener=cancel_opener)
    assert error is None
    assert accepted["status"] == "accepted"
    assert captured[-1].full_url.endswith("/ScheduledTasks/Running/thumb%2Fid/Delete")


def test_control_rejects_unrelated_missing_and_active_tasks_without_posting():
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse(task_data("Running"))

    payload, error = emby_operations.start_task("other", settings(), opener=opener)
    assert error == "forbidden"
    payload, error = emby_operations.start_task("missing", settings(), opener=opener)
    assert error == "not_found"
    payload, error = emby_operations.start_task("thumb/id", settings(), opener=opener)
    assert error == "conflict"
    assert all(request.method == "GET" for request in requests)


def test_activity_filters_task_entries_and_drops_identity_and_overview():
    activity = {
        "Items": [
            {"Name": "Thumbnail Image Extraction completed", "Type": "ScheduledTaskCompleted", "Date": "2026-01-01", "Severity": "Info", "Overview": "private path", "UserId": "user", "ItemId": "item"},
            {"Name": "Someone logged in", "Type": "AuthenticationSucceeded", "UserId": "private"},
        ],
        "TotalRecordCount": 2,
    }

    def opener(request, timeout):
        if "/ActivityLog/" in request.full_url:
            return FakeResponse(activity)
        return FakeResponse(task_data())

    result = emby_operations.load_activity(settings(), opener=opener)
    assert result["status"] == "ready"
    assert result["entries"] == [{"name": "Thumbnail Image Extraction completed", "type": "ScheduledTaskCompleted", "date": "2026-01-01", "severity": "Info"}]
    assert "private" not in str(result)
    assert "UserId" not in str(result)


def test_configuration_malformed_and_permission_failures_are_clean():
    assert emby_operations.load_tasks(settings(emby_api_key=""))["status"] == "not_configured"
    invalid = emby_operations.load_tasks(settings(), opener=lambda *_args, **_kwargs: FakeResponse({}), force=True)
    assert invalid["status"] == "unavailable"

    def forbidden(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "forbidden", {}, io.BytesIO())

    denied = emby_operations.load_tasks(settings(), opener=forbidden, force=True)
    assert denied["status"] == "forbidden"
    assert "secret-token" not in str(denied)


def test_public_routes_do_not_expose_private_task_or_activity_fields(monkeypatch):
    monkeypatch.setattr(routes.emby_operations, "load_tasks", lambda force=False: {"status": "ready", "tasks": [{"id": "one", "name": "Task"}], "message": "ok"})
    monkeypatch.setattr(routes.emby_operations, "load_activity", lambda limit=20: {"status": "ready", "entries": [{"name": "Task", "type": "ScheduledTask", "date": None, "severity": "Info"}], "message": "ok"})
    client = routes.app.test_client()
    assert client.get("/api/emby/tasks").get_json()["tasks"][0] == {"id": "one", "name": "Task"}
    body = client.get("/api/emby/activity").get_json()
    assert "UserId" not in str(body)
    assert "Overview" not in str(body)


def test_task_control_routes_map_errors_and_ui_contains_polling_controls(monkeypatch):
    monkeypatch.setattr(routes.emby_operations, "start_task", lambda task_id: ({"status": "failed", "message": "busy"}, "conflict"))
    monkeypatch.setattr(routes.emby_operations, "cancel_task", lambda task_id: ({"status": "failed", "message": "read only"}, "forbidden"))
    client = routes.app.test_client()
    assert client.post("/api/emby/tasks/task/start").status_code == 409
    assert client.post("/api/emby/tasks/task/cancel").status_code == 403

    html = client.get("/maintenance").get_data(as_text=True)
    script_path = os.path.join(os.path.dirname(routes.app.root_path), "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()
    assert 'data-maint-tab-hash="emby-operations"' in html
    assert 'id="embyOpsTaskRows"' in html
    assert 'id="embyOpsActivityRows"' in html
    assert "fetch(`/api/emby/tasks${force ? '?force=1' : ''}`)" in script
    assert "runningCount ? 2000 : 30000" in script
    assert "document.hidden" in script
    assert "window.confirm('Ask Emby to cancel thumbnail extraction?')" in script
