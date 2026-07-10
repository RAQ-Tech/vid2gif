import importlib
import sys
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
