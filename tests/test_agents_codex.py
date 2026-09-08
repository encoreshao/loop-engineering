import json
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
from agents.codex import CodexAgent


def _write_fake_codex(bin_dir, output=None, exit_code=0):
    script_path = bin_dir / "codex"
    script_path.write_text(
        f"#!/usr/bin/env python3\nimport sys\nprint({json.dumps(output or '')}, end='')\nsys.exit({exit_code})\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)


def test_codex_agent_reports_success_with_no_cost(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_codex(bin_dir, output="did the thing")
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = CodexAgent().run("do the thing", {}, cwd=tmp_path, timeout_seconds=30)

    assert result.status == "success"
    assert result.output == "did the thing"
    assert result.estimated_cost_usd is None


def test_codex_agent_reports_failed_status_on_nonzero_exit(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    _write_fake_codex(bin_dir, output="boom", exit_code=1)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = CodexAgent().run("do the thing", {}, cwd=tmp_path, timeout_seconds=30)

    assert result.status == "failed"
    assert result.exit_code == 1


def test_codex_agent_passes_writable_roots_when_add_dirs_given(tmp_path, monkeypatch):
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir()
    captured_argv_path = tmp_path / "argv.json"
    script_path = bin_dir / "codex"
    script_path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        f"json.dump(sys.argv[1:], open({json.dumps(str(captured_argv_path))!s}, 'w'))\n"
        "print('ok', end='')\n"
    )
    script_path.chmod(script_path.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    CodexAgent().run(
        "a prompt", {}, cwd=tmp_path, timeout_seconds=30,
        add_dirs=[str(tmp_path / "repo"), str(tmp_path / "worktree")],
    )

    argv = json.loads(captured_argv_path.read_text())
    assert argv[:3] == ["exec", "--sandbox", "workspace-write"]
    writable_roots_flag = argv[argv.index("-c") + 1]
    assert str(tmp_path / "repo") in writable_roots_flag
    assert str(tmp_path / "worktree") in writable_roots_flag
    assert argv[-1] == "a prompt"


def test_get_agent_round_trips_both_providers():
    from agents.base import get_agent

    assert get_agent(provider="claude").run.__self__.__class__.__name__ == "ClaudeAgent"
    assert get_agent(provider="codex").run.__self__.__class__.__name__ == "CodexAgent"
