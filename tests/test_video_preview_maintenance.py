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
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_ISSUES_PATH", str(generation_root / "issues.json"))
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
    assert (
        video_preview_maintenance._bif_owner_stem(
            "Movie 2024-01-05-320-10.bif",
            ["Movie 2024", "Movie 2024-01-05"],
        )
        == "Movie 2024-01-05"
    )


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


def test_video_preview_scan_tracks_each_release_in_shared_folder_by_exact_stem(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    folder = lib / "Studio" / "Shared Title"
    stems = [
        "Shared Title - 2024-01-05 [WEBDL-2160p]",
        "Shared Title - 2024-05-20 [WEBDL-2160p]",
        "Shared Title - 2025-02-14 [WEBDL-2160p]",
    ]
    for stem in stems:
        _write(folder / f"{stem}.mp4")
    _write(folder / f"{stems[1]}-320-10.bif")

    scan = _scan(lib, monkeypatch, tmp_path)

    assert scan["counts"] == {
        "scanned_video_count": 3,
        "present_count": 1,
        "missing_count": 2,
    }
    by_name = {item["name"]: item for item in scan["items"]}
    assert by_name[f"{stems[1]}.mp4"]["status"] == "present"
    assert by_name[f"{stems[0]}.mp4"]["status"] == "missing"
    assert by_name[f"{stems[2]}.mp4"]["status"] == "missing"


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


def test_missing_bif_selection_spans_pages_and_holds_previous_failures(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    for index in range(30):
        _write(lib / f"Movie {index:03d}" / f"Movie {index:03d}.mkv")

    scan = _scan(lib, monkeypatch, tmp_path)
    held_item = scan["items"][27]
    video_preview_maintenance._write_json(
        video_preview_maintenance.GENERATION_ISSUES_PATH,
        {
            "schema_version": video_preview_maintenance.GENERATION_ISSUES_SCHEMA_VERSION,
            "records": {
                held_item["id"]: {
                    "item_id": held_item["id"],
                    "status": "refused",
                    "reason": "decoder rejected this video",
                    "video_relative_path": held_item["relative_path"],
                    "video_identity": held_item.get("video_identity")
                    or video_preview_maintenance._stat_identity(held_item["path"]),
                    "retryable": False,
                    # Written by the current extraction logic, so it still holds.
                    "extraction_logic_version": video_preview_maintenance.EXTRACTION_LOGIC_VERSION,
                },
            },
        },
    )

    second_page, page_err = video_preview_maintenance.items_payload(scan["id"], status="missing", offset=25, limit=25)
    default_plan, default_err = video_preview_maintenance.build_generation_plan(
        {
            "scan_id": scan["id"],
            "selection": {
                "mode": "all_eligible",
                "excluded_item_ids": [],
                "include_held_item_ids": [],
            },
        },
        lib_root=str(lib),
    )
    included_plan, included_err = video_preview_maintenance.build_generation_plan(
        {
            "scan_id": scan["id"],
            "selection": {
                "mode": "all_eligible",
                "excluded_item_ids": [scan["items"][0]["id"]],
                "include_held_item_ids": [held_item["id"]],
            },
        },
        lib_root=str(lib),
    )

    assert page_err is None
    assert second_page["selection"] == {
        "missing_total": 30,
        "held_count": 1,
        "default_selected_count": 29,
    }
    held_public = next(item for item in second_page["items"] if item["id"] == held_item["id"])
    assert held_public["generation_held"] is True
    assert held_public["previous_generation_issue"]["reason"] == "decoder rejected this video"
    stored = video_preview_maintenance._generation_issues()["records"][held_item["id"]]
    assert stored["extraction_logic_version"] == video_preview_maintenance.EXTRACTION_LOGIC_VERSION
    assert default_err is None
    assert default_plan["file_count"] == 29
    assert default_plan["held_back_count"] == 1
    assert held_item["id"] not in {item["item_id"] for item in default_plan["files"]}
    assert included_err is None
    assert included_plan["file_count"] == 29
    assert included_plan["held_override_count"] == 1
    assert held_item["id"] in {item["item_id"] for item in included_plan["files"]}


def test_recommended_bif_profile_opens_newest_valid_candidate_first(monkeypatch, tmp_path):
    older = _write(tmp_path / "Older-320-10.bif", b"older")
    newest = _write(tmp_path / "Newest-640-20.bif", b"newest")
    os.utime(older, (100, 100))
    os.utime(newest, (200, 200))
    parsed = []
    monkeypatch.setattr(video_preview_maintenance, "_generation_manifest", lambda: {"records": {}})
    monkeypatch.setattr(
        video_preview_maintenance,
        "parse_bif",
        lambda path, sample_limit=1: (
            parsed.append(path)
            or {
                "valid": True,
                "samples": [{"bytes": _jpeg(b"frame")}],
                "timestamp_multiplier_ms": 20_000,
            }
        ),
    )

    profile = video_preview_maintenance._recommended_bif_profile(
        [
            {
                "bifs": [
                    {"path": str(older), "name": older.name, "interval_seconds": 10},
                    {"path": str(newest), "name": newest.name, "interval_seconds": 20},
                ]
            }
        ]
    )

    assert profile["width"] == 640
    assert profile["interval_seconds"] == 20
    assert parsed == [str(newest)]


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

    decoded = video_preview_maintenance._decode_jpeg_fingerprints([b"first-jpeg", b"second-jpeg"])

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
        lambda frame, timeout=5: (
            calls.append(frame)
            or {
                "hash": frame.decode(),
                "average_luma": 100,
            }
        ),
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
        lambda frames, timeout=5: [{"hash": str(data), "average_luma": 0} for data in frames],
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

    first, err = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)
    second, err2 = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)

    assert err is None and err2 is None
    assert first["counts"]["analyzed_count"] == 2
    assert first["counts"]["reused_count"] == 0
    assert second["counts"]["analyzed_count"] == 0
    assert second["counts"]["reused_count"] == 2
    assert calls == {"probe": 2, "decode": 2}

    first_bif.write_bytes(_bif_bytes([_jpeg(f"one-changed-{index}".encode()) for index in range(8)]))
    changed_bif, err3 = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)

    assert err3 is None
    assert changed_bif["counts"]["analyzed_count"] == 1
    assert changed_bif["counts"]["reused_count"] == 1
    assert changed_bif["counts"]["cached_duration_count"] == 1
    assert calls == {"probe": 2, "decode": 3}

    first_video.write_bytes(b"video-one-was-replaced")
    changed_video, err4 = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)

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
    first, _err = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)
    first["items"][0]["analysis_signature"] = "older-analyzer"

    second, err = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)

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
    summary = emby_catalog.known_matches_summary(settings, 1, catalog_item_count=1, server_id="server")
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
            lib / "Movie" / "Movie-320-10.bif", _bif_bytes([_jpeg(f"changed-{index}".encode()) for index in range(8)])
        )
        return capture(scan, lib_root)

    monkeypatch.setattr(
        video_preview_maintenance,
        "_capture_quality_manifest",
        mutate_then_capture,
    )

    scan, err = video_preview_maintenance.start_quality_scan(str(lib), lib_root=str(lib), synchronous=True)
    public = video_preview_maintenance.public_quality_scan(scan)
    allowed, action_error = maintenance_scan_store.action_allowed("video_previews_quality", scan["id"], str(lib))

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
        lambda *args, **kwargs: (
            notification_calls.append((args, kwargs)) or {"id": "notice", "status": "success", "message": "accepted"}
        ),
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

    def fake_extract(_video, pattern, _width, _interval, _run, _tactic=None):
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
        lambda *args, **kwargs: (
            notification_calls.append((args, kwargs))
            or {"id": "notice-generation", "status": "success", "message": "accepted"}
        ),
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

    def fake_extract(_video, pattern, _width, _interval, _run, _tactic=None):
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

    def fake_extract(video_path, pattern, _width, _interval, _run, _tactic=None):
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
    issues = video_preview_maintenance._generation_issues()["records"]
    assert scan["items"][0]["id"] in issues
    assert issues[scan["items"][0]["id"]]["reason"] == "decoder rejected corrupt video"
    assert scan["items"][1]["id"] not in issues

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
        raise AssertionError("Expected noisy FFmpeg failure")
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
        raise AssertionError("Expected stalled FFmpeg failure")
    except RuntimeError as exc:
        assert "no frame progress" in str(exc)
    assert "-xerror" in commands[0]
    assert commands[0][commands[0].index("-map") + 1] == "0:V:0"


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
    assert 'id="previewSelectionSummary"' in html
    assert 'id="previewPageLimit"' in html
    assert "Select eligible across all pages" in html
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
    assert "selection: previewSelectionPayload()" in script
    assert "Page navigation does not change this selection." in script
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


