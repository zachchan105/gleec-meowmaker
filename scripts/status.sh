#!/usr/bin/env bash
# Quick health snapshot: NonKYC mid, MEWC + LTC balances, current maker orders
# for the MEWC/LTC pair.

source "$(dirname "$0")/_lib.sh"
require_curl
require_jq
require_mm2_json

USERPASS=$(get_userpass)

echo "=== NonKYC reference prices ==="
mewc_usd=$(curl -s --max-time 8 https://api.nonkyc.io/api/v2/market/getbysymbol/MEWC_USDT | jq -r '.lastPriceNumber // empty')
ltc_usd=$(curl  -s --max-time 8 https://api.nonkyc.io/api/v2/market/getbysymbol/LTC_USDT  | jq -r '.lastPriceNumber // empty')
pool=$(curl     -s --max-time 8 https://api.nonkyc.io/api/v2/market/getbysymbol/MEWC_LTC  | jq -r '.lastPriceNumber // empty')

if [[ -n "$mewc_usd" && -n "$ltc_usd" ]]; then
  mid=$(awk -v a="$mewc_usd" -v b="$ltc_usd" 'BEGIN{printf "%.10f", a/b}')
  echo "  MEWC/USDT: $mewc_usd"
  echo "  LTC/USDT : $ltc_usd"
  echo "  derived MEWC/LTC mid: $mid"
  echo "  MEWC/LTC pool last  : $pool"
else
  echo "  (NonKYC unreachable)"
fi
echo

echo "=== Wallet balances (HD account 0, External chain) ==="
for coin in LTC MEWC; do
  body=$(jq -n --arg userpass "$USERPASS" --arg coin "$coin" \
    '{userpass: $userpass, method: "account_balance", mmrpc: "2.0",
      params: {coin: $coin, account_index: 0, chain: "External", limit: 50}}')
  resp=$(kdf_call "$body")
  if echo "$resp" | jq -e '.result.addresses' >/dev/null 2>&1; then
    addr=$(echo "$resp" | jq -r --arg c "$coin" '.result.addresses[0].address')
    sum=$(echo "$resp" | jq -r --arg c "$coin" \
      '[.result.addresses[].balance[$c].spendable | tonumber] | add')
    printf "  %-6s %s  (%s)\n" "$coin" "$sum" "$addr"
  else
    printf "  %-6s NOT ENABLED  (%s)\n" "$coin" "$resp"
  fi
done
echo

echo "=== Open maker orders (MEWC/LTC) ==="
orders=$(kdf_call "{\"userpass\":\"$USERPASS\",\"method\":\"my_orders\"}")
echo "$orders" | jq '
  .result.maker_orders
  | to_entries
  | map(select(
      (.value.base == "MEWC" and .value.rel == "LTC") or
      (.value.base == "LTC"  and .value.rel == "MEWC")
    ))
  | map({uuid: .key, base: .value.base, rel: .value.rel, price: .value.price, available_amount: .value.available_amount})
'
