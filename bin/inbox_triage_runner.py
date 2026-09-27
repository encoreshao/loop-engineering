#!/usr/bin/env python3
"""Runs the Inbox Triage loop - see
docs/superpowers/specs/2026-09-27-inbox-triage-design.md and
docs/tasks/inbox-triage-loop.md. Python owns every mail call; the AI gets
one tool-less, MCP-less call per inbox and returns JSON decisions.
Mirrors bin/topic_monitor_runner.py: one LoopRuntime iteration per inbox,
failures contained per inbox, exit 0 even when an inbox fails.

Known gap: codex has no verified way to disable `codex exec`'s own
rollout/session persistence (see _cli_command's docstring for what was
investigated and ruled out - no key was invented to paper over it).
claude's equivalent leaks (a saved session transcript, and hooks running
against it) are closed below via --no-session-persistence and --settings
'{"disableAllHooks": true}'; codex's ~/.codex/sessions/ rollout of the
prompt is not currently closable from here."""
import json
import subprocess
import tempfile
import time
from pathlib import Path

import ai_cli_config
import inbox_config
import inbox_seen
import inbox_triage
import mail_auth
import mail_http
import mail_providers

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

    codex's own `-c tools.web_search=false` mirrors the key
    bin/topic_monitor_runner.py's `_cli_command` already sets to `true` -
    this loop needs the opposite, and setting it explicitly (rather than
    relying on --help's "off by default") documents the intent the same
    way the rest of this command does.

    Known gap (codex): no equivalent of claude's --no-session-persistence
    exists for `codex exec`. It unconditionally writes a full rollout
    (containing the prompt) under ~/.codex/sessions/ via its
    RolloutRecorder - confirmed by `codex exec resume --help` describing
    resuming "a previous recorded session" (recording is therefore not
    optional) and by the installed binary's own embedded event schema
    carrying a `rollout_path` field on `SessionConfiguredEvent`.
    Investigated `codex --help`, `codex exec --help`, `codex exec resume
    --help`, and the installed binary's own embedded config-field list for
    a disable switch: the only persistence-related config section that
    exists, `[history]` (fields `persistence`/`max_bytes`, confirmed
    present in the binary's own field list), governs the separate
    ~/.codex/history.jsonl plaintext recall log used for interactive
    message history/recall - not the rollout writer. No CODEX_* env var
    or `-c` key for disabling the rollout recorder itself was found. No
    key was invented to paper over this; it is an accepted residual gap on
    the codex path until codex ships one."""
    if ai_cli == "codex":
        return ["codex", "exec", "--sandbox", "read-only", "--skip-git-repo-check",
                "-c", "mcp_servers={}", "-c", "tools.web_search=false", "-"]
    return ["claude", "-p", "--output-format", "json", "--tools", "",
            "--strict-mcp-config", "--mcp-config", json.dumps({"mcpServers": {}}),
            "--no-session-persistence", "--settings", json.dumps({"disableAllHooks": True})]


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
    stdout/stderr can echo message content, which must never persist.

    Runs with cwd set to a fresh, disposable temporary directory rather
    than repo_root: this triage call reads untrusted email content, and
    running it from inside this repo would load this repo's own
    CLAUDE.md/auto-memory into the session for no reason. repo_root is
    kept only to resolve the default unified log path (unchanged)."""
    if repo_root is None:
        repo_root = REPO_ROOT
    ai_cli = ai_cli_config.get_selected_cli()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="inbox-triage-") as scratch_dir:
        try:
            proc = subprocess.run(_cli_command(ai_cli), input=prompt, capture_output=True, text=True,
                                  timeout=timeout_seconds, check=True, cwd=scratch_dir)
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


def _outcome(inbox, status, error=None, **extra):
    base = {"name": inbox["name"], "label": inbox.get("label", inbox["name"]), "status": status,
            "counts": {}, "urgent": [], "rows": [], "overflow": False, "error": error, "cost_usd": None}
    base.update(extra)
    return base


def triage_inbox(inbox, config, now, provider_factory=None, token_fn=None, invoke=None,
                 state_dir=None, instructions=None):
    if provider_factory is None:
        provider_factory = mail_providers.get_provider
    if token_fn is None:
        token_fn = mail_auth.get_access_token
    if instructions is None:
        instructions = INSTRUCTIONS_PATH.read_text()
    categories = inbox_config.categories_for(inbox, config)
    labels = {c["key"]: c["label"] for c in categories}

    try:
        token = token_fn(inbox)
        provider = provider_factory(inbox, token, lambda: token_fn(inbox))
        address = provider.profile_address()
        if address != inbox["account"].strip().lower():
            return _outcome(inbox, "failed", f"Signed in as {address}, expected {inbox['account']}")
        state = inbox_seen.load(inbox["name"], state_dir=state_dir)
        fetched = provider.fetch_new(inbox_seen.since(state, now), set(state["seen"]),
                                     inbox.get("exclude_senders", []), MESSAGE_CAP + 1)
    except (mail_auth.ReauthRequired, mail_http.AuthExpired) as exc:
        return _outcome(inbox, "needs_reauth", str(exc) or "Sign-in expired - reconnect this inbox")
    except (mail_auth.KeychainError, mail_http.MailHTTPError) as exc:
        return _outcome(inbox, "failed", str(exc))

    overflow = len(fetched) > MESSAGE_CAP
    messages = inbox_triage.filter_excluded(fetched[:MESSAGE_CAP], inbox.get("exclude_senders", []))
    if not messages:
        return _outcome(inbox, "quiet", overflow=overflow)

    prompt = inbox_triage.build_prompt(instructions, inbox, categories, messages)
    try:
        decisions, cost = classify(prompt, messages, categories, invoke=invoke)
    except TriageFailed as exc:
        return _outcome(inbox, "failed", f"AI triage failed: {exc}")
    decisions = inbox_triage.apply_rules(decisions, messages, inbox)
    by_id = {m["id"]: m for m in messages}

    labelled = []
    try:
        label_ids = provider.ensure_labels(sorted({labels[d["category"]] for d in decisions}))
        for decision in decisions:
            provider.apply_label(decision["id"], label_ids[labels[decision["category"]]])
            labelled.append(by_id[decision["id"]])
    except (mail_http.AuthExpired, mail_auth.ReauthRequired) as exc:
        inbox_seen.record(inbox["name"], labelled, now, state_dir=state_dir)
        return _outcome(inbox, "needs_reauth", str(exc) or "Sign-in expired - reconnect this inbox", cost_usd=cost)
    except (mail_http.MailHTTPError, mail_auth.KeychainError) as exc:
        inbox_seen.record(inbox["name"], labelled, now, state_dir=state_dir)
        return _outcome(inbox, "failed", f"Labelling stopped after {len(labelled)} of {len(decisions)}: {exc}", cost_usd=cost)
    inbox_seen.record(inbox["name"], labelled, now, state_dir=state_dir)

    rows, urgent = [], []
    for decision in decisions:
        message = by_id[decision["id"]]
        link, draft_failed = None, False
        if decision["draft_body"]:
            try:
                link = provider.create_reply_draft(message, decision["draft_body"])
            except (mail_http.MailHTTPError, mail_auth.ReauthRequired, mail_auth.KeychainError):
                draft_failed = True
        rows.append({"date": message["date"], "from": message["from"], "subject": message["subject"],
                     "category": decision["category"], "reason": decision["reason"], "draft_link": link})
        if decision["category"] == "urgent":
            urgent.append({"from": message["from"], "subject": message["subject"], "draft_link": link,
                           "needs_manual_reply": decision["needs_manual_reply"], "draft_failed": draft_failed})
    return _outcome(inbox, "ok", counts=inbox_triage.count_by_category(decisions), urgent=urgent,
                    rows=rows, overflow=overflow, cost_usd=cost)
