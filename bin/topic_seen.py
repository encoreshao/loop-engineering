#!/usr/bin/env python3
"""Track which items have already been reported for a topic, so the topic
monitor loop's daily briefing doesn't repeat the same story two days
running. State lives in outputs/topic-monitor/state/<topic_name>.json, a
rolling window of {url, title, seen_at} entries pruned to the last
SEEN_WINDOW_DAYS days."""
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / "outputs" / "topic-monitor" / "state"
SEEN_WINDOW_DAYS = 7


def _state_path(topic_name, state_dir=None):
    if state_dir is None:
        state_dir = DEFAULT_STATE_DIR
    safe_name = Path(topic_name).name
    return Path(state_dir) / f"{safe_name}.json"


def _load(topic_name, state_dir=None):
    path = _state_path(topic_name, state_dir)
    if not path.exists():
        return []
    try:
        with open(path) as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def _save(topic_name, entries, state_dir=None):
    path = _state_path(topic_name, state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(entries, f, indent=2)


def _prune(entries, now=None):
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=SEEN_WINDOW_DAYS)
    pruned = []
    for e in entries:
        try:
            seen_at = datetime.fromisoformat(e["seen_at"])
        except (KeyError, ValueError, TypeError):
            continue
        if seen_at >= cutoff:
            pruned.append(e)
    return pruned


def get_seen(topic_name, state_dir=None):
    """Every {url, title} still within the rolling window, pruning anything
    older as a side effect."""
    entries = _prune(_load(topic_name, state_dir))
    _save(topic_name, entries, state_dir)
    return [{"url": e["url"], "title": e["title"]} for e in entries]


def add_seen(topic_name, url, title, state_dir=None, now=None):
    """Record one item as reported, at the current UTC time (or `now`, for
    tests)."""
    if now is None:
        now = datetime.now(timezone.utc)
    entries = _prune(_load(topic_name, state_dir), now=now)
    entries.append({"url": url, "title": title, "seen_at": now.isoformat()})
    _save(topic_name, entries, state_dir)


def main():
    if len(sys.argv) < 3:
        print(
            "Usage: topic_seen.py get <topic_name>\n"
            "       topic_seen.py add <topic_name> <url> <title>",
            file=sys.stderr,
        )
        sys.exit(1)
    command, topic_name = sys.argv[1], sys.argv[2]
    if command == "get":
        print(json.dumps(get_seen(topic_name, DEFAULT_STATE_DIR), indent=2))
    elif command == "add":
        if len(sys.argv) < 5:
            print("Usage: topic_seen.py add <topic_name> <url> <title>", file=sys.stderr)
            sys.exit(1)
        add_seen(topic_name, sys.argv[3], sys.argv[4], DEFAULT_STATE_DIR)
        print("OK")
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
