import json
import os
import struct
import urllib.error
from pathlib import Path

from app import routes, video_preview_maintenance


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reset_preview_state(monkeypatch, tmp_path):
    log_dir = tmp_path / "state" / "maintenance-logs" / "video-previews"
    monkeypatch.setattr(video_preview_maintenance, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(video_preview_maintenance, "LOG_INDEX", str(log_dir / "index.json"))
    generation_root = tmp_path / "state" / "video-preview-generation"
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_ROOT", str(generation_root))
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_MANIFEST_PATH", str(generation_root / "manifest.json"))
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {
            "duplicate_move_root": "",
            "video_preview_bif_width": 320,
            "video_preview_bif_interval_seconds": 10,
        },
    )
    video_preview_maintenance.preview_scans.clear()
    video_preview_maintenance.quality_scans.clear()
    video_preview_maintenance.quality_plans.clear()
    video_preview_maintenance.quality_apply_runs.clear()
    video_preview_maintenance.generation_plans.clear()
    video_preview_maintenance.generation_runs.clear()
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


def _bif_bytes(frames, multiplier=180000, version=0):
    offset = 64 + (len(frames) + 1) * 8
    entries = []
    data = b""
    for index, frame in enumerate(frames):
        entries.append((index, offset))
        data += frame
        offset += len(frame)
    entries.append((0xFFFFFFFF, offset))
    header = bytearray(64)
    header[:8] = video_preview_maintenance.BIF_MAGIC
    struct.pack_into("<III", header, 8, version, len(frames), multiplier)
    index = b"".join(struct.pack("<II", timestamp, frame_offset) for timestamp, frame_offset in entries)
    return bytes(header) + index + data


def _jpeg(payload):
    return b"\xff\xd8" + payload + b"\xff\xd9"


def _quality_scan(lib, monkeypatch, tmp_path, target=None):
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 900)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)
    scan, err = video_preview_maintenance.start_quality_scan(
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


def test_video_preview_scan_counts_any_stem_matched_bif_as_present(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Present" / "Present.mkv")
    _write(lib / "Present" / "Present-320-180.bif")
    _write(lib / "Missing" / "Missing.mp4")
    _write(lib / "TenSecond" / "TenSecond.mkv")
    _write(lib / "TenSecond" / "TenSecond-320-10.bif")

    scan = _scan(lib, monkeypatch, tmp_path)
    missing, err = video_preview_maintenance.items_payload(scan["id"], status="missing")
    present, err2 = video_preview_maintenance.items_payload(scan["id"], status="present")

    assert err is None
    assert err2 is None
    assert scan["counts"]["scanned_video_count"] == 3
    assert scan["counts"]["present_count"] == 2
    assert scan["counts"]["missing_count"] == 1
    assert "stale_count" not in scan["counts"]
    assert missing["items"][0]["name"] == "Missing.mp4"
    assert {item["name"] for item in present["items"]} == {"Present.mkv", "TenSecond.mkv"}
    ten_second = next(item for item in present["items"] if item["name"] == "TenSecond.mkv")
    assert ten_second["status"] == "present"
    assert ten_second["bifs"][0]["interval_seconds"] == 10


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


def test_bif_parser_reads_header_index_and_samples(tmp_path):
    bif = _write(
        tmp_path / "Movie-320-180.bif",
        _bif_bytes([_jpeg(b"one"), _jpeg(b"two"), _jpeg(b"three")]),
    )

    parsed = video_preview_maintenance.parse_bif(str(bif))

    assert parsed["valid"] is True
    assert parsed["image_count"] == 3
    assert parsed["timestamp_multiplier_ms"] == 180000
    assert parsed["frames"][1]["timestamp_ms"] == 180000
    assert parsed["samples"][0]["jpeg_markers"] is True
    assert "bytes" in parsed["samples"][0]


def test_bif_parser_flags_corrupt_magic_and_offsets(tmp_path):
    corrupt = _write(tmp_path / "bad.bif", b"not a bif")
    parsed = video_preview_maintenance.parse_bif(str(corrupt))

    assert parsed["valid"] is False
    assert "header is incomplete" in parsed["errors"][0]

    data = bytearray(_bif_bytes([_jpeg(b"one")]))
    struct.pack_into("<I", data, 68, 999999)
    invalid = _write(tmp_path / "invalid-offset.bif", bytes(data))

    parsed_invalid = video_preview_maintenance.parse_bif(str(invalid))

    assert parsed_invalid["valid"] is False
    assert any("outside the file" in error or "outside the data section" in error for error in parsed_invalid["errors"])


def test_bif_quality_flags_repeated_frames(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 1260)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "bad"
    assert item["repairable"] is True
    assert item["confidence"] >= 90
    assert "byte-identical" in item["reason"]


def test_bif_quality_flags_severe_frame_count_shortfall(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"one"), _jpeg(b"two")]))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 3600)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "bad"
    assert item["repairable"] is True
    assert item["expected_frame_count"] == 20
    assert item["frame_count"] == 2
    assert item["frame_count_ratio"] == 0.1
    assert item["frame_count_detail"] == "2 / 20"
    assert "fewer frames than expected" in item["reason"]


