import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "build_run_prompt.sh"


def run_script(*args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True, text=True,
    )


def test_no_args_returns_the_scheduled_run_prompt():
    result = run_script()
    assert result.returncode == 0
    prompt = result.stdout.strip()
    assert "Follow LOOPX_INSTRUCTIONS.md" in prompt
    assert "scheduled headless run" in prompt
    assert "no user available to answer questions" in prompt
    # This is the all-assigned-issues prompt, so it must not mention a
    # specific issue.
    assert "Process exactly one issue" not in prompt


def test_two_args_returns_the_single_issue_prompt():
    result = run_script("harbor", "482")
    assert result.returncode == 0
    prompt = result.stdout.strip()
    assert "Follow LOOPX_INSTRUCTIONS.md" in prompt
    assert "skip Step 1" in prompt
    assert "project alias 'harbor'" in prompt
    assert "issue IID 482" in prompt
    assert "regardless of who it is assigned to" in prompt
    assert "on-demand single-issue run" in prompt
    assert "no user available to answer questions" in prompt


def test_one_arg_is_rejected():
    result = run_script("harbor")
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_three_args_is_rejected():
    result = run_script("harbor", "482", "extra")
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_run_loop_sh_forwards_its_args_to_build_run_prompt():
    run_loop_sh = Path(__file__).resolve().parent.parent / "run-loop.sh"
    content = run_loop_sh.read_text()
    assert 'bash "$LOOP_DIR/bin/scripts/build_run_prompt.sh" "$@"' in content
