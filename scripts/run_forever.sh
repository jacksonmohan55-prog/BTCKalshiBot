#!/usr/bin/env bash
# Runs paper mode forever, restarting if it ever exits. Logs to kbot.log in the project folder.
# Usage (from the project folder):  ./scripts/run_forever.sh
cd "$(dirname "$0")/.."
[ -f .venv/bin/activate ] && source .venv/bin/activate
echo "kbot is running. This window stays quiet on purpose: everything goes to kbot.log."
echo "  Leave this window open. To stop the bot: press Ctrl+C here."
echo "  To watch it: open a new window (Cmd+N) and run  kbot dashboard"
echo "               (or: cd $(pwd) && source .venv/bin/activate && python -m kbot dashboard)"
while true; do
  echo "==== starting kbot $(date)" >> kbot.log
  python -m kbot run >> kbot.log 2>&1
  code=$?
  echo "==== kbot exited with code $code; restarting in 10s" >> kbot.log
  echo "$(date '+%H:%M:%S') bot stopped (code $code); restarting in 10s. Details: tail -20 kbot.log"
  sleep 10
done
