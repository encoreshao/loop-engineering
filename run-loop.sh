#!/usr/bin/env bash
set -euo pipefail

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$LOOP_DIR/outputs/history"
UNIFIED_LOG_DIR="$LOOP_DIR/logs"
UNIFIED_LOG="$UNIFIED_LOG_DIR/loop-engineering.log"
DATE_STAMP="$(date +%F)"

# A stable identity for this run, threaded through every event emitted
# below and (via the exported env var) every issue/verification event the
# agent itself emits per LOOPX_INSTRUCTIONS.md - see
# docs/superpowers/specs/2026-09-04-event-system-design.md. Generated here
# rather than left to the agent because this is the one place a real,
# collision-free timestamp is cheap and deterministic.
RUN_ID="run_$(date -u +%Y%m%d_%H%M%S)"
export LOOP_RUN_ID="$RUN_ID"

mkdir -p "$LOG_DIR" "$UNIFIED_LOG_DIR"

# Redirect all output (stdout+stderr) for the rest of this script to the
# per-run dated log. This must happen before anything else that could fail
# (config lookups, cd) so failures are actually captured in outputs/history/
# rather than vanishing.
exec >> "$LOG_DIR/$DATE_STAMP.log" 2>&1

# logs/loop-engineering.log is the one place every `claude` CLI invocation
# across this project logs to (see bin/web/dashboard_server.py's
# append_unified_log/render_logs_page) - written directly with `>>` here
# rather than folded into the exec redirect above, so this stays a plain,
# synchronous append with no risk of the last few lines being lost to an
# unflushed background `tee` at process exit (see the claude invocation's
# own tee -a calls below for why that risk is real and how it's avoided there).
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- gitlab-loop ---- run started ----" >> "$UNIFIED_LOG"

cd "$LOOP_DIR"

# A scheduled run has nobody watching it, so any non-zero exit must announce
# itself in Slack rather than dying silently in the log. slack_notify.py and
# dashboard_server.py are both stdlib-only, so plain `python3` works even
# under launchd's minimal PATH. Each command is guarded with `|| true` so a
# failure in the notify/status-write itself can't cause a second ERR trap.
trap 'loop_exit=$?; echo "[$(date "+%Y-%m-%d %H:%M:%S")] ---- gitlab-loop ---- run FAILED (exit $loop_exit) ----" >> "$UNIFIED_LOG"; python3 bin/slack_notify.py "*Daily GitLab loop FAILED* (exit $loop_exit) — see outputs/history/$DATE_STAMP.log" || true; python3 bin/web/dashboard_server.py write-status failed --exit-code $loop_exit || true; python3 bin/events.py emit --type run.failed --run-id "$RUN_ID" --data "{\"exit_code\": $loop_exit}" || true' ERR

# Emit run.started as early as possible - right after the ERR trap above is
# armed, and before anything below that can actually fail under
# `set -euo pipefail` (starting with WORKTREE_ROOT=... further down, this
# script's most common real failure point). Emitting any later risks a
# run.failed with no matching run.started, if one of those earlier commands
# trips the trap first. $# is this script's own positional args, already
# available here regardless of where in the script it's read.
#
# "dashboard" when the dashboard's run-issue chat action scoped this run to
# one issue (build_run_prompt.sh takes the same $@ and branches on it the
# same way); otherwise this script's normal invocation path is launchd, and
# a manual terminal run is indistinguishable from a scheduled one - that
# distinction isn't needed for anything downstream, so no new flag is added
# just to make it.
if [[ $# -eq 2 ]]; then
  RUN_TRIGGER="dashboard"
else
  RUN_TRIGGER="scheduled"
fi
python3 bin/events.py emit --type run.started --run-id "$RUN_ID" \
  --data "{\"trigger\": \"$RUN_TRIGGER\"}" || true

# NOTE: this script deliberately does NOT source ~/.zprofile / ~/.zshrc.
# Those are zsh files containing zsh-only syntax (`typeset -g`, subscript
# flags like `$precmd_functions[(r)...]` from pyenv-virtualenv-init and the
# direnv hook). Sourcing them from bash aborts partway through .zshrc, before
# the line that puts `claude` on PATH — so under launchd's minimal
# environment `claude` was never found and the run died with exit 127.
# Instead, the claude invocation below is delegated to a real interactive
# login zsh, which parses its own rc files correctly and sets up PATH itself.
#
# The bin/*.py helpers still called directly from this script
# (dashboard_server.py, events.py, slack_notify.py) are all stdlib-only, so
# they run fine under plain `python3` (/usr/bin/python3) with launchd's
# minimal PATH — they don't need pyenv. The projects.json/ai_cli.json config
# lookups that used to live here moved into bin/gitlab_loop_runner.py, which
# runs inside the login zsh below.

# Only the loop directory and the worktree root are exposed to
# Read/Edit/Write for the agent's own session; the actual CLI invocation,
# ALLOWED_TOOLS/DISALLOWED_TOOLS enforcement, and per-issue cost
# extraction all moved into bin/gitlab_loop_runner.py, which now runs one
# issue at a time through LoopRuntime - see
# docs/superpowers/specs/2026-09-07-gitlab-loop-runtime-wiring-design.md.
# Only the outer PATH-resolution trick (delegating to a real interactive
# login zsh, see the NOTE above) and the outer whole-batch timeout stay
# here; gitlab_loop_runner.py's own per-issue stop_conditions are a
# second, finer-grained budget nested inside this one.
#
# The outer timeout is 21600s (6 hours) and is NOT the primary safety
# mechanism any more. The real, enforced per-step bounds now live in
# gitlab_loop_runner.py: 1800s per issue (loops/gitlab-issue/loop.yaml's
# max_runtime_minutes: 30) plus 1800s for the end-of-run wrap-up call.
# This one is only a secondary sanity net against a pathological hang at
# the process/script level (a wedged login zsh, an unkillable child).
# It has to be generous for a reason: it used to be 3600s, which two slow
# issues alone (2 x 1800s) could exhaust - and when this timeout trips,
# gitlab_loop_runner.py is killed before its unconditional
# `--batch-end-of-run` wrap-up call, silently losing the day's
# digest/daily-review entirely. 6 hours leaves room for a busy day (a
# double-digit issue count, each taking its full per-issue budget) to
# still reach the wrap-up.
python3 bin/web/dashboard_server.py write-status running

RUNNER_ARGS=("$RUN_ID" "$@")
SERIALIZED_RUNNER_CMD="$(printf '%q ' python3 "$LOOP_DIR/bin/gitlab_loop_runner.py" "${RUNNER_ARGS[@]}")"
zsh -i -l -c "timeout 21600 $SERIALIZED_RUNNER_CMD"

# Reaching here means gitlab_loop_runner.py exited zero for the whole
# batch (a non-zero exit would have tripped `set -e` and the ERR trap
# above instead) - per-issue detail (success/failure/cost) lives in each
# issue's own LoopResult under outputs/loop-runs/.
#
# run.completed is deliberately NOT emitted here any more:
# gitlab_loop_runner.py's main() emits it itself, because only it holds the
# real aggregated cost (one CLI call per issue now, not one per run) that
# bin/cost.py's report reads from data.cost_usd. Emitting it here too would
# double-count the run in bin/metrics.py's/bin/cost.py's run_id aggregation.
python3 bin/web/dashboard_server.py write-status idle --exit-code 0
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- gitlab-loop ---- run finished (exit 0) ----" >> "$UNIFIED_LOG"
