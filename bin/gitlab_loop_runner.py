#!/usr/bin/env python3
"""Wires the real GitLab issue loop to LoopRuntime - see
docs/superpowers/specs/2026-09-07-gitlab-loop-runtime-wiring-design.md.
Ported 1:1 from run-loop.sh's former inline PROMPT/AI_CLI/CLI_CMD/cost-
extraction logic - the agent still self-verifies exactly as before,
LoopRuntime only tracks/bounds each issue's single existing invocation.

Three agent-invocation shapes live here, all sharing one subprocess
boundary (`_invoke_cli_with_prompt`) and differing only in their prompt:

- `invoke_issue_agent` - the dashboard's on-demand single-issue run
  (`run-loop.sh <alias> <iid>`). Uses build_run_prompt.sh's two-arg mode,
  which does its own full "End of run": that run IS one issue, so its
  digest/daily-review is the whole run's report. Deliberately untouched.
- `invoke_batch_issue_agent` - one issue inside the scheduled batch.
  Uses `--batch-issue`, which forbids "End of run" per issue.
- `invoke_batch_end_of_run_agent` - the scheduled batch's single wrap-up.
  Uses `--batch-end-of-run`, which does ONLY "End of run", reconstructing
  the run's outcomes from the event log. `run_all_issues` calls this
  unconditionally - including on a morning with zero assigned issues,
  which is what keeps LOOPX_INSTRUCTIONS.md's "a quiet morning is still
  reported" guarantee true now that no single session spans the batch."""
import json
import os
import re
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import ai_cli_config
import cost as cost_module
import events as events_module
import connectors_config
import issue_tracking_config
import learning
import loop_config
import slack_notify
from list_assigned_issues import list_assigned_issues
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
import loop_serialize
from loop_serialize import write_result
from loop_state import LoopState
from loop_verifiers import build_verifiers

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEFINITION_PATH = REPO_ROOT / "loops" / "gitlab-issue" / "loop.yaml"

# How much of a failed invocation's stderr/stdout goes into an event's
# `data` - enough to identify the failure, not enough to bloat the JSONL
# log with a full traceback (the full text goes to the unified log).
_STDERR_EXCERPT_CHARS = 800

# Where `_run_one_issue` stashes an issue's raw, un-coerced agent cost on
# its LoopResult, and the sentinel that means "this LoopResult never went
# through `_run_one_issue`". See `aggregate_cost_usd` for why the budget
# figure alone is not enough.
_AGENT_COST_ATTR = loop_serialize.AGENT_COST_ATTR
_UNSET = object()

# Where `_run_one_issue` stashes the gate-mode finalize outcome (same plain-
# attribute trick as _AGENT_COST_ATTR, so result.json keeps its shape). A
# loop escalation is also written into final_state/stop_reason, and
# `_alert_on_incomplete_results` reads this to skip already-notified issues.
_GATE_OUTCOME_ATTR = "gate_outcome"

# Where `_run_one_issue` stashes an issue's summed token/cache usage (a
# dict with the four fields below, or None if the issue never got real
# usage data - the Codex path, or a failed Claude cost extraction). Same
# plain-attribute trick as _AGENT_COST_ATTR and for the same reason:
# dataclasses.asdict() ignores it, so result.json's shape is unchanged.
_AGENT_USAGE_ATTR = "agent_usage_tokens"
_USAGE_TOKEN_FIELDS = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")

# The actually-enforced permission list, moved here verbatim from
# run-loop.sh's former ALLOWED_TOOLS/DISALLOWED_TOOLS shell variables.
# LOOPX_INSTRUCTIONS.md's "Tool permissions policy" section describes the
# same policy in prose; the two must be kept in sync whenever either
# changes. Everything below is *why* these strings look the way they do -
# it was load-bearing commentary in run-loop.sh and stays load-bearing
# here.
#
# Only the loop directory and the worktree root are exposed to
# Read/Edit/Write (the two --add-dir arguments in `_cli_command`). The
# projects' primary checkouts (`local_path` in projects.json) are
# deliberately NOT add-dir'd: every file edit is supposed to happen inside
# a per-issue worktree, so leaving them out mechanically enforces "never
# edit files in <local_path> directly" instead of trusting the agent to
# follow prose.
#
# --add-dir only scopes the Read/Edit/Write/Glob/Grep tools; it does not
# filter paths named inside a Bash command. So a Bash command may still
# *name* <local_path> where the allow patterns below permit it - e.g.
# `bash <loop_dir>/bin/scripts/open_merge_request.sh <local_path>
# loop/issue-N ...`, which passes the checkout as an argument and runs
# `git -C` against it inside the script. (Note that a *direct*
# `git -C <local_path> ...` is NOT an example of this: the git patterns
# below are literal-prefix matches on `git status`/`git diff`/`git add`/
# `git commit`/`git push origin loop/issue-`, none of which a string
# starting `git -C` matches, so it is denied.)
#
# git is enumerated per-subcommand rather than `Bash(git *)` so that
# "never merge, never push to the target branch" is enforced by the
# harness, not just by prose. git fetch/merge/worktree-add are not listed
# because they only run inside new_worktree.sh's own execution, which is
# already covered by allowing the outer `bash bin/scripts/new_worktree.sh`
# invocation.
#
# The bin/ scripts are listed in both relative and absolute form: the
# agent starts in the loop directory but spends most of the run cd'd into
# a worktree. bin/'s contents are split by kind (see CLAUDE.md): the loop's
# own Python helpers directly in bin/, the dashboard web server in bin/web/,
# and LoopKit plugins in bin/loop_plugins/ each get a glob pattern (a glob's
# `*` doesn't cross a `/`, so each directory needs its own). The shell
# scripts in bin/scripts/ the agent may run are enumerated in _AGENT_SCRIPTS
# below instead, so one (open_merge_request.sh) can be withheld in gate mode.


_AGENT_SCRIPTS = ("new_worktree.sh", "open_merge_request.sh")


# Offline (golden eval) runs get these bin/ helpers by name instead of the
# bin/ globs: the globs would also reach notify.py (which falls back to the
# real Slack webhook), loopkit.py, the *_runner.py entry points and
# list_assigned_issues.py. They are exactly the helpers the --issue-file
# prompt leaves the agent; project_memory.py is read-only (`get`).
_OFFLINE_BIN_SCRIPTS = ("loop_config.py", "events.py", "risk.py", "memory_store.py", "project_memory.py get")
# The golden fixtures are Python repos: their test_cmd must be runnable.
_OFFLINE_CHECK_COMMANDS = "Bash(python3 -m pytest*)"


