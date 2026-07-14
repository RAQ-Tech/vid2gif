import os
import sys
import threading
import logging

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)
from app import jobs


def _clear_jobs_and_queue():
    jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()
        jobs.job_queue.unfinished_tasks = 0


def test_worker_creates_and_cleans_tmp_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(jobs, 'LIB_ROOT', str(tmp_path))
    logger = logging.getLogger('test_worker')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    job_id = 'job123'
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'\x00')
    out_gif = tmp_path / 'poster.gif'
    tmp_dir = tmp_path / job_id
    job = {
        'id': job_id,
        'video': str(video),
        'out_gif': str(out_gif),
        'tmp_dir': str(tmp_dir),
        'status': 'queued',
        'cfg': {'height': 100, 'fps': 10, 'clip_len': 1, 'loop_forever': True},
        'log_path': str(tmp_path / 'log.txt'),
        'progress_text': '',
        'logger': logger,
    }
    jobs.jobs[job_id] = job
    monkeypatch.setattr(jobs, 'probe_video_details', lambda v: ('', None))
    monkeypatch.setattr(jobs, 'get_duration', lambda v: (10.0, None))
    monkeypatch.setattr(jobs, 'build_segments', lambda d, c: [{'start': 0, 'end': 1}])
    events = []
    def fake_make_gif(video, segs, out_gif, cfg, job, background_image=None):
        events.append('make')
        with open(out_gif, 'wb') as f:
            f.write(b'GIF89a-unoptimized')
        return True, ''
    def fake_optimize_gif(gif_path, job, logger=None):
        events.append('optimize')
        assert os.path.isfile(gif_path)
        assert not os.path.exists(job['out_gif'])
        with open(gif_path, 'wb') as f:
            f.write(b'GIF89a-optimized')
        job.update(
            {
                'gif_size_before_opt_bytes': 11,
                'gif_size_after_opt_bytes': 9,
                'gif_optimization_saved_bytes': 2,
                'gif_optimization_savings_percent': 18.2,
                'gif_optimization_status': 'optimized',
                'gif_optimization_seconds': 1,
                'gif_optimization_label': 'Saved 2 B (18.2%)',
            }
        )
    monkeypatch.setattr(jobs, 'make_gif_multi_inputs', fake_make_gif)
    monkeypatch.setattr(jobs, 'optimize_gif', fake_optimize_gif)
    recorded = []
    monkeypatch.setattr(jobs, 'record_successful_job', lambda job: recorded.append(job['id']) or True)
    t = threading.Thread(target=jobs.worker)
    t.start()
    jobs.job_queue.put(job_id)
    jobs.job_queue.put(None)
    t.join()
    assert job['status'] == 'success'
    assert os.path.isfile(job['out_gif'])
    assert out_gif.read_bytes() == b'GIF89a-optimized'
    assert events == ['make', 'optimize']
    assert recorded == [job_id]
    assert job['gif_optimization_status'] == 'optimized'
    assert not os.path.exists(job['tmp_dir'])
    _clear_jobs_and_queue()


def test_cancel_job_removes_a_queued_job_without_leaving_queue_work(monkeypatch, tmp_path):
    _clear_jobs_and_queue()
    monkeypatch.setattr(jobs, "_persist_job_state", lambda: True)
    logger = logging.getLogger("test_cancel_queued_job")
    logger.handlers.clear()
    logger.addHandler(logging.NullHandler())
    job = {
        "id": "cancel-me",
        "status": "queued",
        "logger": logger,
        "progress_percent": 0,
    }
    jobs.jobs[job["id"]] = job
    jobs.job_queue.put(job["id"])

    payload, error = jobs.cancel_job(job["id"])

    assert error is None
    assert payload["status"] == "stopped"
    assert list(jobs.job_queue.queue) == []
    assert jobs.job_queue.unfinished_tasks == 0
    assert job["logger"] is None
    _clear_jobs_and_queue()


