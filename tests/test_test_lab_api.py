import json
import os

import pytest

from app import routes, test_lab


def _reset_lab_roots(monkeypatch, tmp_path):
    lab_root = tmp_path / "state" / "test-lab"
    logs = tmp_path / "state" / "logs"
    proc = tmp_path / "state" / "processing" / "tmp"
    lab_root.mkdir(parents=True)
    logs.mkdir(parents=True)
    proc.mkdir(parents=True)
    monkeypatch.setattr(test_lab, "TEST_LAB_ROOT", str(lab_root))
    monkeypatch.setattr(test_lab, "LOG_DIR", str(logs))
    monkeypatch.setattr(test_lab, "PROCESS_TMP_ROOT", str(proc))
    test_lab.test_lab_runs.clear()
    with test_lab.test_lab_queue.mutex:
        test_lab.test_lab_queue.queue.clear()
    return lab_root


def _run_payload(video, variants=2):
    return {
        "video": str(video),
        "variants": [
            {
                "name": f"Variant {index}",
                "settings": {
                    "height": "360",
                    "fps": "24",
                    "clip_len": "2",
                    "percent_points": "10,50,90",
                    "abs_early": "0",
                    "abs_late_from_end": "0",
                    "start_buffer": "5",
                    "end_buffer": "5",
                    "loop_forever": "on",
                    "smooth": "off",
                },
            }
            for index in range(1, variants + 1)
        ],
    }