def _allowed_tools(repo_root, gate=False, offline=False):
    """`gate=True` is the harness-gate variant: the agent may neither push
    nor open the MR itself (the runner does both after external
    verification passes) but may write its handoff file. Scripts are listed
    explicitly rather than by glob so open_merge_request.sh can be left out.
    `offline=True` (golden eval) swaps the bin/ globs and GitLab helpers for
    `_OFFLINE_BIN_SCRIPTS` and allows the fixtures' pytest."""
    scripts = [s for s in _AGENT_SCRIPTS if not (gate and s == "open_merge_request.sh")]
    script_patterns = " ".join(
        f"Bash(bash {prefix}bin/scripts/{s}*)" for prefix in ("", f"{repo_root}/") for s in scripts
    )
    push = "" if gate else "Bash(git push origin loop/issue-*) "
    handoff = f" Write({repo_root}/outputs/handoffs/**)" if gate else ""
    if offline:
        helpers = " ".join(
            f"Bash(python3 {prefix}bin/{s}*)" for prefix in ("", f"{repo_root}/") for s in _OFFLINE_BIN_SCRIPTS
        )
        return (
            "Read Edit Write "
            f"Bash(git status*) Bash(git diff*) Bash(git add*) Bash(git commit*) {push}"
            "Bash(cd *) "
            f"{_OFFLINE_CHECK_COMMANDS} "
            f"{helpers} "
            f"{script_patterns}{handoff}"
        )
    return (
        "Read Edit Write "
        f"Bash(git status*) Bash(git diff*) Bash(git add*) Bash(git commit*) {push}"
        "Bash(cd *) "
        "Bash(RAILS_ENV=test bundle exec rspec*) Bash(bundle exec rspec*) Bash(bundle exec rubocop*) "
        "Bash(bundle check*) Bash(bundle install*) Bash(RAILS_ENV=test bundle exec rake db:test:prepare*) "
        "Bash(npm run test*) Bash(npm run lint*) Bash(npm ci*) Bash(yarn install*) "
        "Bash(python3 *gitlab_api.py*) Bash(python3 *gitlab_cache.py*) "
        "Bash(python3 bin/*.py*) Bash(python3 bin/web/*.py*) Bash(python3 bin/loop_plugins/*.py*) "
        f"Bash(python3 {repo_root}/bin/*.py*) Bash(python3 {repo_root}/bin/web/*.py*) "
        f"Bash(python3 {repo_root}/bin/loop_plugins/*.py*) "
        f"{script_patterns}{handoff}"
    )


# Defense in depth: even if an allow pattern above were ever loosened by
# accident, these can never run.
_DISALLOWED_TOOLS = (
    "Bash(git merge*) Bash(git push --force*) Bash(git push -f*) Bash(git checkout*) "
    "Bash(git reset*) Bash(git clean*) Read(**/.env*) Read(**/*.key) Read(**/id_rsa*)"
)


_GATE_DISALLOWED_TOOLS = "Bash(bash *open_merge_request.sh*) Bash(git push origin loop/issue-*)"

# Offline (golden eval) runs work on a synthetic fixture: nothing may reach
# GitLab, Slack or the live dashboard, whatever the prompt says.
_OFFLINE_DISALLOWED_TOOLS = (
    "Bash(python3 *gitlab_api.py*) Bash(python3 *gitlab_cache.py*) Bash(python3 *track_new_comments.py*) "
    "Bash(python3 *slack_notify.py*) Bash(python3 *dashboard_server.py*)"
)

GATE_OVERRIDE = """## Harness gate is ON for this run \u2014 this overrides steps 9 and 10

Do NOT run open_merge_request.sh and do NOT push: skip step 9. The loop re-runs the project's checks itself and opens the merge request only if they pass.

For a fix, once it is committed on loop/issue-<iid> and your own checks pass, do step 10 with these changes: annotate loop_last_action as "fix_handed_off" (not "mr_opened: ..."), still run track_new_comments.py mark-seen, still record a learning if there is one (memory_store.py add and its memory.created emit), and still cd back to the loop directory. Do NOT send the "Finished" Slack message and do NOT emit issue.completed \u2014 the loop does both after it opens the merge request. Then write this JSON to <handoff_path> (also in $LOOP_HANDOFF_PATH) and stop:

{"action": "fix", "branch": "loop/issue-<iid>", "target_branch": "<target>", "title": "Fix #<iid>: <short title>", "summary": "<2-4 sentences on what you changed and why; the loop posts it on the issue next to the merge request link>"}

Every issue in a gated run must end with a handoff file at that path, written after finishing the matching path as usual:
- answered directly: {"action": "answer"}
- escalated (needs clarification, or your own verification failed): {"action": "escalate"}
- Wait for reviewer, or nothing to act on this run: {"action": "wait_for_review"}
A missing or malformed handoff is escalated to a human."""


def handoff_path(run_id, alias, issue_iid, repo_root=None):
    if repo_root is None:
        repo_root = REPO_ROOT
    return Path(repo_root) / "outputs" / "handoffs" / run_id / f"{alias}-{issue_iid}.json"


def _gate_prompt_and_env(prompt, alias, issue_iid, repo_root, run_id):
    """Gate-mode additions: the override section appended last and the
    LOOP_HANDOFF_PATH env var. `run_id` falls back to $LOOP_RUN_ID; with
    neither there is nowhere to put the handoff, so fail loudly."""
    run_id = run_id or os.environ.get("LOOP_RUN_ID")
    if not run_id:
        raise ValueError("gate mode needs a run_id (argument or LOOP_RUN_ID) to locate the handoff file")
    path = handoff_path(run_id, alias, issue_iid, repo_root=repo_root)
    # The path is stated literally: the agent's Write tool needs one and no
    # allowlisted command prints environment variables.
    override = GATE_OVERRIDE.replace("<handoff_path>", str(path))
    return f"{prompt}\n\n{override}", {"LOOP_HANDOFF_PATH": str(path)}


