"""Trace storage.

SQLite, because a reference platform that requires Elasticsearch before it can
show you a trace is a platform nobody runs. The schema is deliberately close to
what a real trace store looks like — spans in one table, indexed on trace id and
timestamp — so swapping in ClickHouse or Tempo later is a driver change rather
than a redesign.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .trace import Trace

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id     TEXT PRIMARY KEY,
    started_at   REAL NOT NULL,
    duration_ms  REAL,
    status       TEXT NOT NULL,
    model        TEXT,
    backend      TEXT,
    span_count   INTEGER NOT NULL,
    prompt_tokens     INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    ttft_ms      REAL,
    valid        INTEGER,
    payload      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_traces_started  ON traces(started_at DESC);
CREATE INDEX IF NOT EXISTS idx_traces_status   ON traces(status);
CREATE INDEX IF NOT EXISTS idx_traces_model    ON traces(model);
CREATE INDEX IF NOT EXISTS idx_traces_duration ON traces(duration_ms DESC);

CREATE TABLE IF NOT EXISTS spans (
    span_id     TEXT PRIMARY KEY,
    trace_id    TEXT NOT NULL,
    parent_id   TEXT,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL,
    started_at  REAL NOT NULL,
    duration_ms REAL,
    status      TEXT NOT NULL,
    payload     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_spans_trace ON spans(trace_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind  ON spans(kind, duration_ms DESC);
"""


