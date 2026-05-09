#!/usr/bin/env bash
# Activates MEWC and LTC against the running KDF using the v2 (HD-aware)
# task::enable_utxo flow. Reads server lists from kdf/electrum_servers.json.
#
# Why v2 and not legacy `electrum`?
#   With `enable_hd: true` set in MM2.json, the legacy `electrum` method
#   activates the coin but then fails with `UnexpectedDerivationMethod
#   (ExpectedSingleAddress)` when it tries to query a single address's
#   balance for the response. We use the HD-aware v2 RPC instead.

source "$(dirname "$0")/_lib.sh"
require_curl
require_jq
require_mm2_json

USERPASS=$(get_userpass)
SERVERS_FILE="$KDF_DIR/electrum_servers.json"

if [[ ! -f "$SERVERS_FILE" ]]; then
  echo "ERROR: $SERVERS_FILE not found." >&2
  exit 1
fi

POLL_TIMEOUT_SECS="${POLL_TIMEOUT_SECS:-180}"
POLL_INTERVAL_SECS="${POLL_INTERVAL_SECS:-2}"

# Activates one coin via task::enable_utxo, then polls until completion.
# Prints a summary line on success or the error body on failure.
enable_one() {
  local coin=$1
  local servers
  servers=$(jq -c --arg c "$coin" '.[$c]' "$SERVERS_FILE")

  if [[ "$servers" == "null" || -z "$servers" ]]; then
    echo "ERROR: no Electrum servers configured for $coin in $SERVERS_FILE" >&2
    return 1
  fi

  local init_body
  init_body=$(jq -n \
    --arg userpass "$USERPASS" \
    --arg coin "$coin" \
    --argjson servers "$servers" \
    '{
       userpass: $userpass,
       method: "task::enable_utxo::init",
       mmrpc: "2.0",
       params: {
         ticker: $coin,
         activation_params: {
           mode: { rpc: "Electrum", rpc_data: { servers: $servers } }
         }
       }
     }')

  echo "Activating $coin via Electrum (HD mode)..."
  local init_resp task_id
  init_resp=$(kdf_call "$init_body")
  task_id=$(echo "$init_resp" | jq -r '.result.task_id // empty')

  if [[ -z "$task_id" ]]; then
    # Most common cause: coin is already activated. Surface the raw error.
    echo "  $coin init response: $init_resp"
    return 0
  fi

  local status_body
  status_body=$(jq -n \
    --arg userpass "$USERPASS" \
    --argjson task_id "$task_id" \
    '{
       userpass: $userpass,
       method: "task::enable_utxo::status",
       mmrpc: "2.0",
       params: { task_id: $task_id, forget_if_finished: true }
     }')

  local elapsed=0 status resp
  while (( elapsed < POLL_TIMEOUT_SECS )); do
    resp=$(kdf_call "$status_body")
    status=$(echo "$resp" | jq -r '.result.status // empty')

    case "$status" in
      Ok)
        # The activation result's JSON shape for `total_balance` varies
        # (sometimes a flat CoinBalance, sometimes a map keyed by ticker),
        # so query `account_balance` v2 RPC for a guaranteed-stable summary.
        local bal_body bal_resp addr0 total
        bal_body=$(jq -n --arg userpass "$USERPASS" --arg coin "$coin" \
          '{userpass: $userpass, method: "account_balance", mmrpc: "2.0",
            params: {coin: $coin, account_index: 0, chain: "External", limit: 50}}')
        bal_resp=$(kdf_call "$bal_body")
        addr0=$(echo "$bal_resp" | jq -r '.result.addresses[0].address // empty')
        total=$(echo "$bal_resp" | jq -r --arg c "$coin" \
          '[.result.addresses[].balance[$c].spendable | tonumber] | add // 0')
        printf "  %-6s OK  address=%s  spendable=%s\n" "$coin" "$addr0" "$total"
        return 0
        ;;
      Error)
        echo "  $coin ERROR: $(echo "$resp" | jq -c '.result.details')"
        return 1
        ;;
      InProgress)
        local stage
        stage=$(echo "$resp" | jq -r '.result.details // "?"')
        printf "  %s ... %ss elapsed (stage: %s)\n" "$coin" "$elapsed" "$stage"
        ;;
      *)
        echo "  $coin unexpected status response: $resp"
        return 1
        ;;
    esac

    sleep "$POLL_INTERVAL_SECS"
    elapsed=$(( elapsed + POLL_INTERVAL_SECS ))
  done
  echo
  echo "  $coin TIMEOUT after ${POLL_TIMEOUT_SECS}s"
  return 1
}

enable_one LTC
enable_one MEWC

# Note: legacy `get_enabled_coins` errors with `'my_address' is deprecated for
# HD wallets`, so we don't call it here. Use `./scripts/status.sh` to re-query
# balances at any time.
