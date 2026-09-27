#!/usr/bin/env python3
"""Runs the Inbox Triage loop - see
docs/superpowers/specs/2026-09-27-inbox-triage-design.md and
docs/tasks/inbox-triage-loop.md. Python owns every mail call; the AI gets
one tool-less, MCP-less call per inbox and returns JSON decisions.
Mirrors bin/topic_monitor_runner.py: one LoopRuntime iteration per inbox,
failures contained per inbox, exit 0 even when an inbox fails."""
import json
import subprocess
import sys
import time
from pathlib import Path

import ai_cli_config
import inbox_triage

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "outputs" / "inbox-triage"
DEFAULT_DEFINITION_PATH = REPO_ROOT / "loops" / "inbox-triage" / "loop.yaml"
INSTRUCTIONS_PATH = REPO_ROOT / "INBOX_TRIAGE_INSTRUCTIONS.md"
MESSAGE_CAP = 50


class TriageFailed(Exception):
    pass


def _cli_command(ai_cli):
    """No tools and no MCP servers: without --strict-mcp-config the user's
    own claude.ai connectors (which can include a Gmail connector able to
    deliver mail) would load into this session. The prompt goes on stdin so
    message content never appears in argv / `ps`."""
    if ai_cli == "codex":
        return ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "-c", "mcp_servers={}", "-"]
    return ["claude", "-p", "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}})]


def _append_unified_log(text, repo_root, unified_log_path):
    if unified_log_path is None:
        unified_log_path = Path(repo_root) / "logs" / "loop-engineering.log"
    path = Path(unified_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] inbox-triage: {text}\n")


def invoke_triage_agent(prompt, repo_root=None, timeout_seconds=900, unified_log_path=None):
    """The one AI subprocess boundary - tests monkeypatch this (or
    subprocess.run). Only exit status, timing, and error class are logged:
    stdout/stderr can echo message content, which must never persist."""
    if repo_root is None:
        repo_root = REPO_ROOT
    ai_cli = ai_cli_config.get_selected_cli()
    started = time.monotonic()
    try:
        proc = subprocess.run(_cli_command(ai_cli), input=prompt, capture_output=True, text=True,
                              timeout=timeout_seconds, check=True, cwd=str(repo_root))
    except subprocess.TimeoutExpired:
        _append_unified_log(f"{ai_cli} triage call FAILED (timed out after {timeout_seconds}s)", repo_root, unified_log_path)
        raise
    except subprocess.CalledProcessError as exc:
        _append_unified_log(f"{ai_cli} triage call FAILED (exited {exc.returncode})", repo_root, unified_log_path)
        raise
    elapsed = time.monotonic() - started
    if ai_cli == "codex":
        _append_unified_log(f"codex triage call ok ({elapsed:.1f}s)", repo_root, unified_log_path)
        return {"text": proc.stdout, "cost_usd": None}
    try:
        envelope = json.loads(proc.stdout)
    except json.JSONDecodeError:
        _append_unified_log("claude triage call FAILED (unparseable CLI envelope)", repo_root, unified_log_path)
        raise TriageFailed("claude returned an unparseable envelope") from None
    if envelope.get("is_error"):
        _append_unified_log("claude triage call FAILED (is_error)", repo_root, unified_log_path)
        raise TriageFailed("claude reported an error")
    _append_unified_log(f"claude triage call ok ({elapsed:.1f}s)", repo_root, unified_log_path)
    return {"text": envelope.get("result", ""), "cost_usd": envelope.get("total_cost_usd")}


def classify(prompt, messages, categories, invoke=None):
    if invoke is None:
        invoke = invoke_triage_agent
    total_cost, last_error = None, None
    for _attempt in range(2):
        try:
            out = invoke(prompt)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TriageFailed) as exc:
            last_error = f"AI call failed: {type(exc).__name__}"
            continue
        if out.get("cost_usd") is not None:
            total_cost = (total_cost or 0) + out["cost_usd"]
        try:
            return inbox_triage.parse_response(out["text"], messages, categories), total_cost
        except inbox_triage.TriageResponseError as exc:
            last_error = str(exc)
    raise TriageFailed(last_error)
