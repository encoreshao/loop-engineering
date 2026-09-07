import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bin"))
import gitlab_loop_runner as glr

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "build_run_prompt.sh"
REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_batch_issue_mode_skips_step_1_and_forbids_end_of_run():
    result = run_script("--batch-issue", "harbor", "482")
    assert result.returncode == 0
    prompt = result.stdout.strip()
    assert "Follow LOOPX_INSTRUCTIONS.md" in prompt
    assert "skip Step 1" in prompt
    assert "project alias 'harbor'" in prompt
    assert "issue IID 482" in prompt
    # The whole point of this mode: this issue is one of N in a batch, so
    # the guaranteed once-per-run digest/daily-review must NOT run here.
    assert "End of run" in prompt
    assert "--batch-end-of-run" in prompt
    assert "no user available to answer questions" in prompt
    # It must not be mistaken for the dashboard's on-demand mode, which
    # does its own full End of run.
    assert "on-demand single-issue run" not in prompt


def test_batch_end_of_run_mode_reconstructs_from_the_event_log():
    result = run_script("--batch-end-of-run")
    assert result.returncode == 0
    prompt = result.stdout.strip()
    assert "Follow LOOPX_INSTRUCTIONS.md" in prompt
    assert "skip Step 1" in prompt
    assert "Step 2" in prompt
    assert "End of run" in prompt
    assert "outputs/events/" in prompt
    assert "$LOOP_RUN_ID" in prompt
    # The specific event types/fields the reconstruction depends on.
    for expected in (
        "issue.completed", "issue.escalated", "issue.started",
        "data.action", "mr_url", "data.reason",
        "needs_clarification", "verification_failed", "worktree_creation_failed",
    ):
        assert expected in prompt, f"expected {expected!r} in the wrap-up prompt"
    # All seven daily-review.md sections must be named explicitly.
    for section in (
        "Summary", "Issues checked", "New comments found", "MRs opened",
        "Answered directly", "Escalations", "No-ops",
    ):
        assert section in prompt, f"expected section {section!r} in the wrap-up prompt"


def test_one_arg_is_rejected():
    result = run_script("harbor")
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_batch_issue_flag_with_wrong_arg_count_is_rejected():
    assert run_script("--batch-issue", "harbor").returncode == 1
    assert run_script("--batch-issue").returncode == 1
    assert run_script("--batch-end-of-run", "harbor").returncode == 1


def test_three_args_is_rejected():
    result = run_script("harbor", "482", "extra")
    assert result.returncode == 1
    assert "Usage" in result.stderr


def test_run_loop_sh_forwards_its_args_to_gitlab_loop_runner():
    # build_run_prompt.sh is no longer invoked directly from run-loop.sh -
    # that responsibility moved into gitlab_loop_runner.py's build_prompt()
    # (see the test below), which run-loop.sh now delegates to, forwarding
    # its own "$@" (the optional `<alias> <issue_iid>` pair) unchanged.
    run_loop_sh = Path(__file__).resolve().parent.parent / "run-loop.sh"
    content = run_loop_sh.read_text()
    assert 'RUNNER_ARGS=("$RUN_ID" "$@")' in content
    assert "gitlab_loop_runner.py" in content


def test_gitlab_loop_runner_build_prompt_forwards_args_to_build_run_prompt():
    prompt = glr.build_prompt("harbor", "482", repo_root=REPO_ROOT)
    assert "Follow LOOPX_INSTRUCTIONS.md" in prompt
    assert "project alias 'harbor'" in prompt
    assert "issue IID 482" in prompt
    assert "on-demand single-issue run" in prompt