class TraceStore:
    """Durable trace storage with the queries an on-call engineer actually runs."""

    def __init__(self, path: str | Path = "traces.db") -> None:
        self.path = str(path)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        # WAL lets the read-only dashboard queries run while writes continue.
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # -- write path ---------------------------------------------------------
    def export(self, trace: Trace) -> None:
        """Exporter interface, so a ``TraceStore`` can be handed to a Tracer."""
        self.save(trace)

    def save(self, trace: Trace) -> None:
        root = trace.root
        if root is None:
            return
        inference = next(
            (s for s in trace.spans if s.kind.value == "inference"), root
        )
        usage = trace.total_usage()
        payload = json.dumps(trace.to_dict())
        # The root span carries the request's time to first token. The first
        # inference span has none when that attempt failed and a retry answered.
        ttft_ms = root.ttft_ms if root.ttft_ms is not None else inference.ttft_ms

        with self._lock:
            self._conn.execute(
                """INSERT OR REPLACE INTO traces
                   (trace_id, started_at, duration_ms, status, model, backend, span_count,
                    prompt_tokens, completion_tokens, ttft_ms, valid, payload)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    trace.trace_id,
                    root.started_at,
                    trace.duration_ms,
                    trace.status.value,
                    inference.model or root.attributes.get("model"),
                    inference.backend or root.attributes.get("backend"),
                    len(trace.spans),
                    usage.prompt_tokens,
                    usage.completion_tokens,
                    ttft_ms,
                    1 if root.attributes.get("valid", True) else 0,
                    payload,
                ),
            )
            self._conn.executemany(
                """INSERT OR REPLACE INTO spans
                   (span_id, trace_id, parent_id, name, kind, started_at, duration_ms,
                    status, payload)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                [
                    (
                        s.span_id, s.trace_id, s.parent_id, s.name, s.kind.value,
                        s.started_at, s.duration_ms, s.status.value, json.dumps(s.to_dict()),
                    )
                    for s in trace.spans
                ],
            )
            self._conn.commit()

    # -- read path ----------------------------------------------------------
    def get(self, trace_id: str) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT payload FROM traces WHERE trace_id = ?", (trace_id,)
        ).fetchone()
        return json.loads(row["payload"]) if row else None

    def recent(
        self,
        limit: int = 50,
        status: str | None = None,
        model: str | None = None,
        min_duration_ms: float | None = None,
        invalid_only: bool = False,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("status = ?")
            params.append(status)
        if model:
            clauses.append("model = ?")
            params.append(model)
        if min_duration_ms is not None:
            clauses.append("duration_ms >= ?")
            params.append(min_duration_ms)
        if invalid_only:
            clauses.append("valid = 0")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(limit)
        rows = self._conn.execute(
            f"""SELECT trace_id, started_at, duration_ms, status, model, backend,
                       prompt_tokens, completion_tokens, ttft_ms, valid
                FROM traces {where} ORDER BY started_at DESC LIMIT ?""",
            params,
        ).fetchall()
        return [dict(row) for row in rows]

    def slowest(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            """SELECT trace_id, duration_ms, status, model, ttft_ms, completion_tokens
               FROM traces ORDER BY duration_ms DESC LIMIT ?""",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    def stage_latency(self) -> list[dict[str, Any]]:
        """Where the time goes, by span kind.

        This is the query that answers "the model got slower" with "no, the
        retrieval step did".
        """
        rows = self._conn.execute(
            """SELECT kind,
                      COUNT(*)      AS count,
                      AVG(duration_ms) AS mean_ms,
                      MAX(duration_ms) AS max_ms,
                      SUM(duration_ms) AS total_ms
               FROM spans WHERE duration_ms IS NOT NULL
               GROUP BY kind ORDER BY total_ms DESC"""
        ).fetchall()
        return [
            {
                "kind": row["kind"],
                "count": row["count"],
                "mean_ms": round(row["mean_ms"], 3),
                "max_ms": round(row["max_ms"], 3),
                "total_ms": round(row["total_ms"], 3),
            }
            for row in rows
        ]

    def outcomes(self, since: float = 0.0, limit: int = 50_000) -> list[dict[str, Any]]:
        """Requests started at or after ``since``, oldest first, with their outcome.

        ``outcome`` is the root span's status: whether the caller got an answer.
        ``status`` is the trace's: "error" when any attempt failed, including
        attempts a retry recovered. Telling the two apart is the difference
        between an outage and a flaky dependency.
        """
        with self._lock:
            rows = self._conn.execute(
                """SELECT t.trace_id, t.started_at, t.duration_ms, t.status, t.ttft_ms,
                          t.prompt_tokens, t.completion_tokens, t.valid, s.status AS outcome
                   FROM traces t
                   JOIN spans s ON s.trace_id = t.trace_id AND s.parent_id IS NULL
                   WHERE t.started_at >= ?
                   ORDER BY t.started_at ASC LIMIT ?""",
                (since, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def breakdown(self, since: float = 0.0) -> list[dict[str, Any]]:
        """Mean exclusive time per stage, so the shares add up to the request."""
        totals: dict[str, float] = {}
        count = 0
        with self._lock:
            rows = self._conn.execute(
                "SELECT payload FROM traces WHERE started_at >= ?", (since,)
            ).fetchall()
        for row in rows:
            count += 1
            for kind, ms in json.loads(row["payload"]).get("breakdown_ms", {}).items():
                totals[kind] = totals.get(kind, 0.0) + (ms or 0.0)
        grand = sum(totals.values()) or 1.0
        return [
            {"kind": kind, "mean_ms": round(total / count, 3), "share": round(total / grand, 4)}
            for kind, total in sorted(totals.items(), key=lambda item: -item[1])
        ]

    def summary(self) -> dict[str, Any]:
        row = self._conn.execute(
            """SELECT COUNT(*) AS traces,
                      SUM(CASE WHEN status != 'ok' THEN 1 ELSE 0 END) AS errors,
                      SUM(CASE WHEN valid = 0 THEN 1 ELSE 0 END) AS invalid,
                      AVG(duration_ms) AS mean_ms,
                      SUM(prompt_tokens) AS prompt_tokens,
                      SUM(completion_tokens) AS completion_tokens
               FROM traces"""
        ).fetchone()
        total = row["traces"] or 0
        return {
            "traces": total,
            "errors": row["errors"] or 0,
            "invalid_responses": row["invalid"] or 0,
            "error_rate": round((row["errors"] or 0) / total, 4) if total else 0.0,
            "mean_duration_ms": round(row["mean_ms"], 3) if row["mean_ms"] else 0.0,
            "prompt_tokens": row["prompt_tokens"] or 0,
            "completion_tokens": row["completion_tokens"] or 0,
        }

    def iter_traces(self, batch_size: int = 500) -> Iterator[dict[str, Any]]:
        offset = 0
        while True:
            rows = self._conn.execute(
                "SELECT payload FROM traces ORDER BY started_at LIMIT ? OFFSET ?",
                (batch_size, offset),
            ).fetchall()
            if not rows:
                return
            for row in rows:
                yield json.loads(row["payload"])
            offset += batch_size

    def prune(self, keep_last: int = 10_000) -> int:
        """Drop all but the most recent ``keep_last`` traces."""
        with self._lock:
            cursor = self._conn.execute(
                """DELETE FROM traces WHERE trace_id NOT IN
                   (SELECT trace_id FROM traces ORDER BY started_at DESC LIMIT ?)""",
                (keep_last,),
            )
            removed = cursor.rowcount
            self._conn.execute(
                "DELETE FROM spans WHERE trace_id NOT IN (SELECT trace_id FROM traces)"
            )
            self._conn.commit()
        return max(removed, 0)

    def close(self) -> None:
        self._conn.close()
