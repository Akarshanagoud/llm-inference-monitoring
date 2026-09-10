"""SLO definitions and error-budget tracking.

An alert that fires on "p99 > 2s" pages someone every time a single slow
request lands. An alert that fires on burn rate pages someone when the service
is actually going to miss its objective. This module implements the latter:
multi-window burn-rate evaluation, which is the part most reference platforms
skip and the part that decides whether on-call trusts the alerts.

Burn rate is the ratio of observed error rate to the rate that would exactly
exhaust the budget over the SLO window. Burn rate 1 means you finish the month
with zero budget left; burn rate 14.4 means a 30-day budget is gone in two
hours.
"""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Severity(str, Enum):
    OK = "ok"
    TICKET = "ticket"
    PAGE = "page"


@dataclass(frozen=True)
class BurnRateWindow:
    """One (window, threshold, severity) rule from the SRE workbook pattern."""

    name: str
    window_seconds: float
    threshold: float
    severity: Severity


# The canonical multi-window configuration for a 30-day objective: a fast
# window that pages on catastrophic burn, a slow window that files a ticket on
# a steady leak, and nothing in between to page at 3am for a blip.
DEFAULT_BURN_WINDOWS = (
    BurnRateWindow("fast", 3600, 14.4, Severity.PAGE),      # 2% of budget in 1h
    BurnRateWindow("medium", 21600, 6.0, Severity.PAGE),    # 5% of budget in 6h
    BurnRateWindow("slow", 259200, 1.0, Severity.TICKET),   # steady 3-day leak
)


@dataclass
class SLO:
    """A single objective over one indicator."""

    name: str
    #: e.g. 0.995 for "99.5% of requests succeed"
    objective: float
    #: "availability" | "latency"
    kind: str = "availability"
    #: For latency SLOs: the threshold a request must beat to count as good.
    latency_threshold_seconds: float | None = None
    window_seconds: float = 30 * 86_400
    burn_windows: tuple[BurnRateWindow, ...] = DEFAULT_BURN_WINDOWS

    def is_good(self, *, ok: bool, latency_seconds: float) -> bool:
        if self.kind == "latency":
            return ok and latency_seconds <= (self.latency_threshold_seconds or float("inf"))
        return ok

    @property
    def error_budget(self) -> float:
        return 1.0 - self.objective


@dataclass
class _Event:
    timestamp: float
    good: bool


@dataclass
class SLOStatus:
    slo: str
    objective: float
    observed: float
    total_events: int
    bad_events: int
    budget_remaining: float
    burn_rates: dict[str, float] = field(default_factory=dict)
    severity: Severity = Severity.OK
    triggered_windows: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "slo": self.slo,
            "objective": self.objective,
            "observed": round(self.observed, 6),
            "total_events": self.total_events,
            "bad_events": self.bad_events,
            "budget_remaining": round(self.budget_remaining, 4),
            "burn_rates": {k: round(v, 3) for k, v in self.burn_rates.items()},
            "severity": self.severity.value,
            "triggered_windows": self.triggered_windows,
        }


class SLOTracker:
    """Records request outcomes and evaluates every registered SLO.

    Events are held in memory for the longest configured window. That is fine
    for a gateway process at moderate volume and honest about its limits: for
    a fleet, record the same events to Prometheus and run these rules there.
    """

    def __init__(self, slos: list[SLO] | None = None, max_events: int = 200_000) -> None:
        self.slos = slos if slos is not None else default_slos()
        self._events: dict[str, deque[_Event]] = {slo.name: deque() for slo in self.slos}
        self.max_events = max_events

    def add_slo(self, slo: SLO) -> None:
        self.slos.append(slo)
        self._events.setdefault(slo.name, deque())

    def record(self, *, ok: bool, latency_seconds: float, timestamp: float | None = None) -> None:
        now = timestamp if timestamp is not None else time.time()
        for slo in self.slos:
            events = self._events[slo.name]
            events.append(_Event(now, slo.is_good(ok=ok, latency_seconds=latency_seconds)))
            self._trim(slo, now)

    def _trim(self, slo: SLO, now: float) -> None:
        events = self._events[slo.name]
        cutoff = now - slo.window_seconds
        while events and events[0].timestamp < cutoff:
            events.popleft()
        while len(events) > self.max_events:
            events.popleft()

    def _error_rate(self, slo: SLO, window_seconds: float, now: float) -> tuple[float, int]:
        cutoff = now - window_seconds
        total = 0
        bad = 0
        for event in reversed(self._events[slo.name]):
            if event.timestamp < cutoff:
                break
            total += 1
            if not event.good:
                bad += 1
        return (bad / total if total else 0.0), total

    def status(self, name: str, now: float | None = None) -> SLOStatus:
        slo = next(s for s in self.slos if s.name == name)
        moment = now if now is not None else time.time()

        overall_rate, total = self._error_rate(slo, slo.window_seconds, moment)
        bad = round(overall_rate * total)
        budget_used = overall_rate / slo.error_budget if slo.error_budget else 0.0

        burn_rates: dict[str, float] = {}
        severity = Severity.OK
        triggered: list[str] = []

        for window in slo.burn_windows:
            rate, window_total = self._error_rate(slo, window.window_seconds, moment)
            burn = rate / slo.error_budget if slo.error_budget else 0.0
            burn_rates[window.name] = burn
            # A handful of requests in a short window can show a 100% error
            # rate and a burn rate of 200. Requiring a minimum sample keeps
            # low-traffic services from paging on statistical noise.
            if window_total >= 10 and burn >= window.threshold:
                triggered.append(window.name)
                if window.severity is Severity.PAGE:
                    severity = Severity.PAGE
                elif severity is Severity.OK:
                    severity = Severity.TICKET

        return SLOStatus(
            slo=slo.name,
            objective=slo.objective,
            observed=1.0 - overall_rate,
            total_events=total,
            bad_events=bad,
            budget_remaining=max(0.0, 1.0 - budget_used),
            burn_rates=burn_rates,
            severity=severity,
            triggered_windows=triggered,
        )

    def all_status(self, now: float | None = None) -> list[SLOStatus]:
        return [self.status(slo.name, now) for slo in self.slos]

    def worst_severity(self, now: float | None = None) -> Severity:
        severities = [s.severity for s in self.all_status(now)]
        if Severity.PAGE in severities:
            return Severity.PAGE
        if Severity.TICKET in severities:
            return Severity.TICKET
        return Severity.OK


def default_slos() -> list[SLO]:
    """A defensible starting set for an inference gateway.

    Latency objectives are stated on TTFT as well as total duration, because a
    3-second response that starts streaming in 200 ms and a 3-second response
    that stares at the user in silence are not the same product.
    """
    return [
        SLO(name="availability", objective=0.995, kind="availability"),
        SLO(
            name="latency_p95_5s",
            objective=0.95,
            kind="latency",
            latency_threshold_seconds=5.0,
        ),
        SLO(
            name="ttft_p95_1s",
            objective=0.95,
            kind="latency",
            latency_threshold_seconds=1.0,
        ),
    ]
