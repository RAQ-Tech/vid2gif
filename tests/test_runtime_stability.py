from app import jobs, test_lab


def test_start_worker_is_idempotent(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append((self.target, self.daemon, self.name))

    monkeypatch.setattr(jobs, "_worker_started", False)
    monkeypatch.setattr(jobs.threading, "Thread", FakeThread)

    jobs.start_worker()
    jobs.start_worker()

    assert started == [
        (jobs.worker, True, "vid2gif-worker"),
    ]


def test_start_test_lab_worker_is_idempotent(monkeypatch):
    started = []

    class FakeThread:
        def __init__(self, target, daemon, name):
            self.target = target
            self.daemon = daemon
            self.name = name

        def start(self):
            started.append((self.target, self.daemon, self.name))

    monkeypatch.setattr(test_lab, "_worker_started", False)
    monkeypatch.setattr(test_lab.threading, "Thread", FakeThread)

    test_lab.start_test_lab_worker()
    test_lab.start_test_lab_worker()

    assert started == [
        (test_lab.worker, True, "vid2gif-test-lab"),
    ]


def test_dockerfile_documents_runtime_port():
    dockerfile = "Dockerfile"
    with open(dockerfile, encoding="utf-8") as f:
        contents = f.read()

    assert "EXPOSE 904" in contents
