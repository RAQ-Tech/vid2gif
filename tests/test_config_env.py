import importlib
import sys

import pytest
from contextlib import contextmanager
from pathlib import Path


def _reload_config_module():
    for name in ("config", "app.config"):
        sys.modules.pop(name, None)
    return importlib.import_module("app.config")


@contextmanager
def config_with_env(monkeypatch, **env):
    try:
        with monkeypatch.context() as scoped:
            for key, value in env.items():
                scoped.setenv(key, value)
            config = _reload_config_module()
            yield config
    finally:
        _reload_config_module()


def test_env_overrides_paths(monkeypatch, tmp_path):
    lib_root = tmp_path / "library"
    state_root = tmp_path / "state"
    log_dir = tmp_path / "custom_logs"
    tmp_root = tmp_path / "custom_tmp"
    process_tmp_root = tmp_path / "processing" / "custom_tmp"

    env_values = {
        "LIB_ROOT": str(lib_root),
        "STATE_ROOT": str(state_root),
        "LOG_DIR": str(log_dir),
        "TMP_ROOT": str(tmp_root),
        "PROCESS_TMP_ROOT": str(process_tmp_root),
    }

    with config_with_env(monkeypatch, **env_values) as config:
        assert config.LIB_ROOT == str(lib_root)
        assert config.STATE_ROOT == str(state_root)
        assert config.LOG_DIR == str(log_dir)
        assert config.TMP_ROOT == str(tmp_root)
        assert config.PROCESS_TMP_ROOT == str(process_tmp_root)

        assert Path(config.LOG_DIR).is_dir()
        assert Path(config.TMP_ROOT).is_dir()
        assert Path(config.PROCESS_TMP_ROOT).is_dir()


def test_state_root_updates_default_directories(monkeypatch, tmp_path):
    state_root = tmp_path / "state"

    with config_with_env(monkeypatch, STATE_ROOT=str(state_root)) as config:
        assert config.STATE_ROOT == str(state_root)
        assert config.LOG_DIR == str(state_root / "logs")
        assert config.TMP_ROOT == str(state_root / "tmp")
        assert config.PROCESS_TMP_ROOT == str(state_root / "processing" / "tmp")

        assert Path(config.LOG_DIR).is_dir()
        assert Path(config.TMP_ROOT).is_dir()
        assert Path(config.PROCESS_TMP_ROOT).is_dir()


def test_gif_optimizer_defaults_are_enabled(monkeypatch):
    for key in (
        "GIF_OPTIMIZE",
        "GIF_OPTIMIZE_LEVEL",
        "GIFSICLE_BIN",
        "GIF_OPTIMIZE_TIMEOUT",
    ):
        monkeypatch.delenv(key, raising=False)
    with config_with_env(monkeypatch) as config:
        assert config.GIF_OPTIMIZE is True
        assert config.GIF_OPTIMIZE_LEVEL == "2"
        assert config.GIFSICLE_BIN == "gifsicle"
        assert config.GIF_OPTIMIZE_TIMEOUT == 600


def test_gif_optimizer_env_overrides(monkeypatch):
    with config_with_env(
        monkeypatch,
        GIF_OPTIMIZE="0",
        GIF_OPTIMIZE_LEVEL="3",
        GIFSICLE_BIN="/usr/local/bin/gifsicle",
        GIF_OPTIMIZE_TIMEOUT="120",
    ) as config:
        assert config.GIF_OPTIMIZE is False
        assert config.GIF_OPTIMIZE_LEVEL == "3"
        assert config.GIFSICLE_BIN == "/usr/local/bin/gifsicle"
        assert config.GIF_OPTIMIZE_TIMEOUT == 120


def test_gif_optimizer_invalid_timeout_uses_default(monkeypatch):
    with config_with_env(monkeypatch, GIF_OPTIMIZE_TIMEOUT="soon") as config:
        assert config.GIF_OPTIMIZE_TIMEOUT == 600


