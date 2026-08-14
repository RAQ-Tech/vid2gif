import logging
from pathlib import Path

from app import jobs, routes


ROOT = Path(__file__).resolve().parents[1]


def _test_lab_frontend_source():
    source_root = ROOT / "frontend" / "test-lab"
    return "\n".join(
        (source_root / name).read_text()
        for name in ("index.js", "logic.js", "player.js")
    )


def _make_job(job_id="job1", status="queued", log_path="/tmp/job.txt"):
    return {
        "id": job_id,
        "video": "/library/video<script>.mp4",
        "out_gif": "/library/poster.gif",
        "tmp_dir": "/tmp/job",
        "status": status,
        "cfg": {},
        "log_path": log_path,
        "progress_text": "<progress>",
        "logger": logging.getLogger(job_id),
    }


def _clear_jobs():
    jobs.jobs.clear()
    with jobs.job_queue.mutex:
        jobs.job_queue.queue.clear()


def test_api_status_returns_public_job_payload_only():
    _clear_jobs()
    jobs.jobs["job1"] = _make_job()

    client = routes.app.test_client()
    res = client.get("/api/status")

    assert res.status_code == 200
    payload = res.get_json()
    assert payload[0]["id"] == "job1"
    assert payload[0]["video"] == "/library/video<script>.mp4"
    assert payload[0]["out_gif"] == "/library/poster.gif"
    assert payload[0]["status"] == "queued"
    assert payload[0]["progress_text"] == "Waiting"
    assert payload[0]["progress_label"] == "Waiting"
    assert payload[0]["progress_percent"] == 0
    assert payload[0]["elapsed_seconds"] is None
    assert payload[0]["eta_seconds"] is None
    assert payload[0]["output_size_bytes"] is None
    assert payload[0]["started_at"] is None
    assert payload[0]["finished_at"] is None
    assert payload[0]["gif_size_before_opt_bytes"] is None
    assert payload[0]["gif_size_after_opt_bytes"] is None
    assert payload[0]["gif_optimization_saved_bytes"] is None
    assert payload[0]["gif_optimization_savings_percent"] is None
    assert payload[0]["gif_optimization_status"] is None
    assert payload[0]["gif_optimization_seconds"] is None
    assert payload[0]["gif_optimization_label"] == ""
    assert "logger" not in payload[0]
    assert "log_path" not in payload[0]
    assert "cfg" not in payload[0]
    _clear_jobs()


def test_queue_status_returns_public_payloads():
    _clear_jobs()
    jobs.jobs["job1"] = _make_job(status="queued")
    jobs.job_queue.put("job1")

    client = routes.app.test_client()
    res = client.get("/api/queue/status")

    payload = res.get_json()
    assert payload["queued"][0]["id"] == "job1"
    assert payload["total_active_items"] == 1
    assert payload["completed_active_items"] == 0
    assert payload["queue_progress_percent"] == 0
    assert payload["queue_progress_label"] == "0% complete · learning timing"
    assert payload["queue_eta_confidence"] == "calibrating"
    assert payload["summary"]["total_active_items"] == 1
    assert "logger" not in payload["queued"][0]
    assert jobs.emit_queue_status() == payload
    _clear_jobs()


def test_queue_status_reports_overall_batch_progress():
    _clear_jobs()
    jobs.jobs["done"] = _make_job(job_id="done", status="success")
    jobs.jobs["done"].update(
        {
            "batch_id": "batch1",
            "progress_percent": 100,
            "elapsed_seconds": 10,
            "_started_ts": 100,
            "_finished_ts": 110,
        }
    )
    jobs.jobs["run"] = _make_job(job_id="run", status="running")
    jobs.jobs["run"].update(
        {
            "batch_id": "batch1",
            "progress_percent": 50,
            "elapsed_seconds": 5,
            "eta_seconds": 5,
            "eta_confidence": "history",
            "_started_ts": 110,
        }
    )
    jobs.jobs["queued"] = _make_job(job_id="queued", status="queued")
    jobs.jobs["queued"].update({
        "batch_id": "batch1",
        "_created_ts": 111,
        "expected_duration_seconds": 10,
        "eta_confidence": "history",
    })
    jobs.job_queue.put("queued")

    client = routes.app.test_client()
    res = client.get("/api/queue/status")

    payload = res.get_json()
    assert payload["total_active_items"] == 3
    assert payload["completed_active_items"] == 1
    assert payload["queue_progress_percent"] == 50
    assert payload["queue_eta_seconds"] == 15
    assert payload["queue_eta_confidence"] == "history"
    assert payload["summary"]["queue_progress_percent"] == 50
    _clear_jobs()


