import json

from app import impact_backfill, impact_metrics


def _reset(monkeypatch, tmp_path):
    """Point both the impact store and the log root at a fresh temp directory."""
    impact_root = tmp_path / "dashboard"
    logs = tmp_path / "maintenance-logs"
    impact_root.mkdir(parents=True)
    logs.mkdir(parents=True)
    monkeypatch.setattr(impact_metrics, "IMPACT_ROOT", str(impact_root))
    monkeypatch.setattr(impact_metrics, "IMPACT_PATH", str(impact_root / "impact-metrics.json"))
    monkeypatch.setattr(impact_metrics, "IMPACT_BACKUP_PATH", str(impact_root / "impact-metrics.json.bak"))
    monkeypatch.setattr(impact_backfill, "MAINTENANCE_LOG_ROOT", str(logs))
    return logs


def _write_jsonl(path, header, records=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(header) + "\n")
        for record in records:
            handle.write(json.dumps(record) + "\n")


def test_backfill_recovers_quarantine_and_delete_totals(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "plan-1.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "move",
            "applied_count": 4,
            "total_applied_bytes": 4000,
        },
        [{"type": "moved", "original_path": "/library/a.mkv"}],
    )
    _write_jsonl(
        logs / "duplicates" / "plan-2.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-06T10:00:00+00:00",
            "action": "delete",
            "applied_count": 2,
            "total_applied_bytes": 500,
        },
    )

    report = impact_backfill.run(now="2026-02-01T00:00:00+00:00")

    assert report["events_found"] == 2
    assert report["events_applied"] == 2
    assert report["files_recovered"] == 6
    assert report["bytes_recovered"] == 4500

    operations = impact_metrics.status_payload()["operations"]
    assert operations["quarantined_files"] == 4
    assert operations["quarantined_bytes"] == 4000
    assert operations["deleted_files"] == 2
    assert operations["deleted_bytes"] == 500


def test_running_the_backfill_twice_does_not_double_count(monkeypatch, tmp_path):
    """The whole point of the deterministic event ids."""
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "plan-1.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "move",
            "applied_count": 3,
            "total_applied_bytes": 300,
        },
    )

    first = impact_backfill.run()
    second = impact_backfill.run()

    assert first["events_applied"] == 1
    assert second["events_applied"] == 0
    assert second["events_already_recorded"] == 1

    operations = impact_metrics.status_payload()["operations"]
    assert operations["quarantined_files"] == 3
    assert operations["quarantined_bytes"] == 300


def test_subtitle_runs_contribute_files_but_never_invented_bytes(monkeypatch, tmp_path):
    """Those logs stored a formatted size label, not a byte count.

    Reversing "12 KB" back into bytes would be a guess presented as a fact, so
    the file count is recovered and the byte total is reported as missing.
    """
    logs = _reset(monkeypatch, tmp_path)
    subtitles = logs / "subtitles"
    subtitles.mkdir(parents=True)
    (subtitles / "run-1.json").write_text(
        json.dumps(
            {
                "id": "run-1",
                "created_at": "2026-01-07T10:00:00+00:00",
                "operation": "quarantine",
                "applied_count": 5,
                "size_label": "12.0 KB",
            }
        ),
        encoding="utf-8",
    )

    report = impact_backfill.run()

    assert report["files_recovered"] == 5
    assert report["bytes_recovered"] == 0
    assert report["runs_missing_byte_totals"] == 1

    operations = impact_metrics.status_payload()["operations"]
    assert operations["quarantined_files"] == 5
    assert operations["quarantined_bytes"] == 0


def test_video_preview_generation_logs_are_not_counted_as_file_operations(monkeypatch, tmp_path):
    """Generating a preview writes a new file; it does not touch the library."""
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "video-previews" / "gen-1.jsonl",
        {"type": "generation", "timestamp": "2026-01-08T10:00:00+00:00", "applied_count": 40},
    )
    _write_jsonl(
        logs / "video-previews" / "repair-1.jsonl",
        {
            "type": "quality-repair-summary",
            "timestamp": "2026-01-09T10:00:00+00:00",
            "action": "quarantine",
            "applied_count": 2,
            "total_applied_bytes": 900,
        },
    )

    report = impact_backfill.run()

    assert report["events_found"] == 1, "generation must not be treated as a cleanup"
    assert report["files_recovered"] == 2
    assert impact_metrics.status_payload()["operations"]["quarantined_files"] == 2


