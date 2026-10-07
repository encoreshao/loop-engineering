"""The hardened, tool-less, MCP-less, non-persisting Claude call shared by
every loop that feeds untrusted content to the model. Claude-only: codex
always gives the model a shell and records the prompt under
~/.codex/sessions/, so it is refused here."""
import json
import subprocess
import tempfile
import time

import ai_cli_config
import cost as cost_module


class SealedCallFailed(Exception):
    pass


def sealed_command(max_budget_usd=None):
    """No tools and no MCP servers: without --strict-mcp-config the user's
    own claude.ai connectors (which can include a Gmail connector able to
    deliver mail) would load into this session. The prompt goes on stdin so
    message content never appears in argv / `ps`.

    --no-session-persistence and --settings '{"disableAllHooks": true}'
    close two more content-persistence leaks, both verified read-only
    against the installed `claude --help` (2.1.283) rather than assumed:
    without --no-session-persistence, `claude -p` still saves the full
    stdin prompt (i.e. the email bodies) as a resumable session transcript
    under ~/.claude/projects/...; --help documents it works with --print,
    which this command already uses. Without disableAllHooks, the user's
    own Stop/SessionEnd hooks still run and can read that transcript.
    `disableAllHooks` isn't just documented - it's confirmed as a real,
    live settings key by the installed binary's own string table, which
    contains the literal message "hooks are turned off in your settings
    (disableAllHooks)".

    There is deliberately no codex command: see the inbox_triage_runner module docstring."""
    return ["claude", "-p", "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
            "--no-session-persistence", "--settings", json.dumps({"disableAllHooks": True}),
            *cost_module.budget_args(max_budget_usd)]


def sealed_call(prompt, timeout_seconds, log=None, runner=None, cli_fn=None, command_fn=None,
                max_budget_usd=None):
    """Run one sealed Claude call in a disposable scratch cwd (never a repo,
    so no CLAUDE.md/auto-memory loads). Returns {"text", "cost_usd",
    "duration_ms"}. Raises SealedCallFailed (with the original subprocess
    error as __cause__ for timeouts/non-zero exits). `log(msg)` receives only
    status/timing strings, never prompt or response content."""
    if runner is None:
        runner = subprocess.run
    if cli_fn is None:
        cli_fn = ai_cli_config.get_selected_cli
    if command_fn is None:
        command_fn = sealed_command
    emit = log if log is not None else (lambda _msg: None)
    if cli_fn() != "claude":
        raise SealedCallFailed("sealed calls require the Claude CLI (codex gives the model a shell)")
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="loop-sealed-") as scratch_dir:
        try:
            proc = runner(command_fn(max_budget_usd=max_budget_usd), input=prompt, capture_output=True, text=True,
                          timeout=timeout_seconds, check=True, cwd=scratch_dir)
        except subprocess.TimeoutExpired as exc:
            emit(f"FAILED (timed out after {timeout_seconds}s)")
            raise SealedCallFailed("claude call timed out") from exc
        except subprocess.CalledProcessError as exc:
            emit(f"FAILED (exited {exc.returncode})")
            raise SealedCallFailed(f"claude exited {exc.returncode}") from exc
    elapsed = time.monotonic() - started
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        emit("FAILED (unparseable CLI envelope)")
        raise SealedCallFailed("claude returned an unparseable envelope") from None
    if not isinstance(envelope, dict):
        emit("FAILED (unparseable CLI envelope)")
        raise SealedCallFailed("claude returned an unparseable envelope")
    if envelope.get("is_error"):
        emit("FAILED (is_error)")
        raise SealedCallFailed("claude reported an error")
    emit(f"ok ({elapsed:.1f}s)")
    return {"text": envelope.get("result", ""), "cost_usd": envelope.get("total_cost_usd"),
            "duration_ms": int(elapsed * 1000)}
