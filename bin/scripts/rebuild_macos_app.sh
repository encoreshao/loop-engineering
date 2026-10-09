#!/usr/bin/env bash
# One command to pick up new changes: fast-forward this checkout, rebuild
# "Loop X.app" + "Loop X.dmg" into dist/, then relaunch the app.
#
# Usage: rebuild_macos_app.sh [--no-pull] [--no-launch] [build_macos_app.sh options...]
# Extra options (e.g. --output-dir DIR, --skip-venv) go to build_macos_app.sh.
# Nothing here touches ~/.loop-engineering or the launchd agents; restart the
# dashboard daemon separately (restart-daemons.sh) if it should serve the new code too.
set -euo pipefail

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PULL=1
LAUNCH=1
BUILD_ARGS=()
OUTPUT_DIR="$LOOP_DIR/dist"

while [ $# -gt 0 ]; do
  case "$1" in
    --no-pull) PULL=0; shift ;;
    --no-launch) LAUNCH=0; shift ;;
    --output-dir) OUTPUT_DIR="$2"; BUILD_ARGS+=("$1" "$2"); shift 2 ;;
    *) BUILD_ARGS+=("$1"); shift ;;
  esac
done

if [ "$PULL" -eq 1 ]; then
  git -C "$LOOP_DIR" pull --ff-only
fi

"$LOOP_DIR/bin/scripts/build_macos_app.sh" --dmg ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"}

if [ "$LAUNCH" -eq 1 ]; then
  osascript -e 'tell application "Loop X" to quit' >/dev/null 2>&1 || true
  sleep 1
  open "$OUTPUT_DIR/Loop X.app"
fi