def test_unreadable_and_empty_logs_are_skipped_rather_than_failing(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    duplicates = logs / "duplicates"
    duplicates.mkdir(parents=True)
    (duplicates / "empty.jsonl").write_text("", encoding="utf-8")
    (duplicates / "garbage.jsonl").write_text("not json at all\n", encoding="utf-8")
    (duplicates / "index.json").write_text(json.dumps({"logs": []}), encoding="utf-8")
    _write_jsonl(
        duplicates / "good.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-10T10:00:00+00:00",
            "action": "move",
            "applied_count": 1,
            "total_applied_bytes": 10,
        },
    )

    report = impact_backfill.run()

    assert report["events_found"] == 1
    assert report["files_recovered"] == 1


def test_backfill_with_no_logs_at_all_is_a_clean_no_op(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    report = impact_backfill.run()

    assert report["events_found"] == 0
    assert report["files_recovered"] == 0
    assert report["not_recoverable"], "the report must still say what it cannot know"
    assert impact_metrics.status_payload()["operations"]["quarantined_files"] == 0


def test_report_states_what_it_could_not_recover(monkeypatch, tmp_path):
    """A total that looks complete when it is not is worse than a small one."""
    _reset(monkeypatch, tmp_path)

    report = impact_backfill.run()
    text = " ".join(report["not_recoverable"]).lower()

    # These hold on every install: no log ever carried them.
    assert "issue discovery and resolution history" in text
    assert "gif creation" in text
    assert "retention" in text

    # These do not apply when there is nothing of that kind on disk, and a
    # disclosure that lists irrelevant caveats is one people stop reading.
    assert "subtitle byte totals" not in text
    assert "actor image" not in text


def test_conditional_gaps_appear_only_when_they_apply(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    subtitles = logs / "subtitles"
    subtitles.mkdir(parents=True)
    # An old-format entry: size recorded only as a label.
    (subtitles / "old.json").write_text(
        json.dumps(
            {
                "id": "old",
                "created_at": "2026-01-01T10:00:00+00:00",
                "operation": "quarantine",
                "applied_count": 2,
                "size_label": "8.0 KB",
            }
        ),
        encoding="utf-8",
    )
    _write_jsonl(logs / "actor-images" / "apply-1.jsonl", {"type": "apply", "timestamp": "2026-01-02T10:00:00+00:00"})

    report = impact_backfill.run()
    text = " ".join(report["not_recoverable"]).lower()

    assert "subtitle byte totals for 1 earlier run" in text
    assert "actor image imports" in text


def test_subtitle_logs_that_record_bytes_are_recovered_in_full(monkeypatch, tmp_path):
    """The fix at the source: new logs carry applied_bytes, so nothing is lost."""
    logs = _reset(monkeypatch, tmp_path)
    subtitles = logs / "subtitles"
    subtitles.mkdir(parents=True)
    (subtitles / "new.json").write_text(
        json.dumps(
            {
                "id": "new",
                "created_at": "2026-03-01T10:00:00+00:00",
                "operation": "quarantine",
                "applied_count": 3,
                "applied_bytes": 8192,
                "size_label": "8.0 KB",
            }
        ),
        encoding="utf-8",
    )

    report = impact_backfill.run()

    assert report["bytes_recovered"] == 8192
    assert report["runs_missing_byte_totals"] == 0
    assert "subtitle byte totals" not in " ".join(report["not_recoverable"]).lower()

    operations = impact_metrics.status_payload()["operations"]
    assert operations["quarantined_files"] == 3
    assert operations["quarantined_bytes"] == 8192


def test_ensure_backfilled_runs_once_and_is_visible_on_the_dashboard(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "plan-1.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "move",
            "applied_count": 7,
            "total_applied_bytes": 700,
        },
    )

    assert impact_metrics.get_backfill_report() is None
    first = impact_backfill.ensure_backfilled()
    assert first is not None and first["events_applied"] == 1

    # Second start-up must not sweep the logs again.
    assert impact_backfill.ensure_backfilled() is None

    payload = impact_metrics.status_payload()
    assert payload["backfill"]["events_applied"] == 1
    assert payload["backfill"]["files_recovered"] == 7
    assert payload["backfill"]["not_recoverable"]
    assert payload["operations"]["quarantined_files"] == 7


def test_survey_counts_logs_without_changing_anything(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(logs / "duplicates" / "a.jsonl", {"type": "summary"})
    _write_jsonl(logs / "actor-images" / "b.jsonl", {"type": "apply"})

    survey = impact_backfill.survey()

    assert survey["duplicate_logs"] == 1
    assert survey["actor_image_logs"] == 1
    assert survey["subtitle_logs"] == 0
    assert impact_metrics.get_backfill_report() is None


# ---------------------------------------------------------------------------
# Error paths. A backfill that dies on one damaged log would leave the whole
# lifetime figure unrecovered, so every reader skips what it cannot parse.
# ---------------------------------------------------------------------------


def test_a_log_that_cannot_be_opened_is_skipped(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "readable.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "move",
            "applied_count": 2,
            "total_applied_bytes": 20,
        },
    )
    _write_jsonl(
        logs / "duplicates" / "locked.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-06T10:00:00+00:00",
            "action": "move",
            "applied_count": 99,
            "total_applied_bytes": 999,
        },
    )

    import builtins

    real_open = builtins.open

    def refuse_locked(path, *args, **kwargs):
        if "locked" in str(path):
            raise OSError("Permission denied")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", refuse_locked)

    report = impact_backfill.run()

    # The readable log still counts; the unreadable one is passed over rather
    # than taking the whole recovery down with it.
    assert report["events_found"] == 1
    assert report["files_recovered"] == 2


