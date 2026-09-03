#!/usr/bin/env bash
set -euo pipefail

# One-command setup for a new loop-engineering user (see
# docs/tasks/gitlab-issue-loop.md - every team member running this loop has
# their own local config). Installs this
# loop's one external dependency, the `gitlab-config` skill from
# encore-skills (github.com/encoreshao/encore-skills), and scaffolds all
# three per-machine config files from their templates if they don't exist
# yet: ~/.loop-engineering/projects.json (the GitLab issue loop),
# ~/.loop-engineering/topics.json (the topic monitor loop), and
# ~/.loop-engineering/ai_cli.json (which AI CLI both loops invoke). Check
# the dashboard's /skills page afterward for a live view of what's installed.

# Colored output, only when stdout is an actual terminal - never for a
# pipe/redirect (e.g. under a test harness). The color variables carry
# literal escape bytes ($'...' ANSI-C quoting), so referencing them works
# the same in echo, printf, and heredocs alike - no `echo -e` needed.
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

SKIP_SKILLS_INSTALL=0
CONFIG_PATH="$HOME/.loop-engineering/projects.json"
TOPICS_CONFIG_PATH="$HOME/.loop-engineering/topics.json"
AI_CLI_CONFIG_PATH="$HOME/.loop-engineering/ai_cli.json"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --skip-skills-install)
      SKIP_SKILLS_INSTALL=1
      shift
      ;;
    --config-path)
      CONFIG_PATH="$2"
      shift 2
      ;;
    --topics-config-path)
      TOPICS_CONFIG_PATH="$2"
      shift 2
      ;;
    --ai-cli-config-path)
      AI_CLI_CONFIG_PATH="$2"
      shift 2
      ;;
    *)
      echo "${C_RED}Usage: setup.sh [--skip-skills-install] [--config-path PATH] [--topics-config-path PATH] [--ai-cli-config-path PATH]${C_RESET}" >&2
      exit 1
      ;;
  esac
done

LOOP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

if [ "$SKIP_SKILLS_INSTALL" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping skills install (--skip-skills-install)${C_RESET}"
else
  echo "${C_BLUE}==> Installing the gitlab-config skill (encore-skills)...${C_RESET}"
  curl -fsSL https://raw.githubusercontent.com/encoreshao/encore-skills/main/scripts/setup.sh | bash -s -- --claude
fi

if [ -f "$CONFIG_PATH" ]; then
  echo "${C_BLUE}==> $CONFIG_PATH already exists, leaving it alone${C_RESET}"
else
  echo "${C_BLUE}==> Creating $CONFIG_PATH from the template${C_RESET}"
  mkdir -p "$(dirname "$CONFIG_PATH")"
  sed "s|{{HOME}}|$HOME|g" "$LOOP_DIR/config/projects.json.template" > "$CONFIG_PATH"
  echo "${C_YELLOW}    Edit it now: GitLab username and each project's path/branch/commands (worktree_root already defaults to \$HOME/.loop-engineering/worktrees).${C_RESET}"
fi

if [ -f "$TOPICS_CONFIG_PATH" ]; then
  echo "${C_BLUE}==> $TOPICS_CONFIG_PATH already exists, leaving it alone${C_RESET}"
else
  echo "${C_BLUE}==> Creating $TOPICS_CONFIG_PATH from the template${C_RESET}"
  mkdir -p "$(dirname "$TOPICS_CONFIG_PATH")"
  cp "$LOOP_DIR/config/topics.json.template" "$TOPICS_CONFIG_PATH"
  echo "${C_YELLOW}    Edit it now: which topics to monitor, and what counts as notable for each (only needed for the topic monitor loop).${C_RESET}"
fi

if [ -f "$AI_CLI_CONFIG_PATH" ]; then
  echo "${C_BLUE}==> $AI_CLI_CONFIG_PATH already exists, leaving it alone${C_RESET}"
else
  echo "${C_BLUE}==> Creating $AI_CLI_CONFIG_PATH from the template${C_RESET}"
  mkdir -p "$(dirname "$AI_CLI_CONFIG_PATH")"
  cp "$LOOP_DIR/config/ai_cli.json.template" "$AI_CLI_CONFIG_PATH"
  echo "${C_YELLOW}    Defaults to Claude - switch to Codex any time from the dashboard's AI CLI page.${C_RESET}"
fi

cat <<EOF

${C_GREEN}Setup done.${C_RESET} Next steps:
  1. Fill in $CONFIG_PATH
  2. Configure ~/.gitlab/config.json and ~/.slack/config.json (the dashboard's Settings page can do this)
  3. Run 'python3 $LOOP_DIR/bin/web/dashboard_server.py' and check its Skills page
EOF