def test_strict_extraction_aborts_on_the_first_error_but_tolerant_does_not():
    """-xerror is why one damaged packet costs the whole preview."""
    strict = video_preview_maintenance._extraction_command(
        "/library/Movie.mkv",
        "/tmp/%08d.jpg",
        320,
        10,
        video_preview_maintenance.EXTRACTION_TACTICS[0],
    )
    tolerant = video_preview_maintenance._extraction_command(
        "/library/Movie.mkv",
        "/tmp/%08d.jpg",
        320,
        10,
        video_preview_maintenance.EXTRACTION_TACTICS[1],
    )

    assert "-xerror" in strict
    assert "-xerror" not in tolerant
    assert "ignore_err" in tolerant
    assert "+discardcorrupt+genpts" in tolerant


def test_reduced_tactic_halves_the_width_without_going_below_the_floor():
    reduced = video_preview_maintenance.EXTRACTION_TACTICS[2]

    assert video_preview_maintenance._tactic_width(640, reduced) == 320
    # Never below the floor, and always even.
    assert video_preview_maintenance._tactic_width(200, reduced) == 160
    assert video_preview_maintenance._tactic_width(321, reduced) % 2 == 0


def test_extraction_escalates_until_a_tactic_produces_frames(tmp_path, monkeypatch):
    work = tmp_path / "work"
    tried = []

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        tried.append(tactic["key"])
        if tactic["key"] == "strict":
            raise RuntimeError("Error while decoding stream")
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        _write(Path(pattern % 1), _jpeg(b"recovered"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    frames, tactic, attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
    )

    assert tried == ["strict", "tolerant"]
    assert tactic["key"] == "tolerant"
    assert len(frames) == 1
    assert attempts[0]["tactic"] == "strict" and "error" in attempts[0]
    assert attempts[1]["frame_count"] == 1


def test_a_tactic_producing_no_frames_escalates_rather_than_succeeding(tmp_path, monkeypatch):
    """A clean exit with an empty directory is still a failure."""
    work = tmp_path / "work"
    tried = []

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        tried.append(tactic["key"])
        if tactic["key"] != "reduced":
            return  # exits cleanly, writes nothing
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        _write(Path(pattern % 1), _jpeg(b"last-resort"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    frames, tactic, _attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
    )

    assert tried == ["strict", "tolerant", "reduced"]
    assert tactic["key"] == "reduced"
    assert len(frames) == 1


def test_frames_from_a_failed_tactic_are_not_reused_by_the_next(tmp_path, monkeypatch):
    """Partial output from a failed attempt must not pollute the next one."""
    work = tmp_path / "work"

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        if tactic["key"] == "strict":
            _write(Path(pattern % 1), _jpeg(b"partial-a"))
            _write(Path(pattern % 2), _jpeg(b"partial-b"))
            raise RuntimeError("decoder gave up midway")
        _write(Path(pattern % 1), _jpeg(b"clean"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    frames, tactic, _attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
    )

    assert tactic["key"] == "tolerant"
    # Only the successful tactic's single frame survives.
    assert len(frames) == 1
    assert Path(frames[0]).read_bytes() == _jpeg(b"clean")


def test_every_tactic_failing_raises_the_last_error(tmp_path, monkeypatch):
    work = tmp_path / "work"

    def fake_extract(_video, _pattern, _width, _interval, _run, tactic=None):
        raise RuntimeError(f"{tactic['key']} failed")

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    try:
        video_preview_maintenance._extract_frames_with_retries(
            "/library/Movie.mkv",
            str(work),
            320,
            10,
            {},
        )
    except RuntimeError as exc:
        # The error surfaced is the last tactic's, not the first.
        assert "reduced failed" in str(exc)
    else:
        raise AssertionError("extraction should have raised once every tactic failed")


def test_stalls_are_retryable_but_a_refusing_decoder_is_not():
    stalled = video_preview_maintenance.GenerationStalled("no frame progress for 120 seconds")
    assert video_preview_maintenance._failure_is_retryable(stalled) is True
    assert video_preview_maintenance._failure_is_retryable(OSError("disk busy")) is True
    assert (
        video_preview_maintenance._failure_is_retryable(RuntimeError("Video changed after the missing-BIF scan"))
        is True
    )
    # A decoder that refused every tactic describes the file, not the machine.
    assert (
        video_preview_maintenance._failure_is_retryable(RuntimeError("Invalid data found when processing input"))
        is False
    )


def test_a_scan_clears_retryable_failures_but_keeps_permanent_ones(monkeypatch, tmp_path):
    issues_path = tmp_path / "issues.json"
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_ISSUES_PATH", str(issues_path))
    video_preview_maintenance._write_json(
        str(issues_path),
        {
            "schema_version": video_preview_maintenance.GENERATION_ISSUES_SCHEMA_VERSION,
            "records": {
                "stalled": {"item_id": "stalled", "retryable": True, "reason": "stalled"},
                "corrupt": {"item_id": "corrupt", "retryable": False, "reason": "undecodable"},
            },
        },
    )

    cleared = video_preview_maintenance._clear_retryable_generation_issues()

    assert cleared == 1
    records = video_preview_maintenance._generation_issues()["records"]
    assert "stalled" not in records
    assert "corrupt" in records


def test_clearing_issues_lets_a_permanent_failure_be_tried_again(monkeypatch, tmp_path):
    issues_path = tmp_path / "issues.json"
    monkeypatch.setattr(video_preview_maintenance, "GENERATION_ISSUES_PATH", str(issues_path))
    video_preview_maintenance._write_json(
        str(issues_path),
        {
            "schema_version": video_preview_maintenance.GENERATION_ISSUES_SCHEMA_VERSION,
            "records": {
                "corrupt": {"item_id": "corrupt", "retryable": False},
                "other": {"item_id": "other", "retryable": False},
            },
        },
    )

    result = video_preview_maintenance.clear_generation_issues(["corrupt"])

    assert result["cleared_count"] == 1
    records = video_preview_maintenance._generation_issues()["records"]
    assert "corrupt" not in records and "other" in records

    video_preview_maintenance.clear_generation_issues()
    assert video_preview_maintenance._generation_issues()["records"] == {}


def test_a_single_frame_result_escalates_instead_of_being_accepted(tmp_path, monkeypatch):
    """The real-world failure: broken timestamps yield exactly one frame.

    ffmpeg exits cleanly, so nothing looks wrong until the installer rejects
    the BIF for having 1 frame where 199 were expected. Escalation has to be
    driven by the result, not only by errors, or the timestamp-rebuilding
    tactic never gets a chance to run.
    """
    work = tmp_path / "work"
    tried = []

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        tried.append(tactic["key"])
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        if tactic["key"] == "strict":
            # Only the very first frame is selected; the time delta never moves.
            _write(Path(pattern % 1), _jpeg(b"frame-zero"))
            return
        for index in range(1, 200):
            _write(Path(pattern % index), _jpeg(b"frame-%d" % index))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    frames, tactic, attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
        expected_frames=199,
    )

    assert tried == ["strict", "tolerant"]
    assert tactic["key"] == "tolerant"
    assert len(frames) == 199
    assert attempts[0]["frame_count"] == 1
    assert attempts[0]["expected_frame_count"] == 199


def test_a_frame_count_within_tolerance_is_accepted_without_retrying():
    # Matches the installer's own +/-1 allowance.
    assert video_preview_maintenance._frame_count_is_sufficient(199, 199) is True
    assert video_preview_maintenance._frame_count_is_sufficient(198, 199) is True
    assert video_preview_maintenance._frame_count_is_sufficient(200, 199) is True
    assert video_preview_maintenance._frame_count_is_sufficient(1, 199) is False
    assert video_preview_maintenance._frame_count_is_sufficient(0, 199) is False
    # With no expectation available, any frames at all count as usable.
    assert video_preview_maintenance._frame_count_is_sufficient(3, None) is True
    assert video_preview_maintenance._frame_count_is_sufficient(0, None) is False


def test_the_fullest_attempt_survives_when_no_tactic_reaches_the_target(tmp_path, monkeypatch):
    """A later tactic failing outright must not discard an earlier partial."""
    work = tmp_path / "work"

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        if tactic["key"] == "strict":
            _write(Path(pattern % 1), _jpeg(b"one"))
            return
        if tactic["key"] == "tolerant":
            for index in range(1, 51):
                _write(Path(pattern % index), _jpeg(b"partial"))
            return
        raise RuntimeError("reduced pass could not decode anything")

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)

    frames, tactic, _attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
        expected_frames=199,
    )

    # 50 frames beats 1, and beats the tactic that produced none at all.
    assert tactic["key"] == "tolerant"
    assert len(frames) == 50


