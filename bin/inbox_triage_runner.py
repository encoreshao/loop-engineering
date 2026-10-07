#!/usr/bin/env python3
"""Runs the Inbox Triage loop - see
docs/superpowers/specs/2026-09-27-inbox-triage-design.md and
docs/tasks/inbox-triage-loop.md. Python owns every mail call; the AI gets
one tool-less, MCP-less call per inbox and returns JSON decisions.
Mirrors bin/topic_monitor_runner.py: one LoopRuntime iteration per inbox,
failures contained per inbox, exit 0 even when an inbox fails.

Claude-only: `claude -p --tools ""` gives the model no tools at all, but
`codex exec` always gives it a shell (even --sandbox read-only can read
files and run commands), and it has no switch to stop recording the prompt
- i.e. the email bodies - under ~/.codex/sessions/. So when the selected
AI CLI is codex, every inbox fails up front with CODEX_REFUSAL, before any
mail is fetched, labelled or recorded, and no AI is invoked."""
import functools
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ai_cli_config
import cost as cost_module
import inbox_config
import inbox_seen
import inbox_status
import inbox_triage
import mail_auth
import mail_http
import mail_providers
import slack_notify
from agents import sealed
from loopkit import exclusive_run_lock as _exclusive_run_lock
from loopkit import raise_on_sigterm as _raise_on_sigterm
from loop_definition import LoopDefinition
from loop_runtime import LoopRuntime
from loop_serialize import write_result
from loop_verifiers import build_verifiers

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "outputs" / "inbox-triage"
DEFAULT_DEFINITION_PATH = REPO_ROOT / "loops" / "inbox-triage" / "loop.yaml"
INSTRUCTIONS_PATH = REPO_ROOT / "INBOX_TRIAGE_INSTRUCTIONS.md"
MESSAGE_CAP = 50


CODEX_REFUSAL = ("Inbox Triage requires the Claude CLI (codex gives the model a shell) - "
                 "switch AI CLI to Claude in Settings")


class TriageFailed(Exception):
    pass


def _cli_command(max_budget_usd=None):
    """Kept so existing callers/tests keep working: see agents.sealed.sealed_command."""
    return sealed.sealed_command(max_budget_usd=max_budget_usd)


def _append_unified_log(text, repo_root, unified_log_path):
    if unified_log_path is None:
        unified_log_path = Path(repo_root) / "logs" / "loop-engineering.log"
    path = Path(unified_log_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] inbox-triage: {text}\n")


def _default_timeout_seconds(definition_path=None):
    """Per-inbox AI subprocess bound, from loop.yaml's
    stop_conditions.max_runtime_minutes (the spec's per-inbox budget)."""
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    return LoopDefinition.from_yaml(definition_path).stop_conditions.max_runtime_minutes * 60


def invoke_triage_agent(prompt, repo_root=None, timeout_seconds=None, unified_log_path=None,
                        max_budget_usd=None):
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
    if timeout_seconds is None:
        timeout_seconds = _default_timeout_seconds()
    def log(msg):
        _append_unified_log(f"claude triage call {msg}", repo_root, unified_log_path)

    try:
        out = sealed.sealed_call(prompt, timeout_seconds, log=log,
                                 runner=lambda *a, **kw: subprocess.run(*a, **kw),
                                 cli_fn=lambda: ai_cli_config.get_selected_cli(),
                                 command_fn=lambda max_budget_usd=None: _cli_command(max_budget_usd),
                                 max_budget_usd=max_budget_usd)
    except sealed.SealedCallFailed as exc:
        if isinstance(exc.__cause__, (subprocess.TimeoutExpired, subprocess.CalledProcessError)):
            raise exc.__cause__ from None
        if ai_cli_config.get_selected_cli() != "claude":
            raise TriageFailed(CODEX_REFUSAL) from None
        raise TriageFailed(str(exc)) from None
    return {"text": out["text"], "cost_usd": out["cost_usd"]}


def _default_max_cost_usd(definition_path=None):
    """Per-inbox spend cap, from loop.yaml's stop_conditions.max_cost_usd."""
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    return LoopDefinition.from_yaml(definition_path).stop_conditions.max_cost_usd


