#!/usr/bin/env bash
set -euo pipefail

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$LOOP_DIR/outputs/history"
UNIFIED_LOG_DIR="$LOOP_DIR/logs"
UNIFIED_LOG="$UNIFIED_LOG_DIR/loop-engineering.log"
DATE_STAMP="$(date +%F)"

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

# Writes are meant to be confined to outputs/topic-monitor/ - this loop
# never touches source code, project checkouts, or git in any way. It is
# also the only loop here whose input is untrusted web content
# (WebSearch/WebFetch), it runs under --permission-mode acceptEdits with
# nobody approving each action, and everything it could reach includes
# LOOPX_INSTRUCTIONS.md and run-loop.sh - the far higher-privilege GitLab
# loop's own control files (git push, GitLab API tokens). A prompt
# injection in a fetched page must not be able to rewrite those.
#
# Three things about how that confinement is actually achieved, each
# verified by running the real CLI rather than assumed (see the
# "Tool permissions policy" section of TOPIC_MONITOR_INSTRUCTIONS.md):
#
#   1. NOT via --add-dir. --add-dir only *adds* directories to the
#      workspace. The agent's cwd is already $LOOP_DIR (the `cd` above),
#      so `--add-dir $LOOP_DIR/outputs/topic-monitor` was a pure no-op:
#      it granted nothing and restricted nothing.
#   2. NOT via the allow list either. An allow rule grants; it never
#      revokes. Scoping the grant to Edit(**/outputs/topic-monitor/**)
#      documents the intent, but a path no rule mentions is still
#      writable (under acceptEdits, and under this machine's own
#      ~/.claude/settings.json, which allows Read/Write globally).
#   3. So the boundary IS the deny list below. Deny beats every allow,
#      local or global, which makes it the only rule kind here that can
#      actually stop a write.
#
# Note the tool names: file permission rules match on Read(...) and
# Edit(...) only - an `Edit(...)` rule covers ALL file-editing tools,
# Write included. A `Write(path)` rule matches nothing and the CLI prints
# a warning about it, so there are deliberately none here.
ALLOWED_TOOLS="Read(**/outputs/topic-monitor/**) Edit(**/outputs/topic-monitor/**) \
Read(**/TOPIC_MONITOR_INSTRUCTIONS.md) Read(**/docs/tasks/topic-monitor-loop.md) \
WebSearch WebFetch \
Bash(cd *) \
Bash(python3 bin/topic_config.py*) Bash(python3 bin/topic_seen.py*) \
Bash(python3 bin/slack_notify.py*) Bash(python3 bin/web/dashboard_server.py*) \
Bash(python3 $LOOP_DIR/bin/topic_config.py*) Bash(python3 $LOOP_DIR/bin/topic_seen.py*) \
Bash(python3 $LOOP_DIR/bin/slack_notify.py*) Bash(python3 $LOOP_DIR/bin/web/dashboard_server.py*)"

