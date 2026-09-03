#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "Usage: open_merge_request.sh <repo_path> <branch> <target_branch> <title>" >&2
  exit 1
fi

REPO_PATH="$1"
BRANCH="$2"
TARGET_BRANCH="$3"
TITLE="$4"

# Hard guard on the "never push to the target branch" rule. The loop is only
# ever allowed to push its own issue branches; anything else (staging, main, a
# typo, an unexpanded placeholder) is refused here rather than relying on the
# agent to follow prose.
if [[ "$BRANCH" != loop/issue-* ]]; then
  echo "Refusing to push non-loop branch: $BRANCH" >&2
  exit 1
fi

CMD=(git -C "$REPO_PATH" push origin "$BRANCH"
  -o merge_request.create
  -o "merge_request.target=$TARGET_BRANCH"
  -o "merge_request.title=$TITLE"
  -o merge_request.remove_source_branch)

if [ "${DRY_RUN:-0}" = "1" ]; then
  printf '%s\n' "${CMD[@]}"
else
  "${CMD[@]}"
fi