def test_completed_job_and_log_retention(monkeypatch, tmp_path):
    _clear_jobs()
    logs = tmp_path / "logs"
    logs.mkdir()
    old_log = logs / "old.txt"
    keep_log = logs / "keep.txt"
    old_log.write_text("old", encoding="utf-8")
    keep_log.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(jobs, "LOG_DIR", str(logs))
    monkeypatch.setattr(jobs, "JOB_RETENTION_COUNT", 1)
    monkeypatch.setattr(jobs, "JOB_MAX_AGE_SECONDS", 60)
    monkeypatch.setattr(jobs, "JOB_LOG_RETENTION_COUNT", 1)
    monkeypatch.setattr(jobs, "JOB_LOG_MAX_AGE_SECONDS", 60)
    keep_ts = jobs.time.time()
    old_ts = keep_ts - 120
    jobs.os.utime(old_log, (old_ts, old_ts))
    jobs.os.utime(keep_log, (keep_ts, keep_ts))
    jobs.jobs["old"] = _make_job(job_id="old", status="success", log_path=str(old_log))
    jobs.jobs["old"].update({"_finished_ts": old_ts, "_created_ts": old_ts})
    jobs.jobs["keep"] = _make_job(job_id="keep", status="success", log_path=str(keep_log))
    jobs.jobs["keep"].update({"_finished_ts": keep_ts, "_created_ts": keep_ts})

    jobs.prune_job_history()

    assert list(jobs.jobs) == ["keep"]
    assert keep_log.exists()
    assert not old_log.exists()
    _clear_jobs()


def test_listdir_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    (sibling / "nested").mkdir(parents=True)
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    client = routes.app.test_client()
    res = client.get("/api/listdir", query_string={"path": str(sibling)})

    assert res.status_code == 200
    assert res.get_json() == []


def test_api_add_rejects_prefix_sibling(monkeypatch, tmp_path):
    lib = tmp_path / "library"
    sibling = tmp_path / "library2"
    lib.mkdir()
    sibling.mkdir()
    video = sibling / "video.mp4"
    video.write_text("x")
    monkeypatch.setattr(routes, "LIB_ROOT", str(lib))

    client = routes.app.test_client()
    res = client.post("/api/add", data={"video": str(video)})

    assert res.status_code == 400