# The real write boundary (see 3. above). Every **/-prefixed pattern below
# is anchored to the run's working directory (this script `cd`s to
# $LOOP_DIR above) - it does NOT match an absolute path outside it. Each
# pattern is a path shape that cannot occur under outputs/topic-monitor/,
# whose only tool-written files are the briefings at
# history/<date>-<topic>.md - so none of these can block the loop's own
# work:
#   - by extension: no .sh/.py/.plist/.json/.yml/.toml is ever written
#     there (the seen-items state JSON is written by topic_seen.py in its
#     own subprocess, which tool rules don't apply to). This is what
#     covers run-loop.sh, run-topic-monitor-loop.sh and every plist under
#     this repo's own launchd/ - but, precisely because it's cwd-anchored,
#     it does NOT cover the INSTALLED copy of the GitLab loop's schedule
#     at ~/Library/LaunchAgents/com.hermes.loop-engineering.plist, which
#     sits outside $LOOP_DIR entirely.
#   - by directory: none of these directories exist under it.
#   - by name: this repo's root markdown files, listed individually since
#     the briefings are markdown too and a blanket **/*.md would block
#     the run itself.
#   - the GitLab loop's own outputs, which live beside this loop's.
#
# ~/Library/LaunchAgents/ is covered by the one absolute-path rule below
# instead (Edit(/$HOME/Library/LaunchAgents/**) - leading `/`, not `**/`).
# Absolute patterns are NOT cwd-anchored, which is exactly why this rule
# needs that form: without it, a prompt injection from fetched web content
# could rewrite that installed plist's ProgramArguments and get arbitrary
# code execution on the machine's own schedule. Verified against the real
# CLI in a scratch replica: this rule denies writes under the fake
# LaunchAgents path while leaving outputs/topic-monitor/** untouched.
DISALLOWED_TOOLS="Bash(git*) \
Read(**/.env*) Read(**/*.key) Read(**/id_rsa*) Read(**/.ssh/**) \
Edit(**/*.sh) Edit(**/*.py) Edit(**/*.plist) Edit(**/*.json) Edit(**/*.yml) Edit(**/*.yaml) Edit(**/*.toml) \
Edit(**/bin/**) Edit(**/launchd/**) Edit(**/docs/**) Edit(**/config/**) Edit(**/tests/**) Edit(**/assets/**) \
Edit(**/.claude/**) Edit(**/.git/**) Edit(**/.ssh/**) \
Edit(**/LOOPX_INSTRUCTIONS.md) Edit(**/TOPIC_MONITOR_INSTRUCTIONS.md) Edit(**/CLAUDE.md) \
Edit(**/README.md) Edit(**/TASK.md) Edit(**/PROGRESS.md) \
Edit(**/outputs/history/**) Edit(**/outputs/daily-review.md) \
Edit(/$HOME/Library/LaunchAgents/**)"

PROMPT="Follow TOPIC_MONITOR_INSTRUCTIONS.md in $LOOP_DIR exactly. This is a scheduled headless run - there is no user available to answer questions."

# Which AI CLI to invoke - same reasoning as run-loop.sh's own comment on
# this; same global setting, same caveat about Codex's coarser sandbox
# model (TOPIC_MONITOR_INSTRUCTIONS.md's "Tool permissions policy"
# section).
AI_CLI="$(python3 bin/ai_cli_config.py get)"

# codex exec has no --ask-for-approval (top-level `codex`-only) and is
# already non-interactive by default - see run-loop.sh's own comment on
# its equivalent codex invocation for how this was verified against the
# real `codex exec --help`. This loop's job is WebSearch/WebFetch, so
# workspace-write's default no-network sandbox and codex exec's default
# disabled web-search tool both need explicit -c overrides too.
case "$AI_CLI" in
  codex)
    CLI_CMD=(codex exec --sandbox workspace-write \
      -c approval_policy=never \
      -c sandbox_workspace_write.network_access=true \
      -c tools.web_search=true \
      "$PROMPT")
    ;;
  *)
    CLI_CMD=(claude -p --permission-mode acceptEdits --allowedTools "$ALLOWED_TOOLS" --disallowedTools "$DISALLOWED_TOOLS" --output-format text "$PROMPT")
    ;;
esac
SERIALIZED_CMD="$(printf '%q ' "${CLI_CMD[@]}")"

# See run-loop.sh's own comment on its equivalent claude invocation line:
# a plain foreground pipeline (not `exec > >(...)`), so the shell actually
# waits for tee to finish flushing before the script can exit; pipefail
# (already in `set -euo pipefail` above) keeps the pipeline's exit status
# reflecting zsh's, not tee's, so `set -e`/the ERR trap still fire on a
# real failure exactly as before this line existed.
zsh -i -l -c "timeout 3600 $SERIALIZED_CMD" | tee -a "$UNIFIED_LOG"

# Reaching here means the delegated command exited zero (a non-zero exit
# would have tripped `set -e` and the ERR trap above instead).
echo "[$(date '+%Y-%m-%d %H:%M:%S')] ---- topic-monitor ---- run finished (exit 0) ----" >> "$UNIFIED_LOG"
