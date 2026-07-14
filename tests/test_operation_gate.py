import threading
import time
from contextlib import contextmanager

from app import operation_gate
from app.operation_gate import LibraryOperationGate, OperationCancelled


def test_library_gate_is_fifo_and_reports_current_waiting_and_recent():
    gate = LibraryOperationGate()
    release = threading.Event()
    order = []
    first_state = {"status": "running", "progress_percent": 25, "progress_label": "Reading"}
    second_state = {"status": "queued"}

    def first():
        with gate.operation("one", label="First", state=first_state):
            order.append("one")
            release.wait(timeout=2)
            first_state["status"] = "success"

    def second():
        with gate.operation("two", label="Second", state=second_state):
            order.append("two")
            second_state["status"] = "success"

    thread_one = threading.Thread(target=first)
    thread_two = threading.Thread(target=second)
    thread_one.start()
    deadline = time.time() + 2
    while not gate.status_payload()["active"] and time.time() < deadline:
        time.sleep(0.01)
    thread_two.start()
    deadline = time.time() + 2
    while gate.status_payload()["waiting_count"] != 1 and time.time() < deadline:
        time.sleep(0.01)

    status = gate.status_payload()
    assert status["current"]["label"] == "First"
    assert status["current"]["progress_percent"] == 25
    assert status["waiting"][0]["label"] == "Second"

    release.set()
    thread_one.join(timeout=2)
    thread_two.join(timeout=2)

    assert order == ["one", "two"]
    final = gate.status_payload()
    assert final["active"] is False
    assert [item["label"] for item in final["recent"][:2]] == ["Second", "First"]


def test_library_gate_cancels_waiting_operation():
    gate = LibraryOperationGate()
    release = threading.Event()
    cancel = threading.Event()
    cancelled = []

    def first():
        with gate.operation("one", label="First"):
            release.wait(timeout=2)

    def second():
        try:
            with gate.operation("two", label="Second", cancel_requested=cancel.is_set):
                raise AssertionError("cancelled waiter must not start")
        except OperationCancelled:
            cancelled.append(True)

    thread_one = threading.Thread(target=first)
    thread_two = threading.Thread(target=second)
    thread_one.start()
    time.sleep(0.05)
    thread_two.start()
    time.sleep(0.05)
    cancel.set()
    thread_two.join(timeout=2)
    release.set()
    thread_one.join(timeout=2)

    assert cancelled == [True]
    assert gate.status_payload()["recent"][0]["status"] in {"completed", "cancelled"}


def test_coordinated_operation_persists_waiting_cancellation(monkeypatch):
    saved = []
    state = {"id": "persisted", "status": "cancelling", "cancel_requested": True}

    @contextmanager
    def cancelled_operation(*args, **kwargs):
        raise OperationCancelled("cancelled")
        yield

    monkeypatch.setattr(operation_gate, "library_operation", cancelled_operation)

    @operation_gate.coordinated_library_operation(
        "Persistent operation",
        on_cancel=lambda current: saved.append(dict(current)),
    )
    def worker(current):
        raise AssertionError("cancelled operation must not start")

    assert worker(state) is None
    assert state["status"] == "cancelled"
    assert saved[0]["status"] == "cancelled"
