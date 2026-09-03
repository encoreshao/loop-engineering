#!/usr/bin/env python3
"""Load this loop's per-machine topic configuration from
~/.loop-engineering/topics.json (see config/topics.json.template in this
repo). Which topics to monitor, and what counts as notable for each one,
live here instead of being hardcoded, since every team member running this
loop chooses their own topics."""
import json
import os
import sys
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "topics.json"


def load_config(config_path=DEFAULT_CONFIG_PATH):
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No topic config at {path}. Copy config/topics.json.template "
            f"there and list the topics you want to monitor."
        )
    with open(path) as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array of topics, got {type(data).__name__}")
    return data


def list_names(config_path=DEFAULT_CONFIG_PATH):
    return [t["name"] for t in load_config(config_path)]


def get_topic(name, config_path=DEFAULT_CONFIG_PATH):
    for t in load_config(config_path):
        if t["name"] == name:
            return t
    raise KeyError(f"No topic named {name!r} in {config_path}")


def _load_topics_or_empty(config_path):
    """Same shape load_config returns, but [] instead of raising when the
    file doesn't exist yet - upsert_topic needs to work on a first-ever
    add, before config/topics.json.template has been copied anywhere."""
    try:
        return load_config(config_path)
    except FileNotFoundError:
        return []


def _write_topics(topics, config_path):
    """Atomic write: json.dump to a temp file in the same directory (so the
    final os.replace is same-filesystem, hence atomic), then replace the
    target. A crash mid-write leaves only the temp file orphaned, never a
    half-written topics.json."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(topics, f, indent=2)
    tmp.replace(path)


def upsert_topic(name, label, brief, slack_bundle="", config_path=DEFAULT_CONFIG_PATH):
    """Add a new topic, or update an existing one in place (matched by
    `name`, which is the identity key used in history filenames and dedup
    state - callers should treat it as fixed once a topic has ever run,
    the same way GitLab project aliases are fixed once configured). Blank
    `slack_bundle` means "use the default webhook", stored as `None` to
    match config/topics.json.template's own convention."""
    name = name.strip()
    label = label.strip()
    brief = brief.strip()
    slack_bundle = slack_bundle.strip()
    if not name:
        return False, "Topic name is required"
    if not label:
        return False, "Label is required"
    if not brief:
        return False, "Brief is required"
    topics = _load_topics_or_empty(config_path)
    entry = {"name": name, "label": label, "brief": brief, "slack_bundle": slack_bundle or None}
    is_new = True
    for i, t in enumerate(topics):
        if t["name"] == name:
            topics[i] = entry
            is_new = False
            break
    if is_new:
        topics.append(entry)
    _write_topics(topics, config_path)
    return True, f"{'Added' if is_new else 'Updated'} topic {name}"


def delete_topic(name, config_path=DEFAULT_CONFIG_PATH):
    topics = _load_topics_or_empty(config_path)
    remaining = [t for t in topics if t["name"] != name]
    if len(remaining) == len(topics):
        return False, f"Unknown topic: {name}"
    _write_topics(remaining, config_path)
    return True, f"Deleted topic {name}"


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: topic_config.py names\n"
            "       topic_config.py topic <name>",
            file=sys.stderr,
        )
        sys.exit(1)
    command = sys.argv[1]
    if command == "names":
        print("\n".join(list_names(DEFAULT_CONFIG_PATH)))
    elif command == "topic":
        if len(sys.argv) < 3:
            print("Usage: topic_config.py topic <name>", file=sys.stderr)
            sys.exit(1)
        try:
            print(json.dumps(get_topic(sys.argv[2], DEFAULT_CONFIG_PATH), indent=2))
        except KeyError as e:
            print(str(e), file=sys.stderr)
            sys.exit(1)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
