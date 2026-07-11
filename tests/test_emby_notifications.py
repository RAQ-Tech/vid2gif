import io
import json
import os
import urllib.error

import pytest

from app import emby_notifications, routes


class FakeResponse:
    def __init__(self, status=204):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return b""


def settings(**updates):
    value = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret-token",
        "emby_admin_notifications": "warnings",
    }
    value.update(updates)
    return value


@pytest.fixture(autouse=True)
def notification_root(monkeypatch, tmp_path):
    monkeypatch.setattr(emby_notifications, "NOTIFICATION_ROOT", str(tmp_path / "notifications"))


def test_notification_uses_header_query_fields_and_json_body_without_secrets():
    captured = {}

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse()

    result = emby_notifications.send_test(settings(), opener=opener)
    request = captured["request"]
    assert result["status"] == "success"
    assert request.method == "POST"
    assert request.get_header("X-emby-token") == "secret-token"
    assert "Name=vid2gif+notification+test" in request.full_url
    assert "Description=" in request.full_url
    assert "secret-token" not in request.full_url
    assert json.loads(request.data) == {"DisplayDateTime": True}


def test_warning_policy_skips_success_and_sends_aggregate_warning_once():
    calls = []
    opener = lambda request, timeout: (calls.append(request) or FakeResponse())
    skipped = emby_notifications.notify_maintenance(
        "Duplicate cleanup", "success", status="success", attempted_count=2, succeeded_count=2, settings=settings(), opener=opener
    )
    assert skipped["status"] == "skipped"
    assert not calls
    first = emby_notifications.notify_maintenance(
        "Duplicate cleanup", "warning", status="success", attempted_count=2, succeeded_count=1, deferred_count=1, reclaimed_bytes=123, emby_sync={"status": "partial"}, settings=settings(), opener=opener
    )
    second = emby_notifications.notify_maintenance(
        "Duplicate cleanup", "warning", status="success", attempted_count=2, succeeded_count=1, deferred_count=1, settings=settings(), opener=opener
    )
    assert first == second
    assert first["status"] == "success"
    assert len(calls) == 1
    url = calls[0].full_url
    assert "deferred+1" in url
    assert "reclaimed+bytes+123" in url
    assert "Emby+sync+partial" in url
    assert "/library/" not in url


def test_all_policy_sends_success_but_zero_cancel_and_off_do_not():
    calls = []
    opener = lambda request, timeout: (calls.append(request) or FakeResponse())
    sent = emby_notifications.notify_maintenance(
        "Poster updates", "all", status="success", attempted_count=1, succeeded_count=1, settings=settings(emby_admin_notifications="all"), opener=opener
    )
    zero = emby_notifications.notify_maintenance(
        "Poster updates", "zero", status="success", attempted_count=0, settings=settings(emby_admin_notifications="all"), opener=opener
    )
    cancelled = emby_notifications.notify_maintenance(
        "Poster updates", "cancel", status="cancelled", attempted_count=1, settings=settings(emby_admin_notifications="all"), opener=opener
    )
    disabled = emby_notifications.notify_maintenance(
        "Poster updates", "off", status="failed", attempted_count=1, failed_count=1, settings=settings(emby_admin_notifications="off"), opener=opener
    )
    assert sent["status"] == "success"
    assert zero["status"] == cancelled["status"] == "skipped"
    assert disabled["status"] == "disabled"
    assert len(calls) == 1


def test_pending_is_persisted_before_send_and_failure_is_not_retried():
    seen_pending = []

    def failing(request, timeout):
        files = os.listdir(emby_notifications.NOTIFICATION_ROOT)
        seen_pending.append(json.load(open(os.path.join(emby_notifications.NOTIFICATION_ROOT, files[0]), encoding="utf-8"))["status"])
        raise urllib.error.HTTPError(request.full_url, 500, "failed", {}, io.BytesIO())

    first = emby_notifications.notify_maintenance(
        "Subtitle cleanup", "failed", status="failed", attempted_count=1, failed_count=1, settings=settings(), opener=failing
    )
    second = emby_notifications.notify_maintenance(
        "Subtitle cleanup", "failed", status="failed", attempted_count=1, failed_count=1, settings=settings(), opener=lambda *_args, **_kwargs: pytest.fail("must not retry")
    )
    assert seen_pending == ["pending"]
    assert first == second
    assert first["status"] == "failed"
    assert "secret-token" not in str(first)


def test_ledger_failure_returns_redacted_failure_instead_of_raising(monkeypatch):
    monkeypatch.setattr(emby_notifications, "_write", lambda _value: (_ for _ in ()).throw(OSError("disk failed secret-token")))
    result = emby_notifications.notify_maintenance(
        "BIF cleanup",
        "ledger-failure",
        status="failed",
        attempted_count=1,
        failed_count=1,
        settings=settings(),
        opener=lambda *_args, **_kwargs: pytest.fail("delivery must not run without the idempotency record"),
    )
    assert result["status"] == "failed"
    assert "secret-token" not in str(result)


def test_retention_removes_old_and_excess_records(monkeypatch):
    os.makedirs(emby_notifications.NOTIFICATION_ROOT)
    for index in range(4):
        value = {"id": str(index), "created_at": f"2026-01-0{index + 1}T00:00:00+00:00"}
        with open(os.path.join(emby_notifications.NOTIFICATION_ROOT, f"{index}.json"), "w", encoding="utf-8") as handle:
            json.dump(value, handle)
    monkeypatch.setattr(emby_notifications, "RETENTION_COUNT", 2)
    emby_notifications._prune(now=1767484800)
    assert sorted(os.listdir(emby_notifications.NOTIFICATION_ROOT)) == ["2.json", "3.json"]


def test_notification_test_route_returns_public_result(monkeypatch):
    monkeypatch.setattr(
        routes.emby_notifications,
        "send_test",
        lambda: {"id": "notice", "status": "success", "message": "accepted", "created_at": "now", "sent_at": "now"},
    )
    response = routes.app.test_client().post("/api/emby/notifications/test")
    assert response.status_code == 200
    assert response.get_json()["emby_notification"]["id"] == "notice"
    assert "secret" not in str(response.get_json())
