#!/usr/bin/env bash
set -euo pipefail

if [ "$#" -ne 4 ]; then
  echo "Usage: new_worktree.sh <repo_path> <default_branch> <issue_iid> <worktree_root>" >&2
  exit 1
fi

REPO_PATH="$1"
DEFAULT_BRANCH="$2"
ISSUE_IID="$3"
WORKTREE_ROOT="$4"

BRANCH="loop/issue-$ISSUE_IID"
REPO_NAME="$(basename "$REPO_PATH")"
WORKTREE_PATH="$WORKTREE_ROOT/$REPO_NAME-issue-$ISSUE_IID"

mkdir -p "$WORKTREE_ROOT"

# Always pull the latest default branch before starting or continuing any
# issue's work, so a fix is never built on stale code.
# Git's own progress/status messages for these commands can land on stdout
# (e.g. "branch 'x' set up to track ...", "HEAD is now at ..."); redirect
# them to stderr so stdout carries only the final worktree path below.
git -C "$REPO_PATH" fetch origin "$DEFAULT_BRANCH" >&2

if git -C "$REPO_PATH" show-ref --verify --quiet "refs/heads/$BRANCH"; then
  if [ ! -d "$WORKTREE_PATH" ]; then
    git -C "$REPO_PATH" worktree add "$WORKTREE_PATH" "$BRANCH" >&2
  fi
  # A follow-up run on an issue that already has a branch/MR: bring it up to
  # date with the latest default branch before any new work happens on it.
  # Merging into a dirty worktree would either fail halfway or silently mix
  # leftover uncommitted edits into the sync, so refuse outright instead.
  if [ -n "$(git -C "$WORKTREE_PATH" status --porcelain)" ]; then
    echo "Worktree has uncommitted changes, refusing to sync: $WORKTREE_PATH" >&2
    exit 1
  fi
  # On conflict, abort the merge before bailing out. Leaving MERGE_HEAD behind
  # would wedge the worktree: every later run would find it mid-merge and fail
  # in a much more confusing way than a clean "conflict, aborted".
  git -C "$WORKTREE_PATH" merge --no-edit "origin/$DEFAULT_BRANCH" >&2 || {
    git -C "$WORKTREE_PATH" merge --abort
    echo "Merge conflict pulling in $DEFAULT_BRANCH, aborted: $WORKTREE_PATH" >&2
    exit 1
  }
else
  git -C "$REPO_PATH" worktree add -b "$BRANCH" "$WORKTREE_PATH" "origin/$DEFAULT_BRANCH" >&2
fi

echo "$WORKTREE_PATH"