def test_a_failure_from_older_extraction_logic_no_longer_holds_a_video(monkeypatch, tmp_path):
    """After the tactics change, old verdicts must not keep a video excluded.

    This is the upgrade path: a video that failed under the previous logic gets
    another attempt automatically rather than waiting for a manual retry.
    """
    lib = tmp_path / "library"
    _write(lib / "Movie" / "Movie.mkv")
    scan = _scan(lib, monkeypatch, tmp_path)
    item = scan["items"][0]

    def record(version):
        video_preview_maintenance._write_json(
            video_preview_maintenance.GENERATION_ISSUES_PATH,
            {
                "schema_version": video_preview_maintenance.GENERATION_ISSUES_SCHEMA_VERSION,
                "records": {
                    item["id"]: {
                        "item_id": item["id"],
                        "status": "refused",
                        "reason": "Generated BIF frame count is unexpected (1 / 199)",
                        "video_identity": item.get("video_identity")
                        or video_preview_maintenance._stat_identity(item["path"]),
                        "retryable": False,
                        **({"extraction_logic_version": version} if version is not None else {}),
                    }
                },
            },
        )

    # An unstamped record predates the version stamp entirely.
    record(None)
    page, _err = video_preview_maintenance.items_payload(scan["id"], status="missing")
    assert page["selection"]["held_count"] == 0
    assert page["items"][0].get("generation_held") is not True

    # So does one written by an older version of the extraction logic.
    record(video_preview_maintenance.EXTRACTION_LOGIC_VERSION - 1)
    page, _err = video_preview_maintenance.items_payload(scan["id"], status="missing")
    assert page["selection"]["held_count"] == 0

    # A record from the current logic still holds.
    record(video_preview_maintenance.EXTRACTION_LOGIC_VERSION)
    page, _err = video_preview_maintenance.items_payload(scan["id"], status="missing")
    assert page["selection"]["held_count"] == 1


