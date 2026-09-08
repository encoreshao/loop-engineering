#!/usr/bin/env python3
"""Wires the real Topic Monitor loop to LoopRuntime - see
docs/superpowers/specs/2026-09-07-topic-monitor-runtime-wiring-design.md.
Mirrors bin/gitlab_loop_runner.py's shape, minus the batch/wrap-up split
that loop needed - the topic monitor has no shared "end of run" artifact,
so each topic's own LoopRuntime call is fully self-contained.

Two consequences of one-session-per-topic are handled here rather than by
the agent or by run-topic-monitor-loop.sh, because neither can see them
any more (both mirror the GitLab loop's own equivalents):

- `_mark_topic_failed` - a topic whose session is killed by the per-topic
  timeout never reaches its own "write status idle/failed" step, and
  outputs/topic-monitor/status.json is shared, cross-topic state that
  blocks every future run while any entry reads "running".
- `_alert_on_incomplete_results` - LoopRuntime contains a per-topic
  failure, so it never reaches run-topic-monitor-loop.sh's exit code and
  its ERR-trap Slack alert can no longer fire."""
import subprocess
import sys
from pathlib import Path

# bin/web/dashboard_server.py owns outputs/topic-monitor/status.json - its
# `write_topic_status` is the very function its `write-topic-status` CLI
# subcommand wraps, i.e. the one the agent itself calls in
# TOPIC_MONITOR_INSTRUCTIONS.md's steps 2 and 8. It lives in bin/web/, not
# this file's own directory, so it needs that directory on sys.path
# explicitly; called in-process here rather than shelled out, matching how
# `slack_notify.post_message` is used below and in
# bin/gitlab_loop_runner.py.
sys.path.insert(0, str(Path(__file__).resolve().parent / "web"))

import ai_cli_config
import dashboard_server
import slack_notify
import topic_config
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
from loop_serialize import write_result
from loop_state import LoopState
from loop_verifiers import build_verifiers

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEFINITION_PATH = REPO_ROOT / "loops" / "topic-monitor" / "loop.yaml"

# Ported from run-topic-monitor-loop.sh's own ALLOWED_TOOLS/
# DISALLOWED_TOOLS, including every rationale comment - this is the one
# loop in this repo whose input is untrusted web content (WebSearch/
# WebFetch), it runs under --permission-mode acceptEdits with nobody
# approving each action, and everything it could reach includes
# LOOPX_INSTRUCTIONS.md and run-loop.sh - the far higher-privilege
# GitLab loop's own control files (git push, GitLab API tokens). A
# prompt injection in a fetched page must not be able to rewrite those.
# Writes are meant to be confined to outputs/topic-monitor/ - this loop
# never touches source code, project checkouts, or git in any way,
# which is why `Bash(git*)` below denies git wholesale rather than being
# enumerated per-subcommand the way bin/gitlab_loop_runner.py's own
# allowlist has to (that loop actually needs some git subcommands).
#
# Three things about how that confinement is actually achieved, each
# verified by running the real CLI rather than assumed (see the
# "Tool permissions policy" section of TOPIC_MONITOR_INSTRUCTIONS.md):
#
#   1. NOT via --add-dir. --add-dir only *adds* directories to the
#      workspace. The agent's cwd is already the repo root, so an
#      --add-dir on outputs/topic-monitor would be a pure no-op: it
#      grants nothing and restricts nothing.
#   2. NOT via the allow list either. An allow rule grants; it never
#      revokes. Scoping the grant to Edit(**/outputs/topic-monitor/**)
#      documents the intent, but a path no rule mentions is still
#      writable (under acceptEdits, and under this machine's own
#      ~/.claude/settings.json, which allows Read/Write globally).
#   3. So the boundary IS the deny list (`_disallowed_tools` below).
#      Deny beats every allow, local or global, which makes it the only
#      rule kind here that can actually stop a write.
#
# Note the tool names: file permission rules match on Read(...) and
# Edit(...) only - an `Edit(...)` rule covers ALL file-editing tools,
# Write included. A `Write(path)` rule matches nothing and the CLI
# prints a warning about it, so there are deliberately none here.
#
# These strings and TOPIC_MONITOR_INSTRUCTIONS.md's own "Tool
# permissions policy" section describe the same policy in prose; the two
# must be kept in sync whenever either changes (see
# bin/gitlab_loop_runner.py's own equivalent comment on this).


