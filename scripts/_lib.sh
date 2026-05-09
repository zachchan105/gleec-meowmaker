#!/usr/bin/env bash
# Shared helpers for gleec-meowmaker scripts. Sourced, not executed.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KDF_DIR="$REPO_ROOT/kdf"
BOT_DIR="$REPO_ROOT/bot"
KDF_BIN="${KDF_BIN:-$REPO_ROOT/kdf/kdf}"
KDF_URL="${KDF_URL:-http://127.0.0.1:7783}"
KDF_PID_FILE="$KDF_DIR/.kdf.pid"
BOT_PID_FILE="$BOT_DIR/.bot.pid"

require_jq() {
  command -v jq >/dev/null 2>&1 || {
    echo "ERROR: 'jq' is required. Install with: sudo apt install jq" >&2
    exit 1
  }
}

require_curl() {
  command -v curl >/dev/null 2>&1 || {
    echo "ERROR: 'curl' is required." >&2
    exit 1
  }
}

require_mm2_json() {
  if [[ ! -f "$KDF_DIR/MM2.json" ]]; then
    echo "ERROR: $KDF_DIR/MM2.json not found." >&2
    echo "Copy MM2.json.example to MM2.json and fill in passphrase + rpc_password." >&2
    exit 1
  fi
}

# Reads rpc_password from MM2.json. Caller must ensure MM2.json exists.
get_userpass() {
  jq -r '.rpc_password' "$KDF_DIR/MM2.json"
}

kdf_call() {
  # $1 = JSON body
  curl -s --max-time 30 --url "$KDF_URL" --data "$1"
}
