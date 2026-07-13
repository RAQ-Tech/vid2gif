import json
import os
import struct
import subprocess
import sys
import time
import urllib.error
from pathlib import Path

from app import (
    app_settings,
    emby_catalog,
    impact_metrics,
    maintenance_scan_store,
    routes,
    video_preview_maintenance,
)


def _write(path, data=b"x"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _reset_preview_state(monkeypatch, tmp_path):
    state_root = tmp_path / "state"
    monkeypatch.setattr(maintenance_scan_store.config, "STATE_ROOT", str(state_root))
    log_dir = tmp_path / "state" / "maintenance-logs" / "video-previews"
    monkeypatch.setattr(video_preview_maintenance, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(video_preview_maintenance, "LOG_INDEX", str(log_dir / "index.json"))
    generation_root = tmp_path / "state" / "video-preview-generation"
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_ROOT", str(generation_root))
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_MANIFEST_PATH", str(generation_root / "manifest.json"))
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_RUN_PATH", str(generation_root / "latest-run.json"))
    impact_root = tmp_path / "state" / "dashboard"
    monkeypatch.setattr(impact_metrics, "IMPACT_ROOT", str(impact_root))
    monkeypatch.setattr(impact_metrics, "IMPACT_PATH", str(impact_root / "impact-metrics.json"))
    monkeypatch.setattr(impact_metrics, "IMPACT_BACKUP_PATH", str(impact_root / "impact-metrics.json.bak"))
    impact_metrics._last_error = ""
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
    monkeypatch.setattr(video_preview_maintenance, "_preview_cache_loaded", True)
    monkeypatch.setattr(video_preview_maintenance, "_quality_cache_loaded", True)
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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])
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


def test_video_preview_scans_skip_local_trailer_folders(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "trailers" / "Movie Trailer.mp4")
    _write(lib / "Other" / "TRAILER" / "Other Trailer.mkv")

    missing_scan = _scan(lib, monkeypatch, tmp_path)
    quality_scan = _quality_scan(lib, monkeypatch, tmp_path)

    assert {item["name"] for item in missing_scan["items"]} == {"Movie.mkv"}
    assert not any("trailer" in item.get("video_relative_path", "").lower() for item in quality_scan["items"])


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


def test_bif_batch_decoder_uses_one_ffmpeg_process(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = (
            bytes([16]) * video_preview_maintenance.DECODED_FINGERPRINT_BYTES
            + bytes([224]) * video_preview_maintenance.DECODED_FINGERPRINT_BYTES
        )

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Result()

    monkeypatch.setattr(video_preview_maintenance.subprocess, "run", fake_run)

    decoded = video_preview_maintenance._decode_jpeg_fingerprints(
        [b"first-jpeg", b"second-jpeg"]
    )

    assert len(calls) == 1
    assert calls[0][1]["input"] == b"first-jpegsecond-jpeg"
    assert calls[0][0][calls[0][0].index("-frames:v") + 1] == "2"
    assert len(decoded) == 2
    assert decoded[0]["hash"] != decoded[1]["hash"]


def test_bif_batch_decoder_falls_back_when_output_is_partial(monkeypatch):
    calls = []

    class Result:
        returncode = 0
        stdout = bytes([16]) * video_preview_maintenance.DECODED_FINGERPRINT_BYTES

    monkeypatch.setattr(
        video_preview_maintenance.subprocess,
        "run",
        lambda *args, **kwargs: Result(),
    )
    monkeypatch.setattr(
        video_preview_maintenance,
        "_decode_jpeg_fingerprint",
        lambda frame, timeout=5: calls.append(frame) or {
            "hash": frame.decode(),
            "average_luma": 100,
        },
    )

    decoded = video_preview_maintenance._decode_jpeg_fingerprints([b"one", b"two"])

    assert calls == [b"one", b"two"]
    assert [item["hash"] for item in decoded] == ["one", "two"]


def test_bif_quality_flags_repeated_frames(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 1260)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])

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
        "_decode_jpeg_fingerprints",
        lambda frames, timeout=5: [
            {"hash": str(data), "average_luma": 0} for data in frames
        ],
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