def classify(prompt, messages, categories, invoke=None, timeout_seconds=None, max_cost_usd=None):
    """Up to two AI attempts; each is capped at what is left of the
    inbox's max_cost_usd (an attempt with unknown cost is booked at its cap)."""
    if invoke is None:
        invoke = functools.partial(invoke_triage_agent, timeout_seconds=timeout_seconds)
    if max_cost_usd is None:
        max_cost_usd = _default_max_cost_usd()
    total_cost, last_error, spent = None, None, 0.0
    for _attempt in range(2):
        cap = cost_module.remaining_budget(max_cost_usd, spent)
        try:
            out = invoke(prompt, max_budget_usd=cap)
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired, TriageFailed) as exc:
            last_error = f"AI call failed: {type(exc).__name__}"
            spent += cap or 0
            continue
        spent += out["cost_usd"] if out.get("cost_usd") is not None else (cap or 0)
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
                 state_dir=None, instructions=None, timeout_seconds=None):
    if provider_factory is None:
        provider_factory = mail_providers.get_provider
    if token_fn is None:
        token_fn = mail_auth.get_access_token
    # Before any token/provider call, so under codex no mail is ever read.
    if ai_cli_config.get_selected_cli() != "claude":
        return _outcome(inbox, "failed", CODEX_REFUSAL)
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
        decisions, cost = classify(prompt, messages, categories, invoke=invoke, timeout_seconds=timeout_seconds)
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
            # Any exception, not just the HTTP/auth ones: labels and seen IDs
            # are already recorded, so an escape here would throw away the
            # urgent list and never retry the remaining drafts. EmailMessage
            # raises ValueError for a header with a linefeed, and a lone
            # surrogate in draft_body raises UnicodeEncodeError. Only the
            # class name is logged - the message text can quote mail content.
            try:
                link = provider.create_reply_draft(message, decision["draft_body"])
            except Exception as exc:  # noqa: BLE001 - see above
                draft_failed = True
                print(f"inbox_triage_runner: draft for one message in {inbox['name']} failed: "
                      f"{type(exc).__name__}", file=sys.stderr)
        rows.append({"date": message["date"], "from": message["from"], "subject": message["subject"],
                     "category": decision["category"], "reason": decision["reason"], "draft_link": link})
        if decision["category"] == "urgent":
            urgent.append({"from": message["from"], "subject": message["subject"], "draft_link": link,
                           "needs_manual_reply": decision["needs_manual_reply"], "draft_failed": draft_failed})
    return _outcome(inbox, "ok", counts=inbox_triage.count_by_category(decisions), urgent=urgent,
                    rows=rows, overflow=overflow, cost_usd=cost)


DEFAULT_HISTORY_DIR = OUTPUT_DIR / "history"
DEFAULT_LOCK_PATH = OUTPUT_DIR / "run.lock"
_STATUS_STATE = {"ok": "idle", "quiet": "idle", "failed": "failed", "needs_reauth": "needs_reauth"}


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x1f\x7f]")
_MD_SPECIAL_RE = re.compile(r"([\\`*_\[\]!|#<>])")
_MD_BULLET_RE = re.compile(r"^(\s*)([-+])(?=\s)")
_MD_ORDERED_RE = re.compile(r"^(\s*\d+)\.")


def _md(text):
    """Untrusted text (sender, subject, AI reason, error) -> markdown that
    dashboard_server.render_markdown shows literally: every metacharacter
    it honours is backslash-escaped (render_markdown's backslash escapes
    stash the character as plain text, so `![p](url)` can't become a
    tracking-pixel <img>, `[text](url)` a spoofed link, `a|b` an extra
    table cell), `://` is broken up so a bare URL isn't auto-linked, a
    leading list marker can't start a list, and newlines/control chars
    (including the renderer's own \\x00 stash marker) become spaces.
    Links the loop writes itself (draft links) are built outside this."""
    text = _CONTROL_CHARS_RE.sub(" ", text or "")
    text = _MD_SPECIAL_RE.sub(r"\\\1", text).replace("://", "\\://")
    # "- x" / "+ x" -> backslash before the marker; "12. x" -> backslash before the dot.
    text = _MD_BULLET_RE.sub(r"\1\\\2", text)
    return _MD_ORDERED_RE.sub(r"\1\\.", text)


def _slack_escape(text):
    """Untrusted text -> Slack mrkdwn that can't ping (`<!channel>`) or
    spoof a link (`<https://evil|Open draft>`): Slack's own required
    escaping of & < > (its control characters)."""
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def write_history(outcome, now, history_dir=None):
    if history_dir is None:
        history_dir = DEFAULT_HISTORY_DIR
    path = Path(history_dir) / f"{now.date().isoformat()}-{outcome['name']}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if not path.exists():
        lines.append(f"# {outcome['label']} - {now.date().isoformat()}\n")
    lines.append(f"\n## Run {now.astimezone().strftime('%H:%M')} - {outcome['status']}\n")
    if outcome.get("error"):
        lines.append(f"\n{_md(outcome['error'])}\n")
    if outcome["rows"]:
        lines.append("\n| Received | From | Subject | Category | Reason | Draft |\n|---|---|---|---|---|---|\n")
        for row in outcome["rows"]:
            draft = f"[draft]({row['draft_link']})" if row["draft_link"] else ""
            lines.append(f"| {row['date'][11:16]} | {_md(row['from'])} | {_md(row['subject'])} | "
                         f"{row['category']} | {_md(row['reason'])} | {draft} |\n")
    elif outcome["status"] == "quiet":
        lines.append("\nNo new mail.\n")
    with open(path, "a") as f:
        f.write("".join(lines))
    return path


