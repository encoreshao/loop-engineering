#!/usr/bin/env bash
set -euo pipefail

# The online installer - no local clone needed first:
#
#   curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash
#
# Already installed and just want the latest code? Same command, plus
# --upgrade:
#
#   curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/install.sh | bash -s -- --upgrade
#
# --upgrade fails fast if --dir isn't an existing clone yet (instead of
# silently doing a fresh clone) - it's meant for a machine that already has
# this installed. Every other step below runs the same either way.
#
# Clones this repo into --dir (or pulls the latest --branch if it's already
# a clone there), runs its own bin/scripts/setup.sh from inside that clone
# (forwarding any setup.sh flags given here), renders this machine's
# launchd/*.plist from their .plist.template (python3 + the clone dir
# substituted in for {{PYTHON3}}/{{LOOP_DIR}}) the first time only - once a
# .plist exists it's left alone, since the dashboard's Daemons page writes
# straight to it when the user saves a custom schedule - then, unless skipped,
# sets up the local nginx reverse proxy and starts the dashboard as an
# always-on launchd agent (or, if it's already running from a previous
# install, restarts it via `launchctl kickstart -k` so it actually picks up
# the code just pulled - `launchctl load` on an already-loaded agent is a
# silent no-op). A plain online install/upgrade ends with the dashboard
# actually reachable and running the latest code, not just cloned and
# configured.
#
# Only the dashboard daemon is auto-started when it isn't already running
# (the GitLab loop and topic monitor are never auto-started this way - they
# act on projects.json/topics.json, which may still be template scaffolds).
# But once ANY of this project's launchd agents is already loaded - the
# dashboard, or the GitLab loop/topic monitor once the user has separately
# opted them in from the dashboard's Daemons page - a successful upgrade
# refreshes its registration so it's running the code just pulled: the
# dashboard gets `launchctl kickstart -k` (it's an always-on server, so
# that's also the only way to make it actually restart), while the loop and
# topic monitor - cron-style jobs that already pick up new code on their
# next scheduled run - just get their plist re-copied and reloaded
# (`unload` + `load -w`, no `-k`), never kickstarted: kickstart would
# trigger a real, out-of-schedule run against live GitLab/Slack, the same
# action the dashboard's own "Run now" button gates behind a confirmation
# dialog, which an unattended `curl | bash --upgrade` has no way to ask for.

# Colored output, only when stdout is an actual terminal - never for a
# pipe/redirect (e.g. under a test harness, or `install.sh > log.txt`).
# The color variables carry literal escape bytes ($'...' ANSI-C quoting),
# so referencing them works the same in echo, printf, and heredocs alike -
# no `echo -e` needed anywhere.
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

DIR="$HOME/.loop-engineering"
BRANCH="main"
REPO_URL="https://github.com/encoreshao/loop-engineering.git"
LAUNCH_AGENTS_DIR="$HOME/Library/LaunchAgents"
SKIP_NGINX=0
SKIP_LAUNCHD_DAEMONS=0
UPGRADE=0
PORT=""
SETUP_ARGS=()

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dir)
      DIR="$2"
      shift 2
      ;;
    --upgrade)
      UPGRADE=1
      shift
      ;;
    --branch)
      BRANCH="$2"
      shift 2
      ;;
    --repo-url)
      REPO_URL="$2"
      shift 2
      ;;
    --launch-agents-dir)
      LAUNCH_AGENTS_DIR="$2"
      shift 2
      ;;
    --skip-nginx)
      SKIP_NGINX=1
      shift
      ;;
    --skip-launchd-daemons)
      SKIP_LAUNCHD_DAEMONS=1
      shift
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --skip-skills-install)
      SETUP_ARGS+=("--skip-skills-install")
      shift
      ;;
    --config-path)
      SETUP_ARGS+=("--config-path" "$2")
      shift 2
      ;;
    --topics-config-path)
      SETUP_ARGS+=("--topics-config-path" "$2")
      shift 2
      ;;
    *)
      echo "${C_RED}Usage: install.sh [--dir PATH] [--branch NAME] [--repo-url URL] [--launch-agents-dir PATH] [--upgrade] [--skip-nginx] [--skip-launchd-daemons] [--port PORT] [--skip-skills-install] [--config-path PATH] [--topics-config-path PATH]${C_RESET}" >&2
      exit 1
      ;;
  esac
done

if ! command -v git >/dev/null 2>&1; then
  echo "${C_RED}git is required - install it first (e.g. 'xcode-select --install').${C_RESET}" >&2
  exit 1
fi

