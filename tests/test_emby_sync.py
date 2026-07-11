import datetime
import json
import os
import time
import urllib.error
import urllib.parse
from pathlib import Path

import pytest

from app import emby_sync, routes


class FakeResponse:
    def __init__(self, status=204):
        self.status = status
        self.code = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


def _settings(**overrides):
    value = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret-token",
        "emby_path_mappings": [],
        "emby_sync_after_maintenance": True,
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def isolate_sync_jobs(monkeypatch, tmp_path):
    monkeypatch.setattr(emby_sync, "SYNC_ROOT", str(tmp_path / "emby-sync"))
    emby_sync._active.clear()
    yield
    emby_sync._active.clear()


def test_item_refresh_uses_documented_parameters_body_and_header(monkeypatch, tmp_path):
    captured = {}
    cleared = []
    media = tmp_path / "Movie.mkv"
    media.write_bytes(b"video")

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: cleared.append(True))
    result = emby_sync.sync_changes(
        [
            {
                "local_path": str(media),
                "update_type": "Modified",
                "emby_item_id": "movie/1",
                "refresh_scope": "image",
            }
        ],
        workflow="posters",
        run_id="run-1",
        settings=_settings(),
        opener=opener,
    )

    request = captured["request"]
    parsed = urllib.parse.urlsplit(request.full_url)
    query = urllib.parse.parse_qs(parsed.query)
    assert result["status"] == "success"
    assert result["item_refresh_count"] == 1
    assert result["path_notification_count"] == 0
    assert parsed.path == "/emby/Items/movie%2F1/Refresh"
    assert query == {
        "Recursive": ["false"],
        "MetadataRefreshMode": ["ValidationOnly"],
        "ImageRefreshMode": ["Default"],
        "ReplaceAllMetadata": ["false"],
        "ReplaceAllImages": ["false"],
    }
    assert request.method == "POST"
    assert json.loads(request.data) == {"ReplaceThumbnailImages": False}
    assert request.get_header("Content-type") == "application/json"
    assert request.get_header("X-emby-token") == "secret-token"
    assert captured["timeout"] == 15
    assert "secret-token" not in request.full_url
    assert "secret-token" not in str(result)
    assert cleared == [True]
    stored = emby_sync.get_sync(result["id"])
    assert stored["changes"][0]["status"] == "accepted"
    assert "secret-token" not in json.dumps(stored)


def test_failed_item_refresh_falls_back_to_mapped_path(monkeypatch, tmp_path):
    media = tmp_path / "library" / "Movie" / "Movie.mkv"
    media.parent.mkdir(parents=True)
    media.write_bytes(b"video")
    requests = []

    def opener(request, timeout):
        requests.append(request)
        if "/Items/" in request.full_url:
            raise urllib.error.HTTPError(request.full_url, 500, "failed", {}, None)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: None)
    result = emby_sync.sync_changes(
        [
            {
                "local_path": str(media),
                "update_type": "Modified",
                "emby_item_id": "movie-1",
                "refresh_scope": "metadata",
            }
        ],
        settings=_settings(
            emby_path_mappings=[
                {"emby_prefix": "/media", "local_prefix": str(tmp_path / "library")}
            ]
        ),
        opener=opener,
    )

    assert result["status"] == "success"
    assert result["item_refresh_count"] == 0
    assert result["path_notification_count"] == 1
    assert len(requests) == 2
    assert requests[1].full_url == "http://emby:8096/emby/Library/Media/Updated"
    assert json.loads(requests[1].data) == {
        "Updates": [{"Path": "/media/Movie/Movie.mkv", "UpdateType": "Modified"}]
    }


def test_path_notifications_are_deduplicated_and_batched_by_one_hundred(monkeypatch, tmp_path):
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: None)
    changes = [
        {
            "local_path": str(tmp_path / "library" / f"Movie-{index}.mkv"),
            "update_type": "Created",
            "prefer_path": True,
        }
        for index in range(205)
    ]
    changes.append(dict(changes[0]))
    result = emby_sync.sync_changes(changes, settings=_settings(), opener=opener)

    assert result["status"] == "success"
    assert result["succeeded_count"] == 205
    assert result["path_notification_count"] == 205
    assert [len(json.loads(request.data)["Updates"]) for request in requests] == [100, 100, 5]