def test_a_subtitle_entry_that_is_not_an_object_is_skipped(monkeypatch, tmp_path):
    logs = _reset(monkeypatch, tmp_path)
    subtitles = logs / "subtitles"
    subtitles.mkdir(parents=True)
    (subtitles / "broken.json").write_text("[1, 2, 3]", encoding="utf-8")
    (subtitles / "truncated.json").write_text('{"operation": "quarantine",', encoding="utf-8")
    (subtitles / "good.json").write_text(
        json.dumps(
            {
                "id": "good",
                "created_at": "2026-01-07T10:00:00+00:00",
                "operation": "delete",
                "applied_count": 1,
                "applied_bytes": 64,
            }
        ),
        encoding="utf-8",
    )

    report = impact_backfill.run()

    assert report["events_found"] == 1
    assert impact_metrics.status_payload()["operations"]["deleted_bytes"] == 64


def test_a_run_that_applied_nothing_contributes_nothing(monkeypatch, tmp_path):
    """An apply that touched no files is not an event worth recording."""
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "empty-run.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "move",
            "applied_count": 0,
            "total_applied_bytes": 0,
        },
    )

    report = impact_backfill.run()

    assert report["events_found"] == 0
    assert report["files_recovered"] == 0


def test_an_unrecognised_action_is_counted_but_not_guessed_at(monkeypatch, tmp_path):
    """Better to record it as "other" than to assume it was a deletion."""
    logs = _reset(monkeypatch, tmp_path)
    _write_jsonl(
        logs / "duplicates" / "odd.jsonl",
        {
            "type": "summary",
            "timestamp": "2026-01-05T10:00:00+00:00",
            "action": "some-future-action",
            "applied_count": 3,
            "total_applied_bytes": 30,
        },
    )

    impact_backfill.run()

    operations = impact_metrics.status_payload()["operations"]
    assert operations["other_files"] == 3
    assert operations["other_bytes"] == 30
    assert operations["deleted_files"] == 0, "an unknown action must not be read as a delete"
    assert operations["quarantined_files"] == 0


def test_an_unreadable_log_directory_is_not_fatal(monkeypatch, tmp_path):
    _reset(monkeypatch, tmp_path)

    def refuse(_path):
        raise OSError("Permission denied")

    monkeypatch.setattr(impact_backfill.os, "listdir", refuse)

    report = impact_backfill.run()

    assert report["events_found"] == 0
    assert report["survey"]["duplicate_logs"] == 0


def test_a_backfill_that_blows_up_cannot_stop_the_app_starting(monkeypatch, tmp_path):
    """ensure_backfilled runs at import in app/wsgi.py.

    Recovering an old total is a nicety; serving the app is not. Whatever is
    wrong with the logs, gunicorn still has to start.
    """
    _reset(monkeypatch, tmp_path)

    def explode(**_kwargs):
        raise RuntimeError("something unforeseen in the logs")

    monkeypatch.setattr(impact_backfill, "run", explode)

    report = impact_backfill.ensure_backfilled()

    assert report is not None, "the failure must still be recorded"
    assert "could not be replayed" in report["error"]
    assert report["events_applied"] == 0
    assert report["not_recoverable"]

    # And it is stored, so start-up does not retry a broken sweep every boot.
    assert impact_metrics.get_backfill_report()["error"]
    assert impact_backfill.ensure_backfilled() is None

    # The dashboard still renders, carrying the failure rather than hiding it.
    payload = impact_metrics.status_payload()
    assert payload["backfill"]["error"]
