import json
import math

from app import estimate_history


def _cfg(**overrides):
    cfg = {
        "height": 480,
        "fps": 15,
        "clip_len": 2.0,
        "percent_points": [10, 20, 30, 40, 50, 60, 70, 80, 90],
        "abs_early": 15.0,
        "abs_late_from_end": 10.0,
    }
    cfg.update(overrides)
    return cfg


def test_settings_unit_uses_sample_count_height_fps_and_clip_length():
    assert estimate_history.sample_count(_cfg()) == 11
    assert math.isclose(estimate_history.settings_unit(_cfg()), 1.0)
    assert math.isclose(
        estimate_history.settings_unit(_cfg(height=960, fps=30, clip_len=1.0)),
        4.0,
    )


def test_original_fps_uses_24_fps_and_low_confidence(monkeypatch):
    monkeypatch.setattr(estimate_history, "load_history", lambda path=None: [])
    cfg = _cfg(fps="original")

    payload = estimate_history.estimate_payload(
        2,
        cfg,
        in_memory_samples=[
            {
                "settings_unit": 1,
                "elapsed_seconds": 10,
                "output_size_bytes": 100,
                "created_at": 1,
            }
        ],
    )

    assert math.isclose(estimate_history.effective_fps(cfg), 24.0)
    assert payload["confidence"] == "low"
    assert payload["low_confidence"] is True
    assert "original FPS" in payload["detail"]


def test_estimate_uses_median_history_ratios(monkeypatch):
    monkeypatch.setattr(estimate_history, "load_history", lambda path=None: [])
    payload = estimate_history.estimate_payload(
        2,
        _cfg(),
        in_memory_samples=[
            {"settings_unit": 1, "elapsed_seconds": 10, "output_size_bytes": 100},
            {"settings_unit": 1, "elapsed_seconds": 30, "output_size_bytes": 300},
            {"settings_unit": 1, "elapsed_seconds": 20, "output_size_bytes": 200},
        ],
    )

    assert payload["estimated_seconds"] == 40
    assert payload["estimated_size_bytes"] == 400
    assert payload["message"] == (
        "2 compatible files, estimated time 40 seconds, estimated total size 400 B"
    )


def test_estimate_filters_history_by_optimization_mode(monkeypatch):
    monkeypatch.setattr(estimate_history, "load_history", lambda path=None: [])
    payload = estimate_history.estimate_payload(
        2,
        _cfg(optimize=False),
        in_memory_samples=[
            {
                "settings_unit": 1,
                "elapsed_seconds": 10,
                "output_size_bytes": 100,
                "optimize": True,
            },
            {
                "settings_unit": 1,
                "elapsed_seconds": 30,
                "output_size_bytes": 300,
                "optimize": False,
            },
        ],
    )

    assert payload["estimated_seconds"] == 60
    assert payload["estimated_size_bytes"] == 600


def test_no_history_returns_calibration_message(monkeypatch):
    monkeypatch.setattr(estimate_history, "load_history", lambda path=None: [])

    payload = estimate_history.estimate_payload(54, _cfg(), in_memory_samples=[])

    assert payload["estimated_seconds"] is None
    assert payload["estimated_size_bytes"] is None
    assert payload["confidence"] == "calibration"
    assert payload["message"] == (
        "54 compatible files. Run one GIF to calibrate time and size estimates."
    )


def test_corrupt_history_file_falls_back_to_no_samples(tmp_path):
    path = tmp_path / "estimate_history.json"
    path.write_text("{not json", encoding="utf-8")

    assert estimate_history.load_history(str(path)) == []


def test_history_samples_are_bounded(monkeypatch, tmp_path):
    path = tmp_path / "estimate_history.json"
    monkeypatch.setattr(estimate_history, "MAX_SAMPLES", 3)

    for i in range(5):
        assert estimate_history.save_sample(
            {
                "settings_unit": 1,
                "elapsed_seconds": i + 1,
                "output_size_bytes": 100 + i,
                "created_at": i,
            },
            path=str(path),
        )

    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == estimate_history.SCHEMA_VERSION
    assert len(data["samples"]) == 3
    assert [s["elapsed_seconds"] for s in data["samples"]] == [3, 4, 5]


def test_sample_from_job_contains_only_aggregate_metrics():
    sample = estimate_history.sample_from_job(
        {
            "id": "job1",
            "status": "success",
            "video": "/library/private/movie.mp4",
            "out_gif": "/library/private/poster.gif",
            "cfg": _cfg(),
            "elapsed_seconds": 12,
            "output_size_bytes": 1234,
            "_finished_ts": 99,
        }
    )

    assert sample == {
        "settings_unit": 1.0,
        "elapsed_seconds": 12.0,
        "output_size_bytes": 1234,
        "optimize": True,
        "created_at": 99.0,
    }


def test_job_duration_estimate_uses_median_normalized_history(monkeypatch):
    monkeypatch.setattr(estimate_history, "load_history", lambda path=None: [])

    estimate = estimate_history.job_duration_estimate(
        _cfg(),
        in_memory_samples=[
            {"settings_unit": 1, "elapsed_seconds": 10, "output_size_bytes": 100, "optimize": True},
            {"settings_unit": 2, "elapsed_seconds": 40, "output_size_bytes": 100, "optimize": True},
            {"settings_unit": 1, "elapsed_seconds": 1000, "output_size_bytes": 100, "optimize": False},
        ],
    )

    assert estimate == {"seconds": 15, "sample_count": 2, "confidence": "learning"}
