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
