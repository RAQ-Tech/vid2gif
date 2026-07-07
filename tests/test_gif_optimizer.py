import shutil
import subprocess

import pytest

from app import gif_optimizer


class DummyLogger:
    def __init__(self):
        self.info_msgs = []
        self.error_msgs = []

    def info(self, msg):
        self.info_msgs.append(msg)

    def error(self, msg):
        self.error_msgs.append(msg)


def _write(path, size):
    path.write_bytes(b"x" * size)


def _enable_optimizer(monkeypatch, *, level="2", timeout=600, binary="gifsicle"):
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE", True)
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE_LEVEL", level)
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE_TIMEOUT", timeout)
    monkeypatch.setattr(gif_optimizer, "GIFSICLE_BIN", binary)


def test_optimize_gif_replaces_original_when_smaller(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    captured = {}
    _enable_optimizer(monkeypatch)
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        captured["args"] = args
        _write(tmp_path / "poster.gif.optimized", 60)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert gif.stat().st_size == 60
    assert captured["args"][1] == "-O2"
    assert metrics["gif_optimization_status"] == "optimized"
    assert metrics["gif_size_before_opt_bytes"] == 100
    assert metrics["gif_size_after_opt_bytes"] == 60
    assert metrics["gif_optimization_saved_bytes"] == 40
    assert metrics["gif_optimization_savings_percent"] == 40.0
    assert job["gif_optimization_label"] == "Saved 40 B (40.0%)"
    assert any("Optimized GIF:" in msg for msg in logger.info_msgs)


def test_optimize_gif_keeps_original_when_candidate_is_larger(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    _enable_optimizer(monkeypatch)
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        _write(tmp_path / "poster.gif.optimized", 120)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert gif.stat().st_size == 100
    assert not (tmp_path / "poster.gif.optimized").exists()
    assert metrics["gif_optimization_status"] == "kept_original"
    assert metrics["gif_size_after_opt_bytes"] == 100
    assert job["gif_optimization_label"] == "No smaller result"


def test_optimize_gif_skips_when_disabled(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE", False)

    def fail_run(*args, **kwargs):
        raise AssertionError("optimizer should not run")

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fail_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert metrics["gif_optimization_status"] == "disabled"
    assert job["gif_optimization_label"] == "Disabled"
    assert gif.stat().st_size == 100


def test_optimize_gif_skips_when_job_disables_optimization(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {"cfg": {"optimize": False}}
    logger = DummyLogger()
    _enable_optimizer(monkeypatch)

    def fail_run(*args, **kwargs):
        raise AssertionError("optimizer should not run")

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fail_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert metrics["gif_optimization_status"] == "disabled"
    assert job["gif_optimization_label"] == "Disabled"
    assert gif.stat().st_size == 100


def test_optimize_gif_job_can_enable_when_default_is_disabled(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {"cfg": {"optimize": True}}
    captured = {}
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE", False)
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE_LEVEL", "2")
    monkeypatch.setattr(gif_optimizer, "GIF_OPTIMIZE_TIMEOUT", 600)
    monkeypatch.setattr(gif_optimizer, "GIFSICLE_BIN", "gifsicle")
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        captured["args"] = args
        _write(tmp_path / "poster.gif.optimized", 80)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job)

    assert captured["args"][1] == "-O2"
    assert metrics["gif_optimization_status"] == "optimized"
    assert gif.stat().st_size == 80


def test_optimize_gif_skips_when_gifsicle_missing(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    _enable_optimizer(monkeypatch)
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: None)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert metrics["gif_optimization_status"] == "missing"
    assert job["gif_optimization_label"] == "Gifsicle not found"
    assert gif.stat().st_size == 100


def test_optimize_gif_keeps_original_on_command_failure(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    _enable_optimizer(monkeypatch)
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        _write(tmp_path / "poster.gif.optimized", 50)
        return subprocess.CompletedProcess(args, 1, stderr="bad gif")

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert metrics["gif_optimization_status"] == "failed"
    assert gif.stat().st_size == 100
    assert not (tmp_path / "poster.gif.optimized").exists()
    assert any("bad gif" in msg for msg in logger.error_msgs)


def test_optimize_gif_keeps_original_on_timeout(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    logger = DummyLogger()
    _enable_optimizer(monkeypatch, timeout=1)
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        _write(tmp_path / "poster.gif.optimized", 50)
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    metrics = gif_optimizer.optimize_gif(str(gif), job, logger)

    assert metrics["gif_optimization_status"] == "timeout"
    assert gif.stat().st_size == 100
    assert not (tmp_path / "poster.gif.optimized").exists()


def test_optimize_gif_invalid_level_falls_back_to_o2(monkeypatch, tmp_path):
    gif = tmp_path / "poster.gif"
    _write(gif, 100)
    job = {}
    captured = {}
    _enable_optimizer(monkeypatch, level="99")
    monkeypatch.setattr(gif_optimizer.shutil, "which", lambda cmd: cmd)

    def fake_run(args, **kwargs):
        captured["args"] = args
        _write(tmp_path / "poster.gif.optimized", 50)
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(gif_optimizer.subprocess, "run", fake_run)

    gif_optimizer.optimize_gif(str(gif), job)

    assert captured["args"][1] == "-O2"


def test_real_gifsicle_optimizer_smoke(monkeypatch, tmp_path):
    if not shutil.which("gifsicle"):
        pytest.skip("gifsicle is not installed locally")
    gif = tmp_path / "poster.gif"
    gif.write_bytes(
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
        b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01"
        b"\x00\x00\x02\x02D\x01\x00;"
    )
    job = {}
    _enable_optimizer(monkeypatch)

    metrics = gif_optimizer.optimize_gif(str(gif), job)

    assert gif.exists()
    assert metrics["gif_optimization_status"] in {"optimized", "kept_original"}
