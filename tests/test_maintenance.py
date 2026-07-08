import os

import pytest

from app import app_settings, maintenance, routes


def _reset_maintenance(monkeypatch, metadata_by_name=None, settings_overrides=None):
    maintenance.duplicate_scans.clear()
    maintenance.cleanup_plans.clear()
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

    scan = _scan(lib, lib, monkeypatch)

    assert len(scan["groups"]) == 1
    assert len(scan["groups"][0]["videos"]) == 4


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
            "scan1",
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
        {"scan_id": scan["id"], "action": "move", "groups": []},
        lib_root=str(lib),
    )
    assert err is None
    assert plan["file_count"] == 2
    assert {item["operation"] for item in plan["files"]} == {"move", "rename"}
    assert plan["total_size_bytes"] == remove.stat().st_size

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 2
    assert keep.exists()
    assert poster.exists()
    assert not remove.exists()
    assert not sidecar.exists()
    assert (movie / "Movie.1080p.en.srt").read_bytes() == b"subtitle"
    assert (lib / ".vid2gif-duplicates" / scan["id"] / "Movie" / "Movie.720p.mkv").is_file()
    assert result["log"]["id"].endswith(".jsonl")


def test_cleanup_plan_moves_equivalent_accessory(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    keep = _write(movie / "Movie.1080p.mkv", b"a" * 200)
    remove = _write(movie / "Movie.720p.mkv", b"b" * 100)
    keeper_sidecar = _write(movie / "Movie.1080p.en.srt", b"keeper")
    duplicate_sidecar = _write(movie / "Movie.720p.en.srt", b"duplicate")
    scan = _scan(lib, lib, monkeypatch)

    plan, err = maintenance.build_duplicate_cleanup_plan(
        {"scan_id": scan["id"], "action": "move", "groups": []},
        lib_root=str(lib),
    )
    assert err is None
    sidecar_item = next(item for item in plan["files"] if item["source_path"] == str(duplicate_sidecar))
    assert sidecar_item["operation"] == "move"

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert keeper_sidecar.exists()
    assert not duplicate_sidecar.exists()
    assert (lib / ".vid2gif-duplicates" / scan["id"] / "Movie" / duplicate_sidecar.name).is_file()


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
        {"scan_id": scan["id"], "action": "move", "groups": []},
        lib_root=str(lib),
    )

    assert err is None
    assert plan["move_root"] == str(move_root / scan["id"])
    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])
    assert err is None
    assert not remove.exists()
    assert (move_root / scan["id"] / "Movie" / remove.name).is_file()


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
        {"scan_id": scan["id"], "action": "move", "groups": []},
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
        {"scan_id": scan["id"], "action": "move", "groups": []},
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
        {"scan_id": scan["id"], "action": "delete", "groups": []},
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


def test_maintenance_routes_report_missing_scan_and_malformed_plan():
    client = routes.app.test_client()

    missing = client.get("/api/maintenance/duplicates/status", query_string={"scan_id": "missing"})
    malformed = client.post(
        "/api/maintenance/duplicates/plan",
        json={"scan_id": "missing", "groups": "bad"},
    )

    assert missing.status_code == 404
    assert missing.get_json()["error"] == "Scan not found"
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
    assert 'data-maint-tab-hash="posters"' in html
    assert 'data-maint-tab-hash="duplicates"' in html
    assert 'id="maintenanceScanButton"' in html
    assert 'id="maintenanceRefreshLogsButton"' in html
    assert 'src="/static/maintenance.js"' in html
    assert "fetch('/api/maintenance/duplicates/scan'" in script
    assert "fetch('/api/maintenance/duplicates/plan'" in script
    assert "fetch('/api/maintenance/duplicates/apply'" in script
    assert "fetch('/api/maintenance/duplicates/logs')" in script
    assert "maintenance_active_tab" in script
    assert "data-maint-operation" in script
    assert "escapeHtml(file.source_path)" in script
    assert "textContent" in script