def test_bif_quality_warns_for_moderate_frame_count_shortfall(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    frames = [_jpeg(f"frame-{index}".encode()) for index in range(16)]
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes(frames))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 3600)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "warning"
    assert item["repairable"] is True
    assert item["expected_frame_count"] == 20
    assert item["frame_count_ratio"] == 0.8
    assert "lower than expected" in item["reason"]


def test_bif_quality_accepts_matching_ten_second_bif(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    frames = [_jpeg(f"frame-{index}".encode()) for index in range(90)]
    bif = _write(lib / "Movie" / "Movie-320-10.bif", _bif_bytes(frames, multiplier=10000))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 900)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "ok"
    assert item["repairable"] is False
    assert item["interval_seconds"] == 10
    assert item["expected_frame_count"] == 90
    assert item["frame_count_detail"] == "90 / 90"
    assert "interval" not in item["reason"].lower()


def test_bif_quality_uses_header_multiplier_when_name_has_no_interval(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    frames = [_jpeg(f"frame-{index}".encode()) for index in range(12)]
    bif = _write(lib / "Movie" / "Movie.bif", _bif_bytes(frames, multiplier=60000))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 720)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "ok"
    assert item["interval_seconds"] == 60
    assert item["expected_frame_count"] == 12
    assert item["frame_count_detail"] == "12 / 12"


def test_bif_quality_skips_expected_count_without_duration(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"one"), _jpeg(b"two")]))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: None)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "ok"
    assert item["expected_frame_count"] is None
    assert item["frame_count_detail"] == "2"
    assert item["reason"] == "BIF passed quality checks"


def test_bif_quality_flags_blank_decoded_frames(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    bif = _write(
        lib / "Movie" / "Movie-320-180.bif",
        _bif_bytes([_jpeg(f"frame-{index}".encode()) for index in range(8)]),
    )
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 900)
    monkeypatch.setattr(
        video_preview_maintenance,
        "_decode_jpeg_fingerprint",
        lambda data, timeout=5: {"hash": str(data), "average_luma": 0},
    )

    item = video_preview_maintenance.analyze_bif_quality(str(bif), str(video), str(lib))

    assert item["status"] == "bad"
    assert "blank" in item["reason"]


