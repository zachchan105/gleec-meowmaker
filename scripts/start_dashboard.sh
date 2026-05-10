#!/usr/bin/env bash
# Launch the read-only web dashboard in the background, logging to
# bot/dashboard.log. Idempotent: re-running while already up is a no-op.

source "$(dirname "$0")/_lib.sh"

DASH_PID_FILE="$BOT_DIR/.dashboard.pid"
DASH_LOG="$BOT_DIR/dashboard.log"

if [[ ! -f "$BOT_DIR/config.toml" ]]; then
  echo "ERROR: $BOT_DIR/config.toml not found." >&2
  echo "Copy bot/config.example.toml to bot/config.toml first." >&2
  exit 1
fi

if [[ -f "$DASH_PID_FILE" ]] && kill -0 "$(cat "$DASH_PID_FILE")" 2>/dev/null; then
  echo "Dashboard already running (pid $(cat "$DASH_PID_FILE"))."
  exit 0
fi

PYTHON="${PYTHON:-python3}"

cd "$REPO_ROOT"
echo "Starting dashboard, logging to $DASH_LOG ..."
nohup "$PYTHON" -u -m bot.dashboard >>"$DASH_LOG" 2>&1 &
echo $! > "$DASH_PID_FILE"
disown || true

sleep 0.5
if kill -0 "$(cat "$DASH_PID_FILE")" 2>/dev/null; then
  PID=$(cat "$DASH_PID_FILE")
  # Resolve the bind URL for the user-friendly message. These keys are
  # optional in config.toml (defaults are 127.0.0.1:7784), so tolerate
  # them missing without tripping `set -e`.
  HOST=$({ grep -E '^dashboard_host' "$BOT_DIR/config.toml" 2>/dev/null || true; } \
    | sed -E 's/.*=\s*"?([^"]+)"?\s*/\1/' | head -1)
  PORT=$({ grep -E '^dashboard_port' "$BOT_DIR/config.toml" 2>/dev/null || true; } \
    | sed -E 's/.*=\s*([0-9]+).*/\1/' | head -1)
  HOST="${HOST:-127.0.0.1}"
  PORT="${PORT:-7784}"
  echo "Dashboard started (pid $PID).  →  http://$HOST:$PORT/"
else
  echo "Dashboard died immediately. Tail $DASH_LOG to debug." >&2
  rm -f "$DASH_PID_FILE"
  exit 1
fi