def test_template_auto_reload_defaults_on_and_the_image_turns_it_off(monkeypatch):
    """Dev reloads templates on every request; the container should not.

    Nobody edits templates inside the image, so the per-request stat() is pure
    overhead there. The default stays on so `python -m app.main` behaves as it
    always has.
    """
    import importlib
    import sys

    def reloaded_routes():
        for name in ("app.routes", "routes"):
            sys.modules.pop(name, None)
        return importlib.import_module("app.routes")

    try:
        monkeypatch.delenv("TEMPLATES_AUTO_RELOAD", raising=False)
        assert reloaded_routes().app.config["TEMPLATES_AUTO_RELOAD"] is True

        for value in ("0", "false", "OFF", "no"):
            monkeypatch.setenv("TEMPLATES_AUTO_RELOAD", value)
            assert reloaded_routes().app.config["TEMPLATES_AUTO_RELOAD"] is False, value

        for value in ("1", "true", "anything-else"):
            monkeypatch.setenv("TEMPLATES_AUTO_RELOAD", value)
            assert reloaded_routes().app.config["TEMPLATES_AUTO_RELOAD"] is True, value
    finally:
        monkeypatch.delenv("TEMPLATES_AUTO_RELOAD", raising=False)
        reloaded_routes()

    dockerfile = (Path(__file__).resolve().parents[1] / "Dockerfile").read_text()
    assert "TEMPLATES_AUTO_RELOAD=0" in dockerfile


def test_default_state_root_refuses_to_create_itself(monkeypatch):
    """Importing app must not invent /state on a machine that has no /state.

    In the container the image creates /state and a volume is mounted over it,
    so the default is right there. Anywhere else the default is a path nobody
    asked for -- a stray folder at the drive root on Windows, a permission error
    on Linux -- and it filled up with 2 MB of test residue before anyone noticed.
    """
    import app.config as config

    created = []
    monkeypatch.setattr(config.os, "makedirs", lambda path, **kw: created.append(path))
    monkeypatch.setattr(config.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(config, "STATE_ROOT_IS_DEFAULT", True)

    with pytest.raises(config.StateRootError) as excinfo:
        config._ensure_state_directories()

    assert created == [], "refused, but still created directories"
    message = str(excinfo.value)
    assert "STATE_ROOT is not set" in message
    # The message has to tell you what to do, or it is just a different failure.
    assert "export STATE_ROOT" in message
    assert "pytest" in message
    assert "Docker" in message


def test_default_state_root_is_used_when_it_already_exists(monkeypatch, tmp_path):
    """The container case, exercised against a real directory.

    In the image, /state exists because the Dockerfile creates it and a volume
    is mounted over it, so creating subdirectories under it is intended. Only
    the literal path differs here: everything else is the real code path, with
    real makedirs against a real directory that already exists.
    """
    import app.config as config

    state_root = tmp_path / "state"
    state_root.mkdir()

    monkeypatch.setattr(config, "STATE_ROOT_IS_DEFAULT", True)
    monkeypatch.setattr(config, "STATE_ROOT", str(state_root))
    monkeypatch.setattr(config, "LOG_DIR", str(state_root / "logs"))
    monkeypatch.setattr(config, "TMP_ROOT", str(state_root / "tmp"))
    monkeypatch.setattr(config, "PROCESS_TMP_ROOT", str(state_root / "processing" / "tmp"))
    monkeypatch.setattr(config, "TEST_LAB_ROOT", str(state_root / "test-lab"))
    monkeypatch.setattr(config, "LANDSCAPE_POSTER_ROOT", str(state_root / "landscape-posters"))

    config._ensure_state_directories()

    assert (state_root / "logs").is_dir()
    assert (state_root / "tmp").is_dir()
    assert (state_root / "processing" / "tmp").is_dir()
    assert (state_root / "test-lab").is_dir()
    assert (state_root / "landscape-posters").is_dir()


def test_an_explicit_state_root_is_always_created(monkeypatch, tmp_path):
    """An explicit value is a request; honour it even if it does not exist yet."""
    import app.config as config

    created = []
    monkeypatch.setattr(config.os, "makedirs", lambda path, **kw: created.append(path))
    monkeypatch.setattr(config.os.path, "isdir", lambda path: False)
    monkeypatch.setattr(config, "STATE_ROOT_IS_DEFAULT", False)

    config._ensure_state_directories()

    assert config.LOG_DIR in created


def test_state_root_is_default_reflects_the_environment(monkeypatch, tmp_path):
    with config_with_env(monkeypatch, STATE_ROOT=str(tmp_path / "explicit")) as config:
        assert config.STATE_ROOT_IS_DEFAULT is False