def _summary_line(outcome):
    label = _slack_escape(outcome["label"])
    if outcome["status"] == "quiet":
        return f"*{label}*: no new mail"
    if outcome["status"] == "needs_reauth":
        return f"*{label}*: needs re-auth - reconnect it from the dashboard's Inbox Triage page"
    if outcome["status"] == "failed":
        return f"*{label}*: failed - {_slack_escape(outcome['error'])}"
    counts = dict(outcome["counts"])
    urgent, action = counts.pop("urgent", 0), counts.pop("action", 0)
    other = sum(counts.values())
    ready = " (drafts ready)" if any(u["draft_link"] for u in outcome["urgent"]) else ""
    return f"*{label}*: {urgent} urgent{ready}, {action} action, {other} other"


def format_digest(outcomes, now):
    lines = [f"*Inbox Triage* - {now.astimezone().strftime('%Y-%m-%d %H:%M')}"]
    for outcome in outcomes:
        lines.append(_summary_line(outcome))
        for item in outcome["urgent"]:
            if item["draft_link"]:
                note = f"<{item['draft_link']}|draft>"
            elif item["draft_failed"]:
                note = "draft failed - reply manually"
            else:
                note = "reply manually"
            lines.append(f"    • {_slack_escape(item['from'])} - \"{_slack_escape(item['subject'])}\" ({note})")
        if outcome.get("overflow"):
            lines.append(f"    more waiting - over {MESSAGE_CAP} new messages, the rest are picked up next run")
    return "\n".join(lines)