def _run_build_run_prompt(script_args, repo_root):
    if repo_root is None:
        repo_root = REPO_ROOT
    script = Path(repo_root) / "bin" / "scripts" / "build_run_prompt.sh"
    result = subprocess.run(
        ["bash", str(script), *script_args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def build_prompt(alias=None, issue_iid=None, repo_root=None):
    """build_run_prompt.sh's legacy 0-arg (whole-batch) and 2-arg
    (dashboard on-demand single-issue) modes. Only the 2-arg form is
    reached in practice - `run_single_issue` is its one caller."""
    args = [] if alias is None else [alias, str(issue_iid)]
    return _run_build_run_prompt(args, repo_root)


def build_batch_issue_prompt(alias, issue_iid, repo_root=None):
    """One issue inside the scheduled batch - same per-issue procedure as
    `build_prompt`'s 2-arg mode, but explicitly WITHOUT "End of run"."""
    return _run_build_run_prompt(["--batch-issue", alias, str(issue_iid)], repo_root)


def build_issue_file_prompt(alias, issue_iid, issue_file, repo_root=None):
    """The golden eval suite's offline single-issue prompt: the issue's
    title/body come from `issue_file` instead of GitLab."""
    return _run_build_run_prompt(["--issue-file", alias, str(issue_iid), str(issue_file)], repo_root)


def build_batch_end_of_run_prompt(repo_root=None):
    """The scheduled batch's wrap-up: "End of run" only, reconstructed
    from this run's own events."""
    return _run_build_run_prompt(["--batch-end-of-run"], repo_root)


def _cli_command(ai_cli, prompt, repo_root, worktree_root, gate=False, max_budget_usd=None, offline=False):
    if ai_cli == "codex":
        # `codex exec` (unlike top-level `codex`) has no --ask-for-approval
        # and no --add-dir at all - both are rejected outright with
        # "unexpected argument". It is already non-interactive, so
        # `-c approval_policy=never` is kept only for explicitness. The two
        # --add-dir roots become a writable_roots override instead, and
        # workspace-write has no network access by default, so
        # network_access=true is added too - this loop needs `git push` and
        # GitLab API calls to work. Codex's -c overrides are far coarser
        # than Claude's per-command allow/deny lists: see
        # LOOPX_INSTRUCTIONS.md's "Tool permissions policy" for what that
        # means for this loop's guardrails when Codex is selected.
        #
        # separators=(",", ":") so this matches run-loop.sh's former
        # bash-built string byte-for-byte (no spaces after , or :).
        writable_roots = json.dumps([str(repo_root), str(worktree_root)], separators=(",", ":"))
        return [
            "codex", "exec", "--sandbox", "workspace-write",
            "-c", "approval_policy=never",
            "-c", f"sandbox_workspace_write.writable_roots={writable_roots}",
            "-c", "sandbox_workspace_write.network_access=true",
            prompt,
        ]
    return [
        "claude", "-p",
        "--add-dir", str(repo_root), "--add-dir", str(worktree_root),
        "--permission-mode", "acceptEdits",
        "--allowedTools", _allowed_tools(repo_root, gate=gate, offline=offline),
        "--disallowedTools", " ".join(
            [_DISALLOWED_TOOLS]
            + ([_GATE_DISALLOWED_TOOLS] if gate else [])
            + ([_OFFLINE_DISALLOWED_TOOLS] if offline else [])
        ),
        *cost_module.budget_args(max_budget_usd),
        "--output-format", "json",
        prompt,
    ]


def _append_unified_log(text, repo_root=None, unified_log_path=None):
    if unified_log_path is None:
        if repo_root is None:
            repo_root = REPO_ROOT
        unified_log_path = Path(repo_root) / "logs" / "loop-engineering.log"
    unified_log_path = Path(unified_log_path)
    unified_log_path.parent.mkdir(parents=True, exist_ok=True)
    with open(unified_log_path, "a") as f:
        f.write(text if text.endswith("\n") else text + "\n")


def _emit_best_effort(event_type, run_id=None, issue_run_id=None, project=None,
                      issue_iid=None, data=None, events_dir=None):
    """Every emit in this module is best-effort, matching run-loop.sh's own
    `|| true` philosophy - observability must never take down a run."""
    if run_id is None:
        run_id = os.environ.get("LOOP_RUN_ID")
    if not run_id:
        return None
    try:
        return events_module.emit(
            event_type, run_id, issue_run_id=issue_run_id, project=project,
            issue_iid=issue_iid, data=data, events_dir=events_dir,
        )
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"gitlab_loop_runner: emitting {event_type} failed: {exc}", file=sys.stderr)
        return None


def _notify_slack_best_effort(message, notification_key=None, bundle=None):
    """A scheduled run has nobody watching it. run-loop.sh's ERR trap used
    to be the one failure alert this system had, but per-issue failures are
    now contained by LoopRuntime and never reach that trap - so failures
    have to announce themselves from here instead. `notification_key`, if
    given, looks up a saved Block Kit template bound to it (Settings ->
    Notifications) and sends its blocks alongside the plain-text message;
    with no bound template (the default on a fresh install), this sends
    exactly what it always has. `bundle` routes to that Slack bundle's
    webhook (None = the default webhook). If Slack rejects a bound template's blocks
    (e.g. malformed JSON from a hand-edited or buggy template), retries
    once with blocks=None so the plain-text alert still has a chance -
    losing the alert entirely would defeat the whole point of this
    function."""
    blocks = slack_notify.resolve_blocks(notification_key, message) if notification_key else None
    try:
        slack_notify.post_message(message, bundle=bundle, blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001 - an alert failing must not cascade
        if blocks:
            try:
                slack_notify.post_message(message, bundle=bundle)
                return True
            except Exception as retry_exc:  # noqa: BLE001 - same reasoning
                print(f"gitlab_loop_runner: Slack notification failed even without blocks: {retry_exc}", file=sys.stderr)
                return False
        print(f"gitlab_loop_runner: Slack notification failed: {exc}", file=sys.stderr)
        return False


def _exception_output(exc):
    """The stderr (preferred) or stdout an exception from `subprocess.run`
    carries. CalledProcessError always has both; TimeoutExpired may have
    either as None, and either may be bytes if text mode was off."""
    for value in (getattr(exc, "stderr", None), getattr(exc, "output", None)):
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return value
    return ""


class AgentCallError(Exception):
    """A CLI call that exited 0 but returned an error envelope. `cost_usd`
    is what it spent, when the envelope said."""

    def __init__(self, message, cost_usd=None):
        super().__init__(message)
        self.cost_usd = cost_usd


def _failed_call_cost(envelope, budget_stop, cap):
    """Spend of a failed call: the envelope's figure; the cap we passed only
    when the CLI stopped on that cap without reporting one; else None
    (unknown - never booked as spend)."""
    cost = envelope.get("total_cost_usd") if envelope else None
    if cost is None and budget_stop:
        return cap
    return cost


def _parse_envelope(stdout):
    """The Claude CLI's JSON result object, or None when absent/unparseable."""
    if isinstance(stdout, bytes):
        stdout = stdout.decode("utf-8", "replace")
    if not stdout:
        return None
    try:
        parsed = json.loads(stdout)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _invoke_cli_with_prompt(prompt, repo_root=None, timeout_seconds=900, unified_log_path=None,
                            alias=None, issue_iid=None, events_dir=None, env=None, gate=False,
                            max_budget_usd=None, offline=False):
    """The one subprocess boundary: everything `invoke_issue_agent` used to
    do after building its prompt. `alias`/`issue_iid`/`events_dir` are used
    only to label the `issue.agent_failed` event on the failure path. `env`
    entries are added to the subprocess environment (gate mode's
    LOOP_HANDOFF_PATH); `gate` selects the gate-mode tool lists and
    `offline` adds the golden-eval denials (no GitLab/Slack/dashboard).

    Raises subprocess.CalledProcessError/TimeoutExpired on failure -
    LoopRuntime.start() catches agent_fn exceptions and turns them into a
    FAILED IterationResult for that issue only. It swallows the exception's
    message doing so, which is exactly why the failure is logged and
    emitted here first."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    worktree_root = loop_config.get_worktree_root()
    ai_cli = ai_cli_config.get_selected_cli()
    cmd = _cli_command(ai_cli, prompt, repo_root, worktree_root, gate=gate, max_budget_usd=max_budget_usd,
                       offline=offline)
    run_kwargs = {"env": {**os.environ, **env}} if env else {}

    def report_failure(reason, detail, event_reason=None):
        label = f" for {alias} #{issue_iid}" if alias is not None else ""
        _append_unified_log(
            f"{ai_cli} invocation{label} FAILED ({reason}):\n{detail}",
            repo_root=repo_root, unified_log_path=unified_log_path,
        )
        run_id = os.environ.get("LOOP_RUN_ID")
        issue_run_id = (
            f"{run_id}_{alias}_{issue_iid}" if run_id and alias is not None else None
        )
        _emit_best_effort(
            "issue.agent_failed", run_id=run_id, issue_run_id=issue_run_id,
            project=alias, issue_iid=issue_iid,
            data={"reason": event_reason or reason, "cli": ai_cli,
                  "stderr_excerpt": detail[-_STDERR_EXCERPT_CHARS:]},
            events_dir=events_dir,
        )

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=True, **run_kwargs)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = _exception_output(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"timed out after {timeout_seconds}s"
        else:
            reason = f"exited {exc.returncode}"
        envelope = _parse_envelope(getattr(exc, "stdout", None)) if ai_cli == "claude" else None
        # What the failed call spent (the CLI's JSON error envelope still
        # carries total_cost_usd), so the run's remaining budget stays honest.
        budget = bool(envelope) and envelope.get("subtype") == "error_max_budget_usd"
        exc.cost_usd = _failed_call_cost(envelope, budget, max_budget_usd)
        report_failure(reason, detail, "budget_exceeded" if budget else None)
        raise

    if ai_cli == "claude":
        parsed = _parse_envelope(proc.stdout)
        if parsed is not None and parsed.get("is_error"):
            budget = parsed.get("subtype") == "error_max_budget_usd"
            report_failure("error envelope on exit 0", proc.stdout, "budget_exceeded" if budget else None)
            raise AgentCallError("claude reported an error",
                                 cost_usd=_failed_call_cost(parsed, budget, max_budget_usd))
        result_text = cost_module.extract_result_text(parsed) if parsed else "(no result text in CLI output)"
        usage = cost_module.extract_claude_usage(parsed) if parsed else None
        cost_usd = usage["cost_usd"] if usage else None
    else:
        result_text = proc.stdout
        cost_usd = None
        usage = None

    print(result_text)
    _append_unified_log(result_text, repo_root=repo_root, unified_log_path=unified_log_path)
    # run-loop.sh let the CLI's stderr flow to the per-run dated log
    # regardless of exit code (it never redirected 2>&1 away from the
    # script's own inherited fd2). subprocess.run captures it instead, so
    # it has to be written out explicitly or that diagnostic trail - CLI
    # warnings, deprecations, partial errors on an otherwise-zero exit -
    # would silently vanish.
    if proc.stderr:
        _append_unified_log(
            f"{ai_cli} stderr:\n{proc.stderr}", repo_root=repo_root, unified_log_path=unified_log_path
        )

    return {"changed": True, "cost_usd": cost_usd, "usage": usage}


def _with_feedback(prompt, feedback):
    return f"{prompt}\n\n{feedback}" if feedback else prompt


def invoke_issue_agent(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                       feedback=None, gate=False, run_id=None, max_budget_usd=None):
    """The dashboard's on-demand single-issue invocation, unchanged: the
    2-arg prompt, which does its own full "End of run"."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = _with_feedback(build_prompt(alias, issue_iid, repo_root=repo_root), feedback)
    env = None
    if gate:
        prompt, env = _gate_prompt_and_env(prompt, alias, issue_iid, repo_root, run_id)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path, alias=alias, issue_iid=issue_iid, env=env, gate=gate,
        max_budget_usd=max_budget_usd,
    )


def invoke_batch_issue_agent(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None,
                             feedback=None, gate=False, run_id=None, max_budget_usd=None):
    """One issue inside the scheduled batch: no "End of run" here - the
    batch's single wrap-up call below does that once for the whole run."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = _with_feedback(build_batch_issue_prompt(alias, issue_iid, repo_root=repo_root), feedback)
    env = None
    if gate:
        prompt, env = _gate_prompt_and_env(prompt, alias, issue_iid, repo_root, run_id)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path, alias=alias, issue_iid=issue_iid, env=env, gate=gate,
        max_budget_usd=max_budget_usd,
    )


def invoke_issue_file_agent(alias, issue_iid, issue_file, repo_root=None, timeout_seconds=900,
                            unified_log_path=None, feedback=None, run_id=None, max_budget_usd=None):
    """The golden eval suite's invocation: always gate mode (handoff file,
    no MR) and offline (GitLab/Slack/dashboard tools hard-denied), with the
    issue text read from `issue_file`."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = _with_feedback(build_issue_file_prompt(alias, issue_iid, issue_file, repo_root=repo_root), feedback)
    prompt, env = _gate_prompt_and_env(prompt, alias, issue_iid, repo_root, run_id)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path, alias=alias, issue_iid=issue_iid, env=env, gate=True,
        max_budget_usd=max_budget_usd, offline=True,
    )


