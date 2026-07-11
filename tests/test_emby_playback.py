import json

import pytest

from app import emby_playback, routes


class FakeResponse:
    status = 200
    code = 200

    def __init__(self, payload):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return json.dumps(self.payload).encode("utf-8")


def _settings(**overrides):
    value = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret-token",
        "emby_path_mappings": [],
        "emby_playback_protection": True,
    }
    value.update(overrides)
    return value


@pytest.fixture(autouse=True)
def clear_cache():
    emby_playback.clear_cache()
    yield
    emby_playback.clear_cache()


def test_sessions_request_is_sanitized_and_paused_playback_is_active():
    captured = {}
    sessions = [
        {
            "Id": "session-secret",
            "UserName": "private-user",
            "DeviceName": "Living Room",
            "RemoteEndPoint": "192.0.2.10",
            "NowPlayingItem": {
                "Id": "movie-1",
                "Path": "/media/Movie.mkv",
                "MediaSources": [{"Path": "/media/Movie.mkv", "DirectStreamUrl": "/secret"}],
            },
            "PlayState": {"IsPaused": True, "MediaSource": {"Path": "/media/Movie.mkv"}},
        }
    ]

    def opener(request, timeout):
        captured.update(request=request, timeout=timeout)
        return FakeResponse(sessions)

    snapshot = emby_playback.load_snapshot(_settings(), opener=opener, force=True)
    public = emby_playback.public_snapshot(snapshot)

    assert snapshot["status"] == "active"
    assert snapshot["active_session_count"] == 1
    assert snapshot["active_item_count"] == 1
    assert snapshot["items"][0]["emby_item_id"] == "movie-1"
    assert captured["request"].full_url == "http://emby:8096/emby/Sessions"
    assert captured["request"].get_header("X-emby-token") == "secret-token"
    assert captured["timeout"] == 15
    assert "secret-token" not in captured["request"].full_url
    assert "items" not in public
    assert "private-user" not in str(public)
    assert "Living Room" not in str(public)
    assert "192.0.2.10" not in str(public)


def test_snapshot_cache_and_force_refresh():
    calls = 0

    def opener(request, timeout):
        nonlocal calls
        calls += 1
        return FakeResponse([])

    emby_playback.load_snapshot(_settings(), opener=opener, now=10)
    emby_playback.load_snapshot(_settings(), opener=opener, now=11)
    assert calls == 1
    emby_playback.load_snapshot(_settings(), opener=opener, now=11, force=True)
    assert calls == 2


def test_targets_match_item_id_and_exact_mapped_paths():
    sessions = [
        {"NowPlayingItem": {"Id": "movie-1", "Path": "Z:\\Movies\\Movie.mkv"}, "PlayState": {}}
    ]
    settings = _settings(
        emby_path_mappings=[
            {"emby_prefix": "Z:\\Movies", "local_prefix": "/library/movies"}
        ]
    )
    result = emby_playback.check_targets(
        [
            {"id": "by-id", "emby_item_id": "movie-1", "local_path": "/other.mkv"},
            {"id": "by-path", "local_path": "/library/movies/Movie.mkv"},
            {"id": "clear", "emby_item_id": "other", "local_path": "/library/Other.mkv"},
        ],
        settings,
        opener=lambda request, timeout: FakeResponse(sessions),
        force=True,
    )

    assert emby_playback.target_status(result, "by-id") == "active"
    assert emby_playback.target_status(result, "by-path") == "active"
    assert emby_playback.target_status(result, "clear") == "clear"
    assert result["active_count"] == 2
    assert result["deferred_count"] == 2


def test_ambiguous_targets_are_unverified_only_when_playback_exists():
    settings = _settings(
        emby_path_mappings=[
            {"emby_prefix": "/one", "local_prefix": "/library"},
            {"emby_prefix": "/two", "local_prefix": "/library"},
        ]
    )
    active = [{"NowPlayingItem": {"Id": "other", "Path": "/elsewhere.mkv"}}]
    result = emby_playback.check_targets(
        [{"id": "target", "local_path": "/library/Movie.mkv"}],
        settings,
        opener=lambda request, timeout: FakeResponse(active),
        force=True,
    )
    assert emby_playback.target_status(result, "target") == "unverified"

    emby_playback.clear_cache()
    clear = emby_playback.check_targets(
        [{"id": "target", "local_path": "/library/Movie.mkv"}],
        settings,
        opener=lambda request, timeout: FakeResponse([]),
        force=True,
    )
    assert emby_playback.target_status(clear, "target") == "clear"


def test_unavailable_sessions_defer_targets_but_disabled_and_unconfigured_do_not():
    target = [{"id": "target", "local_path": "/library/Movie.mkv"}]
    unavailable = emby_playback.check_targets(
        target,
        _settings(),
        opener=lambda request, timeout: (_ for _ in ()).throw(OSError("secret-token")),
        force=True,
    )
    assert unavailable["status"] == "unavailable"
    assert emby_playback.target_status(unavailable, "target") == "unverified"
    assert "secret-token" not in str(emby_playback.public_result(unavailable))

    disabled = emby_playback.check_targets(target, _settings(emby_playback_protection=False))
    assert disabled["status"] == "disabled"
    assert emby_playback.target_status(disabled, "target") == "not_checked"
    assert disabled["deferred_count"] == 0

    unconfigured = emby_playback.check_targets(target, _settings(emby_api_key=""))
    assert unconfigured["status"] == "not_configured"
    assert emby_playback.target_status(unconfigured, "target") == "not_checked"
    assert unconfigured["deferred_count"] == 0


def test_malformed_sessions_are_unavailable():
    snapshot = emby_playback.load_snapshot(
        _settings(), opener=lambda request, timeout: FakeResponse({"Items": []}), force=True
    )
    assert snapshot["status"] == "unavailable"
    assert snapshot["active_session_count"] == 0


def test_group_status_defers_whole_group():
    targets = [
        {"id": "one", "group_id": "group", "emby_item_id": "active"},
        {"id": "two", "group_id": "group", "emby_item_id": "clear"},
    ]
    result = emby_playback.check_targets(
        targets,
        _settings(),
        opener=lambda request, timeout: FakeResponse(
            [{"NowPlayingItem": {"Id": "active", "Path": "/media/Active.mkv"}}]
        ),
        force=True,
    )
    assert emby_playback.group_status(result, targets, "group") == "active"


def test_playback_status_route_returns_only_public_summary(monkeypatch):
    snapshot = {
        "status": "active",
        "checked_at": "2026-07-11T00:00:00Z",
        "active_session_count": 1,
        "active_item_count": 1,
        "message": "Active",
        "items": [{"key": "private", "emby_item_id": "movie", "paths": ["/private"]}],
    }
    monkeypatch.setattr(routes.emby_playback, "load_snapshot", lambda force=True: snapshot)

    response = routes.app.test_client().get("/api/emby/playback")
    payload = response.get_json()["playback"]

    assert response.status_code == 200
    assert payload["status"] == "active"
    assert payload["active_session_count"] == 1
    assert "items" not in payload
    assert "private" not in response.get_data(as_text=True)
