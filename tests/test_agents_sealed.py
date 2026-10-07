# tests/test_agents_sealed.py
import json, subprocess, sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agents import sealed


def test_sealed_command_has_no_tools_and_no_mcp():
    cmd = sealed.sealed_command()
    assert cmd[cmd.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in cmd
    assert json.loads(cmd[cmd.index("--mcp-config") + 1]) == {"mcpServers": {}}
    assert "--no-session-persistence" in cmd
    assert json.loads(cmd[cmd.index("--settings") + 1]) == {"disableAllHooks": True}


def fake_run(stdout, returncode=0):
    def run(cmd, input, capture_output, text, timeout, check, cwd):
        assert "SECRET-BODY" not in " ".join(cmd)       # prompt only on stdin
        assert Path(cwd).name.startswith("loop-sealed-")
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")
    return run


def test_sealed_call_parses_envelope():
    out = sealed.sealed_call("SECRET-BODY", 30, runner=fake_run(json.dumps({"result": "ok", "total_cost_usd": 0.02})), cli_fn=lambda: "claude")
    assert out["text"] == "ok" and out["cost_usd"] == 0.02


def test_sealed_call_refuses_codex():
    with pytest.raises(sealed.SealedCallFailed):
        sealed.sealed_call("x", 30, runner=fake_run("{}"), cli_fn=lambda: "codex")


def test_sealed_call_is_error():
    with pytest.raises(sealed.SealedCallFailed):
        sealed.sealed_call("x", 30, runner=fake_run(json.dumps({"is_error": True})), cli_fn=lambda: "claude")


def test_sealed_call_log_has_no_content():
    logs = []
    sealed.sealed_call("SECRET-BODY", 30, log=logs.append,
                       runner=fake_run(json.dumps({"result": "SECRET-ANSWER"})), cli_fn=lambda: "claude")
    assert logs and all("SECRET" not in l for l in logs)


def test_sealed_command_includes_budget():
    cmd = sealed.sealed_command(max_budget_usd=0.5)
    assert cmd[cmd.index("--max-budget-usd") + 1] == "0.50"


def test_sealed_command_no_budget_by_default():
    assert "--max-budget-usd" not in sealed.sealed_command()


def test_sealed_call_passes_budget_to_command(monkeypatch):
    seen = []
    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "x"}), stderr="")
    sealed.sealed_call("p", 5, runner=run, cli_fn=lambda: "claude", max_budget_usd=1.234)
    assert seen[0][seen[0].index("--max-budget-usd") + 1] == "1.23"
    seen.clear()
    sealed.sealed_call("p", 5, runner=run, cli_fn=lambda: "claude")
    assert "--max-budget-usd" not in seen[0]


def test_sealed_call_passes_budget_into_command_fn_once():
    got = []
    def command_fn(max_budget_usd=None):
        got.append(max_budget_usd)
        return sealed.sealed_command(max_budget_usd=max_budget_usd)
    seen = []
    def run(cmd, **kw):
        seen.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps({"result": "x"}), stderr="")
    sealed.sealed_call("p", 5, runner=run, cli_fn=lambda: "claude", command_fn=command_fn, max_budget_usd=0.5)
    assert got == [0.5] and seen[0].count("--max-budget-usd") == 1
