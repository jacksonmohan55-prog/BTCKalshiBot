#!/usr/bin/env bash
# Runs paper mode forever, restarting if it ever exits. Logs to kbot.log in the project folder.
# Usage (from the project folder):  ./scripts/run_forever.sh
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && source .venv/bin/activate
while true; do
  echo "==== starting kbot $(date)" >> kbot.log
  python -m kbot run >> kbot.log 2>&1
  echo "==== kbot exited with code $?; restarting in 10s" >> kbot.log
  sleep 10
done
