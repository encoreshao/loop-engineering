import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agents.claude import ClaudeAgent


def _write_fake_claude(bin_dir, output_json=None, raw_output=None, exit_code=0):
    script_path = bin_dir / "claude"
    if output_json is not None:
        body = f"print({json.dumps(json.dumps(output_json))})"
    else:
        body = f"print({json.dumps(raw_output or '')}, end='')"
    script_path.write_text(f"#!/usr/bin/env python3\nimport sys\n{body}\nsys.exit({exit_code})\n")
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def test_claude_agent_extracts_json_result_and_cost(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir, output_json={
        "result": "Fixed the issue.",
        "total_cost_usd": 0.42,
        "usage": {"input_tokens": 100, "output_tokens": 50},
        "modelUsage": {"claude-sonnet": {}},
    })
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = ClaudeAgent().run(
        "do the thing", {}, cwd=tmp_path, timeout_seconds=30, output_format="json",
    )

    assert result.status == "success"
    assert result.output == "Fixed the issue."
    assert result.exit_code == 0
    assert result.input_tokens == 100
    assert result.output_tokens == 50
    assert result.estimated_cost_usd == 0.42


def test_claude_agent_text_format_has_no_cost(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir, raw_output="plain text result")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = ClaudeAgent().run(
        "do the thing", {}, cwd=tmp_path, timeout_seconds=30, output_format="text",
    )

    assert result.status == "success"
    assert result.output == "plain text result"
    assert result.estimated_cost_usd is None


def test_claude_agent_reports_failed_status_on_nonzero_exit(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_claude(bin_dir, raw_output="boom", exit_code=1)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = ClaudeAgent().run(
        "do the thing", {}, cwd=tmp_path, timeout_seconds=30, output_format="text",
    )

    assert result.status == "failed"
    assert result.exit_code == 1
    assert "boom" in result.output


def test_claude_agent_passes_add_dirs_and_tool_flags(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    captured_argv_path = tmp_path / "argv.json"
    script_path = bin_dir / "claude"
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({json.dumps(str(captured_argv_path))!s}, 'w'))\n"
        "print(json.dumps({'result': 'ok', 'total_cost_usd': 0.0, 'usage': {}, 'modelUsage': {}}))\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    ClaudeAgent().run(
        "a prompt", {}, cwd=tmp_path, timeout_seconds=30,
        allowed_tools="Read Edit", disallowed_tools="Bash(git merge*)",
        add_dirs=[str(tmp_path / "repo"), str(tmp_path / "worktree")],
        output_format="json",
    )

    argv = json.loads(captured_argv_path.read_text())
    assert "--add-dir" in argv
    assert str(tmp_path / "repo") in argv
    assert str(tmp_path / "worktree") in argv
    assert "--allowedTools" in argv
    assert "Read Edit" in argv
    assert "--disallowedTools" in argv
    assert "Bash(git merge*)" in argv
    assert argv[-1] == "a prompt"
