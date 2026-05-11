# AGENTS.md — operating gleec-meowmaker with an AI coding agent

This repo is structured to be friendly to AI coding agents (Cursor,
Claude Code, Codex CLI, OpenAI Agents, Aider, etc.) acting as the
operator. If you've cloned this repo and pointed an agent at it, this
file is the agent's quick orientation; the [README](./README.md) is
the long-form reference.

The goal: an operator should be able to say things like "start the
bot", "what's it doing right now?", "tighten the spread to 0.5%",
"add another MEWC Electrum server", or "why did that swap fail?" and
have the agent execute correctly without touching funds it shouldn't.

## Project shape (1 minute version)

- **Python market-maker bot** in `bot/` (asyncio loop, ~250 LOC).
 Single process. Reads price + balance, posts maker orders, repeats.
- **Read-only web dashboard** in `bot/dashboard.py` (stdlib HTTP
 server + embedded single-page UI). Optional. Separate process from
 the bot, never calls write-RPCs, default-binds to `127.0.0.1:7784`.
- **Bash control scripts** in `scripts/` (start/stop/status). All
 idempotent — re-running a started script is a no-op.
- **Komodo DeFi Framework binary** at `kdf/kdf` (Rust, user-supplied
  from <https://github.com/GLEECBTC/komodo-defi-framework/releases>).
  This is the atomic-swap engine the bot drives over local HTTP RPC.
- **Configuration**: `kdf/MM2.json` (KDF: seed + rpc password) and
  `bot/config.toml` (bot: rpc password, sizing, spread). Both are
  gitignored. Templates live next to them as `*.example`.
- **Runtime state**: `kdf/kdf.log`, `bot/bot.log`, `kdf/DB/`. All
  gitignored. The agent is encouraged to read the logs liberally —
  they are the system's primary observability surface.

## What an agent CAN do safely

- Read every file in the repo (everything but the gitignored
  config/log/DB content is committed).
- Modify `bot/main.py`, `bot/kdf_client.py`, `bot/price_oracle.py`
 for strategy or feature changes.
- Modify `bot/dashboard.py` to add/change visualizations. The
 dashboard is read-only by contract (no `setprice`, `withdraw`,
 `cancel_*`); preserve that — anything that mutates state belongs
 in the bot, not the dashboard.
- Modify `bot/config.toml` to tweak `spread`, `usd_per_side`, optional
  `usd_per_side_mewc` / `usd_per_side_ltc`, `refresh_seconds`,
  `min_post_volume_*`, `max_drift_vs_pool`.
- Modify `kdf/electrum_servers.json` to add/remove Electrum servers.
- Run any script under `scripts/` — they are designed to be safe to
  invoke any time and report state clearly.
- Tail or read `bot/bot.log` and `kdf/kdf.log` to diagnose behavior.
- Call KDF RPC directly using the password from `kdf/MM2.json` for
  read-only methods (`my_balance`, `my_orders`, `orderbook`,
  `my_recent_swaps`, `my_swap_status`, `account_balance`). See
  `scripts/status.sh` for the calling pattern.

## What an agent should NOT do without explicit user consent

- **Never commit** `kdf/MM2.json` or `bot/config.toml`. They contain
  the seed phrase and rpc password. The `.gitignore` already protects
  this; do not `git add -f` either file.
- **Never push** to a public remote without confirming the user wants
  it there.
- **Never call** `withdraw`, `send_raw_transaction`, `setprice`
  outside the bot, or any RPC that moves coins, without the user
  asking for that specific action.
- **Never change** `enable_hd: true` in `MM2.json`. It changes the
  derivation scheme and makes the funded addresses appear empty.
- **Never delete** `kdf/DB/` while a swap is in flight (`my_orders`
  shows `currently_matching` or `my_recent_swaps` shows an unfinished
  swap). It would lose swap state and may strand funds in HTLCs until
  the refund timelock expires.
- **Never widen** `usd_per_side` / `usd_per_side_mewc` / `usd_per_side_ltc`,
  change `spread`, or alter pricing logic without first surfacing the
  financial impact in plain English.
- **Never `kill -9`** the bot — its SIGTERM handler cancels open
  orders before exit. Use `./scripts/stop_bot.sh` instead.

## Useful one-liners the agent will reach for

```bash
# Health snapshot: prices, balances, open orders.
./scripts/status.sh

# Watch the bot live.
tail -f bot/bot.log

# Watch KDF live (verbose; grep for 'swap' or 'error' usually enough).
tail -f kdf/kdf.log

# Recent swap history (full event timeline per swap).
USERPASS=$(jq -r .rpc_password kdf/MM2.json)
curl -s --url http://127.0.0.1:7783 \
  --data "{\"userpass\":\"$USERPASS\",\"method\":\"my_recent_swaps\",\"limit\":10}" | jq

# Detail on a specific swap.
curl -s --url http://127.0.0.1:7783 --data "{
  \"userpass\":\"$USERPASS\",
  \"method\":\"my_swap_status\",
  \"params\":{\"uuid\":\"<UUID>\"}
}" | jq

# Public orderbook view of the pair.
curl -s --url http://127.0.0.1:7783 \
  --data "{\"userpass\":\"$USERPASS\",\"method\":\"orderbook\",\"base\":\"MEWC\",\"rel\":\"LTC\"}" | jq

# Our own open orders only.
curl -s --url http://127.0.0.1:7783 \
  --data "{\"userpass\":\"$USERPASS\",\"method\":\"my_orders\"}" | jq
```

## Common operator requests, mapped to actions

| User says | Agent does |
|-----------|-----------|
| "Start everything" | `./scripts/start_kdf.sh && ./scripts/enable_coins.sh && ./scripts/start_bot.sh` |
| "Stop everything" | `./scripts/stop_bot.sh && ./scripts/stop_kdf.sh` |
| "Status" / "How's the bot?" | `./scripts/status.sh` then summarize |
| "Show me / open the dashboard" | `./scripts/start_dashboard.sh`, report URL (default `http://127.0.0.1:7784/`) |
| "Stop the dashboard" | `./scripts/stop_dashboard.sh` |
| "What's the bot doing?" | `tail -50 bot/bot.log` |
| "Why did X happen?" | Read `bot/bot.log` AND `kdf/kdf.log`, then explain |
| "Tighten the spread to 1%" | Edit `spread = 0.01` in `bot/config.toml`, then `./scripts/stop_bot.sh && ./scripts/start_bot.sh` |
| "Increase sizing to $50/side" | Confirm with user first (real money), then edit `usd_per_side = 50.0`, restart bot |
| "Add Electrum server X for MEWC" | Edit `kdf/electrum_servers.json`, then `disable_coin` + re-run `enable_coins.sh` for that coin |
| "Cancel all my orders" | `cancel_all_orders` RPC with `{"by":{"type":"All"}}`, or just `./scripts/stop_bot.sh` |

## Runtime invariants the agent should maintain

1. **KDF up before the bot.** The bot will refuse / error if KDF RPC
   is not reachable on `127.0.0.1:7783`.
2. **Coins activated before the bot posts.** Run `enable_coins.sh`
   after any KDF restart, even if the seed/passphrase hasn't changed.
3. **One bot process at a time.** Two bots on the same KDF would race
   on cancel/repost cycles. `start_bot.sh` checks the pid file but
   it's not bulletproof; trust but verify with `pgrep -f bot.main`.
4. **rpc_password byte-equality.** `bot/config.toml.rpc_password`
   must match `kdf/MM2.json.rpc_password` exactly. Mismatches surface
   as 401 errors in `bot/bot.log`.
5. **Graceful shutdown.** Always `./scripts/stop_bot.sh` (SIGTERM,
 cancels orders) before `./scripts/stop_kdf.sh`. Reverse order
 leaves orders live on the P2P network with KDF dead, which is bad.
6. **Dashboard stays loopback.** `bot/dashboard.py` defaults to
 binding `127.0.0.1`. Don't change `dashboard_host` in
 `config.toml` to a public address without explicit user consent —
 the page exposes balances, addresses, open orders, and the bot
 log. If the user wants remote access, suggest an SSH tunnel
 (`ssh -L 7784:127.0.0.1:7784`) instead.

## When in doubt

Read the README. Then ask the user before doing anything that touches
funds, configuration of the trading parameters, the orderbook, or the
git remote.
