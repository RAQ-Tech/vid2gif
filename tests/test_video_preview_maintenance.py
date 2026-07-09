import json
import os
import urllib.error

from app import routes, video_preview_maintenance


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reset_preview_state(monkeypatch, tmp_path):
    log_dir = tmp_path / "state" / "maintenance-logs" / "video-previews"
    monkeypatch.setattr(video_preview_maintenance, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(video_preview_maintenance, "LOG_INDEX", str(log_dir / "index.json"))
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {"duplicate_move_root": ""},
    )
    video_preview_maintenance.preview_scans.clear()
    return log_dir


def _scan(lib, monkeypatch, tmp_path, target=None):
    _reset_preview_state(monkeypatch, tmp_path)
    scan, err = video_preview_maintenance.start_scan(
        str(target or lib),
        lib_root=str(lib),
        synchronous=True,
    )
    assert err is None
    assert scan["status"] == "success"
    return scan


class FakeResponse:
    def __init__(self, payload=None, status=200):
        self.payload = payload
        self.status = status
        self.code = status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        if self.payload is None:
            return b""
        return json.dumps(self.payload).encode("utf-8")


def test_bif_filename_matching_and_interval_parsing():
    assert video_preview_maintenance.bif_matches_video("Movie.bif", "Movie")
    assert video_preview_maintenance.bif_matches_video("Movie-thumb.bif", "Movie")
    assert video_preview_maintenance.bif_matches_video("Movie-320-180.bif", "Movie")
    assert not video_preview_maintenance.bif_matches_video("Other-320-180.bif", "Movie")
    assert video_preview_maintenance.bif_interval_seconds("Movie.bif", "Movie") is None
    assert video_preview_maintenance.bif_interval_seconds("Movie-320-180.bif", "Movie") == 180
    assert video_preview_maintenance.bif_interval_seconds("Movie-320-10.bif", "Movie") == 10


