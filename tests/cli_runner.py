"""Run a loop CLI as a black-box subprocess, as either the Python script or
the Rust `loopx` subcommand that replaces it.

Set `LOOPX_BIN` to the path of a built `loopx` binary and every test that goes
through `run_cli` exercises the Rust version; leave it unset and the same test
runs the legacy `bin/*.py` script. This is how the migration (see
docs/执行计划.md, P1) keeps one suite green on both sides: a script is only
deleted once its contract tests pass against `loopx`.
"""
import os
import subprocess
import sys
from pathlib import Path

BIN_DIR = Path(__file__).resolve().parent.parent / "bin"

# Scripts that live outside this repo (the gitlab-config skill). Resolved from
# the real home at import time, so a scratch HOME in a test's env can't hide it.
EXTERNAL_SCRIPTS = {
    "gitlab_api.py": Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts" / "gitlab_api.py",
}

# legacy script -> loopx subcommand words. Grow this as scripts migrate.
SUBCOMMANDS = {
    "events.py": ["events"],
    "loops_config.py": ["loops"],
    "ai_cli_config.py": ["ai-cli"],
    "loop_cli.py": ["loop"],  # `loopx loop <sub>`; `ledger` is also top-level `loopx ledger`
    "metrics.py": ["metrics"],
    "update_runner.py": ["update"],
    "loop_config.py": ["config"],
    "list_assigned_issues.py": ["issues", "list"],
    "track_new_comments.py": ["issues", "comments"],
    "project_memory.py": ["memory"],
    "topic_config.py": ["topic", "config"],
    "topic_seen.py": ["topic", "seen"],
    "slack_notify.py": ["notify"],
    "gitlab_api.py": ["gitlab"],
    "new_worktree.sh": ["worktree", "new"],
    "open_merge_request.sh": ["mr", "open"],
    "risk.py": ["risk"],
    "cost.py": ["cost"],
    "learning.py": ["learning"],
    "memory_store.py": ["memory-store"],
    "notify.py": ["notify-loop"],
    "inbox_triage_runner.py": ["inbox", "run"],
}


def loopx_bin():
    """The binary under test, or None to test the Python scripts."""
    return os.environ.get("LOOPX_BIN") or None


def command_for(script, *args):
    binary = loopx_bin()
    if binary:
        return [binary, *SUBCOMMANDS[script], *args]
    if script in EXTERNAL_SCRIPTS:
        return [sys.executable, str(EXTERNAL_SCRIPTS[script]), *args]
    if script.endswith(".sh"):
        return ["bash", str(BIN_DIR / "scripts" / script), *args]
    return [sys.executable, str(BIN_DIR / script), *args]


def run_cli(script, *args, env=None, input=None, cwd=None, check=False):
    """Run `script` (legacy filename) with args; returns CompletedProcess (text mode).

    `check=True` raises CalledProcessError on a non-zero exit, like subprocess.run."""
    full_env = {**os.environ, **(env or {})}
    return subprocess.run(
        command_for(script, *args),
        env=full_env, input=input, cwd=cwd, check=check,
        capture_output=True, text=True,
    )
