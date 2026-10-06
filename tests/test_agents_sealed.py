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
