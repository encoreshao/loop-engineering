import json
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import inbox_config  # noqa: E402
import inbox_triage_runner as runner  # noqa: E402

CATS = inbox_config.DEFAULT_CATEGORIES


@pytest.fixture(autouse=True)
def _no_real_ai_cli(sanitized_path):
    pass


def test_claude_command_disables_tools_and_mcp():
    cmd = runner._cli_command("claude")
    assert cmd[:2] == ["claude", "-p"]
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert cmd[cmd.index("--output-format") + 1] == "json"
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}
    assert not any("Bash" in part or "WebFetch" in part for part in cmd)


def test_codex_command_is_read_only_without_mcp_and_reads_stdin():
    cmd = runner._cli_command("codex")
    assert cmd[:2] == ["codex", "exec"]
    assert cmd[cmd.index("--sandbox") + 1] == "read-only"
    assert "mcp_servers={}" in cmd
    assert "tools.web_search=false" in cmd
    assert cmd[-1] == "-"


class _Run:
    def __init__(self, stdout="", returncode=0, exc=None):
        self.stdout, self.returncode, self.exc, self.calls = stdout, returncode, exc, []

    def __call__(self, cmd, **kwargs):
        self.calls.append((cmd, kwargs))
        if self.exc:
            raise self.exc
        return subprocess.CompletedProcess(cmd, self.returncode, self.stdout, "")


def test_invoke_claude_passes_prompt_on_stdin_and_parses_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": 0.02, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    log = tmp_path / "log.txt"
    out = runner.invoke_triage_agent("SECRET BODY TEXT", repo_root=tmp_path, unified_log_path=log)
    assert out == {"text": "[]", "cost_usd": 0.02}
    cmd, kwargs = fake.calls[0]
    assert kwargs["input"] == "SECRET BODY TEXT"
    assert "SECRET BODY TEXT" not in " ".join(cmd)
    assert "SECRET BODY TEXT" not in log.read_text()


def test_invoke_runs_in_a_fresh_temp_dir_not_the_repo(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    fake = _Run(json.dumps({"result": "[]", "total_cost_usd": None, "is_error": False}))
    monkeypatch.setattr(runner.subprocess, "run", fake)
    runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")
    cwd = fake.calls[0][1]["cwd"]
    assert cwd != str(tmp_path)
    assert not Path(cwd).is_relative_to(tmp_path)
    assert not Path(cwd).exists()  # cleaned up once invoke_triage_agent returns


def test_invoke_claude_is_error_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(runner.subprocess, "run", _Run(json.dumps({"result": "rate limited", "is_error": True})))
    with pytest.raises(runner.TriageFailed):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=tmp_path / "l")


def test_invoke_claude_unparseable_envelope_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    monkeypatch.setattr(runner.subprocess, "run", _Run("not json at all"))
    log = tmp_path / "log.txt"
    with pytest.raises(runner.TriageFailed, match="unparseable envelope"):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    assert "unparseable CLI envelope" in log.read_text()


def test_invoke_codex_returns_raw_stdout_and_no_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    monkeypatch.setattr(runner.subprocess, "run", _Run("[]"))
    log = tmp_path / "log.txt"
    out = runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    assert out == {"text": "[]", "cost_usd": None}
    assert "codex triage call ok" in log.read_text()


def test_invoke_timeout_logs_no_prompt_text(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "claude")
    exc = subprocess.TimeoutExpired(["claude"], 900)
    monkeypatch.setattr(runner.subprocess, "run", _Run(exc=exc))
    log = tmp_path / "log.txt"
    with pytest.raises(subprocess.TimeoutExpired):
        runner.invoke_triage_agent("SECRET PROMPT TEXT", repo_root=tmp_path, timeout_seconds=900, unified_log_path=log)
    text = log.read_text()
    assert "timed out after 900s" in text
    assert "SECRET PROMPT TEXT" not in text


def test_invoke_failure_logs_no_output_content(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.ai_cli_config, "get_selected_cli", lambda: "codex")
    exc = subprocess.CalledProcessError(1, ["codex"], output="LEAKED BODY", stderr="LEAKED BODY")
    monkeypatch.setattr(runner.subprocess, "run", _Run(exc=exc))
    log = tmp_path / "log.txt"
    with pytest.raises(subprocess.CalledProcessError):
        runner.invoke_triage_agent("p", repo_root=tmp_path, unified_log_path=log)
    text = log.read_text()
    assert "exited 1" in text and "LEAKED BODY" not in text


MSGS = [{"id": "m1", "from": "a@x.com"}]
GOOD = json.dumps([{"id": "m1", "category": "fyi", "reason": "r", "draft_body": None}])


def test_classify_retries_once_on_bad_json_then_succeeds():
    replies = iter([{"text": "garbage", "cost_usd": 0.01}, {"text": GOOD, "cost_usd": 0.02}])
    decisions, cost = runner.classify("p", MSGS, CATS, invoke=lambda prompt: next(replies))
    assert decisions[0]["category"] == "fyi"
    assert cost == pytest.approx(0.03)


def test_classify_fails_after_two_bad_replies():
    with pytest.raises(runner.TriageFailed, match="no JSON array"):
        runner.classify("p", MSGS, CATS, invoke=lambda prompt: {"text": "nope", "cost_usd": None})


def test_classify_retries_on_subprocess_failure():
    calls = []

    def invoke(prompt):
        calls.append(1)
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(["claude"], 900)
        return {"text": GOOD, "cost_usd": None}
    decisions, cost = runner.classify("p", MSGS, CATS, invoke=invoke)
    assert len(calls) == 2 and cost is None


def test_instructions_doc_exists_and_states_json_contract():
    text = runner.INSTRUCTIONS_PATH.read_text()
    for needle in ('"id"', '"category"', '"reason"', '"draft_body"', "JSON array", "never"):
        assert needle in text
