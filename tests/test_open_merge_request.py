import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "bin" / "scripts" / "open_merge_request.sh"


def test_dry_run_prints_expected_git_push_command():
    env = {**os.environ, "DRY_RUN": "1"}

    result = subprocess.run(
        ["bash", str(SCRIPT), "/tmp/some-repo", "loop/issue-42", "staging", "Fix #42: sample title"],
        check=True, capture_output=True, text=True, env=env,
    )

    assert result.stdout.splitlines() == [
        "git", "-C", "/tmp/some-repo", "push", "origin", "loop/issue-42",
        "-o", "merge_request.create",
        "-o", "merge_request.target=staging",
        "-o", "merge_request.title=Fix #42: sample title",
        "-o", "merge_request.remove_source_branch",
    ]


def test_missing_arguments_exits_nonzero():
    result = subprocess.run(["bash", str(SCRIPT), "/tmp/some-repo"], capture_output=True, text=True)

    assert result.returncode != 0


def test_refuses_to_push_non_loop_branch():
    result = subprocess.run(
        ["bash", str(SCRIPT), "/tmp/some-repo", "staging", "staging", "not a loop branch"],
        capture_output=True, text=True,
    )

    assert result.returncode != 0
    assert "refusing" in result.stderr.lower()
