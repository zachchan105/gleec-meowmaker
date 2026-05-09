#!/usr/bin/env bash
# Stops the bot via SIGTERM. The bot's signal handler cancels all open
# MEWC/LTC orders before exiting, so we never leave stale orders behind.

source "$(dirname "$0")/_lib.sh"

if [[ ! -f "$BOT_PID_FILE" ]]; then
  echo "Bot is not running (no pid file)."
  exit 0
fi

PID=$(cat "$BOT_PID_FILE")
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Bot pid $PID is not alive; clearing pid file."
  rm -f "$BOT_PID_FILE"
  exit 0
fi

echo "Sending SIGTERM to bot (pid $PID); it will cancel open orders and exit..."
kill -TERM "$PID"

for _ in $(seq 1 30); do
  sleep 0.5
  kill -0 "$PID" 2>/dev/null || break
done

if kill -0 "$PID" 2>/dev/null; then
  echo "Bot did not exit in 15s; sending SIGKILL (NOTE: orders may be left open)."
  kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$BOT_PID_FILE"
echo "Bot stopped."
