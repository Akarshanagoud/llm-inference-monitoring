"""SLO evaluation and multi-window burn-rate alerting."""
import time

from llmobs import SLO, Severity, SLOTracker
from llmobs.slo import BurnRateWindow, default_slos


def _tracker(objective=0.99):
    return SLOTracker([SLO(name="availability", objective=objective)])


def test_all_good_events_leave_the_budget_intact():
    tracker = _tracker()
    for _ in range(100):
        tracker.record(ok=True, latency_seconds=0.1)
    status = tracker.status("availability")
    assert status.observed == 1.0
    assert status.budget_remaining == 1.0
    assert status.severity is Severity.OK


def test_error_rate_consumes_budget_proportionally():
    tracker = _tracker(objective=0.99)  # 1% budget
    for i in range(1000):
        tracker.record(ok=i % 200 != 0, latency_seconds=0.1)  # 0.5% errors
    status = tracker.status("availability")
    assert status.bad_events == 5
    # Half the budget spent means half remaining.
    assert 0.4 < status.budget_remaining < 0.6


def test_exhausted_budget_floors_at_zero():
    tracker = _tracker(objective=0.99)
    for _ in range(100):
        tracker.record(ok=False, latency_seconds=0.1)
    assert tracker.status("availability").budget_remaining == 0.0


def test_high_burn_rate_pages():
    tracker = _tracker(objective=0.99)
    for _ in range(100):
        tracker.record(ok=False, latency_seconds=0.1)
    status = tracker.status("availability")
    assert status.severity is Severity.PAGE
    assert "fast" in status.triggered_windows


def test_low_traffic_does_not_page_on_noise():
    # Three requests, all failing, is a 100% error rate and an enormous burn
    # rate - but it is also three requests. The minimum-sample guard exists
    # so a quiet service does not page at 3am on statistical noise.
    tracker = _tracker(objective=0.99)
    for _ in range(3):
        tracker.record(ok=False, latency_seconds=0.1)
    assert tracker.status("availability").severity is Severity.OK


def test_slow_steady_leak_files_a_ticket_not_a_page():
    tracker = SLOTracker(
        [
            SLO(
                name="availability",
                objective=0.99,
                burn_windows=(
                    BurnRateWindow("fast", 3600, 14.4, Severity.PAGE),
                    BurnRateWindow("slow", 259200, 1.0, Severity.TICKET),
                ),
            )
        ]
    )
    now = time.time()
    # 1.5% errors spread over three days: above the objective, well below the
    # fast-burn threshold.
    for i in range(2000):
        tracker.record(
            ok=i % 67 != 0, latency_seconds=0.1, timestamp=now - 259_000 + i * 100
        )
    status = tracker.status("availability", now=now)
    assert status.severity is Severity.TICKET
    assert "fast" not in status.triggered_windows


def test_latency_slo_counts_slow_successes_as_bad():
    tracker = SLOTracker(
        [SLO(name="latency", objective=0.95, kind="latency", latency_threshold_seconds=1.0)]
    )
    for _ in range(90):
        tracker.record(ok=True, latency_seconds=0.5)
    for _ in range(10):
        tracker.record(ok=True, latency_seconds=3.0)
    status = tracker.status("latency")
    assert status.bad_events == 10
    assert status.observed == 0.9


def test_availability_slo_ignores_latency():
    tracker = _tracker()
    tracker.record(ok=True, latency_seconds=600.0)
    assert tracker.status("availability").observed == 1.0


def test_events_outside_the_window_are_dropped():
    tracker = SLOTracker([SLO(name="availability", objective=0.99, window_seconds=60)])
    now = time.time()
    tracker.record(ok=False, latency_seconds=0.1, timestamp=now - 3600)
    tracker.record(ok=True, latency_seconds=0.1, timestamp=now)
    assert tracker.status("availability", now=now).total_events == 1


def test_default_slos_cover_availability_and_both_latency_shapes():
    names = {s.name for s in default_slos()}
    assert names == {"availability", "latency_p95_5s", "ttft_p95_1s"}


def test_worst_severity_is_the_maximum_across_slos():
    tracker = SLOTracker(
        [
            SLO(name="a", objective=0.99),
            SLO(name="b", objective=0.99),
        ]
    )
    for _ in range(100):
        tracker.record(ok=True, latency_seconds=0.1)
    assert tracker.worst_severity() is Severity.OK
    for _ in range(100):
        tracker.record(ok=False, latency_seconds=0.1)
    assert tracker.worst_severity() is Severity.PAGE


def test_status_serialises():
    tracker = _tracker()
    tracker.record(ok=True, latency_seconds=0.1)
    payload = tracker.status("availability").to_dict()
    assert payload["slo"] == "availability"
    assert "burn_rates" in payload