def test_video_preview_scan_classifies_missing_present_and_stale(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Present" / "Present.mkv")
    _write(lib / "Present" / "Present-320-180.bif")
    _write(lib / "Missing" / "Missing.mp4")
    _write(lib / "Stale" / "Stale.mkv")
    _write(lib / "Stale" / "Stale-320-10.bif")

    scan = _scan(lib, monkeypatch, tmp_path)
    missing, err = video_preview_maintenance.items_payload(scan["id"], status="missing")
    stale, err2 = video_preview_maintenance.items_payload(scan["id"], status="stale")

    assert err is None
    assert err2 is None
    assert scan["counts"]["scanned_video_count"] == 3
    assert scan["counts"]["present_count"] == 2
    assert scan["counts"]["missing_count"] == 1
    assert scan["counts"]["stale_count"] == 1
    assert missing["items"][0]["name"] == "Missing.mp4"
    assert stale["items"][0]["name"] == "Stale.mkv"


def test_video_preview_scan_skips_quarantine_and_symlinked_files(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / ".vid2gif-duplicates" / "Dup" / "Dup.mkv")
    link = lib / "Movie" / "Linked.mkv"
    try:
        os.symlink(lib / "Movie" / "Movie.mkv", link)
    except (OSError, NotImplementedError):
        link = None

    scan = _scan(lib, monkeypatch, tmp_path)
    names = {item["name"] for item in scan["items"]}

    assert names == {"Movie.mkv"}
    if link is not None:
        assert "Linked.mkv" not in names


def test_video_preview_items_paging_caps_large_results(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(120):
        _write(lib / f"Movie {index:03d}" / f"Movie {index:03d}.mkv")

    scan = _scan(lib, monkeypatch, tmp_path)
    page, err = video_preview_maintenance.items_payload(
        scan["id"],
        status="missing",
        offset=0,
        limit=999,
    )

    assert err is None
    assert page["limit"] == video_preview_maintenance.ITEM_PAGE_MAX
    assert page["count"] == video_preview_maintenance.ITEM_PAGE_MAX
    assert page["total"] == 120
    assert page["large_result"] is True
    assert "items" not in video_preview_maintenance.public_scan(scan)


def test_video_preview_scan_reuses_active_scan_and_can_cancel(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _reset_preview_state(monkeypatch, tmp_path)

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            return None

    monkeypatch.setattr(video_preview_maintenance.threading, "Thread", FakeThread)
    first, err = video_preview_maintenance.start_scan(str(lib), lib_root=str(lib))
    second, err2 = video_preview_maintenance.start_scan(str(lib), lib_root=str(lib))
    cancelled, cancel_err = video_preview_maintenance.cancel_scan(first["id"])

    assert err is None
    assert err2 is None
    assert first["id"] == second["id"]
    assert cancel_err is None
    assert cancelled["status"] == "cancelled"


def test_video_preview_routes_return_bounded_json(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Missing" / "Missing.mkv")
    _write(lib / "Present" / "Present.mkv")
    _write(lib / "Present" / "Present-320-180.bif")
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    client = routes.app.test_client()

    scan_res = client.post(
        "/api/maintenance/video-previews/scan",
        json={"path": str(lib), "synchronous": True},
    )
    scan_data = scan_res.get_json()
    status_res = client.get(
        "/api/maintenance/video-previews/status",
        query_string={"scan_id": scan_data["scan"]["id"]},
    )
    items_res = client.get(
        "/api/maintenance/video-previews/items",
        query_string={"scan_id": scan_data["scan"]["id"], "status": "missing"},
    )

    assert scan_res.status_code == 200
    assert scan_data["scan"]["missing_count"] == 1
    assert "items" not in scan_data["scan"]
    assert status_res.status_code == 200
    assert "items" not in status_res.get_json()["scan"]
    assert items_res.status_code == 200
    assert items_res.get_json()["items"][0]["name"] == "Missing.mkv"


def test_video_preview_routes_reject_invalid_and_missing_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    outside = tmp_path / "library-other"
    lib.mkdir()
    outside.mkdir()
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    client = routes.app.test_client()

    scan_res = client.post(
        "/api/maintenance/video-previews/scan",
        json={"path": str(outside), "synchronous": True},
    )
    items_res = client.get(
        "/api/maintenance/video-previews/items",
        query_string={"scan_id": "missing"},
    )

    assert scan_res.status_code == 400
    assert scan_res.get_json()["error"] == "Path not found"
    assert items_res.status_code == 404
    assert items_res.get_json()["error"] == "Scan not found"


def test_video_preview_emby_discovers_and_runs_thumbnail_task(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)
    settings = {"emby_url": "http://emby:8096", "emby_api_key": "abc 123"}
    captured = []

    def fake_open(request, timeout):
        captured.append((request.method, request.full_url, request.data, timeout))
        if request.method == "GET":
            return FakeResponse(
                [
                    {"Id": "other", "Name": "Refresh Guide"},
                    {
                        "Id": "task1",
                        "Name": "Thumbnail Image Extraction",
                        "Key": "ExtractChapterImages",
                    },
                ]
            )
        return FakeResponse(None, status=204)

    tasks = video_preview_maintenance.discover_thumbnail_tasks(
        settings=settings,
        opener=fake_open,
    )
    payload, err = video_preview_maintenance.run_thumbnail_extraction(
        settings=settings,
        opener=fake_open,
    )

    assert err is None
    assert tasks["thumbnail_task"]["id"] == "task1"
    assert payload["result"]["status"] == "success"
    assert captured[0][1] == "http://emby:8096/emby/ScheduledTasks?api_key=abc+123"
    assert captured[-1][1] == "http://emby:8096/emby/ScheduledTasks/Running/task1?api_key=abc+123"
    assert captured[-1][2] == b""
    assert "abc 123" not in str(payload)


def test_video_preview_emby_handles_base_url_ending_in_emby(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)
    captured = {}

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        return FakeResponse([])

    video_preview_maintenance.discover_thumbnail_tasks(
        settings={"emby_url": "http://emby:8096/emby", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert captured["url"] == "http://emby:8096/emby/ScheduledTasks?api_key=secret"


def test_video_preview_emby_no_task_found_returns_failed(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)

    def fake_open(request, timeout):
        return FakeResponse([{"Id": "other", "Name": "Refresh Guide"}])

    payload, err = video_preview_maintenance.run_thumbnail_extraction(
        settings={"emby_url": "http://emby:8096", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert err is None
    assert payload["result"]["status"] == "failed"
    assert payload["task"] is None
    assert payload["log"]["type"] == "emby-task"
    assert "secret" not in str(payload)


def test_video_preview_emby_failed_http_response_is_redacted(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)

    def fake_open(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized secret",
            hdrs=None,
            fp=None,
        )

    tasks = video_preview_maintenance.discover_thumbnail_tasks(
        settings={"emby_url": "http://emby:8096", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert tasks["result"]["status"] == "failed"
    assert "secret" not in str(tasks)


def test_video_preview_ui_assets_render():
    client = routes.app.test_client()

    res = client.get("/maintenance")
    html = res.get_data(as_text=True)
    base = routes.app.root_path and os.path.dirname(routes.app.root_path)
    script_path = os.path.join(base, "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()

    assert res.status_code == 200
    assert 'data-maint-tab-hash="video-previews"' in html
    assert 'id="previewScanButton"' in html
    assert 'id="previewRunExtractionButton"' in html
    assert "fetch('/api/maintenance/video-previews/scan'" in script
    assert "/api/maintenance/video-previews/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/emby/tasks')" in script
    assert "fetch('/api/maintenance/video-previews/emby/run-extraction'" in script
    assert "escapeHtml(item.relative_path" in script
