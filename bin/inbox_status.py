#!/usr/bin/env python3
"""outputs/inbox-triage/status.json - per-inbox state the dashboard's
Inbox Triage page reads (idle/running/failed/needs_reauth, last counts,
the latest urgent list). Never holds message body text."""
import json
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_STATUS_PATH = REPO_ROOT / "outputs" / "inbox-triage" / "status.json"


def read(status_path=None):
    if status_path is None:
        status_path = DEFAULT_STATUS_PATH
    try:
        data = json.loads(Path(status_path).read_text())
    except (OSError, ValueError):
        return {"inboxes": {}}
    if not isinstance(data, dict) or not isinstance(data.get("inboxes"), dict):
        return {"inboxes": {}}
    return data


def write(name, state, status_path=None, **extra):
    if status_path is None:
        status_path = DEFAULT_STATUS_PATH
    path = Path(status_path)
    data = read(path)
    entry = data["inboxes"].setdefault(name, {})
    entry.update(extra)
    entry["state"] = state
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)
    return entry


def any_running(status_path=None):
    return any(e.get("state") == "running" for e in read(status_path)["inboxes"].values())