def invoke_batch_end_of_run_agent(repo_root=None, timeout_seconds=900, unified_log_path=None):
    """The scheduled batch's wrap-up: daily-review.md, outputs/history/,
    PROGRESS.md and the one guaranteed Slack digest, reconstructed from
    this run's events."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = build_batch_end_of_run_prompt(repo_root=repo_root)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path,
    )


_FEEDBACK_HEADER = (
    "## Previous attempt failed external verification\n\n"
    "The loop re-ran this project's own checks in your worktree after your last attempt. "
    "Fix the cause, re-run the same commands yourself, and only then finish.\n\n"
)
_FEEDBACK_MAX_CHARS = 6000
_FEEDBACK_PER_RESULT_CHARS = 2500


def format_feedback(previous_iteration):
    """Failure feedback for the next attempt: each failed verifier's output
    tail (its output already carries `$ <command>` headers), bounded."""
    parts = []
    for r in previous_iteration.verification_results:
        if r.passed:
            continue
        commands = (r.evidence or {}).get("commands")
        if commands:
            parts.extend(
                f"$ {c['command']}\n{(c.get('output') or '')[-_FEEDBACK_PER_RESULT_CHARS:]}"
                for c in commands if not c.get("passed")
            )
        else:
            parts.append(r.output[-_FEEDBACK_PER_RESULT_CHARS:])
    return (_FEEDBACK_HEADER + "\n\n".join(parts))[:_FEEDBACK_MAX_CHARS]


def _iteration_failed_verification(iteration):
    return any(not r.passed for r in iteration.verification_results)


def _emit_verification_events(run_id, issue_run_id, alias, issue_iid, mode, iteration, events_dir=None):
    """external_skipped for a vacuous pass (no worktree / no commands),
    external_completed otherwise - the vocabulary bin/metrics.py reads. An
    iteration that never reached verification (agent failed, budget stop)
    also counts as skipped."""
    results = iteration.verification_results
    if not results:
        _emit_best_effort(
            "verification.external_skipped", run_id=run_id, issue_run_id=issue_run_id,
            project=alias, issue_iid=issue_iid, events_dir=events_dir,
        )
    for result in results:
        evidence = result.evidence or {}
        if evidence.get("vacuous") or evidence.get("commands") == []:
            _emit_best_effort(
                "verification.external_skipped", run_id=run_id, issue_run_id=issue_run_id,
                project=alias, issue_iid=issue_iid, events_dir=events_dir,
            )
            continue
        data = {
            "verifier": result.name, "passed": result.passed,
            "observed_passed": evidence.get("observed_passed", result.passed),
            "mode": mode, "iteration": iteration.iteration,
        }
        if evidence.get("error"):
            data["error"] = True  # a verifier config error says nothing about the agent's fix
        _emit_best_effort(
            "verification.external_completed", run_id=run_id, issue_run_id=issue_run_id,
            project=alias, issue_iid=issue_iid, data=data, events_dir=events_dir,
        )


_HANDOFF_ACTIONS = ("fix", "answer", "escalate", "wait_for_review")
# Handoff actions the agent finished itself; the loop has nothing to add.
_AGENT_OUTCOMES = {"answer": "answered", "wait_for_review": "waiting_for_review", "escalate": "escalated:agent"}
_NEEDS_HUMAN_LABEL = "loop:needs-human"
_FAILURE_TAIL_CHARS = 1500
_OPENER_TIMEOUT_SECONDS = 300


def read_handoff(path, issue_iid):
    """The agent's handoff JSON, or None when it is missing/malformed. A
    `fix` must name exactly this issue's loop/issue-<iid> branch and carry a
    non-empty target_branch and title."""
    try:
        data = json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("action") not in _HANDOFF_ACTIONS:
        return None
    if data["action"] == "fix":
        branch = data.get("branch")
        if not isinstance(branch, str) or not re.fullmatch(r"loop/issue-\d+", branch):
            return None
        if branch != f"loop/issue-{issue_iid}":
            return None
        for key in ("target_branch", "title"):
            if not isinstance(data.get(key), str) or not data[key].strip():
                return None
    return data


def _run_open_merge_request(local_path, branch, target_branch, title, repo_root=None):
    """The real opener: open_merge_request.sh keeps its loop/issue-* guard."""
    if repo_root is None:
        repo_root = REPO_ROOT
    script = Path(repo_root) / "bin" / "scripts" / "open_merge_request.sh"
    try:
        proc = subprocess.run(
            ["bash", str(script), local_path, branch, target_branch, title],
            cwd=str(repo_root), check=False, capture_output=True, text=True, timeout=_OPENER_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def _last_failed_iteration(result):
    """The newest iteration whose verification failed, or None. A retry that
    crashed or hit the budget has no verification results of its own, so the
    failure worth reporting is the attempt before it."""
    for iteration in reversed(getattr(result, "iterations", None) or []):
        if iteration.verification_results:
            return iteration if _iteration_failed_verification(iteration) else None
    return None


def _failure_text(result):
    """Failing commands' per-command tails from the newest failed
    verification (falling back to the verifier's own output), bounded."""
    parts = []
    iteration = _last_failed_iteration(result)
    for r in (iteration.verification_results if iteration else []):
        if r.passed:
            continue
        commands = (r.evidence or {}).get("commands")
        if commands:
            parts.extend(f"$ {c['command']}\n{c.get('output') or ''}" for c in commands if not c.get("passed"))
        else:
            parts.append(r.output or "")
    return "\n\n".join(parts)[-_FAILURE_TAIL_CHARS:]


_STOP_REASON_TEXT = {
    "agent_failed": "The agent session failed or timed out (details in the loop's log).",
    "budget_exceeded": "The run hit its iteration, time or cost budget.",
}


def _escalation_comment(issue_iid, reason, failure_text, stop_reason=None):
    fenced = f"\n\n```\n{failure_text}\n```" if failure_text else ""
    if reason == "handoff_invalid":
        tried = "I worked on this issue but did not leave a valid handoff, so the loop could not tell what to do next."
        failed = "The handoff file was missing or malformed; no merge request was opened."
    elif reason == "run_incomplete":
        tried = "I started on this issue, but the run stopped before I finished, so no merge request was opened."
        failed = _STOP_REASON_TEXT.get(stop_reason, f"The run stopped early ({stop_reason}).")
    elif reason == "mr_open_failed":
        tried = "I fixed this and the loop's own checks passed, but the merge request could not be opened."
        failed = "Opening the merge request failed:" + (fenced or "\n(No output.)")
    else:
        tried = "I implemented a fix, but the loop's own verification of it failed, so no merge request was opened."
        how = "failed the same way on consecutive attempts" if stop_reason == "no_progress" else "failed"
        failed = f"The project's checks, re-run by the loop in my worktree, {how}:" + (
            fenced or "\n(The checks produced no output.)")
        if stop_reason in _STOP_REASON_TEXT:
            failed += "\n\nThe retry did not finish: " + _STOP_REASON_TEXT[stop_reason]
    return (
        f"**What I tried**\n{tried}\n\n"
        f"**What failed**\n{failed}\n\n"
        f"**Where the work is**\n- Branch `loop/issue-{issue_iid}` in this issue's local worktree (not pushed).\n\n"
        f"**What I need from you**\n- Look at the failure above and the unpushed branch, then fix it or tell me how to proceed."
    )


def _project_slack_target(alias, project, gitlab_config_path=None):
    """(bundle, issue_web_url) for this alias from ~/.gitlab/config.json - the
    same `bundle` the agent's own slack_notify.py calls use. Either may be
    None; a numeric project_id gives no URL. Never raises."""
    if gitlab_config_path is None:
        gitlab_config_path = Path.home() / ".gitlab" / "config.json"
    try:
        config = json.loads(Path(gitlab_config_path).read_text())
        if not isinstance(config, dict):
            config = {}
    except (OSError, ValueError):
        config = {}
    entry = (config.get("projects") or {}).get(alias)
    bundle = entry.get("bundle") if isinstance(entry, dict) else None
    instance = (config.get("instances") or {}).get(project.get("instance"))
    base = instance.get("url") if isinstance(instance, dict) else None
    project_id = project.get("project_id")
    url = None
    if isinstance(base, str) and base.startswith(("http://", "https://")) \
            and isinstance(project_id, str) and "/" in project_id:
        url = f"{base.rstrip('/')}/{project_id}"
    return (bundle if isinstance(bundle, str) and bundle else None), url


def finalize_gated_issue(result, alias, issue_iid, run_id, repo_root, handoff=None, project=None,
                         opener=None, gitlab=None, notifier=None, events_dir=None, gitlab_config_path=None):
    """Gate mode's last step: open the MR (only after verification passed)
    or escalate. Returns "mr_opened" | "answered" | "waiting_for_review" |
    "escalated:<reason>". GitLab/Slack calls are best-effort and never
    raise out of here."""
    if handoff is None:
        handoff = handoff_path(run_id, alias, issue_iid, repo_root=repo_root)
    issue_run_id = f"{run_id}_{alias}_{issue_iid}"

    def log(text):
        try:
            _append_unified_log(f"gate finalize {alias} #{issue_iid}: {text}", repo_root=repo_root)
        except OSError:
            pass

    bundle, issue_label = None, f"#{issue_iid} ({alias})"

    def notify(reason=None, message=None):
        message = message or f"Loop escalated {issue_label} ({reason}); see the issue for details."
        try:
            if notifier is not None:
                notifier(message)
            else:
                _notify_slack_best_effort(message, bundle=bundle)
        except Exception as exc:  # noqa: BLE001
            log(f"notify failed: {type(exc).__name__}: {exc}")

    try:
        if project is None:
            project = loop_config.get_project(alias)
        bundle, project_url = _project_slack_target(alias, project, gitlab_config_path)
        if project_url:
            issue_label = f"<{project_url}/-/issues/{issue_iid}|{issue_label}>"
        notes_path = f"/projects/{urllib.parse.quote(str(project['project_id']), safe='')}/issues/{issue_iid}"
        project["local_path"], project["instance"]
    except Exception as exc:  # noqa: BLE001 - the committed fix must not be stranded silently
        log(f"project config error: {type(exc).__name__}: {exc}")
        _emit_best_effort("issue.escalated", run_id=run_id, issue_run_id=issue_run_id, project=alias,
                          issue_iid=issue_iid, data={"reason": "project_config_error", "gated": True},
                          events_dir=events_dir)
        notify("project_config_error")
        return "escalated:project_config_error"

    def api(method, path, body):
        nonlocal gitlab
        try:
            if gitlab is None:
                gitlab = connectors_config.load_connector(project["instance"])
            gitlab.api(method, path, json_body=body)
        except Exception as exc:  # noqa: BLE001 - best-effort, must not crash a batch
            log(f"GitLab {method} {path} failed: {type(exc).__name__}: {exc}")

    def emit(event_type, data):
        _emit_best_effort(event_type, run_id=run_id, issue_run_id=issue_run_id, project=alias,
                          issue_iid=issue_iid, data=data, events_dir=events_dir)

    def escalate(reason, failure_text="", stop_reason=None):
        comment = _escalation_comment(issue_iid, reason, failure_text, stop_reason=stop_reason)
        api("POST", f"{notes_path}/notes", {"body": comment})
        api("PUT", notes_path, {"add_labels": _NEEDS_HUMAN_LABEL})
        emit("issue.escalated", {"reason": reason, "gated": True,
                                 **({"stop_reason": stop_reason} if stop_reason else {})})
        notify(reason)
        return f"escalated:{reason}"

    data = read_handoff(handoff, issue_iid)
    if data is not None and data["action"] in _AGENT_OUTCOMES:
        return _AGENT_OUTCOMES[data["action"]]
    # The handoff is cleared before every attempt, so after a crash or a
    # budget stop a missing handoff means "never got that far", not a bad one.
    # A retry that crashed or hit the budget still owes the human the
    # earlier attempt's failed checks, not just "the run stopped".
    if result.final_state in (LoopState.FAILED, LoopState.STOPPED):
        if _last_failed_iteration(result) is not None:
            return escalate("verification_failed", _failure_text(result), stop_reason=result.stop_reason)
        return escalate("run_incomplete", stop_reason=result.stop_reason)
    if data is None:
        return escalate("handoff_invalid")
    if result.final_state != LoopState.COMPLETED:
        return escalate("verification_failed", _failure_text(result), stop_reason=result.stop_reason)

    if opener is None:
        def opener(local_path, branch, target, title):
            return _run_open_merge_request(local_path, branch, target, title, repo_root=repo_root)
    try:
        ok, detail = opener(project["local_path"], data["branch"], data["target_branch"], data["title"])
    except Exception as exc:  # noqa: BLE001
        ok, detail = False, f"{type(exc).__name__}: {exc}"
    if not ok:
        return escalate("mr_open_failed", str(detail)[-_FAILURE_TAIL_CHARS:])

    checks = "\n".join(f"- `{project[k]}` \u2705" for k in ("test_cmd", "lint_cmd") if project.get(k))
    body = (
        f"Opened merge request for this fix: {data['branch']} \u2192 {data['target_branch']}. "
        f"Verified by the loop with:\n{checks}\n\n{data.get('summary') or ''}"
    ).rstrip()
    api("POST", f"{notes_path}/notes", {"body": body})
    # Same shape the batch wrap-up reads from an agent-emitted fix event.
    found = re.search(r"https?://\S+/merge_requests/\d+", str(detail))
    mr_url = found.group(0) if found else None
    emit("issue.completed", {"outcome": "mr_opened", "gated": True, "action": "fix", "mr_url": mr_url})
    # Step 10's "Finished" message, which the gate override tells the agent not to send.
    notify(message=f"*Finished* {issue_label}: MR opened \u2192 " + (f"<{mr_url}|view MR>" if mr_url else data["branch"]))
    return "mr_opened"


def _is_loop_escalation(outcome):
    """A gate outcome the loop itself escalated (finalize already commented,
    labelled and notified) - as opposed to the agent's own escalation."""
    return bool(outcome) and outcome.startswith("escalated:") and outcome != "escalated:agent"


def _run_one_issue(run_id, alias, issue_iid, definition, results_dir, repo_root,
                   agent_invoker=None, events_dir=None):
    """`agent_invoker` defaults to `invoke_issue_agent` (the dashboard's
    path). Resolved inside the body, never as a def-time default, per
    CLAUDE.md's dependency-injection rule - a def-time default would bind
    the function object at import and make
    `monkeypatch.setattr(glr, "invoke_issue_agent", ...)` silently
    ineffective. External verification (the `project_commands` verifier) runs
    inside LoopRuntime: observe mode records it without changing the outcome,
    gate mode fails the iteration and retries with `format_feedback`."""
    if agent_invoker is None:
        agent_invoker = invoke_issue_agent
    issue_run_id = f"{run_id}_{alias}_{issue_iid}"
    timeout_seconds = definition.stop_conditions.max_runtime_minutes * 60
    mode = definition.verification.mode
    gate = mode == "gate"
    raw_costs = []
    raw_usages = []
    issue = {"alias": alias, "issue_iid": issue_iid, "timeout_seconds": timeout_seconds}
    if gate:
        # The verifier checks only an attempt that handed off a fix.
        issue["handoff_path"] = handoff_path(run_id, alias, issue_iid, repo_root=repo_root)

    def agent_fn(context):
        if gate:
            # Each attempt starts with no handoff, so finalize can never read
            # an earlier attempt's.
            issue["handoff_path"].unlink(missing_ok=True)
        previous = context.get("previous")
        feedback = format_feedback(previous) if previous and _iteration_failed_verification(previous) else None
        cap = cost_module.remaining_budget(
            definition.stop_conditions.max_cost_usd, sum(c or 0 for c in raw_costs))
        try:
            agent_result = agent_invoker(
                alias, issue_iid, repo_root=repo_root, timeout_seconds=timeout_seconds,
                feedback=feedback, gate=gate, run_id=run_id, max_budget_usd=cap,
            )
        except Exception as exc:
            # A failed call may still have spent money; None = unknown.
            raw_costs.append(getattr(exc, "cost_usd", None))
            raise
        if isinstance(agent_result, dict):
            raw_costs.append(agent_result.get("cost_usd"))
            raw_usages.append(agent_result.get("usage"))
        return agent_result

    verifiers = build_verifiers(definition.verifiers, cwd=None, issue=issue, mode=mode)
    emitted_iterations = []

    def on_iteration(_run_id, _loop_id, _definition_name, iterations):
        # Called with the cumulative list after every iteration; emit only
        # the newest one's verification events.
        latest = iterations[-1]
        if latest.iteration in emitted_iterations:
            return
        emitted_iterations.append(latest.iteration)
        _emit_verification_events(run_id, issue_run_id, alias, issue_iid, mode, latest, events_dir=events_dir)

    runtime = LoopRuntime(agent_fn=agent_fn, verifiers=verifiers, events_dir=events_dir, on_iteration=on_iteration)
    result = runtime.start(definition, run_id=issue_run_id)
    # LoopRuntime counts a None cost_usd as 0 in the budget, so the budget
    # figures alone can't tell "this issue really cost $0" from "we never got
    # a cost figure at all" (the Codex path, or a failed Claude cost
    # extraction). Record the raw, un-coerced value so `aggregate_cost_usd`
    # and the loop.result ledger event (loop_serialize.result_summary) can
    # keep them apart. A plain attribute rather
    # than a LoopResult field on purpose: dataclasses.asdict() ignores it,
    # so outputs/loop-runs/<run>/result.json's shape is unchanged.
    setattr(result, _AGENT_COST_ATTR, _sum_or_none(raw_costs))
    setattr(result, _AGENT_USAGE_ATTR, _sum_usages(raw_usages))

    if gate:
        # An unexpected failure here must not crash the batch; the issue
        # just keeps its runtime outcome.
        try:
            outcome = finalize_gated_issue(result, alias, issue_iid, run_id, repo_root, events_dir=events_dir)
        except Exception as exc:  # noqa: BLE001
            outcome = None
            _append_unified_log(
                f"gate finalize for {alias} #{issue_iid} failed: {type(exc).__name__}: {exc}", repo_root=repo_root)
        setattr(result, _GATE_OUTCOME_ATTR, outcome)
        if _is_loop_escalation(outcome) and result.final_state == LoopState.COMPLETED:
            # Visible on Loop Runs (result.json) as an escalation, not
            # "completed". A non-completed result already says why it stopped.
            result.final_state = LoopState.ESCALATED
            result.stop_reason = f"gate:{outcome.split(':', 1)[1]}"

    write_result(result, results_dir=results_dir, events_dir=events_dir)
    return result


def run_all_issues(run_id, results_dir=None, definition_path=None, repo_root=None,
                   aliases=None, username=None, events_dir=None, unified_log_path=None):
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    if repo_root is None:
        repo_root = REPO_ROOT
    if aliases is None:
        aliases = loop_config.list_aliases()
    if username is None:
        username = loop_config.get_assignee_username()

    definition = LoopDefinition.from_yaml(definition_path)
    assigned = list_assigned_issues(aliases, username)
    timeout_seconds = definition.stop_conditions.max_runtime_minutes * 60

    results = []
    for alias, issues in assigned.items():
        for issue in issues:
            if not issue_tracking_config.is_issue_enabled(alias, issue["iid"]):
                continue
            results.append(_run_one_issue(
                run_id, alias, issue["iid"], definition, results_dir, repo_root,
                agent_invoker=invoke_batch_issue_agent, events_dir=events_dir,
            ))

    # UNCONDITIONAL, and deliberately outside LoopRuntime: "End of run" is
    # housekeeping (daily-review.md, outputs/history/, PROGRESS.md, the one
    # guaranteed Slack digest), not a verifiable task, so it produces no
    # LoopResult. It must run even when `results` is empty - a morning with
    # zero assigned issues is exactly the case where nothing else would
    # report anything at all.
    try:
        invoke_batch_end_of_run_agent(
            repo_root=repo_root, timeout_seconds=timeout_seconds,
            unified_log_path=unified_log_path,
        )
    except Exception as exc:  # noqa: BLE001 - never let wrap-up sink the run
        detail = f"{type(exc).__name__}: {exc}"
        _append_unified_log(
            f"end-of-run wrap-up FAILED - no daily digest was sent: {detail}",
            repo_root=repo_root, unified_log_path=unified_log_path,
        )
        _emit_best_effort(
            "run.wrapup_failed", run_id=run_id,
            data={"error": detail[-_STDERR_EXCERPT_CHARS:]}, events_dir=events_dir,
        )
        _notify_slack_best_effort(
            "*Daily GitLab loop:* the end-of-run wrap-up FAILED, so today's "
            f"digest/daily-review was NOT produced — {detail}",
            notification_key="gitlab_wrapup_failed",
        )

    # Down-weight lessons whose reuse preceded a failure. Best-effort and
    # idempotent (applied.json), so a recent two-day event window is safe.
    try:
        since = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        learning.apply_outcomes(
            list(events_module.iter_events(events_dir=events_dir, since_date=since)),
            applied_path=Path(repo_root) / "outputs" / "learning" / "applied.json",
        )
    except Exception as exc:  # noqa: BLE001 - never let scoring sink the run
        _append_unified_log(
            f"memory down-weighting FAILED: {type(exc).__name__}: {exc}",
            repo_root=repo_root, unified_log_path=unified_log_path,
        )

    return results


def run_single_issue(run_id, alias, issue_iid, results_dir=None, definition_path=None,
                     repo_root=None, events_dir=None):
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    if repo_root is None:
        repo_root = REPO_ROOT
    definition = LoopDefinition.from_yaml(definition_path)
    # No agent_invoker override: the dashboard's on-demand path keeps using
    # the 2-arg prompt with its own full "End of run" (design spec non-goal:
    # don't touch the chat-triggered single-issue run path).
    return _run_one_issue(
        run_id, alias, issue_iid, definition, results_dir, repo_root, events_dir=events_dir
    )


def _sum_or_none(values):
    """Sum of the non-None entries, or None when there are none of them -
    "nobody told us a cost" is a different fact from "it cost $0.00"."""
    priced = [v for v in values if v is not None]
    return sum(priced) if priced else None


def _sum_usages(usages):
    """Sum each of _USAGE_TOKEN_FIELDS across the non-None entries in
    `usages` (one per agent_fn call - LoopRuntime may retry within an
    issue), or None when none of them carried real usage data - same
    "no data" convention as _sum_or_none, for the same reason: a Codex
    call or a failed Claude cost extraction contributes nothing, not 0."""
    real = [u for u in usages if u]
    if not real:
        return None
    return {field: sum(u.get(field) or 0 for u in real) for field in _USAGE_TOKEN_FIELDS}


def aggregate_cost_usd(results):
    """Total real dollars across `results`, or **None** when not a single
    result contributed an actual figure (a Codex-only run, a zero-issue
    morning, a failed Claude cost extraction).

    None, not 0.0: bin/cost.py's `_priced_run_ids` reads a non-None
    data.cost_usd on run.completed as "this run was priced" and then folds
    that run's issues into cost_per_issue/cost_per_resolution's
    denominators. Reporting 0.0 for an unpriced run would dilute those
    metrics with issues that were never priced - the exact failure mode
    `compute_cost_metrics`'s own docstring says the design avoids.

    Each issue's raw, un-coerced cost is stashed on its LoopResult by
    `_run_one_issue`; the budget field (the same one `loop_cli.py cost` and
    `loop_serialize.py` read) is the fallback for LoopResults built
    elsewhere."""
    contributions = []
    for result in results:
        cost = getattr(result, _AGENT_COST_ATTR, _UNSET)
        if cost is _UNSET:
            if not result.iterations:
                continue
            budget = result.iterations[-1].budget or {}
            cost = (budget.get("cost") or {}).get("used_usd")
        contributions.append(cost)
    return _sum_or_none(contributions)


def aggregate_usage_tokens(results):
    """{"input_tokens", "output_tokens", "cache_read_tokens",
    "cache_write_tokens"} summed across `results`, or **None** when not a
    single result carried real usage data (a Codex-only run, a zero-issue
    morning, a failed Claude cost extraction) - mirrors `aggregate_cost_usd`
    for the same reason: cost.py's `compute_cost_metrics` needs to tell
    "never measured" apart from "measured as zero"."""
    per_result = [
        usage for usage in (getattr(result, _AGENT_USAGE_ATTR, None) for result in results) if usage
    ]
    if not per_result:
        return None
    return {field: sum(u.get(field) or 0 for u in per_result) for field in _USAGE_TOKEN_FIELDS}


def _emit_run_completed(run_id, results, events_dir=None):
    """bin/cost.py's `compute_cost_metrics` reads run.completed's
    data.cost_usd and its four token fields, and bin/metrics.py aggregates
    on run_id - so this event must be emitted exactly once per run, by
    whoever actually holds the cost figures. That used to be run-loop.sh
    (one CLI call per run, cost extracted from its JSON); now it's one call
    per issue, so Python owns both the aggregation and the emit, and
    run-loop.sh emits nothing.

    The token fields exist specifically so cost.py's cache_hit_rate can
    tell whether the identical system-prompt/tool-definitions prefix this
    loop sends for every issue (see _allowed_tools) is actually landing in
    Anthropic's prompt cache across separate `claude -p` invocations,
    rather than that being pure guesswork.

    Both `cost_usd` and the token fields are *omitted entirely* - not set
    to 0/None - when nothing was actually priced, which is exactly the
    pre-existing shape of the event run-loop.sh used to emit and what
    bin/cost.py's `_priced_run_ids` reads as "unpriced". See
    `aggregate_cost_usd`/`aggregate_usage_tokens`."""
    data = {"issues": len(results)}
    cost_usd = aggregate_cost_usd(results)
    if cost_usd is not None:
        data["cost_usd"] = cost_usd
    usage_totals = aggregate_usage_tokens(results)
    if usage_totals is not None:
        data.update(usage_totals)
    return _emit_best_effort(
        "run.completed", run_id=run_id, data=data, events_dir=events_dir,
    )


def _alert_on_incomplete_results(results):
    """A per-issue failure is caught by LoopRuntime and never propagates to
    run-loop.sh's exit code, so its ERR trap can no longer see it. Ping
    Slack from here instead. Each LoopResult's run_id is
    `<run_id>_<alias>_<issue_iid>`, so naming it names the project and the
    issue. A gated issue the loop escalated already notified from
    `finalize_gated_issue`, so it is left out here to alert exactly once."""
    incomplete = [
        r for r in results
        if r.final_state != LoopState.COMPLETED and not _is_loop_escalation(getattr(r, _GATE_OUTCOME_ATTR, None))
    ]
    if not incomplete:
        return []
    detail = "; ".join(
        f"{r.run_id} ({r.final_state.value}: {r.stop_reason})" for r in incomplete
    )
    _notify_slack_best_effort(
        f"*Daily GitLab loop:* {len(incomplete)} of {len(results)} issues did not "
        f"complete — {detail} — see outputs/loop-runs/ and logs/loop-engineering.log",
        notification_key="gitlab_issues_incomplete",
    )
    return incomplete


def main(argv=None, results_dir=None, definition_path=None, repo_root=None,
         aliases=None, username=None, events_dir=None):
    if argv is None:
        argv = sys.argv[1:]
    # Strictly 1 or 3: anything else is a mistake, not a batch run. A typo
    # like `run-loop.sh harbor` (missing the issue IID) would otherwise
    # fall through and process every assigned issue across every project.
    if len(argv) not in (1, 3):
        print("Usage: gitlab_loop_runner.py <run_id> [alias issue_iid]", file=sys.stderr)
        return 2
    run_id = argv[0]
    if len(argv) == 3:
        results = [run_single_issue(
            run_id, argv[1], argv[2], results_dir=results_dir,
            definition_path=definition_path, repo_root=repo_root, events_dir=events_dir,
        )]
    else:
        results = run_all_issues(
            run_id, results_dir=results_dir, definition_path=definition_path,
            repo_root=repo_root, aliases=aliases, username=username, events_dir=events_dir,
        )
    _emit_run_completed(run_id, results, events_dir=events_dir)
    _alert_on_incomplete_results(results)
    # Still 0 even when issues failed: each failure is contained, recorded
    # in its own LoopResult, and now announced in Slack. A non-zero exit
    # here would trip run-loop.sh's ERR trap and report the whole run as
    # failed, which it wasn't.
    return 0


if __name__ == "__main__":
    sys.exit(main())
