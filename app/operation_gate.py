import contextlib
import datetime
import functools
import threading
import time
from collections import deque


ACTIVE_STATUSES = {"queued", "running", "cancelling", "waiting"}


def _utc_iso(timestamp=None):
    timestamp = time.time() if timestamp is None else float(timestamp)
    return datetime.datetime.fromtimestamp(
        timestamp, tz=datetime.timezone.utc
    ).isoformat()


def _state_value(state, *keys, default=None):
    if not isinstance(state, dict):
        return default
    for key in keys:
        value = state.get(key)
        if value not in (None, ""):
            return value
    return default


class OperationCancelled(RuntimeError):
    pass


class OperationLease:
    def __init__(self, gate, record):
        self._gate = gate
        self._record = record
        self.outcome = ""

    def set_outcome(self, status):
        self.outcome = str(status or "").strip().lower()


class LibraryOperationGate:
    """FIFO serialization and observability for library-intensive work."""

    def __init__(self, recent_limit=20):
        self._condition = threading.Condition()
        self._waiting = []
        self._current = None
        self._recent = deque(maxlen=max(1, int(recent_limit)))

    def reset_for_tests(self):
        with self._condition:
            self._waiting.clear()
            self._current = None
            self._recent.clear()
            self._condition.notify_all()

    def _public(self, record, *, waiting=False, finished=False):
        state = record.get("state") if isinstance(record.get("state"), dict) else {}
        status = str(_state_value(state, "status", default="") or "").lower()
        if waiting:
            status = "waiting"
        elif not status:
            status = "completed" if finished else "running"
        progress_label = _state_value(
            state,
            "progress_label",
            "progress_text",
            "progress_detail",
            default="",
        )
        if waiting:
            progress_label = "Waiting for the current library operation"
        path = _state_value(
            state,
            "current_video",
            "video",
            "path",
            "source_path",
            default="",
        )
        result = {
            "id": record["id"],
            "label": record["label"],
            "kind": record["kind"],
            "status": status,
            "progress_percent": _state_value(state, "progress_percent", default=None),
            "progress_label": str(progress_label or ""),
            "path": str(path or ""),
            "href": record.get("href") or "",
            "cancel_url": record.get("cancel_url") or "",
            "queued_at": record.get("queued_at"),
            "started_at": record.get("started_at"),
            "finished_at": record.get("finished_at"),
        }
        if finished:
            result["status"] = record.get("outcome") or status or "completed"
            result["progress_percent"] = record.get(
                "final_progress_percent", result["progress_percent"]
            )
            result["progress_label"] = record.get(
                "final_progress_label", result["progress_label"]
            )
        return result

    @contextlib.contextmanager
    def operation(
        self,
        operation_id,
        *,
        label,
        kind="library",
        state=None,
        href="",
        cancel_url="",
        cancel_requested=None,
    ):
        record = {
            "id": str(operation_id),
            "label": str(label),
            "kind": str(kind),
            "state": state,
            "href": str(href or ""),
            "cancel_url": str(cancel_url or ""),
            "queued_at": _utc_iso(),
            "started_at": None,
            "finished_at": None,
        }
        lease = OperationLease(self, record)

        with self._condition:
            self._waiting.append(record)
            if isinstance(state, dict):
                state["operation_waiting"] = True
                state["operation_kind"] = record["kind"]
            while self._current is not None or self._waiting[0] is not record:
                if cancel_requested and cancel_requested():
                    self._waiting.remove(record)
                    if isinstance(state, dict):
                        state["operation_waiting"] = False
                    record["finished_at"] = _utc_iso()
                    record["outcome"] = "cancelled"
                    record["final_progress_label"] = "Cancelled while waiting"
                    record["final_progress_percent"] = _state_value(
                        state, "progress_percent", default=0
                    )
                    self._recent.appendleft(self._public(record, finished=True))
                    self._condition.notify_all()
                    raise OperationCancelled("Operation cancelled while waiting")
                self._condition.wait(timeout=0.2)

            self._waiting.pop(0)
            self._current = record
            record["started_at"] = _utc_iso()
            if isinstance(state, dict):
                state["operation_waiting"] = False

        try:
            yield lease
        except BaseException:
            if not lease.outcome:
                lease.outcome = "failed"
            raise
        finally:
            with self._condition:
                if self._current is record:
                    self._current = None
                record["finished_at"] = _utc_iso()
                inferred = str(_state_value(state, "status", default="") or "").lower()
                if inferred in ACTIVE_STATUSES:
                    inferred = "completed"
                record["outcome"] = lease.outcome or inferred or "completed"
                record["final_progress_label"] = str(
                    _state_value(
                        state,
                        "progress_label",
                        "progress_text",
                        "progress_detail",
                        default="",
                    )
                    or ""
                )
                record["final_progress_percent"] = _state_value(
                    state, "progress_percent", default=None
                )
                self._recent.appendleft(self._public(record, finished=True))
                self._condition.notify_all()

    def status_payload(self):
        with self._condition:
            current = self._public(self._current) if self._current else None
            waiting = [self._public(record, waiting=True) for record in self._waiting]
            recent = [dict(record) for record in self._recent]
        return {
            "active": current is not None,
            "current": current,
            "waiting": waiting,
            "waiting_count": len(waiting),
            "recent": recent,
        }


library_gate = LibraryOperationGate()


def library_operation(*args, **kwargs):
    return library_gate.operation(*args, **kwargs)


def status_payload():
    return library_gate.status_payload()


def coordinated_library_operation(
    label,
    *,
    kind="scan",
    href="/maintenance",
    state_index=0,
    cancel_url=None,
    on_cancel=None,
):
    """Decorate a worker whose state dictionary is one positional argument."""

    def decorate(function):
        @functools.wraps(function)
        def wrapped(*args, **kwargs):
            state = args[state_index] if len(args) > state_index else None
            if not isinstance(state, dict):
                return function(*args, **kwargs)
            operation_id = state.get("id") or f"{function.__name__}:{id(state)}"
            operation_label = label(state) if callable(label) else label
            operation_cancel_url = cancel_url(state) if callable(cancel_url) else cancel_url
            try:
                with library_operation(
                    f"{kind}:{operation_id}",
                    label=operation_label,
                    kind=kind,
                    state=state,
                    href=href,
                    cancel_url=operation_cancel_url or "",
                    cancel_requested=lambda: bool(state.get("cancel_requested")),
                ) as lease:
                    result = function(*args, **kwargs)
                    lease.set_outcome(state.get("status"))
                    return result
            except OperationCancelled:
                now = time.time()
                state.update(
                    {
                        "status": "cancelled",
                        "operation_waiting": False,
                        "progress_label": "Cancelled while waiting for library access",
                        "error": "",
                        "_finished_ts": now,
                        "finished_at": _utc_iso(now),
                    }
                )
                if on_cancel:
                    try:
                        on_cancel(state)
                    except Exception:
                        pass
                return None

        return wrapped

    return decorate
