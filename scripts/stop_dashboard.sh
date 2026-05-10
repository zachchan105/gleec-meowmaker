#!/usr/bin/env bash
# Stops the dashboard. The dashboard is read-only, so SIGKILL is fine — but
# we send SIGTERM first to give the HTTP server a clean shutdown.

source "$(dirname "$0")/_lib.sh"

DASH_PID_FILE="$BOT_DIR/.dashboard.pid"

if [[ ! -f "$DASH_PID_FILE" ]]; then
  echo "Dashboard is not running (no pid file)."
  exit 0
fi

PID=$(cat "$DASH_PID_FILE")
if ! kill -0 "$PID" 2>/dev/null; then
  echo "Dashboard pid $PID is not alive; clearing pid file."
  rm -f "$DASH_PID_FILE"
  exit 0
fi

echo "Sending SIGTERM to dashboard (pid $PID)..."
kill -TERM "$PID"

for _ in $(seq 1 10); do
  sleep 0.3
  kill -0 "$PID" 2>/dev/null || break
done

if kill -0 "$PID" 2>/dev/null; then
  echo "Dashboard did not exit in 3s; sending SIGKILL."
  kill -KILL "$PID" 2>/dev/null || true
fi

rm -f "$DASH_PID_FILE"
echo "Dashboard stopped."