def test_extraction_skips_embedded_cover_art_when_choosing_a_video_stream():
    """Library MP4s often carry cover art as the first video stream.

    "0:v:0" selects it, which yields exactly one frame from an otherwise clean
    run no matter how forgiving the decoder flags are. Capital V excludes
    attached pictures, so the film is chosen instead.
    """
    for tactic in video_preview_maintenance.EXTRACTION_TACTICS:
        command = video_preview_maintenance._extraction_command(
            "/library/Movie.mp4",
            "/tmp/%08d.jpg",
            320,
            10,
            tactic,
        )
        assert "0:V:0" in command, tactic["key"]
        assert "0:v:0" not in command, tactic["key"]


def test_stream_inventory_reports_attached_pictures(monkeypatch):
    payload = {
        "streams": [
            {
                "index": 0,
                "codec_type": "video",
                "codec_name": "mjpeg",
                "width": 600,
                "height": 900,
                "nb_frames": "1",
                "disposition": {"attached_pic": 1},
            },
            {
                "index": 1,
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "nb_frames": "47000",
                "disposition": {"attached_pic": 0},
            },
        ]
    }

    class _Result:
        returncode = 0
        stdout = json.dumps(payload)

    monkeypatch.setattr(video_preview_maintenance.subprocess, "run", lambda *a, **k: _Result())

    inventory = video_preview_maintenance.probe_stream_inventory("/library/Movie.mp4")

    assert inventory[0]["attached_pic"] == 1
    assert inventory[0]["codec_name"] == "mjpeg"
    assert inventory[1]["attached_pic"] == 0
    assert inventory[1]["width"] == 3840


