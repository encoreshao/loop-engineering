#!/usr/bin/env bash
set -euo pipefail

# Reverses everything bin/scripts/setup.sh and bin/scripts/setup-nginx.sh set up on this
# machine: unloads and removes this repo's launchd agents from
# ~/Library/LaunchAgents/, and removes the nginx reverse proxy config +
# /etc/hosts entry setup-nginx.sh added (if any - safe to run even if
# nginx was never set up). Idempotent: safe to re-run.
#
# Deliberately leaves alone anything shared with other tools rather than
# exclusive to this loop: ~/.gitlab/config.json, ~/.slack/config.json (read
# by the gitlab-config skill, not this repo), ~/.encore-skills (that skill's
# own install), and Homebrew's nginx itself. ~/.loop-engineering/ - which,
# with install.sh's default --dir, holds this repo's own cloned code as
# well as your personal config (projects.json, topics.json,
# instructions.md) - IS removed by default, since that's the whole point
# of an uninstall; pass --keep-config to leave it in place instead (e.g.
# you're about to reinstall).
#
# --project-dir is independent of where this uninstall.sh happens to be
# running from (LOOP_DIR below, used only to read this repo's own
# launchd/*.plist as source of truth when a local clone is available) -
# it defaults to the same fixed ~/.loop-engineering path install.sh uses,
# so this also works correctly with no local clone at all (`curl | bash`).

# Colored output, only when stdout is an actual terminal - never for a
# pipe/redirect (e.g. under a test harness). The color variables carry
# literal escape bytes ($'...' ANSI-C quoting), so referencing them works
# the same in echo, printf, and heredocs alike - no `echo -e` needed.
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

# ${BASH_SOURCE[0]:-} - when piped via `curl | bash`, the script has no
# on-disk path of its own, so BASH_SOURCE[0] is unset; under `set -u` a
# bare ${BASH_SOURCE[0]} would abort with "unbound variable" instead of
# falling through to the known-agent-names fallback below.
SELF_PATH="${BASH_SOURCE[0]:-}"
if [ -n "$SELF_PATH" ]; then
  LOOP_DIR="$(cd "$(dirname "$SELF_PATH")/../.." && pwd)"
else
  LOOP_DIR=""
fi
LAUNCHD_DIR="$LOOP_DIR/launchd"

# Fallback when there's no local repo clone to read launchd/*.plist from
# (e.g. run via `curl | bash`) - must be kept in sync with the filenames
# under launchd/ in this repo.
KNOWN_LAUNCHD_AGENT_NAMES=(
  "com.hermes.loop-engineering.plist"
  "com.hermes.loop-engineering-dashboard.plist"
  "com.hermes.loop-engineering-topic-monitor.plist"
)
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
NGINX_DOMAIN="loop.local"
NGINX_SERVERS_DIR=""
HOSTS_FILE="/etc/hosts"
PROJECT_DIR="$HOME/.loop-engineering"
PROJECT_DIR_GIVEN=0
SKIP_NGINX=0
SKIP_HOSTS=0
SKIP_SERVICE=0
KEEP_CONFIG=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --launchd-dir)
      LAUNCHD_DIR="$2"
      shift 2
      ;;
    --launch-agents-dir)
      LAUNCH_AGENTS_DIR="$2"
      shift 2
      ;;
    --nginx-domain)
      NGINX_DOMAIN="$2"
      shift 2
      ;;
    --nginx-servers-dir)
      NGINX_SERVERS_DIR="$2"
      shift 2
      ;;
    --hosts-file)
      HOSTS_FILE="$2"
      shift 2
      ;;
    --project-dir)
      PROJECT_DIR="$2"
      PROJECT_DIR_GIVEN=1
      shift 2
      ;;
    --skip-nginx)
      SKIP_NGINX=1
      shift
      ;;
    --skip-hosts)
      SKIP_HOSTS=1
      shift
      ;;
    --skip-service)
      SKIP_SERVICE=1
      shift
      ;;
    --keep-config)
      KEEP_CONFIG=1
      shift
      ;;
    *)
      echo "${C_RED}Usage: uninstall.sh [--launchd-dir PATH] [--launch-agents-dir PATH] [--nginx-domain NAME] [--nginx-servers-dir PATH] [--hosts-file PATH] [--project-dir PATH] [--skip-nginx] [--skip-hosts] [--skip-service] [--keep-config]${C_RESET}" >&2
      exit 1
      ;;
  esac
done

