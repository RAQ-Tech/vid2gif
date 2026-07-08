import os

import pytest

from app import maintenance, routes


def _reset_maintenance(monkeypatch, metadata_by_name=None):
    maintenance.duplicate_scans.clear()
    maintenance.cleanup_plans.clear()
    metadata_by_name = metadata_by_name or {}

    def fake_probe(path):
        return metadata_by_name.get(os.path.basename(path), {})

    monkeypatch.setattr(maintenance, "probe_video_metadata", fake_probe)


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _scan(lib, target, monkeypatch, metadata_by_name=None):
    _reset_maintenance(monkeypatch, metadata_by_name)
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
    assert (
        maintenance.normalize_duplicate_name("Show.S01E01.1080p")
        != maintenance.normalize_duplicate_name("Show.S01E02.1080p")
    )


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


def test_cleanup_plan_moves_duplicate_video_and_stem_accessory(monkeypatch, tmp_path):
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
    assert plan["total_size_bytes"] == remove.stat().st_size + sidecar.stat().st_size

    result, err = maintenance.apply_duplicate_cleanup_plan(plan["id"])

    assert err is None
    assert result["applied_count"] == 2
    assert keep.exists()
    assert poster.exists()
    assert not remove.exists()
    assert not sidecar.exists()
    assert (lib / ".vid2gif-duplicates" / scan["id"] / "Movie" / "Movie.720p.mkv").is_file()
    assert (lib / ".vid2gif-duplicates" / scan["id"] / "Movie" / "Movie.720p.en.srt").is_file()


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
    assert 'id="maintenanceScanButton"' in html
    assert 'src="/static/maintenance.js"' in html
    assert "fetch('/api/maintenance/duplicates/scan'" in script
    assert "fetch('/api/maintenance/duplicates/plan'" in script
    assert "fetch('/api/maintenance/duplicates/apply'" in script
    assert "escapeHtml(file.source_path)" in script
    assert "textContent" in script
