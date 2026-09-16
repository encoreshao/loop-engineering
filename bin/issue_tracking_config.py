#!/usr/bin/env python3
"""Per-issue opt-out of the gitlab-issue loop's tracking, set from the Live
GitLab dashboard page's per-issue enable/disable switch (see
render_gitlab_live_fragment / the /gitlab/issues/<alias>/<iid>/enable and
.../disable routes in bin/web/dashboard_server.py). Selection for the loop
itself stays 100% assignee+project-list driven (see
docs/tasks/gitlab-issue-loop.md) - this is a pure filter on top of that,
consulted by gitlab_loop_runner.run_all_issues before it invokes an issue's
agent.

Only DISABLED issues get an entry, keyed "<alias>#<issue_iid>" - a
newly-discovered assigned issue with no entry at all is enabled by default,
matching the loop's behavior before this file existed."""
import json
import os
from pathlib import Path

# LOOP_ENGINEERING_HOME lets dev/verification work (see CLAUDE.md's
# "Development mode" section) point this at a sandbox directory instead of
# the real, possibly-live ~/.loop-engineering.
LOOP_ENGINEERING_HOME = Path(os.environ.get("LOOP_ENGINEERING_HOME", str(Path.home() / ".loop-engineering")))
DEFAULT_TRACKING_PATH = LOOP_ENGINEERING_HOME / "issue_tracking.json"


def _key(alias, issue_iid):
    return f"{alias}#{issue_iid}"


def _read(path):
    path = Path(path)
    if not path.exists():
        return {}
    with open(path) as f:
        return json.load(f)


def _write(entries, path):
    """Atomic write: json.dump to a temp file in the same directory (so the
    final os.replace is same-filesystem, hence atomic), then replace the
    target - same pattern as loops_config._write_loops."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "w") as f:
        json.dump(entries, f, indent=2)
    tmp.replace(path)


def is_issue_enabled(alias, issue_iid, path=None):
    if path is None:
        path = DEFAULT_TRACKING_PATH
    entry = _read(path).get(_key(alias, issue_iid))
    if entry is None:
        return True
    return bool(entry.get("enabled", True))


def set_issue_enabled(alias, issue_iid, enabled, path=None):
    """Record whether the loop should keep tracking this issue. Returns
    (ok, message) - same shape as loops_config.set_enabled. Re-enabling
    removes the entry entirely rather than storing {"enabled": true}, so
    the file only ever grows with issues someone actually opted out of."""
    if path is None:
        path = DEFAULT_TRACKING_PATH
    entries = _read(path)
    key = _key(alias, issue_iid)
    if enabled:
        entries.pop(key, None)
    else:
        entries[key] = {"enabled": False}
    _write(entries, path)
    verb = "Enabled" if enabled else "Disabled"
    return True, f"{verb} tracking for #{issue_iid}"
