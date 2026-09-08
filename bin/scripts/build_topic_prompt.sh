#!/usr/bin/env bash
set -euo pipefail

# Prints the PROMPT run-topic-monitor-loop.sh (via
# bin/topic_monitor_runner.py) should hand to claude -p/codex exec, to
# stdout - kept in its own script (rather than inlined) so it's testable
# via a real subprocess call. Called with no args: NOTHING CALLS THIS ANY
# MORE once the LoopRuntime migration is live - kept as a documented
# reference / manual escape hatch for reproducing the old single-session,
# all-topics-in-one-call behavior by hand (same judgment call already made
# for bin/scripts/build_run_prompt.sh's own 0-arg legacy mode). Called
# with exactly one arg (topic name): the real path every scheduled run
# uses now, one call per configured topic.

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [[ $# -eq 0 ]]; then
  echo "Follow TOPIC_MONITOR_INSTRUCTIONS.md in $LOOP_DIR exactly. This is a scheduled headless run - there is no user available to answer questions."
elif [[ $# -eq 1 ]]; then
  TOPIC_NAME="$1"
  echo "Follow TOPIC_MONITOR_INSTRUCTIONS.md in $LOOP_DIR exactly, except skip Step 1 (listing today's topics) entirely. Process exactly one topic: '$TOPIC_NAME'. Look up its details via \`python3 $LOOP_DIR/bin/topic_config.py topic $TOPIC_NAME\`, then follow Step 2's per-topic procedure for just this topic and stop. This run's own verification checklist applies only to this one topic, not the full topic list: do NOT check whether every configured topic has a briefing or a terminal status - the other topics are other sessions' work and are not yours to verify. This is still a headless run with no user available to answer questions."
else
  echo "Usage: build_topic_prompt.sh [topic_name]" >&2
  exit 1
fi