def test_stream_inventory_is_recorded_when_no_tactic_reaches_the_target(tmp_path, monkeypatch):
    """A shortfall must leave evidence rather than requiring another guess."""
    work = tmp_path / "work"

    def fake_extract(_video, pattern, _width, _interval, _run, tactic=None):
        Path(pattern % 1).parent.mkdir(parents=True, exist_ok=True)
        _write(Path(pattern % 1), _jpeg(b"only-one"))

    monkeypatch.setattr(video_preview_maintenance, "_run_frame_extraction", fake_extract)
    monkeypatch.setattr(
        video_preview_maintenance,
        "probe_stream_inventory",
        lambda path, timeout=10: [{"index": 0, "codec_type": "video", "attached_pic": 1}],
    )

    _frames, _tactic, attempts = video_preview_maintenance._extract_frames_with_retries(
        "/library/Movie.mkv",
        str(work),
        320,
        10,
        {},
        expected_frames=199,
    )

    inventory = next(a["stream_inventory"] for a in attempts if "stream_inventory" in a)
    assert inventory[0]["attached_pic"] == 1


def test_a_short_preview_is_only_useful_above_the_floors():
    # Too few frames to scrub with, whatever the ratio.
    assert video_preview_maintenance._degraded_bif_is_useful(4, 10) is False
    # 30% of a long video is a real stretch of the timeline.
    assert video_preview_maintenance._degraded_bif_is_useful(59, 199) is True
    assert video_preview_maintenance._degraded_bif_is_useful(223, 285) is True
    # A sliver of a long video is not worth writing.
    assert video_preview_maintenance._degraded_bif_is_useful(10, 500) is False


