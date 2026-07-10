import os
import time

import pytest

from app import app_settings, routes, subtitle_maintenance


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
    monkeypatch.setattr(
        subtitle_maintenance.app_settings,
        "load_settings",
        lambda: dict(settings or _settings()),
    )


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


def test_subtitle_status_reports_expired_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    scan = _scan(lib, monkeypatch)

    monkeypatch.setattr(subtitle_maintenance, "SCAN_MAX_AGE_SECONDS", 1)
    scan["_finished_ts"] = time.time() - 2
    payload, err = subtitle_maintenance.status_payload(scan["id"])

    assert payload is None
    assert err == "Scan not found"


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
    assert 'id="subtitleSearch"' in html
    assert "fetch('/api/maintenance/subtitles/scan'" in script
    assert "/api/maintenance/subtitles/items?" in script
    assert "fetch('/api/maintenance/subtitles/cancel'" in script
    assert "escapeHtml(item.detail || '')" in script