def _allowed_tools(repo_root):
    return (
        "Read(**/outputs/topic-monitor/**) Edit(**/outputs/topic-monitor/**) "
        "Read(**/TOPIC_MONITOR_INSTRUCTIONS.md) Read(**/docs/tasks/topic-monitor-loop.md) "
        "WebSearch WebFetch "
        "Bash(cd *) "
        "Bash(python3 bin/topic_config.py*) Bash(python3 bin/topic_seen.py*) "
        "Bash(python3 bin/slack_notify.py*) Bash(python3 bin/web/dashboard_server.py*) "
        f"Bash(python3 {repo_root}/bin/topic_config.py*) Bash(python3 {repo_root}/bin/topic_seen.py*) "
        f"Bash(python3 {repo_root}/bin/slack_notify.py*) Bash(python3 {repo_root}/bin/web/dashboard_server.py*)"
    )


# The real write boundary (see point 3. above). Every **/-prefixed rule
# below is anchored to the run's cwd ($LOOP_DIR, since
# invoke_topic_agent's subprocess call inherits this process's own cwd) -
# it does NOT match an absolute path outside it. Each pattern is a path
# shape that cannot occur under outputs/topic-monitor/, whose only
# tool-written files are the briefings at history/<date>-<topic>.md - so
# none of these can block the loop's own work:
#   - by extension: no .sh/.py/.plist/.json/.yml/.toml is ever written
#     there (the seen-items state JSON is written by topic_seen.py in
#     its own subprocess, which tool permission rules don't apply to at
#     all - Edit(**/*.json) never sees that write, so it can't block
#     it). This is what covers run-loop.sh, run-topic-monitor-loop.sh
#     and every plist under this repo's own launchd/ - but, precisely
#     because it's cwd-anchored, it does NOT cover the INSTALLED copy of
#     the GitLab loop's schedule at
#     ~/Library/LaunchAgents/com.hermes.loop-engineering.plist, which
#     sits outside $LOOP_DIR entirely.
#   - by directory: none of these directories exist under it.
#   - by name: this repo's root markdown files, listed individually
#     since the briefings are markdown too and a blanket Edit(**/*.md)
#     would block the loop's own briefing writes.
#   - the GitLab loop's own outputs, which live beside this loop's.
#
# ~/Library/LaunchAgents/ is covered by the one absolute-path rule below
# instead (leading `/`, not `**/`) - and unlike every other rule here it
# has to be built from the REAL, expanded home directory rather than a
# literal `$HOME`. run-topic-monitor-loop.sh's own DISALLOWED_TOOLS is a
# double-quoted bash string, so bash expands $HOME to the real path
# BEFORE the CLI ever sees it, producing a `//`-prefixed absolute rule
# (the exact form TOPIC_MONITOR_INSTRUCTIONS.md documents). This Python
# port hands argv straight to subprocess.run with no shell involved, so a
# literal "$HOME" string would never be expanded and would match nothing
# on disk - `_disallowed_tools` resolves it itself via `Path.home()`
# instead (the `home` parameter, resolved inside the function body per
# CLAUDE.md's dependency-injection convention, never a module-level
# default). Absolute patterns are NOT cwd-anchored, which is exactly why
# this rule needs that form: without it, a prompt injection from fetched
# web content could rewrite that installed plist's ProgramArguments and
# get arbitrary code execution on the machine's own schedule - the same
# escalation class this loop's confinement exists to prevent.
#
# Residual gap, known and accepted (see TOPIC_MONITOR_INSTRUCTIONS.md): a
# new root-level file whose extension isn't denied below (a stray
# .md/.txt) can still be created - clutter, not a privilege escalation,
# since nothing reads such a file and every existing control file at that
# level is covered by name.


def _disallowed_tools(home=None):
    if home is None:
        home = Path.home()
    return (
        "Bash(git*) "
        "Read(**/.env*) Read(**/*.key) Read(**/id_rsa*) Read(**/.ssh/**) "
        "Edit(**/*.sh) Edit(**/*.py) Edit(**/*.plist) Edit(**/*.json) Edit(**/*.yml) Edit(**/*.yaml) Edit(**/*.toml) "
        "Edit(**/bin/**) Edit(**/launchd/**) Edit(**/docs/**) Edit(**/config/**) Edit(**/tests/**) Edit(**/assets/**) "
        "Edit(**/.claude/**) Edit(**/.git/**) Edit(**/.ssh/**) "
        "Edit(**/LOOPX_INSTRUCTIONS.md) Edit(**/TOPIC_MONITOR_INSTRUCTIONS.md) Edit(**/CLAUDE.md) "
        "Edit(**/README.md) Edit(**/TASK.md) Edit(**/PROGRESS.md) "
        "Edit(**/outputs/history/**) Edit(**/outputs/daily-review.md) "
        f"Edit(/{home}/Library/LaunchAgents/**)"
    )


