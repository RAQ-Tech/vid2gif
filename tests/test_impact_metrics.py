import json
import threading

from app import impact_metrics


def _reset(monkeypatch, tmp_path):
    root = tmp_path / "dashboard"
    monkeypatch.setattr(impact_metrics, "IMPACT_ROOT", str(root))
    monkeypatch.setattr(impact_metrics, "IMPACT_PATH", str(root / "impact-metrics.json"))
    monkeypatch.setattr(impact_metrics, "IMPACT_BACKUP_PATH", str(root / "impact-metrics.json.bak"))
    impact_metrics._last_error = ""
    return root


def _issue(issue_id, path, finding="finding"):
    return {
        "issue_id": issue_id,
        "finding_ids": [finding],
        "label": issue_id,
        "path": str(path),
    }


def test_fresh_tracker_starts_at_zero_without_backfill(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    payload = impact_metrics.status_payload()

    assert payload["status"] == "ok"
    assert payload["total_fixes"] == 0
    assert payload["discovered_count"] == 0
    assert payload["tracking_started_at"]
    assert len(payload["daily"]) == 30


def test_scan_deduplicates_clears_and_counts_reopened_occurrences(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    scope = tmp_path / "library"
    path = scope / "Movie" / "video.mkv"

    assert impact_metrics.record_scan("one", "duplicates", "duplicates", str(scope), [_issue("dup:1", path)])
    assert not impact_metrics.record_scan("one", "duplicates", "duplicates", str(scope), [_issue("dup:1", path)])
    assert impact_metrics.record_scan("two", "duplicates", "duplicates", str(scope), [_issue("dup:1", path)])
    current = impact_metrics.status_payload()
    assert current["discovered_count"] == 1
    assert current["open_count"] == 1

    impact_metrics.record_scan("three", "duplicates", "duplicates", str(scope), [])
    cleared = impact_metrics.status_payload()
    assert cleared["open_count"] == 0
    assert cleared["cleared_elsewhere_count"] == 1

    impact_metrics.record_scan("four", "duplicates", "duplicates", str(scope), [_issue("dup:1", path)])
    reopened = impact_metrics.status_payload()
    assert reopened["discovered_count"] == 2
    assert reopened["open_count"] == 1


def test_issue_remains_open_until_all_workflow_sources_are_resolved(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    scope = tmp_path / "library"
    video = scope / "Movie" / "video.mkv"
    issue_id = "video-preview:1"
    impact_metrics.record_scan("missing", "video_previews", "missing", str(scope), [_issue(issue_id, video, "missing")])
    impact_metrics.record_scan("quality", "video_previews", "quality", str(scope), [_issue(issue_id, video, "bad-bif")])

    impact_metrics.record_maintenance_action(
        "clean",
        "video_previews",
        resolutions=[{"issue_id": issue_id, "stream": "quality", "finding_ids": ["bad-bif"]}],
    )
    assert impact_metrics.status_payload()["open_count"] == 1
    assert impact_metrics.status_payload()["total_fixes"] == 0

    impact_metrics.record_maintenance_action(
        "generate",
        "video_previews",
        resolutions=[{"issue_id": issue_id, "stream": "missing", "finding_ids": ["missing"]}],
    )
    payload = impact_metrics.status_payload()
    assert payload["open_count"] == 0
    assert payload["total_fixes"] == 1
    assert payload["resolution_percent"] == 100


def test_action_counts_resolved_issues_and_operations_once(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)
    path = tmp_path / "library" / "movie.srt"
    resolution = {
        **_issue("subtitle:1", path, "file-1"),
        "stream": "subtitles",
        "ensure_issue": True,
    }
    operations = {"quarantined_files": 1, "quarantined_bytes": 2048}

    assert impact_metrics.record_maintenance_action(
        "apply-1", "subtitles", resolutions=[resolution], operations=operations
    )
    assert not impact_metrics.record_maintenance_action(
        "apply-1", "subtitles", resolutions=[resolution], operations=operations
    )
    payload = impact_metrics.status_payload()
    assert payload["total_fixes"] == 1
    assert payload["discovered_count"] == 1
    assert payload["operations"]["quarantined_files"] == 1
    assert payload["operations"]["quarantined_bytes"] == 2048
    assert payload["operations"]["quarantined_size_label"] == "2.0 KB"


def test_creative_output_is_separate_and_idempotent(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    impact_metrics.record_creative_output("job-1", "standard", output_bytes=1000, saved_bytes=100)
    impact_metrics.record_creative_output("job-1", "standard", output_bytes=1000, saved_bytes=100)
    impact_metrics.record_creative_output("run-1:v1", "test_lab", output_bytes=2000, saved_bytes=200)

    payload = impact_metrics.status_payload()
    creative = payload["creative_output"]
    assert payload["total_fixes"] == 0
    assert creative["standard_gifs"] == 1
    assert creative["test_lab_variants"] == 1
    assert creative["output_bytes"] == 3000
    assert creative["optimization_saved_bytes"] == 300


def test_concurrent_events_are_not_lost(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def record(index):
        impact_metrics.record_maintenance_action(
            f"poster-{index}",
            "posters",
            resolutions=[
                {
                    **_issue(f"poster:{index}", tmp_path / f"poster-{index}.jpg"),
                    "stream": "posters",
                    "ensure_issue": True,
                    "resolve_all": True,
                }
            ],
        )

    threads = [threading.Thread(target=record, args=(index,)) for index in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert impact_metrics.status_payload()["total_fixes"] == 12


def test_corrupt_primary_recovers_from_backup(monkeypatch, tmp_path):
    root = _reset(monkeypatch, tmp_path)
    impact_metrics.record_creative_output("job-1", "standard", output_bytes=100)
    impact_metrics.record_creative_output("job-2", "standard", output_bytes=200)
    (root / "impact-metrics.json").write_text("{broken", encoding="utf-8")

    payload = impact_metrics.status_payload()

    assert payload["status"] == "warning"
    assert "last-known-good backup" in payload["error"]
    assert payload["creative_output"]["standard_gifs"] == 1
    with open(root / "impact-metrics.json", "r", encoding="utf-8") as handle:
        assert json.load(handle)["schema_version"] == 1
