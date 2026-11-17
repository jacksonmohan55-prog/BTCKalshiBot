#!/usr/bin/env bash
# One-time setup on a fresh Ubuntu server. Installs everything and makes the bot run 24/7:
# it starts automatically when the server boots and restarts if it ever crashes.
#
# Run from the project folder:   sudo ./scripts/server_setup.sh
# Re-running it is safe (use it again after `git pull` to pick up new requirements).
set -euo pipefail
cd "$(dirname "$0")/.."
DIR="$(pwd)"
RUN_AS="${SUDO_USER:-$(whoami)}"
as_user() { if [ "$RUN_AS" = "$(whoami)" ]; then "$@"; else sudo -u "$RUN_AS" "$@"; fi; }

echo "==> Installing Python and git"
apt-get update -y -qq
apt-get install -y -qq python3 python3-venv python3-pip git >/dev/null

echo "==> Creating the Python environment in $DIR/.venv"
as_user python3 -m venv .venv
as_user .venv/bin/pip install -q --upgrade pip
as_user .venv/bin/pip install -q -r requirements.txt

if [ ! -f config.toml ]; then
  echo "==> No config.toml found: creating one (paper mode, real Kalshi market data, no key)"
  as_user tee config.toml >/dev/null <<'EOF'
db_path = "kbot.sqlite"

[horizons]
"15m" = "KXBTC15M"
"60m" = "KXBTCD"

[kalshi]
env = "prod"
# key_id = "your-key-id"
# private_key_path = "kalshi.pem"
EOF
fi

if ! command -v systemctl >/dev/null || [ ! -d /run/systemd/system ]; then
  echo "systemd isn't available here; start the bot with ./scripts/run_forever.sh instead."
  exit 0
fi

echo "==> Installing the kbot service"
cat > /etc/systemd/system/kbot.service <<EOF
[Unit]
Description=kbot BTC/Kalshi paper trader
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_AS
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m kbot run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable kbot >/dev/null 2>&1
systemctl restart kbot
sleep 3
systemctl --no-pager --lines=0 status kbot | head -3
echo
echo "Done. The bot is running and will keep running after you disconnect."
echo "  Dashboard:   cd $DIR && .venv/bin/python -m kbot dashboard"
echo "  Scoreboard:  cd $DIR && .venv/bin/python -m kbot report"
echo "  Live log:    journalctl -u kbot -f"
echo "  Restart:     sudo systemctl restart kbot      Stop: sudo systemctl stop kbot"
