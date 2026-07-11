import os
import subprocess
import time

import pytest

from app import app_settings, emby_catalog, maintenance, routes


def _reset_maintenance(monkeypatch, metadata_by_name=None, settings_overrides=None):
    maintenance.duplicate_scans.clear()
    maintenance.cleanup_plans.clear()
    maintenance.duplicate_apply_runs.clear()
    metadata_by_name = metadata_by_name or {}
    settings = app_settings.default_settings()
    settings.update(settings_overrides or {})

    def fake_probe(path):
        return metadata_by_name.get(os.path.basename(path), {})

    monkeypatch.setattr(maintenance, "probe_video_metadata", fake_probe)
    monkeypatch.setattr(maintenance.app_settings, "load_settings", lambda: dict(settings))


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _scan(lib, target, monkeypatch, metadata_by_name=None, settings_overrides=None):
    _reset_maintenance(monkeypatch, metadata_by_name, settings_overrides)
    scan, err = maintenance.start_duplicate_scan(
        str(target),
        lib_root=str(lib),
        synchronous=True,
    )
    assert err is None
    assert scan["status"] == "success"
    return scan


def _write_duplicate_pair(lib, folder_name, base_name=None):
    folder = lib / folder_name
    base_name = base_name or folder_name
    keep = _write(folder / f"{base_name}.1080p.mkv", b"a" * 200)
    remove = _write(folder / f"{base_name}.720p.mkv", b"b" * 100)
    return keep, remove


def _visible_group_ids(scan):
    return [group["id"] for group in scan.get("groups") or []]


def test_duplicate_name_normalization_removes_quality_tags():
    assert maintenance.normalize_duplicate_name("Movie.1080p.WEB-DL.x265") == "movie"
    assert maintenance.normalize_duplicate_name("Movie 720p BluRay H264") == "movie"
    assert maintenance.normalize_duplicate_name("Movie [WEBDL-2160p](1)") == "movie"
    assert maintenance.normalize_duplicate_name("Movie [WEBDL-2160p](2)") == "movie"
    assert (
        maintenance.normalize_duplicate_name("Show.S01E01.1080p")
        != maintenance.normalize_duplicate_name("Show.S01E02.1080p")
    )


def test_duplicate_scan_groups_four_copy_suffixes_together(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    for name in (
        "Movie [WEBDL-1080p].mp4",
        "Movie [WEBDL-2160p].mp4",
        "Movie [WEBDL-2160p](1).mp4",
        "Movie [WEBDL-2160p](2).mp4",
    ):
        _write(movie / name, b"x" * 100)

    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            "Movie [WEBDL-1080p].mp4": {"width": 1920, "height": 1080},
            "Movie [WEBDL-2160p].mp4": {"width": 3840, "height": 2160},
            "Movie [WEBDL-2160p](1).mp4": {"width": 3840, "height": 2160},
            "Movie [WEBDL-2160p](2).mp4": {"width": 3840, "height": 2160},
        },
    )

    assert len(scan["groups"]) == 1
    assert len(scan["groups"][0]["videos"]) == 4
    keeper = next(
        video for video in scan["groups"][0]["videos"]
        if video["id"] == scan["groups"][0]["recommended_keep_id"]
    )
    assert keeper["name"] == "Movie [WEBDL-2160p].mp4"


def test_group_projection_reselects_copy_when_keeper_changes(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    original = _write(movie / "Movie [WEBDL-2160p].mp4", b"video")
    copied = _write(movie / "Movie [WEBDL-2160p](1).mp4", b"video")
    copied_srt = _write(movie / "Movie [WEBDL-2160p](1).eng.srt", b"subtitle")
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            original.name: {"width": 3840, "height": 2160},
            copied.name: {"width": 3840, "height": 2160},
        },
    )
    group = scan["groups"][0]

    payload, err = maintenance.group_payload(scan["id"], group["id"], keep_video_id=group["recommended_keep_id"])

    assert err is None
    projected_copy = next(video for video in payload["group"]["videos"] if video["name"] == copied.name)
    assert projected_copy["default_selected"] is True
    projected_srt = next(item for item in projected_copy["accessories"] if item["path"] == str(copied_srt))
    assert projected_srt["default_selected"] is True


