import os
import threading

from app import test_lab


def _reset_lab(monkeypatch, tmp_path):
    lab_root = tmp_path / "state" / "test-lab"
    logs = tmp_path / "state" / "logs"
    proc = tmp_path / "state" / "processing" / "tmp"
    lab_root.mkdir(parents=True)
    logs.mkdir(parents=True)
    proc.mkdir(parents=True)
    monkeypatch.setattr(test_lab, "TEST_LAB_ROOT", str(lab_root))
    monkeypatch.setattr(test_lab, "LOG_DIR", str(logs))
    monkeypatch.setattr(test_lab, "PROCESS_TMP_ROOT", str(proc))
    monkeypatch.setattr(test_lab, "_worker_started", False)
    monkeypatch.setattr(test_lab, "start_test_lab_worker", lambda: None)
    test_lab.test_lab_runs.clear()
    test_lab.preview_jobs.clear()
    with test_lab.test_lab_queue.mutex:
        test_lab.test_lab_queue.queue.clear()
    return lab_root


def _variant(name, height=360):
    return {
        "name": name,
        "cfg": {
            "height": height,
            "fps": 12,
            "clip_len": 1,
            "percent_points": [50],
            "abs_early": 0,
            "abs_late_from_end": 0,
            "start_buffer": 0,
            "end_buffer": 0,
            "loop_forever": True,
            "smooth": False,
        },
    }


def test_test_lab_worker_outputs_under_state_and_continues_after_failure(monkeypatch, tmp_path):
    lab_root = _reset_lab(monkeypatch, tmp_path)
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(test_lab, "probe_video_details", lambda path: ("{}", None))
    monkeypatch.setattr(test_lab, "summarize_video_details", lambda details: "h264 1280x720")
    monkeypatch.setattr(test_lab, "get_duration", lambda path: (10.0, None))
    monkeypatch.setattr(test_lab, "build_segments", lambda dur, cfg: [{"start": 1, "end": 2}])
    monkeypatch.setattr(test_lab, "find_background_image", lambda path: None)
    recorded = []
    events = []

    def fake_make_gif(video_path, segs, out_gif, cfg, job, background_image=None):
        events.append(("make", job["id"]))
        assert str(lab_root) not in video_path
        assert "processing" in out_gif
        if job["id"] == "variant-2":
            return False, "ffmpeg failed"
        with open(out_gif, "wb") as f:
            f.write(b"gif")
        return True, ""

    def fake_optimize_gif(gif_path, job, logger=None):
        events.append(("optimize", job["id"]))
        assert os.path.isfile(gif_path)
        job.update(
            {
                "gif_size_before_opt_bytes": 3,
                "gif_size_after_opt_bytes": 3,
                "gif_optimization_saved_bytes": 0,
                "gif_optimization_savings_percent": 0.0,
                "gif_optimization_status": "kept_original",
                "gif_optimization_seconds": 0,
                "gif_optimization_label": "No smaller result",
            }
        )

    monkeypatch.setattr(test_lab, "make_gif_multi_inputs", fake_make_gif)
    monkeypatch.setattr(test_lab, "optimize_gif", fake_optimize_gif)
    monkeypatch.setattr(test_lab, "record_successful_job", lambda job: recorded.append(job["id"]) or True)

    run_id, err = test_lab.enqueue_test_run(
        str(video),
        [_variant("A", 360), _variant("B", 480), _variant("C", 720)],
        lib_root=str(lib),
    )
    assert err is None

    thread = threading.Thread(target=test_lab.worker)
    thread.start()
    test_lab.test_lab_queue.put(None)
    thread.join(timeout=5)

    run = test_lab.test_lab_runs[run_id]
    assert run["status"] == "partial"
    assert [v["status"] for v in run["variants"]] == ["success", "failed", "success"]
    assert events == [
        ("make", "variant-1"),
        ("optimize", "variant-1"),
        ("make", "variant-2"),
        ("make", "variant-3"),
        ("optimize", "variant-3"),
    ]
    assert recorded == ["variant-1", "variant-3"]
    assert (lab_root / run_id / "variant-1.gif").is_file()
    assert not (lab_root / run_id / "variant-2.gif").exists()
    assert (lab_root / run_id / "variant-3.gif").is_file()
    assert not (lib / "poster.gif").exists()
    assert not any((tmp_path / "state" / "processing" / "tmp").rglob("poster.gif"))


