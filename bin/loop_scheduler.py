#!/usr/bin/env python3
"""Polls the loops registry (~/.loop-engineering/loops.json, see
bin/loops_config.py) and runs whichever registered loop(s) are due right
now, via the shared run-loop-now.sh - see
docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md. This
is the single launchd job (com.hermes.loop-engineering, StartInterval)
that replaces the old per-loop StartCalendarInterval jobs; adding a third
loop means a new ~/.loop-engineering/loops.json entry, not a new plist."""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

# dashboard_server lives in bin/web/ - see topic_monitor_runner.py for the
# same sys.path convention.
sys.path.insert(0, str(Path(__file__).resolve().parent / "web"))

import dashboard_server
import loops_config

LOOP_DIR = Path(__file__).resolve().parent.parent
RUN_LOOP_NOW_SH = LOOP_DIR / "run-loop-now.sh"
# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead
# of the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_STATE_PATH = LOOP_ENGINEERING_HOME / "loop_scheduler_state.json"


def _read_state(state_path=None):
    if state_path is None:
        state_path = DEFAULT_STATE_PATH
    path = Path(state_path)
    if not path.exists():
        return {}
    try:
        with open(path) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(state, state_path=None):
    if state_path is None:
        state_path = DEFAULT_STATE_PATH
    path = Path(state_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2)


def is_due(loop, state, now=None):
    """`loop` is one entry from loops_config.list_loops() ("name" and
    "schedule" = {"weekdays": [1-7,...] or "all", "hour", "minute"}).
    `state` is the full scheduler-state dict (loop name -> {"last_attempted_date": "YYYY-MM-DD"}).
    `now` is injectable for tests; defaults to datetime.now() (LOCAL
    time, matching launchd's own StartCalendarInterval Hour/Minute
    semantics - the same convention the old per-loop plists used)."""
    if now is None:
        now = datetime.now()
    schedule = loop["schedule"]
    weekdays = schedule["weekdays"]
    if weekdays != "all" and now.isoweekday() not in weekdays:
        return False
    if (now.hour, now.minute) < (schedule["hour"], schedule["minute"]):
        return False
    today = now.date().isoformat()
    last_attempted = state.get(loop["name"], {}).get("last_attempted_date")
    return last_attempted != today


def _is_running(loop_name):
    status = dashboard_server.read_status(dashboard_server.status_path_for_loop(loop_name))
    return status.get("state") == "running"


def run_due_loops(loops=None, state_path=None, run_loop_now_path=None, now=None, runner=None):
    """For each loop that `is_due` and isn't already `running` (per its
    own status file - a belt-and-suspenders guard against two overlapping
    scheduler polls, on top of the once-per-day state check below),
    invokes `runner` (defaults to subprocess.run) as
    ["bash", str(run_loop_now_path), loop["name"]], blocking until it
    returns before checking the next loop, and records
    last_attempted_date immediately after - regardless of exit code or
    exception, so a failed run is never retried until tomorrow (matching
    how a missed/failed StartCalendarInterval slot behaved before this).
    One loop raising never stops the rest from being checked. Returns the
    list of loop names actually attempted, in registry order."""
    if loops is None:
        loops = loops_config.list_loops()
    if run_loop_now_path is None:
        run_loop_now_path = RUN_LOOP_NOW_SH
    if runner is None:
        runner = subprocess.run
    if now is None:
        now = datetime.now()
    state = _read_state(state_path)
    attempted = []
    for loop in loops:
        if not is_due(loop, state, now=now):
            continue
        if _is_running(loop["name"]):
            continue
        try:
            runner(["bash", str(run_loop_now_path), loop["name"]])
        except Exception:
            pass
        state.setdefault(loop["name"], {})["last_attempted_date"] = now.date().isoformat()
        _write_state(state, state_path)
        attempted.append(loop["name"])
    return attempted


def main():
    run_due_loops()


if __name__ == "__main__":
    main()
