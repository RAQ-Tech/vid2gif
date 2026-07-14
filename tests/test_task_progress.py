from app import config, task_progress


def test_first_scan_is_indeterminate_and_explains_calibration(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    scan = {"status": "running", "_started_ts": 100.0}

    task_progress.update_scan(
        scan, "video-previews", 56, "Scanned 1490 videos", now=130.0
    )

    assert scan["progress_indeterminate"] is True
    assert scan["progress_percent"] == 0
    assert scan["eta_seconds"] is None
    assert "Learning timing" in scan["progress_label"]


def test_history_produces_stable_countdown_instead_of_reprojecting(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for duration in (100, 110, 120):
        assert task_progress.record_duration("video-previews", duration)
    scan = {"status": "running", "_started_ts": 100.0}

    task_progress.update_scan(scan, "video-previews", 25, "Scanning", now=130.0)
    first_eta = scan["eta_seconds"]
    task_progress.update_scan(scan, "video-previews", 80, "Scanning", now=140.0)

    assert first_eta == 80
    assert scan["eta_seconds"] == 70
    assert scan["eta_confidence"] == "history"
    assert scan["progress_indeterminate"] is True


def test_scan_can_disable_history_when_workload_duration_is_not_comparable(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    assert task_progress.record_duration("video-previews", 600)
    scan = {"status": "running", "_started_ts": 100.0}

    task_progress.update_scan(
        scan,
        "video-previews",
        80,
        "Matching results with Emby",
        now=130.0,
        use_history=False,
        current_stage="Matching results with Emby",
        progress_detail="120 videos found; loading the Emby catalog",
    )

    assert scan["progress_indeterminate"] is True
    assert scan["eta_seconds"] is None
    assert scan["eta_confidence"] == "none"
    assert scan["progress_label"] == "Matching results with Emby"
    assert scan["progress_detail"] == "120 videos found; loading the Emby catalog"
    assert task_progress.public_fields(scan)["current_stage"] == "Matching results with Emby"


def test_success_records_duration_for_future_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    scan = {"status": "running", "_started_ts": 100.0}

    task_progress.update_scan(
        scan, "subtitles", 100, "Subtitle scan complete", now=145.0, status="success"
    )

    estimate = task_progress.duration_estimate("subtitles")
    assert scan["progress_percent"] == 100
    assert scan["progress_indeterminate"] is False
    assert estimate["seconds"] == 45
