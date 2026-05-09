#!/usr/bin/env bash
# Politely stops the local KDF daemon. Tries the 'stop' RPC first, then
# escalates to TERM/KILL if needed.

source "$(dirname "$0")/_lib.sh"
require_curl
require_jq

if [[ -f "$KDF_DIR/MM2.json" ]]; then
  USERPASS=$(get_userpass)
  kdf_call "{\"userpass\":\"$USERPASS\",\"method\":\"stop\"}" >/dev/null 2>&1 || true
fi

if [[ -f "$KDF_PID_FILE" ]]; then
  PID=$(cat "$KDF_PID_FILE")
  if kill -0 "$PID" 2>/dev/null; then
    for _ in $(seq 1 20); do
      sleep 0.5
      kill -0 "$PID" 2>/dev/null || break
    done
    if kill -0 "$PID" 2>/dev/null; then
      echo "KDF still alive after 'stop' RPC; sending SIGTERM."
      kill -TERM "$PID" 2>/dev/null || true
      sleep 2
      kill -0 "$PID" 2>/dev/null && kill -KILL "$PID" 2>/dev/null || true
    fi
  fi
  rm -f "$KDF_PID_FILE"
fi

echo "KDF stopped."
