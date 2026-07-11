import os
import urllib.error

from app import poster_maintenance, routes


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reset_poster_state(monkeypatch, tmp_path):
    state_root = tmp_path / "state" / "landscape-posters"
    state_root.mkdir(parents=True)
    monkeypatch.setattr(poster_maintenance, "LANDSCAPE_POSTER_ROOT", str(state_root))
    monkeypatch.setattr(
        poster_maintenance,
        "SETTINGS_PATH",
        str(state_root / "settings.json"),
    )
    monkeypatch.setattr(
        poster_maintenance,
        "MANIFEST_PATH",
        str(state_root / "manifest.json"),
    )
    monkeypatch.setattr(
        poster_maintenance,
        "EMBY_STATUS_PATH",
        str(state_root / "emby-status.json"),
    )
    poster_maintenance.poster_runs.clear()
    monkeypatch.setattr(poster_maintenance, "_current_run_id", "")
    poster_maintenance._scheduler_state.clear()
    poster_maintenance._scheduler_state.update(
        {"last_checked_at": None, "next_run_at": None, "last_error": ""}
    )
    def fake_dimensions(path, timeout=10):
        try:
            with open(path, "rb") as handle:
                data = handle.read()
        except OSError:
            return None
        landscape = b"landscape" in data or data == b"same image"
        return {
            "width": 1920 if landscape else 1000,
            "height": 1080 if landscape else 1500,
            "landscape": landscape,
        }
    monkeypatch.setattr(poster_maintenance, "_probe_image_dimensions", fake_dimensions)
    return state_root


def _settings(**overrides):
    settings = poster_maintenance.default_settings()
    settings.update(
        {
            "enabled": False,
            "emby_refresh_enabled": False,
            "emby_url": "",
            "emby_api_key": "",
        }
    )
    settings.update(overrides)
    return settings


def _run(lib, monkeypatch, tmp_path, mode="full", settings=None):
    _reset_poster_state(monkeypatch, tmp_path)
    run, err = poster_maintenance.start_landscape_poster_run(
        str(lib),
        mode=mode,
        synchronous=True,
        lib_root=str(lib),
        settings=settings or _settings(),
    )
    assert err is None
    assert run["status"] == "success"
    return run