def test_media_browser_lists_compatible_files_and_skips_symlinks(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    nested = lib / "nested"
    nested.mkdir(parents=True)
    (lib / "movie.mp4").write_text("x")
    (lib / "notes.txt").write_text("x")
    target = tmp_path / "target"
    target.mkdir()
    (target / "hidden.mp4").write_text("x")
    link = lib / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().get("/api/media-browser", query_string={"path": str(lib)})

    assert res.status_code == 200
    payload = res.get_json()
    assert [item["name"] for item in payload["folders"]] == ["nested"]
    assert [item["name"] for item in payload["files"]] == ["movie.mp4"]


def test_media_browser_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().get(
        "/api/media-browser",
        query_string={"path": str(sibling)},
    )

    assert res.status_code == 400
    assert res.get_json()["files"] == []


def test_test_lab_run_rejects_unsupported_file(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    unsupported = lib / "movie.txt"
    unsupported.write_text("x")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/test-lab/run",
        json=_run_payload(unsupported),
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Choose one compatible video file"


def test_test_lab_run_validates_variant_count(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_text("x")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    res = routes.app.test_client().post(
        "/api/test-lab/run",
        json=_run_payload(video, variants=1),
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Choose 2 to 4 variants"


def test_test_lab_run_normalizes_settings_and_queues(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_text("x")
    captured = {}
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    def fake_enqueue(path, variants, lib_root):
        captured["path"] = path
        captured["variants"] = variants
        captured["lib_root"] = lib_root
        return "run1", None

    monkeypatch.setattr(routes.test_lab, "enqueue_test_run", fake_enqueue)
    monkeypatch.setattr(routes.test_lab, "status_payload", lambda: {"active_run": None})
    payload = _run_payload(video)
    payload["variants"][0]["settings"]["fps_preset"] = "original"
    payload["variants"][0]["settings"]["optimize"] = "off"
    payload["variants"][1]["settings"]["smooth"] = "on"
    payload["variants"][1]["settings"]["loop_forever"] = "off"

    res = routes.app.test_client().post("/api/test-lab/run", json=payload)

    assert res.status_code == 200
    assert res.get_json()["run_id"] == "run1"
    assert captured["path"] == str(video)
    assert captured["lib_root"] == str(lib)
    assert captured["variants"][0]["cfg"]["fps"] == "original"
    assert captured["variants"][0]["cfg"]["optimize"] is False
    assert captured["variants"][1]["cfg"]["smooth"] is True
    assert captured["variants"][1]["cfg"]["loop_forever"] is False


def test_test_lab_file_serving_is_confined_to_lab_root(monkeypatch, tmp_path):
    lab_root = _reset_lab_roots(monkeypatch, tmp_path)
    run_dir = lab_root / "run1"
    run_dir.mkdir()
    gif = run_dir / "variant-1.gif"
    gif.write_bytes(b"GIF89a")

    client = routes.app.test_client()
    res = client.get("/test-lab/files/run1/variant-1.gif")

    assert res.status_code == 200
    assert res.mimetype == "image/gif"
    assert client.get("/test-lab/files/..%2Foutside/variant-1.gif").status_code == 404


def test_test_lab_delete_reports_inventory_and_refuses_active_files(monkeypatch, tmp_path):
    lab_root = _reset_lab_roots(monkeypatch, tmp_path)
    run_dir = lab_root / "run1"
    run_dir.mkdir()
    stale = run_dir / "variant-1.gif"
    active = run_dir / "variant-2.gif"
    stale.write_bytes(b"GIF89a")
    active.write_bytes(b"GIF89aactive")
    test_lab.test_lab_runs["run1"] = {
        "id": "run1",
        "status": "running",
        "variants": [{"filename": "variant-2.gif"}],
    }

    payload = test_lab.delete_files(["run1/variant-1.gif", "run1/variant-2.gif"])

    assert payload["deleted"] == ["run1/variant-1.gif"]
    assert payload["refused"] == ["run1/variant-2.gif"]
    assert not stale.exists()
    assert active.exists()
    assert payload["total_size_bytes"] == active.stat().st_size


def test_test_lab_rename_updates_manifest_and_inventory(monkeypatch, tmp_path):
    lab_root = _reset_lab_roots(monkeypatch, tmp_path)
    run_dir = lab_root / "run1"
    run_dir.mkdir()
    gif = run_dir / "variant-1.gif"
    gif.write_bytes(b"GIF89a")
    manifest = {
        "schema_version": 1,
        "run_id": "run1",
        "source_name": "movie.mp4",
        "variants": [
            {
                "id": "variant-1",
                "name": "Variant 1",
                "filename": "variant-1.gif",
                "request_fingerprint": "fingerprint-1",
                "settings_label": "360px high",
            }
        ],
    }
    (run_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    res = routes.app.test_client().post(
        "/api/test-lab/rename",
        json={"file_id": "run1/variant-1.gif", "name": "  Phone test  "},
    )

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["renamed"] == "run1/variant-1.gif"
    assert payload["files"][0]["name"] == "Phone test"
    updated = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert updated["variants"][0]["name"] == "Phone test"
    assert updated["variants"][0]["filename"] == "variant-1.gif"
    assert updated["variants"][0]["request_fingerprint"] == "fingerprint-1"


def test_test_lab_rename_rejects_missing_or_invalid_file(monkeypatch, tmp_path):
    _reset_lab_roots(monkeypatch, tmp_path)

    res = routes.app.test_client().post(
        "/api/test-lab/rename",
        json={"file_id": "../outside.gif", "name": "Nope"},
    )

    assert res.status_code == 400
    assert res.get_json()["error"] == "Test GIF not found"


def test_request_fingerprint_changes_with_source_and_background(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_bytes(b"video")
    cfg = {
        "height": 360,
        "fps": 24,
        "clip_len": 2,
        "percent_points": [10, 50, 90],
        "abs_early": 0,
        "abs_late_from_end": 0,
        "start_buffer": 5,
        "end_buffer": 5,
        "loop_forever": True,
        "smooth": False,
    }

    base = test_lab.request_fingerprint(
        str(video),
        cfg,
        lib_root=str(lib),
        background_image=None,
    )
    background = lib / "background.jpg"
    background.write_bytes(b"background")
    with_background = test_lab.request_fingerprint(
        str(video),
        cfg,
        lib_root=str(lib),
        background_image=str(background),
    )
    video.write_bytes(b"changed video")
    source_changed = test_lab.request_fingerprint(
        str(video),
        cfg,
        lib_root=str(lib),
        background_image=None,
    )
    background.write_bytes(b"changed background")
    background_changed = test_lab.request_fingerprint(
        str(video),
        cfg,
        lib_root=str(lib),
        background_image=str(background),
    )
    unoptimized = test_lab.request_fingerprint(
        str(video),
        {**cfg, "optimize": False},
        lib_root=str(lib),
        background_image=str(background),
    )

    assert base != with_background
    assert base != source_changed
    assert with_background != background_changed
    assert background_changed != unoptimized
