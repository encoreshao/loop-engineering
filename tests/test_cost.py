import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import cost

REPO_ROOT = Path(__file__).resolve().parent.parent

# Verbatim (trimmed to relevant fields) from a real `claude -p "say hi"
# --output-format json` call made while designing this spec - see
# docs/superpowers/specs/2026-09-04-cost-tracking-design.md's
# "Investigation" section.
REAL_CLAUDE_JSON = {
    "duration_api_ms": 2792,
    "stop_reason": "end_turn",
    "session_id": "b05ae2bb-99ca-46d2-97d4-09a041077ef0",
    "total_cost_usd": 0.14211259999999998,
    "usage": {
        "input_tokens": 2,
        "cache_creation_input_tokens": 34536,
        "cache_read_input_tokens": 19123,
        "output_tokens": 14,
    },
    "modelUsage": {
        "claude-sonnet-5": {
            "inputTokens": 2,
            "outputTokens": 14,
            "cacheReadInputTokens": 19123,
            "cacheCreationInputTokens": 34536,
            "costUSD": 0.14211259999999998,
            "canonicalModel": "claude-sonnet-5",
            "provider": "firstParty",
        }
    },
    "is_error": False,
    "result": "Hi! What can I help you with today?",
    "type": "result",
    "duration_ms": 3109,
}


def test_extract_claude_usage_from_real_shape():
    usage = cost.extract_claude_usage(REAL_CLAUDE_JSON)

    assert usage == {
        "provider": "anthropic",
        "model": "claude-sonnet-5",
        "input_tokens": 2,
        "output_tokens": 14,
        "cache_read_tokens": 19123,
        "cache_write_tokens": 34536,
        "duration_ms": 3109,
        "cost_usd": 0.14211259999999998,
    }


def test_extract_claude_usage_returns_none_on_missing_total_cost_usd():
    broken = {k: v for k, v in REAL_CLAUDE_JSON.items() if k != "total_cost_usd"}

    assert cost.extract_claude_usage(broken) is None


def test_extract_claude_usage_returns_none_on_missing_usage():
    broken = {k: v for k, v in REAL_CLAUDE_JSON.items() if k != "usage"}

    assert cost.extract_claude_usage(broken) is None


def test_extract_claude_usage_returns_none_on_codex_shaped_input():
    codex_event = {"type": "thread.started", "thread_id": "01a06cff-09fb-7411-acf5-5a27f5cc46d2"}

    assert cost.extract_claude_usage(codex_event) is None


def test_extract_claude_usage_model_is_none_with_zero_or_multiple_model_usage_keys():
    zero_models = {**REAL_CLAUDE_JSON, "modelUsage": {}}
    assert cost.extract_claude_usage(zero_models)["model"] is None

    multi_models = {**REAL_CLAUDE_JSON, "modelUsage": {"claude-sonnet-5": {}, "claude-haiku-4-5": {}}}
    result = cost.extract_claude_usage(multi_models)
    assert result["model"] is None
    assert result["cost_usd"] == REAL_CLAUDE_JSON["total_cost_usd"]  # rest of the dict still populated


def test_extract_result_text_returns_result_field():
    assert cost.extract_result_text(REAL_CLAUDE_JSON) == "Hi! What can I help you with today?"


def test_extract_result_text_placeholder_when_missing():
    missing = {k: v for k, v in REAL_CLAUDE_JSON.items() if k != "result"}

    assert cost.extract_result_text(missing) == "(no result text in CLI output)"


def _run_cli(args):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "bin" / "cost.py"), *args],
        capture_output=True,
        text=True,
    )


