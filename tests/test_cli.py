"""CLI commands and exit codes."""
import json

from llmobs.cli import main


def _db(tmp_path):
    return str(tmp_path / "traces.db")


def test_load_runs_and_reports(tmp_path, capsys):
    code = main(["load", "-n", "20", "-c", "4", "--speed", "1000", "--db", _db(tmp_path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "requests" in out and "SLO status" in out


def test_load_json_output_is_parseable(tmp_path, capsys):
    main(["load", "-n", "10", "-c", "2", "--speed", "1000", "--json", "--db", _db(tmp_path)])
    payload = json.loads(capsys.readouterr().out)
    assert payload["load"]["requests"] == 10
    assert "latency_ms" in payload["load"]


def test_high_failure_rate_exits_nonzero(tmp_path, capsys):
    code = main([
        "load", "-n", "20", "-c", "2", "--speed", "1000",
        "--failure-rate", "1.0", "--max-retries", "0", "--db", _db(tmp_path),
    ])
    capsys.readouterr()
    assert code == 1


def test_traces_lists_recorded_requests(tmp_path, capsys):
    db = _db(tmp_path)
    main(["load", "-n", "5", "-c", "2", "--speed", "1000", "--db", db])
    capsys.readouterr()
    assert main(["traces", "--db", db, "--limit", "5"]) == 0
    assert "TRACE" in capsys.readouterr().out


def test_trace_command_prints_the_span_tree(tmp_path, capsys):
    db = _db(tmp_path)
    main(["load", "-n", "3", "-c", "1", "--speed", "1000", "--db", db])
    capsys.readouterr()
    main(["traces", "--db", db, "--limit", "1", "--json"])
    trace_id = json.loads(capsys.readouterr().out)[0]["trace_id"]

    assert main(["trace", trace_id, "--db", db]) == 0
    out = capsys.readouterr().out
    assert "span tree" in out and "breakdown by stage" in out


def test_missing_trace_exits_nonzero(tmp_path, capsys):
    db = _db(tmp_path)
    main(["load", "-n", "1", "-c", "1", "--speed", "1000", "--db", db])
    capsys.readouterr()
    assert main(["trace", "nope", "--db", db]) == 1


def test_stages_reports_where_time_goes(tmp_path, capsys):
    db = _db(tmp_path)
    main(["load", "-n", "6", "-c", "2", "--speed", "1000", "--db", db])
    capsys.readouterr()
    assert main(["stages", "--db", db]) == 0
    out = capsys.readouterr().out
    assert "inference" in out and "SHARE" in out


def test_slo_replays_stored_traces(tmp_path, capsys):
    db = _db(tmp_path)
    main(["load", "-n", "12", "-c", "3", "--speed", "1000", "--db", db])
    capsys.readouterr()
    main(["slo", "--db", db, "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["replayed_traces"] == 12
    assert {s["slo"] for s in payload["slos"]} == {
        "availability", "latency_p95_5s", "ttft_p95_1s"
    }


def test_health_probes_the_backend(tmp_path, capsys):
    assert main(["health", "--db", _db(tmp_path)]) == 0
    assert json.loads(capsys.readouterr().out)["up"] is True


def test_metrics_command_emits_exposition_format(tmp_path, capsys):
    assert main([
        "metrics", "-n", "5", "-c", "2", "--speed", "1000", "--db", _db(tmp_path)
    ]) == 0
    out = capsys.readouterr().out
    assert "# TYPE llm_requests_total counter" in out


def test_version(capsys):
    assert main(["--version"]) == 0
    assert "llmobs" in capsys.readouterr().out


def test_no_command_prints_help(capsys):
    assert main([]) == 2
    assert "usage" in capsys.readouterr().out.lower()