def test_duplicate_scan_groups_by_nfo_provider_id(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    first = _write(movie / "Odd Name 1080p.mp4", b"x" * 100)
    second = _write(movie / "Different Name 2160p.mp4", b"x" * 200)
    _write(first.with_suffix(".nfo"), b'<movie><uniqueid type="theporndb">scenes/123</uniqueid></movie>')
    _write(second.with_suffix(".nfo"), b'<movie><uniqueid type="theporndb">scenes/123</uniqueid></movie>')

    scan = _scan(lib, lib, monkeypatch)

    assert len(scan["groups"]) == 1
    assert {video["name"] for video in scan["groups"][0]["videos"]} == {first.name, second.name}


def test_duplicate_scan_skips_trailer_folders(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    trailers = movie / "trailers"
    _write(movie / "Movie.1080p.mp4", b"x")
    _write(movie / "Movie.2160p.mp4", b"x")
    _write(trailers / "Movie Trailer.1080p.mp4", b"x")
    _write(trailers / "Movie Trailer.2160p.mp4", b"x")

    scan = _scan(lib, lib, monkeypatch)

    assert scan["scanned_video_count"] == 2
    assert len(scan["groups"]) == 1


def test_duplicate_scan_groups_same_folder_matches_and_ranks_quality(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.WEB-DL.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.WEB-DL.mkv", b"b" * 100)
    sidecar = _write(movie / "Movie.720p.WEB-DL.en.srt", b"subtitle")
    folder_poster = _write(movie / "poster.jpg", b"poster")
    _write(movie / "Show.S01E01.1080p.mkv", b"e1")
    _write(movie / "Show.S01E02.1080p.mkv", b"e2")

    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            keep.name: {"width": 1920, "height": 1080, "bit_rate": 8_000_000},
            remove.name: {"width": 1280, "height": 720, "bit_rate": 4_000_000},
        },
    )

    assert scan["scanned_video_count"] == 4
    assert len(scan["groups"]) == 1
    group = scan["groups"][0]
    assert group["recommended_keep_id"] == maintenance._path_id(str(keep), str(lib))
    removed_video = next(video for video in group["videos"] if video["path"] == str(remove))
    assert [item["path"] for item in removed_video["accessories"]] == [str(sidecar)]
    assert str(folder_poster) not in [
        item["path"]
        for video in group["videos"]
        for item in video["accessories"]
    ]


def test_quarantine_destination_preserves_library_relative_path(tmp_path):
    lib = tmp_path / "library"
    source = _write(lib / "Movie" / "Movie.720p.mkv")

    dest = maintenance.quarantine_destination(str(source), str(lib), "scan1")

    assert maintenance.path_is_under(dest, str(lib))
    assert dest.endswith(
        os.path.join(
            ".vid2gif-duplicates",
            "Movie",
            "Movie.720p.mkv",
        )
    )


def test_cleanup_plan_moves_duplicate_video_and_renames_unmatched_accessory(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    sidecar = _write(movie / "Movie.720p.en.srt", b"subtitle")
    poster = _write(movie / "poster.jpg", b"poster")
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            keep.name: {"width": 1920, "height": 1080, "bit_rate": 8_000_000},
            remove.name: {"width": 1280, "height": 720, "bit_rate": 4_000_000},
        },
    )

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    assert plan["file_count"] == 2
    assert {item["operation"] for item in plan["files"]} == {"move", "rename"}
    assert plan["total_size_bytes"] == remove.stat().st_size
    sync_calls = []

    def fake_sync(changes, **kwargs):
        sync_calls.append((changes, kwargs))
        return {"id": "sync-duplicates", "status": "failed", "retryable": True}

    monkeypatch.setattr(maintenance.emby_sync, "sync_changes", fake_sync)

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 2
    assert keep.exists()
    assert poster.exists()
    assert not remove.exists()
    assert not sidecar.exists()
    assert (movie / "Movie.1080p.en.srt").read_bytes() == b"subtitle"
    quarantine = lib / ".vid2gif-duplicates" / "Movie" / "Movie.720p.mkv"
    assert quarantine.is_file()
    assert result["log"]["id"].endswith(".jsonl")
    assert result["emby_sync"]["status"] == "failed"
    assert maintenance._public_apply_result(result)["emby_sync"]["id"] == "sync-duplicates"
    changes = sync_calls[0][0]
    assert {(item["local_path"], item["update_type"]) for item in changes} == {
        (str(remove), "Deleted"),
        (str(sidecar), "Deleted"),
        (str(movie / "Movie.1080p.en.srt"), "Created"),
    }
    assert str(quarantine) not in {item["local_path"] for item in changes}


