import os
import time

import pytest

from app import app_settings, emby_catalog, routes, subtitle_maintenance


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _settings(**overrides):
    settings = app_settings.default_settings()
    settings.update(overrides)
    return settings


def _reset_subtitles(monkeypatch, settings=None):
    subtitle_maintenance.subtitle_scans.clear()
    subtitle_maintenance.subtitle_plans.clear()
    subtitle_maintenance.subtitle_apply_runs.clear()
    monkeypatch.setattr(
        subtitle_maintenance.app_settings,
        "load_settings",
        lambda: dict(settings or _settings()),
    )


def test_subtitle_plan_quarantines_only_flagged_visible_srt(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    log_dir = tmp_path / "state" / "subtitle-logs"
    monkeypatch.setattr(subtitle_maintenance, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(subtitle_maintenance, "LOG_INDEX", str(log_dir / "index.json"))
    _write(lib / "Movie" / "Movie.mkv")
    expected = _write(lib / "Movie" / "Movie.eng.srt", b"english")
    flagged = _write(lib / "Movie" / "Movie.nno.srt", b"norwegian")
    scan = _scan(lib, monkeypatch)
    page, err = subtitle_maintenance.items_payload(scan["id"], status="language_review")
    assert err is None
    files = page["items"][0]["srt_files"]
    flagged_item = next(item for item in files if item["path"] == str(flagged))
    expected_item = next(item for item in files if item["path"] == str(expected))

    plan, err = subtitle_maintenance.build_action_plan(
        {
            "scan_id": scan["id"],
            "operation": "quarantine",
            "visible_file_ids": [item["id"] for item in files],
            "selected_file_ids": [flagged_item["id"]],
        },
        lib_root=str(lib),
    )
    assert err is None
    assert plan["file_count"] == 1
    assert expected_item["id"] not in {item["file_id"] for item in plan["files"]}
    subtitle_maintenance.subtitle_plans[plan["id"]]["files"][0]["emby_item_id"] = "movie-1"
    sync_calls = []

    def fake_sync(changes, **kwargs):
        sync_calls.append((changes, kwargs))
        return {"id": "sync-subtitles", "status": "failed", "retryable": True}

    monkeypatch.setattr(subtitle_maintenance.emby_sync, "sync_changes", fake_sync)

    run, err = subtitle_maintenance.start_action_apply(plan["id"], synchronous=True)

    assert err is None
    assert run["status"] == "success"
    assert expected.exists()
    assert not flagged.exists()
    assert (lib / ".vid2gif-subtitle-quarantine" / "Movie" / flagged.name).read_bytes() == b"norwegian"
    assert run["emby_sync"]["status"] == "failed"
    assert sync_calls[0][0] == [
        {
            "local_path": str(flagged),
            "update_type": "Deleted",
            "emby_item_id": "movie-1",
            "refresh_scope": "metadata",
        }
    ]


def test_subtitle_plan_rejects_offscreen_selection(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    first = _write(lib / "Movie" / "Movie.nno.srt")
    second = _write(lib / "Movie" / "Movie.spa.srt")
    scan = _scan(lib, monkeypatch)
    page, _err = subtitle_maintenance.items_payload(scan["id"], status="language_review")
    files = {item["path"]: item for item in page["items"][0]["srt_files"]}

    plan, err = subtitle_maintenance.build_action_plan(
        {
            "scan_id": scan["id"],
            "operation": "delete",
            "visible_file_ids": [files[str(first)]["id"]],
            "selected_file_ids": [files[str(second)]["id"]],
        },
        lib_root=str(lib),
    )

    assert plan is None
    assert err == "Selected subtitles must be visible on the current page"


def _scan(lib, monkeypatch, settings=None):
    _reset_subtitles(monkeypatch, settings=settings)
    scan, err = subtitle_maintenance.start_scan(
        str(lib),
        lib_root=str(lib),
        synchronous=True,
    )
    assert err is None
    assert scan["status"] == "success"
    return scan


def test_subtitle_filename_matching_and_language_parsing():
    stem = "Brazzers Exxtra - 2026-01-12 - Trespassing Her Yard [WEBDL-1080p]"

    assert subtitle_maintenance.subtitle_matches_video(
        f"{stem}.subgen.large-v3.eng.srt",
        stem,
    )
    assert subtitle_maintenance.subtitle_language_code(
        f"{stem}.subgen.large-v3.eng.srt",
        stem,
    ) == "eng"
    assert subtitle_maintenance.subtitle_language_code(
        f"{stem}.subgen.large-v3.nno.srt",
        stem,
    ) == "nno"
    assert subtitle_maintenance.subtitle_language_code(f"{stem}.en-US.srt", stem) == "en-us"
    assert subtitle_maintenance.subtitle_language_code(f"{stem}.eng.forced.srt", stem) == "eng"
    assert subtitle_maintenance.subtitle_language_code(f"{stem}.subgen.large-v3.srt", stem) is None
    assert subtitle_maintenance.subtitle_language_code(f"{stem}.srt", stem) is None


def test_subtitle_scan_flags_missing_non_english_and_unknown(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie One" / "Movie One.mkv")
    _write(lib / "Movie One" / "Movie One.subgen.large-v3.eng.srt")
    _write(lib / "Movie Two" / "Movie Two.mkv")
    bad = _write(lib / "Movie Two" / "Movie Two.subgen.large-v3.nno.srt")
    _write(lib / "Movie Three" / "Movie Three.mkv")
    _write(lib / "Movie Four" / "Movie Four.mkv")
    _write(lib / "Movie Four" / "Movie Four.srt")

    scan = _scan(lib, monkeypatch)

    assert scan["counts"]["scanned_video_count"] == 4
    assert scan["counts"]["ok_count"] == 1
    assert scan["counts"]["language_review_count"] == 1
    assert scan["counts"]["missing_count"] == 1
    assert scan["counts"]["unknown_count"] == 1
    page, err = subtitle_maintenance.items_payload(scan["id"], status="language_review")
    assert err is None
    assert page["total"] == 1
    assert page["items"][0]["srt_files"][0]["path"] == str(bad)
    assert page["items"][0]["srt_files"][0]["language_code"] == "nno"
    assert "items" not in subtitle_maintenance.public_scan(scan)


def test_subtitle_scan_accepts_configured_expected_language(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie.subgen.large-v3.spa.srt")

    scan = _scan(
        lib,
        monkeypatch,
        settings=_settings(subtitle_expected_languages=["eng", "spa"]),
    )

    assert scan["counts"]["ok_count"] == 1
    assert scan["counts"]["language_review_count"] == 0


def test_subtitle_scan_can_disable_subgen_language_detection(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie.subgen.large-v3.eng.srt")

    scan = _scan(
        lib,
        monkeypatch,
        settings=_settings(subtitle_subgen_detection=False),
    )

    assert scan["counts"]["unknown_count"] == 1
    assert scan["counts"]["ok_count"] == 0


def test_subtitle_scan_skips_quarantine_and_symlink_files(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie.eng.srt")
    _write(lib / ".vid2gif-duplicates" / "Old" / "Old.mkv")
    link = lib / "Movie" / "Linked.srt"
    try:
        link.symlink_to(lib / "Movie" / "Movie.eng.srt")
    except OSError:
        pass

    scan = _scan(lib, monkeypatch)

    assert scan["counts"]["scanned_video_count"] == 1
    assert scan["counts"]["ok_count"] == 1


def test_subtitle_items_payload_pages_searches_and_caps(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(120):
        folder = lib / f"Movie {index:03d}"
        _write(folder / f"Movie {index:03d}.mkv")
        _write(folder / f"Movie {index:03d}.subgen.large-v3.nno.srt")
    scan = _scan(lib, monkeypatch)

    page, err = subtitle_maintenance.items_payload(scan["id"], status="language_review", limit=999)
    searched, search_err = subtitle_maintenance.items_payload(scan["id"], status="all", q="Movie 119")

    assert err is None
    assert page["total"] == 120
    assert page["limit"] == subtitle_maintenance.ITEM_PAGE_MAX
    assert page["count"] == subtitle_maintenance.ITEM_PAGE_MAX
    assert page["large_result"] is True
    assert search_err is None
    assert searched["total"] == 1
    assert searched["items"][0]["name"] == "Movie 119.mkv"


def test_subtitle_scan_reuses_active_scan_and_can_cancel(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    _reset_subtitles(monkeypatch)

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(subtitle_maintenance.threading, "Thread", FakeThread)

    first, err = subtitle_maintenance.start_scan(str(lib), lib_root=str(lib))
    second, err2 = subtitle_maintenance.start_scan(str(lib), lib_root=str(lib))
    cancelled, cancel_err = subtitle_maintenance.cancel_scan(first["id"])

    assert err is None
    assert err2 is None
    assert second["id"] == first["id"]
    assert cancel_err is None
    assert cancelled["status"] == "cancelled"


def test_subtitle_routes_reject_invalid_paths_and_page_results(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie.subgen.large-v3.nno.srt")
    sibling.mkdir()
    _reset_subtitles(monkeypatch)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    client = routes.app.test_client()

    invalid = client.post(
        "/api/maintenance/subtitles/scan",
        json={"path": str(sibling), "synchronous": True},
    )
    scan_res = client.post(
        "/api/maintenance/subtitles/scan",
        json={"path": str(lib), "synchronous": True},
    )
    scan = scan_res.get_json()["scan"]
    status_res = client.get(
        "/api/maintenance/subtitles/status",
        query_string={"scan_id": scan["id"]},
    )
    items_res = client.get(
        "/api/maintenance/subtitles/items",
        query_string={"scan_id": scan["id"], "status": "language_review"},
    )
    missing_res = client.get(
        "/api/maintenance/subtitles/items",
        query_string={"scan_id": "missing"},
    )

    assert invalid.status_code == 400
    assert invalid.get_json()["error"] == "Path not found"
    assert scan_res.status_code == 200
    assert scan_res.is_json
    assert "items" not in scan
    assert status_res.status_code == 200
    assert "items" not in status_res.get_json()["scan"]
    assert items_res.status_code == 200
    assert items_res.get_json()["total"] == 1
    assert missing_res.status_code == 404


def test_subtitle_scan_route_rejects_symlink(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    target = tmp_path / "target"
    lib.mkdir()
    target.mkdir()
    link = lib / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")
    _reset_subtitles(monkeypatch)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/maintenance/subtitles/scan",
        json={"path": str(link), "synchronous": True},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Path not found"


def test_subtitle_status_retains_latest_persisted_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    scan = _scan(lib, monkeypatch)

    monkeypatch.setattr(subtitle_maintenance, "SCAN_MAX_AGE_SECONDS", 1)
    scan["_finished_ts"] = time.time() - 2
    payload, err = subtitle_maintenance.status_payload(scan["id"])

    assert err is None
    assert payload["scan"]["id"] == scan["id"]


def test_subtitle_ui_assets_render():
    client = routes.app.test_client()

    res = client.get("/maintenance")
    html = res.get_data(as_text=True)
    script_path = os.path.join(os.path.dirname(routes.app.root_path), "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()

    assert res.status_code == 200
    assert 'data-maint-tab-hash="subtitles"' in html
    assert 'id="pane-subtitles"' in html
    assert 'id="subtitleScanButton"' in html
    assert 'id="subtitleItemStatus"' in html
    assert 'value="index_mismatch"' in html
    assert 'id="subtitleSearch"' in html
    assert 'id="subtitleAction"' in html
    assert 'id="subtitlePlanButton"' in html
    assert 'id="subtitleApplyButton"' in html
    assert "fetch('/api/maintenance/subtitles/scan'" in script
    assert "/api/maintenance/subtitles/items?" in script
    assert "fetch('/api/maintenance/subtitles/cancel'" in script
    assert "fetch('/api/maintenance/subtitles/plan'" in script
    assert "fetch('/api/maintenance/subtitles/apply'" in script
    assert "/api/maintenance/subtitles/apply/status?apply_id=" in script
    assert "escapeHtml(item.detail || '')" in script
    assert "subtitleStreamsCell" in script
    assert "Playback deferred" in script


def test_subtitle_scan_and_plan_carry_parent_emby_id(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie.nno.srt")
    catalog = emby_catalog._build_catalog(
        [{"Id": "movie-1", "Name": "Movie", "Type": "Movie", "Path": str(video)}],
        {"Id": "server"},
        emby_catalog.configuration_fingerprint({}),
    )
    monkeypatch.setattr(
        subtitle_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 0, catalog_item_count=1)),
    )
    scan = _scan(lib, monkeypatch)
    item = scan["items"][0]
    child = item["srt_files"][0]
    assert item["emby_item_id"] == "movie-1"
    assert child["emby_parent_item_id"] == "movie-1"

    plan, err = subtitle_maintenance.build_action_plan(
        {
            "scan_id": scan["id"],
            "operation": "delete",
            "visible_file_ids": [child["id"]],
            "selected_file_ids": [child["id"]],
        },
        lib_root=str(lib),
    )
    assert err is None
    assert plan["files"][0]["emby_item_id"] == "movie-1"


def test_embedded_expected_stream_satisfies_subtitle_health(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    catalog = emby_catalog._build_catalog(
        [
            {
                "Id": "movie-1",
                "Name": "Movie",
                "Type": "Movie",
                "Path": str(video),
                "MediaSources": [
                    {
                        "Id": "source-1",
                        "Path": str(video),
                        "MediaStreams": [
                            {
                                "Type": "Subtitle",
                                "Index": 2,
                                "Language": "eng",
                                "Codec": "ass",
                                "IsForced": True,
                                "IsHearingImpaired": True,
                            }
                        ],
                    }
                ],
            }
        ],
        {"Id": "server"},
        emby_catalog.configuration_fingerprint({}),
    )
    summary = emby_catalog.known_matches_summary({}, 1, catalog_item_count=1, server_id="server")
    monkeypatch.setattr(
        subtitle_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, summary),
    )

    scan = _scan(lib, monkeypatch)
    item = scan["items"][0]

    assert item["status"] == "ok"
    assert item["subtitle_count"] == 0
    assert item["emby_subtitle_stream_count"] == 1
    assert item["embedded_subtitle_count"] == 1
    assert item["emby_subtitle_streams"][0]["is_forced"] is True
    assert item["emby_subtitle_streams"][0]["is_hearing_impaired"] is True
    assert scan["emby_streams"]["status"] == "complete"
    assert scan["emby_streams"]["stream_count"] == 1
    assert scan["counts"]["missing_count"] == 0


def test_emby_only_unexpected_stream_is_reviewable_but_not_actionable(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    catalog = emby_catalog._build_catalog(
        [
            {
                "Id": "movie-1",
                "Path": str(video),
                "MediaSources": [
                    {
                        "Id": "source",
                        "Path": str(video),
                        "MediaStreams": [{"Type": "Subtitle", "Index": 3, "Language": "spa"}],
                    }
                ],
            }
        ],
        {"Id": "server"},
        "fingerprint",
    )
    monkeypatch.setattr(
        subtitle_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 1)),
    )

    scan = _scan(lib, monkeypatch)
    item = scan["items"][0]

    assert item["status"] == "language_review"
    assert item["srt_files"] == []
    assert item["emby_subtitle_streams"][0]["language_code"] == "spa"


def test_unknown_emby_stream_language_is_classified_unknown(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    catalog = emby_catalog._build_catalog(
        [
            {
                "Id": "movie-1",
                "Path": str(video),
                "MediaSources": [
                    {
                        "Id": "source",
                        "Path": str(video),
                        "MediaStreams": [{"Type": "Subtitle", "Index": 1, "Language": "und"}],
                    }
                ],
            }
        ],
        {},
        "fingerprint",
    )
    monkeypatch.setattr(
        subtitle_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 1)),
    )

    scan = _scan(lib, monkeypatch)

    assert scan["items"][0]["status"] == "unknown"
    assert scan["items"][0]["emby_subtitle_streams"][0]["language_code"] == ""


def test_stream_summary_becomes_stale_after_emby_settings_change():
    summary = subtitle_maintenance._stream_summary("complete", "complete", {"emby_url": "http://one"})
    public = subtitle_maintenance.public_stream_summary(summary, {"emby_url": "http://two"})
    assert public["status"] == "stale"
    assert "rescan" in public["message"].lower()


def test_external_stream_matching_and_index_mismatch_filter(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    expected = _write(lib / "Movie" / "Movie.eng.srt")
    unexpected = _write(lib / "Movie" / "Movie.nno.srt")
    catalog = emby_catalog._build_catalog(
        [
            {
                "Id": "movie-1",
                "Path": str(video),
                "MediaSources": [
                    {
                        "Id": "source",
                        "Path": str(video),
                        "MediaStreams": [
                            {
                                "Type": "Subtitle",
                                "Index": 4,
                                "Language": "eng",
                                "IsExternal": True,
                                "Path": str(expected),
                            }
                        ],
                    }
                ],
            }
        ],
        {"Id": "server"},
        "fingerprint",
    )
    monkeypatch.setattr(
        subtitle_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 1)),
    )

    scan = _scan(lib, monkeypatch)
    item = scan["items"][0]
    by_path = {sidecar["path"]: sidecar for sidecar in item["srt_files"]}
    page, err = subtitle_maintenance.items_payload(scan["id"], status="index_mismatch")

    assert err is None
    assert by_path[str(expected)]["emby_stream_match_status"] == "matched"
    assert by_path[str(expected)]["emby_stream_index"] == 4
    assert by_path[str(unexpected)]["emby_stream_match_status"] == "unmatched"
    assert item["emby_index_status"] == "mismatch"
    assert scan["emby_streams"]["index_mismatch_count"] == 1
    assert page["total"] == 1


def test_legacy_subtitle_cache_reports_streams_not_checked():
    public = subtitle_maintenance.public_scan(
        {"id": "legacy", "status": "success", "counts": {}, "settings": {}}
    )
    assert public["emby_streams"]["status"] == "not_checked"


def test_subtitle_cleanup_defers_active_parent_without_sync(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    flagged = _write(lib / "Movie" / "Movie.nno.srt")
    scan = _scan(lib, monkeypatch)
    child = scan["items"][0]["srt_files"][0]

    def active(targets, **kwargs):
        return {
            "status": "active",
            "checked_at": "now",
            "active_session_count": 1,
            "active_item_count": 1,
            "target_count": len(targets),
            "clear_count": 0,
            "active_count": len(targets),
            "unverified_count": 0,
            "deferred_count": len(targets),
            "message": "Active",
            "_target_statuses": {target["id"]: "active" for target in targets},
        }

    monkeypatch.setattr(subtitle_maintenance.emby_playback, "check_targets", active)
    monkeypatch.setattr(
        subtitle_maintenance.emby_sync,
        "sync_changes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync should not run")),
    )
    notification_calls = []
    monkeypatch.setattr(
        subtitle_maintenance.emby_notifications,
        "notify_maintenance",
        lambda *args, **kwargs: notification_calls.append((args, kwargs))
        or {"id": "notice", "status": "success", "message": "accepted"},
    )
    plan, err = subtitle_maintenance.build_action_plan(
        {
            "scan_id": scan["id"],
            "operation": "delete",
            "visible_file_ids": [child["id"]],
            "selected_file_ids": [child["id"]],
        },
        lib_root=str(lib),
    )
    run, apply_err = subtitle_maintenance.start_action_apply(plan["id"], synchronous=True)

    assert err is None
    assert apply_err is None
    assert plan["emby_playback"]["deferred_count"] == 1
    assert run["status"] == "success"
    assert run["applied_count"] == 0
    assert run["refused_count"] == 0
    assert run["deferred_count"] == 1
    assert run["result"]["records"][0]["status"] == "deferred"
    assert notification_calls[0][1]["deferred_count"] == 1
    assert run["emby_notification"]["id"] == "notice"
    assert flagged.exists()
