import os
import sys
import logging

ROOT = os.path.dirname(os.path.dirname(__file__))
sys.path.append(ROOT)
sys.path.append(os.path.join(ROOT, 'app'))

from app import jobs


def test_enqueue_job_tmp_dir_contains_job_id(tmp_path, monkeypatch):
    proc_tmp = tmp_path / 'proc_tmp'
    proc_tmp.mkdir()
    logs_dir = tmp_path / 'logs'
    logs_dir.mkdir()
    lib_dir = tmp_path / 'library'
    lib_dir.mkdir()

    monkeypatch.setattr(jobs, 'PROCESS_TMP_ROOT', str(proc_tmp))
    monkeypatch.setattr(jobs, 'LOG_DIR', str(logs_dir))
    monkeypatch.setattr(jobs, 'LIB_ROOT', str(lib_dir))

    # create dummy video file under library
    video = lib_dir / 'sample.mp4'
    video.write_bytes(b'0')

    cfg = {}
    job_id, err = jobs.enqueue_job(str(video), cfg)
    assert err is None
    job = jobs.jobs[job_id]
    base = os.path.splitext(video.name)[0]
    assert job['tmp_dir'] == os.path.join(str(proc_tmp), f"{base}_{job_id}")

    # cleanup
    for h in job['logger'].handlers:
        h.close()
    jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()
