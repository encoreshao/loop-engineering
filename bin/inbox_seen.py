#!/usr/bin/env python3
"""Per-inbox triage memory for the Inbox Triage loop: a high-water mark
(newest labelled message's date) plus the IDs already triaged in the last
14 days, so no message is labelled or drafted twice. Stored under
outputs/inbox-triage/state/<inbox>.json - per-checkout run state, like
outputs/topic-monitor/state/."""
import json
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATE_DIR = REPO_ROOT / "outputs" / "inbox-triage" / "state"
RETENTION_DAYS = 14
FIRST_RUN_HOURS = 48


def _path(name, state_dir):
    if state_dir is None:
        state_dir = DEFAULT_STATE_DIR
    return Path(state_dir) / f"{name}.json"


def load(name, state_dir=None):
    path = _path(name, state_dir)
    try:
        data = json.loads(path.read_text())
    except (OSError, ValueError):
        return {"high_water": None, "seen": {}}
    if not isinstance(data, dict) or not isinstance(data.get("seen"), dict):
        return {"high_water": None, "seen": {}}
    return {"high_water": data.get("high_water"), "seen": data["seen"]}


def since(state, now):
    if state.get("high_water"):
        return datetime.fromisoformat(state["high_water"])
    return now - timedelta(hours=FIRST_RUN_HOURS)


def record(name, messages, now, state_dir=None):
    path = _path(name, state_dir)
    state = load(name, state_dir)
    for message in messages:
        state["seen"][message["id"]] = now.isoformat()
        if state["high_water"] is None or datetime.fromisoformat(message["date"]) > datetime.fromisoformat(state["high_water"]):
            state["high_water"] = message["date"]
    cutoff = now - timedelta(days=RETENTION_DAYS)
    state["seen"] = {k: v for k, v in state["seen"].items() if datetime.fromisoformat(v) >= cutoff}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)
    return state


def forget(name, state_dir=None):
    """Delete a deleted inbox's triage memory, so a re-added inbox of the
    same name starts fresh (a new 48-hour first-run lookback) instead of
    inheriting another mailbox's high-water mark. True if there was one."""
    path = _path(name, state_dir)
    try:
        path.unlink()
    except FileNotFoundError:
        return False
    return True
