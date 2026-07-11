from pathlib import Path

from app import dashboard, impact_metrics, routes


ROOT = Path(__file__).resolve().parents[1]


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reset_dashboard(monkeypatch, tmp_path, lib):
    state = tmp_path / "state" / "dashboard"
    monkeypatch.setattr(dashboard, "DASHBOARD_ROOT", str(state))
    monkeypatch.setattr(dashboard, "LIBRARY_INVENTORY_PATH", str(state / "library-inventory.json"))
    monkeypatch.setattr(dashboard, "LIB_ROOT", str(lib))
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    monkeypatch.setattr(impact_metrics, "IMPACT_ROOT", str(state))
    monkeypatch.setattr(impact_metrics, "IMPACT_PATH", str(state / "impact-metrics.json"))
    monkeypatch.setattr(impact_metrics, "IMPACT_BACKUP_PATH", str(state / "impact-metrics.json.bak"))
    impact_metrics._last_error = ""
    dashboard.library_scan = None


def test_dashboard_page_and_status_api_render(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    _reset_dashboard(monkeypatch, tmp_path, lib)
    client = routes.app.test_client()

    home = client.get("/")
    alias = client.get("/dashboard")
    status = client.get("/api/dashboard/status")

    assert home.status_code == 200
    assert alias.status_code == 200
    assert "Dashboard" in home.get_data(as_text=True)
    assert 'src="/static/dashboard.js"' in home.get_data(as_text=True)
    assert status.status_code == 200
    assert status.is_json
    payload = status.get_json()
    assert "workstreams" in payload
    assert payload["impact"]["total_fixes"] == 0
    assert payload["creative_output"]["standard_gifs"] == 0
    assert {item["key"] for item in payload["workstreams"]} >= {
        "gifs",
        "duplicates",
        "posters",
        "video_previews",
        "subtitles",
        "actor_images",
    }
    assert "groups" not in str(payload["duplicates"].get("scan", {}))
    assert "folders" not in payload["library"]
    assert "libraries" not in payload["library"]


def test_library_inventory_scan_counts_sidecars_and_skips_quarantine(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "XXX" / "Movie"
    _write(movie / "Movie.mkv", b"v" * 10)
    _write(movie / "Movie.en.srt")
    _write(movie / "Movie.nfo")
    _write(movie / "Movie-320-10.bif")
    _write(movie / "Movie-poster.jpg")
    _write(movie / "Movie-background.jpg")
    _write(movie / "Movie-performer-Jane Doe-image.jpg")
    _write(lib / ".vid2gif-duplicates" / "XXX" / "Movie" / "Duplicate.mkv", b"ignored")
    _reset_dashboard(monkeypatch, tmp_path, lib)

    scan, err = dashboard.start_library_scan(str(lib), synchronous=True)
    folder_page = dashboard.library_folders_payload()

    assert err is None
    assert scan["status"] == "success"
    assert "folders" not in scan
    assert "libraries" not in scan
    root_stats = scan["root"]
    child_stats = next(item for item in folder_page["folders"] if item["name"] == "XXX")
    assert root_stats["video_count"] == 1
    assert root_stats["subtitle_count"] == 1
    assert root_stats["nfo_count"] == 1
    assert root_stats["bif_count"] == 1
    assert root_stats["poster_count"] == 1
    assert root_stats["background_count"] == 1
    assert root_stats["actor_image_count"] == 1
    assert child_stats["video_count"] == 1

    cached = dashboard.library_scan_status()["scan"]
    assert cached["video_count"] == 1
    assert cached["root"]["video_size_label"] != "0 B"
    assert "folders" not in cached
    assert folder_page["total"] == 1


def test_dashboard_library_scan_route_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    _reset_dashboard(monkeypatch, tmp_path, lib)

    res = routes.app.test_client().post(
        "/api/dashboard/library-scan",
        json={"path": str(sibling), "synchronous": True},
    )

    assert res.status_code == 400
    assert res.is_json
    assert res.get_json()["error"] == "Path not found"


def test_library_folder_payload_pages_searches_sorts_and_caps(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(120):
        folder = lib / f"Folder {index:03d}"
        _write(folder / f"Movie {index:03d}.mkv", b"x" * (index + 1))
    _reset_dashboard(monkeypatch, tmp_path, lib)
    scan, err = dashboard.start_library_scan(str(lib), synchronous=True)

    first = dashboard.library_folders_payload(limit=999, sort="video_size_bytes", direction="desc")
    searched = dashboard.library_folders_payload(q="Folder 119")
    invalid = dashboard.library_folders_payload(limit="bad", sort="bad", direction="sideways")
    route_res = routes.app.test_client().get(
        "/api/dashboard/library-scan/folders",
        query_string={"offset": 0, "limit": 100, "sort": "video_size_bytes", "direction": "desc"},
    )

    assert err is None
    assert scan["folder_count"] == 120
    assert first["limit"] == 25
    assert first["total"] == 120
    assert first["count"] == 25
    assert first["folders"][0]["name"] == "Folder 119"
    assert first["large_result"] is True
    assert searched["total"] == 1
    assert searched["folders"][0]["name"] == "Folder 119"
    assert invalid["limit"] == 25
    assert invalid["sort"] == "name"
    assert invalid["direction"] == "asc"
    assert route_res.status_code == 200
    assert route_res.is_json
    route_payload = route_res.get_json()
    assert route_payload["limit"] == 100
    assert route_payload["folders"][0]["name"] == "Folder 119"


def test_old_library_inventory_cache_is_read_without_full_status_payload(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    _reset_dashboard(monkeypatch, tmp_path, lib)
    old_cache = {
        "schema_version": 1,
        "status": "success",
        "finished_at": "2026-07-10T00:00:00Z",
        "library_count": 2,
        "video_count": 3,
        "video_size_bytes": 300,
        "video_size_label": "300 B",
        "libraries": [
            {"name": "library", "path": str(lib), "kind": "root", "video_count": 3, "video_size_bytes": 300, "video_size_label": "300 B"},
            {"name": "Movies", "path": str(lib / "Movies"), "kind": "library", "video_count": 2, "video_size_bytes": 200, "video_size_label": "200 B"},
        ],
    }
    dashboard._write_json(dashboard.LIBRARY_INVENTORY_PATH, old_cache)

    status = dashboard.library_scan_status()["scan"]
    folders = dashboard.library_folders_payload()

    assert status["status"] == "cached"
    assert status["video_count"] == 3
    assert "libraries" not in status
    assert folders["total"] == 1
    assert folders["folders"][0]["name"] == "Movies"


def test_dashboard_static_assets_escape_dynamic_output():
    template = (ROOT / "app" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="dashboardWorkstreams"' in template
    assert 'id="dashboardTotalFixes"' in template
    assert 'id="dashboardImpactCategories"' in template
    assert 'id="dashboardImpactTrend"' in template
    assert 'id="dashboardIssueChart"' in template
    assert 'id="dashboardLibraries"' in template
    assert "fetch('/api/dashboard/status')" in script
    assert "fetch('/api/dashboard/maintenance-scans'" in script
    assert 'id="dashboardScanAllButton"' in template
    assert "setInterval(refreshDashboard" not in script
    assert "fetch('/api/dashboard/library-scan/status')" in script
    assert "/api/dashboard/library-scan/folders" not in script
    assert "readJsonResponse" in script
    assert "escapeHtml(item.title)" in script
    assert "escapeHtml(root.path || scan.path || '')" in script
    assert "escapeHtml(item.area || 'Maintenance')" in script
    assert "renderImpact(data)" in script
    assert "escapeHtml(item.title" in script
