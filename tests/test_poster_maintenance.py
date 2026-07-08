import os

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
    poster_maintenance.poster_runs.clear()
    monkeypatch.setattr(poster_maintenance, "_current_run_id", "")
    poster_maintenance._scheduler_state.clear()
    poster_maintenance._scheduler_state.update(
        {"last_checked_at": None, "next_run_at": None, "last_error": ""}
    )
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
    assert "fetch('/api/maintenance/landscape-posters/status')" in script
    assert "fetch('/api/maintenance/landscape-posters/run'" in script
    assert "fetch('/api/maintenance/landscape-posters/settings'" in script
    assert "escapeHtml(item.source)" in script
    assert "escapeHtml(item.poster)" in script