def test_landscape_poster_run_replaces_existing_poster_and_preserves_backup(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    background = _write(movie / "Movie-background.jpg", b"landscape")
    poster = _write(movie / "Movie-poster.jpg", b"portrait")
    marker = _write(movie / ".posters_done", b"old marker")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["updated"] == 1
    assert poster.read_bytes() == background.read_bytes()
    assert (movie / "Movie-poster-backup.jpg").read_bytes() == b"portrait"
    assert marker.read_bytes() == b"old marker"


def test_landscape_poster_run_only_updates_existing_posters(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"landscape")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["missing_poster"] == 1
    assert not (movie / "Movie-poster.jpg").exists()
    assert not (movie / "Movie-poster-backup.jpg").exists()


def test_landscape_poster_run_never_overwrites_existing_backup(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"new landscape")
    poster = _write(movie / "Movie-poster.jpg", b"current poster")
    backup = _write(movie / "Movie-poster-backup.jpg", b"original backup")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["updated"] == 1
    assert poster.read_bytes() == b"new landscape"
    assert backup.read_bytes() == b"original backup"


def test_landscape_poster_run_skips_when_poster_already_matches(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"same image")
    _write(movie / "Movie-poster.jpg", b"same image")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["already_matching"] == 1
    assert not (movie / "Movie-poster-backup.jpg").exists()


def test_landscape_poster_run_skips_non_landscape_background(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"portrait background")
    poster = _write(movie / "Movie-poster.jpg", b"portrait poster")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["updated"] == 0
    assert run["counters"]["skipped"] == 1
    assert poster.read_bytes() == b"portrait poster"
    assert not (movie / "Movie-poster-backup.jpg").exists()


def test_landscape_poster_run_skips_existing_landscape_poster(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"new landscape")
    poster = _write(movie / "Movie-poster.jpg", b"old landscape")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["already_matching"] == 1
    assert poster.read_bytes() == b"old landscape"
    assert not (movie / "Movie-poster-backup.jpg").exists()


def test_landscape_poster_run_skips_ambiguous_backgrounds(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"first landscape")
    _write(movie / "Movie-background.png", b"second landscape")
    poster = _write(movie / "Movie-poster.jpg", b"portrait")

    run = _run(lib, monkeypatch, tmp_path)

    assert run["counters"]["updated"] == 0
    assert run["counters"]["skipped"] == 1
    assert poster.read_bytes() == b"portrait"


def test_landscape_poster_status_prunes_old_memory_runs(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    monkeypatch.setattr(poster_maintenance, "POSTER_RUN_RETENTION_COUNT", 1)
    poster_maintenance.poster_runs["old"] = {
        "id": "old",
        "status": "success",
        "created_at": "2026-01-01T00:00:00Z",
        "finished_at": "2026-01-01T00:00:00Z",
        "counters": {},
        "items": [],
    }
    poster_maintenance.poster_runs["new"] = {
        "id": "new",
        "status": "success",
        "created_at": "2026-01-02T00:00:00Z",
        "finished_at": "2026-01-02T00:00:00Z",
        "counters": {},
        "items": [],
    }

    payload = poster_maintenance.status_payload()

    assert payload["last_run"]["id"] == "new"
    assert list(poster_maintenance.poster_runs) == ["new"]


def test_landscape_poster_manifest_skips_unchanged_folders_incrementally(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"landscape")
    _write(movie / "Movie-poster.jpg", b"portrait")
    _reset_poster_state(monkeypatch, tmp_path)

    first, err = poster_maintenance.start_landscape_poster_run(
        str(lib),
        mode="full",
        synchronous=True,
        lib_root=str(lib),
        settings=_settings(),
    )
    second, err = poster_maintenance.start_landscape_poster_run(
        str(lib),
        mode="incremental",
        synchronous=True,
        lib_root=str(lib),
        settings=_settings(),
    )

    assert err is None
    assert first["counters"]["updated"] == 1
    assert second["counters"]["folders_skipped_unchanged"] == 1
    assert second["counters"]["candidates"] == 0


def test_emby_refresh_posts_to_library_refresh_endpoint():
    captured = {}

    class FakeResponse:
        status = 204

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["data"] = request.data
        captured["timeout"] = timeout
        return FakeResponse()

    result = poster_maintenance.refresh_emby(
        {
            "emby_refresh_enabled": True,
            "emby_url": "http://emby:8096",
            "emby_api_key": "abc 123",
        },
        opener=fake_open,
    )

    assert result["status"] == "success"
    assert captured["url"] == "http://emby:8096/emby/Library/Refresh?api_key=abc+123"
    assert captured["data"] == b""
    assert captured["timeout"] == 15


def test_emby_connection_test_reads_system_info_and_persists(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ServerName":"Media Server","Version":"4.8.10"}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        return FakeResponse()

    result, err = poster_maintenance.test_emby_connection(
        {"emby_url": "http://emby:8096", "emby_api_key": "abc 123"},
        opener=fake_open,
    )
    status = poster_maintenance.load_emby_status()

    assert err is None
    assert result["status"] == "success"
    assert result["server_name"] == "Media Server"
    assert result["version"] == "4.8.10"
    assert captured["url"] == "http://emby:8096/emby/System/Info?api_key=abc+123"
    assert captured["timeout"] == 15
    assert status["last_test"]["status"] == "success"
    assert "abc 123" not in str(result)


def test_emby_connection_test_handles_base_url_ending_in_emby(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b"{}"

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    result, err = poster_maintenance.test_emby_connection(
        {"emby_url": "http://emby:8096/emby", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert err is None
    assert result["status"] == "success"
    assert captured["url"] == "http://emby:8096/emby/System/Info?api_key=secret"


def test_emby_connection_test_skips_missing_config(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    called = False

    def fake_open(request, timeout):
        nonlocal called
        called = True

    result, err = poster_maintenance.test_emby_connection({}, opener=fake_open)

    assert err is None
    assert result["status"] == "skipped"
    assert called is False


def test_emby_connection_test_redacts_failed_http_response(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)

    def fake_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized secret",
            hdrs=None,
            fp=None,
        )

    result, err = poster_maintenance.test_emby_connection(
        {"emby_url": "http://emby:8096", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert err is None
    assert result["status"] == "failed"
    assert result["http_status"] == 401
    assert "secret" not in str(result)


def test_landscape_poster_routes_run_and_report_status(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie-background.jpg", b"landscape")
    _write(movie / "Movie-poster.jpg", b"portrait")
    _reset_poster_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    client = routes.app.test_client()
    run_res = client.post(
        "/api/maintenance/landscape-posters/run",
        json={"path": str(lib), "mode": "full", "synchronous": True},
    )
    status_res = client.get("/api/maintenance/landscape-posters/status")

    assert run_res.status_code == 200
    assert run_res.get_json()["run"]["counters"]["updated"] == 1
    assert status_res.status_code == 200
    assert status_res.get_json()["last_run"]["counters"]["updated"] == 1


def test_landscape_poster_route_rejects_paths_outside_library(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    _reset_poster_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/run",
        json={"path": str(sibling), "synchronous": True},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Path not found"


def test_landscape_poster_settings_require_emby_config_when_enabled(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/settings",
        json={"emby_refresh_enabled": True, "emby_url": "http://emby:8096"},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Emby URL and API key are required when refresh is enabled"


def test_landscape_poster_settings_save_without_exposing_api_key(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/settings",
        json={
            "enabled": True,
            "scan_interval_seconds": 30,
            "full_scan_interval_seconds": 60,
            "emby_refresh_enabled": True,
            "emby_url": "http://emby:8096",
            "emby_api_key": "secret",
        },
    )

    payload = res.get_json()
    assert res.status_code == 200
    assert payload["settings"]["enabled"] is True
    assert payload["settings"]["scan_interval_seconds"] == 60
    assert payload["settings"]["full_scan_interval_seconds"] == 3600
    assert payload["settings"]["emby_api_key_configured"] is True
    assert "secret" not in str(payload)


def test_landscape_poster_settings_migrate_and_recover_backup(monkeypatch, tmp_path):
    root = _reset_poster_state(monkeypatch, tmp_path)
    path = root / "settings.json"
    path.write_text('{"schema_version":0,"enabled":false,"scan_interval_seconds":120}', encoding="utf-8")

    migrated = poster_maintenance.load_settings()
    assert migrated["enabled"] is False
    assert migrated["scan_interval_seconds"] == 120

    assert poster_maintenance.save_settings(_settings(enabled=False, scan_interval_seconds=180))
    assert poster_maintenance.save_settings(_settings(enabled=True, scan_interval_seconds=240))
    path.write_text("{broken", encoding="utf-8")

    recovered = poster_maintenance.load_settings()
    assert recovered["enabled"] is False
    assert recovered["scan_interval_seconds"] == 180


def test_landscape_poster_partial_patch_preserves_api_key(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    assert poster_maintenance.save_settings(_settings(emby_url="http://emby:8096", emby_api_key="secret"))

    res = routes.app.test_client().patch(
        "/api/maintenance/landscape-posters/settings",
        json={"enabled": True},
    )

    assert res.status_code == 200
    assert poster_maintenance.load_settings()["emby_api_key"] == "secret"
    assert "secret" not in str(res.get_json())


def test_landscape_poster_emby_test_route_uses_saved_key_fallback(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    poster_maintenance.save_settings(
        _settings(emby_url="http://saved:8096", emby_api_key="saved-secret")
    )
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ServerName":"Saved Emby","Version":"4.9.0"}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(poster_maintenance.urllib.request, "urlopen", fake_open)

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/emby/test",
        json={"emby_url": "http://saved:8096"},
    )
    payload = res.get_json()

    assert res.status_code == 200
    assert payload["result"]["status"] == "success"
    assert captured["url"] == "http://saved:8096/emby/System/Info?api_key=saved-secret"
    assert payload["status"]["emby_status"]["last_test"]["server_name"] == "Saved Emby"
    assert "saved-secret" not in str(payload)


def test_landscape_poster_emby_test_route_allows_unsaved_values(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)
    captured = {}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"ServerName":"Unsaved Emby"}'

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse()

    monkeypatch.setattr(poster_maintenance.urllib.request, "urlopen", fake_open)

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/emby/test",
        json={"emby_url": "http://unsaved:8096", "emby_api_key": "unsaved-secret"},
    )
    status_res = routes.app.test_client().get("/api/maintenance/landscape-posters/status")

    assert res.status_code == 200
    assert captured["url"] == "http://unsaved:8096/emby/System/Info?api_key=unsaved-secret"
    assert poster_maintenance.load_settings()["emby_api_key"] == ""
    assert status_res.get_json()["emby_status"]["last_test"]["server_name"] == "Unsaved Emby"
    assert "unsaved-secret" not in str(res.get_json())


def test_landscape_poster_emby_test_route_rejects_malformed_payload(monkeypatch, tmp_path):
    _reset_poster_state(monkeypatch, tmp_path)

    res = routes.app.test_client().post(
        "/api/maintenance/landscape-posters/emby/test",
        json=["bad"],
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Settings are invalid"


def test_landscape_poster_ui_assets_render():
    client = routes.app.test_client()

    res = client.get("/maintenance")
    html = res.get_data(as_text=True)
    script_path = os.path.join(
        os.path.dirname(routes.app.root_path),
        "app",
        "static",
        "maintenance.js",
    )
    script = open(script_path, encoding="utf-8").read()

    assert res.status_code == 200
    assert "Landscape Posters" in html
    assert 'id="posterRunButton"' in html
    assert 'id="posterEmbyTestButton"' in html
    assert 'id="posterEmbyLastTest"' in html
    assert 'id="posterEmbyLastRefresh"' in html
    assert "fetch('/api/maintenance/landscape-posters/status')" in script
    assert "fetch('/api/maintenance/landscape-posters/scan'" in script
    assert 'id="posterApplyButton"' in html
    assert "fetch('/api/maintenance/landscape-posters/settings'" in script
    assert "fetch('/api/maintenance/landscape-posters/emby/test'" in script
    assert "server.textContent" in script
    assert "escapeHtml(item.source)" in script
    assert "escapeHtml(item.poster)" in script