echo "${C_BLUE}==> Removing launchd agents${C_RESET}"
if [ -d "$LAUNCHD_DIR" ]; then
  shopt -s nullglob
  plists=("$LAUNCHD_DIR"/*.plist)
  shopt -u nullglob
  if [ "${#plists[@]}" -eq 0 ]; then
    echo "    No *.plist files found in $LAUNCHD_DIR"
  fi
else
  echo "    No local $LAUNCHD_DIR - falling back to this repo's known agent names"
  plists=("${KNOWN_LAUNCHD_AGENT_NAMES[@]}")
fi
for plist in "${plists[@]+"${plists[@]}"}"; do
  name="$(basename "$plist")"
  dest="$LAUNCH_AGENTS_DIR/$name"
  if [ -f "$dest" ]; then
    launchctl unload -w "$dest" >/dev/null 2>&1 || true
    rm -f "$dest"
    echo "${C_GREEN}    Removed $name${C_RESET}"
  else
    echo "    $name was not installed"
  fi
done

if [ "$SKIP_NGINX" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping nginx cleanup (--skip-nginx)${C_RESET}"
else
  echo "${C_BLUE}==> Removing nginx reverse proxy for $NGINX_DOMAIN${C_RESET}"
  if [ -z "$NGINX_SERVERS_DIR" ] && command -v brew >/dev/null 2>&1; then
    NGINX_SERVERS_DIR="$(brew --prefix)/etc/nginx/servers"
  fi
  conf_path="$NGINX_SERVERS_DIR/$NGINX_DOMAIN.conf"
  if [ -n "$NGINX_SERVERS_DIR" ] && [ -f "$conf_path" ]; then
    rm -f "$conf_path"
    echo "${C_GREEN}    Removed $conf_path${C_RESET}"
  else
    echo "    No nginx config found for $NGINX_DOMAIN, nothing to remove"
  fi

  HOSTS_HAS_DOMAIN=0
  if grep -qE "^[[:space:]]*127\.0\.0\.1[[:space:]]+$NGINX_DOMAIN([[:space:]]|\$)" "$HOSTS_FILE"; then
    HOSTS_HAS_DOMAIN=1
  fi
  NGINX_INSTALLED=0
  if command -v brew >/dev/null 2>&1 && brew list nginx >/dev/null 2>&1; then
    NGINX_INSTALLED=1
  fi

  # Prime the sudo ticket once, up front, so the two independent
  # sudo-needing steps below ($HOSTS_FILE cleanup, nginx service restart)
  # share a single password prompt instead of each risking its own.
  NEED_SUDO=0
  [ "$SKIP_HOSTS" -eq 0 ] && [ "$HOSTS_HAS_DOMAIN" -eq 1 ] && NEED_SUDO=1
  [ "$SKIP_SERVICE" -eq 0 ] && [ "$NGINX_INSTALLED" -eq 1 ] && NEED_SUDO=1
  if [ "$NEED_SUDO" -eq 1 ]; then
    echo "    The steps below need sudo - enter your password once:"
    sudo -v
  fi

  if [ "$SKIP_HOSTS" -eq 1 ]; then
    echo "    Skipping $HOSTS_FILE cleanup (--skip-hosts)"
  elif [ "$HOSTS_HAS_DOMAIN" -eq 1 ]; then
    echo "    Removing $NGINX_DOMAIN from $HOSTS_FILE (requires sudo)"
    sudo sh -c "grep -vE '^[[:space:]]*127\.0\.0\.1[[:space:]]+$NGINX_DOMAIN([[:space:]]|\$)' '$HOSTS_FILE' > '$HOSTS_FILE.uninstall-tmp' && mv '$HOSTS_FILE.uninstall-tmp' '$HOSTS_FILE'"
  else
    echo "    $NGINX_DOMAIN not found in $HOSTS_FILE"
  fi

  if [ "$SKIP_SERVICE" -eq 1 ]; then
    echo "    Skipping nginx service restart (--skip-service)"
  elif [ "$NGINX_INSTALLED" -eq 1 ]; then
    echo "    Restarting nginx (requires sudo)..."
    sudo brew services restart nginx >/dev/null 2>&1 || true
  fi
fi

echo "${C_BLUE}==> Project directory ($PROJECT_DIR)${C_RESET}"
if [ "$KEEP_CONFIG" -eq 1 ]; then
  echo "    Left in place (--keep-config)"
else
  # Refuse an implicit (no --project-dir) delete when this script's own
  # location (LOOP_DIR) doesn't match the directory it's about to rm -rf.
  # A real end user's install has the script living inside PROJECT_DIR
  # itself (LOOP_DIR == PROJECT_DIR), or has no local clone at all (LOOP_DIR
  # empty, e.g. curl | bash) - both fine. But a developer running a
  # *separate* clone's copy of this script bare would otherwise silently
  # delete an unrelated real install at the default path - that's the
  # actual incident this guards against (see CLAUDE.md's dev-mode section).
  if [ "$PROJECT_DIR_GIVEN" -eq 0 ] && [ -n "$LOOP_DIR" ] && [ "$LOOP_DIR" != "$PROJECT_DIR" ]; then
    echo "${C_RED}    Refusing to remove $PROJECT_DIR: this uninstall.sh is running from $LOOP_DIR, a different directory.${C_RESET}" >&2
    echo "    If you're really uninstalling that real install, pass --project-dir '$PROJECT_DIR' to confirm. If you're developing on a separate clone, pass --project-dir to target a scratch directory instead." >&2
    exit 1
  fi
  if [ -d "$PROJECT_DIR" ]; then
    rm -rf "$PROJECT_DIR"
    echo "${C_GREEN}    Removed $PROJECT_DIR${C_RESET}"
  else
    echo "    $PROJECT_DIR does not exist, nothing to remove"
  fi
fi

cat <<EOF

${C_GREEN}Uninstall done.${C_RESET}

Removed (if present): this repo's launchd agents, its nginx reverse proxy
config/hosts entry$([ "$KEEP_CONFIG" -eq 0 ] && echo ", and $PROJECT_DIR (its cloned code and your project config together)").

Left untouched (shared with other tools, not this loop's alone):
  - ~/.gitlab/config.json, ~/.slack/config.json
  - ~/.encore-skills (the gitlab-config skill)
  - Homebrew's nginx itself$([ "$KEEP_CONFIG" -eq 1 ] && echo "
  - $PROJECT_DIR (kept, --keep-config was passed)")
EOF
