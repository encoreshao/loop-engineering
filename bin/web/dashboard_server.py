#!/usr/bin/env python3
"""Lightweight, read-only, localhost-only web dashboard for the GitLab daily
loop. Runs as its own always-on background daemon (a separate launchd entry
from the main loop's schedule — see launchd/com.hermes.loop-engineering-
dashboard.plist), stdlib Python only. Shows current/last run state, run
history, per-project memory, live GitLab issues/MRs, and the load/PID/
schedule status of every launchd daemon in launchd/*.plist.

Also doubles as the tiny CLI the main loop's run-loop.sh uses to record its
own state:

    python3 bin/web/dashboard_server.py write-status running
    python3 bin/web/dashboard_server.py write-status idle --exit-code 0
"""
import concurrent.futures
import fcntl
import hashlib
import html
import json
import os
import plistlib
import re
import secrets
import shlex
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

# This file lives at bin/web/dashboard_server.py; loop_config.py,
# memory_store.py, and project_memory.py are siblings in bin/, not this
# file's own directory, so they need bin/ on sys.path explicitly - unlike a
# script run directly (`python3 bin/web/dashboard_server.py`), which only
# gets its own directory auto-added, an import by another module (e.g. this
# file being imported by tests) gets no implicit path at all.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ai_cli_config
import cost
import health
import loop_config
import memory_store
import metrics
import project_memory
import topic_config

LOOP_DIR = Path(__file__).resolve().parent.parent.parent
STATUS_PATH = LOOP_DIR / "outputs" / "status.json"
HISTORY_DIR = LOOP_DIR / "outputs" / "history"
# The one place every `claude` CLI invocation across this project writes
# its raw output - run-loop.sh and run-topic-monitor-loop.sh each already
# have their own per-day outputs/history/*.log (unchanged, still used by
# Slack failure alerts and _today_log_tail), and this dashboard's own live
# chat assistant (_run_chat_job) had no log at all before this existed.
# logs/ sits outside outputs/ so it reads as "the raw process log", not
# another piece of the loop's own saved review state - see the Logs page
# (render_logs_page) and append_unified_log/read_unified_log_tail below.
LOGS_DIR = LOOP_DIR / "logs"
UNIFIED_LOG_PATH = LOGS_DIR / "loop-engineering.log"
MESSAGES_PATH = LOOP_DIR / "outputs" / "messages.json"
LAUNCHD_DIR = LOOP_DIR / "launchd"
TOPIC_MONITOR_DIR = LOOP_DIR / "outputs" / "topic-monitor"
TOPIC_MONITOR_HISTORY_DIR = TOPIC_MONITOR_DIR / "history"
TOPIC_MONITOR_STATUS_PATH = TOPIC_MONITOR_DIR / "status.json"
FAVICON_PATH = LOOP_DIR / "assets" / "favicon.ico"
RUN_LOOP_SH = LOOP_DIR / "run-loop.sh"
RUN_TOPIC_MONITOR_LOOP_SH = LOOP_DIR / "run-topic-monitor-loop.sh"
PROGRESS_PATH = LOOP_DIR / "PROGRESS.md"
README_PATH = LOOP_DIR / "README.md"
# LOOP_ENGINEERING_HOME lets a dev instance (see CLAUDE.md's "Development
# mode" section) point this at a sandbox directory instead of the real,
# possibly-live ~/.loop-engineering - read once at import, same as every
# other module-level path constant here.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
CUSTOM_INSTRUCTIONS_PATH = LOOP_ENGINEERING_HOME / "instructions.md"

GITLAB_API = Path.home() / ".encore-skills" / "skills" / "gitlab-config" / "scripts" / "gitlab_api.py"
GITLAB_CONFIG_PATH = Path.home() / ".gitlab" / "config.json"
SLACK_CONFIG_PATH = Path.home() / ".slack" / "config.json"

SKILLS_ROOT = Path.home() / ".encore-skills"
SETUP_SH = LOOP_DIR / "bin" / "scripts" / "setup.sh"
SKILLS_INSTALL_STATUS_PATH = LOOP_DIR / "outputs" / "skills_install_status.json"
SKILLS_INSTALL_LOG_PATH = HISTORY_DIR / "skills-install.log"
DASHBOARD_DAEMON_LABEL = "com.hermes.loop-engineering-dashboard"
# The exact plist filename the chat assistant must never be allowed to
# disable (see _chat_tool_daemon_disable) - disabling it would kill the
# very dashboard process serving the chat reply, with no way to
# re-enable it from a now-dead UI.
DASHBOARD_DAEMON_PLIST = DASHBOARD_DAEMON_LABEL + ".plist"

# Every external skill this loop calls out to, from the `encore-skills`
# library (github.com/encoreshao/encore-skills). `check_path` is the file
# whose presence under SKILLS_ROOT means the skill is actually installed -
# get_skills_status() below just checks it exists, the same "is it there"
# question get_daemons_status() answers for launchd jobs.
_REQUIRED_SKILLS = (
    {
        "key": "gitlab-config",
        "name": "gitlab-config",
        "description": "GitLab API access - instances, tokens, project aliases, and the local instance/project/issue cache every gitlab_api.py/gitlab_cache.py call reads from.",
        "check_path": "skills/gitlab-config/scripts/gitlab_api.py",
        "used_by": (
            "bin/web/dashboard_server.py",
            "bin/project_memory.py",
            "bin/track_new_comments.py",
            "bin/list_assigned_issues.py",
            "LOOPX_INSTRUCTIONS.md",
        ),
    },
)

DEFAULT_PORT = 8420

# A fresh random token per process start. Never persisted, never sent to any
# third party - it only ever appears embedded in pages this server itself
# renders, so a cross-origin page (which cannot read this server's response
# bodies due to the browser's same-origin policy) has no way to learn it and
# therefore cannot forge a valid state-changing request, unlike the previous
# "POST-only" mitigation which was not a real defense (a plain cross-origin
# HTML form POST needs neither JavaScript nor a CORS preflight).
_CSRF_TOKEN = secrets.token_urlsafe(32)

_CHAT_MESSAGE_HISTORY_LIMIT = 10

_CHAT_ASSISTANT_SYSTEM_PROMPT = (
    "You are the assistant embedded in the Loop X Engineering dashboard's "
    "Activity page chat box. The ONLY command you may run is "
    f"`python3 {LOOP_DIR}/bin/web/dashboard_server.py chat-tool <action> "
    "[args]` - you have no other shell, file, git, or GitLab access, and "
    "cannot see the projects the loop works on. Available actions: "
    "status, history-list, history-read <name>, "
    "memory, progress, daemon-list, daemon-enable <filename>, "
    "daemon-disable <filename>, run-now gitlab, run-now topic-monitor, "
    "run-issue <url>. Only the New message (the current turn) can "
    "trigger run-issue - a GitLab issue link that appears only in the "
    "Recent conversation history above it does not count, even if it's "
    "still within the last few turns. Only call `chat-tool run-issue "
    "<that url>` when the New message both contains a GitLab issue link "
    "AND expresses actual intent to act on it right now - e.g. \"work "
    "on\", \"fix\", \"start\", or \"handle\" this issue - not merely a "
    "mention or an informational question like \"what's the status of "
    "<url>?\". This starts a scoped, on-demand run of the loop for "
    "exactly that one issue, regardless of who it's assigned to. Never "
    "re-run an issue that was already started earlier in this "
    "conversation unless the user explicitly asks again in the New "
    "message. Never call run-issue more than once in the same reply, "
    "even if the New message contains multiple issue links - handle "
    "one at a time. If you use one of the mutating actions "
    "(daemon-enable, daemon-disable, run-now, run-issue), say plainly in "
    "your reply what you did - for run-issue, say which issue and "
    "whether it actually started (it can refuse if that project isn't "
    "tracked, or if a run is already in progress). If the question isn't "
    "about this repo's own status, history, memory, or daemons, just "
    "answer it directly as a general assistant. Keep replies short - "
    "this is a chat bubble, not a report."
)

# In-memory registry for the Activity page's live chat replies (see
# docs/superpowers/specs/2026-08-23-activity-page-live-chat-assistant-
# design.md). Deliberately NOT persisted to disk - a dashboard restart
# mid-reply simply drops the job; the browser's stream ends and the user
# can just ask again, the same way a lost network connection would.
_CHAT_JOBS = {}
_CHAT_JOBS_LOCK = threading.Lock()

# How long _iter_chat_job_chunks blocks per wakeup before giving up and
# looping again with nothing new to report - the SSE route (see
# _stream_chat_reply) turns each such empty wakeup into a keepalive
# comment line so a slow-to-start reply doesn't look like a dead
# connection to a proxy sitting in front of this dashboard (see
# bin/scripts/setup-nginx.sh's default proxy_read_timeout). Exposed as a
# module constant (read at call time, per this file's own DI convention)
# so a test can shrink it instead of waiting out a real 15s idle period.
_CHAT_STREAM_IDLE_TIMEOUT_SECONDS = 15


def _chat_job_create():
    """Registers a new empty streaming job and returns its reply_key.
    "chunks" is every text delta appended so far, in order - a late-
    connecting or reconnecting SSE stream (see _iter_chat_job_chunks)
    replays all of them before waiting for anything new, so a client that
    misses the start of a reply due to network timing never loses text.
    "final_text" is the authoritative saved reply text set by
    _chat_job_finish on success - distinct from "chunks" because chunks
    only capture text_delta events, which can diverge from the final
    consolidated `result` event actually persisted via append_message
    (e.g. anything emitted around a chat-tool call)."""
    reply_key = str(uuid.uuid4())
    with _CHAT_JOBS_LOCK:
        _CHAT_JOBS[reply_key] = {
            "chunks": [],
            "done": False,
            "error": None,
            "final_text": None,
            "cond": threading.Condition(),
        }
    return reply_key


def _chat_job_append(reply_key, text):
    """Appends one text delta to the job's buffer and wakes any stream
    currently waiting on it. No-op if the job no longer exists (e.g. it
    was already cleaned up) rather than raising - this is called from a
    background thread with nothing useful to do about a missing job."""
    with _CHAT_JOBS_LOCK:
        job = _CHAT_JOBS.get(reply_key)
    if job is None:
        return
    with job["cond"]:
        job["chunks"].append(text)
        job["cond"].notify_all()


def _chat_job_finish(reply_key, error=None, final_text=None):
    """Marks a job done (successfully, or with `error` set to a short
    message on failure), wakes any waiting stream, and schedules the
    job's removal from the registry 60 seconds later - long enough for a
    client reconnecting right after completion to still replay the full
    buffer, short enough not to leak memory across a long-running
    dashboard process. On success, `final_text` must be the exact text
    already saved via append_message - this is what the SSE route's
    terminal "done" frame sends the browser (see _stream_chat_reply), so
    the bubble the user sees always matches what a page reload would show
    instead of whatever the streamed text_delta chunks happened to
    accumulate to."""
    with _CHAT_JOBS_LOCK:
        job = _CHAT_JOBS.get(reply_key)
    if job is None:
        return
    with job["cond"]:
        job["done"] = True
        job["error"] = error
        job["final_text"] = final_text
        job["cond"].notify_all()

    def _cleanup():
        with _CHAT_JOBS_LOCK:
            _CHAT_JOBS.pop(reply_key, None)

    threading.Timer(60, _cleanup).start()


def _iter_chat_job_chunks(reply_key, idle_timeout=None):
    """Generator yielding every chunk appended to this job, live: replays
    whatever's already buffered first, then blocks (waking on
    _chat_job_append/_chat_job_finish, or every `idle_timeout` seconds
    regardless) for more, until the job is marked done - at which point it
    yields exactly one final ("done", error, final_text) tuple and
    returns. Yields nothing at all, immediately, if reply_key is unknown
    (e.g. the dashboard restarted mid-reply) - the caller (the SSE route)
    treats an immediately-exhausted generator as "job not found."

    Each wakeup that finds nothing new AND the job not yet done yields
    ("idle", None) before looping again - this is what lets the SSE route
    (_stream_chat_reply) turn a still-silent reply into a keepalive
    comment line instead of leaving the connection looking dead to a
    proxy sitting in front of it. `idle_timeout` defaults to the module
    constant _CHAT_STREAM_IDLE_TIMEOUT_SECONDS (resolved at call time, not
    def time) so a test can shrink the wait instead of waiting out a real
    15s idle period; a caller that already has buffered chunks and/or a
    finished job never observes an "idle" tuple at all, since it's drained
    immediately without ever calling wait()."""
    if idle_timeout is None:
        idle_timeout = _CHAT_STREAM_IDLE_TIMEOUT_SECONDS
    with _CHAT_JOBS_LOCK:
        job = _CHAT_JOBS.get(reply_key)
    if job is None:
        return
    sent = 0
    while True:
        with job["cond"]:
            # Only actually wait if there's nothing to report yet - a job
            # that already has buffered chunks and/or is already done (the
            # common case for a client that connects after a fast reply
            # finished) must be drained immediately, not held for a full
            # idle_timeout keepalive tick first.
            waited = False
            if sent >= len(job["chunks"]) and not job["done"]:
                job["cond"].wait(timeout=idle_timeout)
                waited = True
            pending = job["chunks"][sent:]
            sent = len(job["chunks"])
            done = job["done"]
            error = job["error"]
            final_text = job["final_text"]
        for chunk in pending:
            yield ("chunk", chunk)
        if done:
            yield ("done", error, final_text)
            return
        if waited and not pending:
            yield ("idle", None)


def read_status(status_path=STATUS_PATH):
    """Read outputs/status.json. Returns {"state": "never_run"} if the file
    doesn't exist, {"state": "unknown"} if it's corrupt JSON, else the parsed
    dict."""
    path = Path(status_path)
    if not path.exists():
        return {"state": "never_run"}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"state": "unknown"}
        return data
    except (json.JSONDecodeError, OSError):
        return {"state": "unknown"}


def write_status(state, status_path=None, **extra):
    """Write {"state": state, "updated_at": <UTC ISO8601>, **extra} to
    outputs/status.json, creating parent dirs if needed. Returns what was
    written."""
    if status_path is None:
        status_path = STATUS_PATH
    path = Path(status_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    with open(path, "w") as f:
        json.dump(payload, f, indent=2)
    return payload


def read_topic_status(status_path=None):
    """Read outputs/topic-monitor/status.json: {"topics": {<name>: {"state":
    ..., "updated_at": ..., ...}}}. Same missing/corrupt-file contract as
    read_status - {"topics": {}} for either case, never a crash."""
    if status_path is None:
        status_path = TOPIC_MONITOR_STATUS_PATH
    path = Path(status_path)
    if not path.exists():
        return {"topics": {}}
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, dict) or not isinstance(data.get("topics"), dict):
            return {"topics": {}}
        return data
    except (json.JSONDecodeError, OSError):
        return {"topics": {}}


def write_topic_status(topic_name, state, status_path=None, **extra):
    """Update one topic's entry in outputs/topic-monitor/status.json,
    leaving every other topic's entry untouched. Returns the full updated
    document, same "returns what was written" contract as write_status."""
    if status_path is None:
        status_path = TOPIC_MONITOR_STATUS_PATH
    path = Path(status_path)
    data = read_topic_status(path)
    data["topics"][topic_name] = {
        "state": state,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        **extra,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return data


def read_messages(path=None):
    """The full message thread, oldest first. [] if the file is missing or
    malformed - same best-effort contract as read_gitlab_config, so a fresh
    install with no messages yet just shows an empty thread, not a crash.
    Non-dict list elements are silently dropped for the same reason."""
    if path is None:
        path = MESSAGES_PATH
    try:
        with open(path) as f:
            data = json.load(f)
        if not isinstance(data, list):
            return []
        return [m for m in data if isinstance(m, dict)]
    except (OSError, ValueError):
        return []


def append_message(from_, text, path=None):
    """Appends one message with the current UTC timestamp. from_ is "user"
    or "loop". User messages start with seen_by_loop: False so a later
    pop_unseen_user_messages() call can find them; loop messages carry no
    such flag, since nothing ever needs to "see" the loop's own message.

    The full read-modify-write cycle is held under an exclusive
    fcntl.flock on a sibling `<path>.lock` file (not `path` itself, to
    stay out of the way of _atomic_write_json's own temp-file-and-rename
    dance). This function and pop_unseen_user_messages are called from
    genuinely different OS processes - this dashboard's own background
    chat threads (see _run_chat_job) vs. the separately-scheduled GitLab
    loop's own `python3 dashboard_server.py read-messages` invocation - so
    an in-process threading.Lock would not protect the two of them from
    each other; flock is real cross-process, POSIX file locking. Closing
    the lock file (the `with` block exiting) always releases the lock, so
    a crash mid-write can leave a stale lock file on disk but never a
    stuck lock."""
    if path is None:
        path = MESSAGES_PATH
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            messages = read_messages(path)
            entry = {
                "from": from_,
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
            if from_ == "user":
                entry["seen_by_loop"] = False
            messages.append(entry)
            _atomic_write_json(messages, path)
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def pop_unseen_user_messages(path=None):
    """Every unseen "from": "user" message, marked seen in the same atomic
    write - a second call returns [] for the same messages. This is what
    the `read-messages` CLI subcommand calls.

    Same cross-process fcntl.flock discipline as append_message, over the
    same sibling `<path>.lock` file - see that function's docstring for
    why an in-process threading.Lock wouldn't be enough here."""
    if path is None:
        path = MESSAGES_PATH
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = Path(str(path) + ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file, fcntl.LOCK_EX)
        try:
            messages = read_messages(path)
            unseen = [m for m in messages if m.get("from") == "user" and m.get("seen_by_loop") is False]
            if unseen:
                for m in messages:
                    if m.get("from") == "user" and m.get("seen_by_loop") is False:
                        m["seen_by_loop"] = True
                _atomic_write_json(messages, path)
            return unseen
        finally:
            fcntl.flock(lock_file, fcntl.LOCK_UN)


def build_chat_prompt(user_text, recent_messages):
    """Builds the positional prompt passed to `claude -p` for one chat
    turn - _CHAT_ASSISTANT_SYSTEM_PROMPT goes in separately via
    --append-system-prompt; this is just the conversation content.
    recent_messages must be read BEFORE the new user_text was appended to
    messages.json (so it's never duplicated in its own context) and
    already trimmed by the caller to the last _CHAT_MESSAGE_HISTORY_LIMIT
    entries. Each `claude -p` invocation is a fresh, stateless call with
    no session continuity, so this plain-text transcript is what makes a
    follow-up question like "now resume it" work at all."""
    if not recent_messages:
        return user_text
    lines = ["Recent conversation:"]
    for m in recent_messages:
        speaker = "User" if m.get("from") == "user" else "Assistant"
        lines.append(f"{speaker}: {m.get('text', '')}")
    lines.append("")
    lines.append(f"New message: {user_text}")
    return "\n".join(lines)


_CHAT_SUBPROCESS_TIMEOUT_SECONDS = 90


def build_chat_command(prompt):
    """Builds the argv `zsh -i -l -c "..."` wraps around, matching
    run-loop.sh's own reasoning exactly: launchd's minimal PATH doesn't
    have `claude` on it (see run-loop.sh's own comment on this), so the
    call is delegated to a real interactive login shell, which sources
    the rc files that put `claude` on PATH. shlex.join (not manual string
    concatenation) so the raw, untrusted chat text can never break out of
    its argument regardless of what characters it contains - the same
    reasoning run-loop.sh's own printf %q serialization exists for, done
    natively in Python here. --safe-mode disables CLAUDE.md/hooks/skills/
    plugins discovery (this assistant needs none of that, and it would
    otherwise inject this repo's own SessionStart hook output into every
    chat turn) while keeping normal OAuth/keychain auth - --bare looks
    similar but requires an explicit ANTHROPIC_API_KEY, which this
    machine doesn't have configured. --verbose is required by
    --output-format=stream-json, not optional (confirmed: omitting it is
    a hard error, not a silent fallback). --allowedTools alone already
    blocks anything outside the one Bash pattern below (confirmed
    empirically: an out-of-scope `ls -la /` request came back in
    permission_denials without --disallowedTools present at all) -
    --disallowedTools is added anyway as the same defense-in-depth
    run-loop.sh itself uses ("even if an allow pattern were ever loosened
    by accident, these can never run" - see that script's own comment),
    not because it's load-bearing today. No --permission-mode is passed
    at all (a prior version passed "acceptEdits", which this assistant
    has no legitimate use for - it never edits anything, and the mode
    itself should not imply any auto-approval; the safety story should
    rest on --allowedTools/--disallowedTools alone, not partly on a
    permission mode too). Grep/Glob are disallowed alongside Read/Write/
    Edit since they read file content just as much as Read does; curl/
    sh/bash/zsh/python3 -c/nc/osascript/launchctl are disallowed even
    though --allowedTools' closed Bash(...) pattern already excludes them,
    for the same defense-in-depth reasoning as git*/rm* above."""
    allowed = f"Bash(python3 {LOOP_DIR}/bin/web/dashboard_server.py chat-tool *)"
    disallowed = (
        "Read Write Edit Grep Glob "
        "Bash(git*) Bash(rm*) Bash(curl*) Bash(sh*) Bash(bash*) Bash(zsh*) "
        "Bash(python3 -c*) Bash(nc*) Bash(osascript*) Bash(launchctl*) "
        "WebFetch WebSearch"
    )
    claude_argv = [
        "claude", "-p",
        "--output-format", "stream-json",
        "--include-partial-messages",
        "--verbose",
        "--safe-mode",
        "--allowedTools", allowed,
        "--disallowedTools", disallowed,
        "--append-system-prompt", _CHAT_ASSISTANT_SYSTEM_PROMPT,
        prompt,
    ]
    command = f"timeout {_CHAT_SUBPROCESS_TIMEOUT_SECONDS} " + shlex.join(claude_argv)
    return ["zsh", "-i", "-l", "-c", command]


def _run_chat_job(reply_key, prompt, messages_path=None):
    """Runs in a background thread started by POST /activity/chat.
    Spawns the claude subprocess, reads its stdout line by line through
    parse_chat_stream_line: a ("delta", text) result streams live via
    _chat_job_append; a ("result", text, is_error) result is the final
    outcome. On success (is_error False and non-empty text), that text is
    saved as the thread's actual reply via append_message("loop", ...) -
    durable, shows up on a plain page reload exactly like any other
    message - and the job finishes with that same text as its
    authoritative final_text (see _chat_job_finish), so a live SSE client
    and a page reload always agree on what the reply actually was. A
    failing subprocess (auth failure, the timeout wrapper killing it, or
    no "result" event at all) finishes the job with an error instead, and
    nothing is appended to messages.json - a failed attempt shouldn't
    leave a confusing empty or partial loop message in the thread.

    Everything from here on (reading process.stdout, and the
    append_message call that persists a successful reply) is wrapped in
    one try/except/finally so _chat_job_finish is called exactly once no
    matter where things go wrong - this runs in a background thread, so
    ANY uncaught exception (a UnicodeDecodeError from unexpected bytes
    under text=True, any other I/O error mid-stream, or append_message
    itself raising - e.g. a full disk, or _atomic_write_json's own
    re-raise-after-unlink-on-failure) must still reach _chat_job_finish;
    otherwise the job registry's "always eventually reaches done"
    guarantee breaks and a client's SSE stream (see
    _iter_chat_job_chunks) hangs forever with no error and no done
    event, pinning that request thread indefinitely. On the read-error
    path the child process is killed rather than left for the `timeout
    90` wrapper to eventually reap it.

    Also writes a "turn started" entry to logs/loop-engineering.log up
    front, then a "reply"/"error" entry with the human-readable outcome
    once one is known (see append_unified_log) - the raw --output-format
    stream-json isn't human-readable, so this logs the same text the
    reply bubble/error actually shows, not the raw subprocess output.
    Every one of those entries' detail string also carries the AI
    provider's display name in parentheses (e.g. "reply (Claude Code)"),
    so the Logs page shows which provider produced it without opening
    the entry. That name is always _AI_CLI_DISPLAY_NAMES["claude"], not
    derived from ai_cli_config.get_selected_cli() - build_chat_command
    always invokes the `claude` binary regardless of that project-loop
    setting (which only governs run-loop.sh/run-topic-monitor-loop.sh),
    so deriving it from get_selected_cli would mislabel entries as
    "Codex CLI" on a machine configured to use codex for the loop while
    this chat assistant still actually ran claude."""
    if messages_path is None:
        messages_path = MESSAGES_PATH
    ai_cli_name = _AI_CLI_DISPLAY_NAMES["claude"]
    append_unified_log("chat-assistant", f"turn started ({ai_cli_name})")
    try:
        process = subprocess.Popen(
            build_chat_command(prompt),
            cwd=str(LOOP_DIR),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except OSError as exc:
        append_unified_log("chat-assistant", f"error ({ai_cli_name})", body=f"Could not start assistant: {exc}")
        _chat_job_finish(reply_key, error=f"Could not start assistant: {exc}")
        return

    finish_error = None
    finish_text = None
    try:
        final_text = None
        final_is_error = False
        for line in process.stdout:
            parsed = parse_chat_stream_line(line)
            if parsed is None:
                continue
            if parsed[0] == "delta":
                _chat_job_append(reply_key, parsed[1])
            elif parsed[0] == "result":
                final_text, final_is_error = parsed[1], parsed[2]
        process.wait()

        if final_text and not final_is_error:
            append_message("loop", final_text, messages_path)
            finish_text = final_text
        else:
            finish_error = final_text or "The assistant didn't return a reply."
    except Exception as exc:
        try:
            process.kill()
            process.wait()
        except Exception:
            pass
        finish_error = f"Error while reading assistant output: {exc}"
    finally:
        # The raw stream-json --output-format isn't human-readable (see
        # append_unified_log's own contract), so this logs the same
        # human-readable text the reply bubble/error actually shows, not
        # the raw subprocess output - "reply"/"error" mirrors the two
        # outcomes _chat_job_finish itself distinguishes.
        if finish_error:
            append_unified_log("chat-assistant", f"error ({ai_cli_name})", body=finish_error)
        else:
            append_unified_log("chat-assistant", f"reply ({ai_cli_name})", body=finish_text)
        _chat_job_finish(reply_key, error=finish_error, final_text=finish_text)


def list_run_history(history_dir=HISTORY_DIR):
    """Return .md filenames under outputs/history/, sorted descending (most
    recent first). Empty list if the directory doesn't exist."""
    path = Path(history_dir)
    if not path.exists():
        return []
    return sorted((p.name for p in path.iterdir() if p.suffix == ".md"), reverse=True)


def read_latest_review(loop_dir=LOOP_DIR):
    """Return the contents of outputs/daily-review.md, or "No run yet." if it
    doesn't exist."""
    path = Path(loop_dir) / "outputs" / "daily-review.md"
    if not path.exists():
        return "No run yet."
    return path.read_text()


def read_history_file(name, history_dir=HISTORY_DIR):
    """Read one history file by name. Security-critical: this serves
    user-supplied input (a URL path segment) as a filename, so path traversal
    must be airtight. Path(name).name strips any directory components before
    joining with history_dir; returns None if the resolved file doesn't exist
    or doesn't have a .md suffix."""
    safe_name = Path(name).name
    if not safe_name.endswith(".md"):
        return None
    path = Path(history_dir) / safe_name
    if not path.exists() or not path.is_file():
        return None
    return path.read_text()


def delete_history_file(name, history_dir=None):
    """Delete one history file by name. Same path-traversal discipline as
    read_history_file: Path(name).name strips any directory components
    before it ever touches the filesystem, and only a `.md` name is
    considered at all - generic over which history directory it's given,
    so the same function backs both the GitLab loop's outputs/history/ and
    the topic monitor's outputs/topic-monitor/history/. Returns (ok,
    message)."""
    if history_dir is None:
        history_dir = HISTORY_DIR
    safe_name = Path(name).name
    if not safe_name.endswith(".md"):
        return False, f"Invalid history filename: {name!r}"
    path = Path(history_dir) / safe_name
    if not path.exists() or not path.is_file():
        return False, f"{safe_name} not found"
    path.unlink()
    return True, f"Deleted {safe_name}"


_MD_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
_MD_UL_ITEM_RE = re.compile(r"^[-*+]\s+(.*)$")
_MD_OL_ITEM_RE = re.compile(r"^\d+\.\s+(.*)$")
_MD_FENCE_RE = re.compile(r"^```")
_MD_TABLE_ROW_RE = re.compile(r"^\|(.+)\|\s*$")
_MD_TABLE_SEP_RE = re.compile(r"^\|(?:\s*:?-+:?\s*\|)+\s*$")

_MD_CODE_SPAN_RE = re.compile(r"`([^`]+)`")
_MD_BOLD_RE = re.compile(r"\*\*([^*]+?)\*\*|__([^_]+?)__")
_MD_ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+?)\*(?!\*)|(?<!_)_([^_\n]+?)_(?!_)")
_MD_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)\s]+)\)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)\s]+)\)")
_MD_BARE_URL_RE = re.compile(r"https?://[^\s<>\"]+")
_MD_SLUG_STRIP_RE = re.compile(r"[^\w\s-]")
_MD_SLUG_SPACE_RE = re.compile(r"\s+")
_MD_LINK_SCHEME_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")


def _has_disallowed_link_scheme(url):
    """False for the shapes this renderer allows as a real `<a href>`: an
    explicit http(s) URL, an absolute path, a same-page `#anchor` (used by
    README.md's own table of contents and this dashboard's readme
    quicknav), or a bare relative path (`TASK.md`, `config/foo.json`, used
    throughout README.md's own cross-references). True for anything with
    another URI scheme (`javascript:`, `data:`, ...) or a protocol-relative
    `//host/path` - the latter would otherwise sail through a naive
    "starts with /" check and let the browser navigate cross-origin."""
    if url.startswith("//"):
        return True
    scheme = _MD_LINK_SCHEME_RE.match(url)
    return scheme is not None and scheme.group(1).lower() not in ("http", "https")


def _slugify_heading(text):
    """GitHub-style heading anchor slug: lowercase, drop anything that isn't
    a word character/space/hyphen, then collapse whitespace to hyphens -
    "How it works" -> "how-it-works", matching the anchors a hand-written
    `[text](#anchor)` link in one of this repo's own markdown files (e.g.
    README.md's table of contents) already assumes."""
    slug = _MD_SLUG_STRIP_RE.sub("", text.lower()).strip()
    return _MD_SLUG_SPACE_RE.sub("-", slug)


def _split_table_row(line):
    """"| a | b |" -> ["a", "b"] - strip the outer pipes then split on the
    rest, trimming each cell's surrounding whitespace."""
    return [cell.strip() for cell in line.strip()[1:-1].split("|")]


_MD_H2_RE = re.compile(r"^##\s+(.*)$")


def _markdown_h2_sections(text):
    """Every level-2 heading in `text`, in order, as (title, slug) - used to
    build the README page's "jump to section" quicknav straight from the
    document's own structure rather than a hand-maintained list that could
    drift out of sync with it."""
    return [(m.group(1), _slugify_heading(m.group(1))) for m in map(_MD_H2_RE.match, text.splitlines()) if m]


def _markdown_section_body(text, heading):
    """The body of one level-2 markdown section (every line after "##
    <heading>" up to the next "## " heading or end of text), stripped of
    surrounding blank lines - or None if that exact heading (case
    insensitive) never appears. Used to pull a specific section's content
    out of a run-history file without parsing the whole document."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        m = _MD_H2_RE.match(line)
        if m and m.group(1).strip().lower() == heading.lower():
            start = i + 1
            break
    if start is None:
        return None
    body_lines = []
    for line in lines[start:]:
        if _MD_H2_RE.match(line):
            break
        body_lines.append(line)
    return "\n".join(body_lines).strip()


def extract_history_overview(content, max_length=240):
    """The "at a glance" summary for one run-history entry: the `##
    Summary` section's body if present (the GitLab loop's daily-review.md
    always has one - see LOOPX_INSTRUCTIONS.md's End of run section), else
    the leading paragraph before the first heading (what a topic monitor
    briefing opens with instead - see TOPIC_MONITOR_INSTRUCTIONS.md's
    "write the briefing" step). One rule covers both loops' actual file
    shapes without hardcoding either one's structure by name. Truncated to
    `max_length` at a word boundary with a trailing ellipsis, since this
    renders inline in a list rather than a full page."""
    summary = _markdown_section_body(content, "Summary")
    if summary is None:
        para_lines = []
        started = False
        for line in content.splitlines():
            if _MD_H2_RE.match(line):
                break
            if line.startswith("# "):
                continue
            if not line.strip():
                if started:
                    break
                continue
            started = True
            para_lines.append(line.strip())
        summary = " ".join(para_lines)
    summary = " ".join(summary.split())
    if len(summary) <= max_length:
        return summary
    truncated = summary[:max_length].rsplit(" ", 1)[0]
    return truncated + "…"


_GITLAB_HISTORY_HIGHLIGHT_SECTIONS = (
    ("MRs opened", "MR", "MRs"),
    ("Escalations", "escalation", "escalations"),
    ("Answered directly", "answered", "answered"),
)


def _count_bullet_items(body):
    """Number of markdown bullet list items (lines starting with `-` or
    `*`) in a section body."""
    return sum(1 for line in body.splitlines() if re.match(r"^\s*[-*]\s+\S", line))


def _history_section_count(content, heading):
    """0 if `heading`'s section in one GitLab-loop history entry is missing
    or "None." (the loop's own convention for an empty section, see
    LOOPX_INSTRUCTIONS.md), else its number of bullet items (or 1 if it has
    content but no bullets). Shared by gitlab_history_tags (per-entry tags)
    and _gitlab_loop_stats (Dashboard-page totals) so both agree on what
    counts as "something happened" that day."""
    body = _markdown_section_body(content, heading)
    if not body or body.strip().rstrip(".").lower() == "none":
        return 0
    return _count_bullet_items(body) or 1


def gitlab_history_tags(content):
    """Highlight tags for one GitLab-loop history entry, derived from
    whichever of its "MRs opened"/"Escalations"/"Answered directly"
    sections are actually non-empty that day - "None." (the loop's own
    convention for an empty section, see LOOPX_INSTRUCTIONS.md) means
    nothing to tag. A day with none of the three becomes a single "Quiet
    day" tag, rather than no tags at all, so a quiet day still reads as
    something rather than a blank row."""
    tags = []
    for heading, singular, plural in _GITLAB_HISTORY_HIGHLIGHT_SECTIONS:
        count = _history_section_count(content, heading)
        if not count:
            continue
        tags.append(f"{count} {singular if count == 1 else plural}")
    return tags or ["Quiet day"]


def _gitlab_loop_stats(history_dir=None):
    """Aggregate the GitLab loop's outputs/history/<YYYY-MM-DD>.md entries
    for the Dashboard page's stats section: total runs logged, and
    all-time MRs-opened/escalations/answered-directly counts (reusing
    _history_section_count, so these totals always agree with the tags
    shown on the Run History page). Also returns a `strip` of the most
    recent 7 calendar days, oldest first, each {"date": <ISO date>,
    "outcome": "escalation" | "mr" | "quiet" | None} - None means no run
    was logged that day. A day with both an escalation and an MR is
    labelled "escalation", since that's the one that needs attention.
    Filenames encode their own date (see LOOPX_INSTRUCTIONS.md), so the
    date comes straight from the name rather than file mtime."""
    if history_dir is None:
        history_dir = HISTORY_DIR
    names = list_run_history(history_dir)

    totals = {"runs": len(names), "mrs_opened": 0, "escalations": 0, "answered": 0}
    outcome_by_date = {}
    for name in names:
        content = read_history_file(name, history_dir) or ""
        mrs = _history_section_count(content, "MRs opened")
        escalations = _history_section_count(content, "Escalations")
        answered = _history_section_count(content, "Answered directly")
        totals["mrs_opened"] += mrs
        totals["escalations"] += escalations
        totals["answered"] += answered

        date_str = name[: -len(".md")]
        if escalations:
            outcome_by_date[date_str] = "escalation"
        elif mrs:
            outcome_by_date[date_str] = "mr"
        else:
            outcome_by_date[date_str] = "quiet"

    today = datetime.now(timezone.utc).date()
    strip = []
    for offset in range(6, -1, -1):
        date_str = (today - timedelta(days=offset)).isoformat()
        strip.append({"date": date_str, "outcome": outcome_by_date.get(date_str)})

    return {**totals, "strip": strip}


def topic_history_tags(name, content):
    """Tags for one topic-monitor history entry: the topic's own name
    (parsed from the "<date>-<topic-name>.md" filename convention every
    briefing is saved under - see docs/tasks/topic-monitor-loop.md), plus
    "Quiet" when the briefing explicitly found nothing notable (the exact
    phrasing TOPIC_MONITOR_INSTRUCTIONS.md's failure/quiet-day step asks
    the loop to write)."""
    m = re.match(r"^\d{4}-\d{2}-\d{2}-(.+)\.md$", name)
    tags = [m.group(1)] if m else []
    if "nothing notable" in content.lower() or "no notable" in content.lower():
        tags.append("Quiet")
    return tags


def _markdown_inline(escaped_text, gitlab_url_prefixes=None):
    """Apply inline markdown formatting to text that has ALREADY been through
    html.escape - the caller's job, not this function's, since callers vary
    in what they hand off (a whole line vs. a joined paragraph). Because the
    input is pre-escaped, none of `<`, `>`, `&`, `"`, `'` can appear
    literally, so every regex below only ever matches markdown punctuation
    the escaping left untouched (`*_\\[\\]()\\``) - there's no way for
    embedded raw HTML to survive into the output.

    Every substitution that produces actual HTML (code spans, links,
    gitlab-issue references) is stashed behind a placeholder and restored
    only at the very end, AFTER bold/italic run - not just for the
    "formatting punctuation inside a code span" case, but because the HTML
    those substitutions emit contains punctuation of its own: an inserted
    `target="_blank"` has exactly one underscore, and if the bold/italic
    pass ran first, that lone underscore could pair up with an unrelated
    one later in the same paragraph (e.g. a bare word like `ht_documents`)
    and splice an <em> into the middle of the attribute, corrupting the tag.
    Keeping every inserted `<...>` behind a placeholder until bold/italic
    have already finished means those passes only ever see the original
    escaped text, never markup this function itself produced.
    """
    stashes = []

    def stash(html_fragment):
        stashes.append(html_fragment)
        return f"\x00{len(stashes) - 1}\x00"

    text = _MD_CODE_SPAN_RE.sub(lambda m: stash(f"<code>{m.group(1)}</code>"), escaped_text)

    if gitlab_url_prefixes:
        gitlab_ref_re = re.compile(
            r"\b(" + "|".join(re.escape(a) for a in sorted(gitlab_url_prefixes, key=len, reverse=True)) + r")\s+#(\d+)\b"
        )

        def make_gitlab_link(m):
            url = f"{gitlab_url_prefixes[m.group(1)]}/-/issues/{m.group(2)}"
            return stash(f'<a href="{url}" rel="noopener" target="_blank">{m.group(0)}</a>')

        text = gitlab_ref_re.sub(make_gitlab_link, text)

    def make_image(m):
        alt, url = m.group(1), m.group(2)
        if not _has_disallowed_link_scheme(url):
            return stash(f'<img src="{url}" alt="{alt}" loading="lazy">')
        return m.group(0)

    # Must run before _MD_LINK_RE: `![alt](url)` also matches the plain
    # link pattern (`[alt](url)`) once the leading `!` is ignored, so an
    # unhandled image would otherwise render as a literal "!" in front of
    # a link instead of an <img>.
    text = _MD_IMAGE_RE.sub(make_image, text)

    def make_link(m):
        text_part, url = m.group(1), m.group(2)
        if not _has_disallowed_link_scheme(url):
            return stash(f'<a href="{url}" rel="noopener" target="_blank">{text_part}</a>')
        return m.group(0)

    text = _MD_LINK_RE.sub(make_link, text)

    def make_bare_link(m):
        # A bare URL mentioned in prose - never wrapped in [text](url) -
        # used to render as inert plain text. Runs after _MD_LINK_RE, so a
        # URL that's already inside an explicit markdown link has already
        # been replaced by that link's own stash placeholder and can't
        # match here a second time. Trailing punctuation almost never
        # belongs to the URL itself (a review sentence ending "...
        # https://x.com/y." or wrapping one in "(https://x.com/y)"), so
        # it's peeled off and left outside the <a> tag.
        url = m.group(0)
        trailing = ""
        while url and url[-1] in ".,;:!?)]}":
            trailing = url[-1] + trailing
            url = url[:-1]
        if not url:
            return m.group(0)
        return stash(f'<a href="{url}" rel="noopener" target="_blank">{url}</a>') + trailing

    text = _MD_BARE_URL_RE.sub(make_bare_link, text)
    text = _MD_BOLD_RE.sub(lambda m: f"<strong>{m.group(1) or m.group(2)}</strong>", text)
    text = _MD_ITALIC_RE.sub(lambda m: f"<em>{m.group(1) or m.group(2)}</em>", text)

    # Reverse order matters: a later stash (e.g. a link) can contain an
    # earlier stash's still-unresolved placeholder nested inside it (a
    # code span stashed inside a link's text, e.g. [`code`](url) - the
    # code span is stashed first at a lower index, then make_link stashes
    # the whole `<a>...</a>` - placeholder and all - at a higher index).
    # Restoring low-to-high would substitute the link placeholder in
    # `text` only after the loop had already passed the code span's index,
    # leaving that inner placeholder in the final output unresolved.
    # High-to-low guarantees the outer (higher-index) placeholder is
    # substituted into `text` before its own index comes up for the inner
    # marker it exposes.
    for i, fragment in reversed(list(enumerate(stashes))):
        text = text.replace(f"\x00{i}\x00", fragment)
    return text


def render_markdown(text, gitlab_url_prefixes=None):
    """Minimal, dependency-free Markdown -> HTML for the loop's own review
    reports, history files, and README.md. Deliberately covers only what
    those actually use - headings (each gets a GitHub-style `id` slug, so a
    hand-written `[text](#anchor)` link elsewhere works), paragraphs, flat
    (unnested) lists, GFM tables, fenced code blocks, bold/italic, inline
    code, links (both `[text](url)` and bare `https://...` mentions, which
    get auto-linked even without markdown link syntax), and images
    (`![alt](url)`, e.g. README.md's banner and shields.io badges) - rather
    than pulling in a markdown library, matching this project's stdlib-only,
    no-external-deps convention (see the _STYLE comment above). Raw HTML
    (e.g. a hand-written `<img>` tag) is deliberately NOT passed through -
    see the html.escape note below - so README.md's own banner must use
    markdown image syntax, not an `<img>` tag, to render inside the
    dashboard's README page.

    Every literal chunk of text is passed through html.escape() before any
    markdown syntax is interpreted, so the output is safe to embed as-is:
    a review that quotes a GitLab issue title containing `<script>` renders
    that title as text, never as a tag, regardless of what markdown-like
    punctuation happens to sit next to it.

    `gitlab_url_prefixes` also turns "<alias> #<iid>" mentions into links to
    the actual GitLab issue - defaults to this machine's real config via
    gitlab_issue_url_prefixes() (computed once per call, not per line/block),
    resolved lazily so callers/tests can pass an explicit dict instead."""
    if gitlab_url_prefixes is None:
        gitlab_url_prefixes = gitlab_issue_url_prefixes()
    lines = text.splitlines()
    blocks = []
    i = 0
    while i < len(lines):
        line = lines[i]

        if _MD_FENCE_RE.match(line):
            i += 1
            code_lines = []
            while i < len(lines) and not _MD_FENCE_RE.match(lines[i]):
                code_lines.append(lines[i])
                i += 1
            i += 1  # skip the closing fence (or EOF if the fence was never closed)
            blocks.append(f"<pre><code>{html.escape(chr(10).join(code_lines))}</code></pre>")
            continue

        heading = _MD_HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            slug = _slugify_heading(heading.group(2))
            inline = _markdown_inline(html.escape(heading.group(2)), gitlab_url_prefixes)
            blocks.append(f'<h{level} id="{slug}">{inline}</h{level}>')
            i += 1
            continue

        if _MD_TABLE_ROW_RE.match(line) and i + 1 < len(lines) and _MD_TABLE_SEP_RE.match(lines[i + 1].strip()):
            header_cells = _split_table_row(line)
            i += 2  # skip the header row and the |---|---| separator
            body_rows = []
            while i < len(lines) and _MD_TABLE_ROW_RE.match(lines[i]):
                body_rows.append(_split_table_row(lines[i]))
                i += 1
            thead = "".join(
                f"<th>{_markdown_inline(html.escape(c), gitlab_url_prefixes)}</th>" for c in header_cells
            )
            tbody = "".join(
                "<tr>" + "".join(
                    f"<td>{_markdown_inline(html.escape(c), gitlab_url_prefixes)}</td>" for c in row
                ) + "</tr>"
                for row in body_rows
            )
            blocks.append(
                "<div class='table-wrap'><table class='daemons md-table'>"
                f"<thead><tr>{thead}</tr></thead><tbody>{tbody}</tbody></table></div>"
            )
            continue

        ul_item = _MD_UL_ITEM_RE.match(line)
        if ul_item:
            items = []
            while i < len(lines) and (m := _MD_UL_ITEM_RE.match(lines[i])):
                items.append(f"<li>{_markdown_inline(html.escape(m.group(1)), gitlab_url_prefixes)}</li>")
                i += 1
            blocks.append(f"<ul>{''.join(items)}</ul>")
            continue

        ol_item = _MD_OL_ITEM_RE.match(line)
        if ol_item:
            items = []
            while i < len(lines) and (m := _MD_OL_ITEM_RE.match(lines[i])):
                items.append(f"<li>{_markdown_inline(html.escape(m.group(1)), gitlab_url_prefixes)}</li>")
                i += 1
            blocks.append(f"<ol>{''.join(items)}</ol>")
            continue

        if not line.strip():
            i += 1
            continue

        para_lines = []
        while i < len(lines) and lines[i].strip() and not (
            _MD_FENCE_RE.match(lines[i]) or _MD_HEADING_RE.match(lines[i])
            or _MD_UL_ITEM_RE.match(lines[i]) or _MD_OL_ITEM_RE.match(lines[i])
        ):
            para_lines.append(lines[i])
            i += 1
        blocks.append(f"<p>{_markdown_inline(html.escape(' '.join(para_lines)), gitlab_url_prefixes)}</p>")

    return "\n".join(blocks)


def parse_chat_stream_line(line):
    """Parses one line of `claude -p --output-format stream-json
    --include-partial-messages --verbose` stdout. Returns ("delta", text)
    for a live text chunk, ("result", text, is_error) for the final
    consolidated reply (from the terminal {"type": "result", ...} event),
    or None for every other event type (system/init, rate_limit_event,
    the non-text-delta stream_event subtypes, the intermediate
    "assistant" event that duplicates what the deltas already built up) -
    none of those matter to a chat bubble. A blank line or invalid JSON
    also returns None rather than raising - this reads a live subprocess's
    stdout, which can include a trailing blank line or (if the process is
    killed mid-write, e.g. by the `timeout` wrapper) a truncated final
    line."""
    line = line.strip()
    if not line:
        return None
    try:
        event = json.loads(line)
    except ValueError:
        return None
    event_type = event.get("type")
    if event_type == "stream_event":
        # `or {}` (not a plain .get(..., {}) default) because the key can
        # be PRESENT with an explicit JSON null value (`"event": null`) -
        # a .get default only kicks in when the key is missing entirely,
        # so a malformed line shaped like that would otherwise raise
        # AttributeError on the next .get() call. Since Fix 2 (see
        # _run_chat_job) now turns any exception in the stdout-read loop
        # into a full-reply failure, letting one malformed line raise here
        # would abort an entire reply instead of just being ignored like
        # every other unrecognized event shape already is.
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                return ("delta", delta.get("text", ""))
        return None
    if event_type == "result":
        return ("result", event.get("result", "") or "", bool(event.get("is_error")))
    return None


def _sse_frame(event, data):
    """One Server-Sent Events frame. `data` is always sent as a single
    JSON-encoded field (never raw interpolated text) so a chat reply
    chunk containing embedded newlines can't be misread as the blank-line
    frame terminator SSE itself uses."""
    payload = json.dumps(data)
    return f"event: {event}\ndata: {payload}\n\n".encode("utf-8")


def get_project_learnings(config_path=None):
    """{alias: [learning_entry, ...]} for each configured project alias. If
    the config file doesn't exist yet, return {} rather than crashing — the
    dashboard should still render something useful before setup is
    complete."""
    if config_path is None:
        config_path = loop_config.DEFAULT_CONFIG_PATH
    try:
        config = loop_config.load_config(config_path)
    except FileNotFoundError:
        return {}
    default_instance = config["gitlab_instance"]
    result = {}
    for alias, project in config["projects"].items():
        instance = project.get("instance", default_instance)
        result[alias] = project_memory.get_learnings(instance, project["project_id"])
    return result


def get_project_memory(config_path=None, memory_root=None):
    """{alias: {"legacy": [...same shape as get_project_learnings...],
    "tasks": [...memory_store.list_task_memories(alias)...]}} for each
    configured project alias. Empty dict if the config file doesn't exist
    yet - same best-effort contract as get_project_learnings, which this
    wraps for the legacy half."""
    legacy = get_project_learnings(config_path)
    return {
        alias: {
            "legacy": entries,
            "tasks": memory_store.list_task_memories(alias, root=memory_root),
        }
        for alias, entries in legacy.items()
    }


def get_configured_topics(config_path=None):
    """Every configured topic's {name, label, brief, slack_bundle}, or []
    if ~/.loop-engineering/topics.json doesn't exist yet or is malformed -
    same best-effort contract as get_project_learnings for projects.json."""
    if config_path is None:
        config_path = topic_config.DEFAULT_CONFIG_PATH
    try:
        return topic_config.load_config(config_path)
    except (FileNotFoundError, ValueError):
        return []


def list_topic_history(topic_name=None, history_dir=None):
    """Filenames under outputs/topic-monitor/history/, sorted descending
    (most recent first) - same convention as list_run_history. Filters to
    one topic's briefings when `topic_name` is given; files are named
    <date>-<topic_name>.md.

    The filter anchors the whole filename against <date>-<topic_name>.md
    rather than just testing endswith("-<topic_name>.md"): topic names can
    be suffixes of one another (news/ai-news, rust/async-rust), and an
    endswith test would hand topic "news" every one of "ai-news"'s
    briefings too. Requiring the part before the topic name to be exactly a
    YYYY-MM-DD date is what makes the two sets disjoint."""
    if history_dir is None:
        history_dir = TOPIC_MONITOR_HISTORY_DIR
    path = Path(history_dir)
    if not path.exists():
        return []
    names = (p.name for p in path.iterdir() if p.suffix == ".md")
    if topic_name is not None:
        pattern = re.compile(r"\d{4}-\d{2}-\d{2}-" + re.escape(topic_name) + r"\.md")
        names = (n for n in names if pattern.fullmatch(n))
    return sorted(names, reverse=True)


def gitlab_issue_url_prefixes(loop_config_path=None, gitlab_config_path=None):
    """Best-effort {alias: 'https://host/namespace/project'} for every
    project this loop's own config.json knows about, used to turn plain-text
    "<alias> #<iid>" mentions in review reports into real links to the
    GitLab issue.

    Combines two separately-configured files: this loop's own project
    aliases -> project_id (loop_config, same as get_project_learnings), and
    the unrelated gitlab-config skill's instance -> base URL mapping
    (GITLAB_CONFIG_PATH, the same file gitlab_api.py reads for API auth).
    Only that file's `url` field is ever read here - never `token`, which
    that file also holds, and which must never end up in an HTML response.

    Returns {} if either file is missing or malformed: linkifying issue
    mentions is a nice-to-have, not something a review page should ever
    fail to render over."""
    if loop_config_path is None:
        loop_config_path = loop_config.DEFAULT_CONFIG_PATH
    if gitlab_config_path is None:
        gitlab_config_path = GITLAB_CONFIG_PATH
    try:
        loop_cfg = loop_config.load_config(loop_config_path)
    except (FileNotFoundError, ValueError):
        return {}

    try:
        with open(gitlab_config_path) as f:
            gitlab_cfg = json.load(f)
    except (OSError, ValueError):
        return {}
    instance_urls = {
        name: inst["url"] for name, inst in gitlab_cfg.get("instances", {}).items() if inst.get("url")
    }
    default_instance = loop_cfg.get("gitlab_instance")

    result = {}
    for alias, project in loop_cfg.get("projects", {}).items():
        project_id = project.get("project_id")
        if not project_id:
            continue
        base_url = instance_urls.get(project.get("instance", default_instance))
        if not base_url:
            continue
        result[alias] = f"{base_url.rstrip('/')}/{project_id}"
    return result


def _resolve_gitlab_issue_url(url, prefixes):
    """Match a pasted GitLab issue URL against gitlab_issue_url_prefixes()'s
    {alias: base_url} map. Returns (alias, issue_iid) on a match, or None
    if it doesn't match any tracked project's issue URL shape - wrong
    host, wrong project, or not an issue URL at all (e.g. a merge
    request link). Pure function, no I/O, so the caller (_chat_tool_run_issue)
    controls exactly which prefixes are considered."""
    url = url.strip()
    for alias, base_url in prefixes.items():
        pattern = re.escape(base_url.rstrip("/")) + r"/-/issues/(\d+)/?$"
        match = re.match(pattern, url)
        if match:
            return alias, int(match.group(1))
    return None


def _run_gitlab_api(alias, subcommand):
    result = subprocess.run(
        [sys.executable, str(GITLAB_API), subcommand, alias, "opened"],
        capture_output=True, text=True, check=True, timeout=15,
    )
    return json.loads(result.stdout)


def _relative_time(iso_timestamp):
    """Human-friendly "X ago" for a GitLab ISO-8601 timestamp (always UTC,
    "Z"-suffixed) - e.g. "2h ago", "3d ago" - instead of the raw
    "2026-08-20T08:59:18.756Z" GitLab's API returns. Falls back to the raw
    string on anything unparseable rather than raising."""
    if not iso_timestamp:
        return ""
    try:
        dt = datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00"))
    except ValueError:
        return iso_timestamp
    seconds = (datetime.now(timezone.utc) - dt).total_seconds()
    if seconds < 60:
        return "just now"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}h ago"
    days = hours / 24
    if days < 30:
        return f"{int(days)}d ago"
    months = days / 30
    if months < 12:
        return f"{int(months)}mo ago"
    return f"{int(days / 365)}y ago"


def _message_date(iso_timestamp):
    """The calendar date (UTC) a message's ISO-8601 timestamp falls on, or
    None if it's missing/unparseable - used to decide where the Dashboard
    page's message thread needs a day separator. Same tolerant-parsing
    contract as _relative_time (never raises)."""
    if not iso_timestamp:
        return None
    try:
        return datetime.fromisoformat(iso_timestamp.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _day_separator_label(day, today):
    """"Today" / "Yesterday" / "Aug 25" (plus ", <year>" once it's not this
    year) for one message-thread day separator."""
    delta = (today - day).days
    if delta == 0:
        return "Today"
    if delta == 1:
        return "Yesterday"
    if day.year == today.year:
        return day.strftime("%b %-d")
    return day.strftime("%b %-d, %Y")


def _describe_gitlab_api_error(exc):
    """Short, human-readable reason a _run_gitlab_api call failed. Prefers
    the subprocess's own stderr (e.g. "Error: Instance 'x' not found") over
    a bare CalledProcessError's str(), which only says "returned non-zero
    exit status" and never says why - that's the message worth showing."""
    stderr = getattr(exc, "stderr", "") or ""
    for line in stderr.strip().splitlines():
        line = line.strip()
        if line and not line.startswith("Warning:"):
            return line
    return str(exc)


def _fetch_alias_gitlab_state(alias, username):
    """One alias's {"issues": [...], "mrs": [...], "issues_error": str|None,
    "mrs_error": str|None} - the per-alias body get_live_gitlab_state used to
    run inline, factored out so it can run in its own worker thread."""
    entry = {}
    try:
        issues = _run_gitlab_api(alias, "list-issues")
        entry["issues"] = [
            i for i in issues
            if any(a.get("username") == username for a in i.get("assignees", []))
        ]
        entry["issues_error"] = None
    except Exception as e:
        entry["issues"] = []
        entry["issues_error"] = _describe_gitlab_api_error(e)
    try:
        entry["mrs"] = _run_gitlab_api(alias, "list-mrs")
        entry["mrs_error"] = None
    except Exception as e:
        entry["mrs"] = []
        entry["mrs_error"] = _describe_gitlab_api_error(e)
    return entry


def get_live_gitlab_state(config_path=None):
    """{alias: {"issues": [...], "mrs": [...], "issues_error": str|None,
    "mrs_error": str|None}} for each configured alias, filtered to the
    configured assignee. On any per-alias failure (network error, non-zero
    exit, bad JSON), that list becomes empty and its "_error" sibling is set
    to a human-readable reason - one broken project must not break the whole
    dashboard, and a failure must never be mistaken for zero real issues.

    Each alias's two _run_gitlab_api calls (issues, mrs) are a real
    subprocess + GitLab API round trip, up to 15s each on GITLAB_API's own
    timeout - fetching aliases one after another made this whole call take
    roughly (number of aliases) times as long as the slowest one. Running
    each alias in its own thread instead bounds the wall-clock time to
    roughly the single slowest alias, since these calls spend virtually all
    their time waiting on I/O (subprocess + network), not the GIL."""
    if config_path is None:
        config_path = loop_config.DEFAULT_CONFIG_PATH
    try:
        config = loop_config.load_config(config_path)
    except FileNotFoundError:
        return {}
    username = config["assignee_username"]
    aliases = list(config["projects"])
    if not aliases:
        return {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(aliases)) as executor:
        futures = {alias: executor.submit(_fetch_alias_gitlab_state, alias, username) for alias in aliases}
        return {alias: future.result() for alias, future in futures.items()}


def read_gitlab_config(path=None):
    """{} if the file is missing or malformed - same best-effort contract as
    gitlab_issue_url_prefixes's existing try/except, so a first-run machine
    with no config yet just sees an empty Settings page, not a crash."""
    if path is None:
        path = GITLAB_CONFIG_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def _atomic_write_json(config, path):
    """Write `config` as JSON to `path` atomically: json.dump to a
    NamedTemporaryFile in the same directory (guaranteeing os.replace is
    same-filesystem, hence atomic), chmod it to 0o600 before the replace
    (both config files this is used for hold secrets), then os.replace over
    the target. A crash mid-write leaves only the temp file orphaned, never
    a half-written target."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        json.dump(config, tmp, indent=2)
        tmp.close()
        os.chmod(tmp.name, 0o600)
        os.replace(tmp.name, path)
    except BaseException:
        os.unlink(tmp.name)
        raise


def write_gitlab_config(config, path=None):
    if path is None:
        path = GITLAB_CONFIG_PATH
    _atomic_write_json(config, path)


def read_slack_config(path=None):
    """Same best-effort contract as read_gitlab_config."""
    if path is None:
        path = SLACK_CONFIG_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_slack_config(config, path=None):
    if path is None:
        path = SLACK_CONFIG_PATH
    _atomic_write_json(config, path)


def read_loop_projects_config(path=None):
    """Same best-effort contract as read_gitlab_config - {} if
    ~/.loop-engineering/projects.json is missing or malformed, so the
    Settings page's Tracked Projects section renders on a fresh machine
    instead of crashing. loop_config.load_config raises on the same cases
    by design (the loop itself should fail fast); this is the UI's own,
    more forgiving read."""
    if path is None:
        path = loop_config.DEFAULT_CONFIG_PATH
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_loop_projects_config(config, path=None):
    if path is None:
        path = loop_config.DEFAULT_CONFIG_PATH
    _atomic_write_json(config, path)


def upsert_tracked_project(alias, project_id, local_path, target_branch, install_cmd, lint_cmd, test_cmd,
                            instance="", config_path=None, original_alias=""):
    """Add or update one entry in ~/.loop-engineering/projects.json's
    `projects` map. `instance` blank means "use this config's default
    gitlab_instance" - stored by omitting the key entirely (never as an
    empty string), matching loop_config.get_project's setdefault-based
    fallback. `original_alias`, when non-empty and different from `alias`,
    renames the existing entry at that key to `alias` instead of adding a
    second one - the Tracked Projects edit form sends it as a hidden field
    alongside the now-editable alias input."""
    if config_path is None:
        config_path = loop_config.DEFAULT_CONFIG_PATH
    alias = alias.strip()
    original_alias = original_alias.strip()
    project_id = project_id.strip()
    instance = instance.strip()
    if not alias:
        return False, "Project alias is required"
    if not project_id:
        return False, "Project ID is required"
    config = read_loop_projects_config(config_path)
    projects = config.setdefault("projects", {})
    renaming = bool(original_alias) and original_alias != alias
    if renaming:
        if original_alias not in projects:
            return False, f"Unknown project: {original_alias}"
        if alias in projects:
            return False, f"Project alias already in use: {alias}"
        del projects[original_alias]
    is_new = alias not in projects
    entry = {
        "project_id": project_id,
        "local_path": local_path.strip(),
        "target_branch": target_branch.strip(),
        "install_cmd": install_cmd.strip(),
        "lint_cmd": lint_cmd.strip(),
        "test_cmd": test_cmd.strip(),
    }
    if instance:
        entry["instance"] = instance
    projects[alias] = entry
    write_loop_projects_config(config, config_path)
    if renaming:
        return True, f"Renamed project {original_alias} to {alias}"
    return True, f"{'Added' if is_new else 'Updated'} project {alias}"


def delete_tracked_project(alias, config_path=None):
    if config_path is None:
        config_path = loop_config.DEFAULT_CONFIG_PATH
    config = read_loop_projects_config(config_path)
    if alias not in config.get("projects", {}):
        return False, f"Unknown project: {alias}"
    del config["projects"][alias]
    write_loop_projects_config(config, config_path)
    return True, f"Deleted project {alias}"


def update_loop_project_settings(assignee_username, worktree_root, gitlab_instance,
                                  config_path=None, gitlab_config_path=None):
    """Updates the three top-level fields in ~/.loop-engineering/projects.json.
    `gitlab_instance` must already exist in ~/.gitlab/config.json's
    instances - it's used elsewhere (get_project_learnings,
    gitlab_issue_url_prefixes, the loop's own cache lookups) as a key into
    that file, so an unknown value would silently break all of those."""
    if config_path is None:
        config_path = loop_config.DEFAULT_CONFIG_PATH
    if gitlab_config_path is None:
        gitlab_config_path = GITLAB_CONFIG_PATH
    assignee_username = assignee_username.strip()
    worktree_root = worktree_root.strip()
    gitlab_instance = gitlab_instance.strip()
    if not assignee_username:
        return False, "GitLab username is required"
    if not worktree_root:
        return False, "Worktree root is required"
    gitlab_config = read_gitlab_config(gitlab_config_path)
    if gitlab_instance not in gitlab_config.get("instances", {}):
        return False, f"Unknown instance: {gitlab_instance}"
    config = read_loop_projects_config(config_path)
    config["assignee_username"] = assignee_username
    config["worktree_root"] = worktree_root
    config["gitlab_instance"] = gitlab_instance
    config.setdefault("projects", {})
    write_loop_projects_config(config, config_path)
    return True, "Updated project settings"


def _custom_select(name, options, selected, empty_label=None, onchange=None):
    """Renders a <select name=...> as this dashboard's custom-styled
    dropdown instead of the browser's native popup (see the .custom-select
    CSS/JS in _render_shell). The real <select> stays in the DOM, just
    hidden, so the form still submits a plain `name=value` pair - the
    trigger/listbox next to it is what the user actually sees and clicks.
    `options` is any iterable of strings used as both value and label.
    `empty_label`, if given, prepends a value='' option with that label -
    used for optional selects like "(use instance default)". `onchange`,
    if given, is a raw JS expression attached to the underlying native
    <select> - picking a custom-dropdown option sets that select's `.value`
    and dispatches a real `change` event for it (see the global
    `selectOption` script in _render_shell), so this fires exactly like a
    native <select onchange> would."""
    options = list(options)
    pairs = ([("", empty_label)] if empty_label is not None else []) + [(v, v) for v in options]
    selected_value = selected or ""
    option_tags = "".join(
        f"<option value='{html.escape(v)}'{' selected' if v == selected_value else ''}>{html.escape(l)}</option>"
        for v, l in pairs
    )
    menu_items = "".join(
        f"<div class='custom-select-option{' is-selected' if v == selected_value else ''}' role='option' "
        f"tabindex='-1' data-value='{html.escape(v)}'>{html.escape(l)}</div>"
        for v, l in pairs
    )
    label = next((l for v, l in pairs if v == selected_value), (pairs[0][1] if pairs else ""))
    onchange_attr = f" onchange=\"{html.escape(onchange, quote=True)}\"" if onchange else ""
    return (
        "<div class='custom-select'>"
        f"<select name='{html.escape(name)}' class='custom-select-native'{onchange_attr}>{option_tags}</select>"
        "<button type='button' class='custom-select-trigger' aria-haspopup='listbox' aria-expanded='false'>"
        f"<span class='custom-select-value'>{html.escape(label)}</span>"
        "<span class='material-symbols-outlined' aria-hidden='true'>expand_more</span>"
        "</button>"
        f"<div class='custom-select-menu' role='listbox' hidden>{menu_items}</div>"
        "</div>"
    )


def _cli_available(name):
    """Whether the `name` CLI binary (claude/codex) resolves on PATH,
    checked via a real login shell rather than shutil.which. This
    dashboard runs as the com.hermes.loop-engineering-dashboard launchd
    agent, which - like run-loop.sh's own agent - starts with launchd's
    minimal PATH (no ~/.local/bin, no Homebrew paths); shutil.which
    against that minimal PATH would report both CLIs "not found" even
    when they're installed and working fine for the loop scripts, which
    resolve them the same way this does (see run-loop.sh's comment on
    delegating to `zsh -i -l` for why)."""
    try:
        result = subprocess.run(
            ["zsh", "-i", "-l", "-c", f"command -v {name}"],
            capture_output=True, timeout=5, text=True,
        )
        return result.returncode == 0 and bool(result.stdout.strip())
    except (subprocess.SubprocessError, OSError):
        return False


def _mask_secret(secret):
    """"••••" + the last 4 characters when the secret is long enough to have
    a safe suffix to show; plain "••••" (no suffix) when it's shorter than
    the mask itself - showing all 3 characters of a 3-char "secret" via a
    last-4 slice would just be the whole secret with extra dots in front."""
    secret = str(secret)
    if len(secret) >= 4:
        return "••••" + secret[-4:]
    return "••••"


def _loaded_by_label(launchctl_output=None):
    """{label: (pid, status)} for every job launchd currently has loaded.

    `launchctl list` output is tab-separated PID\tStatus\tLabel per line
    (plus a header line); this builds the map from whatever's actually
    there rather than assuming an exact column format.

    launchctl_output is None by default, meaning "actually call `launchctl
    list`"; callers (and tests) pass a literal string instead so this is
    usable without mocking subprocess. Shared by get_daemons_status, which
    reports load state for the Daemons page, and update_daemon_schedule,
    which must not reload a daemon that isn't loaded.
    """
    if launchctl_output is None:
        try:
            result = subprocess.run(
                ["launchctl", "list"], capture_output=True, text=True, timeout=5,
            )
            launchctl_output = result.stdout
        except Exception:
            launchctl_output = ""

    loaded = {}
    for line in launchctl_output.splitlines():
        fields = line.split("\t")
        if len(fields) < 3:
            continue
        pid, status, label = fields[0], fields[1], fields[-1]
        loaded[label] = (pid, status)
    return loaded


def get_daemons_status(launchd_dir=LAUNCHD_DIR, launchctl_output=None):
    """Discover every *.plist file under launchd_dir and report, for each,
    whether launchd currently has it loaded (and its PID if so), what it
    runs, and its schedule/always-on configuration.

    Generic over whatever plist files exist in launchd/ - this project has
    grown a second daemon (the dashboard's own always-on process) alongside
    the main loop's schedule, and any future one should show up here without
    code changes.

    launchctl_output is None by default, meaning "actually call `launchctl
    list`"; tests pass a literal string instead so this is unit-testable
    without mocking subprocess.
    """
    path = Path(launchd_dir)
    if not path.exists():
        return []

    loaded_by_label = _loaded_by_label(launchctl_output)

    daemons = []
    for plist_path in sorted(path.glob("*.plist")):
        entry = {"file": plist_path.name}
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            if not isinstance(data, dict):
                raise ValueError(f"plist root is a {type(data).__name__}, expected a dict")
            label = data.get("Label")
        except Exception as e:
            entry["error"] = str(e)
            daemons.append(entry)
            continue
        loaded = False
        pid = None
        if label is not None and label in loaded_by_label:
            loaded = True
            raw_pid, _status = loaded_by_label[label]
            pid = raw_pid if raw_pid and raw_pid != "-" else None

        entry.update({
            "label": label,
            "loaded": loaded,
            "pid": pid,
            "program_arguments": data.get("ProgramArguments", []),
            "run_at_load": bool(data.get("RunAtLoad", False)),
            "keep_alive": bool(data.get("KeepAlive", False)),
            "schedule": data.get("StartCalendarInterval"),
            "stdout_path": data.get("StandardOutPath"),
            "stderr_path": data.get("StandardErrorPath"),
        })
        daemons.append(entry)

    return daemons


def get_skills_status(skills_root=None):
    """One entry per _REQUIRED_SKILLS registration: its own metadata plus
    "installed" (whether check_path exists under skills_root right now) and
    "path" (the full path checked) - so the Skills page can show a real
    install/missing pill instead of just documenting the dependency."""
    if skills_root is None:
        skills_root = SKILLS_ROOT
    skills_root = Path(skills_root)
    results = []
    for skill in _REQUIRED_SKILLS:
        path = skills_root / skill["check_path"]
        results.append({
            "key": skill["key"],
            "name": skill["name"],
            "description": skill["description"],
            "used_by": skill["used_by"],
            "installed": path.exists(),
            "path": str(path),
        })
    return results


_WEEKDAY_ABBR = {0: "Sun", 1: "Mon", 2: "Tue", 3: "Wed", 4: "Thu", 5: "Fri", 6: "Sat"}


def _describe_schedule(schedule):
    """Best-effort human-readable summary of a StartCalendarInterval value:
    a single dict with no Weekday/Day key (every day), the classic Mon-Fri
    shape, an arbitrary weekday subset, or a monthly Day-of-month schedule -
    all reduced to one line as long as every entry shares the same
    Hour/Minute. Anything else (mixed times, an unrecognized shape) falls
    back to a generic description rather than trying to summarize every
    possible StartCalendarInterval shape."""
    if not schedule:
        return None
    entries = schedule if isinstance(schedule, list) else [schedule]
    try:
        hours = {e.get("Hour") for e in entries}
        minutes = {e.get("Minute") for e in entries}
        if len(hours) != 1 or len(minutes) != 1:
            return "scheduled (see plist)"
        hour, minute = hours.pop(), minutes.pop()
        days_of_month = sorted({e["Day"] for e in entries if "Day" in e})
        if days_of_month:
            return f"Monthly on day {days_of_month[0]} {hour:02d}:{minute:02d}"
        weekdays = sorted({e["Weekday"] for e in entries if "Weekday" in e})
        if not weekdays:
            return f"Every day {hour:02d}:{minute:02d}"
        if weekdays == [1, 2, 3, 4, 5]:
            return f"Mon–Fri {hour:02d}:{minute:02d}"
        labels = ", ".join(_WEEKDAY_ABBR[d] for d in weekdays)
        return f"{labels} {hour:02d}:{minute:02d}"
    except (KeyError, TypeError, ValueError, AttributeError):
        return "scheduled (see plist)"


def _describe_trigger(daemon):
    """Human-readable description of what makes a daemon run."""
    schedule_desc = _describe_schedule(daemon.get("schedule"))
    if schedule_desc:
        return schedule_desc
    if daemon.get("run_at_load") and daemon.get("keep_alive"):
        return "always-on (RunAtLoad + KeepAlive)"
    if daemon.get("run_at_load"):
        return "runs at load"
    return "manual/on-demand"


def _installed_plist_path(filename, launch_agents_dir=None):
    """Where launchctl load/unload actually operate: the copy in
    ~/Library/LaunchAgents/, not the source in this repo's launchd/ dir."""
    if launch_agents_dir is None:
        launch_agents_dir = Path.home() / "Library" / "LaunchAgents"
    return Path(launch_agents_dir) / filename


def _resolve_runner(runner):
    """`runner=None` means "the real subprocess.run, looked up now". Note the
    deliberate absence of `runner=subprocess.run` as a default value: default
    argument values are evaluated once at def-time, so such a default would
    permanently bind whatever subprocess.run was at import time and quietly
    ignore a test's monkeypatch of it - the same def-time-binding hazard
    do_POST's comment warns about for the module-level LAUNCHD_DIR constant."""
    return subprocess.run if runner is None else runner


def enable_daemon(filename, launchd_dir=LAUNCHD_DIR, launch_agents_dir=None, runner=None):
    """Copy launchd/<filename> (this repo's source of truth) to
    ~/Library/LaunchAgents/ and `launchctl load -w` it. Returns (ok: bool,
    message: str). `filename` is untrusted (comes from a URL path segment) -
    Path(filename).name strips any directory components before it touches
    the filesystem, exactly like read_history_file does for history names.

    `runner` defaults to the real subprocess.run but can be swapped for a
    fake in tests (same dependency-injection style as get_daemons_status's
    launchctl_output), so tests never need to invoke a real launchctl.

    `-w` matters here for the mirror-image reason it matters in
    disable_daemon: disable persists a "Disabled" override via `unload -w`,
    and a plain `load` (no `-w`) cannot clear that override - it exits 0
    (reported to launchctl's caller as success) while stderr says `Load
    failed: 5: Input/output error` and nothing actually loads. Without `-w`
    here, Enable silently no-ops for any daemon that was ever disabled.

    If `launchctl load -w` fails, the just-copied plist is removed again:
    leaving it in ~/Library/LaunchAgents/ would let launchd auto-load it at
    the next login even though the UI reported the enable as failed, so a
    reported failure must leave nothing behind."""
    runner = _resolve_runner(runner)
    safe_name = Path(filename).name
    if not safe_name.endswith(".plist"):
        return False, f"Invalid plist filename: {filename!r}"
    src = Path(launchd_dir) / safe_name
    if not src.exists():
        return False, f"{safe_name} not found in {launchd_dir}"
    dest = _installed_plist_path(safe_name, launch_agents_dir)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, dest)
    try:
        result = runner(
            ["launchctl", "load", "-w", str(dest)], capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        dest.unlink(missing_ok=True)
        return False, f"launchctl load failed to run: {e}"
    if result.returncode != 0:
        dest.unlink(missing_ok=True)
        return False, (result.stderr.strip() or f"launchctl load exited {result.returncode}")
    return True, f"Loaded {safe_name}"


def disable_daemon(filename, launchd_dir=LAUNCHD_DIR, launch_agents_dir=None, runner=None):
    """`launchctl unload -w` the installed copy (leaves the file in place -
    this disables the daemon, it doesn't uninstall it). Returns (ok, message).
    Same filename-sanitization discipline as enable_daemon, and the same
    injectable `runner` for tests.

    `-w` matters: a plain `launchctl unload` only removes the job from the
    current session, and since the plist file is deliberately left in
    ~/Library/LaunchAgents/, launchd would auto-load it again at the next
    login and silently revert the "Disable" click. `-w` persists the disabled
    state via the job's overrides, which is what "Disable" is supposed to mean.

    `launchd_dir` is this project's own source-of-truth directory, and
    `safe_name` must name a file that actually exists there. Without that
    check, disable would happily unload ANY *.plist sitting in
    ~/Library/LaunchAgents/ - postgres, redis, anything else on the user's
    machine - since that directory is shared with the rest of the system.
    This restricts disable to exactly the set of daemons enable can reach."""
    runner = _resolve_runner(runner)
    safe_name = Path(filename).name
    if not safe_name.endswith(".plist"):
        return False, f"Invalid plist filename: {filename!r}"
    if not (Path(launchd_dir) / safe_name).exists():
        return False, f"{safe_name} is not a known project daemon"
    dest = _installed_plist_path(safe_name, launch_agents_dir)
    if not dest.exists():
        return True, f"{safe_name} was not loaded"
    try:
        result = runner(
            ["launchctl", "unload", "-w", str(dest)], capture_output=True, text=True, timeout=10,
        )
    except Exception as e:
        return False, f"launchctl unload failed to run: {e}"
    if result.returncode != 0:
        return False, (result.stderr.strip() or f"launchctl unload exited {result.returncode}")
    return True, f"Unloaded {safe_name}"


def build_calendar_interval(hour, minute, weekdays, day_of_month=None):
    """A StartCalendarInterval value for the given hour/minute, either on a
    specific day of the month (`day_of_month`, launchd's `Day` key - this
    takes precedence over `weekdays` whenever both are given, since a
    monthly schedule and a weekly one are mutually exclusive here) or on
    the given set of weekdays (0=Sunday..6=Saturday). An empty weekday set
    and "all seven selected" both mean "every day" - launchd's own
    convention is to omit the Weekday key entirely for that case rather
    than listing all seven values, so build_calendar_interval does the
    same."""
    if day_of_month:
        return {"Day": day_of_month, "Hour": hour, "Minute": minute}
    weekdays = sorted(set(weekdays))
    if not weekdays or weekdays == list(range(7)):
        return {"Hour": hour, "Minute": minute}
    return [{"Weekday": d, "Hour": hour, "Minute": minute} for d in weekdays]


def update_daemon_schedule(filename, hour, minute, weekdays, day_of_month=None, launchd_dir=LAUNCHD_DIR, launch_agents_dir=None, runner=None, launchctl_output=None):
    """Rewrite <filename>'s StartCalendarInterval in launchd_dir (this
    project's source of truth) to run at hour:minute on the given weekdays.
    If the daemon is currently installed in launch_agents_dir, that copy is
    updated too. Returns (ok, message). Same filename-sanitization
    discipline as enable_daemon/disable_daemon.

    A real unload+load follows ONLY when the daemon is currently *loaded* -
    a plain filesystem edit isn't enough for a running job, since launchd
    caches the plist content it loaded rather than re-reading the file
    (same reasoning CLAUDE.md documents for any launchd plist edit).
    "Currently loaded" is decided by asking launchd (via _loaded_by_label,
    the same `launchctl list` parse the Daemons page's own status uses),
    NOT by whether the plist file exists in launch_agents_dir:
    disable_daemon deliberately leaves that file in place ("disable", not
    "uninstall"), so a disabled daemon's file is present too. Reloading on
    that basis would run `launchctl load -w`, and the `-w` clears the
    persisted disable override (see enable_daemon's docstring) - saving a
    schedule would silently switch a disabled daemon back on. In that case
    the new schedule is still written to disk, ready for whenever the user
    re-enables it.

    `launchctl_output` is the same test seam as get_daemons_status's: None
    means "really run `launchctl list`"."""
    runner = _resolve_runner(runner)
    safe_name = Path(filename).name
    if not safe_name.endswith(".plist"):
        return False, f"Invalid plist filename: {filename!r}"
    src = Path(launchd_dir) / safe_name
    if not src.exists():
        return False, f"{safe_name} not found in {launchd_dir}"
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return False, "Hour must be 0-23 and minute must be 0-59"
    if day_of_month is not None and not (1 <= day_of_month <= 31):
        return False, "Day of month must be 1-31"

    with open(src, "rb") as f:
        data = plistlib.load(f)
    data["StartCalendarInterval"] = build_calendar_interval(hour, minute, weekdays, day_of_month)
    with open(src, "wb") as f:
        plistlib.dump(data, f)

    dest = _installed_plist_path(safe_name, launch_agents_dir)
    if not dest.exists():
        return True, f"Updated schedule for {safe_name}"

    previous_dest_bytes = dest.read_bytes()
    shutil.copyfile(src, dest)

    label = data.get("Label")
    if label is None or label not in _loaded_by_label(launchctl_output):
        return True, (
            f"Updated schedule for {safe_name} (it is currently disabled — "
            "the new schedule takes effect once re-enabled)"
        )

    try:
        runner(["launchctl", "unload", "-w", str(dest)], capture_output=True, text=True, timeout=10)
        load_result = runner(["launchctl", "load", "-w", str(dest)], capture_output=True, text=True, timeout=10)
    except Exception as e:
        return False, f"Schedule saved, but reloading launchd failed to run: {e}"
    if load_result.returncode != 0:
        # The unload already took the daemon down. Put back exactly the
        # plist launchd was running and load that, so a rejected new
        # schedule doesn't leave a working daemon stopped.
        dest.write_bytes(previous_dest_bytes)
        reason = load_result.stderr.strip() or load_result.returncode
        try:
            restore = runner(["launchctl", "load", "-w", str(dest)], capture_output=True, text=True, timeout=10)
        except Exception:
            restore = None
        if restore is not None and restore.returncode == 0:
            return False, (
                f"Schedule saved, but launchctl load failed: {reason} — "
                f"restored and reloaded the previous schedule for {safe_name}"
            )
        return False, (
            f"Schedule saved, but launchctl load failed: {reason} — "
            f"{safe_name} is now stopped; re-enable it from the Daemons page"
        )
    return True, f"Updated schedule for {safe_name} and reloaded it"


def set_default_gitlab_instance(instance, config_path=None):
    if config_path is None:
        config_path = GITLAB_CONFIG_PATH
    config = read_gitlab_config(config_path)
    if instance not in config.get("instances", {}):
        return False, f"Unknown instance: {instance}"
    config["default"] = instance
    write_gitlab_config(config, config_path)
    return True, f"Default instance set to {instance}"


def upsert_gitlab_instance(alias, url, token, config_path=None):
    if config_path is None:
        config_path = GITLAB_CONFIG_PATH
    alias = alias.strip()
    url = url.strip()
    token = token.strip()
    if not alias:
        return False, "Instance name is required"
    if not url:
        return False, "URL is required"
    config = read_gitlab_config(config_path)
    instances = config.setdefault("instances", {})
    is_new = alias not in instances
    if is_new and not token:
        return False, "Token is required for a new instance"
    entry = dict(instances.get(alias, {}))
    entry["url"] = url
    entry["token"] = token if token else entry.get("token", "")
    instances[alias] = entry
    write_gitlab_config(config, config_path)
    return True, f"{'Added' if is_new else 'Updated'} instance {alias}"


def delete_gitlab_instance(alias, config_path=None):
    if config_path is None:
        config_path = GITLAB_CONFIG_PATH
    config = read_gitlab_config(config_path)
    if alias not in config.get("instances", {}):
        return False, f"Unknown instance: {alias}"
    if config.get("default") == alias:
        return False, f"Cannot delete {alias}: it is the default instance"
    referencing_projects = [p for p, proj in config.get("projects", {}).items() if proj.get("instance") == alias]
    referencing_bundles = [b for b, bundle in config.get("bundles", {}).items() if bundle.get("instance") == alias]
    if referencing_projects or referencing_bundles:
        parts = []
        if referencing_projects:
            parts.append(f"project(s) {', '.join(referencing_projects)}")
        if referencing_bundles:
            parts.append(f"bundle(s) {', '.join(referencing_bundles)}")
        return False, f"Cannot delete {alias}: still used by {' and '.join(parts)}"
    del config["instances"][alias]
    write_gitlab_config(config, config_path)
    return True, f"Deleted instance {alias}"


def upsert_gitlab_project(alias, project_id, instance, bundle="", config_path=None):
    if config_path is None:
        config_path = GITLAB_CONFIG_PATH
    alias = alias.strip()
    project_id = project_id.strip()
    instance = instance.strip()
    bundle = bundle.strip()
    if not alias:
        return False, "Project alias is required"
    if not project_id:
        return False, "Project ID is required"
    config = read_gitlab_config(config_path)
    if instance not in config.get("instances", {}):
        return False, f"Unknown instance: {instance}"
    if bundle:
        bundle_entry = config.get("bundles", {}).get(bundle)
        if bundle_entry is None:
            return False, f"Unknown bundle: {bundle}"
        if bundle_entry.get("instance") != instance:
            return False, f"Bundle {bundle} is for instance {bundle_entry.get('instance')}, not {instance}"
    projects = config.setdefault("projects", {})
    is_new = alias not in projects
    entry = dict(projects.get(alias, {}))
    entry["project_id"] = project_id
    entry["instance"] = instance
    if bundle:
        entry["bundle"] = bundle
    else:
        entry.pop("bundle", None)
    projects[alias] = entry
    write_gitlab_config(config, config_path)
    return True, f"{'Added' if is_new else 'Updated'} project {alias}"


def delete_gitlab_project(alias, config_path=None):
    if config_path is None:
        config_path = GITLAB_CONFIG_PATH
    config = read_gitlab_config(config_path)
    if alias not in config.get("projects", {}):
        return False, f"Unknown project: {alias}"
    del config["projects"][alias]
    write_gitlab_config(config, config_path)
    return True, f"Deleted project {alias}"


def upsert_access_bundle(name, instance, token, webhook_url="", gitlab_config_path=None, slack_config_path=None):
    """Upserts bundles.<name> (instance+token) in the GitLab config, and,
    if webhook_url is non-blank, bundle_webhooks.<name> in the Slack config
    - the two files are joined only by this shared name, never read by each
    other's owning script. Blank token/webhook_url on an edit means "leave
    the existing value alone", same convention as upsert_gitlab_instance."""
    if gitlab_config_path is None:
        gitlab_config_path = GITLAB_CONFIG_PATH
    if slack_config_path is None:
        slack_config_path = SLACK_CONFIG_PATH
    name = name.strip()
    instance = instance.strip()
    token = token.strip()
    webhook_url = webhook_url.strip()
    if not name:
        return False, "Bundle name is required"
    gitlab_config = read_gitlab_config(gitlab_config_path)
    if instance not in gitlab_config.get("instances", {}):
        return False, f"Unknown instance: {instance}"
    bundles = gitlab_config.setdefault("bundles", {})
    is_new = name not in bundles
    if is_new and not token:
        return False, "Token is required for a new bundle"
    if not is_new and bundles[name].get("instance") != instance:
        referencing = [p for p, proj in gitlab_config.get("projects", {}).items() if proj.get("bundle") == name]
        if referencing:
            return False, (
                f"Cannot change instance for bundle {name}: still used by "
                f"project(s) {', '.join(referencing)}"
            )
    entry = dict(bundles.get(name, {}))
    entry["instance"] = instance
    entry["token"] = token if token else entry.get("token", "")
    bundles[name] = entry
    write_gitlab_config(gitlab_config, gitlab_config_path)

    if webhook_url:
        slack_config = read_slack_config(slack_config_path)
        slack_config.setdefault("bundle_webhooks", {})[name] = webhook_url
        write_slack_config(slack_config, slack_config_path)

    return True, f"{'Added' if is_new else 'Updated'} bundle {name}"


def delete_access_bundle(name, gitlab_config_path=None, slack_config_path=None):
    if gitlab_config_path is None:
        gitlab_config_path = GITLAB_CONFIG_PATH
    if slack_config_path is None:
        slack_config_path = SLACK_CONFIG_PATH
    gitlab_config = read_gitlab_config(gitlab_config_path)
    if name not in gitlab_config.get("bundles", {}):
        return False, f"Unknown bundle: {name}"
    referencing = [p for p, proj in gitlab_config.get("projects", {}).items() if proj.get("bundle") == name]
    if referencing:
        return False, f"Cannot delete {name}: still used by project(s) {', '.join(referencing)}"
    del gitlab_config["bundles"][name]
    write_gitlab_config(gitlab_config, gitlab_config_path)

    slack_config = read_slack_config(slack_config_path)
    if name in slack_config.get("bundle_webhooks", {}):
        del slack_config["bundle_webhooks"][name]
        write_slack_config(slack_config, slack_config_path)

    return True, f"Deleted bundle {name}"


def clear_bundle_webhook(name, slack_config_path=None):
    if slack_config_path is None:
        slack_config_path = SLACK_CONFIG_PATH
    slack_config = read_slack_config(slack_config_path)
    if name not in slack_config.get("bundle_webhooks", {}):
        return False, f"No Slack webhook override set for bundle {name}"
    del slack_config["bundle_webhooks"][name]
    write_slack_config(slack_config, slack_config_path)
    return True, f"Cleared Slack webhook override for bundle {name}"


def update_slack_webhook(webhook_url, config_path=None):
    if config_path is None:
        config_path = SLACK_CONFIG_PATH
    webhook_url = webhook_url.strip()
    if not webhook_url:
        return False, "Webhook URL is required"
    config = read_slack_config(config_path)
    config["webhook_url"] = webhook_url
    write_slack_config(config, config_path)
    return True, "Slack webhook updated"


def read_custom_instructions(path=None):
    """The free-text content of the Instructions page - "" if it doesn't
    exist yet (a fresh install, or the user has never saved anything)."""
    if path is None:
        path = CUSTOM_INSTRUCTIONS_PATH
    try:
        return Path(path).read_text()
    except OSError:
        return ""


def write_custom_instructions(text, path=None):
    """Overwrites the Instructions page's saved text, including with ""
    (clearing it is a valid, deliberate action here, unlike the blank-
    means-leave-unchanged convention the credential fields elsewhere on
    Settings use - this isn't a secret with an existing value to
    protect). Same atomic same-directory temp-file-then-replace approach
    as _atomic_write_json, just writing plain text instead of JSON."""
    if path is None:
        path = CUSTOM_INSTRUCTIONS_PATH
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, prefix=f".{path.name}.", suffix=".tmp", delete=False
    )
    try:
        tmp.write(text)
        tmp.close()
        os.replace(tmp.name, path)
    except BaseException:
        os.unlink(tmp.name)
        raise
    return True, "Instructions saved"


def send_user_message(text, path=None):
    if path is None:
        path = MESSAGES_PATH
    text = text.strip()
    if not text:
        return False, "Message is required"
    append_message("user", text, path)
    return True, "Message sent"


def delete_message(timestamp, path=None):
    """Deletes the message whose `timestamp` matches exactly - a message's
    own timestamp (microsecond precision) is already a de-facto unique key
    for this tool's message volume, so there's no need for a separate id
    field just to support deletion."""
    if path is None:
        path = MESSAGES_PATH
    messages = read_messages(path)
    remaining = [m for m in messages if m.get("timestamp") != timestamp]
    if len(remaining) == len(messages):
        return False, "Message not found"
    _atomic_write_json(remaining, path)
    return True, "Message deleted"


def _chat_tool_status(status_path=None, topic_status_path=None,
                       projects_config_path=None, topics_config_path=None):
    """One combined snapshot of everything the chat assistant might be
    asked about "what's going on right now" - the GitLab loop's own
    status.json, the topic monitor's per-topic status, which topics are
    configured, and which project aliases are tracked. Every one of these
    is already read elsewhere in this file for the Overview/Topic
    Monitor/Settings pages; this just combines them into one JSON object
    for a single `chat-tool status` call instead of four.

    Every path is an injectable, None-default parameter (resolved at call
    time, per this file's own DI convention) rather than this function
    reading module globals directly with no way to redirect them - without
    this, a test calling _chat_tool_status() with no arguments has no way
    to point read_loop_projects_config()/get_configured_topics() at
    fixtures, and ends up reading this machine's real
    ~/.loop-engineering/projects.json and topics.json."""
    if status_path is None:
        status_path = STATUS_PATH
    if topic_status_path is None:
        topic_status_path = TOPIC_MONITOR_STATUS_PATH
    projects = read_loop_projects_config(projects_config_path).get("projects", {})
    return {
        "gitlab_loop": read_status(status_path),
        "topic_monitor": read_topic_status(topic_status_path),
        "configured_topics": get_configured_topics(topics_config_path),
        "tracked_projects": list(projects.keys()),
    }


def _chat_tool_history_list(history_dir=None):
    if history_dir is None:
        history_dir = HISTORY_DIR
    return list_run_history(history_dir)


def _chat_tool_history_read(name, history_dir=None):
    if history_dir is None:
        history_dir = HISTORY_DIR
    content = read_history_file(name, history_dir)
    if content is None:
        return {"error": f"No history entry named {name!r}"}
    return {"content": content}


def _chat_tool_memory(config_path=None):
    return get_project_memory(config_path)


def _chat_tool_progress(progress_path=None):
    if progress_path is None:
        progress_path = PROGRESS_PATH
    try:
        return {"content": Path(progress_path).read_text()}
    except OSError as exc:
        return {"error": str(exc)}


def _chat_tool_daemon_list(launchd_dir=None):
    if launchd_dir is None:
        launchd_dir = LAUNCHD_DIR
    return get_daemons_status(launchd_dir)


def _chat_tool_daemon_enable(filename, launchd_dir=None):
    if launchd_dir is None:
        launchd_dir = LAUNCHD_DIR
    ok, message = enable_daemon(filename, launchd_dir)
    return {"ok": ok, "message": message}


def _chat_tool_daemon_disable(filename, launchd_dir=None):
    """Wraps disable_daemon, but refuses the dashboard's own plist by name
    first - disable_daemon uses `launchctl unload -w`, which persists the
    disabled state (won't reload at next login), so a chat message that
    disabled the dashboard's own daemon would kill the very process
    serving that reply, with no way to re-enable it from the now-dead
    dashboard UI. The History page's delete button has no such self-harm
    equivalent, but daemons include this one, so this specific filename is
    special-cased rather than trusted to whatever the caller passes."""
    if launchd_dir is None:
        launchd_dir = LAUNCHD_DIR
    # Case-insensitive: this repo lives on a case-insensitive filesystem
    # (macOS APFS), so a differently-cased filename like
    # "COM.HERMES.LOOP-ENGINEERING-DASHBOARD.plist" would walk straight
    # past a case-sensitive `==` here and still resolve to (and disable)
    # the real dashboard daemon plist once it reaches disable_daemon,
    # which does no case normalization of its own either.
    if Path(filename).name.lower() == DASHBOARD_DAEMON_PLIST.lower():
        return {
            "ok": False,
            "message": (
                f"Refusing to disable {DASHBOARD_DAEMON_PLIST} from chat - "
                "that's the dashboard's own daemon, and disabling it would "
                "kill the process serving this reply with no way to "
                "re-enable it from here. Disable it manually (launchctl "
                "unload -w) if you really want to."
            ),
        }
    ok, message = disable_daemon(filename, launchd_dir)
    return {"ok": ok, "message": message}


def _chat_tool_run_now(kind, status_path=None, run_loop_path=None,
                        topic_status_path=None, topic_run_loop_path=None):
    if kind == "gitlab":
        if status_path is None:
            status_path = STATUS_PATH
        if run_loop_path is None:
            run_loop_path = RUN_LOOP_SH
        ok, message = trigger_manual_run(status_path, run_loop_path)
    elif kind == "topic-monitor":
        if topic_status_path is None:
            topic_status_path = TOPIC_MONITOR_STATUS_PATH
        if topic_run_loop_path is None:
            topic_run_loop_path = RUN_TOPIC_MONITOR_LOOP_SH
        ok, message = trigger_topic_monitor_run(topic_status_path, topic_run_loop_path)
    else:
        return {"error": f"Unknown run-now kind {kind!r} - expected 'gitlab' or 'topic-monitor'"}
    return {"ok": ok, "message": message}


def _chat_tool_run_issue(url, status_path=None, run_loop_path=None,
                          loop_config_path=None, gitlab_config_path=None):
    """Launches run-loop.sh scoped to exactly one issue, resolved from a
    pasted GitLab issue URL - the chat-tool action behind the Activity
    page chat's "paste an issue link" flow. Reuses trigger_manual_run's
    exact concurrency guard (same STATUS_PATH) so a single-issue run can
    never overlap with the scheduled loop, a plain run-now, or another
    single-issue run. The issue does NOT need to be assigned to the
    configured username - pasting the link here is itself the
    authorization - but its project must still resolve to one of the
    tracked aliases in projects.json."""
    if status_path is None:
        status_path = STATUS_PATH
    if run_loop_path is None:
        run_loop_path = RUN_LOOP_SH
    prefixes = gitlab_issue_url_prefixes(loop_config_path, gitlab_config_path)
    resolved = _resolve_gitlab_issue_url(url, prefixes)
    if resolved is None:
        return {"ok": False, "message": f"Could not match {url!r} to a tracked project"}
    alias, issue_iid = resolved
    status = read_status(status_path)
    if status.get("state") == "running":
        return {"ok": False, "message": "A run is already in progress"}
    if not run_loop_path.exists():
        return {"ok": False, "message": f"run-loop.sh not found at {run_loop_path}"}
    subprocess.Popen(
        ["bash", str(run_loop_path), alias, str(issue_iid)],
        cwd=str(LOOP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return {"ok": True, "message": f"Started work on {alias} #{issue_iid}"}


def _chat_tool_history_delete(name, history_dir=None):
    if history_dir is None:
        history_dir = HISTORY_DIR
    ok, message = delete_history_file(name, history_dir)
    return {"ok": ok, "message": message}


def _dispatch_chat_tool(action, args):
    """Dispatches one `chat-tool <action> [args]` CLI call, prints the
    result as JSON, and returns. This function's own action list IS the
    entire capability surface granted to the chat assistant's `claude -p`
    subprocess via --allowedTools (see build_chat_command) - every branch
    here must stay a thin, safe wrapper around an existing internal
    function, never a new capability invented just for chat.

    Deliberately NOT wired up here: history-delete. GitLab issue titles/
    descriptions/comments authored by other people flow into this repo's
    own history/progress/memory files, which this dispatcher exposes
    read access to, and the last _CHAT_MESSAGE_HISTORY_LIMIT thread
    messages get pasted verbatim into this assistant's own prompt on every
    turn - so third-party, attacker-influenceable text could reach a
    context able to call an irreversible delete action. The History
    page's own delete button (with its own confirm dialog) remains the
    only way to delete a history entry; _chat_tool_history_delete itself
    is kept (and still tested directly) since some future non-chat caller
    may still want it, but nothing routes to it from this dispatcher."""
    if action == "status":
        result = _chat_tool_status()
    elif action == "history-list":
        result = _chat_tool_history_list()
    elif action == "history-read":
        if not args:
            print("Usage: chat-tool history-read <name>", file=sys.stderr)
            sys.exit(1)
        result = _chat_tool_history_read(args[0])
    elif action == "memory":
        result = _chat_tool_memory()
    elif action == "progress":
        result = _chat_tool_progress()
    elif action == "daemon-list":
        result = _chat_tool_daemon_list()
    elif action == "daemon-enable":
        if not args:
            print("Usage: chat-tool daemon-enable <filename>", file=sys.stderr)
            sys.exit(1)
        result = _chat_tool_daemon_enable(args[0])
    elif action == "daemon-disable":
        if not args:
            print("Usage: chat-tool daemon-disable <filename>", file=sys.stderr)
            sys.exit(1)
        result = _chat_tool_daemon_disable(args[0])
    elif action == "run-now":
        if not args:
            print("Usage: chat-tool run-now <gitlab|topic-monitor>", file=sys.stderr)
            sys.exit(1)
        result = _chat_tool_run_now(args[0])
    elif action == "run-issue":
        if not args:
            print("Usage: chat-tool run-issue <gitlab issue url>", file=sys.stderr)
            sys.exit(1)
        result = _chat_tool_run_issue(args[0])
    else:
        print(f"Unknown chat-tool action: {action!r}", file=sys.stderr)
        sys.exit(1)
    print(json.dumps(result, indent=2))


def trigger_manual_run(status_path=None, run_loop_path=None):
    """Launches run-loop.sh as a detached background process for an
    on-demand run, outside its normal launchd schedule. Refuses if a run
    is already in progress (per the same status.json the loop itself
    writes) rather than starting a second, overlapping one - the loop
    assumes it's the only writer of its own git worktrees and PROGRESS.md.
    stdout/stderr are left to run-loop.sh's own redirect (it `exec`s its
    output into outputs/history/<date>.log near the top of the script,
    before anything else that could fail), so this doesn't need to capture
    or manage them itself. start_new_session=True detaches the child from
    this server process's session, so the run keeps going even if the
    dashboard daemon restarts - same independence a launchd-triggered run
    already has."""
    if status_path is None:
        status_path = STATUS_PATH
    if run_loop_path is None:
        run_loop_path = RUN_LOOP_SH
    status = read_status(status_path)
    if status.get("state") == "running":
        return False, "A run is already in progress"
    if not run_loop_path.exists():
        return False, f"run-loop.sh not found at {run_loop_path}"
    subprocess.Popen(
        ["bash", str(run_loop_path)],
        cwd=str(LOOP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, "Run started - check back here for progress"


def trigger_topic_monitor_run(status_path=None, run_loop_path=None):
    """The topic monitor loop's own equivalent of trigger_manual_run:
    launches run-topic-monitor-loop.sh as a detached background process for
    an on-demand run, outside its normal launchd schedule. Refuses if any
    configured topic is currently running (per outputs/topic-monitor/
    status.json's per-topic "state" entries) rather than starting a second,
    overlapping run - the loop assumes it's the only writer of its own
    outputs/topic-monitor/ state files."""
    if status_path is None:
        status_path = TOPIC_MONITOR_STATUS_PATH
    if run_loop_path is None:
        run_loop_path = RUN_TOPIC_MONITOR_LOOP_SH
    topics = read_topic_status(status_path).get("topics", {})
    if any(entry.get("state") == "running" for entry in topics.values()):
        return False, "A run is already in progress"
    if not run_loop_path.exists():
        return False, f"run-topic-monitor-loop.sh not found at {run_loop_path}"
    subprocess.Popen(
        ["bash", str(run_loop_path)],
        cwd=str(LOOP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, "Run started - check back here for progress"


def trigger_skills_install(status_path=None, setup_script_path=None, log_path=None, daemon_label=None):
    """Launches bin/scripts/setup.sh in the background so a missing skill can be
    installed straight from the Skills page - no terminal required. Refuses
    if an install is already in progress, the same guard trigger_manual_run
    uses for the main loop's own status.json.

    The whole install-then-restart sequence is chained into one detached
    shell command rather than done from this process, because the last
    step - restarting the dashboard daemon via `launchctl kickstart -k` -
    would otherwise kill the very process running this function before it
    could finish. Each stage writes its own state to status_path via this
    same script's `write-skills-install-status` CLI subcommand (mirroring
    how run-loop.sh itself calls `write-status`), so the Skills page can
    show live progress across a request that outlives this one."""
    if status_path is None:
        status_path = SKILLS_INSTALL_STATUS_PATH
    if setup_script_path is None:
        setup_script_path = SETUP_SH
    if log_path is None:
        log_path = SKILLS_INSTALL_LOG_PATH
    if daemon_label is None:
        daemon_label = DASHBOARD_DAEMON_LABEL

    status = read_status(status_path)
    if status.get("state") == "installing":
        return False, "A setup is already in progress"
    if not setup_script_path.exists():
        return False, f"setup.sh not found at {setup_script_path}"

    this_script = Path(__file__).resolve()
    command = (
        f"{shlex.quote(sys.executable)} {shlex.quote(str(this_script))} "
        f"write-skills-install-status installing --status-path {shlex.quote(str(status_path))}; "
        f"{shlex.quote(str(setup_script_path))} >>{shlex.quote(str(log_path))} 2>&1; "
        f"ec=$?; "
        f"if [ $ec -eq 0 ]; then state=done; else state=failed; fi; "
        f"{shlex.quote(sys.executable)} {shlex.quote(str(this_script))} "
        f"write-skills-install-status $state --status-path {shlex.quote(str(status_path))}; "
        f"launchctl kickstart -k gui/$(id -u)/{shlex.quote(daemon_label)}"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.Popen(
        ["bash", "-c", command],
        cwd=str(LOOP_DIR),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    return True, "Setup started in the background - the dashboard will restart automatically when it's done"


def _today_log_tail(lines=30):
    today = datetime.now().strftime("%Y-%m-%d")
    log_path = HISTORY_DIR / f"{today}.log"
    if not log_path.exists():
        return None
    content = log_path.read_text().splitlines()
    return "\n".join(content[-lines:])


def append_unified_log(source, detail, body=None, log_path=None):
    """Appends one human-readable entry to logs/loop-engineering.log - see
    UNIFIED_LOG_PATH's own comment for why this file exists. `source`
    identifies which of the 3 `claude` CLI call sites wrote the entry
    (e.g. "chat-assistant"); `detail` is a short one-line status ("turn
    started", "reply", "error"); `body` is the human-readable output
    itself, when there is any yet (the "turn started" entry has none).
    Every entry gets a `[YYYY-MM-DD HH:MM:SS] ---- source ---- detail
    ----` header line so a reader (or the Logs page) can tell entries
    from different sources and different turns apart at a glance, even
    though they all share one file.

    Best-effort and silent on failure: this is a convenience trail for a
    human to read later, never load-bearing for the caller's own job (in
    particular, _run_chat_job must still finish and reply even if the
    disk is full or logs/ isn't writable)."""
    if log_path is None:
        log_path = UNIFIED_LOG_PATH
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    header = f"[{timestamp}] ---- {source} ---- {detail} ----\n"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with open(log_path, "a") as f:
            f.write(header)
            if body:
                f.write(body if body.endswith("\n") else body + "\n")
    except OSError:
        pass


def read_unified_log_tail(lines=500, log_path=None):
    """The most recent `lines` lines of logs/loop-engineering.log, or None
    if it doesn't exist yet (no `claude` invocation has happened since
    this file was introduced) - same "tail, not the whole possibly-huge
    file" contract as _today_log_tail, generalized across all 3 log
    sources and the file's whole lifetime rather than just today."""
    if log_path is None:
        log_path = UNIFIED_LOG_PATH
    if not log_path.exists():
        return None
    content = log_path.read_text().splitlines()
    return "\n".join(content[-lines:])


_LOG_ENTRY_HEADER_RE = re.compile(
    r"^\[(?P<timestamp>[^\]]+)\] ---- (?P<source>.*?) ---- (?P<detail>.*?) ----\s*$"
)


def _parse_unified_log_entries(tail_text):
    """Splits a loop-engineering.log tail into individual entries, one per
    append_unified_log call, using that function's own `[timestamp] ----
    source ---- detail ----` header line as the delimiter - lets the Logs
    page draw a visual boundary around one full source/detail/body call
    instead of showing the tail as one undifferentiated blob of text.
    Text preceding the first header (the tail cut into the middle of an
    older entry) becomes a headerless entry so nothing is silently
    dropped."""
    entries = []
    current = None
    for line in tail_text.splitlines():
        match = _LOG_ENTRY_HEADER_RE.match(line)
        if match:
            current = {
                "timestamp": match.group("timestamp"),
                "source": match.group("source"),
                "detail": match.group("detail"),
                "body_lines": [],
            }
            entries.append(current)
        elif current is None:
            current = {"timestamp": None, "source": None, "detail": None, "body_lines": [line]}
            entries.append(current)
        else:
            current["body_lines"].append(line)
    for entry in entries:
        entry["body"] = "\n".join(entry.pop("body_lines")).strip("\n")
    return entries


def _log_entry_html(entry):
    """One Logs-page entry: a header row naming its source/detail/timestamp
    (or a "continued from an earlier entry" note for the headerless
    leading entry - see _parse_unified_log_entries) plus its body, each
    wrapped in its own bordered block so a reader can tell where one
    append_unified_log call ends and the next begins at a glance."""
    if entry["source"] is None:
        header_html = "<span class='log-entry-meta'>(continued from an earlier entry)</span>"
    else:
        header_html = (
            f"<span class='log-entry-source'>{html.escape(entry['source'])}</span>"
            f"<span class='log-entry-detail'>{html.escape(entry['detail'])}</span>"
            f"<span class='log-entry-time'>{html.escape(entry['timestamp'])}</span>"
        )
    body_html = f"<pre class='log-entry-body'>{html.escape(entry['body'])}</pre>" if entry["body"] else ""
    return f"<div class='log-entry'><div class='log-entry-header'>{header_html}</div>{body_html}</div>"


# Body/UI typeface choices for the Preferences page's Font picker - client-
# only, like color mode/accent (see render_preferences_page), so every
# option's actual CSS rule already has to be present in _STYLE up front
# rather than fetched on choice: switching is an instant --font-family-stack
# swap via the :root[data-font=...] rules built below, never a page reload
# or a new network request. (key, label, Google Fonts family name); "roboto"
# is the original default, matching every dashboard screenshot/design spec
# before this picker existed.
_FONT_CHOICES = (
    ("roboto", "Roboto", "Roboto"),
    ("inter", "Inter", "Inter"),
    ("open-sans", "Open Sans", "Open Sans"),
    ("nunito-sans", "Nunito Sans", "Nunito Sans"),
    ("source-sans-3", "Source Sans 3", "Source Sans 3"),
    ("ibm-plex-sans", "IBM Plex Sans", "IBM Plex Sans"),
)

_FALLBACK_FONT_STACK = "-apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif"

# One <link> requests every choice's family at once (fewer round trips than
# one per font) - see the <link> tag built in _render_shell. 400/500/700 are
# the only weights _STYLE actually sets (h1-h3 use 500, a couple of
# emphasis spots use 700, everything else is the unset browser default of
# 400), so that's all the widths asked for per family, not each family's
# full variable axis range.
_GOOGLE_FONTS_FAMILIES_PARAM = "&".join(
    f"family={name.replace(' ', '+')}:wght@400;500;700" for _, _, name in _FONT_CHOICES
)

# :root[data-font="<key>"] { --font-family-stack: '<Name>', <fallback>; }
# per choice, plus the bare :root default (roboto, unprefixed) so a first
# visit with nothing in localStorage yet still renders the original font
# before _render_shell's pre-paint script has a chance to set data-font.
_FONT_FACE_VARS = "\n".join(
    f":root[data-font=\"{key}\"] {{ --font-family-stack: '{name}', {_FALLBACK_FONT_STACK}; }}"
    for key, _, name in _FONT_CHOICES
    if key != "roboto"
)

# Every Material Symbols glyph name used anywhere on this dashboard (see the
# _SECTION_ICON_*/_*_ICON constants and inline spans below), alphabetically
# sorted. Google Fonts' icon_names= parameter (see the <link> tag built in
# _render_shell) subsets the served font to exactly this set, the same way
# this dashboard used to subset a locally-vendored copy - so using a glyph
# name that isn't listed here renders as tofu/missing glyph. Add a new name
# to this list before shipping a new icon constant that uses it.
_MATERIAL_SYMBOLS_ICON_NAMES = (
    "add,bolt,check_circle,chevron_left,circle,delete,description,"
    "dns,edit_note,error,expand_more,extension,folder,folder_off,forum,history,lightbulb,merge,monitoring,newspaper,"
    "open_in_new,palette,send,settings,smart_toy,space_dashboard,terminal,topic,warning"
)


# Shared stylesheet for every page this server renders (the 5 top-level
# pages plus the /history/<name> sub-page), so they all feel like one
# product rather than a styled page and a bare text dump. Pure CSS/typography;
# Roboto and the Material Symbols icon font are loaded from Google Fonts (see
# the <link> tags in _render_shell) rather than self-hosted, so this page
# does reach the network for those two requests despite auto-refreshing
# every 30s with nobody watching most of the time.
_STYLE = f"""
:root {{
  --font-family-stack: 'Roboto', {_FALLBACK_FONT_STACK};

  --md-primary: #9CC0FC;
  --md-on-primary: #032763;
  --md-primary-container: #043B95;
  --md-on-primary-container: #CDE0FE;

  --md-surface-dim: #0E0F11;
  --md-surface: #121416;
  --md-surface-container-lowest: #090A0B;
  --md-surface-container-low: #17191C;
  --md-surface-container: #1C1E22;
  --md-surface-container-high: #272A30;
  --md-surface-container-highest: #32373E;
  --md-on-surface: #E3E5E8;
  --md-on-surface-variant: #C7CBD1;
  --md-outline: #8F97A3;
  --md-outline-variant: #454B54;

  --md-success: #B5E3C6;
  --md-on-success: #1C4A2D;
  --md-success-container: #2A6F43;
  --md-on-success-container: #D9F2E2;
  --md-warning: #E8CCB0;
  --md-on-warning: #4F3317;
  --md-warning-container: #774C22;
  --md-on-warning-container: #F5E6D6;
  --md-error: #E8B4B0;
  --md-on-error: #4F1B17;
  --md-error-container: #772822;
  --md-on-error-container: #F5D8D6;
}}

/* Theme/accent color (Preferences page): a fixed, mode-independent wash
   for the sidebar/topbar background - unlike --md-primary above (the
   app's single fixed accent for buttons/links/etc, unaffected by this
   choice), these are deliberately light, flat colors used exactly as
   given regardless of light/dark color mode, the way a workspace/
   sidebar accent works in many real apps (Notion, Linear, Slack) rather
   than a mode-aware semantic palette. `data-accent` is always present on
   <html> (defaulted to "default" by _render_shell's pre-paint script if
   localStorage has no saved choice yet), so "Default" - no tint, the
   original neutral sidebar - is a plain named selector like every other
   choice, not a special-cased absence branch.

   Four tokens per accent:
   - --md-nav-surface: the sidebar/topbar background itself.
   - --md-nav-on-surface: text/icons living directly on that background
     (brand, nav labels, the sidebar toggle) - these washes are all very
     light, so this needs to be a dark, legible color even while the
     rest of the page is in dark mode, hence not reusing --md-on-surface.
   - --md-nav-active-surface / --md-nav-active-on-surface: the current
     page's nav link and hover state - a distinctly different, slightly
     deeper tint of the same hue, so the active item doesn't disappear
     into the sidebar's own background color. */
:root[data-accent="default"] {{
  --md-nav-surface: var(--md-surface-container-low);
  --md-nav-on-surface: var(--md-on-surface-variant);
  --md-nav-active-surface: var(--md-surface-container-highest);
  --md-nav-active-on-surface: var(--md-primary);
}}
:root[data-accent="indigo"] {{
  --md-nav-surface: #f4f0ff;
  --md-nav-on-surface: #3A3550;
  --md-nav-active-surface: #E0D6FF;
  --md-nav-active-on-surface: #4B3FA8;
}}
:root[data-accent="blue"] {{
  --md-nav-surface: #e9f3fc;
  --md-nav-on-surface: #2C3E4A;
  --md-nav-active-surface: #CFE7FB;
  --md-nav-active-on-surface: #0B57D0;
}}
:root[data-accent="green"] {{
  --md-nav-surface: #ecf4ee;
  --md-nav-on-surface: #2A3B2D;
  --md-nav-active-surface: #D2E8D6;
  --md-nav-active-on-surface: #2E7D3C;
}}
:root[data-accent="red"] {{
  --md-nav-surface: #fcf1ef;
  --md-nav-on-surface: #4A2F2B;
  --md-nav-active-surface: #F7D9D3;
  --md-nav-active-on-surface: #B3261E;
}}
:root[data-accent="gray"] {{
  --md-nav-surface: #ececef;
  --md-nav-on-surface: #3A3A3D;
  --md-nav-active-surface: #D6D6DB;
  --md-nav-active-on-surface: #3A3A3D;
}}

/* Font (Preferences page): one rule per non-default choice, generated from
   _FONT_CHOICES - "Roboto" needs no rule of its own since it's already the
   bare :root default above. */
{_FONT_FACE_VARS}

/* Color mode (Preferences page): dark is the base scheme above (this
   app's original, unchanged default). Light is layered on two ways -
   "Auto" (no explicit data-color-mode) follows the OS via the media
   query, guarded so an explicit "Dark" choice can't be overridden by a
   light OS preference; an explicit "Light" choice applies regardless of
   OS preference via the plain attribute selector below. Every non-accent
   token from the dark :root block gets a light counterpart here - most
   are a genuinely new light palette, but success/warning/error simply
   swap each dark pair's two halves (light mode's "container" is what was
   dark mode's "on-container" hue, and vice versa). */
@media (prefers-color-scheme: light) {{
  :root:not([data-color-mode="dark"]) {{
    --md-primary: #0B57D0;
    --md-on-primary: #FFFFFF;
    --md-primary-container: #D3E3FD;
    --md-on-primary-container: #041E49;

    --md-surface-dim: #FFF;
    --md-surface: #FBF8FA;
    --md-surface-container-lowest: #FFFFFF;
    --md-surface-container-low: #F5F1F4;
    --md-surface-container: #EFEBEE;
    --md-surface-container-high: #E9E4E8;
    --md-surface-container-highest: #E3DEE2;
    --md-on-surface: #1C1B1E;
    --md-on-surface-variant: #47464A;
    --md-outline: #77767A;
    --md-outline-variant: #C7C5CA;

    --md-success: #1C4A2D;
    --md-on-success: #B5E3C6;
    --md-success-container: #D9F2E2;
    --md-on-success-container: #2A6F43;
    --md-warning: #4F3317;
    --md-on-warning: #E8CCB0;
    --md-warning-container: #F5E6D6;
    --md-on-warning-container: #774C22;
    --md-error: #4F1B17;
    --md-on-error: #E8B4B0;
    --md-error-container: #F5D8D6;
    --md-on-error-container: #772822;
  }}
}}
:root[data-color-mode="light"] {{
  --md-primary: #0B57D0;
  --md-on-primary: #FFFFFF;
  --md-primary-container: #D3E3FD;
  --md-on-primary-container: #041E49;

  --md-surface-dim: #FFF;
  --md-surface: #FBF8FA;
  --md-surface-container-lowest: #FFFFFF;
  --md-surface-container-low: #F5F1F4;
  --md-surface-container: #EFEBEE;
  --md-surface-container-high: #E9E4E8;
  --md-surface-container-highest: #E3DEE2;
  --md-on-surface: #1C1B1E;
  --md-on-surface-variant: #47464A;
  --md-outline: #77767A;
  --md-outline-variant: #C7C5CA;

  --md-success: #1C4A2D;
  --md-on-success: #B5E3C6;
  --md-success-container: #D9F2E2;
  --md-on-success-container: #2A6F43;
  --md-warning: #4F3317;
  --md-on-warning: #E8CCB0;
  --md-warning-container: #F5E6D6;
  --md-on-warning-container: #774C22;
  --md-error: #4F1B17;
  --md-on-error: #E8B4B0;
  --md-error-container: #F5D8D6;
  --md-on-error-container: #772822;
}}

* {{ box-sizing: border-box; }}

body {{
  margin: 0;
  padding: 0 0 4rem;
  background: var(--md-surface-dim);
  color: var(--md-on-surface-variant);
  font-family: var(--font-family-stack);
  line-height: 1.55;
  font-size: 16px;
}}

.wrap {{ max-width: 1080px; margin: 0 auto; padding: 2rem 1.25rem 0; }}

h1, h2, h3 {{ font-family: var(--font-family-stack); color: var(--md-on-surface); font-weight: 500; margin: 0 0 0.5rem; }}
/* Page titles (h1) get a size step up from h2/h3 - stays at the same
   500 weight as every other heading (this app's MD3 restyle deliberately
   keeps to 400/500/700 only, see test_no_font_weight_600_remains), just
   larger, so a page's own title reads as a clear step above a card's
   section heading one size down. */
h1 {{ font-size: 1.65rem; }}
h2 {{ font-size: 1.05rem; margin: 0 0 0.85rem; }}
h3 {{ font-size: 0.95rem; margin: 1rem 0 0.35rem; }}
h3:first-child {{ margin-top: 0; }}

p {{ margin: 0 0 0.5rem; }}

a {{
  color: var(--md-primary);
  text-decoration: none;
  cursor: pointer;
  transition: color 150ms ease, background-color 150ms ease;
}}
a:hover {{ color: var(--md-primary); text-decoration: underline; }}

/* One keyboard-focus treatment for every plain interactive element that
   doesn't already define its own (form inputs, the custom-select trigger,
   and the md-checkbox all set a more specific :focus-visible/:focus rule
   below, which wins on specificity over this element-level one). Without
   this, tabbing to a nav link, .btn, tab, pref swatch/segmented option, or
   the Daemons page's on/off switch fell back to whatever the browser's
   default focus ring happens to be - inconsistent with the deliberate
   ring used everywhere else, and in some browsers barely visible against
   these surface colors at all. */
a:focus-visible,
button:focus-visible,
[role="button"]:focus-visible,
[tabindex]:focus-visible {{
  outline: 2px solid var(--md-primary);
  outline-offset: 2px;
}}

/* Fixed left sidebar (brand + nav), shared by every page this server
   renders. Collapses to an icon-only rail either by user toggle
   (html.collapsed, set/read via localStorage - see _render_shell's head
   script and _sidebar_html's toggle button) or automatically below the
   mobile breakpoint. */
.sidebar {{
  position: fixed;
  top: 0;
  left: 0;
  width: 220px;
  height: 100vh;
  background: var(--md-nav-surface);
  border-right: 1px solid var(--md-outline-variant);
  display: flex;
  flex-direction: column;
  overflow-y: auto;
  transition: width 150ms ease;
  z-index: 100;
}}

.sidebar-top {{
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  padding: 1rem 1rem 0.75rem;
  flex-shrink: 0;
}}

.brand {{
  display: inline-flex;
  align-items: center;
  gap: 0.45rem;
  color: var(--md-nav-on-surface);
  font-family: var(--font-family-stack);
  font-weight: 700;
  font-size: 1.05rem;
  min-width: 0;
  overflow: hidden;
}}
.brand:hover {{ color: var(--md-nav-on-surface); text-decoration: none; }}
.brand-mark {{ display: none; color: var(--md-primary); flex-shrink: 0; }}
.brand-name {{ overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

.sidebar-toggle {{
  background: none;
  border: none;
  color: var(--md-nav-on-surface);
  cursor: pointer;
  padding: 0.3rem;
  border-radius: 6px;
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex-shrink: 0;
  transition: background-color 150ms ease, color 150ms ease;
}}
.sidebar-toggle:hover {{ background: var(--md-nav-active-surface); color: var(--md-nav-active-on-surface); }}
.sidebar-toggle .material-symbols-outlined {{ transition: transform 150ms ease; font-size: 18px; }}

.sidebar-nav {{ display: flex; flex-direction: column; gap: 0.15rem; padding: 0.5rem; overflow-y: auto; }}
.sidebar-nav a {{
  display: flex;
  align-items: center;
  gap: 0.65rem;
  padding: 0.55rem 0.9rem;
  border-radius: 999px;
  color: var(--md-nav-on-surface);
  font-weight: 500;
  font-size: 0.85rem;
  white-space: nowrap;
  overflow: hidden;
  transition: color 150ms ease, background-color 150ms ease;
}}
/* Hover and active share the same tinted highlight - the sidebar's own
   background is now a fixed, mode-independent wash (--md-nav-surface,
   see .sidebar above), so a dark-mode-oriented color like
   --md-surface-dim would look inverted/wrong sitting on top of it. */
.sidebar-nav a:hover {{ background: var(--md-nav-active-surface); color: var(--md-nav-active-on-surface); text-decoration: none; }}
.sidebar-nav a.active {{ background: var(--md-nav-active-surface); color: var(--md-nav-active-on-surface); }}
.nav-icon {{ display: inline-flex; flex-shrink: 0; }}
.nav-label {{ overflow: hidden; text-overflow: ellipsis; }}
/* One per _NAV_GROUPS entry that has a label (Overview stays label-less,
   it's the landing page rather than part of a category). Hidden when
   collapsed like every other nav label - see .sidebar-nav a below. */
.sidebar-group-label {{
  margin: 0.9rem 0.9rem 0.25rem;
  font-family: var(--font-family-stack);
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--md-nav-on-surface);
  opacity: 0.6;
}}
.sidebar-group-label:first-child {{ margin-top: 0.25rem; }}

.content-area {{ margin-left: 220px; min-height: 100vh; transition: margin-left 150ms ease; }}

.topbar {{
  position: sticky;
  top: 0;
  z-index: 90;
  background: var(--md-nav-surface);
  border-bottom: 1px solid var(--md-outline-variant);
  padding: 0.75rem 1.25rem;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.75rem;
}}
.topbar .header-right {{ justify-content: flex-end; flex-shrink: 0; }}

/* Shown once the page's own <h1> has scrolled up behind this sticky
   topbar (see the IntersectionObserver script in _render_shell), so
   scrolling down a page never leaves the topbar with no indication of
   which page you're on. */
.topbar-page-title {{
  font-size: 1.15rem;
  font-weight: 700;
  color: var(--md-nav-on-surface);
  opacity: 0;
  transform: translateY(4px);
  transition: opacity 150ms ease, transform 150ms ease;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  min-width: 0;
}}
.topbar-page-title.is-visible {{ opacity: 1; transform: translateY(0); }}

.header-right {{ display: flex; align-items: center; gap: 0.65rem; }}
.refresh-note {{ font-size: 0.75rem; color: var(--md-nav-on-surface); white-space: nowrap; }}

html.collapsed .sidebar {{ width: 64px; }}
html.collapsed .content-area {{ margin-left: 64px; }}
html.collapsed .brand-name,
html.collapsed .nav-label,
html.collapsed .sidebar-group-label {{ display: none; }}
html.collapsed .brand-mark {{ display: inline-flex; }}
html.collapsed .sidebar-toggle .material-symbols-outlined {{ transform: rotate(180deg); }}
html.collapsed .sidebar-nav a {{ justify-content: center; }}
html.collapsed .sidebar-top {{
  /* Side-by-side, the brand icon and the toggle button (~20px + gap +
     ~28px ≈ 56px) don't fit the 64px collapsed rail's ~32px content box
     once padding is subtracted - .sidebar-toggle refuses to shrink
     (flex-shrink: 0), so .brand absorbed the entire overflow and got
     crushed by its own overflow: hidden, clipping the icon down to
     nothing. Stacking them means neither has to shrink at all. */
  flex-direction: column;
  justify-content: center;
  gap: 0.4rem;
}}
html.collapsed .activity-composer {{ left: 64px; }}

@media (max-width: 720px) {{
  .sidebar {{ width: 64px; }}
  .content-area {{ margin-left: 64px; }}
  .brand-name, .nav-label, .sidebar-group-label {{ display: none; }}
  .brand-mark {{ display: inline-flex; }}
  .sidebar-toggle {{ display: none; }}
  .sidebar-nav a {{ justify-content: center; }}
  .sidebar-top {{ justify-content: center; }}
  .activity-composer {{ left: 64px; }}
}}

/* The Activity page's message composer floats fixed to the bottom of the
   content area (not the whole viewport - `left` matches .content-area's
   current margin-left, including its collapsed/mobile widths above), so
   it's always reachable without scrolling down through the whole thread. */
.activity-messages-grid {{ margin-bottom: 6rem; }}
/* The Dashboard page's stats section - tracked-projects/configured-topics
   setup counts plus GitLab-loop run totals (see _gitlab_loop_stats),
   sitting above the message thread as a quick-glance summary. */
.dash-stats-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 0.75rem;
  margin-bottom: 1rem;
}}
.dash-stat-tile {{
  display: flex;
  flex-direction: column;
  gap: 0.15rem;
  padding: 0.75rem 0.9rem;
  border-radius: 12px;
  background: var(--md-surface-container-high);
}}
.dash-stat-icon {{ color: var(--md-primary); font-size: 20px; }}
.dash-stat-value {{ font-size: 1.4rem; font-weight: 700; }}
.dash-stat-label {{ font-size: 0.78rem; color: var(--md-on-surface-variant); }}
.analytics-days-selector {{ display: flex; gap: 0.5rem; margin: 0 0 1rem 0; }}
.analytics-days-selector a {{ padding: 0.3rem 0.75rem; border-radius: 6px; background: var(--md-surface-container-low); color: var(--md-on-surface-variant); text-decoration: none; font-size: 0.85rem; }}
.analytics-days-selector a.active {{ background: var(--md-primary); color: var(--md-on-primary); }}
.analytics-health-score {{ font-size: 2.5rem; font-weight: 700; margin: 0.25rem 0; }}
.analytics-health-note {{ font-size: 0.8rem; color: var(--md-on-surface-variant); margin: 0 0 0.75rem 0; }}
.trend-charts-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap: 1rem; }}
.trend-chart {{ background: var(--md-surface-container-low); border-radius: 8px; padding: 0.75rem; }}
.trend-chart-title {{ font-size: 0.85rem; font-weight: 500; margin: 0 0 0.5rem 0; color: var(--md-on-surface-variant); }}
.trend-chart-empty p:last-child {{ font-size: 0.8rem; color: var(--md-on-surface-variant); }}
.dash-activity-strip-row {{ display: flex; align-items: center; gap: 0.6rem; }}
.dash-activity-strip-label {{ font-size: 0.78rem; color: var(--md-on-surface-variant); }}
/* One bar per day, oldest first - color is the day's outcome (see
   _gitlab_loop_stats: escalation > mr > quiet), an outline-only bar means
   no run was logged that day at all. */
.activity-strip {{ display: flex; gap: 0.3rem; }}
.activity-bar {{ width: 22px; height: 22px; border-radius: 6px; }}
.activity-bar-quiet {{ background: var(--md-success-container); }}
.activity-bar-mr {{ background: var(--md-primary-container); }}
.activity-bar-escalation {{ background: var(--md-warning-container); }}
.activity-bar-none {{ background: none; border: 1px dashed var(--md-outline-variant); }}
/* The message thread becomes an actual scrollable chat panel - a fixed,
   generous viewport height instead of the whole page growing with every
   new message - so a long conversation stays navigable the way a real
   chat UI's thread pane does. */
#activity-message-list {{ max-height: 60vh; overflow-y: auto; padding-right: 0.25rem; }}
.activity-composer {{
  position: fixed;
  left: 220px;
  right: 0;
  bottom: 0;
  background: var(--md-nav-surface);
  border-top: 1px solid var(--md-outline-variant);
  padding: 0.85rem 1.25rem;
  z-index: 80;
  transition: left 150ms ease;
}}
.activity-composer-inner {{ max-width: 1080px; margin: 0 auto; }}
.activity-composer-form {{ width: 100%; flex-wrap: nowrap; align-items: flex-end; }}
/* Overrides the generic .daemon-action-form textarea rule (flex-basis:
   100%, its own full-width line) - the composer's textarea shares its
   line with the Send button instead, same as the input it replaced. */
.activity-composer-form textarea.activity-composer-input {{
  flex: 1 1 auto;
  min-width: 0;
  flex-basis: auto;
  resize: vertical;
  min-height: calc(1.4em * 3 + 0.7rem);
  max-height: 40vh;
  line-height: 1.4;
}}
.activity-composer-form button {{ flex: 0 0 120px; width: 120px; justify-content: center; }}

.page-title {{ margin: 0.25rem 0 1.5rem; }}
.page-title h1 {{ margin-bottom: 0.35rem; }}
.page-title .subtitle {{ margin: 0; font-size: 0.95rem; }}

.section-header {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.85rem; }}
.section-header .material-symbols-outlined,
.section-header .slack-mark,
.section-header .gitlab-mark {{ color: var(--md-primary); flex-shrink: 0; }}
/* Preferences page's Theme section only - larger than every other
   section-header icon on the dashboard, since this one doubles as a
   preview of the palette icon it represents. */
.pref-theme-icon .material-symbols-outlined {{ font-size: 28px; }}
.section-subtitle {{ margin: 0 0 0.85rem; font-size: 0.85rem; color: var(--md-on-surface-variant); }}
.material-symbols-outlined {{
  font-family: 'Material Symbols Outlined';
  font-weight: normal;
  font-style: normal;
  line-height: 1;
  letter-spacing: normal;
  text-transform: none;
  display: inline-block;
  white-space: nowrap;
  word-wrap: normal;
  direction: ltr;
  vertical-align: middle;
  user-select: none;
}}
.nav-icon .material-symbols-outlined,
.section-header .material-symbols-outlined {{ font-size: 18px; }}
.pill .material-symbols-outlined {{ font-size: 14px; }}
.btn .material-symbols-outlined {{ font-size: 16px; }}
.section-header h2 {{ margin: 0; }}

.grid {{ display: grid; grid-template-columns: 1fr; gap: 1.25rem; margin-bottom: 1.25rem; }}

/* Activity page only: a narrow column of status cards (this loop
   actually runs two independent daemons, GitLab issue review and topic
   monitoring - see .activity-card-stack below) beside a wide column of
   the loop's own review reports - unlike every other page's single-card
   .grid, this one is worth actually using as a grid once there's room
   for it. Stays single-column below the sidebar-collapse breakpoint used
   elsewhere in this file. */
.overview-layout {{ display: grid; grid-template-columns: 1fr; gap: 1.25rem; margin-bottom: 1.25rem; align-items: start; }}
@media (min-width: 901px) {{
  .overview-layout {{ grid-template-columns: minmax(280px, 360px) 1fr; }}
}}
/* Shared by both .overview-layout columns: the left column stacks
   GitLab Monitor and Topic Monitor as their own always-visible cards
   (they used to share one tabbed card, so switching tabs is never
   required to see either one's status); the right column stacks the
   GitLab loop's Latest Run Review above the topic monitor's Latest
   Topic Run Review the same way. */
.activity-card-stack {{ display: flex; flex-direction: column; gap: 1.25rem; min-width: 0; }}

.card {{
  background: var(--md-nav-surface);
  border-radius: 12px;
  padding: 1.25rem 1.5rem;
  min-width: 0;
}}

/* Every page section's background now matches the sidebar/topbar's own
   accent wash (--md-nav-surface) instead of the mode-based surface tiers
   - see .card above. For the "Default" accent that's a no-op in practice
   (--md-nav-surface already resolves to --md-surface-container-low, a
   mode-based tone from the same ladder .card used before), but the five
   named accents are a fixed, mode-independent light wash (see the
   :root[data-accent="..."] blocks above) - unlike the sidebar, which has
   always had its own dedicated on-surface/active tokens for exactly this
   reason, ordinary card content (headings, body text, links, buttons,
   inputs, nested chips like .learning-item/.gitlab-item, code blocks)
   reads its color from the plain mode-based --md-on-surface(-variant) and
   --md-primary tokens - in dark mode those are light colors meant to sit
   on a dark surface, so left alone they'd go straight to unreadable
   against this now-light card background.
   Redirecting the surface/text/primary custom properties themselves,
   scoped to .card, fixes every one of those nested rules at once without
   touching each individually - CSS custom properties resolve using the
   cascade at the element that *uses* them, so anything inside .card that
   asks for var(--md-on-surface) etc. picks up these instead. Scoped to
   the five named accents only (not [data-accent="default"] or its
   absence): default's own --md-nav-active-on-surface is itself defined
   as var(--md-primary) (see :root[data-accent="default"] above), so
   applying this same remap there would make --md-primary depend on
   itself through --md-nav-active-on-surface - a circular custom property,
   which computes to invalid and would blank out every link/icon/button
   that uses it. Default doesn't need the remap anyway, since its wash
   already tracks the current color mode.
   --md-outline/--md-outline-variant join the same redirect for the same
   reason: left as the plain mode-based grays, every border inside a
   tinted card (table rows, the Dashboard's "no run logged" dashed
   activity-bar outline) would sit as a flat gray line on top of a
   colored wash instead of picking up the accent family like everything
   else in the card - the "no run" bars in the Last 7 days strip were the
   most visible instance of this. --md-success-container/
   --md-warning-container (the strip's "quiet"/"escalation" bars) are
   deliberately NOT redirected here - those need to stay their fixed
   green/amber regardless of accent so the strip's outcome color-coding
   stays legible; only --md-primary-container (the "mr" bar) is meant to
   track the chosen accent. */
:root[data-accent="indigo"] .card,
:root[data-accent="blue"] .card,
:root[data-accent="green"] .card,
:root[data-accent="red"] .card,
:root[data-accent="gray"] .card {{
  --md-surface-dim: var(--md-nav-active-surface);
  --md-surface: var(--md-nav-active-surface);
  --md-surface-container-lowest: var(--md-nav-surface);
  --md-surface-container-low: var(--md-nav-active-surface);
  --md-surface-container: var(--md-nav-active-surface);
  --md-surface-container-high: var(--md-nav-active-surface);
  --md-surface-container-highest: var(--md-nav-active-surface);
  --md-on-surface: var(--md-nav-on-surface);
  --md-on-surface-variant: var(--md-nav-on-surface);
  --md-outline: var(--md-nav-on-surface);
  --md-outline-variant: var(--md-nav-active-surface);
  --md-primary: var(--md-nav-active-on-surface);
  --md-on-primary: var(--md-nav-surface);
  --md-primary-container: var(--md-nav-active-surface);
  --md-on-primary-container: var(--md-nav-active-on-surface);
  color: var(--md-nav-on-surface);
}}

/* Generic tabbed-card styling for the data-tabs/data-tab-target/
   data-tab-panel script in _render_shell - no page currently uses it
   (the Activity page's GitLab Monitor / Topic Monitor tabs were
   replaced by two always-visible stacked cards, see
   .activity-card-stack), but the mechanism stays available for a
   future page that genuinely needs only one panel visible at a time. */
.tab-list {{ display: flex; gap: 0.25rem; margin: -0.25rem 0 1rem; border-bottom: 1px solid var(--md-outline-variant); }}
.tab-button {{
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;
  padding: 0.6rem 0.25rem;
  font-family: var(--font-family-stack);
  font-size: 0.85rem;
  font-weight: 500;
  color: var(--md-on-surface-variant);
  cursor: pointer;
  transition: color 150ms ease, border-color 150ms ease;
}}
.tab-button:hover {{ color: var(--md-on-surface); }}
.tab-button.is-active {{ color: var(--md-primary); border-bottom-color: var(--md-primary); }}
.tab-button .material-symbols-outlined {{ font-size: 16px; }}
/* The GitLab Monitor tab's SVG mark - sized down from its 18px default
   (see _SECTION_ICON_GITLAB) to match the Topic Monitor tab's 16px
   Material Symbols glyph right next to it. */
.tab-button svg {{ width: 16px; height: 16px; }}

/* Overview page's GitLab Monitor / Topic Monitor tab panels: the state
   pill (see .pill-lg below) carries the one value that matters at a
   glance, so it's pulled out of the field list entirely rather than
   sitting in it at the same weight as "Updated at". */
.status-hero {{ margin: 0.25rem 0 1rem; }}

.field-list {{ display: grid; gap: 0; margin: 0 0 0.5rem; padding: 0; list-style: none; }}
.field-list li {{
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 0.4rem 0.75rem;
  font-size: 0.85rem;
  padding: 0.5rem 0;
  border-top: 1px solid var(--md-outline-variant);
}}
.field-list li:first-child {{ border-top: none; padding-top: 0; }}
.field-list .k {{ font-weight: 500; color: var(--md-on-surface-variant); }}

/* Run now's own action area (see _run_now_action_html) - shared by
   render_overview_page's two loop cards and render_topic_monitor_page's
   own button, not overview-specific despite the name it started with -
   visually separated from whatever's above it rather than just trailing
   off the bottom of the card, and, since it's the card's one primary
   action, full-width like a card footer button rather than an
   inline-sized one. */
.run-now-action {{ margin-top: 0.85rem; padding-top: 0.85rem; border-top: 1px solid var(--md-outline-variant); }}
.run-now-action button {{ width: 100%; justify-content: center; }}
.run-now-action button:disabled {{ opacity: 0.5; cursor: not-allowed; }}
.run-now-hint {{ margin: 0.6rem 0 0; font-size: 0.8rem; color: var(--md-on-surface-variant); }}
.message-list {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 0.5rem; }}
.message-row {{ display: flex; align-items: flex-end; gap: 0.35rem; }}
.message-row-user {{ justify-content: flex-end; }}
.message-row-loop {{ justify-content: flex-start; }}
.message-bubble {{
  max-width: 75%;
  border-radius: 16px;
  padding: 0.6rem 0.85rem;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
}}
/* A chat-bubble "tail" corner (the flat corner nearest the thread's own
   edge) instead of a uniformly rounded rectangle - user messages sit
   flush-right so their tail is bottom-right; loop messages sit
   flush-left so theirs is bottom-left. */
.message-bubble-user {{ background: var(--md-primary-container); color: var(--md-on-primary-container); border-bottom-right-radius: 4px; }}
.message-bubble-loop {{ background: var(--md-surface-container-highest); color: var(--md-on-surface); border-bottom-left-radius: 4px; }}
.message-meta {{ display: flex; align-items: center; gap: 0.5rem; margin-bottom: 0.15rem; }}
.message-brand-icon {{ color: var(--md-primary); vertical-align: middle; }}
.message-meta .k {{ font-size: 0.78rem; font-weight: 500; }}
.message-time {{ font-size: 0.72rem; opacity: 0.75; }}
/* A centered date label between messages sent on different days (see
   _message_date/_day_separator_label) - the same convention as
   full-featured chat UIs, so a long thread reads as a timeline rather
   than one undifferentiated stack of bubbles. */
.message-day-sep {{ display: flex; justify-content: center; margin: 0.4rem 0; }}
.message-day-sep span {{
  font-size: 0.72rem;
  font-weight: 500;
  color: var(--md-on-surface-variant);
  background: var(--md-surface-container-high);
  padding: 0.2rem 0.7rem;
  border-radius: 999px;
}}
/* Consecutive messages from the same sender (no day separator between
   them) sit closer together than a sender change does, so the thread
   groups by "who's talking" instead of every bubble having identical
   breathing room. */
.message-row-consecutive {{ margin-top: -0.25rem; }}
.message-text {{ font-size: 0.9rem; }}
.message-text.markdown > :last-child {{ margin-bottom: 0; }}
.message-delete-form {{ margin: 0; }}
.message-delete-form button {{
  background: none;
  border: none;
  cursor: pointer;
  color: var(--md-on-surface-variant);
  display: flex;
  align-items: center;
  padding: 0.3rem;
  border-radius: 50%;
}}
.message-delete-form button:hover {{ background: var(--md-surface-container-highest); color: var(--md-error); }}
.message-delete-form .material-symbols-outlined {{ font-size: 18px; }}

pre.log {{
  background: var(--md-surface-dim);
  border: 1px solid var(--md-outline-variant);
  border-radius: 6px;
  padding: 0.85rem 1rem;
  overflow-x: auto;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  margin: 0.5rem 0 0;
  line-height: 1.5;
  color: var(--md-on-surface-variant);
}}

/* Logs page: each append_unified_log call (see _parse_unified_log_entries,
   _log_entry_html) gets its own bordered block instead of one continuous
   <pre> dump, so a reader can see at a glance where one call's output
   ends and the next begins. */
.log-entries {{ display: flex; flex-direction: column; gap: 0.75rem; margin-top: 0.5rem; }}
.log-entry {{
  border: 1px solid var(--md-outline-variant);
  border-radius: 8px;
  overflow: hidden;
}}
.log-entry-header {{
  display: flex;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 0.5rem;
  padding: 0.5rem 0.85rem;
  background: var(--md-surface-container-highest);
  font-size: 0.8rem;
}}
.log-entry-source {{ font-weight: 500; color: var(--md-on-surface); }}
.log-entry-detail {{ color: var(--md-on-surface-variant); }}
.log-entry-time {{ margin-left: auto; color: var(--md-on-surface-variant); opacity: 0.75; font-size: 0.75rem; white-space: nowrap; }}
.log-entry-meta {{ color: var(--md-on-surface-variant); font-style: italic; font-size: 0.8rem; }}
.log-entry-body {{
  background: var(--md-surface-dim);
  margin: 0;
  padding: 0.75rem 0.85rem;
  overflow-x: auto;
  font-size: 0.85rem;
  white-space: pre-wrap;
  word-break: break-word;
  line-height: 1.5;
  color: var(--md-on-surface-variant);
}}

.markdown {{
  color: var(--md-on-surface-variant);
  font-size: 0.9rem;
  line-height: 1.6;
  margin-top: 0.5rem;
}}
.markdown h1, .markdown h2, .markdown h3, .markdown h4, .markdown h5, .markdown h6 {{
  color: var(--md-on-surface);
  margin: 1.1rem 0 0.5rem;
}}
.markdown > :first-child {{ margin-top: 0; }}
.markdown p {{ margin: 0 0 0.75rem; }}
.markdown ul, .markdown ol {{ margin: 0 0 0.75rem; padding-left: 1.4rem; }}
.markdown li {{ margin: 0.2rem 0; }}
.markdown strong {{ color: var(--md-on-surface); }}
.markdown a {{ color: var(--md-primary); }}
.markdown code {{
  background: var(--md-surface-dim);
  border: 1px solid var(--md-outline-variant);
  border-radius: 4px;
  padding: 0.1rem 0.35rem;
  font-size: 0.85em;
}}
.markdown pre {{
  background: var(--md-surface-dim);
  border: 1px solid var(--md-outline-variant);
  border-radius: 6px;
  padding: 0.85rem 1rem;
  overflow-x: auto;
  margin: 0 0 0.75rem;
}}
.markdown pre code {{ background: none; border: none; padding: 0; }}
.markdown img {{ max-width: 100%; height: auto; }}

ul.plain {{ list-style: none; margin: 0.35rem 0 0; padding: 0; display: grid; gap: 0.35rem; }}
ul.plain li {{ font-size: 0.9rem; }}

.learning-item {{
  background: var(--md-surface-container);
  border-radius: 8px;
  padding: 0.6rem 0.75rem;
}}
.learning-item .markdown {{ font-size: 0.9rem; }}
.learning-item .markdown > :last-child {{ margin-bottom: 0; }}
.pill-row {{ display: flex; flex-wrap: wrap; gap: 0.35rem; margin-top: 0.35rem; }}

.gitlab-list {{ gap: 0.15rem; }}
.gitlab-item {{
  background: var(--md-surface-container);
  border-radius: 8px;
  padding: 0.3rem 0.65rem;
}}
.gitlab-item-row {{ display: flex; align-items: baseline; justify-content: space-between; gap: 0.75rem; }}
.gitlab-item-title {{ font-weight: 500; color: var(--md-on-surface); text-decoration: none; flex: 1 1 auto; min-width: 0; }}
.gitlab-item-title:hover {{ color: var(--md-primary); text-decoration: underline; }}
.gitlab-item-meta {{ font-size: 0.78rem; color: var(--md-on-surface-variant); flex-shrink: 0; white-space: nowrap; text-align: right; }}

table.daemons {{ border-collapse: collapse; width: 100%; font-size: 0.87rem; }}
table.daemons th {{
  text-align: left;
  padding: 0.5rem 0.6rem;
  border-bottom: 2px solid var(--md-outline-variant);
  background: var(--md-surface-container-high);
  color: var(--md-on-surface);
  font-family: var(--font-family-stack);
  font-weight: 500;
}}
table.daemons td {{ text-align: left; padding: 0.55rem 0.6rem; border-bottom: 1px solid var(--md-outline-variant); vertical-align: top; }}
table.daemons tbody tr {{ transition: background-color 150ms ease; }}
table.daemons tbody tr:hover {{ background: var(--md-surface-container-low); }}
table.daemons code {{
  font-size: 0.85em;
  word-break: break-all;
  background: var(--md-surface-dim);
  padding: 0.1rem 0.3rem;
  border-radius: 4px;
}}
.table-wrap {{ overflow-x: auto; }}

/* Skills page: Used-by/Path aren't columns - each skill's summary row
   (name/status/description) is immediately followed by its own detail
   row, hidden until that summary row gets .is-expanded (see
   render_skills_page's onclick). The `+` sibling selector is why the
   detail row must come directly after its own summary row in the HTML. */
table.skills tr.skill-row {{ cursor: pointer; }}
table.skills tr.skill-detail-row {{ display: none; }}
table.skills tr.skill-row.is-expanded + tr.skill-detail-row {{ display: table-row; }}
table.skills tr.skill-detail-row td {{ background: var(--md-surface-container-low); }}
table.skills tr.skill-detail-row p {{ margin: 0 0 0.2rem; }}
table.skills tr.skill-detail-row p:not(:first-child) {{ margin-top: 0.6rem; }}
.skill-expand-icon {{ font-size: 16px; vertical-align: middle; color: var(--md-outline); transition: transform 150ms ease; }}
table.skills tr.skill-row.is-expanded .skill-expand-icon {{ transform: rotate(180deg); }}

.pill {{
  display: inline-flex;
  align-items: center;
  gap: 0.35rem;
  font-size: 0.75rem;
  font-weight: 500;
  padding: 0.3rem 0.75rem;
  border-radius: 999px;
  line-height: 1;
  white-space: nowrap;
  text-decoration: none;
}}
.pill-blue {{ background: var(--md-primary-container); color: var(--md-on-primary-container); }}
/* A pill that's also a link (e.g. the Project Memory page's issue-
   number badge, once it resolves to a real GitLab URL - see
   render_memory_page) - same colors as .pill-blue so a reader can
   tell "this one is clickable" apart from a plain .pill-grey tag,
   underlining only on hover/focus so the pill shape alone doesn't read
   as a wall of underlined text. */
.pill-link {{ background: var(--md-primary-container); color: var(--md-on-primary-container); }}
.pill-link:hover, .pill-link:focus-visible {{ text-decoration: underline; }}
.pill-green {{ background: var(--md-success-container); color: var(--md-on-success-container); }}
.pill-red {{ background: var(--md-error-container); color: var(--md-on-error-container); }}
.pill-grey {{ background: var(--md-surface-container-highest); color: var(--md-on-surface-variant); }}
/* Overview page's status-hero pill: the same state pill shown small in
   the topbar on every page, sized up since here it's the Latest Run
   card's headline value, not a small persistent indicator. */
.pill-lg {{ font-size: 0.95rem; padding: 0.5rem 1rem; gap: 0.45rem; }}
.pill-lg .material-symbols-outlined {{ font-size: 18px; }}

.badge-count {{
  display: inline-flex;
  align-items: center;
  justify-content: center;
  min-width: 1.35rem;
  padding: 0.05rem 0.4rem;
  border-radius: 999px;
  background: var(--md-primary-container);
  color: var(--md-on-primary-container);
  font-size: 0.72rem;
  font-weight: 700;
}}

.project-block + .project-block {{ margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--md-outline-variant); }}

/* Shared "nothing to show because setup is missing" state - see
   _empty_state_html - used by the Live GitLab and Memory pages in
   place of a bare "(no projects configured)" line, since the fix here
   is always the same one click away (the GitLab settings page) and a
   plain text line was too easy to skim past. */
.empty-state {{
  display: flex;
  flex-direction: column;
  align-items: center;
  text-align: center;
  gap: 0.5rem;
  padding: 2.5rem 1.5rem;
  color: var(--md-on-surface-variant);
}}
.empty-state-icon {{ color: var(--md-outline); }}
.empty-state-icon .material-symbols-outlined {{ font-size: 40px; }}
.empty-state-message {{ margin: 0; font-size: 0.9rem; max-width: 32rem; }}
.empty-state-action {{ margin-top: 0.5rem; text-decoration: none; }}

.history-entry + .history-entry {{ margin-top: 0.85rem; padding-top: 0.85rem; border-top: 1px solid var(--md-outline-variant); }}
.history-entry-header {{ display: flex; align-items: center; gap: 0.6rem; flex-wrap: wrap; }}
.history-entry-header .pill-row {{ margin-top: 0; flex: 1 1 auto; }}
.history-entry-header form {{ margin: 0; }}
.history-delete-btn {{ padding: 0.3rem; }}
.history-entry-overview {{ margin: 0.3rem 0 0; color: var(--md-on-surface-variant); font-size: 0.85rem; }}

/* "last run 3h ago" beside a Topic Monitor heading: secondary information
   next to the state pill, so it reads at the weight of metadata rather
   than competing with the topic's own name. */
.topic-last-run {{ font-size: 0.78rem; font-weight: 400; color: var(--md-on-surface-variant); }}

/* Topic Monitor's Latest Data section: each topic's summary block is
   immediately followed by its own detail block, hidden until the summary
   gets .is-expanded (see render_topic_monitor_page's onclick) - same
   sibling-selector convention as table.skills's summary/detail rows, just
   with divs instead of table rows since a full rendered briefing needs to
   flow rather than sit in a table cell. */
.topic-latest-item + .topic-latest-item {{ margin-top: 1rem; padding-top: 1rem; border-top: 1px solid var(--md-outline-variant); }}
.topic-latest-summary {{ cursor: pointer; }}
.topic-latest-summary .skill-expand-icon {{ margin-left: 0.2rem; }}
.topic-latest-summary.is-expanded .skill-expand-icon {{ transform: rotate(180deg); }}
.topic-latest-overview {{ margin: 0.3rem 0 0; color: var(--md-on-surface-variant); font-size: 0.85rem; }}
.topic-latest-detail {{ display: none; margin-top: 0.75rem; }}
.topic-latest-summary.is-expanded + .topic-latest-detail {{ display: block; }}

.flash {{
  border-radius: 8px;
  border: 1px solid transparent;
  padding: 0.75rem 1rem;
  margin-bottom: 1.25rem;
  font-size: 0.9rem;
  font-weight: 500;
}}
.flash-success {{
  background: var(--md-success-container);
  border-color: var(--md-success-container);
  color: var(--md-on-success-container);
}}
.flash-danger {{
  background: var(--md-error-container);
  border-color: var(--md-error-container);
  color: var(--md-on-error-container);
}}

/* Replaces the browser's native "Please fill out this field."-style
   validation bubble (unstyled OS chrome, can't be restyled with CSS) with
   an MD3-styled one built by JS - same color pairing as .flash-danger,
   positioned like a tooltip next to the invalid field. position: fixed
   for the same reason .custom-select-menu uses it: escapes any ancestor's
   overflow clipping (e.g. .table-wrap). */
.field-error-bubble {{
  position: fixed;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 0.4rem;
  background: var(--md-error-container);
  color: var(--md-on-error-container);
  border-radius: 8px;
  padding: 0.5rem 0.75rem;
  font-size: 0.8rem;
  font-weight: 500;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  max-width: 320px;
}}
.field-error-bubble .material-symbols-outlined {{ font-size: 18px; flex-shrink: 0; }}
.field-error-bubble[hidden] {{ display: none; }}

/* Replaces the browser's native, unstyled confirmation popup for every
   destructive action - Delete/Clear/Enable buttons carry a
   data-confirm="<message>" attribute instead of an inline click handler
   that calls the native dialog directly; JS in _render_shell intercepts
   the click, fills in this dialog, and submits the button's own form only
   once the user accepts. <dialog> gives real modal behavior (focus trap,
   Escape to close, backdrop) for free - no custom overlay/z-index
   management. */
.confirm-dialog {{
  border: none;
  border-radius: 24px;
  padding: 2rem;
  background: var(--md-surface-container-high);
  color: var(--md-on-surface);
  width: 90vw;
  max-width: 520px;
  min-width: 360px;
  box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
}}
.confirm-dialog::backdrop {{ background: rgba(0, 0, 0, 0.5); }}
.confirm-dialog-icon {{ color: var(--md-error); margin-bottom: 0.75rem; }}
.confirm-dialog-icon .material-symbols-outlined {{ font-size: 36px; }}
.confirm-dialog-message {{ margin: 0 0 2rem; font-size: 1.1rem; line-height: 1.5; }}
.confirm-dialog-actions {{ display: flex; justify-content: flex-end; gap: 0.75rem; }}
.confirm-dialog-actions button {{ padding: 0.55rem 1.25rem; font-size: 0.9rem; }}

.inline-error {{
  display: flex;
  align-items: center;
  gap: 0.3rem;
  color: var(--md-error);
  font-size: 0.8rem;
  margin: 0.25rem 0 0.5rem;
}}
.inline-error .material-symbols-outlined {{ font-size: 16px; flex-shrink: 0; }}

.daemon-action-form {{ margin: 0; display: flex; flex-wrap: wrap; gap: 0.4rem; align-items: center; }}
/* The "add a new instance/project" row sits directly under a table with no
   gap of its own - give it some breathing room from the last table row
   above it. */
.daemon-action-form.add-row-form {{ margin-top: 0.85rem; }}
/* Topic Settings row (render_topic_settings_page): label + Slack bundle
   share line 1, the "what counts as notable" description gets its own
   full-width line 2 (flex-basis: 100% forces the wrap inside the already
   flex-wrap: wrap .daemon-action-form), and Save/Delete sit together as
   one button column beside the fields rather than Delete trailing below
   in its own separate row. The Delete <form> itself renders with no
   visible content (just its CSRF input) - its button lives in
   .topic-row-actions and targets it via `form=`, the same trick used for
   Save targeting the edit form it isn't nested inside. */
.topic-row {{ display: flex; gap: 0.75rem; align-items: flex-start; }}
.topic-row-fields {{ flex: 1 1 auto; min-width: 0; }}
.topic-row-fields .topic-row-brief {{ flex-basis: 100%; }}
.topic-row-actions {{ display: flex; flex-direction: column; gap: 0.4rem; flex: 0 0 auto; margin: 0; }}
.topic-row-actions form {{ margin: 0; }}
.daemon-action-form input[type='text'],
.daemon-action-form input[type='password'],
.daemon-action-form input[type='time'],
.daemon-action-form select,
.daemon-action-form textarea {{
  font-family: var(--font-family-stack);
  font-size: 0.8rem;
  padding: 0.35rem 0.5rem;
  border-radius: 8px;
  border: 1px solid var(--md-outline);
  background: var(--md-surface);
  color: var(--md-on-surface);
  flex: 1 1 160px;
  min-width: 120px;
  transition: border-color 150ms ease;
}}
.daemon-action-form input[type='time'] {{ flex: 0 0 auto; min-width: 0; }}
.weekday-checks {{ display: flex; flex-wrap: wrap; gap: 0.35rem; }}
.weekday-check {{ display: inline-flex; align-items: center; gap: 0.2rem; font-size: 0.78rem; }}
.monthly-controls {{ display: inline-flex; align-items: center; gap: 0.35rem; font-size: 0.8rem; color: var(--md-on-surface-variant); }}
/* A Material Design 3 style filled checkbox (18px box, rounded corners,
   a CSS-only checkmark via clip-path - no image asset) in place of the
   browser's native checkbox, for the schedule editor's weekday picker.
   `appearance: none` strips all native styling so these three rules are
   the checkbox's entire visual, in both the unchecked and checked state. */
.md-checkbox {{ cursor: pointer; user-select: none; }}
.md-checkbox input[type='checkbox'] {{
  appearance: none; -webkit-appearance: none;
  width: 18px; height: 18px; margin: 0;
  border: 2px solid var(--md-outline);
  border-radius: 3px;
  background: transparent;
  display: inline-grid;
  place-content: center;
  cursor: pointer;
  vertical-align: middle;
  transition: background-color 120ms ease, border-color 120ms ease;
}}
.md-checkbox input[type='checkbox']::before {{
  content: "";
  width: 10px;
  height: 10px;
  transform: scale(0);
  transition: transform 100ms ease;
  background: var(--md-on-primary);
  clip-path: polygon(14% 44%, 0 65%, 50% 100%, 100% 16%, 80% 0%, 43% 62%);
}}
.md-checkbox input[type='checkbox']:checked {{ background: var(--md-primary); border-color: var(--md-primary); }}
.md-checkbox input[type='checkbox']:checked::before {{ transform: scale(1); }}
.md-checkbox input[type='checkbox']:focus-visible {{ outline: 2px solid var(--md-primary); outline-offset: 2px; }}
/* The Instructions textarea is the only multi-line field in any form
   here - it needs the full width of the card and its own line, not to
   compete for space in a row of short fields like every other input. */
.daemon-action-form textarea {{
  flex-basis: 100%;
  font-family: var(--font-family-stack);
  resize: vertical;
}}
/* Bigger than a generic form textarea: this is a page you write real
   prose into (potentially many paragraphs of standing instructions), not
   a short parameter field, so it gets a roomier font size and a tall
   minimum height in addition to its 24-row default. */
.instructions-textarea {{
  font-size: 0.95rem;
  line-height: 1.5;
  min-height: 420px;
}}
/* A line with exactly one field and one button: fix the button's width so
   every remaining pixel on the line goes to the field instead of the field
   sizing to its placeholder text. */
.daemon-action-form.single-field {{ flex-wrap: nowrap; }}
.daemon-action-form.single-field input,
.daemon-action-form.single-field .custom-select {{ flex: 1 1 auto; min-width: 0; }}
.daemon-action-form.single-field button[type='submit'] {{ flex: 0 0 120px; justify-content: center; }}

/* Custom-styled dropdown: replaces the browser's native <select> popup
   (which can't be restyled - it always renders with the OS's own menu
   chrome) with a JS-driven trigger + listbox built from this dashboard's
   own tokens. The real <select> stays in the DOM, just hidden, so form
   submission works exactly like a plain <select> would. */
.custom-select {{ position: relative; display: inline-flex; flex: 1 1 160px; min-width: 120px; }}
.custom-select-native {{ display: none; }}
.custom-select-trigger {{
  width: 100%;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 0.5rem;
  font-family: var(--font-family-stack);
  font-size: 0.8rem;
  padding: 0.35rem 0.5rem;
  border-radius: 8px;
  border: 1px solid var(--md-outline);
  background: var(--md-surface);
  color: var(--md-on-surface);
  cursor: pointer;
  transition: border-color 150ms ease, background-color 150ms ease;
}}
.custom-select-trigger:hover {{ background: var(--md-surface-container-high); }}
.custom-select-trigger:focus-visible {{ border-color: var(--md-primary); border-width: 2px; outline: none; }}
.custom-select.is-open .custom-select-trigger {{ border-color: var(--md-primary); border-width: 2px; }}
.custom-select-trigger .material-symbols-outlined {{
  font-size: 18px;
  color: var(--md-on-surface-variant);
  transition: transform 150ms ease;
}}
.custom-select.is-open .custom-select-trigger .material-symbols-outlined {{ transform: rotate(180deg); }}
.custom-select-menu {{
  /* position: fixed (not absolute) so this isn't clipped by .table-wrap's
     scroll boundary - top/left/width are computed from the trigger's
     getBoundingClientRect() in JS when the menu opens, since fixed
     positioning has no relation to the trigger's location otherwise. */
  position: fixed;
  z-index: 20;
  background: var(--md-surface-container);
  border: 1px solid var(--md-outline-variant);
  border-radius: 8px;
  box-shadow: 0 4px 12px rgba(0, 0, 0, 0.4);
  padding: 0.25rem;
  max-height: 240px;
  overflow-y: auto;
}}
.custom-select-menu[hidden] {{ display: none; }}
.custom-select-option {{
  padding: 0.35rem 0.6rem;
  border-radius: 6px;
  font-size: 0.8rem;
  color: var(--md-on-surface);
  cursor: pointer;
}}
.custom-select-option:hover {{ background: var(--md-surface-container-high); }}
.custom-select-option.is-selected {{ color: var(--md-primary); font-weight: 500; }}
/* Keyboard focus while the listbox is open (see the ArrowUp/ArrowDown
   handling in _render_shell's script) - outline-offset is negative since
   these sit flush against the menu's own padding, an outward ring would
   get clipped by .custom-select-menu's overflow-y: auto. */
.custom-select-option:focus-visible {{ background: var(--md-surface-container-high); outline: 2px solid var(--md-primary); outline-offset: -2px; }}
.daemon-action-form input[type='text']:focus,
.daemon-action-form input[type='password']:focus,
.daemon-action-form select:focus,
.daemon-action-form textarea:focus {{
  border-color: var(--md-primary);
  border-width: 2px;
  outline: none;
}}
.btn {{
  font-family: var(--font-family-stack);
  font-size: 0.8rem;
  font-weight: 500;
  padding: 0.4rem 0.9rem;
  border-radius: 999px;
  border: 1px solid transparent;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 0.3rem;
  transition: background-color 150ms ease, color 150ms ease, border-color 150ms ease;
}}
.btn-warning {{
  background: var(--md-warning-container);
  color: var(--md-on-warning-container);
  border-color: var(--md-warning-container);
}}
.btn-warning:hover {{ background: var(--md-warning); color: var(--md-on-warning); }}
.btn-neutral {{
  background: transparent;
  color: var(--md-on-surface);
  border-color: var(--md-outline);
}}
.btn-neutral:hover {{ background: var(--md-surface-container-high); }}
.btn-primary {{
  background: var(--md-primary);
  color: var(--md-on-primary);
  border-color: var(--md-primary);
}}
.btn-primary:hover {{ background: var(--md-primary-container); color: var(--md-on-primary-container); }}

.switch {{
  position: relative;
  width: 40px;
  height: 22px;
  padding: 0;
  border-radius: 999px;
  border: 1px solid var(--md-outline);
  background: var(--md-surface-container-highest);
  cursor: pointer;
  flex-shrink: 0;
  transition: background-color 150ms ease, border-color 150ms ease;
}}
.switch-thumb {{
  position: absolute;
  top: 1px;
  left: 1px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: var(--md-on-surface-variant);
  transition: transform 150ms ease, background-color 150ms ease;
}}
.switch.is-on {{
  background: var(--md-success-container);
  border-color: var(--md-success-container);
}}
.switch.is-on .switch-thumb {{
  transform: translateX(18px);
  background: var(--md-on-success-container);
}}
.switch.is-off:hover {{ border-color: var(--md-warning); }}
.switch.is-off:hover .switch-thumb {{ background: var(--md-warning); }}

.progress-pulse {{ display: inline-flex; align-items: center; margin-right: 0.4rem; vertical-align: middle; }}

/* A moving gradient sliver under the topbar - visible on every page, not
   just Activity - so switching away from Activity while the loop is
   running doesn't lose the "something is actively happening" signal. Its
   .is-active class is set in _render_shell from the same "running" check
   that already drives the status pill's pulsing dot, not a separate
   state check. */
.topbar-progress-bar {{ position: absolute; bottom: -1px; left: 0; right: 0; height: 3px; overflow: hidden; }}
.topbar-progress-bar.is-active::before {{
  content: '';
  position: absolute;
  top: 0;
  left: -50%;
  width: 50%;
  height: 100%;
  background: linear-gradient(90deg, transparent, var(--md-primary), var(--md-primary-container), transparent);
}}

/* A page section loaded via data-lazy-load (see _render_shell's script)
   shows this in place of its real content until the fetch resolves - a
   layered, two-ring indeterminate spinner, plain CSS, no assets. The
   element itself draws no border; ::before/::after each draw one ring so
   they can spin in opposite directions at different speeds - reads as a
   genuine "orbiting" loader rather than one ring going around. */
.lazy-loading {{ display: flex; flex-direction: column; align-items: center; gap: 1rem; padding: 3rem 0; }}
.md-spinner {{ position: relative; width: 88px; height: 88px; }}
.md-spinner::before,
.md-spinner::after {{
  content: '';
  position: absolute;
  border-radius: 50%;
  border-style: solid;
  border-color: transparent;
}}
.md-spinner::before {{
  inset: 0;
  border-width: 7px;
  border-top-color: var(--md-primary);
  border-right-color: var(--md-primary-container);
}}
.md-spinner::after {{
  inset: 18px;
  border-width: 6px;
  border-bottom-color: var(--md-success);
  border-left-color: var(--md-success-container);
}}
.loading-text {{ margin: 0; font-size: 0.85rem; color: var(--md-on-surface-variant); }}
/* Static and fully visible by default ("Loading live GitLab data...") -
   only animated (each dot fading in turn) under
   prefers-reduced-motion: no-preference, below. */
.loading-dots span {{ opacity: 1; }}

/* A small inline variant of the same spinner, for a status line that
   needs a "something's actively happening" signal without the full
   88px loading-placeholder version - e.g. the Activity page's Current
   Progress line. */
.md-spinner.md-spinner-sm {{ width: 20px; height: 20px; }}
.md-spinner.md-spinner-sm::before {{ border-width: 3px; }}
.md-spinner.md-spinner-sm::after {{ inset: 5px; border-width: 2px; }}

/* _SPINNER_ICON: the same spinner again, sized in em rather than a fixed
   px, for every "running"/"in progress" pill (the header status badge,
   the Dashboard page's hero pills, per-topic badges, the Skills page's
   "setup in progress" pill) - one variant that scales with whichever
   pill's own font-size it lands in (.pill's 0.75rem or .pill-lg's
   0.95rem) instead of needing a separate fixed-px size per pill. This
   replaced a plain pulsing dot, which read as much less "alive" than
   this already-established spinner treatment. */
.md-spinner.md-spinner-pill {{ width: 0.9em; height: 0.9em; }}
.md-spinner.md-spinner-pill::before {{ border-width: 0.14em; }}
.md-spinner.md-spinner-pill::after {{ inset: 0.16em; border-width: 0.12em; }}

/* README page: a floating "on this page" card, built from the doc's own
   H2 headings, fixed to the top-right of the viewport so it stays
   reachable no matter how far down the README you've scrolled - rather
   than a row of chips that scrolls away with the content above it. */
.readme-quicknav {{
  position: fixed;
  top: 4.75rem;
  right: 1.5rem;
  z-index: 85;
  display: flex;
  flex-direction: column;
  gap: 0.2rem;
  width: 200px;
  max-height: calc(100vh - 7rem);
  overflow-y: auto;
  padding: 0.75rem;
  border-radius: 12px;
  background: var(--md-surface-container-high);
  border: 1px solid var(--md-outline-variant);
  box-shadow: 0 4px 16px rgba(0, 0, 0, 0.35);
}}
.readme-quicknav-title {{
  margin: 0 0 0.35rem;
  padding: 0 0.6rem;
  font-family: var(--font-family-stack);
  font-size: 0.7rem;
  font-weight: 700;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--md-outline);
}}
.readme-quicknav-link {{
  padding: 0.35rem 0.6rem;
  border-radius: 6px;
  color: var(--md-on-surface-variant);
  font-size: 0.8rem;
  font-weight: 500;
  line-height: 1.3;
}}
.readme-quicknav-link:hover {{ background: var(--md-primary-container); color: var(--md-on-primary-container); text-decoration: none; }}

@media (max-width: 900px) {{
  .readme-quicknav {{ display: none; }}
}}

/* Preferences page: a segmented control for color mode, a row of
   swatches for accent - both apply instantly via a page-local <script>
   (see render_preferences_page), no page reload, no server round-trip. */
.pref-segmented {{
  display: inline-flex;
  flex-wrap: wrap;
  border: 1px solid var(--md-outline-variant);
  border-radius: 999px;
  padding: 0.2rem;
  gap: 0.2rem;
}}
.pref-segmented-option {{
  border: none;
  background: none;
  color: var(--md-on-surface-variant);
  font-family: var(--font-family-stack);
  font-size: 0.85rem;
  font-weight: 500;
  padding: 0.4rem 1.1rem;
  border-radius: 999px;
  cursor: pointer;
  transition: background-color 150ms ease, color 150ms ease;
}}
.pref-segmented-option:hover {{ background: var(--md-surface-container-high); }}
.pref-segmented-option.is-active {{ background: var(--md-primary-container); color: var(--md-on-primary-container); }}

.pref-swatches {{ display: flex; flex-wrap: wrap; gap: 1rem; }}
.pref-swatch {{
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 0.5rem;
  border: 2px solid transparent;
  background: none;
  color: var(--md-on-surface-variant);
  font-family: var(--font-family-stack);
  font-size: 0.8rem;
  font-weight: 500;
  padding: 0.75rem;
  border-radius: 12px;
  cursor: pointer;
  transition: border-color 150ms ease, background-color 150ms ease;
}}
.pref-swatch:hover {{ background: var(--md-surface-container-high); }}
.pref-swatch.is-active {{ border-color: var(--md-primary); background: var(--md-surface-container-high); }}
/* A miniature sidebar+content layout, not a plain color dot - shows how
   the page will actually look with this accent, not just its color. */
.pref-swatch-preview {{
  display: flex;
  width: 140px;
  height: 100px;
  border-radius: 10px;
  overflow: hidden;
  border: 1px solid var(--md-outline-variant);
}}
.pref-swatch-preview-nav {{ width: 32%; }}
.pref-swatch-preview-content {{ flex: 1; background: var(--md-surface); }}

/* The loading spinner always spins, deliberately not gated behind
   prefers-reduced-motion like the decorative animations below - its
   motion is the only signal that the page is still working, not mere
   decoration, and an OS-level reduce-motion preference would otherwise
   make it look permanently frozen/broken rather than just calmer.
   The outer ring spins clockwise, the inner ring counter-clockwise and
   faster - two independently moving, differently colored rings read as
   a much livelier "orbiting" loader than a single ring ever could. */
.md-spinner::before {{ animation: md-spin-cw 1.1s linear infinite; }}
.md-spinner::after {{ animation: md-spin-ccw 0.75s linear infinite; }}
@keyframes md-spin-cw {{ to {{ transform: rotate(360deg); }} }}
@keyframes md-spin-ccw {{ to {{ transform: rotate(-360deg); }} }}

@media (prefers-reduced-motion: no-preference) {{
  .topbar-progress-bar.is-active::before {{ animation: topbar-progress-slide 1.6s linear infinite; }}
  @keyframes topbar-progress-slide {{ 0% {{ left: -50%; }} 100% {{ left: 100%; }} }}
  /* Each dot fades in and out in turn (staggered via animation-delay) -
     the classic "still working" typing-indicator look, instead of a
     single static "…" character. */
  .loading-dots span {{ animation: loading-dots-fade 1.4s infinite; }}
  .loading-dots span:nth-child(2) {{ animation-delay: 0.2s; }}
  .loading-dots span:nth-child(3) {{ animation-delay: 0.4s; }}
  @keyframes loading-dots-fade {{ 0%, 80%, 100% {{ opacity: 0; }} 40% {{ opacity: 1; }} }}
  #activity-message-list {{ scroll-behavior: smooth; }}
}}
"""

_CHECK_ICON = "<span class='material-symbols-outlined' aria-hidden='true'>check_circle</span>"
_EXPAND_ICON = "<span class='material-symbols-outlined skill-expand-icon' aria-hidden='true'>expand_more</span>"

_DOT_ICON_TEMPLATE = "<span class='material-symbols-outlined {cls}' aria-hidden='true'>circle</span>"

# The same two-ring spinner already used for the Activity page's Current
# Progress line (see .md-spinner in _STYLE), sized to sit inline inside a
# pill via .md-spinner-pill - every "running"/"in progress" pill uses
# this instead of a plain pulsing dot, for one consistent "actively
# working" treatment across the whole app.
_SPINNER_ICON = "<span class='md-spinner md-spinner-pill' aria-hidden='true'></span>"

# Purely decorative, static markup (no dynamic data ever flows through these,
# so they don't go through html.escape() like the rest of the page). The
# brand mark stays a hand-drawn inline SVG (currentColor, no external
# dependency) by design; every other icon constant below is a Material
# Symbols glyph name rendered through the Google Fonts-hosted icon font
# linked in _render_shell's head (see _MATERIAL_SYMBOLS_ICON_NAMES) - real
# Material Design iconography, not a hand-drawn approximation.
_BRAND_MARK_ICON = (
    "<svg class='brand-mark' viewBox='0 0 24 24' width='20' height='20' fill='none' "
    "stroke='currentColor' stroke-width='2' aria-hidden='true'>"
    "<circle cx='8' cy='12' r='4.5'/><circle cx='16' cy='12' r='4.5'/></svg>"
)

# A small brand-mark variant for chat bubbles (see render_activity_page) -
# not a reuse of _BRAND_MARK_ICON's own `brand-mark` class, since that
# class is `display: none` by default (only shown in the collapsed
# sidebar rail - see html.collapsed .brand-mark in _STYLE) and would
# render as invisible here.
_MESSAGE_BRAND_ICON = (
    "<svg class='message-brand-icon' viewBox='0 0 24 24' width='20' height='20' fill='none' "
    "stroke='currentColor' stroke-width='2' aria-hidden='true'>"
    "<circle cx='8' cy='12' r='4.5'/><circle cx='16' cy='12' r='4.5'/></svg>"
)

_SECTION_ICON_OVERVIEW = "<span class='material-symbols-outlined' aria-hidden='true'>space_dashboard</span>"

_SECTION_ICON_HISTORY = "<span class='material-symbols-outlined' aria-hidden='true'>history</span>"

_SECTION_ICON_ANALYTICS = "<span class='material-symbols-outlined' aria-hidden='true'>monitoring</span>"

# The real GitLab "tanuki" brand mark, not a Material Symbols glyph -
# that icon set has no generic "GitLab" glyph, so this is an inline SVG
# (path data from Simple Icons' gitlab.svg, a single monochrome outline
# meant to be recolored) drawn in currentColor - same reasoning and
# pattern as _SECTION_ICON_SLACK below: it inherits color exactly like
# every other nav/section-header/tab icon, rather than GitLab's own fixed
# brand orange.
_SECTION_ICON_GITLAB = (
    "<svg class='gitlab-mark' viewBox='0 0 24 24' width='18' height='18' fill='currentColor' aria-hidden='true'>"
    "<path d='m23.6 9.593-.033-.086L20.3.98a.85.85 0 0 0-.336-.405.875.875 0 0 0-1 .054.88.88 0 0 0-.29.44L16.47 "
    "7.818H7.537L5.333 1.07a.86.86 0 0 0-.29-.441.875.875 0 0 0-1-.054.86.86 0 0 0-.336.405L.433 9.502l-.032.086a"
    "6.066 6.066 0 0 0 2.012 7.01l.01.009.03.021 4.977 3.727 2.462 1.863 1.5 1.132a1.01 1.01 0 0 0 1.22 0l1.499-"
    "1.132 2.461-1.863 5.006-3.75.013-.01a6.07 6.07 0 0 0 2.01-7.002'/>"
    "</svg>"
)

_SECTION_ICON_MEMORY = "<span class='material-symbols-outlined' aria-hidden='true'>lightbulb</span>"

_SECTION_ICON_TOPIC_MONITOR = "<span class='material-symbols-outlined' aria-hidden='true'>newspaper</span>"

_SECTION_ICON_DAEMONS = "<span class='material-symbols-outlined' aria-hidden='true'>dns</span>"

_SECTION_ICON_SETTINGS = "<span class='material-symbols-outlined' aria-hidden='true'>settings</span>"
# The Slack mark, not a Material Symbols glyph - that icon set has no
# generic "Slack" glyph, so this is an inline SVG (same pattern as
# _BRAND_MARK_ICON above) sized to match the 18px Material Symbols glyphs
# it sits alongside in the nav and section headers. Drawn in currentColor
# (Slack's own brand colors deliberately dropped) so it inherits color
# exactly like every other nav/section-header icon - nav link color/hover/
# active state for free via inheritance, and --md-primary in section
# headers via the ".section-header .slack-mark" rule in _STYLE below.
_SECTION_ICON_SLACK = (
    "<svg class='slack-mark' viewBox='0 0 122.8 122.8' width='18' height='18' fill='currentColor' aria-hidden='true'>"
    "<path d='M25.8 77.6c0 7.1-5.8 12.9-12.9 12.9S0 84.7 0 77.6s5.8-12.9 12.9-12.9h12.9v12.9z'/>"
    "<path d='M32.3 77.6c0-7.1 5.8-12.9 12.9-12.9s12.9 5.8 12.9 12.9v32.3c0 7.1-5.8 12.9-12.9 12.9s-12.9-5.8-12.9-12.9V77.6z'/>"
    "<path d='M45.2 25.8c-7.1 0-12.9-5.8-12.9-12.9S38.1 0 45.2 0s12.9 5.8 12.9 12.9v12.9H45.2z'/>"
    "<path d='M45.2 32.3c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9H12.9C5.8 58.1 0 52.3 0 45.2s5.8-12.9 12.9-12.9h32.3z'/>"
    "<path d='M97 45.2c0-7.1 5.8-12.9 12.9-12.9s12.9 5.8 12.9 12.9-5.8 12.9-12.9 12.9H97V45.2z'/>"
    "<path d='M90.5 45.2c0 7.1-5.8 12.9-12.9 12.9s-12.9-5.8-12.9-12.9V12.9C64.7 5.8 70.5 0 77.6 0s12.9 5.8 12.9 12.9v32.3z'/>"
    "<path d='M77.6 97c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9-12.9-5.8-12.9-12.9V97h12.9z'/>"
    "<path d='M77.6 90.5c-7.1 0-12.9-5.8-12.9-12.9s5.8-12.9 12.9-12.9h32.3c7.1 0 12.9 5.8 12.9 12.9s-5.8 12.9-12.9 12.9H77.6z'/>"
    "</svg>"
)
_SECTION_ICON_SKILLS = "<span class='material-symbols-outlined' aria-hidden='true'>extension</span>"
_SECTION_ICON_README = "<span class='material-symbols-outlined' aria-hidden='true'>description</span>"
_SECTION_ICON_PREFERENCES = "<span class='material-symbols-outlined' aria-hidden='true'>palette</span>"
_SECTION_ICON_INSTRUCTIONS = "<span class='material-symbols-outlined' aria-hidden='true'>edit_note</span>"
_SECTION_ICON_AI_CLI = "<span class='material-symbols-outlined' aria-hidden='true'>smart_toy</span>"

# Human-readable names for ai_cli_config.VALID_CLIS, shared by the topbar's
# always-visible AI CLI badge (_render_shell) and every page's own copy
# that used to hardcode "Claude"/"Claude CLI" - see render_ai_cli_page for
# the one place that still annotates these with install-availability.
_AI_CLI_DISPLAY_NAMES = {"claude": "Claude Code", "codex": "Codex CLI"}

_SECTION_ICON_ACTIVITY = "<span class='material-symbols-outlined' aria-hidden='true'>bolt</span>"

_SECTION_ICON_LOGS = "<span class='material-symbols-outlined' aria-hidden='true'>terminal</span>"

_SIDEBAR_TOGGLE_ICON = "<span class='material-symbols-outlined'>chevron_left</span>"

_STEP_LABELS = {
    "analyzing": "Analyzing",
    "implementing": "Implementing a fix",
    "verifying": "Running verification",
    "opening_mr": "Opening the merge request",
    # The topic monitor loop's only step (TOPIC_MONITOR_INSTRUCTIONS.md).
    "researching": "Researching",
}


def _progress_text(status):
    """Human-readable one-line summary of what the loop is doing right now
    - shared between the Activity page's "GitLab Loop" section, the
    per-topic badges on the Topic Monitor page, and the topbar badge shown
    on every page, so switching away from Activity doesn't lose sight of
    what's actually running.

    `current_issue` is optional: the GitLab loop always records one, but
    the topic monitor's per-topic status has a `current_step` and no issue
    at all (the topic's own name is already the block heading beside the
    badge), and "Starting up" for the whole of a topic's research pass
    would be plainly wrong."""
    state = status.get("state", "unknown")
    current_issue = status.get("current_issue")
    current_step = status.get("current_step")
    if state == "running" and current_step:
        step_label = _STEP_LABELS.get(current_step, current_step)
        if current_issue:
            return f"Processing {current_issue} — {step_label}"
        return step_label
    if state == "running":
        return "Starting up"
    return "Idle"


def _topic_monitor_progress_text(topic_status, topics):
    """Human-readable one-line summary of what the topic monitor loop is
    doing right now - the Activity page's topic-monitor equivalent of
    _progress_text. Its status is kept per-topic (read_topic_status)
    rather than as one shared state like the GitLab loop's, but only one
    topic ever runs at a time (trigger_topic_monitor_run refuses to start
    a second while one is already running), so this looks for at most one
    "running" entry among all configured topics and reports its label -
    falling back to the topic's own name if it's since been removed from
    topics.json (get_configured_topics returns [] in that case), rather
    than showing nothing."""
    labels = {t["name"]: t.get("label", t["name"]) for t in topics}
    for name, entry in topic_status.items():
        if entry.get("state") == "running":
            step = entry.get("current_step")
            step_label = _STEP_LABELS.get(step, step) if step else "Starting up"
            return f"{step_label} — {labels.get(name, name)}"
    return "Idle"


def _status_badge(state):
    """Map a status "state" value to a (pill-css-class, icon-html) pair for
    the header badge. Unknown/unexpected states fall back to a neutral grey
    pill rather than guessing."""
    state_str = str(state)
    if state_str == "running":
        return "pill-blue", _SPINNER_ICON
    if state_str == "idle":
        return "pill-green", _CHECK_ICON
    if state_str == "never_run":
        return "pill-grey", _DOT_ICON_TEMPLATE.format(cls="")
    if state_str == "failed":
        return "pill-red", _DOT_ICON_TEMPLATE.format(cls="")
    return "pill-grey", _DOT_ICON_TEMPLATE.format(cls="")


_NAV_ITEMS = (
    ("overview", "/", "Dashboard", _SECTION_ICON_OVERVIEW),
    ("analytics", "/analytics", "Analytics", _SECTION_ICON_ANALYTICS),
    ("history", "/history", "Run History", _SECTION_ICON_HISTORY),
    ("gitlab", "/gitlab", "Live GitLab", _SECTION_ICON_GITLAB),
    ("memory", "/memory", "Memory", _SECTION_ICON_MEMORY),
    ("topic_monitor", "/topic-monitor", "Topic Monitor", _SECTION_ICON_TOPIC_MONITOR),
    ("daemons", "/daemons", "Daemons", _SECTION_ICON_DAEMONS),
    ("skills", "/skills", "Skills", _SECTION_ICON_SKILLS),
    ("settings", "/settings", "GitLab", _SECTION_ICON_SETTINGS),
    ("notifications", "/notifications", "Notifications", _SECTION_ICON_SLACK),
    ("activity", "/activity", "Activity", _SECTION_ICON_ACTIVITY),
    ("logs", "/logs", "Logs", _SECTION_ICON_LOGS),
    ("readme", "/readme", "README", _SECTION_ICON_README),
    ("preferences", "/preferences", "Preferences", _SECTION_ICON_PREFERENCES),
    ("instructions", "/instructions", "Instructions", _SECTION_ICON_INSTRUCTIONS),
    ("topic_settings", "/topic-monitor/settings", "Topic Settings", _SECTION_ICON_SETTINGS),
    ("ai_cli", "/ai-cli", "AI CLI", _SECTION_ICON_AI_CLI),
)


_NAV_GROUPS = (
    # (label or None, keys...) - None means "ungrouped, no label" (just
    # Dashboard: the landing page, not really part of any category).
    # Monitor = watching what the loop is doing/has done; System = the
    # infrastructure underneath it (launchd daemons, external skill
    # deps); Configuration = settings/meta pages; Docs = reference
    # material, deliberately last since it's the least-visited group.
    (None, ("overview",)),
    ("Monitor", ("analytics", "gitlab", "topic_monitor", "memory", "activity", "logs", "history")),
    ("System", ("daemons", "skills")),
    ("Configuration", ("settings", "notifications", "topic_settings", "preferences", "instructions", "ai_cli")),
    ("Docs", ("readme",)),
)
_NAV_GROUP_OF = {key: label for label, keys in _NAV_GROUPS if label for key in keys}


def _nav_link(key, href, label, icon, active_page):
    """One <a> in the sidebar nav. `active_page` is the key of whichever
    page is currently rendering; a matching key gets the `active` class.
    `title` carries the label even when the sidebar is collapsed and
    `.nav-label` is hidden, so the link stays identifiable via a native
    tooltip."""
    cls = " class='active'" if key == active_page else ""
    return (
        f"<a href='{href}' title='{label}'{cls}>"
        f"<span class='nav-icon'>{icon}</span><span class='nav-label'>{label}</span></a>"
    )


def _state_label(status):
    """Human-readable label for status["state"] - "Processing ... -
    Verifying" while running (via _progress_text), else the state name
    title-cased. Shared by _status_badge_markup (the small topbar pill)
    and render_overview_page's own larger status-hero pill, so the two
    never drift out of sync."""
    state = status.get("state", "unknown")
    if state == "running":
        return _progress_text(status)
    return state.replace("_", " ").title() if isinstance(state, str) else str(state)


def _status_badge_markup(status):
    """The small state pill shown in the header on every top-level page.
    Takes an already-read status dict (not a path), so callers control when
    the file read happens - same dependency style as this module's other
    read_status()/get_daemons_status() functions."""
    state = status.get("state", "unknown")
    badge_class, badge_icon = _status_badge(state)
    return f"<span class='pill {badge_class}'>{badge_icon}{html.escape(_state_label(status))}</span>"


def _run_now_action_html(action, confirm_text, csrf_input, disabled_hint_html=None):
    """One card's "Run now" action area - shared by render_overview_page's
    GitLab loop and Topic Monitor sections, and by render_topic_monitor_page's
    own button. `disabled_hint_html` given means that loop has nothing
    configured to run: rather than silently hiding the button (which reads
    as "broken", not "nothing to do"), show it disabled with a visible
    explanation of what to set up first - the same "explain what to fix"
    treatment as every other empty state in this app, just phrased for a
    button instead of a list. Omit `disabled_hint_html` (the default) for
    the normal, submittable case; callers hide this area entirely (pass
    `run_now_html = ""`) for the third state - already running - since
    there's nothing to configure or click there at all."""
    if disabled_hint_html is not None:
        return f"""
<div class='run-now-action'>
<button type='button' class='btn btn-primary' disabled>
<span class='material-symbols-outlined' aria-hidden='true'>bolt</span> Run now
</button>
<p class='run-now-hint'>{disabled_hint_html}</p>
</div>
"""
    confirm_attr = html.escape(confirm_text, quote=True)
    return f"""
<div class='run-now-action'>
<form method='post' action='{action}' class='daemon-action-form'>
{csrf_input}
<button type='submit' class='btn btn-primary' data-confirm="{confirm_attr}">
<span class='material-symbols-outlined' aria-hidden='true'>bolt</span> Run now
</button>
</form>
</div>
"""


def _empty_state_html(message, action_href, action_label):
    """A page section has nothing to show because setup is missing (no
    project aliases configured yet) - render an icon + message + a
    shortcut button straight to the settings page that fixes it, instead
    of a bare "(no projects configured)" line. Used by
    render_gitlab_live_fragment and render_memory_page."""
    return f"""
<div class='empty-state'>
<div class='empty-state-icon'><span class='material-symbols-outlined' aria-hidden='true'>folder_off</span></div>
<p class='empty-state-message'>{message}</p>
<a class='btn btn-primary empty-state-action' href='{action_href}'>
<span class='material-symbols-outlined' aria-hidden='true'>settings</span> {action_label}
</a>
</div>
"""


def _sidebar_html(active_page):
    """The dashboard's persistent left nav: brand mark, a collapse toggle
    (plain inline onclick — this is a fully server-rendered, no-JS-framework
    page, so there's no other client-side state to hook the toggle into),
    and the page links built from _NAV_ITEMS, clustered into the labeled
    groups _NAV_GROUPS defines (a small uppercase label per group, hidden
    when collapsed like every other nav label - see .sidebar-group-label).
    `active_page` is forwarded straight to _nav_link for each item."""
    items_by_key = {item[0]: item for item in _NAV_ITEMS}
    group_blocks = []
    for label, keys in _NAV_GROUPS:
        label_html = f"<p class='sidebar-group-label'>{html.escape(label)}</p>" if label else ""
        links_html = "".join(_nav_link(*items_by_key[key], active_page) for key in keys)
        group_blocks.append(f"{label_html}{links_html}")
    nav_html = "".join(group_blocks)
    return (
        "<div class='sidebar-top'>"
        f"<a class='brand' href='/'>{_BRAND_MARK_ICON}<span class='brand-name'>Loop X</span></a>"
        "<button type='button' class='sidebar-toggle' aria-label='Toggle sidebar' "
        "onclick=\"document.documentElement.classList.toggle('collapsed');"
        "localStorage.setItem('loop-dashboard-sidebar', "
        "document.documentElement.classList.contains('collapsed') ? '1' : '0')\">"
        f"{_SIDEBAR_TOGGLE_ICON}</button>"
        "</div>"
        f"<nav class='sidebar-nav' aria-label='Pages'>{nav_html}</nav>"
    )


def _favicon_version():
    """Short content hash of FAVICON_PATH, used as a `?v=` cache-buster on
    the favicon link. Browsers cache favicons far more aggressively than
    normal page assets - a bare /favicon.ico URL that once 404'd or changed
    content can stay stuck in that state across ordinary reloads, so the
    URL itself needs to change whenever the file does. Missing file -> "0"
    rather than raising, since a stale/absent icon shouldn't break the page."""
    try:
        data = FAVICON_PATH.read_bytes()
    except OSError:
        return "0"
    return hashlib.sha256(data).hexdigest()[:8]


def _render_shell(title, active_page, status_badge_html, body_html, refresh=False, refresh_note=False):
    """The <!doctype>...</html> skeleton shared by every page this server
    renders: head (style, viewport, a pre-paint script that restores the
    sidebar's collapsed state from localStorage before the page ever
    paints, optional auto-refresh), the fixed left sidebar (built by
    _sidebar_html), a slim topbar (AI CLI badge + status badge + refresh
    note) above the page body, and the .wrap container. Auto-refresh
    defaults to off:
    only the pages whose data actually changes out from under a reader
    while they watch it - render_gitlab_page, render_topic_monitor_page,
    render_activity_page, render_logs_page - pass `refresh=True,
    refresh_note=True` explicitly. Every other page (overview, history,
    preferences, instructions, readme, memory, topic settings, skills,
    daemons, settings, slack) is mostly static or user-edited, so a silent
    30s reload there would just interrupt reading/typing for no benefit.
    The /history/<name> and
    /topic-monitor/history/<name> sub-pages also rely on this default - they
    show a fixed past run and shouldn't reload out from under someone
    reading it. The topbar's animated progress sliver is active whenever
    `status_badge_html` carries the "md-spinner" class (i.e. whenever the
    caller's own state is "running" - see _status_badge's _SPINNER_ICON)
    - reusing that marker instead of a separate parameter keeps every
    render_*_page() call site unchanged."""
    # Passes ai_cli_config.DEFAULT_CONFIG_PATH explicitly rather than
    # relying on get_selected_cli's own default, for the same reason
    # render_ai_cli_page does - a test's monkeypatch.setattr(ai_cli_config,
    # "DEFAULT_CONFIG_PATH", ...) needs to actually change what this reads.
    ai_cli_name = _AI_CLI_DISPLAY_NAMES[ai_cli_config.get_selected_cli(ai_cli_config.DEFAULT_CONFIG_PATH)]
    ai_cli_badge_html = (
        f"<a class='pill pill-grey' href='/ai-cli'>{_SECTION_ICON_AI_CLI}{html.escape(ai_cli_name)}</a>"
    )
    refresh_html = "<span class='refresh-note' id='refresh-note-text'>auto-refreshes every 30s</span>" if refresh_note else ""
    refresh_schedule_script = ""
    if refresh:
        # Auto-refresh interval (see render_preferences_page) is a
        # per-browser preference now, not a fixed <meta http-equiv=
        # "refresh">, which could never have been made configurable -
        # the server has no way to know this browser's saved choice at
        # render time, so the reload itself has to be scheduled by JS
        # reading localStorage instead.
        # window.__loopChatStreaming (set/cleared by the Activity page's
        # own chat script, see the "activity-composer-form" IIFE below) is
        # checked right before reloading, not just when scheduling the
        # timer - a reply can still be mid-stream whenever this timer
        # fires. Rather than skip the reload outright (which would just
        # mean it never happens again on a long chat session), it
        # reschedules itself for another refreshSeconds and re-checks -
        # coordinating with, not replacing, the existing timer mechanism.
        # Without this, a routine 30s auto-refresh tears down an in-flight
        # EventSource stream, the pending bubble, and its accumulated
        # text mid-reply.
        refresh_schedule_script = """
  var refreshSeconds = parseInt(localStorage.getItem('loop-dashboard-refresh-interval'), 10) || 30;
  (function __loopScheduleRefresh() {
    setTimeout(function() {
      if (window.__loopChatStreaming) { __loopScheduleRefresh(); return; }
      location.reload();
    }, refreshSeconds * 1000);
  })();"""
    refresh_note_script = ""
    if refresh_note:
        refresh_note_script = """
(function() {
  document.addEventListener('DOMContentLoaded', function() {
    var refreshSeconds = parseInt(localStorage.getItem('loop-dashboard-refresh-interval'), 10) || 30;
    var labels = {5: '5s', 11: '11s', 30: '30s', 60: '1 min', 300: '5 min'};
    var el = document.getElementById('refresh-note-text');
    if (el) el.textContent = 'auto-refreshes every ' + (labels[refreshSeconds] || (refreshSeconds + 's'));
  });
})();"""
    return f"""<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<link rel="icon" href="/favicon.ico?v={_favicon_version()}" type="image/x-icon">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?{_GOOGLE_FONTS_FAMILIES_PARAM}&display=swap" rel="stylesheet">
<link href="https://fonts.googleapis.com/css2?family=Material+Symbols+Outlined:opsz,wght,FILL,GRAD@24,400,0,0&icon_names={_MATERIAL_SYMBOLS_ICON_NAMES}&display=block" rel="stylesheet">
<style>{_STYLE}</style>
<script>
(function() {{
  if (localStorage.getItem('loop-dashboard-sidebar') === '1') {{
    document.documentElement.classList.add('collapsed');
  }}
  // Preferences page (see render_preferences_page): color mode stays
  // absent for "Auto" - only an explicit light/dark choice ever gets
  // written here, so @media (prefers-color-scheme) in _STYLE keeps
  // driving the "Auto" case untouched. Accent and font both always get
  // set (defaulting to 'default'/'roboto' - today's original look - when
  // nothing's been chosen yet), since every choice for either, including
  // the defaults, is its own named CSS attribute selector with no
  // "absence" branch.
  var colorMode = localStorage.getItem('loop-dashboard-color-mode');
  if (colorMode === 'light' || colorMode === 'dark') {{
    document.documentElement.setAttribute('data-color-mode', colorMode);
  }}
  var accent = localStorage.getItem('loop-dashboard-accent');
  document.documentElement.setAttribute('data-accent', accent || 'default');
  var font = localStorage.getItem('loop-dashboard-font');
  document.documentElement.setAttribute('data-font', font || 'roboto');{refresh_schedule_script}
}})();{refresh_note_script}
(function() {{
  function closeAll(except) {{
    document.querySelectorAll('.custom-select.is-open').forEach(function(root) {{
      if (root === except) return;
      root.classList.remove('is-open');
      var menu = root.querySelector('.custom-select-menu');
      if (menu) menu.hidden = true;
      var trigger = root.querySelector('.custom-select-trigger');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    }});
  }}
  function selectOption(root, option) {{
    var native = root.querySelector('.custom-select-native');
    var valueEl = root.querySelector('.custom-select-value');
    root.querySelectorAll('.custom-select-option').forEach(function(o) {{ o.classList.remove('is-selected'); }});
    option.classList.add('is-selected');
    if (native) {{
      native.value = option.getAttribute('data-value');
      // Setting .value in JS never fires a native 'change' event, so any
      // onchange="..." attribute on the <select> itself (e.g. the schedule
      // editor's frequency dropdown) would otherwise never run when picked
      // through this custom UI - only via direct keyboard/native use.
      native.dispatchEvent(new Event('change', {{ bubbles: true }}));
    }}
    if (valueEl) valueEl.textContent = option.textContent;
  }}
  document.addEventListener('click', function(ev) {{
    var trigger = ev.target.closest('.custom-select-trigger');
    if (trigger) {{
      var root = trigger.closest('.custom-select');
      var wasOpen = root.classList.contains('is-open');
      closeAll(root);
      root.classList.toggle('is-open', !wasOpen);
      var menu = root.querySelector('.custom-select-menu');
      if (menu) {{
        if (!wasOpen) {{
          var rect = trigger.getBoundingClientRect();
          menu.style.top = (rect.bottom + 4) + 'px';
          menu.style.left = rect.left + 'px';
          menu.style.width = rect.width + 'px';
          // Move focus into the listbox itself (selected option, or the
          // first one) - without this, a keyboard user who opens the menu
          // via Enter/Space has no way to reach the options at all, since
          // the real <select> behind them is display:none and the options
          // themselves aren't in the tab order until the menu is open.
          var toFocus = menu.querySelector('.is-selected') || menu.querySelector('.custom-select-option');
          if (toFocus) toFocus.focus();
        }}
        menu.hidden = wasOpen;
      }}
      trigger.setAttribute('aria-expanded', String(!wasOpen));
      return;
    }}
    var option = ev.target.closest('.custom-select-option');
    if (option) {{
      selectOption(option.closest('.custom-select'), option);
      closeAll(null);
      return;
    }}
    if (!ev.target.closest('.custom-select')) closeAll(null);
  }});
  // Tabbing away from an open menu (rather than clicking outside, or
  // Escape) reached no code path at all before this - the menu would stay
  // open, detached from whatever now has focus.
  document.addEventListener('focusout', function(ev) {{
    var root = ev.target.closest && ev.target.closest('.custom-select');
    if (!root) return;
    setTimeout(function() {{
      if (!root.contains(document.activeElement)) closeAll(null);
    }}, 0);
  }});
  document.addEventListener('keydown', function(ev) {{
    var option = ev.target.closest('.custom-select-option');
    if (option) {{
      var root = option.closest('.custom-select');
      var trigger = root.querySelector('.custom-select-trigger');
      var opts = Array.prototype.slice.call(root.querySelectorAll('.custom-select-option'));
      var idx = opts.indexOf(option);
      if (ev.key === 'ArrowDown') {{
        ev.preventDefault();
        (opts[idx + 1] || opts[0]).focus();
      }} else if (ev.key === 'ArrowUp') {{
        ev.preventDefault();
        (opts[idx - 1] || opts[opts.length - 1]).focus();
      }} else if (ev.key === 'Enter' || ev.key === ' ') {{
        ev.preventDefault();
        selectOption(root, option);
        closeAll(null);
        if (trigger) trigger.focus();
      }} else if (ev.key === 'Escape') {{
        ev.preventDefault();
        closeAll(null);
        if (trigger) trigger.focus();
      }} else if (ev.key === 'Tab') {{
        closeAll(null);
      }}
      return;
    }}
    var trigger = ev.target.closest('.custom-select-trigger');
    if (trigger && (ev.key === 'ArrowDown' || ev.key === 'ArrowUp')) {{
      var root = trigger.closest('.custom-select');
      if (!root.classList.contains('is-open')) {{
        ev.preventDefault();
        trigger.click();
      }}
      return;
    }}
    if (ev.key === 'Escape') closeAll(null);
  }});
  // The schedule editor's Daily/Weekly/Monthly frequency dropdown toggles
  // which of its own sibling controls (the weekday checkboxes vs. the
  // day-of-month dropdown) are visible - delegated the same way every
  // other interactive bit on this page is, rather than an inline
  // onchange="..." attribute, since picking a custom-dropdown option only
  // fires a real 'change' event on the underlying native <select> (see
  // selectOption above), which this listener catches either way.
  document.addEventListener('change', function(ev) {{
    var select = ev.target.closest("select[name='frequency']");
    if (!select) return;
    var form = select.closest('form');
    if (!form) return;
    var freq = select.value.toLowerCase();
    var weekly = form.querySelector('.weekly-controls');
    var monthly = form.querySelector('.monthly-controls');
    if (weekly) weekly.style.display = (freq === 'weekly') ? '' : 'none';
    if (monthly) monthly.style.display = (freq === 'monthly') ? '' : 'none';
  }});
}})();
(function() {{
  // Deferred to DOMContentLoaded because this whole <script> block is
  // emitted in <head> (see _render_shell), before #activity-composer-form
  // and #activity-message-list exist further down the rendered page -
  // looking them up eagerly here would always find null and the guard
  // below would silently no-op on every page load, matching the pattern
  // already used elsewhere in this file (e.g. the topbar-page-title and
  // data-lazy-load blocks).
  document.addEventListener('DOMContentLoaded', function() {{
    var form = document.getElementById('activity-composer-form');
    var list = document.getElementById('activity-message-list');
    if (!form || !list) return;

    function scrollToBottom() {{
      list.scrollTop = list.scrollHeight;
    }}
    // Open on the most recent messages, not the top of a long thread.
    scrollToBottom();

    function appendBubble(className, whoHtml, text) {{
      var ul = list.querySelector('.message-list');
      if (!ul) {{
        ul = document.createElement('ul');
        ul.className = 'message-list';
        list.innerHTML = '';
        list.appendChild(ul);
      }}
      var li = document.createElement('li');
      li.className = 'message-row ' + (className === 'message-bubble-user' ? 'message-row-user' : 'message-row-loop');
      li.innerHTML =
        "<div class='message-bubble " + className + "'>" +
        "<div class='message-meta'>" + whoHtml + "<span class='message-time'>just now</span></div>" +
        "<div class='message-text'></div>" +
        "</div>";
      li.querySelector('.message-text').textContent = text;
      ul.appendChild(li);
      scrollToBottom();
      return li.querySelector('.message-text');
    }}

    var composerInput = form.querySelector("[name='text']");
    if (composerInput) {{
      // Textarea default is a literal newline on Enter; match the old
      // single-line input's submit-on-Enter behavior and reserve
      // Shift+Enter for an actual line break.
      composerInput.addEventListener('keydown', function(ev) {{
        if (ev.key === 'Enter' && !ev.shiftKey) {{
          ev.preventDefault();
          form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event('submit', {{ cancelable: true }}));
        }}
      }});
    }}

    form.addEventListener('submit', function(ev) {{
      ev.preventDefault();
      var input = form.querySelector("[name='text']");
      var button = form.querySelector("button[type='submit']");
      var text = input.value.trim();
      if (!text) return;
      var csrfToken = form.querySelector("input[name='csrf_token']").value;

      appendBubble('message-bubble-user', "<span class='k'>You</span>", text);
      input.value = '';
      button.disabled = true;

      var pendingTextEl = appendBubble(
        'message-bubble-loop',
        "<span class='k' aria-label='Loop X'>" +
          "<svg class='message-brand-icon' viewBox='0 0 24 24' width='20' height='20' fill='none' " +
          "stroke='currentColor' stroke-width='2' aria-hidden='true'>" +
          "<circle cx='8' cy='12' r='4.5'/><circle cx='16' cy='12' r='4.5'/></svg></span>",
        ''
      );
      var spinner = document.createElement('span');
      spinner.className = 'md-spinner md-spinner-sm';
      pendingTextEl.parentElement.insertBefore(spinner, pendingTextEl);

      var body = new URLSearchParams();
      body.set('text', text);
      body.set('csrf_token', csrfToken);

      // Checked by the auto-refresh timer (see refresh_schedule_script in
      // _render_shell) so a routine 30s page reload never tears down this
      // stream mid-reply - cleared on every terminal path below (the
      // request itself failing, and the stream's own done/error events).
      window.__loopChatStreaming = true;
      function stopStreamingFlag() {{ window.__loopChatStreaming = false; }}

      fetch('/activity/chat', {{ method: 'POST', body: body }})
        .then(function(response) {{ return response.json().then(function(data) {{ return {{ ok: response.ok, data: data }}; }}); }})
        .then(function(result) {{
          if (!result.ok) {{
            pendingTextEl.textContent = result.data.error || 'Something went wrong.';
            spinner.remove();
            button.disabled = false;
            stopStreamingFlag();
            return;
          }}
          var source = new EventSource('/activity/chat-stream?reply_key=' + encodeURIComponent(result.data.reply_key));
          var accumulated = '';
          source.addEventListener('chunk', function(ev) {{
            accumulated += JSON.parse(ev.data);
            pendingTextEl.textContent = accumulated;
            spinner.remove();
            scrollToBottom();
          }});
          source.addEventListener('done', function(ev) {{
            // The authoritative, already-persisted reply text (see
            // _chat_job_finish/append_message on the server) replaces
            // whatever the streamed chunks accumulated to - but only when
            // the server actually sent one back non-empty. An empty/falsy
            // payload here must leave the bubble showing whatever text the
            // streamed chunks already accumulated, rather than blanking it.
            var parsedText = null;
            try {{ parsedText = JSON.parse(ev.data); }} catch (e) {{}}
            if (parsedText) {{
              pendingTextEl.textContent = parsedText;
            }}
            spinner.remove();
            button.disabled = false;
            stopStreamingFlag();
            scrollToBottom();
            source.close();
          }});
          source.addEventListener('error', function(ev) {{
            var message = 'Something went wrong - try again.';
            try {{ message = JSON.parse(ev.data) || message; }} catch (e) {{}}
            if (accumulated === '') {{
              pendingTextEl.textContent = message;
            }} else {{
              // Partial text already streamed into the bubble - a failed
              // or timed-out reply must never look like a normal, complete
              // answer that will simply vanish on the next reload (nothing
              // partial was ever saved via append_message), so this marks
              // it visibly rather than leaving the bubble unchanged.
              pendingTextEl.textContent = accumulated + ' (reply interrupted)';
            }}
            spinner.remove();
            button.disabled = false;
            stopStreamingFlag();
            source.close();
          }});
        }})
        .catch(function() {{
          pendingTextEl.textContent = 'Something went wrong - try again.';
          spinner.remove();
          button.disabled = false;
          stopStreamingFlag();
        }});
    }});
  }});
}})();
(function() {{
  function showFieldError(field) {{
    var bubble = document.getElementById('field-error-bubble');
    if (!bubble) return;
    var text = bubble.querySelector('.field-error-bubble-text');
    if (text) text.textContent = field.validationMessage || 'Please fill out this field.';
    var rect = field.getBoundingClientRect();
    bubble.style.left = rect.left + 'px';
    bubble.style.top = (rect.bottom + 6) + 'px';
    bubble.hidden = false;
  }}
  function hideFieldError() {{
    var bubble = document.getElementById('field-error-bubble');
    if (bubble) bubble.hidden = true;
  }}
  // Native form validation fires 'invalid' on every invalid control in one
  // submit attempt (that's what blocks submission) but only shows its
  // bubble UI for the first one. preventDefault() suppresses that native,
  // unstyled bubble; invalidBatchActive replicates "first one only" so we
  // don't flash through every invalid field's message in the same tick.
  var invalidBatchActive = false;
  document.addEventListener('invalid', function(ev) {{
    ev.preventDefault();
    if (invalidBatchActive) return;
    invalidBatchActive = true;
    showFieldError(ev.target);
    ev.target.focus();
    setTimeout(function() {{ invalidBatchActive = false; }}, 0);
  }}, true);
  document.addEventListener('input', hideFieldError);
  document.addEventListener('click', function(ev) {{
    if (!ev.target.closest('.field-error-bubble')) hideFieldError();
  }});
  document.addEventListener('keydown', function(ev) {{
    if (ev.key === 'Escape') hideFieldError();
  }});
}})();
(function() {{
  var pendingForm = null;
  document.addEventListener('click', function(ev) {{
    var trigger = ev.target.closest('[data-confirm]');
    if (trigger) {{
      ev.preventDefault();
      var dialog = document.getElementById('confirm-dialog');
      if (!dialog) return;
      var messageEl = dialog.querySelector('.confirm-dialog-message');
      if (messageEl) messageEl.textContent = trigger.getAttribute('data-confirm');
      pendingForm = trigger.closest('form');
      dialog.showModal();
      return;
    }}
    if (ev.target.closest('[data-confirm-cancel]')) {{
      var dialog = document.getElementById('confirm-dialog');
      if (dialog) dialog.close();
      pendingForm = null;
      return;
    }}
    if (ev.target.closest('[data-confirm-ok]')) {{
      var dialog = document.getElementById('confirm-dialog');
      if (dialog) dialog.close();
      if (pendingForm) pendingForm.submit();
      pendingForm = null;
      return;
    }}
  }});
}})();
(function() {{
  // Any element with data-lazy-load="/some/url" starts empty (or holding a
  // loading placeholder, e.g. render_gitlab_page's spinner) and gets its
  // content fetched and swapped in after the page has already painted -
  // for a page whose real data (get_live_gitlab_state, a subprocess + real
  // GitLab API call per configured project) is too slow to block the page
  // switch on. One generic handler here rather than a per-page <script>,
  // so any future slow page can opt in with just the attribute.
  function loadLazyContent(el) {{
    fetch(el.getAttribute('data-lazy-load'))
      .then(function(response) {{ return response.text(); }})
      .then(function(html) {{ el.innerHTML = html; }})
      .catch(function() {{
        el.innerHTML = "<p class='inline-error'><span class='material-symbols-outlined' aria-hidden='true'>error</span> Couldn't load this section.</p>";
      }});
  }}
  document.addEventListener('DOMContentLoaded', function() {{
    document.querySelectorAll('[data-lazy-load]').forEach(loadLazyContent);
  }});
}})();
(function() {{
  // Reveals #topbar-page-title (see .topbar-page-title in _STYLE) once
  // the page's own <h1> has scrolled up behind the sticky topbar -
  // otherwise scrolling down leaves the topbar with no indication of
  // which page this is at all. rootMargin is set to the topbar's own
  // rendered height (read at runtime, not hardcoded, so it can't drift
  // out of sync with the CSS) so the h1 counts as "gone" exactly when it
  // disappears behind the topbar, not only once it's fully above y=0.
  document.addEventListener('DOMContentLoaded', function() {{
    var titleEl = document.getElementById('topbar-page-title');
    var topbar = document.querySelector('.topbar');
    var h1 = document.querySelector('.content-area h1');
    if (!titleEl || !topbar || !h1 || !window.IntersectionObserver) return;
    titleEl.textContent = h1.textContent.trim();
    var observer = new IntersectionObserver(function(entries) {{
      entries.forEach(function(entry) {{
        titleEl.classList.toggle('is-visible', !entry.isIntersecting);
      }});
    }}, {{ rootMargin: '-' + topbar.offsetHeight + 'px 0px 0px 0px' }});
    observer.observe(h1);
  }});
}})();
(function() {{
  // Generic tab switcher: any [data-tabs] container with [data-tab-target]
  // buttons and matching [data-tab-panel] sections - one click listener
  // here covers every tab group on any page, the same "opt in with just
  // the attribute" pattern as the data-lazy-load handler above.
  document.addEventListener('click', function(ev) {{
    var button = ev.target.closest('[data-tab-target]');
    if (!button) return;
    var group = button.closest('[data-tabs]');
    if (!group) return;
    var target = button.getAttribute('data-tab-target');
    group.querySelectorAll('[data-tab-target]').forEach(function(b) {{
      var isActive = b === button;
      b.classList.toggle('is-active', isActive);
      b.setAttribute('aria-selected', String(isActive));
    }});
    group.querySelectorAll('[data-tab-panel]').forEach(function(panel) {{
      panel.hidden = panel.getAttribute('data-tab-panel') !== target;
    }});
  }});
}})();
(function() {{
  // Every .daemon-action-form is a plain `method='post'` form (no fetch/JS
  // submit interception), so saving one is a full POST-redirect-GET - a
  // fresh page load that the browser scrolls to the top of by default.
  // That's jarring for a form living far down a long page (e.g. the loop
  // settings form at the bottom of /settings) when the redirect lands
  // back on the same page. Stash the scroll offset in sessionStorage,
  // keyed by the page being submitted from, and restore it if the next
  // page load is that same page.
  var SCROLL_KEY_PREFIX = 'daemon-action-scroll:';
  document.addEventListener('submit', function(ev) {{
    if (!ev.target.matches || !ev.target.matches('.daemon-action-form')) return;
    try {{
      sessionStorage.setItem(SCROLL_KEY_PREFIX + location.pathname, String(window.scrollY));
    }} catch (e) {{}}
  }});
  document.addEventListener('DOMContentLoaded', function() {{
    var key = SCROLL_KEY_PREFIX + location.pathname;
    var saved;
    try {{ saved = sessionStorage.getItem(key); }} catch (e) {{ saved = null; }}
    if (saved === null) return;
    try {{ sessionStorage.removeItem(key); }} catch (e) {{}}
    window.scrollTo(0, parseInt(saved, 10) || 0);
  }});
}})();
</script>
</head>
<body>

<div class="field-error-bubble" id="field-error-bubble" hidden>
<span class="material-symbols-outlined" aria-hidden="true">error</span>
<span class="field-error-bubble-text"></span>
</div>

<dialog class="confirm-dialog" id="confirm-dialog">
<div class="confirm-dialog-icon"><span class="material-symbols-outlined" aria-hidden="true">error</span></div>
<p class="confirm-dialog-message"></p>
<div class="confirm-dialog-actions">
<button type="button" class="btn btn-neutral" data-confirm-cancel>Cancel</button>
<button type="button" class="btn btn-warning" data-confirm-ok>Confirm</button>
</div>
</dialog>

<aside class="sidebar">
{_sidebar_html(active_page)}
</aside>

<main class="content-area">
<div class="topbar">
<div class="topbar-progress-bar{" is-active" if "md-spinner" in status_badge_html else ""}"></div>
<span class="topbar-page-title" id="topbar-page-title"></span>
<div class="header-right">
{ai_cli_badge_html}
{status_badge_html}
{refresh_html}
</div>
</div>
<div class="wrap">
{body_html}
</div>
</main>

</body>
</html>
"""


_ACTIVITY_STRIP_OUTCOME_LABEL = {
    "escalation": "Escalation filed",
    "mr": "MR opened",
    "quiet": "Ran clean",
    None: "No run logged",
}


def _activity_strip_html(strip):
    """The Dashboard page's 7-day activity strip: one small bar per day,
    coloured by that day's outcome (see _gitlab_loop_stats) - escalation
    (amber, needs attention) beats mr (blue, something shipped) beats
    quiet (green, ran clean); an outline-only bar means no run was logged
    that day at all. `strip` is oldest-first, matching how it reads
    left-to-right."""
    bars = []
    for day in strip:
        outcome = day["outcome"]
        css_class = f"activity-bar-{outcome}" if outcome else "activity-bar-none"
        title = f"{day['date']} – {_ACTIVITY_STRIP_OUTCOME_LABEL[outcome]}"
        bars.append(f"<span class='activity-bar {css_class}' title=\"{html.escape(title)}\"></span>")
    return f"<div class='activity-strip'>{''.join(bars)}</div>"


def _dashboard_stats_html(stats, projects_count, topics_count):
    """The Dashboard page's stats section: tracked-projects/configured-topics
    setup counts plus the GitLab loop's all-time run totals (from
    _gitlab_loop_stats) and its 7-day activity strip - everything a glance
    at the Dashboard should answer without a click to Live GitLab,
    Memory, or Run History."""
    tiles = (
        ("folder", "Tracked projects", projects_count),
        ("topic", "Configured topics", topics_count),
        ("history", "Runs logged", stats["runs"]),
        ("merge", "MRs opened", stats["mrs_opened"]),
        ("warning", "Escalations", stats["escalations"]),
        ("forum", "Answered directly", stats["answered"]),
    )
    tiles_html = "".join(
        "<div class='dash-stat-tile'>"
        f"<span class='material-symbols-outlined dash-stat-icon' aria-hidden='true'>{icon}</span>"
        f"<span class='dash-stat-value'>{value}</span>"
        f"<span class='dash-stat-label'>{html.escape(label)}</span>"
        "</div>"
        for icon, label, value in tiles
    )
    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_OVERVIEW}<h2>Overview</h2></div>
<div class="dash-stats-grid">{tiles_html}</div>
<div class="dash-activity-strip-row">
<span class="dash-activity-strip-label">Last 7 days</span>
{_activity_strip_html(stats["strip"])}
</div>
</section>
"""


def render_overview_page(flash=None, flash_ok=True):
    """The dashboard's home page: a stats-at-a-glance section (see
    _dashboard_stats_html) above a two-way async message thread with the
    GitLab loop. You can send a message anytime; the loop reads unseen ones
    at the start of its next issue (see pop_unseen_user_messages, called by
    the `read-messages` CLI subcommand) and may reply here. This is NOT
    real-time chat: the loop is still a scheduled, one-shot process
    (run-loop.sh), not a persistent one - see LOOPX_INSTRUCTIONS.md for
    exactly when it checks. A separate live chat assistant (/activity/chat,
    /activity/chat-stream) also replies inline in the same thread, right
    away, independent of the loop itself.

    `flash`/`flash_ok` carry a POST-redirect-GET result from sending or
    deleting a message (/activity/messages, /activity/messages/<ts>/delete)."""
    status = read_status(STATUS_PATH)
    messages = read_messages(MESSAGES_PATH)

    stats = _gitlab_loop_stats()
    projects_count = len(read_loop_projects_config().get("projects", []))
    topics_count = len(get_configured_topics())
    stats_html = _dashboard_stats_html(stats, projects_count, topics_count)

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    message_rows = []
    today = datetime.now(timezone.utc).date()
    last_day = None
    last_sender = None
    for m in messages:
        is_user = m.get("from") == "user"
        text = str(m.get("text", ""))
        timestamp = str(m.get("timestamp", ""))
        relative_time = _relative_time(timestamp)
        timestamp_url_safe = urllib.parse.quote(timestamp, safe="")
        delete_confirm = html.escape("Delete this message?", quote=True)

        day = _message_date(timestamp)
        if day is not None and day != last_day:
            message_rows.append(
                f"<li class='message-day-sep'><span>{html.escape(_day_separator_label(day, today))}</span></li>"
            )
            last_day = day
            last_sender = None

        row_classes = "message-row " + ("message-row-user" if is_user else "message-row-loop")
        if last_sender is not None and last_sender == is_user:
            row_classes += " message-row-consecutive"
        last_sender = is_user

        who_html = (
            "<span class='k'>You</span>" if is_user
            else f"<span class='k' aria-label='Loop X'>{_MESSAGE_BRAND_ICON}</span>"
        )
        message_rows.append(
            f"<li class='{row_classes}'>"
            f"<div class='message-bubble {'message-bubble-user' if is_user else 'message-bubble-loop'}'>"
            f"<div class='message-meta'>{who_html}<span class='message-time'>{html.escape(relative_time)}</span></div>"
            f"<div class='message-text markdown'>{render_markdown(text)}</div>"
            "</div>"
            f"<form method='post' action='/activity/messages/{timestamp_url_safe}/delete' class='message-delete-form'>"
            f"{csrf_input}"
            f"<button type='submit' aria-label='Delete message' data-confirm=\"{delete_confirm}\">"
            "<span class='material-symbols-outlined' aria-hidden='true'>delete</span></button>"
            "</form>"
            "</li>"
        )
    messages_html = (
        "<div id='activity-message-list'>"
        + (
            f"<ul class='message-list'>{''.join(message_rows)}</ul>"
            if message_rows
            else "<p>(no messages yet)</p>"
        )
        + "</div>"
    )

    message_form = f"""
<div class="activity-composer">
<div class="activity-composer-inner">
<form method='post' action='/activity/messages' class='daemon-action-form activity-composer-form' id='activity-composer-form'>
{csrf_input}
<textarea name='text' class='activity-composer-input' rows='3' placeholder='Message the loop - a live assistant replies right away' required></textarea>
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>send</span> Send</button>
</form>
</div>
</div>
"""

    body = f"""
<div class="page-title">
<h1>Dashboard</h1>
<p class="subtitle">A quick-glance summary, plus a place to message the loop - a live assistant replies right away.</p>
</div>

{flash_html}

<div class="grid">
{stats_html}
</div>

<div class="grid activity-messages-grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_ACTIVITY}<h2>Conversation</h2></div>
{messages_html}
</section>
</div>

{message_form}
"""
    return _render_shell("Dashboard · Loop X Engineering", "overview", _status_badge_markup(status), body)


def _history_entry_html(name, detail_href, delete_href, overview, tags, csrf_input):
    """One run-history entry's row: a link to its full detail page, a
    truncated one-line overview, its highlight tags, and a delete form -
    shared by both the GitLab loop and topic monitor sections of
    render_history_page, since both link/overview/tags/delete shapes are
    identical, only the routes differ."""
    safe_name = html.escape(name)
    tags_html = "".join(f"<span class='pill pill-grey'>{html.escape(t)}</span>" for t in tags)
    delete_confirm = html.escape(f"Delete {name}? This can't be undone.", quote=True)
    return f"""
<div class='history-entry'>
<div class='history-entry-header'>
<a href='{detail_href}'>{safe_name}</a>
<span class='pill-row'>{tags_html}</span>
<form method='post' action='{delete_href}' class='daemon-action-form'>
{csrf_input}
<button type='submit' class='btn btn-warning history-delete-btn' data-confirm="{delete_confirm}" aria-label="Delete {safe_name}">
<span class='material-symbols-outlined' aria-hidden='true'>delete</span></button>
</form>
</div>
<p class='history-entry-overview'>{html.escape(overview)}</p>
</div>
"""


def render_history_page():
    """Run History page: every archived run report from BOTH loops, most
    recent first within each - the GitLab issue loop's own reviews
    (/history/<name>) and every configured topic's saved briefings
    (/topic-monitor/history/<name>), each shown with a one-line overview
    (extract_history_overview) and highlight tags (gitlab_history_tags /
    topic_history_tags), with its own delete form. Kept as two separate
    sections rather than one merged list: the two loops' history entries
    link to different detail routes and aren't otherwise distinguishable
    by filename alone."""
    status = read_status(STATUS_PATH)
    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    gitlab_entries = []
    for name in list_run_history(HISTORY_DIR):
        content = read_history_file(name, HISTORY_DIR) or ""
        safe_name = urllib.parse.quote(name)
        gitlab_entries.append(_history_entry_html(
            name, f"/history/{safe_name}", f"/history/{safe_name}/delete",
            extract_history_overview(content), gitlab_history_tags(content), csrf_input,
        ))
    gitlab_items = "".join(gitlab_entries) or "<p>(none yet)</p>"

    topic_entries = []
    for name in list_topic_history(None, TOPIC_MONITOR_HISTORY_DIR):
        content = read_history_file(name, TOPIC_MONITOR_HISTORY_DIR) or ""
        safe_name = urllib.parse.quote(name)
        topic_entries.append(_history_entry_html(
            name, f"/topic-monitor/history/{safe_name}", f"/topic-monitor/history/{safe_name}/delete",
            extract_history_overview(content), topic_history_tags(name, content), csrf_input,
        ))
    topic_items = "".join(topic_entries) or "<p>(none yet)</p>"

    body = f"""
<div class="page-title">
<h1>Run History</h1>
<p class="subtitle">Every archived run report, most recent first.</p>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_HISTORY}<h2>GitLab Loop</h2></div>
{gitlab_items}
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_TOPIC_MONITOR}<h2>Topic Monitor</h2></div>
{topic_items}
</section>
</div>
"""
    return _render_shell("Run History · Loop X Engineering", "history", _status_badge_markup(status), body)


def render_logs_page():
    """Logs page: the tail of logs/loop-engineering.log - the one place
    every AI CLI invocation across this project (the GitLab loop,
    the topic monitor loop, and this dashboard's own live chat assistant)
    writes a human-readable entry (see append_unified_log). Shows the
    most recent lines only, same "tail, not the whole file" contract as
    the Activity page's own "Today's log" excerpt, since this file only
    grows and could otherwise get large. Entries are split apart (see
    _parse_unified_log_entries) and shown newest-first, each in its own
    bordered block, rather than as one continuous dump of the raw tail -
    a reader lands on the latest call immediately and can see where it
    starts and ends. Auto-refreshes like every other page whose data
    changes out from under a reader (Live GitLab, Topic Monitor,
    Activity)."""
    status = read_status(STATUS_PATH)
    tail = read_unified_log_tail()
    if tail:
        entries = _parse_unified_log_entries(tail)
        entries.reverse()  # newest first - the tail itself is oldest-first
        log_html = f"<div class='log-entries'>{''.join(_log_entry_html(e) for e in entries)}</div>"
    else:
        log_html = "<p>No log entries yet.</p>"

    ai_cli_name = _AI_CLI_DISPLAY_NAMES[ai_cli_config.get_selected_cli(ai_cli_config.DEFAULT_CONFIG_PATH)]
    body = f"""
<div class="page-title">
<h1>Logs</h1>
<p class="subtitle">The most recent output from every {html.escape(ai_cli_name)} invocation - the GitLab loop, the topic monitor loop, and the chat assistant.</p>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_LOGS}<h2>loop-engineering.log</h2></div>
{log_html}
</section>
</div>
"""
    return _render_shell(
        "Logs · Loop X Engineering", "logs", _status_badge_markup(status), body, refresh=True, refresh_note=True
    )


def render_gitlab_live_fragment():
    """The actual GitLab data for the Live GitLab page: open issues and MRs
    assigned to the configured user, per configured project alias. Split out
    of render_gitlab_page so that slow part (get_live_gitlab_state does a
    subprocess + real GitLab API round trip per configured project, easily
    several seconds with more than one or two projects) only ever runs when
    the browser fetches /gitlab/live, never while rendering the page shell
    itself - see render_gitlab_page's data-lazy-load placeholder."""
    live = get_live_gitlab_state()

    def error_notice(message):
        return (
            f"<p class='inline-error'><span class='material-symbols-outlined' aria-hidden='true'>error</span> "
            f"Couldn't check: {html.escape(message)}</p>"
        ) if message else ""

    def gitlab_item(item, prefix):
        assignees = item.get("assignees") or []
        assignee_names = ", ".join(a.get("name") or a.get("username", "") for a in assignees) or "Unassigned"
        updated = _relative_time(item.get("updated_at", ""))
        labels = item.get("labels") or []
        label_pills = "".join(f"<span class='pill pill-grey'>{html.escape(l)}</span>" for l in labels)
        label_row = f"<div class='pill-row'>{label_pills}</div>" if label_pills else ""
        return (
            "<li class='gitlab-item'>"
            "<div class='gitlab-item-row'>"
            f"<a class='gitlab-item-title' href='{html.escape(item.get('web_url', '#'))}'>"
            f"{prefix}{html.escape(str(item.get('iid', '?')))} {html.escape(item.get('title', ''))}</a>"
            f"<span class='gitlab-item-meta'>{html.escape(assignee_names)} &middot; {html.escape(updated)}</span>"
            "</div>"
            f"{label_row}"
            "</li>"
        )

    gitlab_sections = []
    for alias, entry in live.items():
        issues = entry.get("issues", [])
        mrs = entry.get("mrs", [])
        issue_items = "".join(gitlab_item(i, "#") for i in issues) or "<li>(none)</li>"
        mr_items = "".join(gitlab_item(m, "!") for m in mrs) or "<li>(none)</li>"
        gitlab_sections.append(
            "<div class='project-block'>"
            f"<h3>{html.escape(alias)}</h3>"
            f"<p>Issues <span class='badge-count'>{len(issues)}</span></p>"
            f"{error_notice(entry.get('issues_error'))}"
            f"<ul class='plain gitlab-list'>{issue_items}</ul>"
            f"<p>MRs <span class='badge-count'>{len(mrs)}</span></p>"
            f"{error_notice(entry.get('mrs_error'))}"
            f"<ul class='plain gitlab-list'>{mr_items}</ul>"
            "</div>"
        )
    return "".join(gitlab_sections) or _empty_state_html(
        "No projects configured yet, so there's nothing to check for issues or MRs.",
        "/settings", "Set up a project",
    )


def render_gitlab_page():
    """Live GitLab page shell. Renders instantly - the actual data (slow:
    a subprocess + real GitLab API call per configured project) is fetched
    by the browser from /gitlab/live after the page paints, replacing the
    data-lazy-load placeholder below (see render_gitlab_live_fragment and
    _render_shell's lazy-load script)."""
    status = read_status(STATUS_PATH)

    body = f"""
<div class="page-title">
<h1>Live GitLab</h1>
<p class="subtitle">Open issues and merge requests assigned to you, across configured projects.</p>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_GITLAB}<h2>Live GitLab (open issues &amp; MRs)</h2></div>
<div data-lazy-load='/gitlab/live'>
<div class="lazy-loading"><div class="md-spinner"></div><p class="loading-text">Loading live GitLab data<span class="loading-dots"><span>.</span><span>.</span><span>.</span></span></p></div>
</div>
</section>
</div>
"""
    return _render_shell(
        "Live GitLab · Loop X Engineering", "gitlab", _status_badge_markup(status), body, refresh=True, refresh_note=True
    )


_ACCENT_CHOICES = (
    # (key, label, nav-preview color) - the third value is the exact,
    # fixed --md-nav-surface hex for that accent (see _STYLE), used as
    # the swatch's own mini-layout preview color so the picker always
    # looks the same regardless of the dashboard's current light/dark
    # mode. "Default" uses a plain neutral instead of a hex, matching its
    # actual (lack of) sidebar tint.
    ("default", "Default", "#E3DFE3"),
    ("indigo", "Indigo", "#f4f0ff"),
    ("blue", "Blue", "#e9f3fc"),
    ("green", "Green", "#ecf4ee"),
    ("red", "Red", "#fcf1ef"),
    ("gray", "Gray", "#ececef"),
)


def render_preferences_page():
    """Appearance preferences: color mode (light/dark/auto), accent color,
    and font. Deliberately client-only - saved to this browser's
    localStorage (see _render_shell's pre-paint script, which restores all
    three as data-color-mode/data-accent/data-font on <html> before first
    paint) rather than a server-side file, since this is a single-user
    localhost tool and every other page already uses the same localStorage
    pattern for the sidebar's collapsed state. No POST route, no CSRF token
    needed - there is nothing here for the server to do."""
    status = read_status(STATUS_PATH)

    mode_buttons = "".join(
        f"<button type='button' class='pref-segmented-option' data-color-mode-choice=\"{mode}\">{label}</button>"
        for mode, label in (("light", "Light"), ("dark", "Dark"), ("auto", "Auto"))
    )

    swatch_buttons = "".join(
        f"<button type='button' class='pref-swatch' data-accent-choice=\"{key}\">"
        "<span class='pref-swatch-preview'>"
        f"<span class='pref-swatch-preview-nav' style='background:{nav_color}'></span>"
        "<span class='pref-swatch-preview-content'></span>"
        f"</span>{label}</button>"
        for key, label, nav_color in _ACCENT_CHOICES
    )

    # Each button previews its own typeface directly in the label (inline
    # style, not a --font-family-stack swap) so the picker shows what every
    # choice actually looks like without switching the whole page first.
    font_buttons = "".join(
        f"<button type='button' class='pref-segmented-option' data-font-choice=\"{key}\" "
        f"style=\"font-family: '{name}', {_FALLBACK_FONT_STACK}\">{label}</button>"
        for key, label, name in _FONT_CHOICES
    )

    refresh_buttons = "".join(
        f"<button type='button' class='pref-segmented-option' data-refresh-choice=\"{seconds}\">{label}</button>"
        for seconds, label in (("5", "5s"), ("11", "11s"), ("30", "30s"), ("60", "1 min"), ("300", "5 min"))
    )

    body = f"""
<div class="page-title">
<h1>Preferences</h1>
<p class="subtitle">Appearance settings for this browser - saved locally, not shared across devices.</p>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_PREFERENCES}<h2>Color mode</h2></div>
<p class="section-subtitle">Choose how the interface looks - light, dark, or match your system setting.</p>
<div class="pref-segmented" role="group" aria-label="Color mode">{mode_buttons}</div>
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header"><span class="pref-theme-icon">{_SECTION_ICON_PREFERENCES}</span><h2>Theme</h2></div>
<p class="section-subtitle">Select the accent color for the application interface.</p>
<div class="pref-swatches" role="group" aria-label="Accent color">{swatch_buttons}</div>
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_PREFERENCES}<h2>Font</h2></div>
<p class="section-subtitle">Choose the typeface used across the dashboard.</p>
<div class="pref-segmented" role="group" aria-label="Font">{font_buttons}</div>
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_PREFERENCES}<h2>Auto-refresh</h2></div>
<p class="section-subtitle">How often pages reload themselves to show live status.</p>
<div class="pref-segmented" role="group" aria-label="Auto-refresh interval">{refresh_buttons}</div>
</section>
</div>

<script>
(function() {{
  function apply() {{
    var mode = localStorage.getItem('loop-dashboard-color-mode') || 'auto';
    var accent = localStorage.getItem('loop-dashboard-accent') || 'default';
    var font = localStorage.getItem('loop-dashboard-font') || 'roboto';
    var refreshSeconds = localStorage.getItem('loop-dashboard-refresh-interval') || '30';
    document.querySelectorAll('[data-color-mode-choice]').forEach(function(btn) {{
      btn.classList.toggle('is-active', btn.getAttribute('data-color-mode-choice') === mode);
    }});
    document.querySelectorAll('[data-accent-choice]').forEach(function(btn) {{
      btn.classList.toggle('is-active', btn.getAttribute('data-accent-choice') === accent);
    }});
    document.querySelectorAll('[data-font-choice]').forEach(function(btn) {{
      btn.classList.toggle('is-active', btn.getAttribute('data-font-choice') === font);
    }});
    document.querySelectorAll('[data-refresh-choice]').forEach(function(btn) {{
      btn.classList.toggle('is-active', btn.getAttribute('data-refresh-choice') === refreshSeconds);
    }});
  }}
  document.querySelectorAll('[data-color-mode-choice]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var mode = btn.getAttribute('data-color-mode-choice');
      if (mode === 'auto') {{
        localStorage.removeItem('loop-dashboard-color-mode');
        document.documentElement.removeAttribute('data-color-mode');
      }} else {{
        localStorage.setItem('loop-dashboard-color-mode', mode);
        document.documentElement.setAttribute('data-color-mode', mode);
      }}
      apply();
    }});
  }});
  document.querySelectorAll('[data-accent-choice]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var accent = btn.getAttribute('data-accent-choice');
      localStorage.setItem('loop-dashboard-accent', accent);
      document.documentElement.setAttribute('data-accent', accent);
      apply();
    }});
  }});
  document.querySelectorAll('[data-font-choice]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      var font = btn.getAttribute('data-font-choice');
      localStorage.setItem('loop-dashboard-font', font);
      document.documentElement.setAttribute('data-font', font);
      apply();
    }});
  }});
  document.querySelectorAll('[data-refresh-choice]').forEach(function(btn) {{
    btn.addEventListener('click', function() {{
      localStorage.setItem('loop-dashboard-refresh-interval', btn.getAttribute('data-refresh-choice'));
      apply();
    }});
  }});
  apply();
}})();
</script>
"""
    return _render_shell("Preferences · Loop X Engineering", "preferences", _status_badge_markup(status), body)


def render_instructions_page(flash=None, flash_ok=True):
    """Lets you write your own free-text instructions, saved server-side
    to CUSTOM_INSTRUCTIONS_PATH (~/.loop-engineering/instructions.md) via
    the /instructions POST route below - unlike Preferences, this has to
    actually be read by the loop itself (see LOOPX_INSTRUCTIONS.md's own
    step reading this file), not just influence how the browser renders,
    so it's a real server-side file rather than localStorage."""
    status = read_status(STATUS_PATH)
    current_text = read_custom_instructions()

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    ai_cli_name = _AI_CLI_DISPLAY_NAMES[ai_cli_config.get_selected_cli(ai_cli_config.DEFAULT_CONFIG_PATH)]
    body = f"""
<div class="page-title">
<h1>Instructions</h1>
<p class="subtitle">Include specific instructions in {html.escape(ai_cli_name)}'s system prompt whenever the loop runs.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_INSTRUCTIONS}<h2>Your instructions</h2></div>
<p class="section-subtitle">Saved to <code>~/.loop-engineering/instructions.md</code> - read at the start of every run, on top of everything already in <code>LOOPX_INSTRUCTIONS.md</code>.</p>
<form method='post' action='/instructions' class='daemon-action-form'>
{csrf_input}
<textarea name='instructions' class='instructions-textarea' rows='24' placeholder="e.g. Prefer descriptive commit messages. Never touch files under vendor/.">{html.escape(current_text)}</textarea>
<button type='submit' class='btn btn-primary'>Save</button>
</form>
</section>
</div>
"""
    return _render_shell("Instructions · Loop X Engineering", "instructions", _status_badge_markup(status), body)


def render_readme_page():
    """This repo's own README.md, rendered in-app for anyone who'd rather
    not leave the dashboard (or doesn't have a GitHub/editor view of the
    repo handy) to see it. A quicknav built from the README's own H2
    headings floats fixed to the top-right of the viewport (see the
    .readme-quicknav CSS) so it stays reachable no matter how far down the
    page you've scrolled - see _markdown_h2_sections."""
    status = read_status(STATUS_PATH)
    try:
        content = README_PATH.read_text()
    except OSError:
        content = "# README\n\nNo README.md found in this repo."

    quicknav_links = "".join(
        f"<a href='#{slug}' class='readme-quicknav-link'>{html.escape(title)}</a>"
        for title, slug in _markdown_h2_sections(content)
        if title.strip().lower() != "table of contents"
    )
    quicknav_html = (
        "<nav class='readme-quicknav' aria-label='Jump to section'>"
        "<p class='readme-quicknav-title'>On this page</p>"
        f"{quicknav_links}</nav>"
        if quicknav_links else ""
    )

    body = f"""
<div class="page-title">
<h1>README</h1>
<p class="subtitle">This project's README, rendered here for reference.</p>
</div>

<div class="grid">
<section class="card">
{quicknav_html}
<div class="markdown">{render_markdown(content)}</div>
</section>
</div>
"""
    return _render_shell("README · Loop X Engineering", "readme", _status_badge_markup(status), body)


def render_memory_page():
    """Project Memory page: per-project task memory recorded by the
    automated review loop - one markdown file per GitLab issue
    (memory_store.list_task_memories), plus any entries recorded before
    this format existed (project_memory.get_learnings, shown under
    "Legacy learnings" so nothing already recorded disappears from view).
    An entry's issue number becomes a real link to that GitLab issue (via
    gitlab_issue_url_prefixes, same source render_markdown itself uses for
    "<alias> #<iid>" mentions), styled as a distinct pill-link rather than
    the plain pill-grey tags around it, so a reader can tell at a glance
    which pill is clickable. Falls back to the old plain pill when the
    alias's URL can't be resolved (no gitlab-config entry for it yet)
    rather than linking to a guessed, possibly-wrong URL."""
    status = read_status(STATUS_PATH)
    memory = get_project_memory()
    url_prefixes = gitlab_issue_url_prefixes()

    def issue_pill(alias, issue_iid):
        safe_iid = html.escape(str(issue_iid))
        base_url = url_prefixes.get(alias)
        if base_url:
            issue_url = f"{base_url}/-/issues/{issue_iid}"
            return (
                f"<a class='pill pill-link' href='{issue_url}' rel='noopener' target='_blank'>"
                f"<span class='material-symbols-outlined' aria-hidden='true'>open_in_new</span>"
                f"#{safe_iid}</a>"
            )
        return f"<span class='pill pill-grey'>#{safe_iid}</span>"

    def tag_pills(tags):
        return "".join(f"<span class='pill pill-grey'>{html.escape(tag)}</span>" for tag in tags or [])

    def task_item(alias, entry):
        meta_html = f"<div class='pill-row'>{issue_pill(alias, entry['issue_iid'])}{tag_pills(entry.get('tags'))}</div>"
        description = entry.get("description", "")
        description_html = (
            f"<p class='history-entry-overview'>{html.escape(description)}</p>" if description else ""
        )
        return (
            "<li class='learning-item'>"
            f"{description_html}"
            f"<div class='markdown'>{render_markdown(entry.get('body', ''))}</div>"
            f"{meta_html}"
            "</li>"
        )

    def legacy_item(alias, entry):
        issue_iid = entry.get("issue_iid")
        pill = issue_pill(alias, issue_iid) if issue_iid is not None else ""
        meta = f"{pill}{tag_pills(entry.get('tags'))}"
        meta_html = f"<div class='pill-row'>{meta}</div>" if meta else ""
        return (
            "<li class='learning-item'>"
            f"<div class='markdown'>{render_markdown(entry.get('lesson', ''))}</div>"
            f"{meta_html}"
            "</li>"
        )

    memory_sections = []
    for alias, data in memory.items():
        tasks, legacy = data["tasks"], data["legacy"]
        tasks_html = (
            f"<ul class='plain'>{''.join(task_item(alias, e) for e in tasks)}</ul>"
            if tasks else "<p>(no task memory recorded yet)</p>"
        )
        legacy_html = ""
        if legacy:
            legacy_html = (
                "<h4>Legacy learnings</h4>"
                f"<ul class='plain'>{''.join(legacy_item(alias, e) for e in legacy)}</ul>"
            )
        memory_sections.append(
            f"<div class='project-block'><h3>{html.escape(alias)}</h3>{tasks_html}{legacy_html}</div>"
        )
    memory_html = "".join(memory_sections) or _empty_state_html(
        "No projects configured yet, so there is no memory recorded.",
        "/settings", "Set up a project",
    )

    body = f"""
<div class="page-title">
<h1>Project Memory</h1>
<p class="subtitle">Task memory recorded per project by the automated review loop.</p>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_MEMORY}<h2>Project Memory</h2></div>
{memory_html}
</section>
</div>
"""
    return _render_shell("Memory · Loop X Engineering", "memory", _status_badge_markup(status), body)


def _topic_latest_data_html(topics, history_dir=None):
    """Each configured topic's most recently saved briefing, collapsed to
    an overview + tags and expanding inline on click (plain onclick
    toggling `is-expanded`, matching render_skills_page's own no-framework
    convention). Shared by the Topic Monitor page's own "Latest Data"
    section and the Activity page's "Latest Topic Run Review" card so
    neither duplicates the other's markup."""
    if history_dir is None:
        history_dir = TOPIC_MONITOR_HISTORY_DIR
    if not topics:
        return "<p>No topics configured yet. Add one on the <a href='/topic-monitor/settings'>Topic Settings</a> page.</p>"

    blocks = []
    for topic in topics:
        name = topic["name"]
        label = topic.get("label", name)
        history_names = list_topic_history(name, history_dir)
        if not history_names:
            blocks.append(
                f"<div class='topic-latest-item'><h3>{html.escape(str(label))}</h3>"
                "<p class='topic-latest-overview'>(no data yet)</p></div>"
            )
            continue
        latest_name = history_names[0]
        content = read_history_file(latest_name, history_dir) or ""
        overview = extract_history_overview(content)
        tags_html = "".join(
            f"<span class='pill pill-grey'>{html.escape(t)}</span>"
            for t in topic_history_tags(latest_name, content)
        )
        # `latest_name` is guaranteed by list_topic_history's own
        # <date>-<topic_name>.md convention to start with a YYYY-MM-DD
        # date, so the leading 10 characters are always just the date -
        # fed to _relative_time (as midnight UTC; it requires an
        # offset-aware timestamp) for the same "Nd ago" phrasing used
        # elsewhere.
        latest_when = _relative_time(latest_name[:10] + "T00:00:00Z")
        blocks.append(
            "<div class='topic-latest-item'>"
            "<div class='topic-latest-summary' tabindex='0' role='button' aria-expanded='false' "
            "onclick=\"this.classList.toggle('is-expanded'); "
            "this.setAttribute('aria-expanded', this.classList.contains('is-expanded'))\" "
            "onkeydown=\"if (event.key === 'Enter' || event.key === ' ') { "
            "event.preventDefault(); this.click(); }\">"
            f"<h3>{html.escape(str(label))} {_EXPAND_ICON}"
            f"<span class='topic-last-run'>latest {html.escape(latest_when)}</span></h3>"
            f"<p class='topic-latest-overview'>{html.escape(overview)}</p>"
            f"<span class='pill-row'>{tags_html}</span>"
            "</div>"
            "<div class='topic-latest-detail'>"
            f"<div class='markdown'>{render_markdown(content)}</div>"
            "</div>"
            "</div>"
        )
    return "".join(blocks)


def render_topic_monitor_page(flash=None, flash_ok=True):
    """Topic Monitor page: every configured topic's current status (from
    write-topic-status), in its own "Topics" section. Adding/editing/
    deleting topics lives on its own page instead (render_topic_settings_page,
    /topic-monitor/settings), reachable from the sidebar's Configuration
    group, so editing configuration doesn't clutter this at-a-glance status
    view. Full saved briefings still live on the Run History page (see
    render_history_page), alongside the GitLab loop's own history - but a
    "Latest Data" section right after Topics surfaces each topic's most
    recent briefing inline (overview + tags collapsed, expanding to the
    full rendered content on click), so seeing the newest data doesn't
    require leaving this page. Shows the GitLab loop's own status badge in
    the header for the same reason every other page here does - that badge
    is this dashboard's shared "is anything running" indicator, not scoped
    to one page's own subject.

    `flash`/`flash_ok` carry a POST-redirect-GET result from the "Run now"
    button's /topic-monitor/run-now route, same convention as
    render_overview_page's own /run-now button."""
    status = read_status(STATUS_PATH)
    topics = get_configured_topics()
    topic_status = read_topic_status(TOPIC_MONITOR_STATUS_PATH)["topics"]
    any_topic_running = any(entry.get("state") == "running" for entry in topic_status.values())

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    if any_topic_running or not topics:
        # The `not topics` half is the fix: this stayed enabled with zero
        # topics configured before (any_topic_running is vacuously False
        # over an empty topic_status), so clicking Run now would start a
        # run with nothing to actually research. Hidden outright rather
        # than shown disabled-with-a-hint (contrast render_overview_page's
        # own use of _run_now_action_html's disabled_hint_html): this
        # page's own topics_html already renders "No topics configured
        # yet. Add one on the Topic Settings page." right above this exact
        # spot, so a second, separate explanation here would just repeat it.
        run_now_form = ""
    else:
        run_now_form = _run_now_action_html(
            "/topic-monitor/run-now",
            "Run the topic monitor loop now? This starts a real automated run outside its normal schedule.",
            csrf_input,
        )

    if not topics:
        topics_html = _empty_state_html(
            "No topics configured yet, so there's nothing to monitor.",
            "/topic-monitor/settings", "Set up a topic",
        )
        latest_data_section = ""
    else:
        status_blocks = []
        for topic in topics:
            name = topic["name"]
            label = topic.get("label", name)
            # The whole status entry goes to _status_badge_markup, not a
            # synthetic {"state": ...}: that's what lets the badge show
            # current_step ("researching") while a topic is running, via
            # _progress_text. `state` is defaulted in for a topic that has
            # never run and so has no entry at all.
            entry = dict(topic_status.get(name, {}))
            entry.setdefault("state", "never_run")
            badge_html = _status_badge_markup(entry)
            # "Last run" is the third thing the spec asks this page to show,
            # alongside idle/running. Omitted entirely for a never-run topic
            # rather than rendering an empty relative time.
            last_run = _relative_time(entry.get("updated_at", ""))
            last_run_html = (
                f" <span class='topic-last-run'>last run {html.escape(last_run)}</span>" if last_run else ""
            )
            status_blocks.append(
                f"<div class='project-block'><h3>{html.escape(str(label))} {badge_html}{last_run_html}</h3></div>"
            )
        topics_html = "".join(status_blocks)
        # "Latest Data" surfaces each topic's most recently saved briefing
        # right after Topics (see _topic_latest_data_html) instead of
        # linking out to /topic-monitor/history/<name> - this page's own
        # status view deliberately never links there (see
        # render_history_page for the full-page equivalent).
        latest_data_section = f"""
<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_TOPIC_MONITOR}<h2>Latest Data</h2></div>
{_topic_latest_data_html(topics)}
</section>
</div>
"""

    body = f"""
<div class="page-title">
<h1>Topic Monitor</h1>
<p class="subtitle">Status for every configured topic. Saved briefings are on the <a href="/history">Run History</a> page.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_TOPIC_MONITOR}<h2>Topics</h2></div>
{topics_html}
{run_now_form}
</section>
</div>
{latest_data_section}
"""
    return _render_shell(
        "Topic Monitor · Loop X Engineering", "topic_monitor", _status_badge_markup(status), body,
        refresh=True, refresh_note=True,
    )


def render_topic_settings_page(flash=None, flash_ok=True):
    """Topic Settings page: adding/editing/deleting topics - split out of
    render_topic_monitor_page (see its docstring) so editing configuration
    doesn't clutter that page's at-a-glance status view. Reachable from the
    sidebar's Configuration group rather than Monitor, since this page is
    about configuration, not live status.

    `flash`/`flash_ok` carry a POST-redirect-GET result from
    /topic-monitor/topics or /topic-monitor/topics/<name>/delete."""
    status = read_status(STATUS_PATH)
    topics = get_configured_topics()
    bundles = read_gitlab_config(GITLAB_CONFIG_PATH).get("bundles", {})

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    settings_blocks = []
    for index, topic in enumerate(topics):
        name = topic["name"]
        label = topic.get("label", name)
        safe_name = html.escape(name)
        url_safe_name = urllib.parse.quote(name, safe="")
        delete_confirm = html.escape(f"Delete topic {name}? This does not delete its saved briefings.", quote=True)
        # Save/Delete render as one shared action column instead of each
        # sitting inside its own form (see .topic-row-actions below) - the
        # Delete <form> still exists (it needs its own POST target/CSRF
        # input) but stays visually inert; its button lives outside it and
        # targets it via the `form=` attribute, same trick used for Save.
        edit_form_id = f"topic-edit-form-{index}"
        delete_form_id = f"topic-delete-form-{index}"
        settings_blocks.append(f"""
<div class='project-block'>
<h3>{html.escape(str(label))} <code>{safe_name}</code></h3>
<div class='topic-row'>
<form method='post' action='/topic-monitor/topics' class='daemon-action-form topic-row-fields' id='{edit_form_id}'>
{csrf_input}
<input type='hidden' name='name' value='{safe_name}'>
<input type='text' name='label' value='{html.escape(topic.get("label", ""))}' placeholder='label' required>
{_custom_select('slack_bundle', bundles, topic.get('slack_bundle') or '', empty_label='(use default webhook)')}
<input type='text' name='brief' value='{html.escape(topic.get("brief", ""))}' placeholder='what counts as notable' class='topic-row-brief' required>
</form>
<div class='topic-row-actions'>
<button type='submit' form='{edit_form_id}' class='btn btn-neutral'>Save</button>
<form method='post' action='/topic-monitor/topics/{url_safe_name}/delete' id='{delete_form_id}'>{csrf_input}</form>
<button type='submit' form='{delete_form_id}' class='btn btn-warning' data-confirm="{delete_confirm}">
<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>
</div>
</div>
</div>
""")
    settings_html = "".join(settings_blocks)

    add_topic_form = f"""
<form method='post' action='/topic-monitor/topics' class='daemon-action-form add-row-form'>
{csrf_input}
<input type='text' name='name' placeholder='topic name' required>
<input type='text' name='label' placeholder='label' required>
<input type='text' name='brief' placeholder='what counts as notable' required>
{_custom_select('slack_bundle', bundles, None, empty_label='(use default webhook)')}
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>add</span> Add topic</button>
</form>
"""

    body = f"""
<div class="page-title">
<h1>Topic Settings</h1>
<p class="subtitle">Add, edit, or delete topics. Live status is on the <a href="/topic-monitor">Topic Monitor</a> page.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SETTINGS}<h2>Topic Settings</h2></div>
{settings_html}
{add_topic_form}
</section>
</div>
"""
    return _render_shell("Topic Settings · Loop X Engineering", "topic_settings", _status_badge_markup(status), body)


def render_skills_page(flash=None, flash_ok=True):
    """Skills page: every external skill (from the `encore-skills` library)
    this loop depends on, whether it's actually installed on this machine
    right now (SKILLS_ROOT, checked live via get_skills_status), and which
    files in this repo call it - so a new team member setting up this loop
    can see at a glance what else they need before running it, without
    reading through LOOPX_INSTRUCTIONS.md line by line.

    "Used by"/"Path" aren't columns at all - they're not worth a header
    the user sees on every visit just to stay empty. Each skill renders as
    two rows: a summary row (Skill/Status/What it does - enough to answer
    "is this loop ready to run" at a glance) and a detail row directly
    below it, hidden until the summary row is clicked (a plain onclick
    toggling `is-expanded`, matching this dashboard's existing
    no-framework, sprinkle-of-JS style - see _sidebar_html's collapse
    toggle).

    A missing skill gets a one-click "Set up & restart dashboard" button
    (POSTs to /skills/install, see trigger_skills_install) instead of a
    terminal command - `flash`/`flash_ok` carry that action's
    POST-redirect-GET result, same convention as render_daemons_page.
    While an install is already running (skills_install_status.json's
    state), the button is replaced with a pending notice instead of
    letting a second install stack on top of it."""
    status = read_status(STATUS_PATH)
    skills = get_skills_status()
    install_status = read_status(SKILLS_INSTALL_STATUS_PATH)
    installing = install_status.get("state") == "installing"

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    skill_rows = []
    for s in skills:
        if s["installed"]:
            status_cell = f"<span class='pill pill-green'>{_CHECK_ICON}installed</span>"
        elif installing:
            status_cell = (
                f"<span class='pill pill-blue'>{_SPINNER_ICON}setup in progress…</span>"
            )
        else:
            confirm_msg = html.escape(
                f"Set up {s['name']}? This installs it in the background and restarts the dashboard "
                "when done - the page may briefly go offline.",
                quote=True,
            )
            status_cell = (
                "<span class='pill pill-grey'>not installed</span>"
                "<form method='post' action='/skills/install' class='daemon-action-form'>"
                f"{csrf_input}"
                f"<button type='submit' class='btn btn-primary' data-confirm=\"{confirm_msg}\">"
                "Set up &amp; restart dashboard</button>"
                "</form>"
            )
        used_by_html = "".join(f"<li><code>{html.escape(u)}</code></li>" for u in s["used_by"])
        skill_rows.append(
            "<tr class='skill-row' tabindex='0' role='button' aria-expanded='false' "
            "onclick=\"this.classList.toggle('is-expanded'); "
            "this.setAttribute('aria-expanded', this.classList.contains('is-expanded'))\" "
            "onkeydown=\"if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); this.click(); }\">"
            f"<td>{html.escape(s['name'])} {_EXPAND_ICON}</td>"
            f"<td>{status_cell}</td>"
            f"<td>{html.escape(s['description'])}</td>"
            "</tr>"
            "<tr class='skill-detail-row'><td colspan='3'>"
            f"<p><strong>Used by</strong></p><ul class='plain'>{used_by_html}</ul>"
            f"<p><strong>Path</strong></p><code>{html.escape(s['path'])}</code>"
            "</td></tr>"
        )
    skills_html = (
        "<div class='table-wrap'><table class='daemons skills'>"
        "<thead><tr><th>Skill</th><th>Status</th><th>What it does</th></tr></thead>"
        f"<tbody>{''.join(skill_rows)}</tbody>"
        "</table></div>"
    ) if skill_rows else "<p>(no skill dependencies registered)</p>"

    body = f"""
<div class="page-title">
<h1>Skills</h1>
<p class="subtitle">External skills this loop depends on, and whether each is installed on this machine. Click a row for details.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SKILLS}<h2>Required skills</h2></div>
{skills_html}
</section>
</div>
"""
    return _render_shell("Skills · Loop X Engineering", "skills", _status_badge_markup(status), body)


_WEEKDAY_LABELS = (("0", "Su"), ("1", "Mo"), ("2", "Tu"), ("3", "We"), ("4", "Th"), ("5", "Fr"), ("6", "Sa"))


_SCHEDULE_FREQUENCIES = ("Daily", "Weekly", "Monthly")


def _schedule_form_html(daemon, csrf_input):
    """A time input, a Daily/Weekly/Monthly frequency dropdown, and either
    day-of-week checkboxes (Weekly) or a day-of-month dropdown (Monthly)
    for editing one daemon's schedule - pre-filled from its current
    StartCalendarInterval, with only the frequency's own control shown
    (the other stays in the form, hidden, so switching back doesn't lose
    what was picked - see the 'change' listener on select[name=frequency]
    in _render_shell). A schedule with a Day key is Monthly; one with a
    Weekday key is Weekly; anything else (including no schedule at all,
    which never reaches this function - see render_daemons_page's
    `if d.get("schedule") is not None` guard) is Daily, which pre-checks
    every weekday box - matching build_calendar_interval's "empty/all-seven
    both mean every day" convention - so switching to Weekly starts from a
    sensible "every day" state rather than none selected."""
    schedule = daemon.get("schedule")
    entries = schedule if isinstance(schedule, list) else ([schedule] if schedule else [])
    hours = {e.get("Hour") for e in entries if isinstance(e, dict)}
    minutes = {e.get("Minute") for e in entries if isinstance(e, dict)}
    hour = hours.pop() if len(hours) == 1 else 9
    minute = minutes.pop() if len(minutes) == 1 else 0
    day_of_month = next((e["Day"] for e in entries if isinstance(e, dict) and "Day" in e), None)
    selected_weekdays = {e["Weekday"] for e in entries if isinstance(e, dict) and "Weekday" in e}
    if day_of_month:
        frequency = "Monthly"
    elif selected_weekdays:
        frequency = "Weekly"
    else:
        frequency = "Daily"
    if not selected_weekdays:
        selected_weekdays = set(range(7))

    time_value = f"{int(hour):02d}:{int(minute):02d}"
    checkboxes = "".join(
        f"<label class='md-checkbox weekday-check'><input type='checkbox' name='weekday' value='{value}'"
        f"{' checked' if int(value) in selected_weekdays else ''}> {label}</label>"
        for value, label in _WEEKDAY_LABELS
    )
    day_select = _custom_select("day_of_month", (str(d) for d in range(1, 32)), str(day_of_month or 1))
    freq_select = _custom_select("frequency", _SCHEDULE_FREQUENCIES, frequency)
    weekly_style = "" if frequency == "Weekly" else " style='display:none'"
    monthly_style = "" if frequency == "Monthly" else " style='display:none'"
    safe_file = html.escape(daemon["file"])
    return (
        f"<form method='post' action='/daemons/{safe_file}/schedule' class='daemon-action-form schedule-form'>"
        f"{csrf_input}"
        f"<input type='time' name='time' value='{time_value}'>"
        f"{freq_select}"
        f"<span class='weekday-checks weekly-controls'{weekly_style}>{checkboxes}</span>"
        f"<span class='monthly-controls'{monthly_style}>on day {day_select}</span>"
        "<button type='submit' class='btn btn-neutral'>Save schedule</button>"
        "</form>"
    )


def render_daemons_page(flash=None, flash_ok=True):
    """Launchd Daemons page: load state, schedule, and enable/disable
    controls for every launchd daemon in this project.

    `flash`/`flash_ok` carry a POST-redirect-GET result from
    /daemons/<file>/enable or /daemons/<file>/disable (see
    DashboardHandler._redirect_with_flash). `flash` may contain launchctl's
    own stderr output, which is untrusted text, so it always goes through
    html.escape()."""
    status = read_status(STATUS_PATH)
    daemons = get_daemons_status(LAUNCHD_DIR)

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    daemon_rows = []
    for d in daemons:
        if "error" in d:
            # colspan spans the remaining 5 of the table's 6 columns (Label,
            # Status, Trigger, Runs, Schedule, Action) so the error text
            # fills the row.
            daemon_rows.append(
                "<tr>"
                f"<td>{html.escape(d['file'])}</td>"
                f"<td colspan='5'>error parsing plist: {html.escape(d['error'])}</td>"
                "</tr>"
            )
            continue
        label = d.get("label") or d["file"]
        safe_file = html.escape(d["file"])
        safe_label = html.escape(str(label))
        if d["loaded"]:
            loaded_text = f"loaded (pid {html.escape(str(d['pid']))})" if d.get("pid") else "loaded"
            loaded_pill = f"<span class='pill pill-green'>{_CHECK_ICON}{html.escape(loaded_text)}</span>"
            action_html = (
                f"<form method='post' action='/daemons/{safe_file}/disable' class='daemon-action-form'>"
                f"{csrf_input}"
                f"<button type='submit' class='switch is-on' role='switch' aria-checked='true' "
                f"aria-label='Disable {safe_label}' title='Disable {safe_label}'>"
                "<span class='switch-thumb'></span></button>"
                "</form>"
            )
        else:
            loaded_pill = "<span class='pill pill-grey'>not loaded</span>"
            # html.escape(..., quote=True) is enough here (unlike the old
            # onclick="return confirm(...)" this replaced, data-confirm is a
            # plain HTML attribute, not a JS string literal - no json.dumps
            # needed to escape out of anything).
            confirm_msg = f"Enable {label}? This will let it start running on its schedule."
            confirm_attr = html.escape(confirm_msg, quote=True)
            action_html = (
                f"<form method='post' action='/daemons/{safe_file}/enable' class='daemon-action-form'>"
                f"{csrf_input}"
                f"<button type='submit' class='switch is-off' role='switch' aria-checked='false' "
                f"aria-label='Enable {safe_label}' title='Enable {safe_label}' "
                f"data-confirm=\"{confirm_attr}\">"
                "<span class='switch-thumb'></span></button>"
                "</form>"
            )
        trigger = _describe_trigger(d)
        runs = " ".join(str(arg) for arg in (d.get("program_arguments") or []))
        schedule_html = _schedule_form_html(d, csrf_input) if d.get("schedule") is not None else "—"
        daemon_rows.append(
            "<tr>"
            f"<td>{safe_label}</td>"
            f"<td>{loaded_pill}</td>"
            f"<td>{html.escape(trigger)}</td>"
            f"<td><code>{html.escape(runs)}</code></td>"
            f"<td>{schedule_html}</td>"
            f"<td>{action_html}</td>"
            "</tr>"
        )
    daemons_html = (
        "<div class='table-wrap'><table class='daemons'>"
        "<thead><tr><th>Label</th><th>Status</th><th>Trigger</th><th>Runs</th><th>Schedule</th><th>Action</th></tr></thead>"
        f"<tbody>{''.join(daemon_rows)}</tbody>"
        "</table></div>"
    ) if daemon_rows else "<p>(no launchd plist files found)</p>"

    body = f"""
<div class="page-title">
<h1>Launchd Daemons</h1>
<p class="subtitle">Load state, schedule, and enable/disable controls for every launchd daemon in this project.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_DAEMONS}<h2>Launchd Daemons</h2></div>
{daemons_html}
</section>
</div>
"""
    return _render_shell("Daemons · Loop X Engineering", "daemons", _status_badge_markup(status), body)


def render_settings_page(flash=None, flash_ok=True):
    """The GitLab page (nav key stays "settings" - only its visible label
    changed - to avoid clashing with the existing "gitlab" nav key/route,
    which is the unrelated Live GitLab issues/MRs page): view and manage
    ~/.gitlab/config.json (instances, project aliases, default instance,
    access bundles) and ~/.loop-engineering/projects.json (which projects
    this loop tracks, their local checkout/build commands, and the loop's
    own default settings). Slack's own webhook config lives on the
    separate /notifications page now - this function only reads
    ~/.slack/config.json for the access bundles' webhook overrides, which
    stay here since a bundle is fundamentally a GitLab-access construct.

    This function only ever reads (via read_gitlab_config/read_slack_config/
    read_loop_projects_config) and masks every secret it renders
    (_mask_secret) - the real token value is never sent to the browser.
    Every write goes through DashboardHandler's /settings/* POST routes,
    which call the upsert_gitlab_instance/delete_gitlab_instance/
    upsert_gitlab_project/delete_gitlab_project/set_default_gitlab_instance/
    upsert_tracked_project/delete_tracked_project/update_loop_project_settings
    helpers.

    `flash`/`flash_ok` carry a POST-redirect-GET result from any of those
    POST routes, same convention as render_daemons_page."""
    status = read_status(STATUS_PATH)
    gitlab_config = read_gitlab_config(GITLAB_CONFIG_PATH)
    slack_config = read_slack_config(SLACK_CONFIG_PATH)
    loop_projects_config = read_loop_projects_config()
    bundles = gitlab_config.get("bundles", {})
    bundle_webhooks = slack_config.get("bundle_webhooks", {})

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    instances = gitlab_config.get("instances", {})
    projects = gitlab_config.get("projects", {})
    default_instance = gitlab_config.get("default", "")

    default_form = f"""
<form method='post' action='/settings/gitlab/default' class='daemon-action-form single-field'>
{csrf_input}
{_custom_select('instance', instances, default_instance)}
<button type='submit' class='btn btn-neutral'>Set default</button>
</form>
"""

    instance_rows = []
    for name, inst in instances.items():
        safe_name = html.escape(name)
        url_safe_name = urllib.parse.quote(name, safe="")
        badge = " <span class='pill pill-blue'>default</span>" if name == default_instance else ""
        confirm_msg = f"Delete GitLab instance {name}? Any project alias using it will need to be reassigned first."
        confirm_attr = html.escape(confirm_msg, quote=True)
        instance_rows.append(
            "<tr>"
            f"<td>{safe_name}{badge}</td>"
            f"<td>{html.escape(_mask_secret(inst.get('token', '')))}</td>"
            "<td>"
            "<form method='post' action='/settings/gitlab/instances' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<input type='hidden' name='alias' value='{safe_name}'>"
            f"<input type='text' name='url' value='{html.escape(inst.get('url', ''))}' placeholder='https://gitlab.example.com'>"
            "<input type='password' name='token' placeholder='leave blank to keep current'>"
            "<button type='submit' class='btn btn-neutral'>Save</button>"
            "</form>"
            "</td>"
            "<td>"
            f"<form method='post' action='/settings/gitlab/instances/{url_safe_name}/delete' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<button type='submit' class='btn btn-warning' data-confirm=\"{confirm_attr}\">"
            "<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    instances_html = (
        "<div class='table-wrap'><table class='daemons'>"
        "<thead><tr><th>Instance</th><th>Token</th><th>Edit</th><th>Delete</th></tr></thead>"
        f"<tbody>{''.join(instance_rows)}</tbody>"
        "</table></div>"
    ) if instance_rows else "<p>(no GitLab instances configured)</p>"

    add_instance_form = f"""
<form method='post' action='/settings/gitlab/instances' class='daemon-action-form add-row-form'>
{csrf_input}
<input type='text' name='alias' placeholder='instance name' required>
<input type='text' name='url' placeholder='https://gitlab.example.com' required>
<input type='password' name='token' placeholder='required'>
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>add</span> Add instance</button>
</form>
"""

    project_rows = []
    for alias, project in projects.items():
        safe_alias = html.escape(alias)
        url_safe_alias = urllib.parse.quote(alias, safe="")
        confirm_msg = f"Delete project alias {alias}?"
        confirm_attr = html.escape(confirm_msg, quote=True)
        project_rows.append(
            "<tr>"
            f"<td>{safe_alias}</td>"
            "<td>"
            "<form method='post' action='/settings/gitlab/projects' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<input type='hidden' name='alias' value='{safe_alias}'>"
            f"<input type='text' name='project_id' value='{html.escape(project.get('project_id', ''))}' placeholder='namespace/project'>"
            f"{_custom_select('instance', instances, project.get('instance', ''))}"
            f"{_custom_select('bundle', bundles, project.get('bundle', ''), empty_label='(use instance default)')}"
            "<button type='submit' class='btn btn-neutral'>Save</button>"
            "</form>"
            "</td>"
            "<td>"
            f"<form method='post' action='/settings/gitlab/projects/{url_safe_alias}/delete' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<button type='submit' class='btn btn-warning' data-confirm=\"{confirm_attr}\">"
            "<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    projects_html = (
        "<div class='table-wrap'><table class='daemons'>"
        "<thead><tr><th>Alias</th><th>Edit</th><th>Delete</th></tr></thead>"
        f"<tbody>{''.join(project_rows)}</tbody>"
        "</table></div>"
    ) if project_rows else "<p>(no project aliases configured)</p>"

    add_project_form = f"""
<form method='post' action='/settings/gitlab/projects' class='daemon-action-form add-row-form'>
{csrf_input}
<input type='text' name='alias' placeholder='project alias' required>
<input type='text' name='project_id' placeholder='namespace/project' required>
{_custom_select('instance', instances, None)}
{_custom_select('bundle', bundles, None, empty_label='(use instance default)')}
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>add</span> Add project</button>
</form>
"""

    tracked_projects = loop_projects_config.get("projects", {})
    loop_settings_form = f"""
<form method='post' action='/settings/loop-config' class='daemon-action-form'>
{csrf_input}
<input type='text' name='assignee_username' value='{html.escape(loop_projects_config.get("assignee_username", ""))}' placeholder='GitLab username'>
<input type='text' name='worktree_root' value='{html.escape(loop_projects_config.get("worktree_root", ""))}' placeholder='/absolute/path/to/worktrees'>
{_custom_select('gitlab_instance', instances, loop_projects_config.get('gitlab_instance', ''))}
<button type='submit' class='btn btn-neutral'>Save</button>
</form>
"""

    tracked_project_rows = []
    for alias, project in tracked_projects.items():
        safe_alias = html.escape(alias)
        url_safe_alias = urllib.parse.quote(alias, safe="")
        confirm_msg = f"Stop tracking project {alias}?"
        confirm_attr = html.escape(confirm_msg, quote=True)
        tracked_project_rows.append(
            "<tr>"
            "<td>"
            "<form method='post' action='/settings/loop-projects' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<input type='hidden' name='original_alias' value='{safe_alias}'>"
            f"<input type='text' name='alias' value='{safe_alias}' placeholder='project alias' required>"
            f"<input type='text' name='project_id' value='{html.escape(project.get('project_id', ''))}' placeholder='namespace/project'>"
            f"<input type='text' name='local_path' value='{html.escape(project.get('local_path', ''))}' placeholder='/abs/path/to/checkout'>"
            f"<input type='text' name='target_branch' value='{html.escape(project.get('target_branch', ''))}' placeholder='target branch'>"
            f"<input type='text' name='install_cmd' value='{html.escape(project.get('install_cmd', ''))}' placeholder='install command'>"
            f"<input type='text' name='lint_cmd' value='{html.escape(project.get('lint_cmd', ''))}' placeholder='lint command'>"
            f"<input type='text' name='test_cmd' value='{html.escape(project.get('test_cmd', ''))}' placeholder='test command'>"
            f"{_custom_select('instance', instances, project.get('instance', ''), empty_label='(use default)')}"
            "<button type='submit' class='btn btn-neutral'>Save</button>"
            "</form>"
            "</td>"
            "<td>"
            f"<form method='post' action='/settings/loop-projects/{url_safe_alias}/delete' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<button type='submit' class='btn btn-warning' data-confirm=\"{confirm_attr}\">"
            "<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    tracked_projects_html = (
        "<div class='table-wrap'><table class='daemons'>"
        "<thead><tr><th>Project</th><th>Delete</th></tr></thead>"
        f"<tbody>{''.join(tracked_project_rows)}</tbody>"
        "</table></div>"
    ) if tracked_project_rows else "<p>(no tracked projects configured)</p>"

    add_tracked_project_form = f"""
<form method='post' action='/settings/loop-projects' class='daemon-action-form add-row-form'>
{csrf_input}
<input type='text' name='alias' placeholder='project alias' required>
<input type='text' name='project_id' placeholder='namespace/project' required>
<input type='text' name='local_path' placeholder='/abs/path/to/checkout'>
<input type='text' name='target_branch' placeholder='target branch'>
<input type='text' name='install_cmd' placeholder='install command'>
<input type='text' name='lint_cmd' placeholder='lint command'>
<input type='text' name='test_cmd' placeholder='test command'>
{_custom_select('instance', instances, None, empty_label='(use default)')}
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>add</span> Add project</button>
</form>
"""

    bundle_rows = []
    for name, bundle in bundles.items():
        safe_name = html.escape(name)
        url_safe_name = urllib.parse.quote(name, safe="")
        webhook_override = bundle_webhooks.get(name, "")
        webhook_cell = html.escape(_mask_secret(webhook_override)) if webhook_override else "(not set)"
        clear_webhook_form = ""
        if webhook_override:
            clear_confirm = html.escape(f"Clear the Slack webhook override for bundle {name}?", quote=True)
            clear_webhook_form = (
                f" <form method='post' action='/settings/access-bundles/{url_safe_name}/clear-webhook' "
                "class='daemon-action-form' style='display:inline'>"
                f"{csrf_input}"
                f"<button type='submit' class='btn btn-neutral' data-confirm=\"{clear_confirm}\">Clear</button>"
                "</form>"
            )
        confirm_msg = f"Delete access bundle {name}? Any project alias using it will need to be reassigned first."
        confirm_attr = html.escape(confirm_msg, quote=True)
        bundle_rows.append(
            "<tr>"
            f"<td>{safe_name}</td>"
            f"<td>{html.escape(bundle.get('instance', ''))}</td>"
            f"<td>{html.escape(_mask_secret(bundle.get('token', '')))}</td>"
            f"<td>{webhook_cell}{clear_webhook_form}</td>"
            "<td>"
            "<form method='post' action='/settings/access-bundles' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<input type='hidden' name='name' value='{safe_name}'>"
            f"{_custom_select('instance', instances, bundle.get('instance', ''))}"
            "<input type='password' name='token' placeholder='leave blank to keep current token'>"
            "<input type='password' name='webhook_url' placeholder='leave blank to keep current webhook'>"
            "<button type='submit' class='btn btn-neutral'>Save</button>"
            "</form>"
            "</td>"
            "<td>"
            f"<form method='post' action='/settings/access-bundles/{url_safe_name}/delete' class='daemon-action-form'>"
            f"{csrf_input}"
            f"<button type='submit' class='btn btn-warning' data-confirm=\"{confirm_attr}\">"
            "<span class='material-symbols-outlined' aria-hidden='true'>delete</span> Delete</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    bundles_html = (
        "<div class='table-wrap'><table class='daemons'>"
        "<thead><tr><th>Bundle</th><th>Instance</th><th>Token</th><th>Slack webhook</th><th>Edit</th><th>Delete</th></tr></thead>"
        f"<tbody>{''.join(bundle_rows)}</tbody>"
        "</table></div>"
    ) if bundle_rows else "<p>(no access bundles configured)</p>"

    add_bundle_form = f"""
<form method='post' action='/settings/access-bundles' class='daemon-action-form add-row-form'>
{csrf_input}
<input type='text' name='name' placeholder='bundle name' required>
{_custom_select('instance', instances, None)}
<input type='password' name='token' placeholder='GitLab access token'>
<input type='password' name='webhook_url' placeholder='Slack webhook URL (optional)'>
<button type='submit' class='btn btn-neutral'><span class='material-symbols-outlined' aria-hidden='true'>add</span> Add bundle</button>
</form>
"""

    body = f"""
<div class="page-title">
<h1>GitLab</h1>
<p class="subtitle">View and manage the GitLab configuration and tracked projects this loop depends on.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SETTINGS}<h2>GitLab</h2></div>
<h3>Default instance</h3>
{default_form}
<h3>Instances</h3>
{instances_html}
{add_instance_form}
<h3>Project aliases</h3>
{projects_html}
{add_project_form}
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SETTINGS}<h2>Access bundles</h2></div>
<p class="subtitle">A project-specific GitLab token (and optional Slack webhook) for projects whose default instance token doesn't have full access.</p>
{bundles_html}
{add_bundle_form}
</section>
</div>

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SETTINGS}<h2>Tracked Projects</h2></div>
<p class="subtitle">This loop's own ~/.loop-engineering/projects.json - where each project lives locally, its target branch and install/lint/test commands, and (if it differs from the default instance above) which GitLab instance it's on. Different from "Project aliases" above, which is only about GitLab API auth routing.</p>
<h3>Loop settings</h3>
{loop_settings_form}
<h3>Projects</h3>
{tracked_projects_html}
{add_tracked_project_form}
</section>
</div>
"""
    return _render_shell("GitLab · Loop X Engineering", "settings", _status_badge_markup(status), body)


def render_slack_page(flash=None, flash_ok=True):
    """Notifications page, served at /notifications (function/module-level
    names below stay "slack" since Slack is still the only channel this
    loop can notify through - only the user-facing nav label and route
    are generic): view and manage ~/.slack/config.json's default webhook
    URL - split out from the GitLab page since that page's content is
    almost entirely GitLab configuration. Per-bundle Slack webhook
    overrides stay on the GitLab page's Access bundles section, since a
    bundle is fundamentally a GitLab-access construct that happens to
    carry an optional webhook override.

    Only reads (via read_slack_config) and masks the webhook
    (_mask_secret) - the real value is never sent to the browser. Writes
    go through DashboardHandler's /notifications/webhook POST route,
    which calls update_slack_webhook."""
    status = read_status(STATUS_PATH)
    slack_config = read_slack_config(SLACK_CONFIG_PATH)

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    webhook_url = slack_config.get("webhook_url", "")
    webhook_display = _mask_secret(webhook_url) if webhook_url else "(not set)"
    slack_form = f"""
<form method='post' action='/notifications/webhook' class='daemon-action-form single-field'>
{csrf_input}
<input type='password' name='webhook_url' placeholder='paste new Slack webhook URL' required>
<button type='submit' class='btn btn-neutral'>Save</button>
</form>
"""

    body = f"""
<div class="page-title">
<h1>Notifications</h1>
<p class="subtitle">View and manage where this loop sends run notifications. Slack is currently the only channel.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_SLACK}<h2>Slack</h2></div>
<p><strong>Default webhook:</strong> {webhook_display}</p>
{slack_form}
</section>
</div>
"""
    return _render_shell("Notifications · Loop X Engineering", "notifications", _status_badge_markup(status), body)


def render_ai_cli_page(flash=None, flash_ok=True):
    """AI CLI page, served at /ai-cli: choose which AI CLI tool
    (Claude Code or Codex CLI) this loop uses. Only reads
    (ai_cli_config.get_selected_cli) and writes go through
    DashboardHandler's /ai-cli POST route, which calls
    ai_cli_config.set_selected_cli.

    Both run-loop.sh and run-topic-monitor-loop.sh read this same
    global setting (python3 bin/ai_cli_config.py get) - there is no
    per-loop override. See
    docs/superpowers/specs/2026-08-28-ai-cli-switcher-design.md for why
    Codex's coarser --sandbox/--ask-for-approval flags make its
    guardrails prose-enforced rather than harness-enforced, unlike
    Claude's --allowedTools/--disallowedTools.

    Passes ai_cli_config.DEFAULT_CONFIG_PATH explicitly (rather than
    relying on get_selected_cli's own default) so a test's
    monkeypatch.setattr(ai_cli_config, "DEFAULT_CONFIG_PATH", ...) is
    actually honored - that module's default argument is bound once at
    its own def time, same trap this repo's CLAUDE.md already warns
    about for this file's own STATUS_PATH-style constants."""
    status = read_status(STATUS_PATH)
    current_cli = ai_cli_config.get_selected_cli(ai_cli_config.DEFAULT_CONFIG_PATH)

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    availability = {
        "claude": "installed" if _cli_available("claude") else "not found on PATH",
        "codex": "installed" if _cli_available("codex") else "not found on PATH",
    }

    cli_labels = {
        cli: f"{name} ({availability[cli]})" for cli, name in _AI_CLI_DISPLAY_NAMES.items()
    }
    select_html = _custom_select("cli", ai_cli_config.VALID_CLIS, current_cli)
    # _custom_select renders the raw option values ("claude"/"codex") as
    # their own labels; swap in the availability-annotated labels here
    # rather than complicating that shared helper for one caller. This
    # covers all three places that label appears: the hidden native
    # <option>, the custom dropdown's <div> menu item, and the closed
    # dropdown's own <span class='custom-select-value'> trigger text -
    # skipping the trigger span would leave the availability warning
    # invisible until the user actually opens the dropdown.
    for cli, label in cli_labels.items():
        select_html = select_html.replace(f">{cli}</option>", f">{label}</option>")
        select_html = select_html.replace(f">{cli}</div>", f">{label}</div>")
        select_html = select_html.replace(f">{cli}</span>", f">{label}</span>")

    cli_form = f"""
<form method='post' action='/ai-cli' class='daemon-action-form single-field'>
{csrf_input}
{select_html}
<button type='submit' class='btn btn-neutral'>Save</button>
</form>
"""

    body = f"""
<div class="page-title">
<h1>AI CLI</h1>
<p class="subtitle">Choose which AI CLI tool the GitLab issue loop and the Topic Monitor loop both use.</p>
</div>

{flash_html}

<div class="grid">
<section class="card">
<div class="section-header">{_SECTION_ICON_AI_CLI}<h2>Selected CLI</h2></div>
<p><strong>Currently:</strong> {html.escape(cli_labels[current_cli])}</p>
{cli_form}
</section>
</div>
"""
    return _render_shell("AI CLI · Loop X Engineering", "ai_cli", _status_badge_markup(status), body)


def _stat_tile_html(icon, label, value):
    return (
        "<div class='dash-stat-tile'>"
        f"<span class='material-symbols-outlined dash-stat-icon' aria-hidden='true'>{icon}</span>"
        f"<span class='dash-stat-value'>{value}</span>"
        f"<span class='dash-stat-label'>{html.escape(label)}</span>"
        "</div>"
    )


def _na_stat_tile_html(icon, label, reason):
    return (
        f"<div class='dash-stat-tile' title=\"{html.escape(reason)}\">"
        f"<span class='material-symbols-outlined dash-stat-icon' aria-hidden='true'>{icon}</span>"
        "<span class='dash-stat-value'>N/A</span>"
        f"<span class='dash-stat-label'>{html.escape(label)}</span>"
        "</div>"
    )


def _health_section_html(health_report):
    score = health_report["score"]
    score_text = f"{score:.0f}/100" if score is not None else "N/A"
    partial_note = ""
    if health_report["is_partial"]:
        missing = ", ".join(health_report["missing_components"])
        partial_note = (
            f"<p class='analytics-health-note' title=\"{html.escape(health_report['missing_reason'])}\">"
            f"Partial score — not yet tracked: {html.escape(missing)}</p>"
        )

    component_tiles = "".join(
        _stat_tile_html("check_circle", name.capitalize(), f"{value:.0f}" if value is not None else "N/A")
        for name, value in health_report["components"].items()
    )

    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_ANALYTICS}<h2>Loop Health</h2></div>
<p class="analytics-health-score">{score_text}</p>
{partial_note}
<div class="dash-stats-grid">{component_tiles}</div>
</section>
"""


def _outcomes_section_html(metrics_report):
    issue = metrics_report["issue"]
    qa = metrics_report["quality_and_autonomy"]
    autonomy_text = f"{qa['autonomy_rate'] * 100:.1f}%" if qa["autonomy_rate"] is not None else "N/A"

    tiles = "".join([
        _stat_tile_html("history", "Processed", issue["issues_processed"]),
        _stat_tile_html("check_circle", "Completed", issue["issues_completed"]),
        _stat_tile_html("warning", "Escalated", issue["issues_escalated"]),
        _stat_tile_html("error", "Failed", issue["issues_failed"]),
        _stat_tile_html("bolt", "Autonomy", autonomy_text),
    ])

    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_ACTIVITY}<h2>Outcomes</h2></div>
<div class="dash-stats-grid">{tiles}</div>
</section>
"""


def _quality_section_html(metrics_report):
    verification = metrics_report["verification"]
    qa = metrics_report["quality_and_autonomy"]
    verification_text = (
        f"{verification['verification_pass_rate'] * 100:.1f}%"
        if verification["verification_pass_rate"] is not None else "N/A"
    )

    tiles = "".join([
        _stat_tile_html("check_circle", "Verification", verification_text),
        _na_stat_tile_html("merge", "First-pass MR", "needs Phase 10 human-review data, not built yet"),
        _na_stat_tile_html("history", "Retry rate", qa["retry_rate_unavailable_reason"]),
        _na_stat_tile_html("error", "Regression", "not defined by any sprint built so far"),
    ])

    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_LOGS}<h2>Quality</h2></div>
<div class="dash-stats-grid">{tiles}</div>
</section>
"""


def _cost_section_html(cost_report):
    cost_metrics = cost_report["cost"]
    cost_per_issue = (
        f"${cost_metrics['cost_per_issue']:,.2f}" if cost_metrics["cost_per_issue"] is not None else "N/A"
    )
    cost_per_resolution = (
        f"${cost_metrics['cost_per_resolution']:,.2f}" if cost_metrics["cost_per_resolution"] is not None else "N/A"
    )

    tiles = "".join([
        _stat_tile_html("smart_toy", "AI cost", f"${cost_metrics['total_cost_usd']:,.2f}"),
        _stat_tile_html("smart_toy", "Cost / issue", cost_per_issue),
        _stat_tile_html("smart_toy", "Cost / resolution", cost_per_resolution),
    ])

    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_AI_CLI}<h2>Cost</h2></div>
<div class="dash-stats-grid">{tiles}</div>
</section>
"""


def _fmt_trend_value(value, unit):
    return f"${value:,.2f}" if unit == "$" else f"{value:.1f}%"


def _trend_line_chart_svg(label, points, unit="%", width=520, height=140):
    """One inline-SVG line chart for a single metric's trend - see the
    dataviz skill's guidance (a single series needs no legend box; the
    section header already names each chart). `points` is a list of
    (date_label, value_or_None) tuples, oldest first; value is already
    the 0-100 (or dollar) number to plot, never a raw 0-1 rate. A None
    value means that bucket had no data (e.g. zero processed issues that
    week) - it breaks the line at that point rather than plotting a false
    zero. Uses this app's own --md-primary/--md-outline-variant CSS
    tokens (valid inside an inline SVG's stroke/fill attributes in every
    browser this app targets) rather than a new hardcoded hex, so the
    chart stays in sync with the rest of the page's theme automatically."""
    values = [v for _, v in points if v is not None]
    if not values:
        return (
            f"<div class='trend-chart trend-chart-empty'>"
            f"<p class='trend-chart-title'>{html.escape(label)}</p>"
            f"<p>no data in this window</p></div>"
        )

    pad_left, pad_right, pad_top, pad_bottom = 8, 8, 12, 12
    plot_w = width - pad_left - pad_right
    plot_h = height - pad_top - pad_bottom

    v_min, v_max = min(values), max(values)
    if v_min == v_max:
        v_min, v_max = v_min - 1, v_max + 1  # avoid a zero-height range collapsing every point to one y

    n = len(points)

    def x_at(i):
        return pad_left + (plot_w * i / (n - 1) if n > 1 else plot_w / 2)

    def y_at(v):
        return pad_top + plot_h - ((v - v_min) / (v_max - v_min) * plot_h)

    segments = []
    current = []
    for i, (_, v) in enumerate(points):
        if v is None:
            if current:
                segments.append(current)
                current = []
            continue
        current.append((x_at(i), y_at(v)))
    if current:
        segments.append(current)

    polylines_html = "".join(
        "<polyline points='" + " ".join(f"{x:.1f},{y:.1f}" for x, y in seg) + "' "
        "fill='none' stroke='var(--md-primary)' stroke-width='2' "
        "stroke-linecap='round' stroke-linejoin='round' />"
        for seg in segments
    )

    dots_html = "".join(
        f"<circle cx='{x_at(i):.1f}' cy='{y_at(v):.1f}' r='3' fill='var(--md-primary)'>"
        f"<title>{html.escape(date_label)}: {_fmt_trend_value(v, unit)}</title></circle>"
        for i, (date_label, v) in enumerate(points) if v is not None
    )

    baseline_y = pad_top + plot_h
    axis_html = (
        f"<line x1='{pad_left}' y1='{baseline_y}' x2='{width - pad_right}' y2='{baseline_y}' "
        "stroke='var(--md-outline-variant)' stroke-width='1' />"
    )

    return (
        f"<div class='trend-chart'>"
        f"<p class='trend-chart-title'>{html.escape(label)}</p>"
        f"<svg viewBox='0 0 {width} {height}' width='100%' height='{height}' role='img' "
        f"aria-label='{html.escape(label)} trend'>{axis_html}{polylines_html}{dots_html}</svg>"
        f"</div>"
    )


def _pct_or_none(rate):
    return (rate * 100) if rate is not None else None


def _trend_bucket_label(scope):
    return (
        scope["since_date"] if scope["since_date"] == scope["until_date"]
        else f"{scope['since_date']}–{scope['until_date']}"
    )


def _trend_section_html(days):
    bucket_days = 1 if days <= 7 else 7
    metrics_reports = metrics.bucketed_reports(days=days, bucket_days=bucket_days)
    cost_reports = [
        cost.build_cost_report(since_date=r["scope"]["since_date"], until_date=r["scope"]["until_date"])
        for r in metrics_reports
    ]

    autonomy_points = [
        (_trend_bucket_label(r["scope"]), _pct_or_none(r["quality_and_autonomy"]["autonomy_rate"]))
        for r in metrics_reports
    ]
    resolution_points = [
        (_trend_bucket_label(r["scope"]), _pct_or_none(r["quality_and_autonomy"]["resolution_rate"]))
        for r in metrics_reports
    ]
    verification_points = [
        (_trend_bucket_label(r["scope"]), _pct_or_none(r["verification"]["verification_pass_rate"]))
        for r in metrics_reports
    ]
    cost_points = [
        (_trend_bucket_label(mr["scope"]), cr["cost"]["cost_per_resolution"])
        for mr, cr in zip(metrics_reports, cost_reports)
    ]

    charts_html = "".join([
        _trend_line_chart_svg("Autonomy rate", autonomy_points, unit="%"),
        _trend_line_chart_svg("Resolution rate", resolution_points, unit="%"),
        _trend_line_chart_svg("Verification pass rate", verification_points, unit="%"),
        _trend_line_chart_svg("Cost per resolution", cost_points, unit="$"),
        "<div class='trend-chart trend-chart-empty'>"
        "<p class='trend-chart-title'>MR acceptance</p>"
        "<p>Not yet tracked — needs Phase 10 human-review data.</p></div>",
    ])

    return f"""
<section class="card">
<div class="section-header">{_SECTION_ICON_ANALYTICS}<h2>Trend</h2></div>
<div class="trend-charts-grid">{charts_html}</div>
</section>
"""


def render_analytics_page(days=7):
    """The loop's performance-at-a-glance page - see
    docs/superpowers/specs/2026-09-05-analytics-dashboard-design.md.
    Reads bin/metrics.py's and bin/cost.py's own report dicts (no new
    event-reading logic here) for the selected `days` window, computes a
    partial Loop Health score via bin/health.py, and renders 4 sections
    (a 5th, Trend, is added by a later task): Loop Health, Outcomes,
    Quality, Cost. Every unavailable metric renders as "N/A" with its
    reason as a tooltip, exactly like bin/metrics.py's/bin/cost.py's own
    CLI output - this page adds no new judgment about what's available,
    it just presents what those modules already compute."""
    if days not in (7, 30, 90):
        days = 7

    until = datetime.now(timezone.utc).date()
    since = until - timedelta(days=days - 1)
    since_date, until_date = since.isoformat(), until.isoformat()

    metrics_report = metrics.build_report(since_date=since_date, until_date=until_date)
    cost_report = cost.build_cost_report(since_date=since_date, until_date=until_date)
    health_report = health.compute_health_score(metrics_report, cost_report)

    days_selector_html = "".join(
        f"<a href='/analytics?days={n}' class=\"{'active' if n == days else ''}\">{n}d</a>"
        for n in (7, 30, 90)
    )

    body = f"""
<div class="page-title">
<h1>Analytics</h1>
<p class="subtitle">How the loop is performing - no logs required.</p>
</div>

<div class="analytics-days-selector">{days_selector_html}</div>

{_health_section_html(health_report)}
{_outcomes_section_html(metrics_report)}
{_quality_section_html(metrics_report)}
{_cost_section_html(cost_report)}
{_trend_section_html(days)}
"""
    status = read_status(STATUS_PATH)
    return _render_shell("Analytics · Loop X Engineering", "analytics", _status_badge_markup(status), body)


def render_activity_page(flash=None, flash_ok=True):
    """The loop status page: this loop actually runs two independent
    daemons - the GitLab issue review loop and the topic monitor - so this
    page gives each its own compact status section (state, key fields, a
    Run now action), both stacked as always-visible cards (no tabs - see
    .activity-card-stack) so neither needs a click to check, plus each
    loop's own latest review report (GitLab's daily-review.md, and every
    topic's most recent saved briefing via _topic_latest_data_html),
    stacked the same way in the wide column. Only reads what this page
    needs - run history, live GitLab, memory, and daemons each have
    their own page/route now and fetch their own data.

    Each loop's Run now button is disabled - with a visible explanation,
    via _run_now_action_html's `disabled_hint_html` - when that loop has
    nothing configured to run (no tracked GitLab projects / no topics),
    rather than staying clickable and doing nothing useful.

    `flash`/`flash_ok` carry a POST-redirect-GET result from either loop's
    Run now button (/run-now or /topic-monitor/run-now), same convention as
    render_daemons_page."""
    status = read_status(STATUS_PATH)
    review = read_latest_review(LOOP_DIR)
    state = status.get("state", "unknown")

    flash_html = ""
    if flash:
        flash_class = "flash-success" if flash_ok else "flash-danger"
        flash_html = f"<div class='flash {flash_class}'>{html.escape(str(flash))}</div>"

    csrf_input = f"<input type='hidden' name='csrf_token' value=\"{html.escape(_CSRF_TOKEN)}\">"

    badge_class, badge_icon = _status_badge(state)
    state_hero_html = (
        f"<div class='status-hero'><span class='pill pill-lg {badge_class}'>"
        f"{badge_icon}{html.escape(_state_label(status))}</span></div>"
    )

    status_lines = []
    for key, value in status.items():
        if key == "state":
            continue
        label = key.replace("_", " ").capitalize()
        if key == "updated_at" and value:
            display_value = _relative_time(str(value))
            title_attr = f" title='{html.escape(str(value))}'"
        else:
            display_value = str(value)
            title_attr = ""
        status_lines.append(
            f"<li><span class='k'>{html.escape(label)}</span><span{title_attr}>{html.escape(display_value)}</span></li>"
        )

    log_html = ""
    if state == "running":
        tail = _today_log_tail()
        if tail:
            log_html = (
                "<p><strong>Today's log (tail):</strong></p>"
                f"<pre class='log'>{html.escape(tail)}</pre>"
            )

    has_projects = bool(read_loop_projects_config().get("projects"))
    if state == "running":
        gitlab_run_now_html = ""
    elif not has_projects:
        gitlab_run_now_html = _run_now_action_html(
            "/run-now", "", csrf_input,
            disabled_hint_html="No projects configured yet - <a href='/settings'>add one on the GitLab page</a>.",
        )
    else:
        gitlab_run_now_html = _run_now_action_html(
            "/run-now",
            "Run the GitLab loop now? This starts a real automated run outside its normal schedule.",
            csrf_input,
        )

    topics = get_configured_topics()
    topic_status = read_topic_status(TOPIC_MONITOR_STATUS_PATH)["topics"]
    any_topic_running = any(entry.get("state") == "running" for entry in topic_status.values())

    if any_topic_running:
        topic_badge_class, topic_badge_icon = _status_badge("running")
        topic_state_label = _topic_monitor_progress_text(topic_status, topics)
    elif not topics:
        topic_badge_class, topic_badge_icon = _status_badge("never_run")
        topic_state_label = "Not configured"
    else:
        topic_badge_class, topic_badge_icon = _status_badge("idle")
        topic_state_label = "Idle"
    topic_state_hero_html = (
        f"<div class='status-hero'><span class='pill pill-lg {topic_badge_class}'>"
        f"{topic_badge_icon}{html.escape(topic_state_label)}</span></div>"
    )

    topic_fields = [f"<li><span class='k'>Configured topics</span><span>{len(topics)}</span></li>"]
    last_run_values = [entry["updated_at"] for entry in topic_status.values() if entry.get("updated_at")]
    if last_run_values:
        most_recent = max(last_run_values)
        topic_fields.append(
            f"<li><span class='k'>Last run</span>"
            f"<span title='{html.escape(most_recent)}'>{html.escape(_relative_time(most_recent))}</span></li>"
        )

    if any_topic_running:
        topic_run_now_html = ""
    elif not topics:
        topic_run_now_html = _run_now_action_html(
            "/topic-monitor/run-now", "", csrf_input,
            disabled_hint_html=(
                "No topics configured yet - <a href='/topic-monitor/settings'>add one on the Topic Settings page</a>."
            ),
        )
    else:
        topic_run_now_html = _run_now_action_html(
            "/topic-monitor/run-now",
            "Run the topic monitor loop now? This starts a real automated run outside its normal schedule.",
            csrf_input,
        )

    body = f"""
<div class="page-title">
<h1>Activity</h1>
<p class="subtitle">What each automated loop is doing right now, plus the GitLab loop's most recent report.</p>
</div>

{flash_html}

<div class="overview-layout">
<div class="activity-card-stack">
<div class='card'>
<div class="section-header">{_SECTION_ICON_GITLAB}<h2>GitLab Monitor</h2></div>
{state_hero_html}
<ul class="field-list">{''.join(status_lines)}</ul>
{gitlab_run_now_html}
{log_html}
</div>
<div class='card'>
<div class="section-header">{_SECTION_ICON_TOPIC_MONITOR}<h2>Topic Monitor</h2></div>
{topic_state_hero_html}
<ul class="field-list">{''.join(topic_fields)}</ul>
{topic_run_now_html}
</div>
</div>

<div class="activity-card-stack">
<div class="card">
<div class="section-header">{_SECTION_ICON_OVERVIEW}<h2>Latest Run Review</h2></div>
<div class="markdown">{render_markdown(review)}</div>
</div>
<div class="card">
<div class="section-header">{_SECTION_ICON_TOPIC_MONITOR}<h2>Latest Topic Run Review</h2></div>
{_topic_latest_data_html(topics)}
</div>
</div>
</div>
"""
    return _render_shell(
        "Activity · Loop X Engineering", "activity", _status_badge_markup(status), body, refresh=True, refresh_note=True
    )


class DashboardHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        split = urllib.parse.urlsplit(self.path)

        if split.path == "/":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_overview_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/history":
            self._send_html(render_history_page())
            return

        if split.path == "/logs":
            self._send_html(render_logs_page())
            return

        if split.path == "/gitlab":
            self._send_html(render_gitlab_page())
            return

        if split.path == "/gitlab/live":
            self._send_html(render_gitlab_live_fragment())
            return

        if split.path == "/learnings":
            self.send_response(301)
            self.send_header("Location", "/memory")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return

        if split.path == "/memory":
            self._send_html(render_memory_page())
            return

        if split.path == "/topic-monitor":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_topic_monitor_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/topic-monitor/settings":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_topic_settings_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/daemons":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_daemons_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/readme":
            self._send_html(render_readme_page())
            return

        if split.path == "/preferences":
            self._send_html(render_preferences_page())
            return

        if split.path == "/instructions":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_instructions_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/skills":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_skills_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/settings":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_settings_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/notifications":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_slack_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/ai-cli":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_ai_cli_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/analytics":
            query = urllib.parse.parse_qs(split.query)
            days_raw = query.get("days", ["7"])[0]
            try:
                days = int(days_raw)
            except ValueError:
                days = 7
            self._send_html(render_analytics_page(days=days))
            return

        if split.path == "/activity":
            query = urllib.parse.parse_qs(split.query)
            flash = query.get("flash", [None])[0]
            flash_ok = query.get("ok", ["1"])[0] != "0"
            self._send_html(render_activity_page(flash=flash, flash_ok=flash_ok))
            return

        if split.path == "/activity/chat-stream":
            query = urllib.parse.parse_qs(split.query)
            reply_key = query.get("reply_key", [""])[0]
            self._stream_chat_reply(reply_key)
            return

        if split.path == "/favicon.ico":
            self._send_favicon()
            return

        if split.path.startswith("/history/"):
            name = split.path[len("/history/"):]
            content = read_history_file(name, HISTORY_DIR)
            if content is None:
                self._not_found()
                return
            title = Path(name).name
            body = (
                f"<h1>{html.escape(title)}</h1>"
                "<div class='grid'><div class='card'>"
                f"<div class='markdown'>{render_markdown(content)}</div>"
                "</div></div>"
            )
            status = read_status(STATUS_PATH)
            self._send_html(_render_shell(
                title, "history", _status_badge_markup(status), body, refresh=False, refresh_note=False
            ))
            return

        if split.path.startswith("/topic-monitor/history/"):
            name = split.path[len("/topic-monitor/history/"):]
            content = read_history_file(name, TOPIC_MONITOR_HISTORY_DIR)
            if content is None:
                self._not_found()
                return
            title = Path(name).name
            body = (
                f"<h1>{html.escape(title)}</h1>"
                "<div class='grid'><div class='card'>"
                f"<div class='markdown'>{render_markdown(content)}</div>"
                "</div></div>"
            )
            status = read_status(STATUS_PATH)
            self._send_html(_render_shell(
                title, "topic_monitor", _status_badge_markup(status), body, refresh=False, refresh_note=False
            ))
            return

        self._not_found()

    def _send_html(self, body):
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        # Every page here reflects live state (run status, sidebar collapse
        # markup, whatever CSS/JS shipped in this process's own _STYLE/
        # _render_shell) - no-store rules out a browser ever showing a
        # stale copy after a restart or a code change, on this page or the
        # auto-refresh (<meta http-equiv="refresh">) that reloads it every
        # 30s unattended.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _send_json(self, status, obj):
        encoded = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(encoded)

    def _stream_chat_reply(self, reply_key):
        """Serves GET /activity/chat-stream?reply_key=<uuid> as Server-
        Sent Events. reply_key names a job already started by the
        earlier CSRF-checked POST /activity/chat, so this route performs
        no new state-changing action itself and needs no CSRF check of
        its own. X-Accel-Buffering: no defeats nginx's default response
        buffering when this dashboard is reached through
        bin/scripts/setup-nginx.sh's proxy (which has no proxy_buffering
        off of its own) - without it, a reply viewed through
        http://loop.local/ would arrive in one late burst instead of
        streaming, even though it streams correctly hitting
        127.0.0.1:8420 directly."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        # Deliberately no "Connection: keep-alive" header here: BaseHTTP-
        # RequestHandler.send_header special-cases that exact value and
        # sets self.close_connection = False as a side effect, which makes
        # handle() loop waiting to read ANOTHER request off this same
        # socket once this one's body finishes - since nothing else ever
        # arrives, the client (and a proxying nginx) just hangs waiting
        # for EOF that never comes instead of seeing the stream end.
        # protocol_version is "HTTP/1.0" for this handler, so leaving
        # close_connection at its default (True) is exactly what makes the
        # socket actually close once this method returns - which is what
        # tells the client the SSE response has ended. Verified live with
        # curl and a raw socket read: adding the header back reproduces a
        # hang past a 5s deadline; without it, the connection closes and
        # the full framed body arrives immediately.
        self.end_headers()
        found_anything = False
        try:
            for event in _iter_chat_job_chunks(reply_key):
                found_anything = True
                kind = event[0]
                if kind == "chunk":
                    self.wfile.write(_sse_frame("chunk", event[1]))
                elif kind == "idle":
                    # Not a real SSE event - a bare comment line (data-less,
                    # per the SSE spec) so an idle stream doesn't look dead
                    # to a proxy sitting in front of this dashboard (see
                    # bin/scripts/setup-nginx.sh's default
                    # proxy_read_timeout) while the subprocess is still
                    # thinking. Never parsed by the browser's EventSource
                    # as a named event, by design.
                    self.wfile.write(b": keepalive\n\n")
                elif kind == "done":
                    error, final_text = event[1], event[2]
                    if error:
                        self.wfile.write(_sse_frame("error", error))
                    else:
                        # The authoritative, already-persisted reply text
                        # (see _chat_job_finish/append_message) - not
                        # whatever the streamed text_delta chunks happened
                        # to accumulate to - so the frontend can make the
                        # bubble's final text match exactly what a page
                        # reload would show (see the chat script's "done"
                        # handler in _render_shell).
                        self.wfile.write(_sse_frame("done", final_text or ""))
                self.wfile.flush()
            if not found_anything:
                self.wfile.write(_sse_frame("error", "Unknown or expired reply_key"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return

    def _send_favicon(self):
        # FAVICON_PATH is a fixed, module-level constant (never derived from
        # the request), so unlike /history/<name> there's no path-traversal
        # concern here - just a plain "does the file exist" check.
        try:
            data = FAVICON_PATH.read_bytes()
        except OSError:
            self._not_found()
            return
        self.send_response(200)
        self.send_header("Content-Type", "image/x-icon")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        # The only state-changing routes this server exposes. Being POST-only
        # is NOT by itself a CSRF defense: a cross-origin
        # <form method="POST"> submission needs no JavaScript and triggers no
        # CORS preflight for an application/x-www-form-urlencoded body, so any
        # page open in another tab could otherwise trigger these. The real
        # defense is the per-process _CSRF_TOKEN, which only ever appears in
        # pages this server renders and which a cross-origin page cannot read.
        # Anything that doesn't match one of these two exact shapes falls
        # through to the same 404 do_GET uses - no other routes exist here.
        #
        # The body is read unconditionally and up front: a request body left
        # unread would desync a keep-alive connection for the next request.
        try:
            content_length = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            content_length = 0
        body = self.rfile.read(content_length) if content_length else b""

        if self.path == "/run-now":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            ok, message = trigger_manual_run()
            self._redirect_with_flash(ok, message, location="/")
            return

        if self.path.startswith("/history/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            name = urllib.parse.unquote(self.path[len("/history/"):-len("/delete")])
            ok, message = delete_history_file(name, HISTORY_DIR)
            self._redirect_with_flash(ok, message, location="/history")
            return

        if self.path.startswith("/topic-monitor/history/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            name = urllib.parse.unquote(self.path[len("/topic-monitor/history/"):-len("/delete")])
            ok, message = delete_history_file(name, TOPIC_MONITOR_HISTORY_DIR)
            self._redirect_with_flash(ok, message, location="/history")
            return

        if self.path == "/topic-monitor/run-now":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            ok, message = trigger_topic_monitor_run()
            self._redirect_with_flash(ok, message, location="/topic-monitor")
            return

        if self.path == "/topic-monitor/topics":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            name = form.get("name", [""])[0]
            label = form.get("label", [""])[0]
            brief = form.get("brief", [""])[0]
            slack_bundle = form.get("slack_bundle", [""])[0]
            ok, message = topic_config.upsert_topic(name, label, brief, slack_bundle, topic_config.DEFAULT_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/topic-monitor/settings")
            return

        if self.path.startswith("/topic-monitor/topics/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            name = urllib.parse.unquote(self.path[len("/topic-monitor/topics/"):-len("/delete")])
            ok, message = topic_config.delete_topic(name, topic_config.DEFAULT_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/topic-monitor/settings")
            return

        if self.path == "/skills/install":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            ok, message = trigger_skills_install()
            self._redirect_with_flash(ok, message, location="/skills")
            return

        if self.path.startswith("/daemons/") and self.path.endswith("/enable"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            filename = self.path[len("/daemons/"):-len("/enable")]
            # Pass the current module-level LAUNCHD_DIR explicitly. A bare
            # global name referenced inside a function body is resolved
            # against the module's __dict__ at call time, whereas
            # enable_daemon's own `launchd_dir=LAUNCHD_DIR` default was bound
            # once at def-time - so relying on that default would silently
            # ignore a test's monkeypatch of ds.LAUNCHD_DIR and let a "unit
            # test" reach the real repo and the real ~/Library/LaunchAgents.
            ok, message = enable_daemon(filename, LAUNCHD_DIR)
            self._redirect_with_flash(ok, message)
            return

        if self.path.startswith("/daemons/") and self.path.endswith("/disable"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            filename = self.path[len("/daemons/"):-len("/disable")]
            ok, message = disable_daemon(filename, LAUNCHD_DIR)
            self._redirect_with_flash(ok, message)
            return

        if self.path.startswith("/daemons/") and self.path.endswith("/schedule"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            filename = self.path[len("/daemons/"):-len("/schedule")]
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            time_value = form.get("time", [""])[0]
            frequency = form.get("frequency", ["Daily"])[0]
            try:
                hour_str, minute_str = time_value.split(":")
                hour, minute = int(hour_str), int(minute_str)
                if frequency == "Monthly":
                    weekdays, day_of_month = [], int(form.get("day_of_month", [""])[0])
                else:
                    weekdays, day_of_month = [int(v) for v in form.get("weekday", [])], None
            except ValueError:
                ok, message = False, f"Invalid time, weekday, or day-of-month value: {time_value!r}"
            else:
                ok, message = update_daemon_schedule(filename, hour, minute, weekdays, day_of_month, LAUNCHD_DIR)
            self._redirect_with_flash(ok, message)
            return

        if self.path == "/settings/gitlab/default":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            instance = form.get("instance", [""])[0]
            # Passed explicitly for clarity, though set_default_gitlab_instance's
            # own config_path=None default now resolves the current module-level
            # GITLAB_CONFIG_PATH at call time (see the None-sentinel pattern used
            # throughout this file's config helpers), so this is no longer load-bearing.
            ok, message = set_default_gitlab_instance(instance, GITLAB_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/settings/gitlab/instances":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            alias = form.get("alias", [""])[0]
            url = form.get("url", [""])[0]
            token = form.get("token", [""])[0]
            ok, message = upsert_gitlab_instance(alias, url, token, GITLAB_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path.startswith("/settings/gitlab/instances/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            alias = urllib.parse.unquote(self.path[len("/settings/gitlab/instances/"):-len("/delete")])
            ok, message = delete_gitlab_instance(alias, GITLAB_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/settings/gitlab/projects":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            alias = form.get("alias", [""])[0]
            project_id = form.get("project_id", [""])[0]
            instance = form.get("instance", [""])[0]
            bundle = form.get("bundle", [""])[0]
            ok, message = upsert_gitlab_project(alias, project_id, instance, bundle, GITLAB_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path.startswith("/settings/gitlab/projects/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            alias = urllib.parse.unquote(self.path[len("/settings/gitlab/projects/"):-len("/delete")])
            ok, message = delete_gitlab_project(alias, GITLAB_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/settings/access-bundles":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            name = form.get("name", [""])[0]
            instance = form.get("instance", [""])[0]
            token = form.get("token", [""])[0]
            webhook_url = form.get("webhook_url", [""])[0]
            ok, message = upsert_access_bundle(name, instance, token, webhook_url, GITLAB_CONFIG_PATH, SLACK_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path.startswith("/settings/access-bundles/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            name = urllib.parse.unquote(self.path[len("/settings/access-bundles/"):-len("/delete")])
            ok, message = delete_access_bundle(name, GITLAB_CONFIG_PATH, SLACK_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path.startswith("/settings/access-bundles/") and self.path.endswith("/clear-webhook"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            name = urllib.parse.unquote(self.path[len("/settings/access-bundles/"):-len("/clear-webhook")])
            ok, message = clear_bundle_webhook(name, SLACK_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/notifications/webhook":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            webhook_url = form.get("webhook_url", [""])[0]
            ok, message = update_slack_webhook(webhook_url, SLACK_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/notifications")
            return

        if self.path == "/ai-cli":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            cli = form.get("cli", [""])[0]
            ok, message = ai_cli_config.set_selected_cli(cli, ai_cli_config.DEFAULT_CONFIG_PATH)
            self._redirect_with_flash(ok, message, location="/ai-cli")
            return

        if self.path == "/settings/loop-config":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            assignee_username = form.get("assignee_username", [""])[0]
            worktree_root = form.get("worktree_root", [""])[0]
            gitlab_instance = form.get("gitlab_instance", [""])[0]
            ok, message = update_loop_project_settings(assignee_username, worktree_root, gitlab_instance)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/settings/loop-projects":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            alias = form.get("alias", [""])[0]
            original_alias = form.get("original_alias", [""])[0]
            project_id = form.get("project_id", [""])[0]
            local_path = form.get("local_path", [""])[0]
            target_branch = form.get("target_branch", [""])[0]
            install_cmd = form.get("install_cmd", [""])[0]
            lint_cmd = form.get("lint_cmd", [""])[0]
            test_cmd = form.get("test_cmd", [""])[0]
            instance = form.get("instance", [""])[0]
            ok, message = upsert_tracked_project(
                alias, project_id, local_path, target_branch, install_cmd, lint_cmd, test_cmd, instance,
                original_alias=original_alias,
            )
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path.startswith("/settings/loop-projects/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            alias = urllib.parse.unquote(self.path[len("/settings/loop-projects/"):-len("/delete")])
            ok, message = delete_tracked_project(alias)
            self._redirect_with_flash(ok, message, location="/settings")
            return

        if self.path == "/instructions":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            instructions_text = form.get("instructions", [""])[0]
            ok, message = write_custom_instructions(instructions_text)
            self._redirect_with_flash(ok, message, location="/instructions")
            return

        if self.path == "/activity/messages":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            text = form.get("text", [""])[0]
            ok, message = send_user_message(text, MESSAGES_PATH)
            self._redirect_with_flash(ok, message, location="/")
            return

        if self.path.startswith("/activity/messages/") and self.path.endswith("/delete"):
            if not self._csrf_ok(body):
                self._forbidden()
                return
            timestamp = urllib.parse.unquote(self.path[len("/activity/messages/"):-len("/delete")])
            ok, message = delete_message(timestamp, MESSAGES_PATH)
            self._redirect_with_flash(ok, message, location="/")
            return

        if self.path == "/activity/chat":
            if not self._csrf_ok(body):
                self._forbidden()
                return
            form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
            text = form.get("text", [""])[0]
            recent = read_messages(MESSAGES_PATH)[-_CHAT_MESSAGE_HISTORY_LIMIT:]
            ok, message = send_user_message(text, MESSAGES_PATH)
            if not ok:
                self._send_json(400, {"error": message})
                return
            # Logged as its own "question" entry, distinct from _run_chat_job's
            # own "turn started"/"reply"/"error" entries for the same turn, so
            # the Logs page shows what was actually asked - written right here
            # (synchronously, before the background thread even exists) so a
            # question is always logged even if thread.start() below fails.
            append_unified_log("chat-assistant", "question", body=text.strip())
            reply_key = _chat_job_create()
            prompt = build_chat_prompt(text.strip(), recent)
            thread = threading.Thread(target=_run_chat_job, args=(reply_key, prompt), daemon=True)
            try:
                thread.start()
            except RuntimeError as exc:
                # Extremely rare (e.g. resource exhaustion), but if the
                # thread never actually starts running _run_chat_job at
                # all, the job would otherwise sit in the registry forever
                # - never finished, no 60s cleanup timer ever scheduled,
                # and any SSE client blocking on it (see
                # _iter_chat_job_chunks) would hang indefinitely. Finishing
                # it here guarantees the same "always eventually done"
                # contract _run_chat_job itself upholds.
                _chat_job_finish(reply_key, error=f"Could not start assistant thread: {exc}")
            self._send_json(200, {"reply_key": reply_key})
            return

        self._not_found()

    @staticmethod
    def _csrf_ok(body):
        """True only if the URL-encoded request body carries a csrf_token
        field matching this process's _CSRF_TOKEN. compare_digest is a
        timing-safe comparison, so a wrong token leaks nothing about how much
        of a guess was correct. A missing or empty token is always a
        mismatch."""
        form = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"))
        submitted = form.get("csrf_token", [""])[0]
        if not submitted:
            return False
        # compare_digest raises TypeError on a non-ASCII str (it only supports
        # ASCII str or bytes) - the token itself is always ASCII, but a hostile
        # request body isn't, so compare as bytes to avoid a crash on that path.
        return secrets.compare_digest(submitted.encode("utf-8", "replace"), _CSRF_TOKEN.encode("ascii"))

    def _forbidden(self, message=b"Forbidden: missing or invalid CSRF token"):
        self.send_response(403)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(message)))
        self.end_headers()
        self.wfile.write(message)

    def _redirect_with_flash(self, ok, message, location="/daemons"):
        """Standard POST-redirect-GET: 303 back to `location` with the
        result carried as query params, so a refresh of the resulting page
        doesn't resubmit the action. Every render_*_page()/do_GET route that
        reads `flash`/`ok` html.escape()s the flash text before display,
        since it can contain untrusted text (launchctl's stderr, or a
        rejected-write error message)."""
        query = urllib.parse.urlencode({"flash": message, "ok": "1" if ok else "0"}, quote_via=urllib.parse.quote)
        self.send_response(303)
        self.send_header("Location", f"{location}?{query}")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _not_found(self):
        body = b"Not found"
        self.send_response(404)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format, *args):
        # Suppress default request logging: this runs under launchd with its
        # own log files, per-request noise isn't useful.
        pass


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    daemon_threads = True


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "write-status":
        if len(sys.argv) < 3:
            print(
                "Usage: dashboard_server.py write-status <state> [--exit-code N] "
                "[--current-issue TEXT] [--current-step TEXT]",
                file=sys.stderr,
            )
            sys.exit(1)
        state = sys.argv[2]
        extra = {}
        if "--exit-code" in sys.argv:
            idx = sys.argv.index("--exit-code")
            extra["last_exit_code"] = int(sys.argv[idx + 1])
        if "--current-issue" in sys.argv:
            idx = sys.argv.index("--current-issue")
            extra["current_issue"] = sys.argv[idx + 1]
        if "--current-step" in sys.argv:
            idx = sys.argv.index("--current-step")
            extra["current_step"] = sys.argv[idx + 1]
        write_status(state, STATUS_PATH, **extra)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "write-topic-status":
        if len(sys.argv) < 4:
            print(
                "Usage: dashboard_server.py write-topic-status <topic_name> <state> [--current-step TEXT]",
                file=sys.stderr,
            )
            sys.exit(1)
        topic_name, state = sys.argv[2], sys.argv[3]
        extra = {}
        if "--current-step" in sys.argv:
            idx = sys.argv.index("--current-step")
            extra["current_step"] = sys.argv[idx + 1]
        write_topic_status(topic_name, state, TOPIC_MONITOR_STATUS_PATH, **extra)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "write-skills-install-status":
        if len(sys.argv) < 3:
            print(
                "Usage: dashboard_server.py write-skills-install-status <state> [--status-path PATH]",
                file=sys.stderr,
            )
            sys.exit(1)
        state = sys.argv[2]
        status_path = SKILLS_INSTALL_STATUS_PATH
        if "--status-path" in sys.argv:
            idx = sys.argv.index("--status-path")
            status_path = Path(sys.argv[idx + 1])
        write_status(state, status_path=status_path)
        return

    if len(sys.argv) > 1 and sys.argv[1] == "read-messages":
        print(json.dumps(pop_unseen_user_messages(), indent=2))
        return

    if len(sys.argv) > 1 and sys.argv[1] == "add-message":
        if len(sys.argv) < 4:
            print("Usage: dashboard_server.py add-message <from> <text>", file=sys.stderr)
            sys.exit(1)
        if sys.argv[2] not in ("user", "loop"):
            print("Usage: dashboard_server.py add-message <user|loop> <text>", file=sys.stderr)
            sys.exit(1)
        append_message(sys.argv[2], sys.argv[3])
        return

    if len(sys.argv) > 1 and sys.argv[1] == "chat-tool":
        if len(sys.argv) < 3:
            print("Usage: dashboard_server.py chat-tool <action> [args]", file=sys.stderr)
            sys.exit(1)
        _dispatch_chat_tool(sys.argv[2], sys.argv[3:])
        return

    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    server = ThreadingHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"Dashboard serving at http://127.0.0.1:{port}/")
    server.serve_forever()


if __name__ == "__main__":
    main()
