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
# loop_config.py is stdlib-only (json, pathlib, sys), so the lookups below run
# fine under plain `python3` (/usr/bin/python3) with launchd's minimal PATH —
# they don't need pyenv.

# Only the loop directory and the worktree root are exposed to Read/Edit/Write.
# The projects' primary checkouts (`local_path`) are deliberately NOT add-dir'd:
# every file edit is supposed to happen inside a per-issue worktree, so leaving
# them out mechanically enforces "never edit files in <local_path> directly"
# instead of trusting the agent to follow prose.
#
# add-dir only scopes the Read/Edit/Write/Glob/Grep tools; it does not filter
# paths named inside a Bash command. So a Bash command may still *name*
# <local_path> where the ALLOWED_TOOLS patterns below permit it — e.g.
# `bash $LOOP_DIR/bin/scripts/open_merge_request.sh <local_path> loop/issue-N ...`,
# which passes the checkout as an argument and runs git -C against it inside
# the script. (Note that a *direct* `git -C <local_path> ...` is NOT an
# example of this: the git patterns below are literal-prefix matches on
# `git status`/`git diff`/`git add`/`git commit`/`git push origin loop/issue-`,
# none of which a string starting `git -C` matches, so it is denied.)
WORKTREE_ROOT="$(python3 bin/loop_config.py worktree-root)"
ADD_DIR_ARGS=(--add-dir "$LOOP_DIR" --add-dir "$WORKTREE_ROOT")

# The actually-enforced permission list. LOOPX_INSTRUCTIONS.md's "Tool
# permissions policy" section describes the same policy in prose; the two must
# be kept in sync whenever either changes.
#
# git is enumerated per-subcommand rather than `Bash(git *)` so that "never
# merge, never push to the target branch" is enforced by the harness, not just
# by prose. git fetch/merge/worktree-add are not listed because they only run
# inside new_worktree.sh's own execution, which is already covered by allowing
# the outer `bash bin/scripts/new_worktree.sh` invocation.
#
# The bin/ scripts are listed in both relative and absolute form: the agent
# starts in $LOOP_DIR but spends most of the run cd'd into a worktree. Three
# separate patterns per form because bin/'s contents are split by kind (see
# CLAUDE.md): the loop's own Python helpers directly in bin/, the dashboard
# web server in bin/web/, and one-shot shell scripts in bin/scripts/ - a
# glob's `*` doesn't cross a `/`, so each directory needs its own pattern.
ALLOWED_TOOLS="Read Edit Write \
Bash(git status*) Bash(git diff*) Bash(git add*) Bash(git commit*) Bash(git push origin loop/issue-*) \
Bash(cd *) \
Bash(RAILS_ENV=test bundle exec rspec*) Bash(bundle exec rspec*) Bash(bundle exec rubocop*) \
Bash(bundle check*) Bash(bundle install*) Bash(RAILS_ENV=test bundle exec rake db:test:prepare*) \
Bash(npm run test*) Bash(npm run lint*) Bash(npm ci*) Bash(yarn install*) \
Bash(python3 *gitlab_api.py*) Bash(python3 *gitlab_cache.py*) \
Bash(python3 bin/*.py*) Bash(python3 bin/web/*.py*) Bash(bash bin/scripts/*.sh*) \
Bash(python3 $LOOP_DIR/bin/*.py*) Bash(python3 $LOOP_DIR/bin/web/*.py*) Bash(bash $LOOP_DIR/bin/scripts/*.sh*)"

# Defense in depth: even if an allow pattern were ever loosened by accident,
# these can never run.
DISALLOWED_TOOLS="Bash(git merge*) Bash(git push --force*) Bash(git push -f*) Bash(git checkout*) Bash(git reset*) Bash(git clean*) Read(**/.env*) Read(**/*.key) Read(**/id_rsa*)"

# Optionally called as `run-loop.sh <alias> <issue_iid>` (from the
# dashboard's chat-tool run-issue action) to scope this run to exactly
# one issue instead of every assigned issue - build_run_prompt.sh takes
# the same $@ and branches on it (see the RUN_TRIGGER detection above,
# which reads the same $#).
PROMPT="$(bash "$LOOP_DIR/bin/scripts/build_run_prompt.sh" "$@")"

# Which AI CLI to invoke - set via the dashboard's /ai-cli page (see
# bin/ai_cli_config.py), defaulting to "claude". Codex's --sandbox/-c
# config overrides are coarser than Claude's per-command
# --allowedTools/--disallowedTools (see LOOPX_INSTRUCTIONS.md's "Tool
# permissions policy" section for what that means for this loop's
# guardrails when Codex is selected).
AI_CLI="$(python3 bin/ai_cli_config.py get)"