def test_bif_quality_incremental_reuse_and_full_scan_process_counts(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    first_video = _write(lib / "One" / "One.mkv", b"video-one")
    first_bif = _write(
        lib / "One" / "One-320-180.bif",
        _bif_bytes([_jpeg(f"one-{index}".encode()) for index in range(8)]),
    )
    _write(lib / "Two" / "Two.mkv", b"video-two")
    _write(
        lib / "Two" / "Two-320-180.bif",
        _bif_bytes([_jpeg(f"two-{index}".encode()) for index in range(8)]),
    )
    _reset_preview_state(monkeypatch, tmp_path)
    calls = {"probe": 0, "decode": 0}

    def fake_probe(path, timeout=10):
        calls["probe"] += 1
        return 900

    def fake_decode(frames, timeout=5):
        calls["decode"] += 1
        return []

    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", fake_probe)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", fake_decode)

    first, err = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )
    second, err2 = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )

    assert err is None and err2 is None
    assert first["counts"]["analyzed_count"] == 2
    assert first["counts"]["reused_count"] == 0
    assert second["counts"]["analyzed_count"] == 0
    assert second["counts"]["reused_count"] == 2
    assert calls == {"probe": 2, "decode": 2}

    first_bif.write_bytes(
        _bif_bytes([_jpeg(f"one-changed-{index}".encode()) for index in range(8)])
    )
    changed_bif, err3 = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )

    assert err3 is None
    assert changed_bif["counts"]["analyzed_count"] == 1
    assert changed_bif["counts"]["reused_count"] == 1
    assert changed_bif["counts"]["cached_duration_count"] == 1
    assert calls == {"probe": 2, "decode": 3}

    first_video.write_bytes(b"video-one-was-replaced")
    changed_video, err4 = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )

    assert err4 is None
    assert changed_video["counts"]["analyzed_count"] == 1
    assert changed_video["counts"]["reused_count"] == 1
    assert changed_video["counts"]["ffprobe_duration_count"] == 1
    assert calls == {"probe": 3, "decode": 4}

    full, err5 = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True, force_full=True
    )

    assert err5 is None
    assert full["scan_mode"] == "full"
    assert full["counts"]["analyzed_count"] == 2
    assert full["counts"]["reused_count"] == 0
    assert full["counts"]["cached_duration_count"] == 0
    assert calls == {"probe": 5, "decode": 6}


def test_bif_quality_analyzer_signature_invalidates_cached_result(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(
        lib / "Movie" / "Movie-320-180.bif",
        _bif_bytes([_jpeg(f"frame-{index}".encode()) for index in range(8)]),
    )
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video_preview_maintenance,
        "_probe_video_duration",
        lambda path, timeout=10: 900,
    )
    monkeypatch.setattr(
        video_preview_maintenance,
        "_decode_jpeg_fingerprints",
        lambda frames, timeout=5: [],
    )
    first, _err = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )
    first["items"][0]["analysis_signature"] = "older-analyzer"

    second, err = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )

    assert err is None
    assert second["counts"]["analyzed_count"] == 1
    assert second["counts"]["reused_count"] == 0
    assert second["counts"]["cached_duration_count"] == 1


def test_bif_quality_uses_emby_duration_without_ffprobe(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    _write(
        lib / "Movie" / "Movie-320-180.bif",
        _bif_bytes([_jpeg(f"frame-{index}".encode()) for index in range(5)]),
    )
    _reset_preview_state(monkeypatch, tmp_path)
    settings = {
        "emby_url": "http://emby:8096",
        "emby_api_key": "secret",
        "emby_path_mappings": [],
    }
    catalog = emby_catalog._build_catalog(
        [
            {
                "Id": "movie-1",
                "Name": "Movie",
                "Type": "Movie",
                "Path": str(video),
                "RunTimeTicks": 9_000_000_000,
            }
        ],
        {"Id": "server"},
        emby_catalog.configuration_fingerprint(settings),
    )
    summary = emby_catalog.known_matches_summary(
        settings, 1, catalog_item_count=1, server_id="server"
    )
    monkeypatch.setattr(video_preview_maintenance.app_settings, "load_settings", lambda: settings)
    monkeypatch.setattr(
        video_preview_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, summary),
    )
    monkeypatch.setattr(
        video_preview_maintenance,
        "_probe_video_duration",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("ffprobe should not run")),
    )
    monkeypatch.setattr(
        video_preview_maintenance,
        "_decode_jpeg_fingerprints",
        lambda frames, timeout=5: [],
    )

    scan, err = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True, force_full=True
    )

    assert err is None
    assert scan["status"] == "success"
    assert scan["counts"]["emby_duration_count"] == 1
    assert scan["counts"]["ffprobe_duration_count"] == 0
    assert scan["items"][0]["duration_seconds"] == 900
    assert scan["items"][0]["duration_source"] == "emby"


