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
