#!/usr/bin/env python3
"""Load this loop's registry of scheduled loops from
~/.loop-engineering/loops.json (see config/loops.json.template in this
repo). Each entry names a loop (gitlab-loop, topic-loop, and any
future loop), its schedule, its entry point module, and the handful of
per-loop knobs run-loop-now.sh and bin/loop_scheduler.py need - adding a
new loop means adding one entry here, not a new plist/shell script. See
docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md."""
import json
import os
import re
import shlex
import sys
from pathlib import Path

# Loop names end up embedded in file paths built elsewhere (e.g.
# status_path_for_loop in dashboard_server.py, ENTRY_SCRIPT in
# run-loop-now.sh) - not currently exploitable (every caller passes a
# fixed literal, and this registry is a local, user-owned config file at
# the same trust tier as projects.json), but cheap defense in depth all
# the same.
_VALID_LOOP_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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
    name is registered, or ValueError if `name` isn't safe to embed in a
    file path (see _VALID_LOOP_NAME_RE above)."""
    if not _VALID_LOOP_NAME_RE.match(name):
        raise ValueError(
            f"Invalid loop name {name!r} - loop names may only contain "
            f"letters, digits, '.', '_', and '-'."
        )
    for loop in list_loops(config_path):
        if loop["name"] == name:
            return loop
    raise KeyError(f"No loop named {name!r} in the loops registry")


def _write_loops(loops, config_path):
    """Atomic write: json.dump to a temp file in the same directory (so the
    final os.replace is same-filesystem, hence atomic), then replace the
    target - same pattern as topic_config._write_topics."""
    path = Path(config_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(loops, f, indent=2)
    tmp.replace(path)


def set_enabled(name, enabled, config_path=None):
    """Flip the matching entry's "enabled" field and write the registry
    back. Returns (ok, message); (False, ...) and no write at all if no
    loop named `name` is registered."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    loops = list_loops(config_path)
    for loop in loops:
        if loop["name"] == name:
            loop["enabled"] = bool(enabled)
            _write_loops(loops, config_path)
            return True, f"{'Enabled' if enabled else 'Disabled'} {name}"
    return False, f"No loop named {name!r} in the loops registry"


# Each frequency's own required fields and per-field bounds - shared by
# set_schedule's validation and (informally) by bin/loop_scheduler.py's
# is_due, which reads these same keys.
_SCHEDULE_FIELD_BOUNDS = {
    "hour": (0, 23),
    "minute": (0, 59),
    "day": (1, 31),
    "interval_hours": (1, 24),
}
_SCHEDULE_REQUIRED_FIELDS = {
    "daily": ("hour", "minute"),
    "weekly": ("weekdays", "hour", "minute"),
    "monthly": ("day", "hour", "minute"),
    "hourly": ("interval_hours",),
}


def set_schedule(name, schedule, config_path=None):
    """Validate `schedule` (one of the four {"frequency": ...} shapes -
    see docs/superpowers/specs - "daily"/"weekly"/"monthly"/"hourly") and
    write it onto the matching loop's registry entry. Returns (ok,
    message); a validation failure or unknown loop name writes nothing."""
    if config_path is None:
        config_path = DEFAULT_CONFIG_PATH
    frequency = schedule.get("frequency")
    if frequency not in _SCHEDULE_REQUIRED_FIELDS:
        return False, f"Unknown schedule frequency: {frequency!r}"
    missing = [f for f in _SCHEDULE_REQUIRED_FIELDS[frequency] if f not in schedule]
    if missing:
        return False, f"Schedule for {frequency!r} is missing: {', '.join(missing)}"
    for field, (lo, hi) in _SCHEDULE_FIELD_BOUNDS.items():
        if field in schedule and field in _SCHEDULE_REQUIRED_FIELDS[frequency]:
            value = schedule[field]
            if not isinstance(value, int) or isinstance(value, bool) or not (lo <= value <= hi):
                return False, f"{field} must be an integer between {lo} and {hi}, got {value!r}"
    if frequency == "weekly":
        weekdays = schedule["weekdays"]
        if weekdays != "all":
            if not isinstance(weekdays, list) or not all(isinstance(d, int) and 1 <= d <= 7 for d in weekdays):
                return False, f"weekdays must be \"all\" or a list of integers 1-7, got {weekdays!r}"

    loops = list_loops(config_path)
    for loop in loops:
        if loop["name"] == name:
            loop["schedule"] = schedule
            _write_loops(loops, config_path)
            return True, f"Updated schedule for {name}"
    return False, f"No loop named {name!r} in the loops registry"


def main():
    if len(sys.argv) < 2:
        print(
            "Usage: loops_config.py <names|entry-point|timeout-seconds|log-suffix|emit-run-events|bash-env> [name]",
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
    except (KeyError, ValueError, FileNotFoundError, json.JSONDecodeError) as exc:
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
    elif command == "bash-env":
        try:
            entry_point = loop["entry_point"]
            timeout_seconds = loop["timeout_seconds"]
        except KeyError as exc:
            print(f"Loop {name!r} is missing required registry field {exc}", file=sys.stderr)
            sys.exit(1)
        log_suffix = loop.get("log_suffix", "")
        emit_run_events = "true" if loop.get("emit_run_events", False) else "false"
        print(f"ENTRY_POINT={shlex.quote(str(entry_point))}")
        print(f"TIMEOUT_SECONDS={shlex.quote(str(timeout_seconds))}")
        print(f"LOG_SUFFIX={shlex.quote(str(log_suffix))}")
        print(f"EMIT_RUN_EVENTS={shlex.quote(emit_run_events)}")
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