def test_worker_failure_leaves_existing_output_and_cleans_tmp_dir(tmp_path, monkeypatch):
    _clear_jobs_and_queue()
    monkeypatch.setattr(jobs, 'LIB_ROOT', str(tmp_path))
    logger = logging.getLogger('test_worker_failure')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    job_id = 'job-fail'
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'\x00')
    out_gif = tmp_path / 'poster.gif'
    out_gif.write_bytes(b'old poster')
    tmp_dir = tmp_path / job_id
    job = {
        'id': job_id,
        'video': str(video),
        'out_gif': str(out_gif),
        'tmp_dir': str(tmp_dir),
        'status': 'queued',
        'cfg': {'height': 100, 'fps': 10, 'clip_len': 1, 'loop_forever': True},
        'log_path': str(tmp_path / 'log.txt'),
        'progress_text': '',
        'logger': logger,
    }
    jobs.jobs[job_id] = job
    monkeypatch.setattr(jobs, 'probe_video_details', lambda v: ('', None))
    monkeypatch.setattr(jobs, 'get_duration', lambda v: (10.0, None))
    monkeypatch.setattr(jobs, 'build_segments', lambda d, c: [{'start': 0, 'end': 1}])

    def fake_make_gif(video, segs, out_gif, cfg, job, background_image=None):
        with open(out_gif, 'wb') as f:
            f.write(b'partial gif')
        return False, 'ffmpeg failed'

    monkeypatch.setattr(jobs, 'make_gif_multi_inputs', fake_make_gif)
    monkeypatch.setattr(
        jobs,
        'optimize_gif',
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError('optimizer should not run after ffmpeg failure')
        ),
    )
    monkeypatch.setattr(
        jobs,
        'record_successful_job',
        lambda job: (_ for _ in ()).throw(
            AssertionError('estimate history should not update after failure')
        ),
    )
    t = threading.Thread(target=jobs.worker)
    t.start()
    jobs.job_queue.put(job_id)
    jobs.job_queue.put(None)
    t.join()

    assert job['status'] == 'failed'
    assert out_gif.read_bytes() == b'old poster'
    assert not os.path.exists(job['tmp_dir'])
    _clear_jobs_and_queue()


def test_worker_refuses_to_install_when_source_changes_during_generation(tmp_path, monkeypatch):
    _clear_jobs_and_queue()
    monkeypatch.setattr(jobs, 'LIB_ROOT', str(tmp_path))
    logger = logging.getLogger('test_worker_source_race')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    job_id = 'job-source-race'
    video = tmp_path / 'video.mp4'
    video.write_bytes(b'original-video')
    out_gif = tmp_path / 'poster.gif'
    out_gif.write_bytes(b'old-poster')
    job = {
        'id': job_id,
        'video': str(video),
        'out_gif': str(out_gif),
        'tmp_dir': str(tmp_path / job_id),
        'status': 'queued',
        'cfg': {'height': 100, 'fps': 10, 'clip_len': 1, 'loop_forever': True},
        'log_path': str(tmp_path / 'race-log.txt'),
        'progress_text': '',
        'logger': logger,
    }
    jobs.jobs[job_id] = job
    monkeypatch.setattr(jobs, 'probe_video_details', lambda v: ('', None))
    monkeypatch.setattr(jobs, 'get_duration', lambda v: (10.0, None))
    monkeypatch.setattr(jobs, 'build_segments', lambda d, c: [{'start': 0, 'end': 1}])

    def fake_make_gif(video_path, segs, tmp_gif, cfg, active_job, background_image=None):
        with open(tmp_gif, 'wb') as handle:
            handle.write(b'GIF89a-complete')
        video.write_bytes(b'replaced-during-generation')
        return True, ''

    monkeypatch.setattr(jobs, 'make_gif_multi_inputs', fake_make_gif)
    monkeypatch.setattr(jobs, 'optimize_gif', lambda *args, **kwargs: None)

    thread = threading.Thread(target=jobs.worker)
    thread.start()
    jobs.job_queue.put(job_id)
    jobs.job_queue.put(None)
    thread.join()

    assert job['status'] == 'failed'
    assert out_gif.read_bytes() == b'old-poster'
    assert not os.path.exists(job['tmp_dir'])
    _clear_jobs_and_queue()
