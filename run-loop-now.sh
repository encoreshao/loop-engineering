#!/usr/bin/env bash
set -euo pipefail

# Generic entry point for exactly one registered loop's run, right now -
# replaces the old per-loop run-loop.sh/run-topic-monitor-loop.sh (see
# docs/superpowers/specs/2026-09-14-unified-loop-scheduler-design.md).
# Looks up its own per-loop knobs (entry point, timeout, log filename
# suffix, whether to emit run.started/run.failed) from
# ~/.loop-engineering/loops.json via bin/loops_config.py, so a new loop
# needs a registry entry, not a new script. Three callers use this script
# today: bin/loop_scheduler.py (on schedule), the dashboard's "run now"
# buttons (trigger_manual_run/trigger_topic_monitor_run), and the
# dashboard's "paste an issue link" chat tool (which passes <alias>
# <issue_iid> as extra args - the GitLab loop's own scoped single-issue
# mode, gitlab_loop_runner.py's `main(argv=[run_id, alias, issue_iid])`).

if [[ $# -lt 1 ]]; then
  echo "Usage: run-loop-now.sh <loop_name> [extra_args...]" >&2
  exit 2
fi
LOOP_NAME="$1"
shift
EXTRA_ARGS=("$@")

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$LOOP_DIR/outputs/history"
UNIFIED_LOG_DIR="$LOOP_DIR/logs"
UNIFIED_LOG="$UNIFIED_LOG_DIR/loop-engineering.log"
DATE_STAMP="$(date +%F)"

ENTRY_POINT="$(python3 "$LOOP_DIR/bin/loops_config.py" entry-point "$LOOP_NAME")"
TIMEOUT_SECONDS="$(python3 "$LOOP_DIR/bin/loops_config.py" timeout-seconds "$LOOP_NAME")"
LOG_SUFFIX="$(python3 "$LOOP_DIR/bin/loops_config.py" log-suffix "$LOOP_NAME")"
EMIT_RUN_EVENTS="$(python3 "$LOOP_DIR/bin/loops_config.py" emit-run-events "$LOOP_NAME")"
ENTRY_SCRIPT="$LOOP_DIR/$(echo "$ENTRY_POINT" | tr . /).py"

# A stable identity for this run - see run-loop.sh's former comment on
# this (a real, collision-free timestamp is cheap and deterministic to
# generate here). Exported unconditionally for every loop:
# LOOPX_INSTRUCTIONS.md's agent reads it for the GitLab loop; it's simply
# unread (harmless) for any loop whose agent doesn't look for it.
RUN_ID="run_$(date -u +%Y%m%d_%H%M%S)"
export LOOP_RUN_ID="$RUN_ID"

mkdir -p "$LOG_DIR" "$UNIFIED_LOG_DIR"
mkdir -p "$LOOP_DIR/outputs/topic-monitor"

exec >> "$LOG_DIR/$DATE_STAMP$LOG_SUFFIX.log" 2>&1

echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- $LOOP_NAME ---- run started ----" >> "$UNIFIED_LOG"

cd "$LOOP_DIR"

# A scheduled/on-demand run has nobody watching it, so any non-zero exit
# must announce itself in Slack rather than dying silently in the log.
# Each command is guarded with `|| true` so a failure in the notify/
# status-write itself can't cause a second ERR trap. events.py's
# run.failed emission is gated by EMIT_RUN_EVENTS (see loops.json's
# "emit_run_events" field) - see the design spec for why this can't be
# unconditional for every loop.
trap 'loop_exit=$?; echo "[$(date "+%Y-%m-%d %H:%M:%S")] ---- $LOOP_NAME ---- run FAILED (exit $loop_exit) ----" >> "$UNIFIED_LOG"; python3 bin/slack_notify.py "*$LOOP_NAME loop FAILED* (exit $loop_exit) — see outputs/history/$DATE_STAMP$LOG_SUFFIX.log" || true; python3 bin/web/dashboard_server.py write-status failed --loop "$LOOP_NAME" --exit-code $loop_exit || true; if [ "$EMIT_RUN_EVENTS" = "true" ]; then python3 bin/events.py emit --type run.failed --run-id "$RUN_ID" --data "{\"exit_code\": $loop_exit}" || true; fi' ERR

if [ "$EMIT_RUN_EVENTS" = "true" ]; then
  # "dashboard" when EXTRA_ARGS carries a scoped single-issue run (the
  # chat tool's alias+issue_iid pair); otherwise indistinguishable from a
  # scheduled run, same as before this migration.
  if [[ ${#EXTRA_ARGS[@]} -eq 2 ]]; then
    RUN_TRIGGER="dashboard"
  else
    RUN_TRIGGER="scheduled"
  fi
  python3 bin/events.py emit --type run.started --run-id "$RUN_ID" \
    --data "{\"trigger\": \"$RUN_TRIGGER\"}" || true
fi

python3 bin/web/dashboard_server.py write-status running --loop "$LOOP_NAME"

RUNNER_ARGS=("$RUN_ID" "${EXTRA_ARGS[@]+"${EXTRA_ARGS[@]}"}")
SERIALIZED_RUNNER_CMD="$(printf '%q ' python3 "$ENTRY_SCRIPT" "${RUNNER_ARGS[@]}")"
zsh -i -l -c "timeout $TIMEOUT_SECONDS $SERIALIZED_RUNNER_CMD"

python3 bin/web/dashboard_server.py write-status idle --loop "$LOOP_NAME" --exit-code 0
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- $LOOP_NAME ---- run finished (exit 0) ----" >> "$UNIFIED_LOG"
