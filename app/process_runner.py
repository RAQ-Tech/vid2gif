import queue
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass


@dataclass(frozen=True)
class ProcessResult:
    returncode: int | None
    output_tail: str = ""
    cancelled: bool = False
    stalled: bool = False
    timed_out: bool = False
    launch_error: str = ""


def _terminate_process(process, grace_seconds=3.0):
    poll = getattr(process, "poll", None)
    returncode = poll() if callable(poll) else getattr(process, "returncode", None)
    if returncode is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=max(0.1, float(grace_seconds)))
    except (OSError, subprocess.TimeoutExpired):
        try:
            process.kill()
            process.wait(timeout=max(0.1, float(grace_seconds)))
        except (OSError, subprocess.TimeoutExpired):
            pass


def run_streaming_process(
    command,
    *,
    on_line=None,
    cancel_requested=None,
    stall_timeout=None,
    timeout=None,
    tail_lines=40,
    poll_interval=0.1,
    cwd=None,
    env=None,
):
    """Run a child while continuously draining merged stdout/stderr.

    The caller receives only a bounded output tail. Cancellation and timeout
    checks happen outside the reader thread, so a child that stops producing
    output cannot block the controlling worker indefinitely.
    """

    try:
        process = subprocess.Popen(
            list(command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            cwd=cwd,
            env=env,
        )
    except (OSError, ValueError) as exc:
        return ProcessResult(returncode=None, launch_error=str(exc))

    lines = queue.Queue()
    reader_done = threading.Event()
    tail = deque(maxlen=max(1, int(tail_lines or 1)))

    def read_output():
        try:
            if process.stdout:
                for raw in process.stdout:
                    lines.put((raw or "").rstrip("\r\n"))
        finally:
            reader_done.set()

    reader = threading.Thread(
        target=read_output,
        daemon=True,
        name=f"vid2gif-process-reader-{getattr(process, 'pid', 'unknown')}",
    )
    reader.start()

    started = time.monotonic()
    last_output = started
    cancelled = False
    stalled = False
    timed_out = False

    def poll():
        method = getattr(process, "poll", None)
        return method() if callable(method) else getattr(process, "returncode", None)

    while True:
        drained = False
        while True:
            try:
                line = lines.get_nowait()
            except queue.Empty:
                break
            drained = True
            last_output = time.monotonic()
            if line:
                tail.append(line)
                if on_line:
                    on_line(line)

        if poll() is not None and reader_done.is_set() and lines.empty():
            break

        now = time.monotonic()
        if cancel_requested and cancel_requested():
            cancelled = True
        elif timeout and now - started >= float(timeout):
            timed_out = True
        elif stall_timeout and now - last_output >= float(stall_timeout):
            stalled = True

        if cancelled or timed_out or stalled:
            _terminate_process(process)
            continue

        if not drained:
            time.sleep(max(0.01, float(poll_interval)))

    reader.join(timeout=1.0)
    try:
        if process.stdout:
            process.stdout.close()
    except (AttributeError, OSError):
        pass

    return ProcessResult(
        returncode=process.returncode,
        output_tail="\n".join(tail),
        cancelled=cancelled,
        stalled=stalled,
        timed_out=timed_out,
    )
