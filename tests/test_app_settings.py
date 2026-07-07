import json

from app import app_settings, routes


def test_app_settings_defaults_to_720_when_missing(tmp_path):
    path = tmp_path / "missing.json"

    settings = app_settings.load_settings(str(path))

    assert settings["test_lab_preview_height"] == 720


def test_app_settings_persists_custom_preview_height(tmp_path):
    path = tmp_path / "app_settings.json"

    assert app_settings.save_settings({"test_lab_preview_height": 1080}, str(path))

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == app_settings.SCHEMA_VERSION
    assert app_settings.load_settings(str(path))["test_lab_preview_height"] == 1080


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

    res = client.post(
        "/settings",
        data={"preview_height_preset": "custom", "preview_height_custom": "900"},
    )

    assert res.status_code == 302
    assert app_settings.load_settings(str(path))["test_lab_preview_height"] == 900


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
