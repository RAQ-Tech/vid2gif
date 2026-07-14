import json
import logging
import os

from app import jobs


def _reset(monkeypatch, tmp_path):
    log_dir = tmp_path / "state" / "logs"
    process_root = tmp_path / "state" / "processing" / "tmp"
    log_dir.mkdir(parents=True)
    process_root.mkdir(parents=True)
    monkeypatch.setattr(jobs, "LOG_DIR", str(log_dir))
    monkeypatch.setattr(jobs, "PROCESS_TMP_ROOT", str(process_root))
    monkeypatch.setattr(jobs, "_restore_complete", False)
    with jobs.lock:
        jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()
        jobs.job_queue.unfinished_tasks = 0
    jobs.queue_paused.clear()
    return log_dir, process_root


def _close_loggers():
    for job in jobs.jobs.values():
        logger = job.get("logger")
        if logger:
            for handler in logger.handlers:
                handler.close()


def test_restore_requeues_queued_jobs_and_marks_running_job_interrupted(monkeypatch, tmp_path):
    log_dir, process_root = _reset(monkeypatch, tmp_path)
    queued = {
        "id": "queued",
        "video": str(tmp_path / "library" / "queued.mp4"),
        "out_gif": str(tmp_path / "library" / "poster.gif"),
        "tmp_dir": str(process_root / "queued"),
        "status": "queued",
        "cfg": {"height": 480},
        "log_path": str(log_dir / "queued.txt"),
        "progress_percent": 0,
    }
    running = {
        **queued,
        "id": "running",
        "tmp_dir": str(process_root / "running"),
        "log_path": str(log_dir / "running.txt"),
        "status": "running",
    }
    os.makedirs(running["tmp_dir"])
    path = jobs._job_state_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            {
                "schema_version": jobs.JOB_STATE_SCHEMA_VERSION,
                "paused": False,
                "queue": ["queued"],
                "jobs": [queued, running],
            },
            handle,
        )

    jobs._restore_jobs_once()

    assert list(jobs.job_queue.queue) == ["queued"]
    assert jobs.job_queue.unfinished_tasks == 1
    assert jobs.jobs["queued"]["restored"] is True
    assert jobs.jobs["queued"]["log_path"] == str(log_dir / "queued.txt")
    assert os.path.commonpath(
        [jobs.jobs["queued"]["tmp_dir"], str(process_root)]
    ) == str(process_root)
    assert jobs.jobs["running"]["status"] == "interrupted"
    assert "restart" in jobs.jobs["running"]["progress_label"].lower()
    assert not os.path.exists(running["tmp_dir"])
    _close_loggers()


def test_job_state_is_written_atomically_without_logger_objects(monkeypatch, tmp_path):
    log_dir, process_root = _reset(monkeypatch, tmp_path)
    logger = logging.getLogger("persist-test")
    job = {
        "id": "one",
        "video": str(tmp_path / "video.mp4"),
        "out_gif": str(tmp_path / "poster.gif"),
        "tmp_dir": str(process_root / "one"),
        "status": "queued",
        "cfg": {"height": 480},
        "log_path": str(log_dir / "one.txt"),
        "logger": logger,
    }
    with jobs.lock:
        jobs.jobs[job["id"]] = job
    jobs.job_queue.put(job["id"])

    assert jobs._persist_job_state() is True
    with open(jobs._job_state_path(), "r", encoding="utf-8") as handle:
        saved = json.load(handle)

    assert saved["queue"] == ["one"]
    assert saved["jobs"][0]["id"] == "one"
    assert "logger" not in saved["jobs"][0]
