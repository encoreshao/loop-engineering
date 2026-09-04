#!/usr/bin/env bash
set -euo pipefail

# Prints the PROMPT run-loop.sh should hand to claude -p/codex exec, to
# stdout - kept in its own script (rather than inlined in run-loop.sh) so
# it's testable via a real subprocess call instead of by parsing
# run-loop.sh itself. Called with no args: the normal scheduled/on-demand
# run-now prompt (process every assigned issue). Called with exactly two
# args (project alias, issue IID): the single-issue prompt used when the
# dashboard's Activity chat launches a scoped run for one pasted issue
# link - see LOOPX_INSTRUCTIONS.md and this repo's chat-tool run-issue
# action (bin/web/dashboard_server.py).

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $# -eq 0 ]]; then
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly. This is a scheduled headless run - there is no user available to answer questions, so escalate via GitLab comment instead of asking."
elif [[ $# -eq 2 ]]; then
  ISSUE_ALIAS="$1"
  ISSUE_IID="$2"
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly, except skip Step 1 (listing assigned issues) entirely. This is an on-demand single-issue run triggered from the dashboard's Activity chat, not the scheduled batch. Process exactly one issue: project alias '$ISSUE_ALIAS', issue IID $ISSUE_IID - regardless of who it is assigned to. Look up its config via \`loop_config.py project $ISSUE_ALIAS\`, then follow Step 2's per-issue procedure onward (sync/comments, analyze, fix-and-MR or answer or escalate), then 'End of run', reporting on just this one issue. This is still a headless run with no user available to answer questions, so escalate via GitLab comment instead of asking."
else
  echo "Usage: build_run_prompt.sh [alias issue_iid]" >&2
  exit 1
fi