if [ -d "$DIR" ]; then
  if [ -d "$DIR/.git" ]; then
    echo "${C_BLUE}==> $DIR already exists, pulling latest $BRANCH...${C_RESET}"
    git -C "$DIR" pull --ff-only origin "$BRANCH"
  else
    echo "${C_RED}$DIR already exists and isn't a git clone - remove it or pass --dir to pick another location.${C_RESET}" >&2
    exit 1
  fi
elif [ "$UPGRADE" -eq 1 ]; then
  echo "${C_RED}--upgrade given but nothing is installed at $DIR yet - run install.sh without --upgrade first.${C_RESET}" >&2
  exit 1
else
  echo "${C_BLUE}==> Cloning $REPO_URL into $DIR...${C_RESET}"
  git clone --branch "$BRANCH" "$REPO_URL" "$DIR"
fi

echo "${C_BLUE}==> Running setup...${C_RESET}"
# `${SETUP_ARGS[@]}` alone throws "unbound variable" under `set -u` when the
# array is empty (the common case - most installs pass no forwarded flags),
# but only on bash < 4.4. macOS ships bash 3.2 (GPLv2 cutoff) as both
# /bin/bash and whatever `bash` resolves to on PATH, so this bites on every
# real install. The `${arr[@]+"${arr[@]}"}` idiom expands to nothing when
# empty and to the normal quoted elements otherwise, on all bash versions.
"$DIR/bin/scripts/setup.sh" "${SETUP_ARGS[@]+"${SETUP_ARGS[@]}"}"

echo "${C_BLUE}==> Rendering launchd/*.plist for this machine...${C_RESET}"
PYTHON3="$(command -v python3 || true)"
# The port the launchd-installed dashboard daemon listens on: picked once
# and baked into the rendered plist the same way {{PYTHON3}}/{{LOOP_DIR}}
# are (a --port override, like those two, is only honored the first time
# the plist is rendered - see the "don't clobber" comment below). Local
# dev (running bin/web/dashboard_server.py directly, with no plist
# involved) is untouched by any of this - it still falls back to that
# script's own DEFAULT_PORT (8420) exactly as before. Chosen from a
# 5-digit range (48420-48620) specifically so it's visually distinct from
# the old fixed 8420 default and unlikely to collide with common local
# dev ports. (Must stay within 0-65535 - a range like 84200-84300 looks
# similar but overflows the valid port space and crashes bind() with
# OverflowError: bind(): port must be 0-65535.)
if [ -z "$PORT" ]; then
  PORT=$((48420 + RANDOM % 201))
