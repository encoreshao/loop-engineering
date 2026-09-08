#!/usr/bin/env bash
set -euo pipefail

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$LOOP_DIR/outputs/history"
UNIFIED_LOG_DIR="$LOOP_DIR/logs"
UNIFIED_LOG="$UNIFIED_LOG_DIR/loop-engineering.log"
DATE_STAMP="$(date +%F)"

# A stable identity for this run, threaded into topic_monitor_runner.py's
# per-topic run_id scheme (<run_id>_<topic_name>) - same rationale as
# run-loop.sh's own RUN_ID (a real, collision-free timestamp is cheap and
# deterministic to generate here). This script never had one before this
# migration, since nothing downstream previously read it.
RUN_ID="run_$(date -u +%Y%m%d_%H%M%S)"

mkdir -p "$LOG_DIR" "$UNIFIED_LOG_DIR"
mkdir -p "$LOOP_DIR/outputs/topic-monitor"

exec >> "$LOG_DIR/$DATE_STAMP-topic-monitor.log" 2>&1

# See run-loop.sh's own comment on this: a plain synchronous append,
# separate from the exec redirect above, so nothing here risks losing the
# last few lines to an unflushed background process at exit (unlike
# `exec > >(tee ...)` would) - logs/loop-engineering.log is the one place
# every `claude` CLI invocation across this project logs to (the
# dashboard's Logs page reads it via append_unified_log/render_logs_page).
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- topic-monitor ---- run started ----" >> "$UNIFIED_LOG"

cd "$LOOP_DIR"

trap 'loop_exit=$?; echo "[$(date "+%Y-%m-%d %H:%M:%S")] ---- topic-monitor ---- run FAILED (exit $loop_exit) ----" >> "$UNIFIED_LOG"; python3 bin/slack_notify.py "*Topic monitor loop FAILED* (exit $loop_exit) — see outputs/history/$DATE_STAMP-topic-monitor.log" || true' ERR

# See run-loop.sh's own comment for why this is delegated to a login zsh
# rather than sourcing ~/.zprofile/~/.zshrc directly from bash.

# Why this loop's writes are confined to outputs/topic-monitor/, and why
# that confinement is the deny list rather than --add-dir or the allow
# list - plus the one absolute-path LaunchAgents rule and the residual
# gap that is known and accepted - is documented where those rules now
# actually live: the header comment above
# bin/topic_monitor_runner.py's `_allowed_tools()`/`_disallowed_tools()`
# (and in prose in TOPIC_MONITOR_INSTRUCTIONS.md's "Tool permissions
# policy" section). That rationale is load-bearing, but this script no
# longer holds any of the strings it explains, so it is not duplicated
# here.
#
# The actual CLI invocation, ALLOWED_TOOLS/DISALLOWED_TOOLS enforcement,
# and per-topic orchestration all moved into bin/topic_monitor_runner.py,
# which now runs one topic at a time through LoopRuntime - see
# docs/superpowers/specs/2026-09-07-topic-monitor-runtime-wiring-design.md.
# The outer timeout is raised from the old 3600s: with the new 30-minute
# per-topic budget, as few as 2 slow topics could exhaust the old bound
# on their own (this repo's real ~/.loop-engineering/topics.json
# currently configures 3 topics - 3 x 1800s = 5400s already exceeds
# 3600s) - 21600s (6h) matches the same generous, documented sanity
# ceiling the GitLab loop's own migration chose, for the same reason:
# the real per-topic bound is now topic_monitor_runner.py's own timeout,
# not this outer one.
#
# Deliberately NOT piped through `tee -a "$UNIFIED_LOG"` (matching
# run-loop.sh's own equivalent line): topic_monitor_runner.py already
# appends every topic's CLI output to logs/loop-engineering.log itself,
# via its own _append_unified_log, so a tee here would put two copies of
# every topic's output in the unified log. The `exec >> ... 2>&1` at the
# top of this script still captures this process's own stdout/stderr into
# the per-run dated log, so nothing is lost.
SERIALIZED_RUNNER_CMD="$(printf '%q ' python3 "$LOOP_DIR/bin/topic_monitor_runner.py" "$RUN_ID")"
zsh -i -l -c "timeout 21600 $SERIALIZED_RUNNER_CMD"

# Reaching here means the delegated command exited zero (a non-zero exit
# would have tripped `set -e` and the ERR trap above instead).
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- topic-monitor ---- run finished (exit 0) ----" >> "$UNIFIED_LOG"