def test_cleanup_plan_moves_equivalent_accessory(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    keeper_sidecar = _write(movie / "Movie.1080p.en.srt", b"keeper")
    duplicate_sidecar = _write(movie / "Movie.720p.en.srt", b"duplicate")
    scan = _scan(lib, lib, monkeypatch)

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    sidecar_item = next(item for item in plan["files"] if item["source_path"] == str(duplicate_sidecar))
    assert sidecar_item["operation"] == "move"

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert keeper_sidecar.exists()
    assert not duplicate_sidecar.exists()
    assert (lib / ".vid2gif-duplicates" / "Movie" / duplicate_sidecar.name).is_file()


def test_cleanup_plan_uses_custom_move_root(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    move_root = lib / "_quarantine"
    movie = lib / "Movie"
    _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        settings_overrides={"duplicate_move_root": str(move_root)},
    )

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )

    assert err is None
    assert plan["move_root"] == str(move_root)
    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])
    assert err is None
    assert not remove.exists()
    assert (move_root / "Movie" / remove.name).is_file()


def test_cleanup_move_creates_missing_destination_folders(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "XXX" / "Blacked" / "Bratty Bitches"
    _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    scan = _scan(lib, lib, monkeypatch)
    dest = lib / ".vid2gif-duplicates" / "XXX" / "Blacked" / "Bratty Bitches" / remove.name
    assert not dest.parent.exists()

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 1
    assert not remove.exists()
    assert dest.read_bytes() == b"b" * 100


def test_cleanup_move_reuses_existing_destination_folders(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "XXX" / "Blacked" / "Bratty Bitches"
    _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    dest_dir = lib / ".vid2gif-duplicates" / "XXX" / "Blacked" / "Bratty Bitches"
    dest_dir.mkdir(parents=True)
    scan = _scan(lib, lib, monkeypatch)

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 1
    assert not remove.exists()
    assert (dest_dir / remove.name).read_bytes() == b"b" * 100


def test_cleanup_move_refuses_existing_destination_file_and_continues(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    first = lib / "First"
    second = lib / "Second"
    _write(first / "First.1080p.mkv", b"a" * 200)
    first_remove = _write(first / "First.720p.mkv", b"b" * 100)
    _write(second / "Second.1080p.mkv", b"c" * 200)
    second_remove = _write(second / "Second.720p.mkv", b"d" * 100)
    conflict = _write(
        lib / ".vid2gif-duplicates" / "First" / first_remove.name,
        b"existing",
    )
    second_dest = lib / ".vid2gif-duplicates" / "Second" / second_remove.name
    scan = _scan(lib, lib, monkeypatch)

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 1
    assert result["refused_count"] == 1
    assert result["refused"][0]["reason"] == "Destination already exists"
    assert first_remove.exists()
    assert conflict.read_bytes() == b"existing"
    assert not second_remove.exists()
    assert second_dest.read_bytes() == b"d" * 100


def test_cleanup_plan_rejects_move_root_outside_library(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    outside = tmp_path / "outside"
    movie = lib / "Movie"
    _write(movie / "Movie.1080p.mkv", b"a" * 200)
    _write(movie / "Movie.720p.mkv", b"b" * 100)
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        settings_overrides={"duplicate_move_root": str(outside)},
    )

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )

    assert plan is None
    assert err == "Move destination must be inside the mounted library root"


def test_cleanup_log_records_old_and_new_paths(monkeypatch, tmp_path):
    log_root = tmp_path / "logs"
    monkeypatch.setattr(maintenance, "MAINTENANCE_LOG_DIR", str(log_root))
    monkeypatch.setattr(maintenance, "MAINTENANCE_LOG_INDEX", str(log_root / "index.json"))
    lib = tmp_path / "library"
    movie = lib / "Movie"
    _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    scan = _scan(lib, lib, monkeypatch)
    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])
    log, err = maintenance.read_duplicate_cleanup_log(result["log"]["id"])

    assert err is None
    assert remove.name in log["content"]
    assert "old_path" in log["content"]
    assert "new_path" in log["content"]


