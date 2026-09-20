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
import subprocess
import sys
from pathlib import Path

import ai_cli_config
import cost as cost_module
import events as events_module
import issue_tracking_config
import loop_config
import slack_notify
from list_assigned_issues import list_assigned_issues
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
from loop_serialize import write_result
from loop_state import LoopState
from loop_verifiers import CommandVerifier, build_verifiers

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
_AGENT_COST_ATTR = "agent_cost_usd"
_UNSET = object()

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
# a worktree. Three separate patterns per form because bin/'s contents are
# split by kind (see CLAUDE.md): the loop's own Python helpers directly in
# bin/, the dashboard web server in bin/web/, and one-shot shell scripts
# in bin/scripts/ - a glob's `*` doesn't cross a `/`, so each directory
# needs its own pattern.


def _allowed_tools(repo_root):
    return (
        "Read Edit Write "
        "Bash(git status*) Bash(git diff*) Bash(git add*) Bash(git commit*) Bash(git push origin loop/issue-*) "
        "Bash(cd *) "
        "Bash(RAILS_ENV=test bundle exec rspec*) Bash(bundle exec rspec*) Bash(bundle exec rubocop*) "
        "Bash(bundle check*) Bash(bundle install*) Bash(RAILS_ENV=test bundle exec rake db:test:prepare*) "
        "Bash(npm run test*) Bash(npm run lint*) Bash(npm ci*) Bash(yarn install*) "
        "Bash(python3 *gitlab_api.py*) Bash(python3 *gitlab_cache.py*) "
        "Bash(python3 bin/*.py*) Bash(python3 bin/web/*.py*) Bash(bash bin/scripts/*.sh*) "
        f"Bash(python3 {repo_root}/bin/*.py*) Bash(python3 {repo_root}/bin/web/*.py*) "
        f"Bash(bash {repo_root}/bin/scripts/*.sh*)"
    )


# Defense in depth: even if an allow pattern above were ever loosened by
# accident, these can never run.
_DISALLOWED_TOOLS = (
    "Bash(git merge*) Bash(git push --force*) Bash(git push -f*) Bash(git checkout*) "
    "Bash(git reset*) Bash(git clean*) Read(**/.env*) Read(**/*.key) Read(**/id_rsa*)"
)


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


def build_batch_end_of_run_prompt(repo_root=None):
    """The scheduled batch's wrap-up: "End of run" only, reconstructed
    from this run's own events."""
    return _run_build_run_prompt(["--batch-end-of-run"], repo_root)


def _cli_command(ai_cli, prompt, repo_root, worktree_root):
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
        "--allowedTools", _allowed_tools(repo_root),
        "--disallowedTools", _DISALLOWED_TOOLS,
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


def _notify_slack_best_effort(message, notification_key=None):
    """A scheduled run has nobody watching it. run-loop.sh's ERR trap used
    to be the one failure alert this system had, but per-issue failures are
    now contained by LoopRuntime and never reach that trap - so failures
    have to announce themselves from here instead. `notification_key`, if
    given, looks up a saved Block Kit template bound to it (Settings ->
    Notifications) and sends its blocks alongside the plain-text message;
    with no bound template (the default on a fresh install), this sends
    exactly what it always has."""
    try:
        blocks = slack_notify.resolve_blocks(notification_key, message) if notification_key else None
        slack_notify.post_message(message, blocks=blocks)
        return True
    except Exception as exc:  # noqa: BLE001 - an alert failing must not cascade
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


def _invoke_cli_with_prompt(prompt, repo_root=None, timeout_seconds=900, unified_log_path=None,
                            alias=None, issue_iid=None, events_dir=None):
    """The one subprocess boundary: everything `invoke_issue_agent` used to
    do after building its prompt. `alias`/`issue_iid`/`events_dir` are used
    only to label the `issue.agent_failed` event on the failure path.

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
    cmd = _cli_command(ai_cli, prompt, repo_root, worktree_root)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = _exception_output(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"timed out after {timeout_seconds}s"
        else:
            reason = f"exited {exc.returncode}"
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
            data={"reason": reason, "cli": ai_cli, "stderr_excerpt": detail[-_STDERR_EXCERPT_CHARS:]},
            events_dir=events_dir,
        )
        raise

    if ai_cli == "claude":
        parsed = json.loads(proc.stdout) if proc.stdout else None
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


def invoke_issue_agent(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
    """The dashboard's on-demand single-issue invocation, unchanged: the
    2-arg prompt, which does its own full "End of run"."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = build_prompt(alias, issue_iid, repo_root=repo_root)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path, alias=alias, issue_iid=issue_iid,
    )


