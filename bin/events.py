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
        "timestamp": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
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
        written = os.write(fd, line)
        if written != len(line):
            raise RuntimeError(f"short write: wrote {written} of {len(line)} bytes")
    finally:
        os.close(fd)

    return event


def _parse_flag(argv, name):
    if name not in argv:
        return None
    idx = argv.index(name)
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1]


def _cmd_emit(argv):
    event_type = _parse_flag(argv, "--type")
    run_id = _parse_flag(argv, "--run-id")
    if not event_type:
        print("emit: --type is required", file=sys.stderr)
        return 1
    if not run_id:
        print("emit: --run-id is required", file=sys.stderr)
        return 1

    data_raw = _parse_flag(argv, "--data")
    data = None
    if data_raw is not None:
        try:
            data = json.loads(data_raw)
        except json.JSONDecodeError as exc:
            print(f"emit: --data is not valid JSON: {exc}", file=sys.stderr)
            return 1

    issue_iid_raw = _parse_flag(argv, "--issue-iid")
    issue_iid = int(issue_iid_raw) if issue_iid_raw is not None else None

    events_dir_raw = _parse_flag(argv, "--events-dir")
    events_dir = Path(events_dir_raw) if events_dir_raw else None

    emit(
        event_type,
        run_id,
        issue_run_id=_parse_flag(argv, "--issue-run-id"),
        project=_parse_flag(argv, "--project"),
        issue_iid=issue_iid,
        data=data,
        events_dir=events_dir,
    )
    return 0


def _print_matching_events(path, run_id_filter):
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                print(f"list: skipping malformed line in {path}: {exc}", file=sys.stderr)
                continue
            if run_id_filter is not None and event.get("run_id") != run_id_filter:
                continue
            print(json.dumps(event))


def _cmd_list(argv):
    date_stamp = _parse_flag(argv, "--date")
    run_id_filter = _parse_flag(argv, "--run-id")
    if not date_stamp and not run_id_filter:
        print("list: either --date or --run-id is required", file=sys.stderr)
        return 1
    events_dir_raw = _parse_flag(argv, "--events-dir")
    events_dir = Path(events_dir_raw) if events_dir_raw else DEFAULT_EVENTS_DIR

    if date_stamp:
        path = Path(events_dir) / f"{date_stamp}.jsonl"
        if not path.exists():
            return 0
        _print_matching_events(path, run_id_filter)
        return 0

    events_dir = Path(events_dir)
    if not events_dir.exists():
        return 0
    for path in sorted(events_dir.glob("*.jsonl")):
        _print_matching_events(path, run_id_filter)
    return 0


def main():
    if len(sys.argv) < 2:
        print("Usage: events.py emit --type T --run-id R [...] | events.py list (--date D | --run-id R) [...]", file=sys.stderr)
        sys.exit(1)

    command, argv = sys.argv[1], sys.argv[2:]
    if command == "emit":
        sys.exit(_cmd_emit(argv))
    elif command == "list":
        sys.exit(_cmd_list(argv))
    else:
        print(f"Usage: unknown command '{command}' (expected emit|list)", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
