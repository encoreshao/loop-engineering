#!/usr/bin/env python3
"""Append-only, machine-readable event log for this loop's own runs and
issue executions - see docs/superpowers/specs/2026-09-04-event-system-design.md.
Markdown (outputs/daily-review.md, PROGRESS.md) stays the human-readable
state; this is the structured source a future metrics module reads from
instead of parsing that Markdown. Events land in outputs/events/<UTC
date>.jsonl, one JSON object per line, written with the CLI below by
run-loop.sh (for run.* events) and by the agent following
LOOPX_INSTRUCTIONS.md (for issue.*/verification.* events)."""
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_EVENTS_DIR = Path(__file__).resolve().parent.parent / "outputs" / "events"

SCHEMA_VERSION = 1


def emit(event_type, run_id, issue_run_id=None, project=None, issue_iid=None,
         data=None, events_dir=None):
    """Append one event to today's JSONL file and return the event dict
    that was written. `event_type` and `run_id` are required - everything
    else is optional and defaults to None (or {} for `data`). Raises
    ValueError if `event_type` or `run_id` is missing/empty."""
    if not event_type:
        raise ValueError("event_type is required")
    if not run_id:
        raise ValueError("run_id is required")
    if events_dir is None:
        events_dir = DEFAULT_EVENTS_DIR

    now = datetime.now(timezone.utc)
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": f"evt_{uuid.uuid4().hex}",
        "timestamp": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "event_type": event_type,
        "run_id": run_id,
        "issue_run_id": issue_run_id,
        "project": project,
        "issue_iid": issue_iid,
        "data": data if data is not None else {},
    }

    events_dir = Path(events_dir)
    events_dir.mkdir(parents=True, exist_ok=True)
    date_stamp = now.strftime("%Y-%m-%d")
    path = events_dir / f"{date_stamp}.jsonl"

    line = (json.dumps(event) + "\n").encode("utf-8")
    fd = os.open(str(path), os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o644)
    try:
        os.write(fd, line)
    finally:
        os.close(fd)

    return event