fi
if [ -d "$DIR/launchd" ]; then
  shopt -s nullglob
  for template in "$DIR"/launchd/*.plist.template; do
    rendered="${template%.template}"
    # Same "don't clobber what's already there" convention setup.sh uses
    # for projects.json/topics.json: once a .plist has been rendered once,
    # it's this project's own source of truth (see LAUNCHD_DIR's docstring
    # in dashboard_server.py) and the dashboard's Daemons page writes
    # straight to it when the user saves a custom schedule. Re-rendering it
    # from the template unconditionally on every --upgrade silently
    # reverted any such saved schedule back to the template's default.
    if [ -f "$rendered" ]; then
      continue
    fi
    sed -e "s|{{PYTHON3}}|$PYTHON3|g" -e "s|{{LOOP_DIR}}|$DIR|g" -e "s|{{PORT}}|$PORT|g" "$template" > "$rendered"
  done
  shopt -u nullglob
fi

# Read the port back out of the actual rendered dashboard plist rather than
# trusting $PORT directly: on an --upgrade where that plist already existed
# (and so was left alone above, "don't clobber"), $PORT here may be a
# freshly-picked/overridden value that was never actually substituted in -
# the real answer is whatever port is already baked into that file. Falls
# back to 8420 (dashboard_server.py's own DEFAULT_PORT) if the plist isn't
# there at all, e.g. --skip-launchd-daemons on a from-scratch install.
dashboard_plist_path="$DIR/launchd/com.hermes.loop-engineering-dashboard.plist"
if [ -f "$dashboard_plist_path" ]; then
  actual_port="$(grep -o '<string>[0-9]\{4,5\}</string>' "$dashboard_plist_path" | head -1 | grep -o '[0-9]\{4,5\}' || true)"
  if [ -n "$actual_port" ]; then
    PORT="$actual_port"
  fi
else
  PORT="8420"
fi

DASHBOARD_URL="http://127.0.0.1:$PORT"
if [ "$SKIP_NGINX" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping nginx setup (--skip-nginx)${C_RESET}"
else
  echo "${C_BLUE}==> Setting up the local nginx reverse proxy...${C_RESET}"
  if "$DIR/bin/scripts/setup-nginx.sh" --port "$PORT"; then
    DASHBOARD_URL="http://loop.local/"
  else
    echo "${C_YELLOW}    Warning: nginx setup failed - the dashboard is still reachable at http://127.0.0.1:$PORT, just not at http://loop.local/. Re-run 'bin/scripts/setup-nginx.sh' by hand once Homebrew/sudo are sorted.${C_RESET}" >&2
  fi
fi

if [ "$SKIP_LAUNCHD_DAEMONS" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping launchd daemons (--skip-launchd-daemons)${C_RESET}"
else
  dashboard_plist="$DIR/launchd/com.hermes.loop-engineering-dashboard.plist"
  if [ ! -f "$dashboard_plist" ]; then
    echo "${C_YELLOW}    Warning: $dashboard_plist not found, skipping dashboard daemon${C_RESET}" >&2
  else
    mkdir -p "$LAUNCH_AGENTS_DIR"
    dest="$LAUNCH_AGENTS_DIR/com.hermes.loop-engineering-dashboard.plist"
    cp "$dashboard_plist" "$dest"
    # `launchctl load` on a label that's already loaded is a silent no-op
    # (exits 0, "Load failed: 5: Input/output error" on stderr) - it never
    # restarts the running process. Without this check, --upgrade (or just
    # re-running this script) would pull new code but the dashboard would
    # keep serving the old code indefinitely, while this script reported
    # success. `kickstart -k` is the actual "pick up the new code" verb for
    # an already-loaded agent; `load -w` is only correct the first time.
    if launchctl list com.hermes.loop-engineering-dashboard >/dev/null 2>&1; then
      echo "${C_BLUE}==> Restarting the dashboard to pick up the update...${C_RESET}"
      launchctl kickstart -k "gui/$(id -u)/com.hermes.loop-engineering-dashboard"
      echo "${C_GREEN}    Dashboard restarted - $DASHBOARD_URL${C_RESET}"
    else
      echo "${C_BLUE}==> Starting the dashboard as an always-on launchd agent...${C_RESET}"
      if launchctl load -w "$dest"; then
        echo "${C_GREEN}    Dashboard running - $DASHBOARD_URL${C_RESET}"
      else
        rm -f "$dest"
        echo "${C_YELLOW}    Warning: launchctl load failed - start it later from the dashboard's Daemons page, or run 'launchctl load -w $dest' by hand.${C_RESET}" >&2
      fi
    fi
  fi

  # The GitLab loop and topic monitor are cron-style jobs (StartCalendarInterval,
  # RunAtLoad=false) - never auto-started by this script (see the top-of-file
  # comment), and each scheduled fire already runs whatever's on disk, so
  # they don't need restarting to pick up new code the way the always-on
  # dashboard does. But if the user has separately enabled one from the
  # dashboard's Daemons page, its launchd registration should still be
  # refreshed on upgrade (in case its rendered plist changed) - `unload` +
  # `load -w`, deliberately never `kickstart -k`: kickstart would trigger a
  # real, out-of-schedule run against live GitLab/Slack right now, which an
  # unattended `curl | bash --upgrade` has no way to confirm.
  shopt -s nullglob
  for plist in "$DIR"/launchd/*.plist; do
    label="$(basename "$plist" .plist)"
    if [ "$label" = "com.hermes.loop-engineering-dashboard" ]; then
      continue
    fi
    if launchctl list "$label" >/dev/null 2>&1; then
      echo "${C_BLUE}==> Refreshing $label's launchd registration...${C_RESET}"
      dest="$LAUNCH_AGENTS_DIR/$(basename "$plist")"
      mkdir -p "$LAUNCH_AGENTS_DIR"
      cp "$plist" "$dest"
      launchctl unload "$dest" >/dev/null 2>&1 || true
      if launchctl load -w "$dest"; then
        echo "${C_GREEN}    $label refreshed - it'll run the updated code at its next scheduled time${C_RESET}"
      else
        rm -f "$dest"
        echo "${C_YELLOW}    Warning: failed to refresh $label - re-enable it later from the Daemons page.${C_RESET}" >&2
      fi
    fi
  done
  shopt -u nullglob
fi

cat <<EOF

${C_GREEN}Install done.${C_RESET} Next: fill in $DIR/projects.json (and ~/.gitlab/config.json),
then check the dashboard's Skills page.
EOF