def invoke_batch_issue_agent(alias, issue_iid, repo_root=None, timeout_seconds=900, unified_log_path=None):
    """One issue inside the scheduled batch: no "End of run" here - the
    batch's single wrap-up call below does that once for the whole run."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = build_batch_issue_prompt(alias, issue_iid, repo_root=repo_root)
    return _invoke_cli_with_prompt(
        prompt, repo_root=repo_root, timeout_seconds=timeout_seconds,
        unified_log_path=unified_log_path, alias=alias, issue_iid=issue_iid,
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


def _external_verify_issue(alias, issue_iid, timeout_seconds, repo_root=None):
    """Independently re-run `alias`'s real test_cmd/lint_cmd (from
    ~/.loop-engineering/projects.json) against the worktree this issue's
    agent call actually used, if it made one - see
    docs/superpowers/specs/2026-09-13-gitlab-issue-external-verification-design.md.
    Observe-only: the caller folds these into the persisted LoopResult's
    verification_results for visibility (Loop Runs/Budget/Audit,
    loop_serialize's "verified successful" count), but never uses them to
    change final_state/stop_reason - that stays exactly what LoopRuntime
    already decided from the agent's own report.

    Returns [] (not an error) when there's no worktree - an issue the
    agent answered without a code change has nothing to externally
    verify. Never raises: any unexpected failure (a misconfigured
    project, an unreadable projects.json) is caught, logged, and treated
    as "no results" - this must never be what crashes a real batch run."""
    try:
        project = loop_config.get_project(alias)
        worktree_root = loop_config.get_worktree_root()
        worktree_path = Path(worktree_root) / f"{Path(project['local_path']).name}-issue-{issue_iid}"
        if not worktree_path.is_dir():
            return []

        results = []
        for kind, command in (("test", project.get("test_cmd")), ("lint", project.get("lint_cmd"))):
            if not command:
                continue
            verifier = CommandVerifier(
                name=f"external_{kind}", command=command, cwd=worktree_path, timeout_seconds=timeout_seconds,
            )
            verification_result = verifier.verify({})
            # Truncate for storage only, not for the check itself: passed/
            # exit_code are unaffected. Before this plan, verification_
            # results was always [] for this loop, so CommandVerifier's own
            # unbounded output never mattered; now every issue's result.json
            # can carry up to two full test-suite outputs, which would grow
            # outputs/loop-runs/ unbounded and slow every dashboard page that
            # json.load()s it. Scoped to this call site rather than
            # CommandVerifier itself, which is shared by templates/evals too.
            verification_result.output = verification_result.output[-_STDERR_EXCERPT_CHARS:]
            results.append(verification_result)
        return results
    except Exception as exc:  # noqa: BLE001 - see docstring: must never crash a real batch run
        _append_unified_log(
            f"external verification for {alias} #{issue_iid} failed: {type(exc).__name__}: {exc}",
            repo_root=repo_root,
        )
        return []


def _run_one_issue(run_id, alias, issue_iid, definition, results_dir, repo_root,
                   agent_invoker=None, events_dir=None):
    """`agent_invoker` defaults to `invoke_issue_agent` (the dashboard's
    path). Resolved inside the body, never as a def-time default, per
    CLAUDE.md's dependency-injection rule - a def-time default would bind
    the function object at import and make
    `monkeypatch.setattr(glr, "invoke_issue_agent", ...)` silently
    ineffective. Also runs _external_verify_issue after `runtime.start()`
    returns (which may invoke the agent multiple times across retries)
    (observe-only - see that function's own docstring)."""
    if agent_invoker is None:
        agent_invoker = invoke_issue_agent
    issue_run_id = f"{run_id}_{alias}_{issue_iid}"
    timeout_seconds = definition.stop_conditions.max_runtime_minutes * 60
    raw_costs = []
    raw_usages = []

    def agent_fn(context):
        agent_result = agent_invoker(
            alias, issue_iid, repo_root=repo_root, timeout_seconds=timeout_seconds,
        )
        if isinstance(agent_result, dict):
            raw_costs.append(agent_result.get("cost_usd"))
            raw_usages.append(agent_result.get("usage"))
        return agent_result

    verifiers = build_verifiers(definition.verifiers, cwd=None)
    runtime = LoopRuntime(agent_fn=agent_fn, verifiers=verifiers, events_dir=events_dir)
    result = runtime.start(definition, run_id=issue_run_id)
    # LoopRuntime coerces a None cost_usd to 0 on its way into the budget
    # (`total_cost_usd += agent_result.get("cost_usd") or 0`), so the
    # LoopResult alone can no longer tell "this issue really cost $0" from
    # "we never got a cost figure at all" (the Codex path, or a failed
    # Claude cost extraction). Record the raw, un-coerced value so
    # `aggregate_cost_usd` can keep them apart. A plain attribute rather
    # than a LoopResult field on purpose: dataclasses.asdict() ignores it,
    # so outputs/loop-runs/<run>/result.json's shape is unchanged.
    setattr(result, _AGENT_COST_ATTR, _sum_or_none(raw_costs))
    setattr(result, _AGENT_USAGE_ATTR, _sum_usages(raw_usages))

    if result.iterations:
        external_results = _external_verify_issue(alias, issue_iid, timeout_seconds, repo_root=repo_root)
        if external_results:
            result.iterations[-1].verification_results.extend(external_results)
            for external_result in external_results:
                _emit_best_effort(
                    "verification.external_completed", run_id=run_id, issue_run_id=issue_run_id,
                    project=alias, issue_iid=issue_iid,
                    data={"verifier": external_result.name, "passed": external_result.passed},
                    events_dir=events_dir,
                )
        else:
            # Distinguishes "ran cleanly, nothing to verify" from "has been
            # silently broken for weeks" - see the final-review finding this
            # addresses: previously every empty-result exit from
            # _external_verify_issue (no worktree, no commands configured,
            # or an unexpected exception) was outwardly indistinguishable.
            _emit_best_effort(
                "verification.external_skipped", run_id=run_id, issue_run_id=issue_run_id,
                project=alias, issue_iid=issue_iid, events_dir=events_dir,
            )

    write_result(result, results_dir=results_dir)
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
    issue."""
    incomplete = [r for r in results if r.final_state != LoopState.COMPLETED]
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
