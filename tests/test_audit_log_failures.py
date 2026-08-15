"""What happens when the audit log cannot be written.

These logs are the only thing that makes a cleanup reversible. If one fails to
write, the files have already moved -- so the run did succeed, and reporting it
as a failure is wrong. What is gone is the ability to undo it, and that is what
the operator needs to be told.

Both paths used to get this wrong in opposite directions: subtitles swallowed
the error entirely, and duplicates let it fail the whole run after the files had
already been moved.
"""

import os

from app import maintenance, subtitle_maintenance


def _deny_writes(monkeypatch, module):
    """Make the log directory unwritable the way a full or read-only disk would."""

    def refuse(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(module.os, "makedirs", refuse)


def test_subtitle_log_failure_is_reported_rather_than_swallowed(monkeypatch, tmp_path):
    _deny_writes(monkeypatch, subtitle_maintenance)

    error = subtitle_maintenance._save_action_log(
        {"operation": "quarantine"},
        {"id": "run-1", "applied_count": 2, "applied_bytes": 100},
        [],
    )

    assert error, "a failed audit log must not be reported as success"
    assert "No space left on device" in error


def test_subtitle_log_success_reports_no_error(monkeypatch, tmp_path):
    monkeypatch.setattr(subtitle_maintenance, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(subtitle_maintenance, "LOG_INDEX", str(tmp_path / "logs" / "index.json"))

    error = subtitle_maintenance._save_action_log(
        {"operation": "quarantine"},
        {"id": "run-1", "applied_count": 2, "applied_bytes": 100},
        [],
    )

    assert error == ""
    assert os.path.isfile(tmp_path / "logs" / "run-1.json")


def test_subtitle_log_records_the_byte_count_not_only_a_label(monkeypatch, tmp_path):
    """The backfill reads this file; a formatted label is a dead end for it."""
    import json

    monkeypatch.setattr(subtitle_maintenance, "LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(subtitle_maintenance, "LOG_INDEX", str(tmp_path / "logs" / "index.json"))

    subtitle_maintenance._save_action_log(
        {"operation": "quarantine"},
        {"id": "run-2", "applied_count": 3, "applied_bytes": 4096},
        [],
    )

    entry = json.loads((tmp_path / "logs" / "run-2.json").read_text(encoding="utf-8"))
    assert entry["applied_bytes"] == 4096
    assert entry["size_label"], "the human-readable label is still there too"


def test_duplicate_cleanup_survives_a_log_it_cannot_write(monkeypatch, tmp_path):
    """The files moved. A logging failure must not be dressed up as a failed run."""
    lib = tmp_path / "library"
    movie = lib / "Movie"
    movie.mkdir(parents=True)
    keep = movie / "Movie.1080p.mkv"
    remove = movie / "Movie.720p.mkv"
    keep.write_bytes(b"a" * 200)
    remove.write_bytes(b"b" * 100)

    maintenance.duplicate_scans.clear()
    maintenance.cleanup_plans.clear()
    monkeypatch.setattr(maintenance, "probe_video_metadata", lambda path: {})
    monkeypatch.setattr(
        maintenance.app_settings, "load_settings", lambda: dict(maintenance.app_settings.default_settings())
    )

    scan, err = maintenance.start_duplicate_scan(str(lib), lib_root=str(lib), synchronous=True)
    assert err is None and scan["status"] == "success"

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {
            "scan_id": scan["id"],
            "action": "move",
            "groups": [],
            "visible_group_ids": [group["id"] for group in scan.get("groups") or []],
        },
        lib_root=str(lib),
    )
    assert err is None, err

    # Only the audit log write fails; the file operations are untouched.
    def refuse_log(*_args, **_kwargs):
        raise OSError("No space left on device")

    monkeypatch.setattr(maintenance, "_write_cleanup_log", refuse_log)

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None, f"a logging failure must not fail the cleanup: {err}"
    assert result.get("log_error"), "the missing undo record has to be reported"
    assert any("cannot be restored" in warning for warning in result.get("warnings") or [])
    # And the cleanup itself really did happen.
    assert not remove.exists(), "the duplicate should still have been moved"
    assert keep.exists()
