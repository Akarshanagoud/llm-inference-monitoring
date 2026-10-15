# Verified results

Output captured by actually running this repository. Nothing here is hand-written.

| | |
|---|---|
| Date | 2026-10-15 |
| Commit | `71a7067` |
| Python | Python 3.11.9 |
| OS | Windows 11 |

Timing figures (ms, rps) vary by machine and run. Counts and accuracy figures are deterministic.

## Reproduce

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]" pytest
pytest -q
python examples\quickstart.py
```

## Test suite

```text
........................................................................ [ 53%]
..............................................................           [100%]
134 passed in 4.56s
```

## `examples/quickstart.py`

```text

========================================================================
1. One instrumented request
========================================================================
trace_id      : 3344817a44ef489d99ed0e625067c7d4
duration      : 22.0 ms
ttft          : 21.2 ms
tokens        : 10 prompt / 106 completion
attempts      : 1
valid         : True
breakdown     : {'inference': 21.234, 'validation': 0.646, 'request': 0.111}

========================================================================
2. Response validation catches quality regressions
========================================================================
valid: False
  [warn] not_truncated: response does not end at a sentence boundary
  [fail] valid_json: expected JSON but parsing failed: Expecting value

========================================================================
3. Prefix caching shows up in the metrics
========================================================================
cached prompt tokens on the second call: 64

========================================================================
4. Concurrent load, with retries and queueing
========================================================================
requests        : 300 in 0.739s (405.86 rps)
successes       : 300
failures        : 0 (error rate 0.00%)
shed            : 0
invalid replies : 0
latency ms      : p50 23.76  p95 124.86  p99 131.73
ttft ms         : p50 23.49  p95 28.1
output tok/s    : 42753.5
prefix cache    : 80.3% of prompt tokens

========================================================================
5. Where the time actually goes
========================================================================
  request         29.96 ms mean  ###################### 55.6%
  inference       22.62 ms mean  ################ 41.9%
  retry           22.72 ms mean   2.4%
  validation       0.08 ms mean   0.1%

========================================================================
6. The slowest requests, and why
========================================================================
  b0c4e019cd1f  329.7 ms  {'request': 301.008, 'retry': 26.117, 'inference': 2.487, 'validation': 0.063}
  97ee7f2331b6  132.1 ms  {'request': 100.592, 'retry': 28.645, 'inference': 2.776, 'validation': 0.075}
  05934f2f6254  131.7 ms  {'request': 100.586, 'retry': 28.337, 'inference': 2.726, 'validation': 0.063}

========================================================================
7. SLO status and error budget
========================================================================
  availability     observed 100.000%  budget left 100.0%  [ok]
     burn rates: {'fast': 0.0, 'medium': 0.0, 'slow': 0.0}
  latency_p95_5s   observed 100.000%  budget left 100.0%  [ok]
     burn rates: {'fast': 0.0, 'medium': 0.0, 'slow': 0.0}
  ttft_p95_1s      observed 100.000%  budget left 100.0%  [ok]
     burn rates: {'fast': 0.0, 'medium': 0.0, 'slow': 0.0}

========================================================================
8. Prometheus exposition (excerpt)
========================================================================
  # HELP llm_request_duration_seconds End-to-end request latency
  # TYPE llm_request_duration_seconds histogram
  llm_request_duration_seconds_bucket{backend="local-sim",model="sim-7b-instruct",le="0.005"} 0
  llm_request_duration_seconds_bucket{backend="local-sim",model="sim-7b-instruct",le="0.01"} 1
  llm_request_duration_seconds_bucket{backend="local-sim",model="sim-7b-instruct",le="0.025"} 205
  llm_request_duration_seconds_bucket{backend="local-sim",model="sim-7b-instruct",le="0.05"} 288

========================================================================
9. Backend health, normalised across serving stacks
========================================================================
{
  "up": true,
  "model": "sim-7b-instruct",
  "queue_depth": 0.0,
  "running_batch_size": 0.0,
  "kv_cache_utilization": 0.0,
  "detail": "simulated backend"
}

Done. No GPU was harmed in the making of this demo.
```
