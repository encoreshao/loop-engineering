#!/usr/bin/env bash
set -euo pipefail

# One-command local nginx reverse proxy in front of the dashboard, so it's
# reachable at a friendly hostname on the standard port 80 instead of
# remembering http://127.0.0.1:8420 - e.g. http://loop.local/. Idempotent:
# safe to re-run any time (writing /etc/hosts and starting the nginx
# service are both skipped once already done).
#
#   curl -fsSL https://raw.githubusercontent.com/encoreshao/loop-engineering/main/bin/scripts/setup-nginx.sh | bash
#
# Requires Homebrew (https://brew.sh). Writing /etc/hosts and running
# nginx as a system service both need sudo - macOS will prompt for your
# password when this script reaches those steps.

# Colored output, only when stdout is an actual terminal - never for a
# pipe/redirect (e.g. under a test harness). The color variables carry
# literal escape bytes ($'...' ANSI-C quoting), so referencing them works
# the same in echo, printf, and heredocs alike - no `echo -e` needed.
if [ -t 1 ]; then
  C_BLUE=$'\033[1;34m'; C_GREEN=$'\033[1;32m'; C_YELLOW=$'\033[1;33m'; C_RED=$'\033[1;31m'; C_RESET=$'\033[0m'
else
  C_BLUE=""; C_GREEN=""; C_YELLOW=""; C_RED=""; C_RESET=""
fi

DOMAIN="loop.local"
PORT="8420"
SERVERS_DIR=""
HOSTS_FILE="/etc/hosts"
SKIP_BREW_INSTALL=0
SKIP_HOSTS=0
SKIP_SERVICE=0

while [ "$#" -gt 0 ]; do
  case "$1" in
    --domain)
      DOMAIN="$2"
      shift 2
      ;;
    --port)
      PORT="$2"
      shift 2
      ;;
    --servers-dir)
      SERVERS_DIR="$2"
      shift 2
      ;;
    --hosts-file)
      HOSTS_FILE="$2"
      shift 2
      ;;
    --skip-brew-install)
      SKIP_BREW_INSTALL=1
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
    *)
      echo "${C_RED}Usage: setup-nginx.sh [--domain NAME] [--port N] [--servers-dir PATH] [--hosts-file PATH] [--skip-brew-install] [--skip-hosts] [--skip-service]${C_RESET}" >&2
      exit 1
      ;;
  esac
done

if [ "$SKIP_BREW_INSTALL" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping nginx install (--skip-brew-install)${C_RESET}"
else
  if ! command -v brew >/dev/null 2>&1; then
    echo "${C_RED}Homebrew is required (https://brew.sh) - install it first.${C_RESET}" >&2
    exit 1
  fi
  echo "${C_BLUE}==> Installing nginx (if not already installed)...${C_RESET}"
  brew list nginx >/dev/null 2>&1 || brew install nginx
fi

if [ -z "$SERVERS_DIR" ]; then
  SERVERS_DIR="$(brew --prefix)/etc/nginx/servers"
fi
mkdir -p "$SERVERS_DIR"

CONF_PATH="$SERVERS_DIR/$DOMAIN.conf"
echo "${C_BLUE}==> Writing $CONF_PATH${C_RESET}"
cat > "$CONF_PATH" <<EOF
server {
    listen       80;
    server_name  $DOMAIN;

    location / {
        proxy_pass         http://127.0.0.1:$PORT;
        proxy_http_version 1.1;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
    }
}
EOF

HOSTS_HAS_DOMAIN=0
if grep -qE "^[[:space:]]*127\.0\.0\.1[[:space:]]+$DOMAIN([[:space:]]|\$)" "$HOSTS_FILE"; then
  HOSTS_HAS_DOMAIN=1
fi

# Prime the sudo ticket once, up front, so the two independent sudo-needing
# steps below (writing $HOSTS_FILE, restarting the nginx service) share a
# single password prompt instead of each risking its own.
NEED_SUDO=0
[ "$SKIP_HOSTS" -eq 0 ] && [ "$HOSTS_HAS_DOMAIN" -eq 0 ] && NEED_SUDO=1
[ "$SKIP_SERVICE" -eq 0 ] && NEED_SUDO=1
if [ "$NEED_SUDO" -eq 1 ]; then
  echo "${C_BLUE}==> The steps below need sudo - enter your password once:${C_RESET}"
  sudo -v
fi

if [ "$SKIP_HOSTS" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping $HOSTS_FILE (--skip-hosts)${C_RESET}"
elif [ "$HOSTS_HAS_DOMAIN" -eq 1 ]; then
  echo "${C_BLUE}==> $DOMAIN already in $HOSTS_FILE${C_RESET}"
else
  echo "${C_BLUE}==> Adding $DOMAIN to $HOSTS_FILE (requires sudo)${C_RESET}"
  echo "127.0.0.1	$DOMAIN" | sudo tee -a "$HOSTS_FILE" >/dev/null
fi

if [ "$SKIP_SERVICE" -eq 1 ]; then
  echo "${C_BLUE}==> Skipping nginx service start (--skip-service)${C_RESET}"
else
  echo "${C_BLUE}==> Starting nginx as a system service (requires sudo)...${C_RESET}"
  sudo brew services restart nginx
fi

cat <<EOF

${C_GREEN}Done.${C_RESET} Once nginx and /etc/hosts are set up, the dashboard is reachable at:
  http://$DOMAIN/  (proxied to 127.0.0.1:$PORT)
EOF
