import json

from app import app_settings, routes


def test_app_settings_defaults_to_720_when_missing(tmp_path):
    path = tmp_path / "missing.json"

    settings = app_settings.load_settings(str(path))

    assert settings["test_lab_preview_height"] == 720
    assert settings["duplicate_grouping_mode"] == "balanced"
    assert settings["duplicate_keeper_rule"] == "quality"
    assert settings["duplicate_accessory_policy"] == "rename_unmatched"
    assert settings["subtitle_expected_languages"] == ["eng", "en", "en-us", "en-gb"]
    assert settings["subtitle_flag_missing"] is True
    assert settings["subtitle_flag_unknown_language"] is True
    assert settings["video_preview_bif_width"] == 320
    assert settings["video_preview_bif_interval_seconds"] == 10


def test_app_settings_persists_bif_generation_profile(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings(
        {"video_preview_bif_width": 480, "video_preview_bif_interval_seconds": 30},
        str(path),
    )

    settings = app_settings.load_settings(str(path))
    assert settings["video_preview_bif_width"] == 480
    assert settings["video_preview_bif_interval_seconds"] == 30


def test_app_settings_persists_custom_preview_height(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings({"test_lab_preview_height": 1080}, str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == app_settings.SCHEMA_VERSION
    assert app_settings.load_settings(str(path))["test_lab_preview_height"] == 1080


def test_app_settings_persists_duplicate_cleanup_settings(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings(
        {
            "test_lab_preview_height": 720,
            "duplicate_grouping_mode": "folder",
            "duplicate_keeper_rule": "largest",
            "duplicate_accessory_policy": "keep_unmatched",
            "duplicate_move_root": "/library/_duplicates",
            "duplicate_excluded_folders": ["trailers", "samples"],
        },
        str(path),
    )

    settings = app_settings.load_settings(str(path))
    assert settings["duplicate_grouping_mode"] == "folder"
    assert settings["duplicate_keeper_rule"] == "largest"
    assert settings["duplicate_accessory_policy"] == "keep_unmatched"
    assert settings["duplicate_move_root"] == "/library/_duplicates"
    assert settings["duplicate_excluded_folders"] == ["trailers", "samples"]


def test_app_settings_persists_subtitle_health_settings(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings(
        {
            "test_lab_preview_height": 720,
            "subtitle_expected_languages": "eng, spa, es",
            "subtitle_flag_missing": False,
            "subtitle_flag_unknown_language": False,
            "subtitle_subgen_detection": True,
        },
        str(path),
    )

    settings = app_settings.load_settings(str(path))
    assert settings["subtitle_expected_languages"] == ["eng", "spa", "es"]
    assert settings["subtitle_flag_missing"] is False
    assert settings["subtitle_flag_unknown_language"] is False
    assert settings["subtitle_subgen_detection"] is True


def test_app_settings_accepts_original_preview_mode(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings({"test_lab_preview_height": None}, str(path))

    assert app_settings.load_settings(str(path))["test_lab_preview_height"] is None
    assert app_settings.preview_height_label(None) == "Original"


def test_app_settings_invalid_file_falls_back_to_default(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text('{"schema_version":1,"test_lab_preview_height":"bad"}', encoding="utf-8")

    assert app_settings.load_settings(str(path))["test_lab_preview_height"] == 720


def test_app_settings_preserves_recognized_values_from_older_schema(tmp_path):
    path = tmp_path / "app_settings.json"
    path.write_text(
        json.dumps({
            "schema_version": 1,
            "test_lab_preview_height": 1080,
            "subtitle_flag_missing": False,
            "duplicate_keeper_rule": "largest",
        }),
        encoding="utf-8",
    )

    settings = app_settings.load_settings(str(path))

    assert settings["schema_version"] == app_settings.SCHEMA_VERSION
    assert settings["test_lab_preview_height"] == 1080
    assert settings["subtitle_flag_missing"] is False
    assert settings["duplicate_keeper_rule"] == "largest"


def test_app_settings_recovers_last_known_good_backup(tmp_path):
    path = tmp_path / "app_settings.json"
    assert app_settings.save_settings({"test_lab_preview_height": 900}, str(path))
    assert app_settings.save_settings({"test_lab_preview_height": 1080}, str(path))
    path.write_text("{broken", encoding="utf-8")

    assert app_settings.load_settings(str(path))["test_lab_preview_height"] == 900


def test_app_settings_partial_updates_merge_table_preferences(tmp_path):
    path = tmp_path / "app_settings.json"
    settings, err = app_settings.update_settings(
        {"subtitle_flag_missing": False, "table_preferences": {"first": {"widths": {"name": 240}}}},
        str(path),
    )
    assert err is None
    settings, err = app_settings.update_settings(
        {"table_preferences": {"second": {"widths": {"size": 120}, "sort": {"column": "size", "direction": "desc"}}}},
        str(path),
    )

    assert err is None
    assert settings["subtitle_flag_missing"] is False
    assert settings["table_preferences"]["first"]["widths"]["name"] == 240
    assert settings["table_preferences"]["second"]["sort"] == {"column": "size", "direction": "desc"}


def test_settings_page_renders_and_saves(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))

    client = routes.app.test_client()
    res = client.get("/settings")

    assert res.status_code == 200
    assert "Test Lab preview height" in res.get_data(as_text=True)
    assert "Duplicate Cleanup" in res.get_data(as_text=True)
    assert "Subtitle Health Check" in res.get_data(as_text=True)
    assert "Video Preview BIF Generation" in res.get_data(as_text=True)

    res = client.post(
        "/settings",
        data={
            "preview_height_preset": "custom",
            "preview_height_custom": "900",
            "duplicate_grouping_mode": "strict",
            "duplicate_keeper_rule": "newest",
            "duplicate_accessory_policy": "remove_all",
            "duplicate_move_root": "/library/_duplicates",
            "duplicate_excluded_folders": "trailers, samples",
            "subtitle_expected_languages": "eng, spa",
            "subtitle_flag_missing": "on",
            "subtitle_subgen_detection": "on",
            "video_preview_bif_width": "480",
            "video_preview_bif_interval_seconds": "30",
        },
    )

    assert res.status_code == 302
    settings = app_settings.load_settings(str(path))
    assert settings["test_lab_preview_height"] == 900
    assert settings["duplicate_grouping_mode"] == "strict"
    assert settings["duplicate_keeper_rule"] == "newest"
    assert settings["duplicate_accessory_policy"] == "remove_all"
    assert settings["duplicate_move_root"] == "/library/_duplicates"
    assert settings["duplicate_excluded_folders"] == ["trailers", "samples"]
    assert settings["subtitle_expected_languages"] == ["eng", "spa"]
    assert settings["subtitle_flag_missing"] is True
    assert settings["subtitle_flag_unknown_language"] is False
    assert settings["subtitle_subgen_detection"] is True
    assert settings["video_preview_bif_width"] == 480
    assert settings["video_preview_bif_interval_seconds"] == 30


def test_settings_page_rejects_invalid_custom_value(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))

    res = routes.app.test_client().post(
        "/settings",
        data={"preview_height_preset": "custom", "preview_height_custom": "-1"},
    )

    assert res.status_code == 400
    assert "Choose a positive preview height." in res.get_data(as_text=True)


def test_settings_api_gets_and_partially_updates(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))
    client = routes.app.test_client()

    update = client.patch("/api/settings", json={"subtitle_flag_missing": False})
    fetched = client.get("/api/settings")

    assert update.status_code == 200
    assert fetched.status_code == 200
    assert fetched.get_json()["settings"]["subtitle_flag_missing"] is False
    assert fetched.get_json()["settings"]["test_lab_preview_height"] == 720


def test_emby_settings_are_redacted_write_only_and_explicitly_clearable(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    client = routes.app.test_client()

    saved = client.patch(
        "/api/settings",
        json={"emby_url": "http://emby:8096", "emby_api_key": "top-secret"},
    )
    blank = client.patch("/api/settings", json={"emby_api_key": ""})
    fetched = client.get("/api/settings")

    assert saved.status_code == 200
    assert blank.status_code == 200
    assert fetched.get_json()["settings"]["emby_api_key_configured"] is True
    assert "emby_api_key" not in fetched.get_json()["settings"]
    assert "top-secret" not in fetched.get_data(as_text=True)
    assert app_settings.load_settings(str(path))["emby_api_key"] == "top-secret"

    cleared = client.patch("/api/settings", json={"emby_api_key_clear": True})
    assert cleared.get_json()["settings"]["emby_api_key_configured"] is False


def test_emby_path_mapping_text_validation_is_atomic(monkeypatch, tmp_path):
    root = tmp_path / "library"
    movies = root / "movies"
    movies.mkdir(parents=True)
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(app_settings, "LIB_ROOT", str(root))

    settings, err = app_settings.update_settings(
        {"emby_path_mappings": f"/media/movies => {movies}"}, str(path)
    )
    assert err is None
    assert settings["emby_path_mappings"] == [
        {"emby_prefix": "/media/movies", "local_prefix": str(movies.resolve())}
    ]

    previous = path.read_text(encoding="utf-8")
    settings, err = app_settings.update_settings(
        {"emby_path_mappings": "/media => ../outside"}, str(path)
    )
    assert settings is None
    assert "absolute" in err.lower()
    assert path.read_text(encoding="utf-8") == previous


def test_global_settings_import_legacy_emby_connection_once(monkeypatch, tmp_path):
    app_path = tmp_path / "state" / "app_settings.json"
    legacy_path = tmp_path / "state" / "landscape-posters" / "settings.json"
    legacy_path.parent.mkdir(parents=True)
    legacy_path.write_text(
        json.dumps(
            {
                "emby_url": "http://legacy:8096",
                "emby_api_key": "legacy-key",
                "emby_refresh_enabled": True,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("EMBY_SYNC_AFTER_MAINTENANCE", "false")
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(app_path))
    monkeypatch.setattr(app_settings, "LEGACY_EMBY_SETTINGS_PATH", str(legacy_path))

    imported = app_settings.load_settings()
    legacy_path.write_text(
        json.dumps({"emby_url": "http://changed:8096", "emby_api_key": "changed"}),
        encoding="utf-8",
    )
    reloaded = app_settings.load_settings()

    assert imported["emby_url"] == "http://legacy:8096"
    assert imported["emby_api_key"] == "legacy-key"
    assert reloaded["emby_url"] == "http://legacy:8096"
    assert imported["emby_sync_after_maintenance"] is True
    assert imported["emby_playback_protection"] is True
    assert json.loads(app_path.read_text(encoding="utf-8"))["schema_version"] == 8


def test_settings_page_contains_global_emby_controls_without_echoing_secret(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    app_settings.update_settings(
        {"emby_url": "http://emby:8096", "emby_api_key": "html-secret"}, str(path)
    )

    response = routes.app.test_client().get("/settings")
    html = response.get_data(as_text=True)

    assert 'id="emby_url"' in html
    assert 'id="emby_api_key"' in html
    assert 'id="emby_path_mappings"' in html
    assert 'id="emby_sync_after_maintenance"' in html
    assert 'id="emby_playback_protection"' in html
    assert 'id="embyPlaybackButton"' in html
    assert 'id="embyTestButton"' in html
    assert "html-secret" not in html


def test_playback_protection_environment_default_and_patch(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    monkeypatch.setenv("EMBY_PLAYBACK_PROTECTION", "false")

    assert app_settings.default_settings()["emby_playback_protection"] is False
    saved, err = app_settings.update_settings(
        {"emby_playback_protection": True}, str(path)
    )

    assert err is None
    assert saved["emby_playback_protection"] is True
    assert app_settings.public_settings(saved)["emby_playback_protection"] is True


def test_global_emby_test_route_uses_saved_key_without_exposing_it(monkeypatch, tmp_path):
    path = tmp_path / "app_settings.json"
    status_path = tmp_path / "emby-status.json"
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(routes.app_settings, "SETTINGS_PATH", str(path))
    monkeypatch.setattr(routes.poster_maintenance, "EMBY_STATUS_PATH", str(status_path))
    app_settings.update_settings(
        {"emby_url": "http://emby:8096", "emby_api_key": "route-secret"}, str(path)
    )
    captured = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"Id":"server","ServerName":"Emby","Version":"4.9"}'

    def opener(request, timeout):
        captured["url"] = request.full_url
        captured["token"] = request.get_header("X-emby-token")
        return Response()

    monkeypatch.setattr(routes.poster_maintenance.emby_client.urllib.request, "urlopen", opener)
    response = routes.app.test_client().post("/api/emby/test", json={})

    assert response.status_code == 200
    assert response.get_json()["result"]["server_name"] == "Emby"
    assert captured == {"url": "http://emby:8096/emby/System/Info", "token": "route-secret"}
    assert "route-secret" not in response.get_data(as_text=True)
