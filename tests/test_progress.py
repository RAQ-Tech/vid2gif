from app.progress import (
    initialize_job_progress,
    mark_job_finished,
    mark_job_started,
    update_render_progress,
)


def test_render_progress_calculates_percent_elapsed_and_eta():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 10, out_time_seconds=4, now=104)

    assert job["progress_percent"] == 37
    assert job["elapsed_seconds"] == 4
    assert job["eta_seconds"] == 7
    assert job["progress_label"] == "Rendering · 37% · about 7s remaining"
    assert job["progress_text"] == job["progress_label"]


def test_render_progress_uses_frame_count_when_time_is_missing():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 5, frame=30, fps=10, now=101)

    assert job["progress_percent"] == 55
    assert job["eta_seconds"] == 1


def test_render_progress_clamps_and_never_moves_backward():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 10, out_time_seconds=8, now=108)
    update_render_progress(job, 10, out_time_seconds=2, now=109)
    update_render_progress(job, 10, out_time_seconds=99, now=110)

    assert job["progress_percent"] == 92
    assert job["eta_seconds"] >= 0


def test_render_progress_blends_history_without_resetting_eta_each_update():
    job = {
        "status": "queued",
        "expected_duration_seconds": 20,
        "eta_confidence": "history",
    }
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 10, out_time_seconds=2, now=104)
    first_eta = job["eta_seconds"]
    update_render_progress(job, 10, out_time_seconds=4, now=108)

    assert first_eta is not None
    assert job["eta_seconds"] is not None
    assert job["eta_seconds"] < first_eta
    assert job["eta_confidence"] == "history"


def test_mark_job_finished_records_size_and_final_label(tmp_path):
    output = tmp_path / "poster.gif"
    output.write_bytes(b"GIF89a")
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    mark_job_finished(job, "success", str(output), now=112)

    assert job["status"] == "success"
    assert job["progress_percent"] == 100
    assert job["elapsed_seconds"] == 12
    assert job["eta_seconds"] == 0
    assert job["output_size_bytes"] == 6
    assert job["finished_at"]
    assert job["progress_label"].startswith("Complete")