# codex exec (unlike top-level `codex`) has no --ask-for-approval and no
# --add-dir at all - both are rejected outright with "unexpected argument"
# (verified against the real `codex exec --help`). `codex exec` is
# already non-interactive by default, so there's nothing to replace
# --ask-for-approval never with except the equivalent -c override,
# `-c approval_policy=never`, kept for explicitness. The two --add-dir
# roots become a `-c sandbox_workspace_write.writable_roots=[...]`
# override instead, and workspace-write's sandbox has no network access by
# default, so `-c sandbox_workspace_write.network_access=true` is added
# too - this loop needs `git push`/GitLab API calls to work.
CODEX_WRITABLE_ROOTS="[\"$LOOP_DIR\",\"$WORKTREE_ROOT\"]"

case "$AI_CLI" in
  codex)
    CLI_CMD=(codex exec --sandbox workspace-write \
      -c approval_policy=never \
      -c "sandbox_workspace_write.writable_roots=$CODEX_WRITABLE_ROOTS" \
      -c sandbox_workspace_write.network_access=true \
      "$PROMPT")
    ;;
  *)
    CLI_CMD=(claude -p "${ADD_DIR_ARGS[@]}" --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" --disallowedTools "$DISALLOWED_TOOLS" --output-format json "$PROMPT")
    ;;
esac

# Build the command as an array, then serialize it with %q so that every
# argument (including the multi-word prompt and the space-separated tool
# lists) survives being handed to zsh as a single string.
SERIALIZED_CMD="$(printf '%q ' "${CLI_CMD[@]}")"

# Record the run's state for the dashboard (bin/web/dashboard_server.py) before
# handing off to the long-running claude invocation, so a browser check
# mid-run shows "running" rather than stale leftover state from last time.
python3 bin/web/dashboard_server.py write-status running

# `timeout` goes INSIDE the zsh command string, not in front of `zsh`.
# On this machine timeout is /opt/homebrew/bin/timeout, which is not on
# launchd's minimal PATH — running it from this bash context would die with
# exit 127, exactly the failure this rewrite exists to fix. Inside the login
# zsh, PATH is already set up, so timeout resolves. Verified: a bare
# `timeout ... zsh ...` from a minimal PATH exits 127, while
# `zsh -i -l -c "timeout ..."` runs and still enforces the limit (exit 124).
#
# Captured to a temp file rather than piped through tee: the Claude branch's
# --output-format json (see above) means stdout is a single JSON blob that
# must stay parseable, not intermixed with tee's own buffering. Deliberately
# no `2>&1` on this redirect either - stderr keeps flowing to the script's
# own inherited fd2 (the exec redirect at the top of this script already
# sends it to outputs/history/$DATE_STAMP.log), so nothing is lost, it's
# just no longer duplicated into the unified log the way merged
# stdout+stderr used to be under the old `| tee` pipeline. Removing the pipe
# also removes any dependence on `pipefail` for this line: `$?` below
# reflects zsh's own exit code directly.
CLI_OUTPUT_FILE="$(mktemp "${TMPDIR:-/tmp}/loop-cli-output.XXXXXX")"
trap 'rm -f "$CLI_OUTPUT_FILE"' EXIT
zsh -i -l -c "timeout 3600 $SERIALIZED_CMD" > "$CLI_OUTPUT_FILE"

# Reaching here means the delegated command exited zero (a non-zero exit
# would have tripped `set -e` and the ERR trap above instead).
#
# Claude's --output-format json wraps the same final answer text
# (previously the entirety of stdout under --output-format text) inside a
# JSON envelope alongside usage/cost data - bin/cost.py extract-result-text
# pulls the text back out for the human-readable log, and usage-json pulls
# the cost data out for the run.completed event's --data. Codex is
# untouched: its own output format never changed, so its raw output is just
# appended to the unified log and run.completed stays bare, exactly as
# before this sprint - see
# docs/superpowers/specs/2026-09-04-cost-tracking-design.md for why Codex
# usage tracking isn't built yet.
if [[ "$AI_CLI" == "codex" ]]; then
  cat "$CLI_OUTPUT_FILE" | tee -a "$UNIFIED_LOG"
  RUN_USAGE_DATA=""
else
  python3 bin/cost.py extract-result-text --cli-output-file "$CLI_OUTPUT_FILE" | tee -a "$UNIFIED_LOG" || true
  RUN_USAGE_DATA="$(python3 bin/cost.py usage-json --cli-output-file "$CLI_OUTPUT_FILE" || true)"
fi

python3 bin/web/dashboard_server.py write-status idle --exit-code 0
if [[ -n "$RUN_USAGE_DATA" ]]; then
  python3 bin/events.py emit --type run.completed --run-id "$RUN_ID" --data "$RUN_USAGE_DATA" || true
else
  python3 bin/events.py emit --type run.completed --run-id "$RUN_ID" || true
fi
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- gitlab-loop ---- run finished (exit 0) ----" >> "$UNIFIED_LOG"