def test_bif_quality_marks_scan_stale_when_library_changes_during_scan(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    _write(
        lib / "Movie" / "Movie-320-180.bif",
        _bif_bytes([_jpeg(f"frame-{index}".encode()) for index in range(8)]),
    )
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video_preview_maintenance,
        "_probe_video_duration",
        lambda path, timeout=10: 900,
    )
    monkeypatch.setattr(
        video_preview_maintenance,
        "_decode_jpeg_fingerprints",
        lambda frames, timeout=5: [],
    )
    capture = video_preview_maintenance._capture_quality_manifest

    def mutate_then_capture(scan, lib_root):
        _write(
            lib / "Movie" / "Movie-320-10.bif",
            _bif_bytes([_jpeg(f"changed-{index}".encode()) for index in range(8)])
        )
        return capture(scan, lib_root)

    monkeypatch.setattr(
        video_preview_maintenance,
        "_capture_quality_manifest",
        mutate_then_capture,
    )

    scan, err = video_preview_maintenance.start_quality_scan(
        str(lib), lib_root=str(lib), synchronous=True
    )
    public = video_preview_maintenance.public_quality_scan(scan)
    allowed, action_error = maintenance_scan_store.action_allowed(
        "video_previews_quality", scan["id"], str(lib)
    )

    assert err is None
    assert scan["status"] == "success"
    assert public["freshness"]["status"] == "changed"
    assert public["freshness"]["added"] == 1
    assert allowed is False
    assert "changed" in action_error.lower()