def test_test_lab_worker_reuses_existing_fingerprint_without_ffmpeg(monkeypatch, tmp_path):
    lab_root = _reset_lab(monkeypatch, tmp_path)
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_bytes(b"video")
    old_run = lab_root / "oldrun"
    old_run.mkdir()
    (old_run / "variant-1.gif").write_bytes(b"GIF89a")
    manifest = {
        "schema_version": 1,
        "run_id": "oldrun",
        "source_name": "movie.mp4",
        "variants": [
            {
                "id": "variant-1",
                "name": "Existing",
                "filename": "variant-1.gif",
                "request_fingerprint": "same-fingerprint",
                "settings_label": "360px high",
                "gif_optimization_label": "Saved 1 MB",
            }
        ],
    }
    (old_run / "manifest.json").write_text(__import__("json").dumps(manifest))
    monkeypatch.setattr(test_lab, "request_fingerprint", lambda *args, **kwargs: "same-fingerprint")
    monkeypatch.setattr(
        test_lab,
        "make_gif_multi_inputs",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("ffmpeg should not run for a reusable test GIF")
        ),
    )
    monkeypatch.setattr(
        test_lab,
        "optimize_gif",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("optimizer should not run for a reusable test GIF")
        ),
    )

    run_id, err = test_lab.enqueue_test_run(
        str(video),
        [_variant("A"), _variant("B")],
        lib_root=str(lib),
    )
    assert err is None

    thread = threading.Thread(target=test_lab.worker)
    thread.start()
    test_lab.test_lab_queue.put(None)
    thread.join(timeout=5)

    run = test_lab.test_lab_runs[run_id]
    assert run["status"] == "success"
    assert [v["reused"] for v in run["variants"]] == [True, True]
    assert [v["reused_file_id"] for v in run["variants"]] == [
        "oldrun/variant-1.gif",
        "oldrun/variant-1.gif",
    ]
    public = test_lab.status_payload()["active_run"]
    assert public["variants"][0]["file_id"] == "oldrun/variant-1.gif"


def test_test_lab_worker_regenerates_when_reuse_file_is_missing(monkeypatch, tmp_path):
    lab_root = _reset_lab(monkeypatch, tmp_path)
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_bytes(b"video")
    old_run = lab_root / "oldrun"
    old_run.mkdir()
    manifest = {
        "schema_version": 1,
        "run_id": "oldrun",
        "variants": [
            {
                "id": "variant-1",
                "filename": "variant-1.gif",
                "request_fingerprint": "same-fingerprint",
            }
        ],
    }
    (old_run / "manifest.json").write_text(__import__("json").dumps(manifest))
    monkeypatch.setattr(test_lab, "request_fingerprint", lambda *args, **kwargs: "same-fingerprint")
    monkeypatch.setattr(test_lab, "probe_video_details", lambda path: ("{}", None))
    monkeypatch.setattr(test_lab, "summarize_video_details", lambda details: "h264 1280x720")
    monkeypatch.setattr(test_lab, "get_duration", lambda path: (10.0, None))
    monkeypatch.setattr(test_lab, "build_segments", lambda dur, cfg: [{"start": 1, "end": 2}])
    monkeypatch.setattr(test_lab, "find_background_image", lambda path: None)
    events = []

    def fake_make_gif(video_path, segs, out_gif, cfg, job, background_image=None):
        events.append(job["id"])
        with open(out_gif, "wb") as f:
            f.write(b"gif")
        return True, ""

    monkeypatch.setattr(test_lab, "make_gif_multi_inputs", fake_make_gif)
    monkeypatch.setattr(test_lab, "optimize_gif", lambda *args, **kwargs: None)
    monkeypatch.setattr(test_lab, "record_successful_job", lambda job: True)

    run_id, err = test_lab.enqueue_test_run(
        str(video),
        [_variant("A"), _variant("B", 480)],
        lib_root=str(lib),
    )
    assert err is None

    thread = threading.Thread(target=test_lab.worker)
    thread.start()
    test_lab.test_lab_queue.put(None)
    thread.join(timeout=5)

    assert events == ["variant-1"]
    assert (lab_root / run_id / "variant-1.gif").is_file()
    assert test_lab.test_lab_runs[run_id]["variants"][1]["reused"] is True