def test_a_damaged_source_keeps_its_partial_preview(monkeypatch, tmp_path):
    """59 of 199 frames from a corrupt file beats discarding the work."""
    target = tmp_path / "Movie-320-10.bif"
    work = _write(tmp_path / "work.bif", b"bif")
    monkeypatch.setattr(video_preview_maintenance, "_matching_bifs_for_video", lambda _p: [])
    monkeypatch.setattr(
        video_preview_maintenance,
        "parse_bif",
        lambda _p: {"valid": True, "image_count": 59, "timestamp_multiplier_ms": 10000},
    )
    monkeypatch.setattr(video_preview_maintenance, "_expected_bif_frame_count", lambda *_a: 199)
    monkeypatch.setattr(video_preview_maintenance, "atomic_install_file", lambda *a, **k: None)
    monkeypatch.setattr(video_preview_maintenance, "regular_file_identity", lambda _p: {})
    recorded = {}
    monkeypatch.setattr(
        video_preview_maintenance,
        "_record_generated_bif",
        lambda *a, **k: recorded.update(k),
    )

    parsed = video_preview_maintenance._install_generated_bif(
        str(work),
        str(target),
        "/library/Movie.mp4",
        320,
        10,
        lib_root=str(tmp_path),
        duration=1990,
        degraded=True,
    )

    assert parsed["degraded"] is True
    assert parsed["expected_image_count"] == 199
    assert recorded["partial"] is True
    assert recorded["frame_count"] == 59