def test_bif_quality_routes_scan_plan_and_apply(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    movie = lib / "Movie"
    bif = _write(movie / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _write(movie / "Movie.mkv")
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(video_preview_maintenance, "_probe_video_duration", lambda path, timeout=10: 900)
    monkeypatch.setattr(video_preview_maintenance, "_decode_jpeg_fingerprints", lambda frames, timeout=5: [])
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
        json={"path": str(lib), "synchronous": True, "force_full": True},
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
    assert scan["scan_mode"] == "full"
    assert scan["analyzed_count"] == 1
    assert scan["reused_count"] == 0
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


def test_removing_last_bad_bif_keeps_preview_issue_open_as_missing(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    _write(lib / "Movie" / "Movie.mkv")
    scan = _quality_scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {"scan_id": scan["id"], "move_root": str(lib / "_repair")},
        lib_root=str(lib),
    )

    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(plan["id"])
    impact = impact_metrics.status_payload()

    assert err is None
    assert apply_err is None
    assert result["applied_count"] == 1
    assert not bif.exists()
    assert impact["total_fixes"] == 0
    assert impact["discovered_count"] == 1
    assert impact["open_count"] == 1


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
    sync_calls = []
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

    def fake_sync(changes, **kwargs):
        sync_calls.append((changes, kwargs))
        return {"id": "sync-quality", "status": "success", "retryable": False}

    monkeypatch.setattr(video_preview_maintenance.emby_sync, "sync_changes", fake_sync)

    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(
        plan["id"],
        opener=fake_open,
    )

    assert err is None
    assert apply_err is None
    assert result["applied_count"] == 1
    assert calls == []
    assert result["emby_sync"]["id"] == "sync-quality"
    assert sync_calls[0][0][0]["update_type"] == "Deleted"
    assert sync_calls[0][0][0]["refresh_scope"] == "thumbnail"
    assert "secret" not in str(result)


def test_bif_quality_cleanup_defers_when_playback_is_unverified(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    bif = _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    scan = _quality_scan(lib, monkeypatch, tmp_path)

    def unavailable(targets, **kwargs):
        return {
            "status": "unavailable",
            "checked_at": "now",
            "active_session_count": 0,
            "active_item_count": 0,
            "target_count": len(targets),
            "clear_count": 0,
            "active_count": 0,
            "unverified_count": len(targets),
            "deferred_count": len(targets),
            "message": "Unavailable",
            "_target_statuses": {target["id"]: "unverified" for target in targets},
        }

    monkeypatch.setattr(video_preview_maintenance.emby_playback, "check_targets", unavailable)
    monkeypatch.setattr(
        video_preview_maintenance.emby_sync,
        "sync_changes",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync should not run")),
    )
    notification_calls = []
    monkeypatch.setattr(
        video_preview_maintenance.emby_notifications,
        "notify_maintenance",
        lambda *args, **kwargs: notification_calls.append((args, kwargs))
        or {"id": "notice", "status": "success", "message": "accepted"},
    )
    plan, err = video_preview_maintenance.build_quality_repair_plan(
        {"scan_id": scan["id"], "move_root": str(lib / "_repair")},
        lib_root=str(lib),
    )
    result, apply_err = video_preview_maintenance.apply_quality_repair_plan(plan["id"])

    assert err is None
    assert apply_err is None
    assert plan["emby_playback"]["unverified_count"] == 1
    assert result["applied_count"] == 0
    assert result["deferred_count"] == 1
    assert result["refused_count"] == 0
    assert notification_calls[0][1]["deferred_count"] == 1
    assert result["emby_notification"]["id"] == "notice"
    assert bif.exists()


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
    duration_probes = []
    monkeypatch.setattr(
        video_preview_maintenance,
        "_probe_video_duration",
        lambda path: duration_probes.append(path) or 10,
    )
    sync_calls = []

    def fake_sync(changes, **kwargs):
        sync_calls.append((changes, kwargs))
        return {"id": "sync-generation", "status": "success", "retryable": False}

    monkeypatch.setattr(video_preview_maintenance.emby_sync, "sync_changes", fake_sync)
    notification_calls = []
    monkeypatch.setattr(
        video_preview_maintenance.emby_notifications,
        "notify_maintenance",
        lambda *args, **kwargs: notification_calls.append((args, kwargs))
        or {"id": "notice-generation", "status": "success", "message": "accepted"},
    )

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
    assert sync_calls[0][0][0]["local_path"] == str(output)
    assert sync_calls[0][0][0]["update_type"] == "Created"
    assert sync_calls[0][0][0]["refresh_scope"] == "thumbnail"
    assert notification_calls[0][1]["succeeded_count"] == 1
    assert run["emby_notification"]["id"] == "notice-generation"
    assert duration_probes == [str(video)]


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


def test_bif_generation_continues_after_one_video_fails_and_persists_results(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    broken = _write(lib / "A Broken" / "A Broken.mkv", b"broken")
    healthy = _write(lib / "B Healthy" / "B Healthy.mkv", b"healthy")
    scan = _scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [item["id"] for item in scan["items"]]},
        lib_root=str(lib),
    )
    assert err is None

    def fake_extract(video_path, pattern, _width, _interval, _run):
        if video_path == str(broken):
            raise RuntimeError("decoder rejected corrupt video")
        _write(Path(pattern % 1), _jpeg(b"healthy-frame"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)
    monkeypatch.setattr(video_preview_maintenance.emby_sync, "sync_changes", lambda *_args, **_kwargs: None)
    run, run_err = video_preview_maintenance.start_generation(plan["id"], synchronous=True)

    assert run_err is None
    assert run["status"] == "success"
    assert run["processed_count"] == 2
    assert run["generated_count"] == 1
    assert run["refused_count"] == 1
    assert run["items"][0]["reason"] == "decoder rejected corrupt video"
    assert (healthy.parent / "B Healthy-320-10.bif").is_file()
    assert Path(video_preview_maintenance.GENERATION_RUN_PATH).is_file()

    video_preview_maintenance.generation_runs.clear()
    restored, restored_err = video_preview_maintenance.generation_status(run["id"])
    assert restored_err is None
    assert restored["run"]["restored"] is True
    assert restored["run"]["generated_count"] == 1
    assert restored["run"]["items"][0]["status"] == "refused"


def test_generation_status_marks_unfinished_persisted_run_interrupted(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)
    video_preview_maintenance._write_json(
        video_preview_maintenance.GENERATION_RUN_PATH,
        {
            "schema_version": video_preview_maintenance.GENERATION_RUN_SCHEMA_VERSION,
            "run": {
                "id": "unfinished-run",
                "status": "running",
                "file_count": 25,
                "processed_count": 3,
                "generated_count": 3,
                "refused_count": 0,
                "progress_percent": 12,
                "progress_label": "Video 4 of 25",
                "items": [],
            },
        },
    )

    payload, err = video_preview_maintenance.generation_status("unfinished-run")

    assert err is None
    assert payload["run"]["status"] == "interrupted"
    assert "stopped or restarted" in payload["run"]["error"]
    assert payload["run"]["restored"] is True


def test_generation_cancellation_does_not_mislabel_current_video_refused(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv", b"video")
    scan = _scan(lib, monkeypatch, tmp_path)
    plan, err = video_preview_maintenance.build_generation_plan(
        {"scan_id": scan["id"], "item_ids": [scan["items"][0]["id"]]},
        lib_root=str(lib),
    )
    assert err is None

    def cancel_extract(*_args, **_kwargs):
        raise video_preview_maintenance.ScanCancelled()

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", cancel_extract)
    run, run_err = video_preview_maintenance.start_generation(plan["id"], synchronous=True)

    assert run_err is None
    assert run["status"] == "cancelled"
    assert run["processed_count"] == 0
    assert run["refused_count"] == 0
    assert run["items"] == []


def test_frame_extraction_drains_large_stderr_without_pipe_deadlock(monkeypatch, tmp_path):
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()
    real_popen = subprocess.Popen

    def noisy_popen(_command, **kwargs):
        return real_popen(
            [
                sys.executable,
                "-c",
                "import sys; sys.stderr.buffer.write(b'x' * 262144); sys.stderr.flush(); raise SystemExit(1)",
            ],
            **kwargs,
        )

    monkeypatch.setattr(video_preview_maintenance.subprocess, "Popen", noisy_popen)
    started = time.monotonic()
    try:
        video_preview_maintenance._run_frame_extraction(
            "ignored.mkv",
            str(frame_dir / "%08d.jpg"),
            320,
            10,
            {"id": "noisy", "file_count": 1, "processed_count": 0},
        )
        assert False, "Expected noisy FFmpeg failure"
    except RuntimeError as exc:
        assert len(str(exc)) <= 2000
    assert time.monotonic() - started < 5


def test_frame_extraction_times_out_when_no_frames_advance(monkeypatch, tmp_path):
    frame_dir = tmp_path / "frames"
    frame_dir.mkdir()

    class EmptyStderr:
        def read(self, _size):
            return b""

    class StalledProcess:
        def __init__(self):
            self.returncode = None
            self.stderr = EmptyStderr()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    commands = []

    def stalled_popen(command, **_kwargs):
        commands.append(command)
        return StalledProcess()

    monkeypatch.setattr(video_preview_maintenance, "GENERATION_STALL_TIMEOUT_SECONDS", 1)
    monkeypatch.setattr(video_preview_maintenance.subprocess, "Popen", stalled_popen)
    try:
        video_preview_maintenance._run_frame_extraction(
            "ignored.mkv",
            str(frame_dir / "%08d.jpg"),
            320,
            10,
            {"id": "stalled", "file_count": 1, "processed_count": 0},
        )
        assert False, "Expected stalled FFmpeg failure"
    except RuntimeError as exc:
        assert "no frame progress" in str(exc)
    assert "-xerror" in commands[0]
    assert commands[0][commands[0].index("-map") + 1] == "0:v:0"


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
        captured.append(
            (
                request.method,
                request.full_url,
                request.data,
                timeout,
                request.get_header("X-emby-token"),
            )
        )
        if request.method == "GET":
            return FakeResponse(
                [
                    {"Id": "other", "Name": "Refresh Guide"},
                        {
                            "Id": "task1",
                            "Name": "Thumbnail Image Extraction",
                            "Key": "ExtractChapterImages",
                            "State": "Idle",
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
    assert captured[0][1] == "http://emby:8096/emby/ScheduledTasks?IsHidden=false"
    assert captured[-1][1] == "http://emby:8096/emby/ScheduledTasks/Running/task1"
    assert captured[-1][2] == b""
    assert all(call[4] == "abc 123" for call in captured)
    assert "abc 123" not in str(payload)


def test_video_preview_emby_handles_base_url_ending_in_emby(monkeypatch, tmp_path):
    _reset_preview_state(monkeypatch, tmp_path)
    captured = {}

    def fake_open(request, timeout):
        captured["url"] = request.full_url
        captured["token"] = request.get_header("X-emby-token")
        return FakeResponse([])

    video_preview_maintenance.discover_thumbnail_tasks(
        settings={"emby_url": "http://emby:8096/emby", "emby_api_key": "secret"},
        opener=fake_open,
    )

    assert captured["url"] == "http://emby:8096/emby/ScheduledTasks?IsHidden=false"
    assert captured["token"] == "secret"


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
    assert 'id="qualityFullScanButton"' in html
    assert 'id="qualityApplyButton"' in html
    assert 'id="previewGenerationPlanButton"' in html
    assert 'id="previewGenerationStartButton"' in html
    assert 'id="previewBrowserCollapse" class="collapse"' in html
    assert 'id="previewGenerationStatus"' in html
    assert 'id="previewGenerationCurrent"' in html
    assert 'id="qualityAction"' in html
    assert 'id="qualitySelectWarningButton"' in html
    assert "fetch('/api/maintenance/video-previews/scan'" in script
    assert "/api/maintenance/video-previews/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/emby/tasks')" in script
    assert "fetch('/api/maintenance/video-previews/emby/run-extraction'" in script
    assert "fetch('/api/maintenance/video-previews/quality/scan'" in script
    assert "force_full: forceFull" in script
    assert "/api/maintenance/video-previews/quality/items?scan_id=" in script
    assert "fetch('/api/maintenance/video-previews/quality/plan'" in script
    assert "fetch('/api/maintenance/video-previews/quality/apply'" in script
    assert "/api/maintenance/video-previews/quality/apply/status?apply_id=" in script
    assert "fetch('/api/maintenance/video-previews/generation/plan'" in script
    assert "fetch('/api/maintenance/video-previews/generation/start'" in script
    assert "/api/maintenance/video-previews/generation/status?run_id=" in script
    assert "fetch('/api/maintenance/video-previews/scan-path'" in script
    assert "refreshGenerationStatus();" in script
    assert "openPreviewBrowser(config.libRoot || '/library');" not in script
    assert "escapeHtml(item.relative_path" in script
    assert "escapeHtml(change.source || '')" in script
    assert "frame_count_detail" in script
    assert "Frames Actual / Expected" in script
    assert "interval mismatch" not in script


def test_video_preview_scan_source_is_validated_and_persisted(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    selected = lib / "XXX"
    selected.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    settings_path = tmp_path / "state" / "app_settings.json"
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))
    monkeypatch.setattr(app_settings, "LIB_ROOT", str(lib))
    monkeypatch.setattr(app_settings, "SETTINGS_PATH", str(settings_path))

    client = routes.app.test_client()
    saved = client.post(
        "/api/maintenance/video-previews/scan-path",
        json={"path": str(selected)},
    )
    rejected = client.post(
        "/api/maintenance/video-previews/scan-path",
        json={"path": str(outside)},
    )
    page = client.get("/maintenance")

    assert saved.status_code == 200
    assert saved.get_json()["scan_source"]["path"] == str(selected.resolve())
    assert rejected.status_code == 400
    assert app_settings.load_settings()["video_preview_scan_path"] == str(selected.resolve())
    assert f'value="{selected.resolve()}"' in page.get_data(as_text=True)


def test_both_preview_scans_publish_emby_identity(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mkv")
    _write(lib / "Movie" / "Movie-320-180.bif", _bif_bytes([_jpeg(b"same")] * 8))
    catalog = emby_catalog._build_catalog(
        [{"Id": "movie-1", "Name": "Movie", "Type": "Movie", "Path": str(video)}],
        {"Id": "server"},
        emby_catalog.configuration_fingerprint({}),
    )
    monkeypatch.setattr(
        video_preview_maintenance.emby_catalog,
        "load_catalog",
        lambda *args, **kwargs: (catalog, emby_catalog.known_matches_summary({}, 0, catalog_item_count=1)),
    )

    missing_scan = _scan(lib, monkeypatch, tmp_path)
    assert missing_scan["items"][0]["emby_item_id"] == "movie-1"
    assert missing_scan["items"][0]["bifs"][0]["emby_parent_item_id"] == "movie-1"

    quality_scan = _quality_scan(lib, monkeypatch, tmp_path)
    assert quality_scan["items"][0]["emby_item_id"] == "movie-1"
    assert video_preview_maintenance.public_quality_scan(quality_scan)["emby_mapping"]["matched_count"] == 1
