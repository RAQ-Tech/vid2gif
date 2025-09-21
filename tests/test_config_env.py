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
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    try:
        config = _reload_config_module()
        yield config
    finally:
        for key in env:
            monkeypatch.delenv(key, raising=False)
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
