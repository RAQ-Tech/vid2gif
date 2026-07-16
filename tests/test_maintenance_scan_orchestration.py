import os
import time

from app import config
from app import maintenance_scan_orchestrator as orchestrator
from app import maintenance_scan_store
from app import poster_maintenance


def _write(path, data=b"data"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_scan_cache_recovers_last_known_good_backup(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    video = _write(library / "Movie" / "Movie.mkv")
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    scan = {"id": "first", "path": str(library), "status": "success", "finished_at": "2026-01-01T00:00:00+00:00", "items": [], "settings": {"excluded_folders": {"trailers", "trailer"}}}

    assert maintenance_scan_store.persist_success("duplicates", "duplicates", scan, str(library))
    scan["id"] = "second"
    assert maintenance_scan_store.persist_success("duplicates", "duplicates", scan, str(library))
    maintenance_scan_store._path("duplicates")
    with open(maintenance_scan_store._path("duplicates"), "wb") as handle:
        handle.write(b"corrupt")

    restored = maintenance_scan_store.restore_scan("duplicates")

    assert restored["id"] == "first"
    assert restored["_restored"] is True
    assert restored["settings"]["excluded_folders"] == ["trailer", "trailers"]
    assert video.exists()


def test_freshness_ignores_unrelated_files_and_detects_relevant_changes(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    video = _write(library / "Movie" / "Movie.mkv", b"video")
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    scan = {"id": "subtitles-1", "path": str(library), "status": "success", "finished_at": "2026-01-01T00:00:00+00:00", "items": []}
    assert maintenance_scan_store.persist_success("subtitles", "subtitles", scan, str(library))

    _write(library / "Movie" / "notes.txt", b"unrelated")
    unchanged = maintenance_scan_store._check_cache("subtitles", force=True)
    assert unchanged["status"] == "unchanged"

    _write(library / "Movie" / "Movie.en.srt", b"subtitle")
    changed = maintenance_scan_store._check_cache("subtitles", force=True)
    assert changed["status"] == "changed"
    assert changed["added"] == 1
    allowed, error = maintenance_scan_store.action_allowed("subtitles", scan["id"])
    assert allowed is False
    assert "Rescan" in error
    assert video.exists()


def test_partial_scan_reconciliation_preserves_unrelated_freshness(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    video = _write(library / "Movie" / "Movie.mkv", b"video")
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    scan = {
        "id": "duplicates-partial",
        "path": str(library),
        "status": "success",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "groups": [],
    }
    assert maintenance_scan_store.persist_success(
        "duplicates", "duplicates", scan, str(library)
    )
    _write(library / "Other" / "Other.srt", b"external change")
    assert maintenance_scan_store._check_cache("duplicates", force=True)["status"] == "changed"

    scan["reconciled"] = True
    assert maintenance_scan_store.update_persisted_scan(
        "duplicates",
        scan,
        str(library),
        accepted_paths=[str(video)],
    )
    payload = maintenance_scan_store.load_latest("duplicates")

    assert payload["scan"]["reconciled"] is True
    assert payload["freshness"]["status"] == "changed"
    assert maintenance_scan_store.action_allowed("duplicates", scan["id"])[0] is False
    assert maintenance_scan_store.library_root_allowed("duplicates", scan["id"])[0] is True


def test_scan_cache_is_not_restored_or_actionable_after_library_mount_changes(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    _write(library / "Movie" / "Movie.mkv", b"old-library-video")
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    scan = {
        "id": "duplicates-old-mount",
        "path": str(library),
        "status": "success",
        "finished_at": "2026-01-01T00:00:00+00:00",
        "items": [],
    }
    assert maintenance_scan_store.persist_success(
        "duplicates", "duplicates", scan, str(library)
    )

    archived = tmp_path / "library-old"
    library.rename(archived)
    _write(library / "Different" / "Different.mkv", b"new-library-video")

    assert maintenance_scan_store.restore_scan("duplicates") is None
    allowed, error = maintenance_scan_store.action_allowed(
        "duplicates", scan["id"]
    )
    assert allowed is False
    assert "mounted library changed" in error
    freshness = maintenance_scan_store._check_cache("duplicates", force=True)
    assert freshness["status"] == "changed"
    assert freshness["library_root_changed"] is True


def test_scan_all_is_sequential_and_continues_after_failure(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    monkeypatch.setattr(orchestrator, "_current", None)
    calls = []

    def start_for(area, status="success"):
        def start(path):
            calls.append(area)
            return {"id": area, "path": path, "status": status}, None
        return start

    def status_for(area, status="success"):
        return lambda scan_id: ({"scan": {"id": scan_id, "status": status, "progress_percent": 100, "finished_at": "2026-01-01T00:00:00+00:00"}}, None)

    monkeypatch.setattr(orchestrator.dashboard, "start_library_scan", start_for("overview"))
    monkeypatch.setattr(orchestrator, "_library_status", status_for("overview"))
    monkeypatch.setattr(orchestrator.maintenance, "start_duplicate_scan", start_for("duplicates", "failed"))
    monkeypatch.setattr(orchestrator.maintenance, "status_payload", status_for("duplicates", "failed"))
    def start_subtitles(path, mode="missing"):
        calls.append(f"subtitles_{mode}")
        return {"id": f"subtitles_{mode}", "path": path, "status": "success"}, None

    monkeypatch.setattr(orchestrator.subtitle_maintenance, "start_scan", start_subtitles)
    monkeypatch.setattr(orchestrator.subtitle_maintenance, "status_payload", status_for("subtitles"))

    run, error = orchestrator.start(str(library), areas=["overview", "duplicates", "subtitles"], synchronous=True)

    assert error is None
    assert calls == ["overview", "duplicates", "subtitles_missing", "subtitles_coverage"]
    assert run["status"] == "complete_with_issues"
    assert run["areas"]["subtitles_missing"]["status"] == "success"
    assert run["areas"]["subtitles_coverage"]["status"] == "success"


def test_full_scan_plan_expands_each_area_into_recorded_operation_stages():
    assert orchestrator._step_plan(
        ["duplicates", "video_previews_quality", "posters"]
    ) == [
        {"workflow": "duplicate_scan.discovery"},
        {"workflow": "duplicate_scan.analysis"},
        {"workflow": "duplicate_scan.emby"},
        {"workflow": "video_preview_quality_scan.catalog"},
        {"workflow": "video_preview_quality_scan.analysis"},
        {"workflow": "video_preview_quality_scan.emby"},
        {"workflow": "poster_scan.filesystem"},
        {"workflow": "poster_scan.emby"},
    ]


def test_orchestrator_cancel_stops_active_scan(monkeypatch, tmp_path):
    state = tmp_path / "state"
    library = tmp_path / "library"
    library.mkdir()
    monkeypatch.setattr(config, "STATE_ROOT", str(state))
    monkeypatch.setattr(config, "LIB_ROOT", str(library))
    monkeypatch.setattr(orchestrator, "_current", None)
    cancelled = {"value": False}

    monkeypatch.setattr(
        orchestrator.dashboard,
        "start_library_scan",
        lambda path: ({"id": "overview-active", "path": path, "status": "running"}, None),
    )
    monkeypatch.setattr(
        orchestrator,
        "_library_status",
        lambda scan_id: (
            {"scan": {"id": scan_id, "status": "cancelled" if cancelled["value"] else "running", "progress_percent": 100 if cancelled["value"] else 25}},
            None,
        ),
    )
    monkeypatch.setattr(
        orchestrator.dashboard,
        "cancel_library_scan",
        lambda: (cancelled.update(value=True) or {"status": "cancelling"}, None),
    )

    run, error = orchestrator.start(str(library), areas=["overview"])
    assert error is None
    assert run["active"] is True
    cancelled_run, error = orchestrator.cancel()
    assert error is None
    assert cancelled_run["cancel_requested"] is True
    for _ in range(30):
        if not orchestrator.status()["active"]:
            break
        time.sleep(0.02)
    assert orchestrator.status()["status"] == "cancelled"


def test_poster_analysis_is_non_mutating_and_page_scoped(monkeypatch, tmp_path):
    library = tmp_path / "library"
    background = _write(library / "Movie" / "Movie-background.jpg", b"background")
    poster = _write(library / "Movie" / "Movie-poster.jpg", b"portrait")
    monkeypatch.setattr(poster_maintenance, "_poster_cache_loaded", True)
    poster_maintenance.poster_scans.clear()
    poster_maintenance.poster_plans.clear()

    def dimensions(path):
        return {"width": 1920, "height": 1080, "landscape": True} if "background" in os.path.basename(path) else {"width": 1000, "height": 1500, "landscape": False}

    monkeypatch.setattr(poster_maintenance, "_probe_image_dimensions", dimensions)
    monkeypatch.setattr(poster_maintenance.maintenance_scan_store, "persist_success", lambda *args, **kwargs: True)
    scan, error = poster_maintenance.start_poster_scan(str(library), synchronous=True, lib_root=str(library))

    assert error is None
    assert scan["status"] == "success"
    assert scan["counts"]["eligible_count"] == 1
    assert background.read_bytes() == b"background"
    assert poster.read_bytes() == b"portrait"
    assert not (library / "Movie" / "Movie-poster-backup.jpg").exists()

    item_id = scan["items"][0]["id"]
    plan, error = poster_maintenance.build_poster_plan(
        {"scan_id": scan["id"], "visible_item_ids": [item_id], "item_ids": [item_id]},
        lib_root=str(library),
    )
    assert error is None
    assert plan["file_count"] == 1

    poster.write_bytes(b"changed-after-review")
    apply_run, error = poster_maintenance.start_poster_apply(
        plan["id"], synchronous=True, lib_root=str(library)
    )
    assert error is None
    assert apply_run["status"] == "complete_with_issues"
    assert apply_run["updated_count"] == 0
    assert poster.read_bytes() == b"changed-after-review"
    assert not (library / "Movie" / "Movie-poster-backup.jpg").exists()

    plan, error = poster_maintenance.build_poster_plan(
        {"scan_id": scan["id"], "visible_item_ids": ["other"], "item_ids": [item_id]},
        lib_root=str(library),
    )
    assert plan is None
    assert "visible page" in error