def send_digests(outcomes, now, post=None):
    if post is None:
        post = slack_notify.post_message
    groups = {}
    for outcome in outcomes:
        groups.setdefault(outcome.get("slack_bundle"), []).append(outcome)
    for bundle, group in groups.items():
        text = format_digest(group, now)
        blocks = slack_notify.resolve_blocks(notification_key="inbox_triage_digest", message=text)
        # Best-effort, and a rejected Block Kit template must not lose the
        # digest: retry once as plain text - same as
        # topic_monitor_runner._notify_slack_best_effort.
        try:
            post(text, bundle=bundle, blocks=blocks)
        except Exception as exc:  # noqa: BLE001 - a digest failing must not fail the run
            if not blocks:
                print(f"inbox_triage_runner: Slack digest failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                continue
            try:
                post(text, bundle=bundle, blocks=None)
            except Exception as retry_exc:  # noqa: BLE001 - same reasoning
                print(f"inbox_triage_runner: Slack digest failed even without blocks: "
                      f"{type(retry_exc).__name__}: {retry_exc}", file=sys.stderr)


def _mark_inbox_failed(name, reason, status_path=None):
    """Write this inbox's terminal `failed` state ourselves when the normal
    end-of-inbox write never happened - mirrors
    topic_monitor_runner._mark_topic_failed. trigger_inbox_triage_run
    refuses a new run while an inbox reads "running", so an inbox left at
    "running" (a raise in write_result/write_history, a PolicyViolationError
    from LoopRuntime.start, SIGTERM from run-loop-now.sh's `timeout`) would
    otherwise latch there. Best-effort: failing to record a failure must
    not take down the rest of the run."""
    try:
        inbox_status.write(name, "failed", status_path=status_path, error=reason)
        return True
    except Exception as exc:  # noqa: BLE001 - see docstring
        print(f"inbox_triage_runner: writing failed status for {name} failed: {type(exc).__name__}", file=sys.stderr)
        return False


def _run_one_inbox(inbox, config, now, run_id, definition, triage, results_dir, events_dir, history_dir):
    captured = {}

    def agent_fn(context):
        try:
            captured["outcome"] = triage(inbox, config, now)
        except Exception as exc:
            captured["outcome"] = _outcome(inbox, "failed", f"{type(exc).__name__}: {exc}")
        if captured["outcome"]["status"] in ("failed", "needs_reauth"):
            raise TriageFailed(captured["outcome"]["error"])
        return {"changed": True, "cost_usd": captured["outcome"].get("cost_usd")}

    try:
        runtime = LoopRuntime(agent_fn=agent_fn, verifiers=build_verifiers(definition.verifiers, cwd=None),
                              events_dir=events_dir)
        write_result(runtime.start(definition, run_id=f"{run_id}_{inbox['name']}"), results_dir=results_dir,
                     events_dir=events_dir)
    except Exception as exc:  # noqa: BLE001 - e.g. PolicyViolationError; a triage result, if any, is kept
        print(f"inbox_triage_runner: loop runtime for {inbox['name']} failed: {type(exc).__name__}", file=sys.stderr)
        if "outcome" not in captured:
            captured["outcome"] = _outcome(inbox, "failed", f"Loop runtime error: {type(exc).__name__}")
    outcome = {**captured.get("outcome", _outcome(inbox, "failed", "runtime stopped before triage")),
               "slack_bundle": inbox.get("slack_bundle")}
    try:
        write_history(outcome, now, history_dir=history_dir)
    except Exception as exc:  # noqa: BLE001 - history is observability; the status write still has to happen
        print(f"inbox_triage_runner: writing history for {inbox['name']} failed: {type(exc).__name__}", file=sys.stderr)
    return outcome


def run_all_inboxes(run_id, now=None, config_path=None, definition_path=None, results_dir=None,
                    events_dir=None, status_path=None, history_dir=None, triage=None, lock_path=None):
    """One lock for the whole run: the scheduler and the dashboard's run-now
    can both launch a run, and two overlapping runs would each draft replies
    to the same not-yet-recorded messages. A second run exits at once."""
    if lock_path is None:
        lock_path = DEFAULT_LOCK_PATH
    with _exclusive_run_lock(lock_path) as acquired:
        if not acquired:
            print("inbox_triage_runner: another Inbox Triage run is already running - exiting", file=sys.stderr)
            return []
        return _run_all_inboxes_locked(run_id, now, config_path, definition_path, results_dir,
                                       events_dir, status_path, history_dir, triage)


def _run_all_inboxes_locked(run_id, now, config_path, definition_path, results_dir,
                            events_dir, status_path, history_dir, triage):
    if now is None:
        now = datetime.now(timezone.utc)
    if definition_path is None:
        definition_path = DEFAULT_DEFINITION_PATH
    config = inbox_config.load_config(config_path)
    definition = LoopDefinition.from_yaml(definition_path)
    if triage is None:
        triage = functools.partial(triage_inbox,
                                   timeout_seconds=definition.stop_conditions.max_runtime_minutes * 60)
    outcomes = []
    for inbox in [i for i in config["inboxes"] if i.get("enabled", True)]:
        outcome, terminal_written = None, False
        try:
            inbox_status.write(inbox["name"], "running", status_path=status_path)
            outcome = _run_one_inbox(inbox, config, now, run_id, definition, triage,
                                     results_dir, events_dir, history_dir)
            inbox_status.write(inbox["name"], _STATUS_STATE[outcome["status"]], status_path=status_path,
                               last_run_at=now.isoformat(), counts=outcome["counts"], urgent=outcome["urgent"],
                               error=outcome["error"], overflow=outcome["overflow"])
            terminal_written = True
        except Exception as exc:  # noqa: BLE001 - contained per inbox, like every other failure here
            reason = f"Run stopped unexpectedly ({type(exc).__name__})"
            print(f"inbox_triage_runner: {inbox['name']}: {reason}", file=sys.stderr)
            if outcome is None:
                outcome = {**_outcome(inbox, "failed", reason), "slack_bundle": inbox.get("slack_bundle")}
        finally:
            # Also runs on SystemExit (SIGTERM, see _raise_on_sigterm) and
            # KeyboardInterrupt, which then propagate.
            if not terminal_written:
                _mark_inbox_failed(inbox["name"], "Run was interrupted before this inbox finished",
                                   status_path=status_path)
        outcomes.append(outcome)
    send_digests(outcomes, now)
    return outcomes


def main_with_argv(argv, **kwargs):
    if len(argv) != 1:
        print("Usage: inbox_triage_runner.py <run_id>", file=sys.stderr)
        return 2
    run_all_inboxes(argv[0], **kwargs)
    # 0 even when inboxes failed: each failure is contained, recorded in
    # status/history, and announced in the digest - same reasoning as
    # topic_monitor_runner.main_with_argv.
    return 0


def main():
    signal.signal(signal.SIGTERM, _raise_on_sigterm)
    return main_with_argv(sys.argv[1:])


if __name__ == "__main__":
    sys.exit(main())
