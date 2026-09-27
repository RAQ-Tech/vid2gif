import pytest

from app import time_estimate


def test_known_constant_rate_predicts_actual_finish():
    state = {}
    for second in range(41):
        time_estimate.observe(state, second * 2, second)
    eta, confidence = time_estimate.remaining(state, 200, 40, historical_rate=100)
    assert eta == 60  # Actual completion at t=100; old hardware history is irrelevant.
    assert confidence == "live"


def test_recent_rate_adapts_to_slowdown_and_speedup():
    state = {}
    completed = 0
    for second in range(101):
        completed += 10 if second < 50 else 1
        time_estimate.observe(state, completed, second)
    assert time_estimate.remaining(state, completed + 100, 100)[0] == 100
    for second in range(101, 151):
        completed += 10
        time_estimate.observe(state, completed, second)
    assert time_estimate.remaining(state, completed + 100, 150)[0] == 10
    assert len(state["points"]) <= 32


def test_duplicate_updates_and_polling_do_not_change_rate():
    state = {}
    for second in range(11):
        time_estimate.observe(state, second, second)
        for _ in range(20):
            time_estimate.observe(state, second, second)
            time_estimate.remaining(state, 20, second)
    assert time_estimate.remaining(state, 20, 10)[0] == 10
    assert len(state["points"]) == 11


def test_subsecond_reporting_still_uses_a_recent_window():
    state = {}
    completed = 0
    for tick in range(1001):
        completed += 1 if tick < 500 else 0.1
        time_estimate.observe(state, completed, tick / 10)
    eta, _ = time_estimate.remaining(state, completed + 100, 100)
    assert 99 <= eta <= 101
    assert len(state["points"]) <= 32


def test_stalled_work_never_counts_down_to_zero_and_recovers():
    state = {}
    time_estimate.observe(state, 0, 0)
    time_estimate.observe(state, 5, 5)
    assert time_estimate.remaining(state, 10, 10)[0] == 5
    assert time_estimate.remaining(state, 10, 20) == (None, "recalculating")
    time_estimate.observe(state, 6, 21)
    assert time_estimate.remaining(state, 10, 21)[0] > 0


@pytest.mark.parametrize("total", [None, float("nan"), float("inf"), -1])
def test_unknown_or_invalid_workload_has_no_estimate(total):
    state = {}
    time_estimate.observe(state, 1, 1)
    assert time_estimate.remaining(state, total, 2, historical_rate=1)[0] is None


def test_history_expiry_and_unknown_serial_component():
    assert time_estimate.historical_remaining(10, 9.9) == 1
    assert time_estimate.historical_remaining(10, 10) is None
    assert time_estimate.serial_sum([10, None, 30]) is None
    assert time_estimate.serial_sum([10, 20, 30]) == 60