def test_a_healthy_source_still_requires_a_complete_preview(monkeypatch, tmp_path):
    """The strict count guards against generation going wrong on a good file."""
    target = tmp_path / "Movie-320-10.bif"
    work = _write(tmp_path / "work.bif", b"bif")
    monkeypatch.setattr(video_preview_maintenance, "_matching_bifs_for_video", lambda _p: [])
    monkeypatch.setattr(
        video_preview_maintenance,
        "parse_bif",
        lambda _p: {"valid": True, "image_count": 59, "timestamp_multiplier_ms": 10000},
    )
    monkeypatch.setattr(video_preview_maintenance, "_expected_bif_frame_count", lambda *_a: 199)

    try:
        video_preview_maintenance._install_generated_bif(
            str(work),
            str(target),
            "/library/Movie.mp4",
            320,
            10,
            lib_root=str(tmp_path),
            duration=1990,
            degraded=False,
        )
    except ValueError as exc:
        assert "59 / 199" in str(exc)
    else:
        raise AssertionError("a healthy source must not accept a short preview")


def test_a_barely_populated_preview_is_refused_even_when_degraded(monkeypatch, tmp_path):
    target = tmp_path / "Movie-320-10.bif"
    work = _write(tmp_path / "work.bif", b"bif")
    monkeypatch.setattr(video_preview_maintenance, "_matching_bifs_for_video", lambda _p: [])
    monkeypatch.setattr(
        video_preview_maintenance,
        "parse_bif",
        lambda _p: {"valid": True, "image_count": 3, "timestamp_multiplier_ms": 10000},
    )
    monkeypatch.setattr(video_preview_maintenance, "_expected_bif_frame_count", lambda *_a: 199)

    try:
        video_preview_maintenance._install_generated_bif(
            str(work),
            str(target),
            "/library/Movie.mp4",
            320,
            10,
            lib_root=str(tmp_path),
            duration=1990,
            degraded=True,
        )
    except ValueError as exc:
        assert "3 / 199" in str(exc)
    else:
        raise AssertionError("three frames is not a usable preview")


def test_container_paths_translate_to_a_pasteable_local_path():
    settings = {"library_local_path_prefix": r"\\ARTEMIS\media\Emby_Media"}

    result = video_preview_maintenance.local_library_path("/library/XXX/Blacked Raw/Some Title", settings)

    assert result == r"\\ARTEMIS\media\Emby_Media\XXX\Blacked Raw\Some Title"


def test_a_trailing_separator_on_the_prefix_does_not_double_up():
    result = video_preview_maintenance.local_library_path(
        "/library/XXX", {"library_local_path_prefix": "\\\\ARTEMIS\\media\\"}
    )

    assert result == r"\\ARTEMIS\media\XXX"


def test_a_forward_slash_prefix_keeps_forward_slashes():
    result = video_preview_maintenance.local_library_path(
        "/library/XXX/Title", {"library_local_path_prefix": "/mnt/media"}
    )

    assert result == "/mnt/media/XXX/Title"


