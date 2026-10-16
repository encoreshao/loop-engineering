#!/usr/bin/env python3
"""Proactive sign-in check for every mailbox and auth-bearing connector.

loop_scheduler calls `run_if_due` on each poll; it probes at most once per
CHECK_INTERVAL. A probe is a real token refresh (mailboxes) or the
connector's own `test()`, so it also keeps rotating refresh tokens (Outlook)
and idle ones (Google's 6-month inactivity limit) alive. A sign-in that has
died (Google refresh tokens last 7 days while the OAuth consent screen is in
"Testing" mode) is reported once to Slack, repeated daily while it stays
broken, and a recovery message follows. Never holds or sends a secret."""
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import mail_auth
import mail_http

CHECK_INTERVAL = timedelta(hours=6)
REMIND_INTERVAL = timedelta(hours=24)
SOFT_FAILURES_BEFORE_ALERT = 2
# Connector types whose credentials can expire or be revoked. Slack, webhook,
# Telegram and RSS are skipped: their test() posts a message or has no auth.
# "mailbox" accounts are covered by the inbox probes.
AUTH_TYPES = ("google_calendar", "gitlab", "github", "jira", "linear", "notion")


def _home():
    return Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))


def default_state_path():
    return _home() / "auth_health.json"


def read_state(state_path=None):
    if state_path is None:
        state_path = default_state_path()
    try:
        data = json.loads(Path(state_path).read_text())
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    if not isinstance(data.get("checks"), dict):
        data["checks"] = {}
    return data


def _write_state(state, state_path):
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)


def _default_send(text):
    import slack_notify
    slack_notify.post_message(text)


def _elapsed(iso, now, default):
    try:
        return now - datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return default


def _evaluate(probe):
    """(ok, message, hard). hard = the sign-in is definitely dead, so alert at
    once; a soft failure (a connector test() or a transient error) must repeat."""
    try:
        ok, message = probe["run"]()
        return bool(ok), str(message), False
    except (mail_auth.ReauthRequired, mail_http.AuthExpired) as exc:
        return False, str(exc) or "Sign-in expired", True
    except Exception as exc:  # noqa: BLE001 - one broken probe must not stop the rest
        return False, f"{type(exc).__name__}: {exc}", False


def run_checks(probes, state_path=None, now=None, send=None):
    """Probe each entry ({"id", "label", "run"}), update the state file and
    alert on transitions. Returns the list of messages sent."""
    if state_path is None:
        state_path = default_state_path()
    if now is None:
        now = datetime.now()
    if send is None:
        send = _default_send
    state = read_state(state_path)
    checks = state["checks"]
    sent = []

    def notify(text):
        try:
            send(text)
        except Exception:  # noqa: BLE001 - a Slack outage must not lose the check
            return False
        sent.append(text)
        return True

    for probe in probes:
        ok, message, hard = _evaluate(probe)
        entry = checks.get(probe["id"], {})
        now_iso = now.isoformat()
        if ok:
            if entry.get("last_alert_at"):
                notify(f"Sign-in recovered: {probe['label']} is connected again.")
            checks[probe["id"]] = {"ok": True, "message": message, "checked_at": now_iso}
            continue
        was_failing = not entry.get("ok", True)
        new = {"ok": False, "message": message, "checked_at": now_iso,
               "fail_count": entry.get("fail_count", 0) + 1 if was_failing else 1,
               "failing_since": (entry.get("failing_since") if was_failing else None) or now_iso}
        if entry.get("last_alert_at"):
            new["last_alert_at"] = entry["last_alert_at"]
        confirmed = hard or new["fail_count"] >= SOFT_FAILURES_BEFORE_ALERT
        due = _elapsed(entry.get("last_alert_at"), now, REMIND_INTERVAL) >= REMIND_INTERVAL
        if confirmed and due and notify(
                f"Sign-in needs attention: {probe['label']} - {message}. "
                "Reconnect it from the dashboard (Inbox Triage or Connectors)."):
            new["last_alert_at"] = now_iso
        checks[probe["id"]] = new

    # Drop entries for accounts that no longer exist.
    live = {p["id"] for p in probes}
    state["checks"] = {k: v for k, v in checks.items() if k in live}
    state["last_run_at"] = now.isoformat()
    _write_state(state, state_path)
    return sent


def collect_probes(inboxes_fn=None, accounts_fn=None, token_fn=None, connector_fn=None):
    if inboxes_fn is None:
        import inbox_config
        inboxes_fn = inbox_config.enabled_inboxes
    if accounts_fn is None or connector_fn is None:
        import connectors_config
        accounts_fn = accounts_fn or connectors_config.list_accounts
        connector_fn = connector_fn or connectors_config.load_connector
    if token_fn is None:
        token_fn = mail_auth.get_access_token
    probes = []
    for inbox in inboxes_fn():
        def run(inbox=inbox):
            token_fn(inbox)
            return True, "Token refreshed"
        probes.append({"id": f"mailbox:{inbox['name']}", "label": f"{inbox['name']} ({inbox['provider']})", "run": run})
    for account in accounts_fn():
        if account.get("type") not in AUTH_TYPES or account.get("enabled") is False:
            continue

        def run(account=account):
            return connector_fn(account["id"]).test()
        probes.append({"id": f"connector:{account['id']}", "label": account.get("label") or account["id"], "run": run})
    return probes


def run_if_due(probes=None, state_path=None, now=None, send=None):
    """True if it ran. Never raises: it must not break the scheduler poll."""
    if state_path is None:
        state_path = default_state_path()
    if now is None:
        now = datetime.now()
    try:
        last = read_state(state_path).get("last_run_at")
        if last and _elapsed(last, now, CHECK_INTERVAL) < CHECK_INTERVAL:
            return False
        if probes is None:
            probes = collect_probes()
        run_checks(probes, state_path=state_path, now=now, send=send)
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"auth_health: {type(exc).__name__}: {exc}", file=sys.stderr)
        return False
