import json
import os

from app import actor_image_maintenance, emby_client, routes


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


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


def _settings():
    return {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret",
        "emby_refresh_enabled": False,
    }


def _reset_actor_state(monkeypatch, tmp_path, settings=None):
    root = tmp_path / "state" / "actor-images"
    log_dir = tmp_path / "state" / "maintenance-logs" / "actor-images"
    monkeypatch.setattr(actor_image_maintenance, "ACTOR_IMAGE_ROOT", str(root))
    monkeypatch.setattr(actor_image_maintenance, "EXCEPTIONS_PATH", str(root / "exceptions.json"))
    monkeypatch.setattr(actor_image_maintenance, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(actor_image_maintenance, "LOG_INDEX", str(log_dir / "index.json"))
    monkeypatch.setattr(actor_image_maintenance.poster_maintenance, "load_settings", lambda: settings or _settings())
    actor_image_maintenance.actor_scans.clear()
    actor_image_maintenance.actor_plans.clear()
    actor_image_maintenance.actor_apply_runs.clear()
    return root


def _fake_scan_opener(video_path):
    def opener(request, timeout=30):
        url = request.full_url
        if "/emby/Persons" in url:
            return FakeResponse(
                {
                    "Items": [
                        {"Id": "p1", "Name": "Jane Doe", "ImageTags": {}, "ProviderIds": {"tpdb": "jane"}},
                        {"Id": "p2", "Name": "Has Image", "ImageTags": {"Primary": "tag"}},
                        {"Id": "p3", "Name": "No Local", "ImageTags": {}},
                    ],
                    "TotalRecordCount": 3,
                }
            )
        if "/emby/Items?" in url:
            return FakeResponse(
                {
                    "Items": [
                        {
                            "Id": "m1",
                            "Name": "Movie",
                            "Path": str(video_path),
                            "People": [
                                {"Id": "p1", "Name": "Jane Doe", "Type": "Actor"},
                                {"Id": "p2", "Name": "Has Image", "Type": "Actor"},
                                {"Id": "p3", "Name": "No Local", "Type": "Actor"},
                            ],
                        }
                    ],
                    "TotalRecordCount": 1,
                }
            )
        return FakeResponse({})

    return opener


def test_actor_name_normalization_and_candidate_matching(tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    jane = _write(lib / "Movie" / "Movie-performer-Jane_Doe-image.jpg")
    _write(lib / "Movie" / "Other Actor.jpg")

    candidates = actor_image_maintenance.find_actor_image_candidates("Jane Doe", [str(movie)], lib_root=str(lib))

    assert actor_image_maintenance.normalize_actor_name("Jane_Doe!") == "jane doe"
    assert len(candidates) == 1
    assert candidates[0]["path"] == str(jane)


def test_actor_scan_finds_ready_and_unresolved_missing_images(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-performer-Jane Doe-image.jpg")
    _reset_actor_state(monkeypatch, tmp_path)

    scan, err = actor_image_maintenance.start_scan(
        str(lib),
        lib_root=str(lib),
        synchronous=True,
        opener=_fake_scan_opener(movie),
    )
    page, page_err = actor_image_maintenance.items_payload(scan["id"], status="all")

    assert err is None
    assert scan["status"] == "success"
    assert scan["counts"]["missing_actor_count"] == 2
    assert scan["counts"]["ready_count"] == 1
    assert scan["counts"]["no_candidate_count"] == 1
    assert page_err is None
    assert {item["status"] for item in page["items"]} == {"ready", "no_candidate"}
    assert "secret" not in str(actor_image_maintenance.public_scan(scan))
    ready = next(item for item in page["items"] if item["status"] == "ready")
    assert ready["related_videos"][0]["emby_item_id"] == "m1"
    assert scan["emby_mapping"]["matched_count"] == 1


def test_actor_exceptions_persist_and_update_scan_items(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-performer-Jane Doe-image.jpg")
    _reset_actor_state(monkeypatch, tmp_path)
    scan, _err = actor_image_maintenance.start_scan(
        str(lib),
        lib_root=str(lib),
        synchronous=True,
        opener=_fake_scan_opener(movie),
    )

    payload, err = actor_image_maintenance.update_exception(
        {"person_id": "p1", "name": "Jane Doe", "status": "manual", "note": "search later"}
    )
    page, page_err = actor_image_maintenance.items_payload(scan["id"], status="manual")

    assert err is None
    assert payload["exception"]["status"] == "manual"
    assert page_err is None
    assert page["total"] == 1
    assert page["items"][0]["name"] == "Jane Doe"
    assert actor_image_maintenance.load_exceptions()["id:p1"]["note"] == "search later"


def test_actor_plan_and_apply_uploads_image_without_overwrite(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-performer-Jane Doe-image.jpg", b"image")
    _reset_actor_state(monkeypatch, tmp_path)
    scan, _err = actor_image_maintenance.start_scan(
        str(lib),
        lib_root=str(lib),
        synchronous=True,
        opener=_fake_scan_opener(movie),
    )
    plan, plan_err = actor_image_maintenance.build_import_plan({"scan_id": scan["id"]}, lib_root=str(lib))
    calls = []
    notification_calls = []
    monkeypatch.setattr(
        actor_image_maintenance.emby_notifications,
        "notify_maintenance",
        lambda *args, **kwargs: notification_calls.append((args, kwargs))
        or {"id": "notice", "status": "success", "message": "accepted"},
    )

    def opener(request, timeout=30):
        calls.append(
            (
                request.method,
                request.full_url,
                request.data,
                request.get_header("X-emby-token"),
                request.get_header("Content-type"),
            )
        )
        if request.method == "GET":
            return FakeResponse({"Id": "p1", "Name": "Jane Doe", "ImageTags": {}})
        return FakeResponse(None, status=204)

    run, apply_err = actor_image_maintenance.start_import_apply(plan["id"], opener=opener)

    assert plan_err is None
    assert apply_err is None
    assert run["status"] == "success"
    assert run["imported_count"] == 1
    assert calls[0][0] == "GET"
    assert calls[1][0] == "POST"
    assert calls[1][1] == "http://emby:8096/emby/Items/p1/Images/Primary"
    assert calls[1][2] == b"image"
    assert all(call[3] == "secret" for call in calls)
    assert calls[1][4] == "image/jpeg"
    assert len(calls) == 2
    assert "emby_sync" not in run
    assert notification_calls[0][1]["succeeded_count"] == 1
    assert run["emby_notification"]["id"] == "notice"
    assert "secret" not in str(actor_image_maintenance.public_apply_run(run))


def test_actor_apply_refuses_existing_emby_image(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-performer-Jane Doe-image.jpg", b"image")
    _reset_actor_state(monkeypatch, tmp_path)
    scan, _err = actor_image_maintenance.start_scan(
        str(lib),
        lib_root=str(lib),
        synchronous=True,
        opener=_fake_scan_opener(movie),
    )
    plan, _plan_err = actor_image_maintenance.build_import_plan({"scan_id": scan["id"]}, lib_root=str(lib))

    def opener(request, timeout=30):
        return FakeResponse({"Id": "p1", "Name": "Jane Doe", "ImageTags": {"Primary": "exists"}})

    run, err = actor_image_maintenance.start_import_apply(plan["id"], opener=opener)

    assert err is None
    assert run["status"] == "success"
    assert run["imported_count"] == 0
    assert run["refused_count"] == 1


def test_actor_routes_and_ui_assets(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-performer-Jane Doe-image.jpg")
    _reset_actor_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    def fake_emby(settings, api_path, params=None, **_kwargs):
        if api_path == "/Persons":
            return [
                {"Id": "p1", "Name": "Jane Doe", "ImageTags": {}}
            ], emby_client.result("success", "ok")
        if api_path == "/Items":
            return [
                {
                    "Id": "m1",
                    "Name": "Movie",
                    "Path": str(movie),
                    "People": [{"Id": "p1", "Name": "Jane Doe", "Type": "Actor"}],
                }
            ], emby_client.result("success", "ok")
        return [], emby_client.result("success", "ok")

    monkeypatch.setattr(actor_image_maintenance.emby_client, "request_paged_json", fake_emby)
    client = routes.app.test_client()

    html_res = client.get("/maintenance")
    scan_res = client.post(
        "/api/maintenance/actor-images/scan",
        json={"path": str(lib), "synchronous": True},
    )
    scan = scan_res.get_json()["scan"]
    items_res = client.get(
        "/api/maintenance/actor-images/items",
        query_string={"scan_id": scan["id"], "status": "ready"},
    )
    missing_res = client.get(
        "/api/maintenance/actor-images/items",
        query_string={"scan_id": "missing"},
    )
    preview_res = client.get(
        "/api/maintenance/actor-images/preview",
        query_string={"path": str(tmp_path / "outside.jpg")},
    )
    script_path = os.path.join(os.path.dirname(routes.app.root_path), "app", "static", "maintenance.js")
    script = open(script_path, encoding="utf-8").read()
    html = html_res.get_data(as_text=True)

    assert html_res.status_code == 200
    assert 'data-maint-tab-hash="actor-images"' in html
    assert 'id="actorScanButton"' in html
    assert scan_res.status_code == 200
    assert scan["ready_count"] == 1
    assert items_res.status_code == 200
    assert items_res.get_json()["items"][0]["name"] == "Jane Doe"
    assert missing_res.status_code == 404
    assert preview_res.status_code == 404
    assert "fetch('/api/maintenance/actor-images/scan'" in script
    assert "/api/maintenance/actor-images/items?scan_id=" in script
    assert "fetch('/api/maintenance/actor-images/plan'" in script
    assert "fetch('/api/maintenance/actor-images/apply'" in script
    assert "escapeHtml(item.name" in script