def test_item_ids_are_deduplicated_while_each_change_is_recorded(monkeypatch, tmp_path):
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: None)
    first = {
        "local_path": str(tmp_path / "one.mkv"),
        "update_type": "Modified",
        "emby_item_id": "same-id",
        "refresh_scope": "media",
    }
    second = {
        **first,
        "local_path": str(tmp_path / "two.mkv"),
        "refresh_scope": "image",
    }
    result = emby_sync.sync_changes([first, dict(first), second], settings=_settings(), opener=opener)

    assert result["status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["item_refresh_count"] == 1
    assert len(requests) == 1
    assert "ImageRefreshMode=Default" in requests[0].full_url
    assert len(emby_sync.get_sync(result["id"])["changes"]) == 2


def test_same_path_changes_share_one_notification(monkeypatch, tmp_path):
    requests = []

    def opener(request, timeout):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: None)
    path = str(tmp_path / "Movie.mkv")
    result = emby_sync.sync_changes(
        [
            {"local_path": path, "update_type": "Created", "prefer_path": True},
            {"local_path": path, "update_type": "Modified", "prefer_path": True},
        ],
        settings=_settings(),
        opener=opener,
    )

    assert result["status"] == "success"
    assert result["succeeded_count"] == 2
    assert result["path_notification_count"] == 2
    assert len(requests) == 1
    assert json.loads(requests[0].data)["Updates"] == [
        {"Path": os.path.realpath(path), "UpdateType": "Created"}
    ]


def test_longest_local_mapping_wins_and_equal_matches_are_ambiguous(tmp_path):
    media = str(tmp_path / "library" / "movies" / "Film.mkv")
    mappings = [
        {"local_prefix": str(tmp_path / "library"), "emby_prefix": "/media"},
        {"local_prefix": str(tmp_path / "library" / "movies"), "emby_prefix": "/films"},
    ]
    assert emby_sync.emby_paths_for_local(media, mappings) == ["/films/Film.mkv"]

    ambiguous = mappings + [
        {"local_prefix": str(tmp_path / "library" / "movies"), "emby_prefix": "/other"}
    ]
    assert emby_sync.emby_paths_for_local(media, ambiguous) == [
        "/films/Film.mkv",
        "/other/Film.mkv",
    ]


def test_ambiguous_paths_are_unresolved_without_a_request(tmp_path):
    calls = []
    media = str(tmp_path / "library" / "Film.mkv")
    mappings = [
        {"local_prefix": str(tmp_path / "library"), "emby_prefix": "/one"},
        {"local_prefix": str(tmp_path / "library"), "emby_prefix": "/two"},
    ]
    result = emby_sync.sync_changes(
        [{"local_path": media, "update_type": "Deleted", "prefer_path": True}],
        settings=_settings(emby_path_mappings=mappings),
        opener=lambda request, timeout: calls.append(request),
    )

    assert result["status"] == "failed"
    assert result["unresolved_count"] == 1
    assert result["retryable"] is True
    assert calls == []


def test_unexpected_sync_errors_become_retryable_results(monkeypatch, tmp_path):
    monkeypatch.setattr(
        emby_sync,
        "_item_refresh",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("secret-token")),
    )
    result = emby_sync.sync_changes(
        [
            {
                "local_path": str(tmp_path / "Movie.mkv"),
                "update_type": "Modified",
                "emby_item_id": "movie-1",
            }
        ],
        settings=_settings(),
    )

    assert result["status"] == "failed"
    assert result["failed_count"] == 1
    assert result["retryable"] is True
    assert "secret-token" not in str(result)
    assert emby_sync.get_sync(result["id"])["status"] == "failed"


def test_job_persistence_failure_does_not_escape_to_local_workflow(monkeypatch, tmp_path):
    monkeypatch.setattr(
        emby_sync,
        "_save_job",
        lambda job: (_ for _ in ()).throw(OSError("state unavailable")),
    )

    result = emby_sync.sync_changes(
        [{"local_path": str(tmp_path / "Movie.mkv"), "update_type": "Modified"}],
        settings=_settings(),
    )

    assert result["status"] == "failed"
    assert result["failed_count"] == 1
    assert result["retryable"] is True


