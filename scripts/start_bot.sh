#!/usr/bin/env bash
# Launch the Python market-making bot in the background, logging to bot/bot.log.

source "$(dirname "$0")/_lib.sh"

if [[ ! -f "$BOT_DIR/config.toml" ]]; then
  echo "ERROR: $BOT_DIR/config.toml not found." >&2
  echo "Copy bot/config.example.toml to bot/config.toml and adjust if you want." >&2
  exit 1
fi

if [[ -f "$BOT_PID_FILE" ]] && kill -0 "$(cat "$BOT_PID_FILE")" 2>/dev/null; then
  echo "Bot already running (pid $(cat "$BOT_PID_FILE"))."
  exit 0
fi

PYTHON="${PYTHON:-python3}"
LOG="$BOT_DIR/bot.log"

cd "$REPO_ROOT"
echo "Starting bot, logging to $LOG ..."
nohup "$PYTHON" -u -m bot.main >>"$LOG" 2>&1 &
echo $! > "$BOT_PID_FILE"
disown || true

sleep 0.5
if kill -0 "$(cat "$BOT_PID_FILE")" 2>/dev/null; then
  echo "Bot started (pid $(cat "$BOT_PID_FILE"))."
else
  echo "Bot died immediately. Tail $LOG to debug." >&2
  rm -f "$BOT_PID_FILE"
  exit 1
fi
