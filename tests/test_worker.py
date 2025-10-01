import os
import sys
import threading
import logging

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)
sys.path.append(os.path.join(ROOT, 'app'))
from app import jobs

def test_worker_creates_and_cleans_tmp_dir(tmp_path, monkeypatch):
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
    monkeypatch.setattr(jobs, '_select_background_image', lambda p, logger: 'bg.png')
    def fake_make_gif(video, segs, out_gif, cfg, job, still_image):
        assert still_image == 'bg.png'
        with open(out_gif, 'wb') as f:
            f.write(b'GIF89a')
        return True, ''
    monkeypatch.setattr(jobs, 'make_gif_multi_inputs', fake_make_gif)
    t = threading.Thread(target=jobs.worker)
    t.start()
    jobs.job_queue.put(job_id)
    jobs.job_queue.put(None)
    t.join()
    assert job['status'] == 'success'
    assert os.path.isfile(job['out_gif'])
    assert not os.path.exists(job['tmp_dir'])
    jobs.jobs.clear()


def test_worker_skips_without_background(tmp_path, monkeypatch):
    logger = logging.getLogger('test_worker_skip')
    logger.addHandler(logging.NullHandler())
    logger.propagate = False
    job_id = 'job124'
    video = tmp_path / 'video2.mp4'
    video.write_bytes(b'\x00')
    out_gif = tmp_path / 'poster.gif'
    tmp_dir = tmp_path / job_id
    skip_log = tmp_path / 'gif-skipped.txt'

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
    monkeypatch.setattr(jobs, '_select_background_image', lambda p, logger: None)
    monkeypatch.setattr(jobs, 'SKIP_LOG_PATH', str(skip_log))
    t = threading.Thread(target=jobs.worker)
    t.start()
    jobs.job_queue.put(job_id)
    jobs.job_queue.put(None)
    t.join()
    assert job['status'] == 'skipped'
    assert not os.path.exists(job['out_gif'])
    assert skip_log.exists()
    contents = skip_log.read_text()
    assert str(video) in contents
    jobs.jobs.clear()
