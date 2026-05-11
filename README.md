# gleec-meowmaker — passive Meowcoin (MEWC) market maker on the Gleec DEX

*"meowmaker" for short.*

A small, working reference implementation of a passive market-maker that
drives the [Komodo DeFi Framework](https://github.com/GLEECBTC/komodo-defi-framework)
(KDF, the atomic-swap engine that powers the Gleec DEX) to keep two-sided
liquidity on the **MEWC/LTC** pair using public Electrum servers (no full
nodes required). Forkable for any other UTXO pair on KDF with a few config
edits.

Built so any Meowcoin holder can run their own market-making node and
contribute to a tighter, deeper MEWC orderbook on Gleec — no permission,
no custody, just your seed and ~$40 of inventory.

> **⚠️ Real money disclaimer.** This software places real maker orders
> with real coins on a live atomic-swap DEX. KDF protocol DEX fees, on-chain
> tx fees, your configured spread, and bad pricing all cost real money.
> You can lose your entire posted balance to mispricing, failed swaps,
> bugs, or operational error. Read the code and run with small amounts
> first. No warranty — see [LICENSE](./LICENSE).

> **🤖 Want an AI agent to operate this for you?** The repo is laid out
> to be agent-friendly: idempotent scripts, predictable command surface,
> verbose logs as the observability layer, and a separate
> [AGENTS.md](./AGENTS.md) describing what an agent can and can't do
> safely. Clone the repo, open it in Cursor / Claude Code / Codex CLI /
> Aider / your tool of choice, and ask "start the bot and watch it" —
> the agent has everything it needs.

- **Pair**: MEWC <-> LTC
- **Strategy**: passive maker, single seed; no wash trading.
- **Sizing**: `usd_per_side` sets a symmetric USD target per leg (default
  ~$20 sell MEWC + ~$20 sell LTC). Optional `usd_per_side_mewc` /
  `usd_per_side_ltc` override each leg for uneven inventory.
- **Pricing**: NonKYC `MEWC/USDT` and `LTC/USDT` mids, divided to get a
  fair MEWC/LTC ratio. NonKYC's `MEWC/LTC` AMM pool is used only as a
  drift sanity check.
- **Refresh**: every 90s; cycle = cancel-all + repost both sides.
- **Shutdown**: SIGTERM/SIGINT cancels all open MEWC/LTC orders before exit.

## Layout

```
gleec-meowmaker/
  kdf/
    kdf                     # the KDF binary (gitignored — you supply this)
    MM2.json.example        # template (you create MM2.json from this)
    coins                   # MEWC + LTC entries from GLEECBTC/coins
    electrum_servers.json   # cipig (LTC) + community (MEWC) Electrum lists
  bot/
    main.py                 # the loop
    kdf_client.py           # async KDF RPC client
    price_oracle.py         # NonKYC fetcher
    dashboard.py            # read-only web UI (see "Dashboard" below)
    config.example.toml     # template (you create config.toml from this)
  scripts/
    start_kdf.sh stop_kdf.sh
    enable_coins.sh
    status.sh
    start_bot.sh stop_bot.sh
    start_dashboard.sh stop_dashboard.sh
  requirements.txt
  LICENSE                   # MIT
  AGENTS.md                 # quick orientation for AI coding agents
```

## Prereqs

- Linux x86_64 (other platforms work; scripts assume bash)
- The KDF binary placed at `kdf/kdf` (download a release from the
  [GLEECBTC fork](https://github.com/GLEECBTC/komodo-defi-framework/releases),
  or set `KDF_BIN=/path/to/kdf` to point at one already on disk)
- `python3` (>= 3.10), `curl`, `jq`
- A BIP-39 wallet seed that holds **MEWC** and **LTC**
- Network access to NonKYC + the Electrum servers in `kdf/electrum_servers.json`

Install Python deps once (a venv is fine and recommended):

```bash
cd /path/to/gleec-meowmaker
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

If you skip the venv, `pip install --user -r requirements.txt` works too.
On Python 3.11+, `tomli` is skipped automatically (stdlib `tomllib` is used).

## One-time setup

### 1. KDF config

```bash
cp kdf/MM2.json.example kdf/MM2.json
```

Edit `kdf/MM2.json`:

- `passphrase` — your 12 or 24-word BIP-39 seed (the wallet that holds MEWC + LTC).
- `rpc_password` — any strong random string. **Remember this exact value.**
  This is the password our bot uses to talk to KDF on `127.0.0.1:7783`. It
  is not a coin daemon password (we use Electrum, no daemons).
- `enable_hd` — leave as `true`. This makes KDF treat the seed as proper
  BIP-39 and derive at the standard `m/44'/COIN'/0'/0/0` (account 0,
  external chain, address 0) — matching Gleec DEX, AtomicDEX, Komodo
  Wallet HD mode, Trezor, and Electrum. **Without `enable_hd`, KDF falls
  back to legacy "iguana" mode (just `sha256(passphrase)`), which produces
  completely different addresses.**
- `netid` — `6133` (current KDF mainnet as of v3.0.0-beta). The old
  `8762` is in KDF's `DEPRECATED_NETID_LIST` and will refuse to start.
- `seednodes` — required since v2.5.0-beta (no more hardcoded defaults).
  The 4 `seedNN.kmdefi.net` entries in `MM2.json.example` are the
  KomodoPlatform production seeds; you can add/swap others freely.

**Password rules for `rpc_password`:** must be 8-32 chars, contain at
least one digit, lowercase, uppercase, AND special character; cannot
contain the word "password" or 3 of the same character in a row. KDF
refuses to start otherwise. Disable with `"allow_weak_password": true`
if you want to opt out (not recommended).

  Note for LTC: KDF derives a **legacy P2PKH** address (starts with `L`) at
  `m/44'/2'/0'/0/0`. If your existing LTC funds live at a SegWit (`M...`)
  or Native SegWit (`ltc1...`) address, KDF will not see them — those
  use `m/49'/2'` and `m/84'/2'` respectively. Send a fresh $20 from your
  SegWit wallet to whatever `L`-prefix address `enable_coins.sh` prints.

`MM2.json` is gitignored so your seed never leaves the box.

### 2. Bot config

```bash
cp bot/config.example.toml bot/config.toml
```

Edit `bot/config.toml`:

- `rpc_password` — must match `MM2.json.rpc_password` byte-for-byte.
- (Optional) tweak `usd_per_side`, optional `usd_per_side_mewc` /
  `usd_per_side_ltc`, `spread`, `refresh_seconds`.
  Defaults are 1.5% half-spread, $20/side symmetric, 90s refresh.

`config.toml` is gitignored.

## Running

```bash
# 0. Drop the kdf binary at kdf/kdf and chmod +x it (one-time).
#    Or set KDF_BIN=/path/to/kdf in your environment.
chmod +x kdf/kdf

# 1. Boot KDF (logs to kdf/kdf.log)
./scripts/start_kdf.sh

# 2. Activate MEWC and LTC against their Electrum servers.
#    Prints your MEWC + LTC addresses + balances.
./scripts/enable_coins.sh

# 3. Fund those two addresses with $20 of each (skip if already funded).

# 4. Sanity check: NonKYC mid + your balances + open orders
./scripts/status.sh

# 5. Start the maker (logs to bot/bot.log)
./scripts/start_bot.sh

# Watch what it does
tail -f bot/bot.log
```

To stop:

```bash
./scripts/stop_bot.sh   # cancels both MEWC/LTC orders, then exits
./scripts/stop_kdf.sh
```

## Dashboard (optional)

A small read-only web UI at `bot/dashboard.py` shows a live view of:

- bot process liveness, configured spread / sizing / refresh cadence
- NonKYC reference prices, derived mid, and pool drift
- wallet balances vs. configured USD targets per coin (with bars)
- the **full public MEWC/LTC orderbook** with your maker orders highlighted
  (the cat-paw row 🐾) so you can see exactly where you sit in the book
- the last 10 maker swap fills with their state (in-flight, finished, failed)
- a live tail of `bot/bot.log`

Run it as a separate, idempotent process:

```bash
./scripts/start_dashboard.sh    # → http://127.0.0.1:7784/
./scripts/stop_dashboard.sh
```

The dashboard reuses `bot/config.toml` for `kdf_url` + `rpc_password` so it
can read KDF state. It is **read-only by design** — it never calls
`setprice`, `withdraw`, or any `cancel_*` RPC, and the `rpc_password` is
never sent to the browser.

It binds to `127.0.0.1:7784` by default. **Do not bind it to a public
interface.** The page exposes balances, addresses, your open orders, and
the bot's log; treat it like an admin panel. If you need remote access,
SSH-tunnel instead:

```bash
ssh -L 7784:127.0.0.1:7784 your-host
# then open http://127.0.0.1:7784/ on your laptop
```

## What the bot logs every cycle

```
oracle: MEWC/USDT=0.000082  LTC/USDT=87.4  mid=0.00000094 LTC/MEWC  pool=0.00000093  drift=0.0107
balances: MEWC=215000 (M...)  LTC=0.228 (L...)
cancelled 2 existing order(s)
POSTED sell  MEWC -> LTC  price=0.00000095  vol=215000  uuid=...
POSTED buy   LTC  -> MEWC price=1071000     vol=0.228   uuid=...
```

The "buy" side is expressed as a maker order that **sells LTC for MEWC**,
because KDF's `setprice` always quotes `rel per base` and orders are sided
on `(base, rel)`. Same effect: an outside taker holding MEWC can grab your
LTC at that price, which is what "buy MEWC with LTC" means from your side.

## Troubleshooting

**`./scripts/start_kdf.sh` says KDF didn't respond**

```bash
tail -50 kdf/kdf.log
```

Most common causes: missing/invalid `passphrase`, `rpc_password` empty,
port 7783 already taken (e.g. Komodo Wallet GUI is also running).

**`enable_coins.sh` shows an error like `electrum init: ...`**

Probably an Electrum server is down. Re-run; `enable` rotates through
the server list. If a specific server is consistently bad, remove it
from `kdf/electrum_servers.json` and re-run.

**Bot logs `price fetch failed`**

NonKYC is intermittently slow. The bot does not cancel orders on a
single fetch failure — it just waits for the next cycle. If it persists,
NonKYC may be down; orders stay live at their last-posted price until
you stop the bot.

**Bot logs `drift X exceeds max_drift_vs_pool`**

Our USDT-derived mid disagrees with the NonKYC `MEWC/LTC` pool's last
trade by more than `max_drift_vs_pool`. Default is generous (0.50)
because the pool is illiquid and stale. Bump it higher or set to `0`
to disable.

**Bot logs `currently_matching` on cancel**

A swap is in flight on that order — KDF won't let it be cancelled
mid-swap. Bot will skip reposting that side this cycle and try again
on the next one.

**`my_balance` shows zero after funding**

KDF only sees confirmed UTXOs. MEWC needs 3 confirmations, LTC needs 2.
Wait a few minutes after sending funds.

**Lost the seed / want to reset KDF state**

The wallet is derived deterministically from `passphrase`. To wipe
local KDF state without losing funds, stop everything and `rm -rf
kdf/DB`. Re-running `start_kdf.sh` then `enable_coins.sh` rebuilds it.

## Security notes

- `kdf/MM2.json` and `bot/config.toml` are **gitignored**. Don't move
  the seed elsewhere or commit either file.
- KDF binds RPC to `127.0.0.1:7783` (loopback only). Anyone with shell
  access on this box and the `rpc_password` can drain your wallet via
  `withdraw`. Treat the password like a private key.
- The Electrum servers in `kdf/electrum_servers.json` are third-party.
  They cannot steal funds (KDF signs locally) but they can see the
  addresses you query.

## What this bot deliberately does NOT do

- No wash trading / self-cross. Single seed; volume only prints when an
  outside taker hits one of your orders.
- No Telegram alerts.
- No auto-rebalance between MEWC and LTC. If one side gets fully filled,
  the bot will keep posting smaller and smaller orders on the other side
  until you manually rebalance.
- No GUI. Use `scripts/status.sh` or attach Komodo Wallet to the same
  `MM2.json` if you want a visual.

## Adapting this to a different KDF pair

The repo is intentionally MEWC/LTC-shaped, but the moving parts are
small and well-isolated. To target another UTXO pair on KDF:

1. Replace `BASE` / `REL` constants in `bot/main.py`.
2. Add the two coins' entries to `kdf/coins` (copy from
   [GLEECBTC/coins](https://github.com/GLEECBTC/coins)).
3. Add Electrum servers for both coins to `kdf/electrum_servers.json`.
4. Update `bot/price_oracle.py` — currently hardcoded to NonKYC's
   `MEWC/USDT` and `LTC/USDT` markets; swap in whatever oracle quotes
   your pair (CoinGecko, Binance, MEXC, another DEX, etc.).
5. Tweak `min_post_volume_*` in `config.toml` to match the new coins'
   dust limits and your sizing.

Everything else (scripts, KDF client, cycle loop, signal handling) is
pair-agnostic and should work as-is.

## License

[MIT](./LICENSE).