@pytest.mark.parametrize(
    ("settings", "status", "retryable"),
    [
        (_settings(emby_sync_after_maintenance=False), "disabled", False),
        (_settings(emby_api_key=""), "not_configured", True),
    ],
)
def test_disabled_and_unconfigured_sync_do_not_send_requests(tmp_path, settings, status, retryable):
    calls = []
    result = emby_sync.sync_changes(
        [{"local_path": str(tmp_path / "Movie.mkv"), "update_type": "Modified"}],
        settings=settings,
        opener=lambda request, timeout: calls.append(request),
    )

    assert result["status"] == status
    assert result["retryable"] is retryable
    assert calls == []


def test_retry_reloads_current_settings_and_only_retries_failed_targets(monkeypatch, tmp_path):
    media = tmp_path / "library" / "Movie.mkv"
    current = _settings(
        emby_path_mappings=[{"local_prefix": str(tmp_path / "library"), "emby_prefix": "/new"}]
    )

    def fail(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 503, "offline", {}, None)

    initial = emby_sync.sync_changes(
        [{"local_path": str(media), "update_type": "Deleted", "prefer_path": True}],
        settings=_settings(),
        opener=fail,
    )
    requests = []

    def succeed(request, timeout):
        requests.append(request)
        return FakeResponse()

    monkeypatch.setattr(emby_sync.app_settings, "load_settings", lambda: current)
    monkeypatch.setattr(emby_sync.emby_catalog, "clear_cache", lambda: None)
    queued, err = emby_sync.start_retry(initial["id"], opener=succeed)
    assert err is None
    assert queued["status"] in {"queued", "running", "success"}

    deadline = time.time() + 3
    while time.time() < deadline:
        restored = emby_sync.public_sync(emby_sync.get_sync(initial["id"]))
        if restored["status"] not in {"queued", "running"}:
            break
        time.sleep(0.01)

    assert restored["status"] == "success"
    assert len(requests) == 1
    assert json.loads(requests[0].data)["Updates"][0]["Path"] == "/new/Movie.mkv"
    deadline = time.time() + 3
    while initial["id"] in emby_sync._active and time.time() < deadline:
        time.sleep(0.01)
    _queued, second_err = emby_sync.start_retry(initial["id"], opener=succeed)
    assert second_err == "Synchronization job has nothing to retry"


def test_sync_status_and_retry_routes(monkeypatch):
    public = {"id": "sync-1", "status": "failed", "retryable": True}
    monkeypatch.setattr(routes.emby_sync, "get_sync", lambda sync_id: {"id": sync_id})
    monkeypatch.setattr(routes.emby_sync, "public_sync", lambda job: public)
    monkeypatch.setattr(routes.emby_sync, "start_retry", lambda sync_id: ({**public, "status": "queued"}, None))

    client = routes.app.test_client()
    assert client.get("/api/emby/sync/sync-1").get_json()["emby_sync"] == public
    retry = client.post("/api/emby/sync/sync-1/retry")
    assert retry.status_code == 202
    assert retry.get_json()["emby_sync"]["status"] == "queued"

    monkeypatch.setattr(
        routes.emby_sync,
        "start_retry",
        lambda sync_id: (None, "Synchronization retry is already running"),
    )
    assert client.post("/api/emby/sync/sync-1/retry").status_code == 409
    monkeypatch.setattr(routes.emby_sync, "get_sync", lambda sync_id: None)
    assert client.get("/api/emby/sync/missing").status_code == 404


def test_retention_removes_expired_jobs_and_keeps_at_most_one_hundred(tmp_path):
    os.makedirs(emby_sync.SYNC_ROOT, exist_ok=True)
    now = time.time()
    current = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    old = current - datetime.timedelta(days=31)
    for index in range(105):
        created = old if index == 0 else current - datetime.timedelta(seconds=index)
        job = {"id": f"job-{index}", "created_at": created.isoformat(), "changes": [], "result": {}}
        emby_sync._write_json_atomic(emby_sync._job_path(job["id"]), job)

    emby_sync._prune_jobs(now=now)

    files = list(Path(emby_sync.SYNC_ROOT).glob("*.json"))
    assert len(files) == 100
    assert not Path(emby_sync._job_path("job-0")).exists()


def test_production_code_contains_no_whole_library_refresh_request():
    source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    assert "/Library/Refresh" not in source