def test_cli_extract_result_text_from_real_claude_file(tmp_path):
    path = tmp_path / "cli-output.json"
    path.write_text(json.dumps(REAL_CLAUDE_JSON))

    result = _run_cli(["extract-result-text", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "Hi! What can I help you with today?"


def test_cli_extract_result_text_passthrough_on_non_json(tmp_path):
    path = tmp_path / "cli-output.txt"
    path.write_text("plain text output, not JSON at all\n")

    result = _run_cli(["extract-result-text", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert "plain text output, not JSON at all" in result.stdout


def test_cli_usage_json_from_real_claude_file(tmp_path):
    path = tmp_path / "cli-output.json"
    path.write_text(json.dumps(REAL_CLAUDE_JSON))

    result = _run_cli(["usage-json", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    usage = json.loads(result.stdout)
    assert usage["provider"] == "anthropic"
    assert usage["cost_usd"] == REAL_CLAUDE_JSON["total_cost_usd"]


def test_cli_usage_json_empty_stdout_on_codex_shaped_input(tmp_path):
    path = tmp_path / "cli-output.jsonl"
    path.write_text('{"type": "thread.started", "thread_id": "abc"}\n')

    result = _run_cli(["usage-json", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_cli_usage_json_empty_stdout_on_non_json(tmp_path):
    path = tmp_path / "cli-output.txt"
    path.write_text("not json\n")

    result = _run_cli(["usage-json", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == ""


def test_cli_extract_result_text_exits_zero_on_missing_file(tmp_path):
    path = tmp_path / "does-not-exist.json"

    result = _run_cli(["extract-result-text", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert "FileNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_cli_usage_json_exits_zero_on_missing_file(tmp_path):
    path = tmp_path / "does-not-exist.json"

    result = _run_cli(["usage-json", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr
    assert "FileNotFoundError" not in result.stderr
    assert "Traceback" not in result.stderr


def test_extract_claude_usage_returns_none_on_json_array():
    json_array = [1, 2, 3]

    assert cost.extract_claude_usage(json_array) is None


def test_cli_extract_result_text_exits_zero_on_json_array(tmp_path):
    path = tmp_path / "cli-output.json"
    path.write_text(json.dumps([1, 2, 3]))

    result = _run_cli(["extract-result-text", "--cli-output-file", str(path)])

    assert result.returncode == 0, result.stderr


def test_cli_missing_cli_output_file_flag_exits_one(tmp_path):
    result = _run_cli(["extract-result-text"])

    assert result.returncode == 1
    assert "--cli-output-file is required" in result.stderr


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import metrics  # noqa: F401 - not called directly in these tests, but confirms the sibling import bin/cost.py itself relies on is resolvable in this test environment too


def _run_completed(run_id, data=None):
    return {"event_type": "run.completed", "run_id": run_id, "data": data or {}}


def _issue_started(issue_run_id, ts="2026-09-04T10:00:00.000Z"):
    return {"event_type": "issue.started", "issue_run_id": issue_run_id, "timestamp": ts}


def _issue_completed(issue_run_id, ts="2026-09-04T10:05:00.000Z"):
    return {"event_type": "issue.completed", "issue_run_id": issue_run_id, "timestamp": ts}


def test_compute_cost_metrics_sums_and_divides_correctly():
    events = [
        _run_completed("run_1", {"cost_usd": 1.5, "input_tokens": 100, "output_tokens": 50, "cache_read_tokens": 10, "cache_write_tokens": 5}),
        _run_completed("run_2", {"cost_usd": 2.5, "input_tokens": 200, "output_tokens": 60, "cache_read_tokens": 20, "cache_write_tokens": 15}),
        _issue_started("run_1_p_1"),
        _issue_completed("run_1_p_1"),
        _issue_started("run_1_p_2"),
    ]

    result = cost.compute_cost_metrics(events)

    assert result["total_cost_usd"] == 4.0
    assert result["total_tokens"] == 100 + 50 + 10 + 5 + 200 + 60 + 20 + 15
    assert result["cost_per_issue"] == 4.0 / 2  # 2 issues processed
    assert result["cost_per_resolution"] == 4.0 / 1  # 1 completed


def test_compute_cost_metrics_ignores_run_completed_without_cost_usd():
    events = [
        _run_completed("run_codex", {}),  # e.g. a Codex run, no usage data
        _run_completed("run_claude", {"cost_usd": 3.0, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0}),
    ]

    result = cost.compute_cost_metrics(events)

    assert result["total_cost_usd"] == 3.0
    assert result["total_tokens"] == 15


def test_compute_cost_metrics_zero_denominator_returns_none():
    result = cost.compute_cost_metrics([])

    assert result["total_cost_usd"] == 0.0
    assert result["total_tokens"] == 0
    assert result["cost_per_issue"] is None
    assert result["cost_per_resolution"] is None


def test_wasted_cost_always_none_with_fixed_reason_regardless_of_input():
    events = [
        _run_completed("run_1", {"cost_usd": 100.0, "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 1, "cache_write_tokens": 1}),
        _issue_started("run_1_p_1"),
        _issue_completed("run_1_p_1"),
    ]

    result = cost.compute_cost_metrics(events)

    assert result["wasted_cost"] is None
    assert result["wasted_cost_unavailable_reason"] == cost.RETRY_WASTE_UNAVAILABLE_REASON


from datetime import timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import events


def test_build_cost_report_all_time(tmp_path):
    events.emit(
        "run.completed", run_id="run_1", events_dir=tmp_path,
        data={"cost_usd": 2.0, "input_tokens": 10, "output_tokens": 5, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )
    events.emit("issue.started", run_id="run_1", issue_run_id="run_1_p_1", events_dir=tmp_path)
    events.emit("issue.completed", run_id="run_1", issue_run_id="run_1_p_1", events_dir=tmp_path)

    report = cost.build_cost_report(events_dir=tmp_path)

    assert report["cost"]["total_cost_usd"] == 2.0
    assert report["cost"]["cost_per_resolution"] == 2.0
    assert report["scope"] == {"since_date": None, "until_date": None}


def test_format_cost_report_headers_all_time_and_window():
    all_time_report = {"cost": cost.compute_cost_metrics([]), "scope": {"since_date": None, "until_date": None}}
    windowed_report = {"cost": cost.compute_cost_metrics([]), "scope": {"since_date": "2026-08-29", "until_date": "2026-09-04"}}

    assert "Loop Cost (all time)" in cost.format_cost_report(all_time_report)
    assert "Loop Cost (2026-08-29 to 2026-09-04)" in cost.format_cost_report(windowed_report)


def test_format_cost_report_renders_wasted_cost_na_with_reason():
    report = {"cost": cost.compute_cost_metrics([]), "scope": {"since_date": None, "until_date": None}}

    text = cost.format_cost_report(report)

    assert "N/A" in text
    assert cost.RETRY_WASTE_UNAVAILABLE_REASON in text


def test_cli_report_bare_invocation(tmp_path):
    events.emit(
        "run.completed", run_id="run_1", events_dir=tmp_path,
        data={"cost_usd": 3.25, "input_tokens": 1, "output_tokens": 1, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )

    result = _run_cli(["report", "--events-dir", str(tmp_path)])

    assert result.returncode == 0, result.stderr
    assert "Total AI cost" in result.stdout
    assert "$3.25" in result.stdout


def test_cli_report_days_flag(tmp_path):
    older_date = "2000-01-01"
    older_event = {
        "schema_version": 1, "event_id": "evt_old", "timestamp": "2000-01-01T00:00:00.000Z",
        "event_type": "run.completed", "run_id": "run_older", "issue_run_id": None,
        "project": None, "issue_iid": None,
        "data": {"cost_usd": 999.0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
    }
    (tmp_path / f"{older_date}.jsonl").write_text(json.dumps(older_event) + "\n")
    events.emit(
        "run.completed", run_id="run_today", events_dir=tmp_path,
        data={"cost_usd": 1.0, "input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cache_write_tokens": 0},
    )

    result = _run_cli(["report", "--days", "1", "--events-dir", str(tmp_path)])

    assert result.returncode == 0, result.stderr
    assert "$1.00" in result.stdout
    assert "$999.00" not in result.stdout


def test_cli_report_nonexistent_events_dir_fails_clearly(tmp_path):
    missing_dir = tmp_path / "does-not-exist"

    result = _run_cli(["report", "--events-dir", str(missing_dir)])

    assert result.returncode == 1
    assert "events-dir" in result.stderr.lower()


def test_cli_report_bad_days_value_fails_clearly(tmp_path):
    result = _run_cli(["report", "--days", "not-a-number", "--events-dir", str(tmp_path)])

    assert result.returncode == 1
    assert "days" in result.stderr.lower()
