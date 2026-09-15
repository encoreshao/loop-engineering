#!/usr/bin/env bash
set -euo pipefail

# Wraps the manual `launchctl kickstart -k gui/$(id -u)/<label>` workflow
# documented as CLAUDE.md's one exception to "never touch the real daemons"
# - confirming a reviewed, already-merged change is actually live on this
# machine's real install. Restarts every one of this repo's launchd agents
# that is CURRENTLY LOADED, and only that - it never loads, enables, or
# removes an agent (that's install.sh's job for the dashboard, and the
# dashboard's Daemons page for the scheduler).
#
# The dashboard (com.hermes.loop-engineering-dashboard) is an always-on,
# stateless web server: restarting it has no side effect beyond a brief
# reconnect, so it's restarted unconditionally whenever it's loaded - this
# is the only way to make it pick up new code, since it does not hot-reload
# (`launchctl load` on an already-loaded agent is a silent no-op).
#
# Every other agent - today just com.hermes.loop-engineering, the unified
# scheduler that runs every loop registered in loops.json - is NOT
# restarted unless --with-scheduler is passed. Unlike the dashboard,
# kickstarting it forces an immediate poll, and if any registered loop is
# overdue that means a real, unscheduled run against live GitLab/Slack
# right now (comments, Slack messages, possibly an MR) - not just a
# restart. install.sh itself never kickstarts this daemon for exactly this
# reason (see its own top-of-file comment); this script defaults to the
# same caution rather than assuming a manual restart always wants that.

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
LAUNCHD_DIR="$LOOP_DIR/launchd"
DASHBOARD_LABEL="com.hermes.loop-engineering-dashboard"

# Fallback when there's no local repo clone with rendered plists yet - kept
# in sync with the filenames under launchd/ in this repo, same convention
# as uninstall.sh's own KNOWN_LAUNCHD_AGENT_NAMES.
KNOWN_LAUNCHD_AGENT_NAMES=(
  "com.hermes.loop-engineering-dashboard.plist"
  "com.hermes.loop-engineering.plist"
)

WITH_SCHEDULER=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --launchd-dir)
      LAUNCHD_DIR="$2"
      shift 2
      ;;
    --with-scheduler)
      WITH_SCHEDULER=1
      shift
      ;;
    *)
      echo "Usage: restart-daemons.sh [--launchd-dir PATH] [--with-scheduler]" >&2
      exit 1
      ;;
  esac
done

if [ -d "$LAUNCHD_DIR" ]; then
  shopt -s nullglob
  plists=("$LAUNCHD_DIR"/*.plist)
  shopt -u nullglob
  if [ "${#plists[@]}" -eq 0 ]; then
    echo "    No *.plist files found in $LAUNCHD_DIR"
  fi
else
  echo "    No local $LAUNCHD_DIR - falling back to this repo's known agent names"
  plists=("${KNOWN_LAUNCHD_AGENT_NAMES[@]}")
fi

restarted=0
skipped_not_loaded=0
skipped_gated=0

for plist in "${plists[@]+"${plists[@]}"}"; do
  label="$(basename "$plist" .plist)"

  if ! launchctl list "$label" >/dev/null 2>&1; then
    echo "    $label is not loaded, skipping"
    skipped_not_loaded=$((skipped_not_loaded + 1))
    continue
  fi

  if [ "$label" != "$DASHBOARD_LABEL" ] && [ "$WITH_SCHEDULER" -eq 0 ]; then
    echo "    $label not restarted (pass --with-scheduler - this can trigger an immediate live run)"
    skipped_gated=$((skipped_gated + 1))
    continue
  fi

  echo "==> Restarting $label..."
  launchctl kickstart -k "gui/$(id -u)/$label"
  if [ "$label" = "$DASHBOARD_LABEL" ]; then
    echo "    $label restarted"
  else
    echo "    $label restarted (this may have just triggered a live poll/run)"
  fi
  restarted=$((restarted + 1))
done

echo ""
echo "Done: $restarted restarted, $skipped_not_loaded not loaded, $skipped_gated gated behind --with-scheduler."
