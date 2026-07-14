import sys
import threading
import time

from app.process_runner import run_streaming_process


def test_streaming_process_drains_large_output_without_deadlock():
    result = run_streaming_process(
        [
            sys.executable,
            "-u",
            "-c",
            "import sys; [print('error-' + str(i) + '-' + ('x' * 200)) for i in range(5000)]",
        ],
        timeout=15,
        tail_lines=8,
    )

    assert result.returncode == 0
    assert result.cancelled is False
    assert "error-4999" in result.output_tail
    assert "error-0-" not in result.output_tail


def test_streaming_process_stops_after_no_output_timeout():
    result = run_streaming_process(
        [sys.executable, "-u", "-c", "import time; print('start'); time.sleep(5)"],
        stall_timeout=0.2,
        poll_interval=0.02,
    )

    assert result.stalled is True
    assert result.returncode is not None
    assert "start" in result.output_tail


def test_streaming_process_honors_cancellation():
    cancelled = threading.Event()
    timer = threading.Timer(0.2, cancelled.set)
    timer.start()
    try:
        result = run_streaming_process(
            [
                sys.executable,
                "-u",
                "-c",
                "import time\nwhile True:\n print('working', flush=True)\n time.sleep(.05)",
            ],
            cancel_requested=cancelled.is_set,
            stall_timeout=2,
            poll_interval=0.02,
        )
    finally:
        timer.cancel()

    assert result.cancelled is True
    assert result.returncode is not None