def test_test_lab_worker_generates_same_run_duplicate_once(monkeypatch, tmp_path):
    lab_root = _reset_lab(monkeypatch, tmp_path)
    lib = tmp_path / "library"
    lib.mkdir()
    video = lib / "movie.mp4"
    video.write_bytes(b"video")
    monkeypatch.setattr(test_lab, "probe_video_details", lambda path: ("{}", None))
    monkeypatch.setattr(test_lab, "summarize_video_details", lambda details: "h264 1280x720")
    monkeypatch.setattr(test_lab, "get_duration", lambda path: (10.0, None))
    monkeypatch.setattr(test_lab, "build_segments", lambda dur, cfg: [{"start": 1, "end": 2}])
    monkeypatch.setattr(test_lab, "find_background_image", lambda path: None)
    events = []

    def fake_make_gif(video_path, segs, out_gif, cfg, job, background_image=None):
        events.append(job["id"])
        with open(out_gif, "wb") as f:
            f.write(b"gif")
        return True, ""

    monkeypatch.setattr(test_lab, "make_gif_multi_inputs", fake_make_gif)
    monkeypatch.setattr(test_lab, "optimize_gif", lambda *args, **kwargs: None)
    monkeypatch.setattr(test_lab, "record_successful_job", lambda job: True)

    run_id, err = test_lab.enqueue_test_run(
        str(video),
        [_variant("A"), _variant("B")],
        lib_root=str(lib),
    )
    assert err is None

    thread = threading.Thread(target=test_lab.worker)
    thread.start()
    test_lab.test_lab_queue.put(None)
    thread.join(timeout=5)

    run = test_lab.test_lab_runs[run_id]
    assert events == ["variant-1"]
    assert run["variants"][1]["reused"] is True
    assert run["variants"][1]["reused_file_id"] == f"{run_id}/variant-1.gif"
    assert not (lab_root / run_id / "variant-2.gif").exists()


def test_test_lab_status_reads_persisted_inventory_after_memory_reset(monkeypatch, tmp_path):
    lab_root = _reset_lab(monkeypatch, tmp_path)
    run_dir = lab_root / "run1"
    run_dir.mkdir()
    (run_dir / "variant-1.gif").write_bytes(b"GIF89a")
    manifest = {
        "schema_version": 1,
        "run_id": "run1",
        "source_name": "movie.mp4",
        "variants": [
            {
                "id": "variant-1",
                "name": "Small",
                "filename": "variant-1.gif",
                "settings_label": "360px high",
                "gif_optimization_label": "Saved 1 MB",
            }
        ],
    }
    (run_dir / "manifest.json").write_text(__import__("json").dumps(manifest))
    test_lab.test_lab_runs.clear()

    payload = test_lab.status_payload()

    assert payload["runs"] == []
    assert payload["files"][0]["id"] == "run1/variant-1.gif"
    assert payload["files"][0]["name"] == "Small"
    assert payload["files"][0]["source_name"] == "movie.mp4"
    assert payload["total_size_bytes"] == 6
