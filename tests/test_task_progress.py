import json

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


def test_unit_history_scales_estimate_to_current_workload(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for duration in (54, 60, 66):
        assert task_progress.record_duration("duplicate.analysis", duration, 600)

    estimate = task_progress.duration_estimate("duplicate.analysis", 300)

    assert estimate["seconds"] == 30
    assert estimate["seconds_per_unit"] == 0.1
    assert estimate["typical_units"] == 600
    assert estimate["confidence"] == "history"


def test_estimates_use_recent_samples_instead_of_stale_slow_runs(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for _ in range(4):
        assert task_progress.record_duration("duplicate.analysis", 480, 100)
    for _ in range(7):
        assert task_progress.record_duration("duplicate.analysis", 60, 100)

    estimate = task_progress.duration_estimate("duplicate.analysis", 100)

    assert estimate["seconds"] == 60
    assert estimate["sample_count"] == 7


def test_live_throughput_corrects_a_slow_historical_estimate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for _ in range(3):
        assert task_progress.record_duration("duplicate.analysis", 480, 100)
    scan = {"status": "running", "_started_ts": 100.0}

    task_progress.update_scan(
        scan,
        "duplicate_scan",
        10,
        "Checking duplicate candidates",
        now=100.0,
        stage_workflow="duplicate.analysis",
        completed_units=0,
        total_units=100,
        remaining_stages=[],
        unit_label="groups",
    )
    initial_eta = scan["eta_seconds"]
    task_progress.update_scan(
        scan,
        "duplicate_scan",
        80,
        "Checked 80 of 100 duplicate candidates",
        now=148.0,
        stage_workflow="duplicate.analysis",
        completed_units=80,
        total_units=100,
        remaining_stages=[],
        unit_label="groups",
    )

    assert initial_eta == 480
    assert 20 <= scan["eta_seconds"] <= 35
    assert scan["progress_percent"] == 80
    assert scan["progress_indeterminate"] is False


def test_stage_plan_sums_serial_operation_estimates(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for _ in range(3):
        assert task_progress.record_duration("bif.missing", 10, 100)
        assert task_progress.record_duration("bif.integrity", 20, 50)

    estimate = task_progress.plan_estimate(
        [
            {"workflow": "bif.missing", "total_units": 200},
            {"workflow": "bif.integrity", "total_units": 25},
        ]
    )

    assert estimate == {"seconds": 30, "confidence": "history", "sample_count": 6}


def test_stage_transitions_record_each_operation_rate(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    scan = {"status": "running", "_started_ts": 0.0}
    task_progress.update_scan(
        scan,
        "duplicate_scan",
        1,
        "Discovering videos",
        now=0.0,
        stage_workflow="duplicate.discovery",
        completed_units=0,
        total_units=100,
        remaining_stages=[{"workflow": "duplicate.emby"}],
    )
    task_progress.update_scan(
        scan,
        "duplicate_scan",
        90,
        "Matching Emby",
        now=10.0,
        stage_workflow="duplicate.emby",
        completed_units=0,
        total_units=10,
        remaining_stages=[],
    )
    task_progress.update_scan(
        scan,
        "duplicate_scan",
        100,
        "Complete",
        now=15.0,
        status="success",
        stage_workflow="duplicate.emby",
        completed_units=10,
        total_units=10,
        remaining_stages=[],
        overall_units=100,
    )

    assert task_progress.duration_estimate("duplicate.discovery", 50)["seconds"] == 5
    assert task_progress.duration_estimate("duplicate.emby", 20)["seconds"] == 10
    assert task_progress.duration_estimate("duplicate_scan", 200)["seconds"] == 30


def test_unknown_live_total_stops_countdown_after_exceeding_recent_workload(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(config, "STATE_ROOT", str(tmp_path / "state"))
    for _ in range(3):
        assert task_progress.record_duration("subtitle.filesystem", 10, 100)
    scan = {"status": "running", "_started_ts": 0.0}

    task_progress.update_scan(
        scan,
        "subtitle_scan",
        25,
        "Scanning subtitles",
        now=12.0,
        stage_workflow="subtitle.filesystem",
        completed_units=120,
        remaining_stages=[],
        unit_label="videos",
    )

    assert scan["eta_seconds"] is None
    assert scan["eta_confidence"] == "calibrating"
    assert "larger than recent runs" in scan["progress_detail"]


def test_version_one_duration_history_is_migrated_without_losing_samples(
    monkeypatch, tmp_path
):
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    history_path = state / "task_progress_history.json"
    history_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "workflows": {"duplicate_scan": [60, 70, 80]},
            }
        ),
        encoding="utf-8",
    )

    assert task_progress.duration_estimate("duplicate_scan")["seconds"] == 70
    assert task_progress.record_duration("duplicate_scan", 65, 100)
    migrated = json.loads(history_path.read_text(encoding="utf-8"))

    assert migrated["schema_version"] == 2
    assert len(migrated["workflows"]["duplicate_scan"]) == 4
    assert migrated["workflows"]["duplicate_scan"][-1]["work_units"] == 100