def test_cleanup_plan_delete_can_include_only_selected_file(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    sidecar = _write(movie / "Movie.720p.en.srt", b"subtitle")
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            keep.name: {"width": 1920, "height": 1080},
            remove.name: {"width": 1280, "height": 720},
        },
    )
    group = scan["groups"][0]
    remove_id = maintenance._path_id(str(remove), str(lib))

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {
            "scan_id": scan["id"],
            "action": "delete",
            "visible_group_ids": _visible_group_ids(scan),
            "groups": [
                {
                    "id": group["id"],
                    "keep_video_id": maintenance._path_id(str(keep), str(lib)),
                    "include_file_ids": [remove_id],
                }
            ],
        },
        lib_root=str(lib),
    )
    assert err is None
    assert plan["file_count"] == 1

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 1
    assert keep.exists()
    assert not remove.exists()
    assert sidecar.exists()


def test_apply_refuses_file_that_changed_after_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    scan = _scan(
        lib,
        lib,
        monkeypatch,
        {
            keep.name: {"width": 1920, "height": 1080},
            remove.name: {"width": 1280, "height": 720},
        },
    )
    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "delete", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None
    remove.write_bytes(b"changed after scan")

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 0
    assert result["refused_count"] == 1
    assert result["refused"][0]["reason"] == "File changed after scan"
    assert remove.exists()


def test_maintenance_scan_route_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/maintenance/duplicates/scan",
        json={"path": str(sibling), "synchronous": True},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Path not found"


