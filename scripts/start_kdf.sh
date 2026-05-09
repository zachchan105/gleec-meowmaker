#!/usr/bin/env bash
# Launch the KDF daemon in the background, logging to kdf/kdf.log.
# Re-running while KDF is up is a no-op.

source "$(dirname "$0")/_lib.sh"

require_mm2_json

if [[ -f "$KDF_PID_FILE" ]] && kill -0 "$(cat "$KDF_PID_FILE")" 2>/dev/null; then
  echo "KDF already running (pid $(cat "$KDF_PID_FILE"))."
  exit 0
fi

if [[ ! -x "$KDF_BIN" ]]; then
  echo "ERROR: kdf binary not found or not executable at: $KDF_BIN" >&2
  echo "Either place the binary at $REPO_ROOT/kdf/kdf (chmod +x), or" >&2
  echo "set KDF_BIN=/path/to/kdf in your environment before re-running." >&2
  echo "Releases: https://github.com/GLEECBTC/komodo-defi-framework/releases" >&2
  exit 1
fi

cd "$KDF_DIR"
mkdir -p DB

# nohup + setsid so KDF outlives this shell, redirect stdout+stderr to log.
LOG="$KDF_DIR/kdf.log"
echo "Starting KDF, logging to $LOG ..."
nohup "$KDF_BIN" >>"$LOG" 2>&1 &
echo $! > "$KDF_PID_FILE"
disown || true

# Wait briefly for RPC to come up (KDF prints "Listening on 127.0.0.1:7783" when ready).
for i in $(seq 1 40); do
  sleep 0.25
  if curl -s --max-time 1 -o /dev/null -w "%{http_code}" "$KDF_URL" 2>/dev/null \
      | grep -qE '^(200|400|404|405|500)$'; then
    echo "KDF is up (pid $(cat "$KDF_PID_FILE"))."
    exit 0
  fi
done

echo "WARNING: KDF did not respond on $KDF_URL within 10s. Tail $LOG to debug." >&2
exit 1
