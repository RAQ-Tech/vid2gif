from pathlib import Path

from app import dashboard, routes


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
    assert {item["key"] for item in payload["workstreams"]} >= {
        "gifs",
        "duplicates",
        "posters",
        "video_previews",
        "actor_images",
    }
    assert "groups" not in str(payload["duplicates"].get("scan", {}))


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

    assert err is None
    assert scan["status"] == "success"
    root_stats = scan["libraries"][0]
    child_stats = next(item for item in scan["libraries"] if item["name"] == "XXX")
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
    assert cached["libraries"][0]["video_size_label"] != "0 B"


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


def test_dashboard_static_assets_escape_dynamic_output():
    template = (ROOT / "app" / "templates" / "dashboard.html").read_text(encoding="utf-8")
    script = (ROOT / "app" / "static" / "dashboard.js").read_text(encoding="utf-8")

    assert 'id="dashboardWorkstreams"' in template
    assert 'id="dashboardIssueChart"' in template
    assert 'id="dashboardLibraries"' in template
    assert "fetch('/api/dashboard/status')" in script
    assert "fetch('/api/dashboard/library-scan'" in script
    assert "fetch('/api/dashboard/library-scan/status')" in script
    assert "readJsonResponse" in script
    assert "escapeHtml(item.title)" in script
    assert "escapeHtml(item.path || '')" in script
    assert "escapeHtml(item.area || 'Maintenance')" in script