def test_bif_quality_scan_and_items_are_bounded(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(105):
        folder = lib / f"Movie {index:03d}"
        _write(folder / f"Movie {index:03d}.mkv")
        _write(folder / f"Movie {index:03d}-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))

    scan = _quality_scan(lib, monkeypatch, tmp_path)
    page, err = video_preview_maintenance.quality_items_payload(
        scan["id"],
        status="bad",
        limit=999,
    )

    assert err is None
    assert scan["counts"]["bad_count"] == 105
    assert "items" not in video_preview_maintenance.public_quality_scan(scan)
    assert page["limit"] == video_preview_maintenance.ITEM_PAGE_MAX
    assert page["count"] == video_preview_maintenance.ITEM_PAGE_MAX
    assert page["large_result"] is True


def test_bif_quality_routes_scan_plan_and_apply(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    bif = _write(movie / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _write(movie / "Movie.mkv")
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 900)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprint", lambda data, timeout=5: None)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    class ImmediateThread:
        def __init__(self, target=None, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            self.target(*self.args, **self.kwargs)

    monkeypatch.setattr(video_preview_maintenance.threading, "Thread", ImmediateThread)
    client = routes.app.test_client()

    scan_res = client.post(
        "/api/maintenance/video-previews/quality/scan",
        json={"path": str(lib), "synchronous": True},
    )
    scan = scan_res.get_json()["scan"]
    items_res = client.get(
        "/api/maintenance/video-previews/quality/items",
        query_string={"scan_id": scan["id"], "status": "bad"},
    )
    plan_res = client.post(
        "/api/maintenance/video-previews/quality/plan",
        json={
            "scan_id": scan["id"],
            "move_root": str(lib / "_repair"),
            "trigger_emby": False,
        },
    )
    plan = plan_res.get_json()["plan"]
    apply_res = client.post(
        "/api/maintenance/video-previews/quality/apply",
        json={"plan_id": plan["id"]},
    )
    status_res = client.get(
        "/api/maintenance/video-previews/quality/apply/status",
        query_string={"apply_id": apply_res.get_json()["apply"]["id"]},
    )

    assert scan_res.status_code == 200
    assert scan["bad_count"] == 1
    assert items_res.status_code == 200
    assert items_res.get_json()["items"][0]["name"] == "Movie-320-180.bif"
    assert plan_res.status_code == 200
    assert plan["file_count"] == 1
    assert apply_res.status_code == 200
    assert status_res.get_json()["apply"]["status"] == "success"
    assert not bif.exists()
    assert (lib / "_repair" / "Movie" / "Movie-320-180.bif").is_file()
    assert status_res.get_json()["apply"]["result"]["log"]["id"].endswith(".jsonl")


def test_bif_quality_routes_reject_invalid_path_and_missing_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    outside = tmp_path / "library-other"
    lib.mkdir()
    outside.mkdir()
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    client = routes.app.test_client()

    scan_res = client.post(
        "/api/maintenance/video-previews/quality/scan",
        json={"path": str(outside), "synchronous": True},
    )
    status_res = client.get(
        "/api/maintenance/video-previews/quality/status",
        query_string={"scan_id": "missing"},
    )
    items_res = client.get(
        "/api/maintenance/video-previews/quality/items",
        query_string={"scan_id": "missing"},
    )
    plan_res = client.post(
        "/api/maintenance/video-previews/quality/plan",
        json={"scan_id": "missing"},
    )

    assert scan_res.status_code == 400
    assert scan_res.get_json()["error"] == "Path not found"
    assert status_res.status_code == 404
    assert items_res.status_code == 404
    assert plan_res.status_code == 400
    assert plan_res.get_json()["error"] == "Scan not found"


def test_bif_quality_apply_refuses_existing_destination_and_continues(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    first = _write(lib / "One" / "One-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    second = _write(lib / "Two" / "Two-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _write(lib / "One" / "One.mkv")
    _write(lib / "Two" / "Two.mkv")
    scan = _quality_scan(lib, monkeypatch, tmp_path)
    move_root = lib / "_repair"
    _write(move_root / "One" / "One-320-180.bif", b"existing")
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {
            "scan_id": scan["id"],
            "move_root": str(move_root),
            "trigger_emby": False,
        },
        lib_root=str(lib),
    )

    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(plan["id"])

    assert err is None
    assert apply_err is None
    assert result["applied_count"] == 1
    assert result["refused_count"] == 1
    assert first.exists()
    assert not second.exists()
    assert (move_root / "Two" / "Two-320-180.bif").is_file()


def test_bif_quality_apply_revalidates_identity(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _write(lib / "Movie" / "Movie.mkv")
    scan = _quality_scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {"scan_id": scan["id"], "move_root": str(lib / "_repair"), "trigger_emby": False},
        lib_root=str(lib),
    )
    bif.write_bytes(_bif_bytes([_jpeg(b"different")] * 8))

    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(plan["id"])

    assert err is None
    assert apply_err is None
    assert result["applied_count"] == 0
    assert result["refused_count"] == 1
    assert bif.exists()


def test_bif_quality_repair_plan_rejects_outside_destination(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    outside = tmp_path / "outside"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    scan = _quality_scan(lib, monkeypatch, tmp_path)

    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {"scan_id": scan["id"], "move_root": str(outside)},
        lib_root=str(lib),
    )

    assert plan is None
    assert err == "Repair destination must be inside the mounted library root"


def test_bif_quality_apply_does_not_trigger_emby_extraction(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    scan = _quality_scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {"scan_id": scan["id"], "move_root": str(lib / "_repair"), "trigger_emby": True},
        lib_root=str(lib),
    )
    calls = []
    monkeypatch.setattr(
        video_preview_maintenance,
        "_settings",
        lambda: {"emby_url": "http://emby:8096", "emby_api_key": "secret"},
    )

    def fake_open(request, timeout):
        calls.append((request.method, request.full_url))
        if request.method == "GET":
            return FakeResponse([{"Id": "thumbs", "Name": "Thumbnail Image Extraction"}])
        return FakeResponse(None, status=204)

    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(
        plan["id"],
        opener=fake_open,
    )

    assert err is None
    assert apply_err is None
    assert result["applied_count"] == 1
    assert calls == []
    assert result["emby"] == {}
    assert "secret" not in str(result)


def test_bif_generation_plan_requires_mismatch_confirmation(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    scan = _scan(lib, monkeypatch, tmp_path)
    scan["recommended_profile"] = {"width": 320, "interval_seconds": 180, "source_name": "Recent-320-180.bif"}
    missing_id = scan["items"][0]["id"]

    plan, err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [missing_id]},
        lib_root=str(lib),
    )

    assert plan is None
    assert "differ from the latest observed" in err
    confirmed, confirmed_err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [missing_id], "confirm_profile_mismatch": True},
        lib_root=str(lib),
    )
    assert confirmed_err is None
    assert confirmed["interval_seconds"] == 10


def test_bif_generation_stages_validates_and_installs_missing_output(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv", b"video")
    scan = _scan(lib, monkeypatch, tmp_path)
    missing_id = scan["items"][0]["id"]
    plan, err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [missing_id]},
        lib_root=str(lib),
    )
    assert err is None

    def fake_extract(_video, pattern, _width, _interval, _run):
        _write(Path(pattern % 1), _jpeg(b"frame-one"))
        _write(Path(pattern % 2), _jpeg(b"frame-two"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)
    monkeypatch.setattr(video_preview_maintenance, "_refresh_emby_library", lambda **_kwargs: {"status": "success"})

    run, run_err = video_preview_maintenance.start_generation(plan["id"], synchronous=True)

    assert run_err is None
    assert run["status"] == "success"
    assert run["generated_count"] == 1
    output = video.parent / "Movie-320-10.bif"
    assert output.is_file()
    parsed = video_preview_maintenance.parse_bif(str(output))
    assert parsed["valid"] is True
    assert parsed["image_count"] == 2
    assert parsed["timestamp_multiplier_ms"] == 10_000


def test_bif_generation_refuses_late_matching_bif(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv", b"video")
    scan = _scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [scan["items"][0]["id"]]},
        lib_root=str(lib),
    )
    assert err is None
    _write(video.parent / "Movie-existing.bif", _bif_bytes([_jpeg(b"existing")], multiplier=10_000))

    def fake_extract(_video, pattern, _width, _interval, _run):
        _write(Path(pattern % 1), _jpeg(b"new"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)
    run, run_err = video_preview_maintenance.start_generation(plan["id"], synchronous=True)

    assert run_err is None
    assert run["generated_count"] == 0
    assert run["refused_count"] == 1
    assert not (video.parent / "Movie-320-10.bif").exists()


def test_bif_quality_cancel_reuses_active_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _reset_preview_state(monkeypatch, tmp_path)

    class FakeThread:
        def __init__(self, target=None, args=(), kwargs=None, **_options):
            self.target = target
            self.args = args
            self.kwargs = kwargs or {}

        def start(self):
            return None

    monkeypatch.setattr(video_preview_maintenance.threading, "Thread", FakeThread)
    first, err = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib))
    second, err2 = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib))
    cancelled, cancel_err = video_preview_maintenance.cancel_quality_scan(first["id"])

    assert err is None
    assert err2 is None
    assert first["id"] == second["id"]
    assert cancel_err is None
    assert cancelled["status"] == "cancelled"


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
    assert 'id="previewPresentCount"' in html
    assert "Interval Mismatch" not in html
    assert "Interval mismatches" not in html
    assert 'id="qualityScanButton"' in html
    assert 'id="qualityApplyButton"' in html
    assert 'id="previewGenerationPlanButton"' in html
    assert 'id="previewGenerationStartButton"' in html
    assert 'id="qualityAction"' in html
    assert 'id="qualitySelectWarningButton"' in html
    assert "fetch('/api/maintenance/video-previews/scan'" in script
    assert "/api/maintenance/video-previews/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/emby/tasks')" in script
    assert "fetch('/api/maintenance/video-previews/emby/run-extraction'" in script
    assert "fetch('/api/maintenance/video-previews/quality/scan'" in script
    assert "/api/maintenance/video-previews/quality/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/quality/plan'" in script
    assert "fetch('/api/maintenance/video-previews/quality/apply'" in script
    assert "/api/maintenance/video-previews/quality/apply/status?apply_id=" in script
    assert "fetch('/api/maintenance/video-previews/generation/plan'" in script
    assert "fetch('/api/maintenance/video-previews/generation/start'" in script
    assert "/api/maintenance/video-previews/generation/status?run_id=" in script
    assert "escapeHtml(item.relative_path" in script
    assert "escapeHtml(change.source || '')" in script
    assert "frame_count_detail" in script
    assert "Frames Actual / Expected" in script
    assert "interval mismatch" not in script