def test_logs_route_serves_plain_text(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("<script>alert(1)</script>", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/logs/job1")

    assert res.status_code == 200
    assert res.mimetype == "text/plain"
    assert res.get_data(as_text=True) == "<script>alert(1)</script>"
    _clear_jobs()


def test_api_logs_returns_initial_and_offset_chunks(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("first\nsecond\n", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/api/logs/job1")

    assert res.status_code == 200
    payload = res.get_json()
    assert payload["lines"] == ["first", "second"]
    assert payload["reset"] is False
    assert payload["job"]["id"] == "job1"
    offset = payload["offset"]

    with log_path.open("a", encoding="utf-8") as f:
        f.write("third\n")

    res = client.get("/api/logs/job1", query_string={"offset": offset})
    payload = res.get_json()
    assert payload["lines"] == ["third"]
    assert payload["offset"] > offset
    _clear_jobs()


def test_api_logs_missing_job_returns_404():
    _clear_jobs()

    client = routes.app.test_client()
    res = client.get("/api/logs/missing")

    assert res.status_code == 404
    assert res.get_json()["error"] == "Not found"


def test_api_logs_resets_when_offset_exceeds_file_size(tmp_path):
    _clear_jobs()
    log_path = tmp_path / "job.txt"
    log_path.write_text("after-rotate\n", encoding="utf-8")
    jobs.jobs["job1"] = _make_job(log_path=str(log_path))

    client = routes.app.test_client()
    res = client.get("/api/logs/job1", query_string={"offset": 9999})

    payload = res.get_json()
    assert payload["lines"] == ["after-rotate"]
    assert payload["reset"] is True
    assert payload["offset"] == log_path.stat().st_size
    _clear_jobs()


def test_workspace_escapes_dynamic_job_tables():
    workspace_script = (ROOT / "app" / "static" / "gifs.js").read_text()
    test_lab_script = _test_lab_frontend_source()

    assert "escapeHtml(j.video)" in workspace_script
    assert "escapeHtml(j.progress_label" in workspace_script
    assert "escapeHtml(j.out_gif)" in workspace_script
    assert "escapeHtml(formatDuration(j.elapsed_seconds" in workspace_script
    assert "escapeHtml(formatSize(j.output_size_bytes" in workspace_script
    assert "escapeHtml(j.gif_optimization_label || '')" in workspace_script
    assert "escapeHtml(variant.name)" in test_lab_script
    assert "escapeHtml(variant.settings_label" in test_lab_script
    assert "escapeHtml(file.source_name || '')" in test_lab_script
    assert "escapeHtml(file.original_url || file.url)" in test_lab_script
    assert "escapeHtml(file.download_url || file.url)" in test_lab_script
    assert "box.textContent +=" in workspace_script
    assert "opt.textContent =" in workspace_script


def test_gifs_workspace_uses_polling_instead_of_socketio():
    workspace_template = (ROOT / "app" / "templates" / "gifs.html").read_text()
    workspace_script = (ROOT / "app" / "static" / "gifs.js").read_text()
    test_lab_script = _test_lab_frontend_source()
    combined = workspace_template + workspace_script + test_lab_script

    assert "socket.io" not in combined
    assert "const socket = io()" not in combined
    assert "queue_update" not in combined
    assert "EventSource" not in combined
    assert "/api/stream" not in combined
    assert "fetch('/api/queue/status')" in workspace_script
    assert "workspaceRefreshTimer = setTimeout(refreshWorkspace, delay)" in workspace_script
    assert "setInterval(refreshQueue, 1000)" not in workspace_script
    assert "fetch('/api/status')" in workspace_script
    assert "fetch(`/api/logs/${encodeURIComponent(currentJob)}" in workspace_script
    assert "fetch(`/api/scan-estimate?${params.toString()}`" in workspace_script
    assert "fetch('/api/test-lab/run-status')" in test_lab_script
    assert "fetch('/api/test-lab/files')" in test_lab_script
    assert "fetch('/api/test-lab/run'" in test_lab_script
    assert "fetch('/api/test-lab/delete'" in test_lab_script
    assert "fetch('/api/test-lab/preview'" in test_lab_script
    assert "if (state.playerActivated) requestSelectedPreviews(files)" in test_lab_script
    assert "Press Play to load preview" in test_lab_script
    assert "Load and play previews" in test_lab_script
    assert "fetch(`/api/media-browser?path=${encodeURIComponent" in test_lab_script
    assert "setInterval(refreshTestLab, 1000)" not in workspace_script


def test_gifs_workspace_contains_expected_controls_and_metrics():
    base_template = (ROOT / "app" / "templates" / "base.html").read_text()
    workspace_template = (ROOT / "app" / "templates" / "gifs.html").read_text()
    workspace_script = (ROOT / "app" / "static" / "gifs.js").read_text()
    test_lab_script = _test_lab_frontend_source()

    assert '>GIFs</a>' in base_template
    assert '>Settings</a>' in base_template
    assert 'href="/queue"' not in base_template
    assert 'href="/completed"' not in base_template
    assert 'href="/live"' not in base_template
    assert 'data-tab-hash="new"' in workspace_template
    assert 'data-tab-hash="test"' in workspace_template
    assert 'data-tab-hash="queue"' in workspace_template
    assert 'data-tab-hash="completed"' in workspace_template
    assert 'data-tab-hash="logs"' in workspace_template
    assert "const tabHashes = ['new', 'test', 'queue', 'completed', 'logs']" in workspace_script
    assert "localStorage.setItem('gifs_active_tab'" in workspace_script
    assert "jobProgressBar" in workspace_template
    assert "queueProgressBar" in workspace_template
    assert "queue-progress-bar" in workspace_template
    assert "jobOptimization" in workspace_template
    assert "gif_optimization_label" in workspace_script
    assert "topSavings" in workspace_template
    assert "progressText" in workspace_template
    assert "scanEstimateMessage" in workspace_template
    assert "scanEstimateDetail" in workspace_template
    assert "Choose a folder" in workspace_template
    assert "AbortController" in workspace_script
    assert "scanEstimateToken" in workspace_script
    assert "setScanEstimate(data.message" in workspace_script
    assert "messageEl.textContent" in workspace_script
    assert "detailEl.textContent" in workspace_script
    assert "testLabRunProgressBar" in workspace_template
    assert "testLabVariants" in workspace_template
    assert "testLabPreviews" in workspace_template
    assert "testLabFilesBody" in workspace_template
    assert "testLabVariantTabs" in workspace_template
    assert "testLabVariantEditor" in workspace_template
    assert "testLabPlayPause" in workspace_template
    assert "data-player-play-icon" in workspace_template
    assert "testLabTimeline" in workspace_template
    assert "testLabPlaybackSpeed" in workspace_template
    assert "testLabSavedTray" in workspace_template
    assert "Optimize GIF" in workspace_template
    assert "height_preset" in test_lab_script
    assert "fps_preset" in test_lab_script
    assert "clip_len_preset" in test_lab_script
    assert "makeDefaultVariant(config.defaults || {}, 1)" in test_lab_script
    assert "testlab_slots" in test_lab_script
    assert "testlab_comparison_ids" in test_lab_script
    assert "data-test-rename-id" in test_lab_script
    assert "/api/test-lab/rename" in test_lab_script
    assert "/api/test-lab/preview" in test_lab_script
    assert "display_url" in test_lab_script
    assert "preview_status" in test_lab_script
    assert "preview_label" in test_lab_script
    assert "preview-badge" in test_lab_script
    assert "comparisonStructureSignature" in test_lab_script
    assert "bi-download" in test_lab_script
    assert "test-player-dropzone" in test_lab_script
    assert "data-keyboard-deck" in test_lab_script
    assert "Sortable.create" in test_lab_script
    assert "SynchronizedGifPlayer" in test_lab_script
    assert "decompressFrames" in test_lab_script
    assert "this.requestFrame(this.tick)" in test_lab_script
    assert "play.innerHTML" not in test_lab_script
    assert "data-test-file-id" in test_lab_script
    assert "<img" not in test_lab_script
    assert "Original FPS" not in workspace_template
    assert "Original FPS" not in test_lab_script
    assert "fps_original" not in workspace_template
    assert "fps_original" not in test_lab_script
    assert "speed=" not in workspace_template
    assert "speed=" not in test_lab_script


def test_settings_template_contains_preview_controls():
    settings_template = (ROOT / "app" / "templates" / "settings.html").read_text()

    assert "Test Lab preview height" in settings_template
    assert "Original / no scaled preview" in settings_template
    assert "preview_height_preset" in settings_template
    assert "preview_height_custom" in settings_template


def test_live_logs_tracks_last_job_result_instead_of_forcing_running():
    workspace_script = (ROOT / "app" / "static" / "gifs.js").read_text()

    assert "setStatus('running')" not in workspace_script
    assert "pollTimer" in workspace_script
    assert "let lastJob" in workspace_script
    assert "newestFinishedJob(all)" in workspace_script
    assert "clearInterval(pollTimer)" in workspace_script


def test_dockerfile_uses_gunicorn_wsgi_entrypoint():
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert "COPY app ./app" in dockerfile
    assert "gifsicle" in dockerfile
    assert '"gunicorn"' in dockerfile
    assert '"--threads", "8"' in dockerfile
    assert '"--graceful-timeout", "10"' in dockerfile
    assert '"app.wsgi:app"' in dockerfile
    assert "/app/main.py" not in dockerfile


def test_container_runs_exactly_one_gunicorn_worker():
    """All live state is in-process, so a second worker silently breaks it.

    Job queues, scan runs, and cleanup plans are module-level dicts guarded by
    threading.Lock. A second gunicorn worker is a second process with its own
    copy: progress reports against the wrong run, cancellation misses, and the
    file-identity checks that make duplicate cleanup safe compare against
    identities captured somewhere else. Threads share the state; processes do not.
    """
    dockerfile = (ROOT / "Dockerfile").read_text()

    assert '"--workers", "1"' in dockerfile, (
        "The container must run a single gunicorn worker. Move the module-level "
        "state in jobs.py and the *_maintenance.py modules out of process memory "
        "before raising this."
    )
    assert '"--workers", "2"' not in dockerfile

    # The workers are daemon threads started by wsgi.py; without them the queue
    # accepts jobs that nothing ever runs.
    wsgi = (ROOT / "app" / "wsgi.py").read_text()
    for starter in ("start_worker()",
                    "start_test_lab_worker()",
                    "start_landscape_poster_worker()"):
        assert starter in wsgi, f"wsgi.py no longer calls {starter}"


def test_entrypoint_chowns_library_only_when_requested():
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()

    assert "CHOWN_LIBRARY" in entrypoint
    assert 'chown -R app:app /state' in entrypoint
    assert '[ "${CHOWN_LIBRARY:-0}" = "1" ]' in entrypoint
    assert "for dir in /library /state" not in entrypoint


def test_entrypoint_defaults_to_group_writable_umask():
    dockerfile = (ROOT / "Dockerfile").read_text()
    entrypoint = (ROOT / "docker-entrypoint.sh").read_text()

    assert "UMASK=002" in dockerfile
    assert 'UMASK="${UMASK:-002}"' in entrypoint
    assert 'umask "$UMASK"' in entrypoint


def test_runtime_requirements_exclude_dev_tools():
    requirements = (ROOT / "requirements.txt").read_text()
    dev_requirements = (ROOT / "requirements-dev.txt").read_text()
    workflow_files = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
    assert workflow_files, "No CI workflow files found"
    workflow = "\n".join(path.read_text() for path in workflow_files)

    assert "pytest" not in requirements
    assert "pip-audit" not in requirements
    assert "eventlet" not in requirements
    assert "flask-socketio" not in requirements
    assert "gunicorn==26.0.0" in requirements
    assert "werkzeug==3.1.8" in requirements
    assert "pytest==9.0.3" in dev_requirements
    assert "pip-audit==2.10.1" in dev_requirements
    assert "ruff==0.16.3" in dev_requirements
    assert "pytest-cov==7.1.0" in dev_requirements
    assert "pytest-cov" not in requirements
    assert "ruff" not in requirements
    assert "python -m pip_audit -r requirements.txt" in workflow
    assert "python -m pip_audit -r requirements-dev.txt" in workflow
    assert "python -m ruff check ." in workflow
    assert "--cov-fail-under=80" in workflow
    # Without ffmpeg and gifsicle on the runner, the GIF frame regression
    # tests and the optimization test skip and prove nothing.
    assert "ffmpeg gifsicle" in workflow


def test_templates_never_reach_for_a_third_party_cdn():
    """The app is a private-LAN tool; its own interface must not need the internet.

    Bootstrap, Bootstrap Icons, and Inter used to load from jsdelivr and Google
    Fonts, so an isolated network lost the stylesheet, the icons, and the
    typeface. They are vendored under app/static/vendor now.
    """
    template_dir = ROOT / "app" / "templates"
    static_dir = ROOT / "app" / "static"
    vendor_dir = static_dir / "vendor"

    hosts = ("cdn.jsdelivr.net", "fonts.googleapis.com", "fonts.gstatic.com",
             "unpkg.com", "cdnjs.cloudflare.com", "stackpath.bootstrapcdn.com")

    sources = list(template_dir.rglob("*.html"))
    sources += [
        path for path in static_dir.rglob("*.js")
        if vendor_dir not in path.parents
    ]
    sources += [
        path for path in static_dir.rglob("*.css")
        if vendor_dir not in path.parents
    ]
    assert sources, "No templates or static sources found"

    for path in sources:
        content = path.read_text(encoding="utf-8")
        for host in hosts:
            assert host not in content, (
                f"{path.relative_to(ROOT)} loads an asset from {host}; "
                "vendor it under app/static/vendor instead"
            )


def test_vendored_frontend_assets_are_present():
    """A missing vendor file is an unstyled app, so fail loudly at test time."""
    vendor = ROOT / "app" / "static" / "vendor"
    expected = (
        "bootstrap/bootstrap.min.css",
        "bootstrap/bootstrap.bundle.min.js",
        "bootstrap-icons/bootstrap-icons.css",
        "bootstrap-icons/fonts/bootstrap-icons.woff2",
        "inter/inter.css",
        "inter/files/inter-latin-400-normal.woff2",
        "inter/files/inter-latin-600-normal.woff2",
    )
    for relative in expected:
        path = vendor / relative
        assert path.is_file(), f"missing vendored asset: {relative}"
        assert path.stat().st_size > 0, f"empty vendored asset: {relative}"

    base = (ROOT / "app" / "templates" / "base.html").read_text(encoding="utf-8")
    for filename in ("vendor/bootstrap/bootstrap.min.css",
                     "vendor/bootstrap-icons/bootstrap-icons.css",
                     "vendor/inter/inter.css",
                     "vendor/bootstrap/bootstrap.bundle.min.js"):
        assert filename in base, f"base.html no longer links {filename}"


def test_container_publish_waits_for_the_test_suite():
    """A commit that fails tests must never publish an image to GHCR."""
    workflow_dir = ROOT / ".github" / "workflows"
    publishing = []
    for path in sorted(workflow_dir.glob("*.yml")):
        content = path.read_text()
        if "docker/build-push-action" not in content:
            continue
        publishing.append(path.name)
        jobs = content.split("jobs:", 1)[1]
        build_job = jobs.split("build-and-push:", 1)[1]
        assert "needs: [tests]" in build_job, (
            f"{path.name} publishes an image without depending on the tests job"
        )
        assert "python -m pytest" in content, (
            f"{path.name} publishes an image but its tests job is in another "
            "workflow file, where 'needs:' cannot reach it"
        )
    assert publishing, "No workflow builds and pushes the container image"
