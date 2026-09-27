from app.progress import (
    initialize_job_progress,
    mark_job_finished,
    mark_job_started,
    update_render_progress,
    update_job_label,
    update_job_stage,
)


def test_render_progress_calculates_percent_elapsed_and_eta():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 10, out_time_seconds=4, now=104)

    assert job["progress_percent"] == 37
    assert job["elapsed_seconds"] == 4
    assert job["eta_seconds"] is None  # One output cannot measure throughput or the unknown optimizer.
    assert job["progress_label"] == "Rendering · 37%"
    assert job["progress_text"] == job["progress_label"]


def test_render_progress_uses_frame_count_when_time_is_missing():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 5, frame=30, fps=10, now=101)

    assert job["progress_percent"] == 55
    assert job["eta_seconds"] is None


def test_render_progress_clamps_and_never_moves_backward():
    job = {"status": "queued"}
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)

    update_render_progress(job, 10, out_time_seconds=8, now=108)
    update_render_progress(job, 10, out_time_seconds=2, now=109)
    update_render_progress(job, 10, out_time_seconds=99, now=110)

    assert job["progress_percent"] == 92
    assert job["eta_seconds"] is None
    assert job["_render_observations"]["points"][0][1] == 8


def test_render_progress_measures_output_separately_from_startup_and_tail():
    job = {
        "status": "queued",
        "expected_duration_seconds": 20,
        "eta_confidence": "history",
    }
    initialize_job_progress(job, now=100)
    mark_job_started(job, now=100)
    job["_stage_estimates"] = {"Rendering": 2000, "Optimizing": 10, "Installing": 2}

    update_job_stage(job, 0, "Rendering", now=100)
    update_render_progress(job, 10, out_time_seconds=2, now=150)
    update_render_progress(job, 10, out_time_seconds=4, now=154)

    assert job["eta_seconds"] == 24  # 6 media seconds / 0.5 speed + 12s finishing.
    update_job_stage(job, 92, "Optimizing", now=166)
    assert job["eta_seconds"] == 12
    update_job_label(job, now=177)
    assert job["eta_seconds"] is None  # An overdue optimizer must not report zero.
    assert job["eta_confidence"] == "recalculating"


def test_first_run_does_not_invent_finishing_time():
    job = {"status": "queued", "expected_duration_seconds": 5}
    initialize_job_progress(job, now=0)
    mark_job_started(job, now=0)
    update_job_label(job, now=10)
    assert job["elapsed_seconds"] == 10
    assert job["eta_seconds"] is None
    update_job_stage(job, 92, "Optimizing", now=10)
    update_job_label(job, now=100)
    assert job["eta_seconds"] is None


def test_success_records_stage_durations_and_failure_does_not_train_history():
    job = {"status": "queued"}
    initialize_job_progress(job, now=0)
    mark_job_started(job, now=0)
    update_job_stage(job, 0, "Rendering", now=3)
    update_job_stage(job, 92, "Optimizing", now=13)
    update_job_stage(job, 98, "Installing", now=18)
    mark_job_finished(job, "success", now=20)
    assert job["_stage_durations"] == {"Preparing": 3, "Rendering": 10, "Optimizing": 5, "Installing": 2}


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