def build_prompt(name=None, repo_root=None):
    if repo_root is None:
        repo_root = REPO_ROOT
    script = Path(repo_root) / "bin" / "scripts" / "build_topic_prompt.sh"
    args = [] if name is None else [name]
    result = subprocess.run(["bash", str(script), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _cli_command(ai_cli, prompt, repo_root):
    if ai_cli == "codex":
        return [
            "codex", "exec", "--sandbox", "workspace-write",
            "-c", "approval_policy=never",
            "-c", "sandbox_workspace_write.network_access=true",
            "-c", "tools.web_search=true",
            prompt,
        ]
    return [
        "claude", "-p",
        "--permission-mode", "acceptEdits",
        "--allowedTools", _allowed_tools(repo_root),
        "--disallowedTools", _disallowed_tools(),
        "--output-format", "text",
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


def _notify_slack_best_effort(message):
    """A scheduled run has nobody watching it.
    run-topic-monitor-loop.sh's ERR trap used to be the one failure alert
    this loop had, but per-topic failures are now contained by LoopRuntime
    and never reach that trap - so failures have to announce themselves
    from here instead. Mirrors bin/gitlab_loop_runner.py's own
    `_notify_slack_best_effort`."""
    try:
        slack_notify.post_message(message)
        return True
    except Exception as exc:  # noqa: BLE001 - an alert failing must not cascade
        print(f"topic_monitor_runner: Slack notification failed: {exc}", file=sys.stderr)
        return False


def _mark_topic_failed(name, status_path=None):
    """Write this topic's terminal `failed` state to
    outputs/topic-monitor/status.json ourselves.

    Normally the agent does this itself (TOPIC_MONITOR_INSTRUCTIONS.md's
    step 8, via `dashboard_server.py write-topic-status <name> idle|failed`)
    after writing `running` in step 2. But status.json is a single, shared,
    cross-topic file and `trigger_topic_monitor_run` refuses to start a new
    run while ANY topic's entry reads "running" - so if the per-topic
    subprocess timeout kills the CLI before step 8, that topic latches at
    "running" forever, permanently disabling the dashboard's "Run now"
    button. There is no reaper anywhere else, so this is that reaper: it
    runs precisely because the process that was supposed to write it died.

    Best-effort, like every other observability call here: a failure to
    record a failure must not itself take down the rest of the run."""
    if status_path is None:
        status_path = dashboard_server.TOPIC_MONITOR_STATUS_PATH
    try:
        dashboard_server.write_topic_status(name, "failed", status_path=status_path)
        return True
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(
            f"topic_monitor_runner: writing failed status for topic '{name}' failed: {exc}",
            file=sys.stderr,
        )
        return False


def _alert_on_incomplete_results(results):
    """A per-topic failure is caught by LoopRuntime and never propagates to
    run-topic-monitor-loop.sh's exit code, so its ERR trap can no longer
    see it. Ping Slack from here instead. Each LoopResult's run_id is
    `<run_id>_<topic_name>`, so naming it names the topic. Mirrors
    bin/gitlab_loop_runner.py's own `_alert_on_incomplete_results`."""
    incomplete = [r for r in results if r.final_state != LoopState.COMPLETED]
    if not incomplete:
        return []
    detail = "; ".join(
        f"{r.run_id} ({r.final_state.value}: {r.stop_reason})" for r in incomplete
    )
    _notify_slack_best_effort(
        f"*Topic monitor loop:* {len(incomplete)} of {len(results)} topics did not "
        f"complete — {detail} — see outputs/loop-runs/ and logs/loop-engineering.log"
    )
    return incomplete


def _exception_output(exc):
    """The stderr (preferred) or stdout an exception from `subprocess.run`
    carries. CalledProcessError always has both; TimeoutExpired may have
    either as None, and either may be bytes if text mode was off. Mirrors
    bin/gitlab_loop_runner.py's own `_exception_output`."""
    for value in (getattr(exc, "stderr", None), getattr(exc, "output", None)):
        if not value:
            continue
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        return value
    return ""


def invoke_topic_agent(name, repo_root=None, timeout_seconds=1800, unified_log_path=None):
    """The one subprocess boundary - tests monkeypatch this function
    directly, never run_all_topics. Returns {"changed": True,
    "cost_usd": None} (this loop's claude invocation uses
    --output-format text, not json, so there has never been any cost
    data to extract here - unlike the GitLab loop). Raises
    subprocess.CalledProcessError/TimeoutExpired on failure -
    LoopRuntime.start() already catches agent_fn exceptions and turns
    them into a FAILED IterationResult for that topic only. It swallows
    the exception's message doing so, which is exactly why the failure
    is logged here first, matching
    bin/gitlab_loop_runner.py's own `_invoke_cli_with_prompt`."""
    if repo_root is None:
        repo_root = REPO_ROOT
    repo_root = Path(repo_root)
    prompt = build_prompt(name, repo_root=repo_root)
    ai_cli = ai_cli_config.get_selected_cli()
    cmd = _cli_command(ai_cli, prompt, repo_root)

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_seconds, check=True)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        detail = _exception_output(exc)
        if isinstance(exc, subprocess.TimeoutExpired):
            reason = f"timed out after {timeout_seconds}s"
        else:
            reason = f"exited {exc.returncode}"
        _append_unified_log(
            f"{ai_cli} invocation for topic '{name}' FAILED ({reason}):\n{detail}",
            repo_root=repo_root, unified_log_path=unified_log_path,
        )
        raise

    print(proc.stdout)
    _append_unified_log(proc.stdout, repo_root=repo_root, unified_log_path=unified_log_path)
    # run-topic-monitor-loop.sh let the CLI's stderr flow to the per-run
    # dated log regardless of exit code (it never redirected 2>&1 away
    # from the script's own inherited fd2). subprocess.run captures it
    # instead, so it has to be written out explicitly or that diagnostic
    # trail - CLI warnings, deprecations, partial errors on an otherwise-
    # zero exit - would silently vanish.
    if proc.stderr:
        _append_unified_log(
            f"{ai_cli} stderr:\n{proc.stderr}", repo_root=repo_root, unified_log_path=unified_log_path
        )

    return {"changed": True, "cost_usd": None}


def _run_one_topic(run_id, name, definition, results_dir, repo_root, events_dir=None,
                   status_path=None):
    topic_run_id = f"{run_id}_{name}"
    timeout_seconds = definition.stop_conditions.max_runtime_minutes * 60
    agent_fn = lambda context: invoke_topic_agent(  # noqa: E731
        name, repo_root=repo_root, timeout_seconds=timeout_seconds,
    )
    verifiers = build_verifiers(definition.verifiers, cwd=None)
    runtime = LoopRuntime(agent_fn=agent_fn, verifiers=verifiers, events_dir=events_dir)
    result = runtime.start(definition, run_id=topic_run_id)
    # Only on a non-COMPLETED result: a topic that finished normally wrote
    # its own `idle` (or its own `failed`, per the "Failure policy"
    # section) from inside its own agent session, and that self-report is
    # the more accurate one - see `_mark_topic_failed` for why the
    # non-completed case can't be left to the agent.
    if result.final_state != LoopState.COMPLETED:
        _mark_topic_failed(name, status_path=status_path)
    write_result(result, results_dir=results_dir)
    return result


def run_all_topics(run_id, results_dir=None, definition_path=None, repo_root=None, names=None,
                   events_dir=None, status_path=None):
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    if repo_root is None:
        repo_root = REPO_ROOT
    if names is None:
        names = topic_config.list_names()

    definition = LoopDefinition.from_yaml(definition_path)

    results = []
    for name in names:
        results.append(_run_one_topic(
            run_id, name, definition, results_dir, repo_root,
            events_dir=events_dir, status_path=status_path,
        ))
    return results


def main_with_argv(argv, results_dir=None, definition_path=None, repo_root=None, names=None,
                   events_dir=None, status_path=None):
    if len(argv) != 1:
        print("Usage: topic_monitor_runner.py <run_id>", file=sys.stderr)
        return 2
    results = run_all_topics(
        argv[0], results_dir=results_dir, definition_path=definition_path,
        repo_root=repo_root, names=names, events_dir=events_dir, status_path=status_path,
    )
    _alert_on_incomplete_results(results)
    # Still 0 even when topics failed: each failure is contained, recorded
    # in its own LoopResult, its status written back as `failed`, and now
    # announced in Slack. A non-zero exit here would trip
    # run-topic-monitor-loop.sh's ERR trap and report the whole run as
    # failed, which it wasn't - same reasoning as
    # bin/gitlab_loop_runner.py's own `main`.
    return 0


def main():
    return main_with_argv(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
