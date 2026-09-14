#!/usr/bin/env python3
"""Load this loop's registry of scheduled loops from
~/.loop-engineering/loops.json (see config/loops.json.template in this
repo). Each entry names a loop (gitlab-issue-loop, topic-monitor, and any
future loop), its schedule, its entry point module, and the handful of
per-loop knobs run-loop-now.sh and bin/loop_scheduler.py need - adding a
new loop means adding one entry here, not a new plist/shell script. See
docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md."""
import json
import os
import sys
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_CONFIG_PATH = LOOP_ENGINEERING_HOME / "loops.json"


def list_loops(config_path=None):
    """The full registry as a list of dicts, in file order. Raises
    FileNotFoundError with a helpful message if the config doesn't exist
    yet - same convention as loop_config.load_config."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"No loops config at {path}. Copy config/loops.json.template "
            f"there (bin/scripts/setup.sh does this automatically)."
        )
    with open(path) as f:
        return json.load(f)


def get_loop(name, config_path=None):
    """One registry entry by name. Raises KeyError if no loop with that
    name is registered."""
    for loop in list_loops(config_path):
        if loop["name"] == name:
            return loop
    raise KeyError(f"No loop named {name!r} in the loops registry")


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: loops_config.py <names|entry-point|timeout-seconds|log-suffix|emit-run-events> [name]",
            file=sys.stderr,
        )
        sys.exit(1)
    command = sys.argv[1]
    if command == "names":
        print("\n".join(loop["name"] for loop in list_loops()))
        return
    if len(sys.argv) < 3:
        print(f"Usage: loops_config.py {command} <name>", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[2]
    try:
        loop = get_loop(name)
    except KeyError as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
    if command == "entry-point":
        print(loop["entry_point"])
    elif command == "timeout-seconds":
        print(loop["timeout_seconds"])
    elif command == "log-suffix":
        print(loop.get("log_suffix", ""))
    elif command == "emit-run-events":
        print("true" if loop.get("emit_run_events", False) else "false")
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
