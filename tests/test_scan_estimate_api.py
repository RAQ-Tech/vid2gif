import pytest

from app import routes


def _reset_scan_state(monkeypatch, lib_root):
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib_root))
    routes._scan_cache.clear()
    monkeypatch.setattr(routes.estimate_history, "load_history", lambda path=None: [])


def _query(path, **overrides):
    params = {
        "path": str(path),
        "height_preset": "480",
        "fps_preset": "15",
        "clip_len_preset": "2",
        "percent_points": "10,20,30,40,50,60,70,80,90",
        "abs_early": "15",
        "abs_late_from_end": "10",
        "start_buffer": "5",
        "end_buffer": "5",
    }
    params.update(overrides)
    return params


def test_scan_estimate_missing_path_prompts_for_folder():
    client = routes.app.test_client()

    res = client.get("/api/scan-estimate")

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["status"] == "choose_folder"
    assert payload["message"] == "Choose a folder"
    assert payload["compatible_count"] == 0


def test_scan_estimate_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    video = sibling / "movie.mp4"
    video.write_text("x")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(video))

    assert res.status_code == 400
    assert res.get_json()["status"] == "invalid_path"


def test_scan_estimate_counts_single_compatible_file_case_insensitively(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "Movie.MP4"
    video.write_text("x")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(video))

    payload = res.get_json()
    assert res.status_code == 200
    assert payload["scan_status"] == "complete"
    assert payload["compatible_count"] == 1
    assert payload["is_dir"] is False
    assert payload["message"] == (
        "1 compatible file. Run one GIF to calibrate time and size estimates."
    )


def test_scan_estimate_counts_recursive_folder(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    nested = lib / "nested"
    nested.mkdir(parents=True)
    (lib / "a.mp4").write_text("x")
    (lib / "b.txt").write_text("x")
    (nested / "c.webm").write_text("x")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(lib))

    payload = res.get_json()
    assert res.status_code == 200
    assert payload["compatible_count"] == 2
    assert payload["is_dir"] is True


def test_scan_estimate_reuses_cached_count_for_same_path(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    (lib / "a.mp4").write_text("x")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    first = client.get("/api/scan-estimate", query_string=_query(lib)).get_json()
    (lib / "b.mp4").write_text("x")
    second = client.get(
        "/api/scan-estimate",
        query_string=_query(lib, height_preset="720"),
    ).get_json()

    assert first["compatible_count"] == 1
    assert second["compatible_count"] == 1


def test_scan_estimate_does_not_follow_symlinked_directories(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    target = tmp_path / "target"
    lib.mkdir()
    target.mkdir()
    (target / "hidden.mp4").write_text("x")
    link = lib / "linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("directory symlinks are unavailable in this environment")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(lib))

    assert res.status_code == 200
    assert res.get_json()["compatible_count"] == 0


def test_scan_estimate_uses_history_for_time_and_storage(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    for name in ("a.mp4", "b.mp4"):
        (lib / name).write_text("x")
    _reset_scan_state(monkeypatch, lib)
    monkeypatch.setattr(
        routes.estimate_history,
        "load_history",
        lambda path=None: [
            {
                "settings_unit": 1,
                "elapsed_seconds": 600,
                "output_size_bytes": 1024 * 1024 * 50,
                "created_at": 1,
            },
            {
                "settings_unit": 1,
                "elapsed_seconds": 300,
                "output_size_bytes": 1024 * 1024 * 25,
                "created_at": 2,
            },
            {
                "settings_unit": 1,
                "elapsed_seconds": 900,
                "output_size_bytes": 1024 * 1024 * 75,
                "created_at": 3,
            },
        ],
    )

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(lib))

    payload = res.get_json()
    assert payload["compatible_count"] == 2
    assert payload["estimated_seconds"] == 1200
    assert payload["estimated_size_bytes"] == 1024 * 1024 * 100
    assert payload["message"] == (
        "2 compatible files, estimated time 20 minutes, "
        "estimated total size 100.0 MB"
    )


def test_scan_estimate_message_does_not_include_dynamic_path(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "video&name.mp4"
    video.write_text("x")
    _reset_scan_state(monkeypatch, lib)

    client = routes.app.test_client()
    res = client.get("/api/scan-estimate", query_string=_query(video))

    payload = res.get_json()
    assert payload["compatible_count"] == 1
    assert "video&name" not in payload["message"]
