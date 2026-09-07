#!/usr/bin/env bash
set -euo pipefail

# Prints the PROMPT the caller should hand to claude -p/codex exec, to
# stdout - kept in its own script (rather than inlined in its callers) so
# it's testable via a real subprocess call instead of by parsing them.
#
# Four calling conventions:
#
#   (no args)                        The legacy whole-batch prompt: one
#                                    agent session does discovery (Step 1),
#                                    every issue (Step 2), and "End of run"
#                                    once. NOTHING CALLS THIS ANY MORE -
#                                    discovery moved into Python
#                                    (bin/gitlab_loop_runner.py's
#                                    run_all_issues), which now invokes the
#                                    two --batch-* modes below instead.
#                                    Kept as a documented reference / manual
#                                    escape hatch for reproducing the old
#                                    single-session behavior by hand.
#
#   <alias> <issue_iid>              The dashboard's on-demand single-issue
#                                    prompt, used when the Activity chat
#                                    launches a scoped run for one pasted
#                                    issue link (see LOOPX_INSTRUCTIONS.md
#                                    and chat-tool run-issue in
#                                    bin/web/dashboard_server.py). Skips
#                                    Step 1, processes that one issue, and
#                                    does its own full "End of run" - this
#                                    is a run of exactly one issue, so the
#                                    digest/daily-review it writes IS the
#                                    whole run's report. Unchanged.
#
#   --batch-issue <alias> <iid>      One issue inside a scheduled batch.
#                                    Same as the two-arg mode EXCEPT it
#                                    explicitly forbids "End of run" - the
#                                    batch has N of these, and the
#                                    once-per-run digest/daily-review is
#                                    handled separately, below.
#
#   --batch-end-of-run               The batch's single wrap-up call. Skips
#                                    Steps 1 and 2 entirely and does ONLY
#                                    "End of run", reconstructing what the
#                                    per-issue calls did from the event log
#                                    (this is a fresh session with no memory
#                                    of them). Called unconditionally once
#                                    per scheduled run, including on a
#                                    morning with zero assigned issues -
#                                    that's what preserves
#                                    LOOPX_INSTRUCTIONS.md's "a quiet
#                                    morning is still reported" guarantee.

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $# -eq 3 && "$1" == "--batch-issue" ]]; then
  ISSUE_ALIAS="$2"
  ISSUE_IID="$3"
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly, except skip Step 1 (listing assigned issues) entirely. This is ONE issue inside a scheduled batch run: the batch's other issues are each processed by their own separate invocation like this one. Process exactly one issue: project alias '$ISSUE_ALIAS', issue IID $ISSUE_IID. Look up its config via \`loop_config.py project $ISSUE_ALIAS\`, then follow Step 2's per-issue procedure onward (sync/comments, analyze, fix-and-MR or answer or escalate), including every events.py emit call it specifies. STOP as soon as that one issue's own procedure is complete: do NOT do LOOPX_INSTRUCTIONS.md's 'End of run' section at all - no outputs/daily-review.md, no outputs/history/<date>.md, no PROGRESS.md update, and no end-of-run Slack digest. A separate \`build_run_prompt.sh --batch-end-of-run\` invocation does 'End of run' exactly once for the whole batch, after every issue has been processed. This is a scheduled headless run - there is no user available to answer questions, so escalate via GitLab comment instead of asking."
elif [[ $# -eq 1 && "$1" == "--batch-end-of-run" ]]; then
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly, except skip Step 1 (listing assigned issues) and Step 2 (processing issues) entirely - every issue in this run was already processed by its own separate prior invocation. Your ONLY job is LOOPX_INSTRUCTIONS.md's 'End of run' section, covering the whole batch. This is a fresh agent session with no memory of what those prior invocations did, so first reconstruct this run's outcomes from the event log: read $LOOP_DIR/outputs/events/<today's UTC date, YYYY-MM-DD>.jsonl and keep only the entries whose \"run_id\" field equals \$LOOP_RUN_ID (already exported into your environment; the file may not exist at all, which simply means no issues were processed this run). Each entry's \"project\" is the project alias and \"issue_iid\" is the issue. The event types that matter: issue.started (this issue was checked), issue.completed with data.action == \"fix\" (a fix, with the merge request at data.mr_url) or data.action == \"answer\" (answered directly with a GitLab comment, no code change), and issue.escalated with data.reason == \"needs_clarification\" (escalated for clarification) or \"verification_failed\"/\"worktree_creation_failed\" (escalated because verification could not pass). Reconstruct daily-review.md's seven sections - Summary, Issues checked, New comments found, MRs opened, Answered directly, Escalations, No-ops - from those events, applying the same judgment you would normally apply live. An issue that has an issue.started for this run_id but no issue.completed or issue.escalated crashed part-way through: it still counts as checked, and belongs under Escalations as needing human follow-up - say so plainly in the Summary rather than silently dropping it. Then do exactly steps 1-4 of 'End of run' based on that reconstruction: write outputs/daily-review.md, copy it to outputs/history/<YYYY-MM-DD>.md, update PROGRESS.md, and send the end-of-run Slack digest. Send that digest unconditionally, even when every count is zero (no matching events at all means no assigned issues today - report exactly that). This is a scheduled headless run - there is no user available to answer questions."
elif [[ $# -gt 0 && "$1" == --* ]]; then
  # A `--`-prefixed first argument is always a mode flag, never a project
  # alias. Rejected here rather than falling through to the branches below,
  # so a mistyped/miscounted flag (e.g. `--batch-issue harbor`, missing the
  # IID) can't be silently reinterpreted as the two-arg dashboard mode with
  # "--batch-issue" as the alias.
  echo "Usage: build_run_prompt.sh [alias issue_iid | --batch-issue alias issue_iid | --batch-end-of-run]" >&2
  exit 1
elif [[ $# -eq 0 ]]; then
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly. This is a scheduled headless run - there is no user available to answer questions, so escalate via GitLab comment instead of asking."
elif [[ $# -eq 2 ]]; then
  ISSUE_ALIAS="$1"
  ISSUE_IID="$2"
  echo "Follow LOOPX_INSTRUCTIONS.md in $LOOP_DIR exactly, except skip Step 1 (listing assigned issues) entirely. This is an on-demand single-issue run triggered from the dashboard's Activity chat, not the scheduled batch. Process exactly one issue: project alias '$ISSUE_ALIAS', issue IID $ISSUE_IID - regardless of who it is assigned to. Look up its config via \`loop_config.py project $ISSUE_ALIAS\`, then follow Step 2's per-issue procedure onward (sync/comments, analyze, fix-and-MR or answer or escalate), then 'End of run', reporting on just this one issue. This is still a headless run with no user available to answer questions, so escalate via GitLab comment instead of asking."
else
  echo "Usage: build_run_prompt.sh [alias issue_iid | --batch-issue alias issue_iid | --batch-end-of-run]" >&2
  exit 1
fi