def test_maintenance_scan_route_rejects_symlink(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    target = tmp_path / "target"
    lib.mkdir()
    target.mkdir()
    link = lib / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/maintenance/duplicates/scan",
        json={"path": str(link), "synchronous": True},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Path not found"


def test_maintenance_scan_and_status_routes_return_json_settings(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie 2160p.mkv", b"keep")
    remove = _write(movie / "Movie 1080p.mkv", b"remove")
    _reset_maintenance(
        monkeypatch,
        {
            keep.name: {"width": 3840, "height": 2160},
            remove.name: {"width": 1920, "height": 1080},
        },
        settings_overrides={"duplicate_excluded_folders": ["trailers", "trailer"]},
    )
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    client = routes.app.test_client()

    res = client.post(
        "/api/maintenance/duplicates/scan",
        json={"path": str(movie), "synchronous": True},
    )

    assert res.status_code == 200
    assert res.is_json
    scan = res.get_json()["scan"]
    assert scan["status"] == "success"
    assert scan["settings"]["excluded_folders"] == ["trailer", "trailers"]
    assert isinstance(scan["settings"]["excluded_folders"], list)
    assert "groups" not in scan

    status = client.get(
        "/api/maintenance/duplicates/status",
        query_string={"scan_id": scan["id"]},
    )
    assert status.status_code == 200
    assert status.is_json
    status_scan = status.get_json()["scan"]
    assert status_scan["settings"]["excluded_folders"] == ["trailer", "trailers"]
    assert "groups" not in status_scan

    groups_res = client.get(
        "/api/maintenance/duplicates/groups",
        query_string={"scan_id": scan["id"], "offset": 0, "limit": 1},
    )
    assert groups_res.status_code == 200
    groups = groups_res.get_json()
    assert groups["total"] == 1
    assert groups["count"] == 1
    assert "videos" not in groups["groups"][0]

    detail_res = client.get(
        f"/api/maintenance/duplicates/groups/{groups['groups'][0]['id']}",
        query_string={"scan_id": scan["id"]},
    )
    assert detail_res.status_code == 200
    detail = detail_res.get_json()["group"]
    assert len(detail["videos"]) == 2


def test_duplicate_groups_payload_caps_large_result_pages(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(120):
        _write_duplicate_pair(lib, f"Movie {index:03d}", f"Movie {index:03d}")
    scan = _scan(lib, lib, monkeypatch)

    status, err = maintenance.status_payload(scan["id"])
    payload, err = maintenance.groups_payload(scan["id"], offset=0, limit=999)

    assert err is None
    assert "groups" not in status["scan"]
    assert payload["total"] == 120
    assert payload["limit"] == maintenance.DUPLICATE_GROUP_PAGE_MAX
    assert payload["count"] == maintenance.DUPLICATE_GROUP_PAGE_MAX
    assert payload["large_result"] is True
    assert "videos" not in payload["groups"][0]


def test_duplicate_groups_payload_reports_missing_and_retains_latest_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write_duplicate_pair(lib, "Movie")
    scan = _scan(lib, lib, monkeypatch)

    missing, missing_err = maintenance.groups_payload("missing")
    monkeypatch.setattr(maintenance, "DUPLICATE_SCAN_MAX_AGE_SECONDS", 1)
    scan["_finished_ts"] = time.time() - 2
    retained, retained_err = maintenance.status_payload(scan["id"])

    assert missing is None
    assert missing_err == "Scan not found"
    assert retained_err is None
    assert retained["scan"]["id"] == scan["id"]


def test_duplicate_scan_reuses_active_scan_and_can_cancel(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    _reset_maintenance(monkeypatch)

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(maintenance.threading, "Thread", FakeThread)

    first, err = maintenance.start_duplicate_scan(str(lib), lib_root=str(lib))
    second, err2 = maintenance.start_duplicate_scan(str(lib), lib_root=str(lib))
    cancelled, cancel_err = maintenance.cancel_duplicate_scan(first["id"])

    assert err is None
    assert err2 is None
    assert second["id"] == first["id"]
    assert cancel_err is None
    assert cancelled["status"] == "cancelled"


def test_duplicate_scan_cancel_route_returns_json(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    _reset_maintenance(monkeypatch)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(maintenance.threading, "Thread", FakeThread)
    client = routes.app.test_client()

    scan_res = client.post("/api/maintenance/duplicates/scan", json={"path": str(lib)})
    scan_id = scan_res.get_json()["scan"]["id"]
    cancel_res = client.post(
        "/api/maintenance/duplicates/cancel",
        json={"scan_id": scan_id},
    )

    assert cancel_res.status_code == 200
    assert cancel_res.is_json
    assert cancel_res.get_json()["scan"]["status"] == "cancelled"


def test_cleanup_plan_is_limited_to_visible_paged_groups(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write_duplicate_pair(lib, "First")
    _, second_remove = _write_duplicate_pair(lib, "Second")
    scan = _scan(lib, lib, monkeypatch)
    first_group = next(group for group in scan["groups"] if group["folder"].endswith("First"))
    second_group = next(group for group in scan["groups"] if group["folder"].endswith("Second"))

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {
            "scan_id": scan["id"],
            "action": "move",
            "visible_group_ids": [first_group["id"]],
            "groups": [{"id": first_group["id"], "enabled": False}],
        },
        lib_root=str(lib),
    )

    assert err is None
    assert first_group["id"] in plan["skipped_groups"]
    assert plan["file_count"] == 0
    assert second_group["id"] not in plan["visible_group_ids"]
    assert str(second_remove) not in {item["source_path"] for item in plan["files"]}


def test_duplicate_apply_can_run_in_background_and_report_status(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write_duplicate_pair(lib, "Movie")
    scan = _scan(lib, lib, monkeypatch)
    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None

    class ImmediateThread:
        def __init__(self, target=None, args=(), **kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(maintenance.threading, "Thread", ImmediateThread)

    run, err = maintenance.start_duplicate_apply(plan["id"])
    status, status_err = maintenance.duplicate_apply_status(run["id"])

    assert err is None
    assert status_err is None
    assert status["apply"]["status"] == "success"
    assert status["apply"]["processed_count"] == plan["file_count"]
    assert status["apply"]["result"]["applied_count"] == plan["file_count"]
    assert status["apply"]["result"]["log"]["id"].endswith(".jsonl")


def test_duplicate_apply_route_starts_background_run(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write_duplicate_pair(lib, "Movie")
    scan = _scan(lib, lib, monkeypatch)
    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "delete", "groups": [], "visible_group_ids": _visible_group_ids(scan)},
        lib_root=str(lib),
    )
    assert err is None

    class FakeThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            return None

    monkeypatch.setattr(maintenance.threading, "Thread", FakeThread)

    res = routes.app.test_client().post(
        "/api/maintenance/duplicates/apply",
        json={"plan_id": plan["id"]},
    )

    assert res.status_code == 200
    assert res.is_json
    assert res.get_json()["apply"]["status"] == "queued"


def test_duplicate_probe_timeout_returns_empty_metadata(monkeypatch):
    def fake_run(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], kwargs.get("timeout"))

    monkeypatch.setattr(maintenance.subprocess, "run", fake_run)

    assert maintenance.probe_video_metadata("/library/bad.mkv") == {}


def test_maintenance_routes_report_missing_scan_and_malformed_plan():
    client = routes.app.test_client()

    missing = client.get("/api/maintenance/duplicates/status", query_string={"scan_id": "missing"})
    missing_groups = client.get(
        "/api/maintenance/duplicates/groups",
        query_string={"scan_id": "missing"},
    )
    malformed = client.post(
        "/api/maintenance/duplicates/plan",
        json={"scan_id": "missing", "groups": "bad"},
    )

    assert missing.status_code == 404
    assert missing.get_json()["error"] == "Scan not found"
    assert missing_groups.status_code == 404
    assert missing_groups.get_json()["error"] == "Scan not found"
    assert malformed.status_code == 400
    assert malformed.get_json()["error"] == "Group overrides are invalid"


def test_maintenance_page_and_static_assets_render():
    client = routes.app.test_client()

    res = client.get("/maintenance")
    html = res.get_data(as_text=True)
    base = (routes.app.root_path and (os.path.dirname(routes.app.root_path)))
    script_path = os.path.join(base, "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()

    assert res.status_code == 200
    assert "Library Maintenance" in html
    assert 'data-maint-tab-hash="overview"' in html
    assert 'id="tab-overview"' in html
    assert 'id="pane-overview"' in html
    assert 'id="overviewFolderInventory" class="collapse' in html
    assert 'id="overviewSearch"' in html
    assert 'id="overviewSort"' in html
    assert 'id="overviewPageLimit"' in html
    assert 'data-maint-tab-hash="posters"' in html
    assert 'data-maint-tab-hash="video-previews"' in html
    assert 'data-maint-tab-hash="subtitles"' in html
    assert 'data-maint-tab-hash="duplicates"' in html
    assert 'id="previewScanButton"' in html
    assert 'id="previewRunExtractionButton"' in html
    assert 'id="qualityScanButton"' in html
    assert 'id="qualityApplyButton"' in html
    assert 'id="subtitleScanButton"' in html
    assert 'id="subtitleItemStatus"' in html
    assert 'id="maintenanceScanButton"' in html
    assert 'id="maintenanceCancelScanButton"' in html
    assert "visible_group_ids" in script
    assert "data-maint-bulk=\"select\"" in script
    assert "const pageSizes = [10, 25, 50]" in script
    assert 'id="maintenanceRefreshLogsButton"' in html
    assert 'src="/static/maintenance.js"' in html
    assert "fetch('/api/dashboard/library-scan/status')" in script
    assert "fetch('/api/dashboard/library-scan'" in script
    assert "/api/dashboard/library-scan/folders?${params.toString()}" in script
    assert "fetch('/api/maintenance/duplicates/scan'" in script
    assert "fetch('/api/maintenance/duplicates/cancel'" in script
    assert "/api/maintenance/duplicates/groups?scan_id=" in script
    assert "/api/maintenance/duplicates/groups/${encodeURIComponent(groupId)}" in script
    assert "fetch('/api/maintenance/duplicates/plan'" in script
    assert "fetch('/api/maintenance/duplicates/apply'" in script
    assert "/api/maintenance/duplicates/apply/status?apply_id=" in script
    assert "fetch('/api/maintenance/duplicates/logs')" in script
    assert "fetch('/api/maintenance/video-previews/scan'" in script
    assert "/api/maintenance/video-previews/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/emby/tasks')" in script
    assert "fetch('/api/maintenance/video-previews/emby/run-extraction'" in script
    assert "fetch('/api/maintenance/video-previews/quality/scan'" in script
    assert "/api/maintenance/video-previews/quality/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/quality/apply'" in script
    assert "fetch('/api/maintenance/subtitles/scan'" in script
    assert "/api/maintenance/subtitles/items?" in script
    assert "fetch('/api/maintenance/subtitles/cancel'" in script
    assert "maintenance_active_tab_v2" in script
    assert "readJsonResponse" in script
    assert "data-overview-folder-toggle" in script
    assert "escapeHtml(item.path || '')" in script
    assert "data-maint-operation" in script
    assert "data-maint-expand" in script
    assert "data-maint-page" in script
    assert "escapeHtml(change.source || '')" in script
    assert "textContent" in script


def test_duplicate_scan_enriches_video_ids_and_carries_them_into_plan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    keep, remove = _write_duplicate_pair(lib, "Movie")
    catalog = emby_catalog._build_catalog(
        [
            {"Id": "keep-emby", "Name": "Movie HD", "Type": "Movie", "Path": str(keep)},
            {"Id": "remove-emby", "Name": "Movie SD", "Type": "Movie", "Path": str(remove)},
        ],
        {"Id": "server"},
        emby_catalog.configuration_fingerprint({}),
    )
    monkeypatch.setattr(
        maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 0, catalog_item_count=2)),
    )

    scan = _scan(lib, lib, monkeypatch)
    group = scan["groups"][0]
    assert scan["emby_mapping"]["matched_count"] == 2
    assert {video["emby_item_id"] for video in group["videos"]} == {"keep-emby", "remove-emby"}

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "delete", "visible_group_ids": [group["id"]]},
        lib_root=str(lib),
    )
    assert err is None
    assert {item["emby_item_id"] for item in plan["files"]} <= {"keep-emby", "remove-emby"}


def test_legacy_duplicate_scan_projects_emby_identity_as_not_checked(monkeypatch):
    _reset_maintenance(monkeypatch)
    public = maintenance.public_scan(
        {"id": "legacy", "status": "success", "groups": [], "settings": {}}
    )

    assert public["emby_mapping"]["status"] == "not_checked"
    assert "rescan" in public["emby_mapping"]["message"].lower()