def test_local_path_is_empty_without_a_configured_prefix():
    assert video_preview_maintenance.local_library_path("/library/XXX", {}) == ""
    # A path outside the library has no meaningful local equivalent.
    assert (
        video_preview_maintenance.local_library_path(
            "/somewhere/else", {"library_local_path_prefix": r"\\ARTEMIS\media"}
        )
        == ""
    )


def test_quarantining_a_damaged_video_takes_its_sidecars_along(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    folder = lib / "Studio" / "Broken Title"
    video = _write(folder / "Broken Title.mp4", b"video")
    srt = _write(folder / "Broken Title.eng.srt", b"subs")
    poster = _write(folder / "Broken Title-poster.jpg", b"art")
    unrelated = _write(folder / "Another Movie.mp4", b"keep me")

    damaged_root = lib / ".vid2gif-quarantine" / "damaged"
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {"damaged_move_root": str(damaged_root)},
    )
    monkeypatch.setattr(video_preview_maintenance, "_write_log", lambda *a, **k: None)

    result, err = video_preview_maintenance.quarantine_damaged_video(str(video), lib_root=str(lib))

    assert err is None
    assert result["moved_count"] == 3
    assert not video.exists() and not srt.exists() and not poster.exists()
    # A different video in the same folder is untouched.
    assert unrelated.exists()
    moved_root = damaged_root / "Studio" / "Broken Title"
    assert (moved_root / "Broken Title.mp4").read_bytes() == b"video"
    assert (moved_root / "Broken Title.eng.srt").read_bytes() == b"subs"


def test_damaged_quarantine_refuses_a_destination_outside_the_library(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    video = _write(lib / "Movie" / "Movie.mp4", b"video")
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {"damaged_move_root": str(tmp_path / "outside")},
    )

    result, err = video_preview_maintenance.quarantine_damaged_video(str(video), lib_root=str(lib))

    assert result is None
    assert "inside the library" in err
    assert video.exists()


def test_a_quarantined_video_is_not_rediscovered_by_the_next_scan(monkeypatch, tmp_path):
    """Quarantine folders are configurable, so name-based skipping is not enough.

    Chris nests them under a custom parent, and the damaged folder in
    particular was never in any hardcoded skip list.
    """
    lib = tmp_path / "library"
    _write(lib / "Keep Me" / "Keep Me.mkv")
    damaged_root = lib / "vid2gif-quarantine" / ".vid2gif-damaged"
    _write(damaged_root / "Studio" / "Broken.mkv")
    repair_root = lib / "vid2gif-quarantine" / ".vid2gif-video-preview-repairs"
    _write(repair_root / "Old-320-10.bif")

    # The shared reset installs its own settings, so patch after it rather than
    # before, or the quarantine destinations are thrown away.
    _reset_preview_state(monkeypatch, tmp_path)
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {
            "damaged_move_root": str(damaged_root),
            "video_preview_repair_root": str(repair_root),
            "duplicate_move_root": str(lib / "vid2gif-quarantine" / ".vid2gif-duplicates"),
            "subtitle_quarantine_root": str(lib / "vid2gif-quarantine" / "subs"),
            "video_preview_bif_width": 320,
            "video_preview_bif_interval_seconds": 10,
        },
    )

    scan, err = video_preview_maintenance.start_scan(str(lib), lib_root=str(lib), synchronous=True)
    assert err is None
    names = {item["name"] for item in scan["items"]}

    assert "Keep Me.mkv" in names
    # The quarantined video must not come back as a missing preview.
    assert "Broken.mkv" not in names


def test_the_repair_destination_follows_the_configured_setting(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    configured = lib / "vid2gif-quarantine" / ".vid2gif-video-preview-repairs"
    monkeypatch.setattr(
        video_preview_maintenance.app_settings,
        "load_settings",
        lambda: {"video_preview_repair_root": str(configured)},
    )

    resolved = video_preview_maintenance._default_repair_root(str(lib))

    assert os.path.realpath(resolved) == os.path.realpath(str(configured))
