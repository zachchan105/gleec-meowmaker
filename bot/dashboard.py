"""Read-only web dashboard for meowmaker.

Serves a single-page UI at http://127.0.0.1:7784/ (configurable) showing:

  - Bot process liveness, configured spread / USD sizing / refresh
  - NonKYC oracle prices (MEWC/USDT, LTC/USDT, derived mid, pool last)
  - Wallet balances vs. configured target sizing
  - Full MEWC/LTC public orderbook with OUR orders highlighted
  - Last 10 maker swap fills with their state
  - Live tail of bot/bot.log

This process is read-only by design: it never calls setprice, withdraw,
or any cancel_*. Safe to start, stop, and restart at any time without
affecting the bot or the orderbook.

SECURITY: defaults to 127.0.0.1 only. Do not bind to a public address.
The dashboard reuses bot/config.toml for kdf_url + rpc_password so it
can read KDF state, but the rpc_password is NEVER sent to the browser.

Run: `python -m bot.dashboard` (or `./scripts/start_dashboard.sh`).
"""

from __future__ import annotations

import json
import logging
import os
import signal
import sys
import threading
import time
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable

import httpx

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]


LOG = logging.getLogger("dashboard")

BASE = "MEWC"
REL = "LTC"
NONKYC = "https://api.nonkyc.io/api/v2/market/getbysymbol"

# Dashboard files live next to bot.py
HERE = Path(__file__).parent
CONFIG_PATH = HERE / "config.toml"
BOT_LOG_PATH = HERE / "bot.log"
BOT_PID_PATH = HERE / ".bot.pid"


# ---------------------------------------------------------------------------
# Tiny TTL cache so multiple browser tabs don't hammer KDF / NonKYC.
# ---------------------------------------------------------------------------

_cache: dict[str, tuple[float, Any]] = {}
_cache_lock = threading.Lock()


def _cached(key: str, ttl: float, fetch: Callable[[], Any]) -> Any:
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    val = fetch()
    with _cache_lock:
        _cache[key] = (now, val)
    return val


# ---------------------------------------------------------------------------
# KDF + NonKYC fetchers (sync; one connection per request is fine for a
# single-user local dashboard with TTL caching in front).
# ---------------------------------------------------------------------------

def _kdf_call(url: str, userpass: str, body: dict) -> dict:
    payload = {"userpass": userpass, **body}
    with httpx.Client(timeout=15.0) as c:
        r = c.post(url, json=payload)
    try:
        return r.json()
    except ValueError:
        return {"error": f"non-JSON response (HTTP {r.status_code})"}


def _nonkyc_last(symbol: str) -> str | None:
    try:
        with httpx.Client(timeout=8.0) as c:
            r = c.get(f"{NONKYC}/{symbol}")
        last = r.json().get("lastPriceNumber")
        if last in (None, 0):
            return None
        return str(last)
    except Exception:  # noqa: BLE001
        return None


def _gather_balances(url: str, pw: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for coin in (BASE, REL):
        d = _kdf_call(url, pw, {
            "method": "account_balance",
            "mmrpc": "2.0",
            "params": {"coin": coin, "account_index": 0, "chain": "External", "limit": 50},
        })
        addresses = ((d.get("result") or {}).get("addresses")) or []
        spendable = Decimal(0)
        unspendable = Decimal(0)
        for a in addresses:
            bal = ((a.get("balance") or {}).get(coin)) or {}
            spendable += Decimal(str(bal.get("spendable", "0")))
            unspendable += Decimal(str(bal.get("unspendable", "0")))
        out[coin] = {
            "address": addresses[0]["address"] if addresses else None,
            "spendable": str(spendable),
            "unspendable": str(unspendable),
            "ok": bool(addresses),
        }
    return out


def _gather_my_orders(url: str, pw: str) -> list[dict]:
    d = _kdf_call(url, pw, {"method": "my_orders"})
    out: list[dict] = []
    for uuid, o in (((d.get("result") or {}).get("maker_orders")) or {}).items():
        ob, orl = o.get("base"), o.get("rel")
        if (ob == BASE and orl == REL) or (ob == REL and orl == BASE):
            out.append({
                "uuid": uuid,
                "base": ob,
                "rel": orl,
                "price": str(o.get("price", "0")),
                "available_amount": str(o.get("available_amount", "0")),
                "created_at": o.get("created_at"),
            })
    return out


def _gather_orderbook(url: str, pw: str) -> dict:
    d = _kdf_call(url, pw, {"method": "orderbook", "base": BASE, "rel": REL})

    def _slim(entries: list[dict]) -> list[dict]:
        out = []
        for e in entries or []:
            out.append({
                "uuid": e.get("uuid"),
                "address": e.get("address"),
                "price": str(e.get("price", "0")),
                "maxvolume": str(e.get("maxvolume", "0")),
                "is_mine": bool(e.get("is_mine")),
            })
        return out

    return {
        "asks": _slim(d.get("asks") or []),
        "bids": _slim(d.get("bids") or []),
        "numasks": d.get("numasks") or 0,
        "numbids": d.get("numbids") or 0,
        "timestamp": d.get("timestamp"),
    }


def _gather_recent_swaps(url: str, pw: str) -> list[dict]:
    d = _kdf_call(url, pw, {"method": "my_recent_swaps", "limit": 10})
    out: list[dict] = []
    for s in (((d.get("result") or {}).get("swaps")) or []):
        mi = s.get("my_info") or {}
        events = s.get("events") or []
        last_event_type = ""
        if events:
            last_event_type = ((events[-1].get("event") or {}).get("type")) or ""
        out.append({
            "uuid": s.get("uuid"),
            "type": s.get("type"),
            "my_coin": mi.get("my_coin"),
            "my_amount": mi.get("my_amount"),
            "other_coin": mi.get("other_coin"),
            "other_amount": mi.get("other_amount"),
            "started_at": (events[0]["timestamp"] / 1000) if events else None,
            "last_event": last_event_type,
            "is_finished": last_event_type == "Finished",
        })
    return out


def _gather_oracle() -> dict:
    return {
        "mewc_usd": _nonkyc_last("MEWC_USDT"),
        "ltc_usd": _nonkyc_last("LTC_USDT"),
        "pool_last": _nonkyc_last("MEWC_LTC"),
    }


def _read_log_tail(n: int = 40) -> list[str]:
    if not BOT_LOG_PATH.exists():
        return []
    try:
        with BOT_LOG_PATH.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            read_n = min(size, 64 * 1024)
            f.seek(size - read_n)
            tail = f.read().decode("utf-8", errors="replace")
        return tail.splitlines()[-n:]
    except OSError:
        return []


def _bot_pid() -> int | None:
    if not BOT_PID_PATH.exists():
        return None
    try:
        pid = int(BOT_PID_PATH.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, OSError, ProcessLookupError):
        return None


def _usd_targets_resolved(cfg: dict) -> tuple[Any, Any]:
    """Match `bot.main._usd_targets` for dashboard bars."""
    base = cfg["usd_per_side"]
    um = cfg.get("usd_per_side_mewc", base)
    ul = cfg.get("usd_per_side_ltc", base)
    return um, ul


def _gather_snapshot(cfg: dict) -> dict:
    url = cfg["kdf_url"]
    pw = cfg["rpc_password"]

    balances = _cached("balances", 2.0, lambda: _gather_balances(url, pw))
    my_orders = _cached("my_orders", 2.0, lambda: _gather_my_orders(url, pw))
    orderbook = _cached("orderbook", 2.0, lambda: _gather_orderbook(url, pw))
    swaps = _cached("swaps", 5.0, lambda: _gather_recent_swaps(url, pw))
    oracle = _cached("oracle", 10.0, _gather_oracle)

    um, ul = _usd_targets_resolved(cfg)
    return {
        "now": time.time(),
        "config": {
            "pair": f"{BASE}/{REL}",
            "spread": cfg.get("spread"),
            "usd_per_side": cfg.get("usd_per_side"),
            "usd_per_side_mewc": cfg.get("usd_per_side_mewc"),
            "usd_per_side_ltc": cfg.get("usd_per_side_ltc"),
            "usd_target_mewc": um,
            "usd_target_ltc": ul,
            "refresh_seconds": cfg.get("refresh_seconds"),
            "min_post_volume_mewc": cfg.get("min_post_volume_mewc"),
            "min_post_volume_ltc": cfg.get("min_post_volume_ltc"),
        },
        "bot_pid": _bot_pid(),
        "balances": balances,
        "my_orders": my_orders,
        "orderbook": orderbook,
        "recent_swaps": swaps,
        "oracle": oracle,
        "log": _read_log_tail(40),
    }


# ---------------------------------------------------------------------------
# HTTP server
# ---------------------------------------------------------------------------


INDEX_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>meowmaker dashboard</title>
<style>
  :root {
    --bg: #0d1117;
    --panel: #161b22;
    --panel-2: #1c222b;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --ours: #ff8c42;
    --ours-soft: rgba(255, 140, 66, 0.15);
    --ask: #f85149;
    --ask-soft: rgba(248, 81, 73, 0.18);
    --bid: #3fb950;
    --bid-soft: rgba(63, 185, 80, 0.18);
    --accent: #58a6ff;
    --warn: #d29922;
  }
  * { box-sizing: border-box; }
  html, body {
    margin: 0; padding: 0;
    background: var(--bg);
    color: var(--text);
    font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto,
                 "Helvetica Neue", Arial, sans-serif;
    font-size: 14px;
  }
  body { padding: 16px; max-width: 1280px; margin: 0 auto; }
  h1 { font-size: 18px; margin: 0; font-weight: 600; }
  h2 { font-size: 13px; margin: 0 0 8px 0; color: var(--muted);
       text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600; }
  a { color: var(--accent); text-decoration: none; }
  code, .mono { font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }

  header {
    display: flex; align-items: center; gap: 12px;
    margin-bottom: 16px;
  }
  header .title { display: flex; align-items: center; gap: 10px; }
  header .cat { font-size: 22px; }
  header .pair { color: var(--muted); font-weight: 500; }
  header .spacer { flex: 1; }
  .pill {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 12px; font-weight: 500;
    border: 1px solid var(--border); background: var(--panel);
  }
  .pill .dot { width: 8px; height: 8px; border-radius: 50%; background: var(--muted); }
  .pill.alive .dot { background: var(--bid); box-shadow: 0 0 8px var(--bid); }
  .pill.dead .dot { background: var(--ask); }
  .pill.warn .dot { background: var(--warn); }

  .grid {
    display: grid; gap: 12px;
    grid-template-columns: repeat(12, 1fr);
  }
  .panel {
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 8px; padding: 14px 16px;
  }
  .col-3 { grid-column: span 3; }
  .col-4 { grid-column: span 4; }
  .col-6 { grid-column: span 6; }
  .col-8 { grid-column: span 8; }
  .col-12 { grid-column: span 12; }
  @media (max-width: 900px) {
    .col-3, .col-4, .col-6, .col-8 { grid-column: span 12; }
  }

  .stat .v { font-size: 20px; font-weight: 600; font-family: ui-monospace, monospace; }
  .stat .l { font-size: 11px; color: var(--muted); text-transform: uppercase;
             letter-spacing: 0.06em; margin-top: 2px; }

  .kv { display: grid; grid-template-columns: max-content 1fr; gap: 4px 12px;
        font-size: 13px; }
  .kv .k { color: var(--muted); }
  .kv .v { font-family: ui-monospace, monospace; word-break: break-all; }

  /* Balance bars */
  .bal { margin-top: 6px; }
  .bal .row { display: flex; align-items: baseline; justify-content: space-between;
              font-family: ui-monospace, monospace; font-size: 13px; }
  .bal .lab { color: var(--muted); font-size: 11px; text-transform: uppercase;
              letter-spacing: 0.06em; }
  .bar {
    margin-top: 6px; height: 8px; border-radius: 4px;
    background: var(--panel-2); overflow: hidden; position: relative;
  }
  .bar > .fill {
    height: 100%; background: linear-gradient(90deg, var(--accent), #79c0ff);
    transition: width 400ms ease;
  }
  .bar > .target {
    position: absolute; top: -2px; bottom: -2px;
    width: 2px; background: var(--ours); opacity: 0.8;
  }
  .bal .meta { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .bal + .bal { margin-top: 14px; }

  /* Orderbook */
  .ob { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 700px) { .ob { grid-template-columns: 1fr; } }
  .ob h3 { margin: 0 0 6px 0; font-size: 12px; font-weight: 600;
           text-transform: uppercase; letter-spacing: 0.06em; }
  .ob .asks h3 { color: var(--ask); }
  .ob .bids h3 { color: var(--bid); }
  .ob table { width: 100%; border-collapse: collapse; font-family: ui-monospace, monospace;
              font-size: 12px; }
  .ob th { font-weight: 500; color: var(--muted); text-align: right;
           padding: 4px 6px; font-size: 11px; text-transform: uppercase;
           letter-spacing: 0.04em; border-bottom: 1px solid var(--border); }
  .ob th:first-child { text-align: left; }
  .ob td { padding: 4px 6px; text-align: right; position: relative; z-index: 1; }
  .ob td:first-child { text-align: left; }
  .ob tr { position: relative; }
  /* "Ours" = left accent + paw; keep row fill light so depth bars stay readable. */
  .ob tr.mine {
    background: linear-gradient(
      90deg,
      rgba(255, 140, 66, 0.22) 0%,
      rgba(255, 140, 66, 0.06) 28%,
      transparent 72%
    );
    box-shadow: inset 3px 0 0 var(--ours);
  }
  .ob tr.mine td:first-child::before {
    content: "🐾"; margin-right: 6px;
  }
  .ob tr .depth {
    position: absolute; right: 0; top: 0; bottom: 0;
    z-index: 0; opacity: 0.42;
    transition: width 400ms ease;
  }
  .ob .asks tr .depth { background: var(--ask-soft); }
  .ob .bids tr .depth { background: var(--bid-soft); }
  /* Same red/green depth as everyone else so bar width = fair comparison. */
  .ob tr.mine .depth { opacity: 0.5; }
  .ob .empty { padding: 12px; color: var(--muted); font-size: 12px; text-align: center; }
  .ob .spread {
    text-align: center; color: var(--muted); font-size: 11px;
    grid-column: 1 / -1; padding: 4px 0; border-top: 1px dashed var(--border);
    border-bottom: 1px dashed var(--border); margin: 4px 0;
  }

  /* Recent swaps */
  .swap { display: grid; grid-template-columns: max-content 1fr max-content;
          gap: 10px; align-items: center; padding: 8px 0;
          border-bottom: 1px solid var(--border); font-size: 13px; }
  .swap:last-child { border-bottom: none; }
  .swap .when { color: var(--muted); font-family: ui-monospace, monospace;
                font-size: 11px; }
  .swap .desc { font-family: ui-monospace, monospace; font-size: 12px; }
  .swap .arrow { color: var(--muted); margin: 0 4px; }
  .swap .state {
    font-size: 11px; padding: 2px 8px; border-radius: 999px;
    background: var(--panel-2); border: 1px solid var(--border);
    white-space: nowrap;
  }
  .swap .state.finished { color: var(--bid); border-color: var(--bid); }
  .swap .state.flight { color: var(--warn); border-color: var(--warn);
                        animation: pulse 1.6s ease-in-out infinite; }
  .swap .state.failed { color: var(--ask); border-color: var(--ask); }
  @keyframes pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.55; }
  }
  .swap.new { animation: flash 1.2s ease-out; }
  @keyframes flash {
    0% { background: var(--ours-soft); }
    100% { background: transparent; }
  }

  /* Log */
  .log {
    background: var(--panel-2); border: 1px solid var(--border);
    border-radius: 6px; padding: 8px 10px;
    font-family: ui-monospace, monospace; font-size: 11.5px; line-height: 1.45;
    max-height: 320px; overflow: auto; white-space: pre-wrap;
  }
  .log .line.posted { color: var(--bid); }
  .log .line.warn { color: var(--warn); }
  .log .line.err { color: var(--ask); }
  .log .line.cancel { color: var(--accent); }

  footer { color: var(--muted); font-size: 11px; text-align: center;
           margin-top: 18px; padding-top: 12px; border-top: 1px solid var(--border); }
</style>
</head>
<body>

<header>
  <div class="title">
    <div class="cat">🐱</div>
    <div>
      <h1>meowmaker</h1>
      <div class="pair" id="pairLabel">MEWC/LTC</div>
    </div>
  </div>
  <div class="spacer"></div>
  <div id="botPill" class="pill"><span class="dot"></span><span id="botStatus">…</span></div>
  <div id="updPill" class="pill"><span class="dot"></span><span id="upd">connecting…</span></div>
</header>

<div class="grid">

  <div class="panel col-3 stat"><div id="kSpread" class="v">–</div><div class="l">spread (half)</div></div>
  <div class="panel col-3 stat"><div id="kSize" class="v">–</div><div class="l">USD per side</div></div>
  <div class="panel col-3 stat"><div id="kRefresh" class="v">–</div><div class="l">refresh (s)</div></div>
  <div class="panel col-3 stat"><div id="kDrift" class="v">–</div><div class="l">drift vs pool</div></div>

  <div class="panel col-6">
    <h2>NonKYC reference prices</h2>
    <div class="kv">
      <div class="k">MEWC/USDT</div><div class="v" id="pMewc">–</div>
      <div class="k">LTC/USDT</div><div class="v" id="pLtc">–</div>
      <div class="k">derived MEWC/LTC mid</div><div class="v" id="pMid">–</div>
      <div class="k">MEWC/LTC pool last</div><div class="v" id="pPool">–</div>
    </div>
  </div>

  <div class="panel col-6">
    <h2>Wallet balances vs target</h2>
    <div id="balances"><div style="color:var(--muted);">loading…</div></div>
  </div>

  <div class="panel col-12">
    <h2>Public orderbook · MEWC/LTC · 🐾 = ours · shaded bar = size vs largest on that side</h2>
    <div class="ob">
      <div class="asks">
        <h3>Asks · sell MEWC for LTC</h3>
        <table>
          <thead><tr>
            <th>price (LTC/MEWC)</th><th>size (MEWC)</th><th>$ size</th>
          </tr></thead>
          <tbody id="obAsks"></tbody>
        </table>
      </div>
      <div class="bids">
        <h3>Bids · buy MEWC with LTC</h3>
        <table>
          <thead><tr>
            <th>price (LTC/MEWC)</th><th>size (LTC)</th><th>$ size</th>
          </tr></thead>
          <tbody id="obBids"></tbody>
        </table>
      </div>
      <div class="spread" id="obSpread">–</div>
    </div>
  </div>

  <div class="panel col-6">
    <h2>Recent maker swaps</h2>
    <div id="swaps"><div style="color:var(--muted);">loading…</div></div>
  </div>

  <div class="panel col-6">
    <h2>Bot log (tail)</h2>
    <div class="log" id="log"></div>
  </div>

</div>

<footer>
  read-only · auto-refresh every 3s · bound to 127.0.0.1 ·
  <a href="/api/snapshot">/api/snapshot</a>
</footer>

<script>
  const $ = (id) => document.getElementById(id);
  const fmt = (n, d=8) => {
    if (n === null || n === undefined || n === "" || isNaN(+n)) return "–";
    const x = +n;
    if (x === 0) return "0";
    if (Math.abs(x) >= 1000) return x.toLocaleString(undefined, {maximumFractionDigits: 2});
    if (Math.abs(x) >= 1) return x.toFixed(4);
    return x.toExponential(4);
  };
  const fmtAmt = (n) => {
    if (n === null || n === undefined || n === "" || isNaN(+n)) return "–";
    const x = +n;
    if (Math.abs(x) >= 1000) return x.toLocaleString(undefined, {maximumFractionDigits: 2});
    if (Math.abs(x) >= 1) return x.toFixed(4);
    return x.toFixed(8);
  };
  const fmtUsd = (n) => {
    if (n === null || n === undefined || isNaN(+n)) return "–";
    const x = +n;
    if (Math.abs(x) >= 100) return "$" + x.toLocaleString(undefined, {maximumFractionDigits: 0});
    return "$" + x.toFixed(2);
  };
  const ago = (ts) => {
    if (!ts) return "";
    const dt = (Date.now()/1000) - ts;
    if (dt < 60) return Math.floor(dt) + "s ago";
    if (dt < 3600) return Math.floor(dt/60) + "m ago";
    if (dt < 86400) return Math.floor(dt/3600) + "h ago";
    return Math.floor(dt/86400) + "d ago";
  };

  let lastSwapUuids = new Set();

  function classifyLogLine(s) {
    if (/\bERROR\b|\bERR\b|failed|exception/i.test(s)) return "err";
    if (/\bWARN\b/.test(s)) return "warn";
    if (/POSTED/.test(s)) return "posted";
    if (/cancelled|cancel/i.test(s)) return "cancel";
    return "";
  }

  function classifySwapState(ev, finished) {
    if (finished) return "finished";
    if (/Failed|Aborted|Refund/i.test(ev || "")) return "failed";
    return "flight";
  }

  function render(s) {
    $("upd").textContent = "updated " + new Date(s.now*1000).toLocaleTimeString();
    $("updPill").classList.add("alive");

    const botAlive = !!s.bot_pid;
    $("botPill").className = "pill " + (botAlive ? "alive" : "dead");
    $("botStatus").textContent = botAlive ? ("bot pid " + s.bot_pid) : "bot down";

    $("pairLabel").textContent = s.config.pair || "MEWC/LTC";
    const sp = s.config.spread;
    $("kSpread").textContent = (sp != null) ? (+(sp*100).toFixed(3) + "%") : "–";
    const tm = s.config.usd_target_mewc;
    const tl = s.config.usd_target_ltc;
    if (tm != null && tl != null && +tm !== +tl) {
      $("kSize").textContent = "$" + tm + " MEWC / $" + tl + " LTC";
    } else if (tm != null) {
      $("kSize").textContent = "$" + tm + " / side";
    } else {
      $("kSize").textContent = "–";
    }
    $("kRefresh").textContent = (s.config.refresh_seconds != null) ? s.config.refresh_seconds : "–";

    const o = s.oracle || {};
    const mewc = +o.mewc_usd || 0;
    const ltc = +o.ltc_usd || 0;
    const mid = (mewc && ltc) ? (mewc/ltc) : 0;
    const pool = +o.pool_last || 0;
    $("pMewc").textContent = o.mewc_usd ? fmt(o.mewc_usd) : "–";
    $("pLtc").textContent = o.ltc_usd ? fmt(o.ltc_usd) : "–";
    $("pMid").textContent = mid ? mid.toExponential(4) + " LTC/MEWC" : "–";
    $("pPool").textContent = o.pool_last ? (+o.pool_last).toExponential(4) + " LTC/MEWC" : "–";

    let drift = null;
    if (mid && pool) drift = Math.abs(mid - pool) / pool;
    const driftEl = $("kDrift");
    if (drift === null) {
      driftEl.textContent = "n/a";
    } else {
      driftEl.textContent = (drift*100).toFixed(1) + "%";
    }

    // Balances vs target
    const bals = s.balances || {};
    const balsHtml = ["MEWC", "LTC"].map(coin => {
      const b = bals[coin] || {};
      const sp = +b.spendable || 0;
      let target = 0, usdVal = 0;
      const usdT = coin === "MEWC"
        ? (+s.config.usd_target_mewc || +s.config.usd_per_side || 0)
        : (+s.config.usd_target_ltc || +s.config.usd_per_side || 0);
      if (coin === "MEWC" && mewc) {
        target = usdT / mewc;
        usdVal = sp * mewc;
      } else if (coin === "LTC" && ltc) {
        target = usdT / ltc;
        usdVal = sp * ltc;
      }
      const pct = target ? Math.min(200, (sp/target)*100) : 0;
      const fillW = target ? Math.min(100, (sp/target)*100) : 0;
      const targetMark = target ? Math.min(100, 100 * (target / Math.max(sp, target))) : 100;
      // For display we always anchor target at 100% of bar; spendable can overshoot.
      const overshoot = pct > 100;
      const fillClamped = Math.min(100, pct);
      return `
        <div class="bal">
          <div class="row">
            <span class="lab">${coin}</span>
            <span>${fmtAmt(sp)} ${coin} · ${fmtUsd(usdVal)}</span>
          </div>
          <div class="bar">
            <div class="fill" style="width:${fillClamped}%; ${overshoot ? 'background: linear-gradient(90deg, var(--bid), #56d364);' : ''}"></div>
          </div>
          <div class="meta">
            target ${target ? fmtAmt(target) : "–"} ${coin} (${fmtUsd(usdT)})
            · ${target ? Math.round(pct) + "% of target" : "no oracle"}
            ${b.address ? "· " + b.address : ""}
          </div>
        </div>
      `;
    }).join("");
    $("balances").innerHTML = balsHtml;

    // Orderbook
    const ob = s.orderbook || {asks: [], bids: []};
    // KDF returns asks sorted lowest-first, bids sorted highest-first.
    // For visual display, asks should go highest at top → lowest at bottom (closest to mid).
    // Bids go highest at top → lowest at bottom.
    const asks = (ob.asks || []).slice().sort((a, b) => +b.price - +a.price);
    const bids = (ob.bids || []).slice().sort((a, b) => +b.price - +a.price);

    const maxAskVol = Math.max(0, ...asks.map(e => +e.maxvolume || 0));
    const maxBidVol = Math.max(0, ...bids.map(e => +e.maxvolume || 0));

    const renderRow = (e, side) => {
      const vol = +e.maxvolume || 0;
      const px = +e.price || 0;
      const max = (side === "ask") ? maxAskVol : maxBidVol;
      const w = max > 0 ? (vol / max) * 100 : 0;
      // $ size: asks are MEWC volume; bids are LTC volume.
      let usd = 0;
      if (side === "ask") usd = vol * mewc;
      else usd = vol * ltc;
      const cls = e.is_mine ? "mine" : "";
      return `
        <tr class="${cls}">
          <td>${px.toExponential(4)}</td>
          <td>${fmtAmt(vol)}</td>
          <td>${fmtUsd(usd)}</td>
          <td class="depth" style="width:${w}%;"></td>
        </tr>
      `;
    };

    $("obAsks").innerHTML = asks.length
      ? asks.map(e => renderRow(e, "ask")).join("")
      : `<tr><td class="empty" colspan="4">no asks</td></tr>`;
    $("obBids").innerHTML = bids.length
      ? bids.map(e => renderRow(e, "bid")).join("")
      : `<tr><td class="empty" colspan="4">no bids</td></tr>`;

    // Spread between best ask and best bid
    const bestAsk = asks.length ? Math.min(...asks.map(e => +e.price)) : null;
    const bestBid = bids.length ? Math.max(...bids.map(e => +e.price)) : null;
    if (bestAsk && bestBid) {
      const sprPct = ((bestAsk - bestBid) / ((bestAsk + bestBid)/2)) * 100;
      $("obSpread").textContent =
        `book mid ${((bestAsk+bestBid)/2).toExponential(4)} LTC/MEWC · book spread ${sprPct.toFixed(2)}%`;
    } else {
      $("obSpread").textContent = "(one-sided book)";
    }

    // Recent swaps
    const swaps = s.recent_swaps || [];
    const newUuids = new Set(swaps.map(x => x.uuid));
    const swapsHtml = swaps.map(sw => {
      const isNew = !lastSwapUuids.has(sw.uuid) && lastSwapUuids.size > 0;
      const cls = classifySwapState(sw.last_event, sw.is_finished);
      const ts = sw.started_at ? new Date(sw.started_at*1000).toLocaleTimeString() : "";
      const desc = sw.my_coin && sw.other_coin
        ? `<span>${fmtAmt(sw.my_amount)} ${sw.my_coin}</span>` +
          `<span class="arrow">→</span>` +
          `<span>${fmtAmt(sw.other_amount)} ${sw.other_coin}</span>`
        : (sw.uuid || "?");
      return `
        <div class="swap ${isNew ? 'new' : ''}">
          <div class="when">${ts}<br><span style="opacity:0.7;">${ago(sw.started_at)}</span></div>
          <div class="desc">${desc}</div>
          <div class="state ${cls}">${sw.last_event || sw.type || "?"}</div>
        </div>
      `;
    }).join("");
    $("swaps").innerHTML = swapsHtml || `<div style="color:var(--muted);">no swaps yet</div>`;
    lastSwapUuids = newUuids;

    // Log
    const lines = s.log || [];
    const logHtml = lines.map(l => {
      const cls = classifyLogLine(l);
      return `<div class="line ${cls}">${l.replace(/&/g,"&amp;").replace(/</g,"&lt;")}</div>`;
    }).join("");
    const logEl = $("log");
    const wasAtBottom = logEl.scrollTop + logEl.clientHeight >= logEl.scrollHeight - 4;
    logEl.innerHTML = logHtml;
    if (wasAtBottom) logEl.scrollTop = logEl.scrollHeight;
  }

  async function poll() {
    try {
      const r = await fetch("/api/snapshot", { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const s = await r.json();
      render(s);
    } catch (e) {
      $("upd").textContent = "fetch failed: " + e.message;
      $("updPill").className = "pill warn";
    }
  }

  poll();
  setInterval(poll, 3000);
</script>
</body>
</html>
"""


class _Handler(BaseHTTPRequestHandler):
    cfg: dict = {}  # injected by run()

    def log_message(self, fmt: str, *args: Any) -> None:  # quieter access logs
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/" or path == "/index.html":
            self._send(200, INDEX_HTML.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/api/snapshot":
            try:
                snap = _gather_snapshot(self.cfg)
                body = json.dumps(snap, default=str).encode("utf-8")
                self._send(200, body, "application/json")
            except Exception as e:  # noqa: BLE001
                LOG.exception("snapshot failed")
                err = json.dumps({"error": str(e)}).encode("utf-8")
                self._send(500, err, "application/json")
            return
        if path == "/healthz":
            self._send(200, b'{"ok":true}', "application/json")
            return
        self._send(404, b"not found", "text/plain")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(
        fmt="%(asctime)s %(levelname)-5s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def _load_config() -> dict:
    if not CONFIG_PATH.exists():
        sys.exit(f"ERROR: {CONFIG_PATH} not found. Copy bot/config.example.toml first.")
    with CONFIG_PATH.open("rb") as f:
        return tomllib.load(f)


def main() -> None:
    _setup_logging()
    cfg = _load_config()
    host = cfg.get("dashboard_host", "127.0.0.1")
    port = int(cfg.get("dashboard_port", 7784))

    if host not in ("127.0.0.1", "localhost", "::1"):
        LOG.warning(
            "dashboard_host=%s is non-loopback; this exposes balances + orders + bot log "
            "to the network. The dashboard never proxies write-RPCs, but consider an "
            "SSH tunnel instead.", host,
        )

    _Handler.cfg = cfg
    server = ThreadingHTTPServer((host, port), _Handler)

    def _stop(signame: str) -> None:
        LOG.info("got %s; shutting down dashboard", signame)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda s, _f, n=sig.name: _stop(n))

    LOG.info("dashboard listening on http://%s:%d/", host, port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        LOG.info("dashboard stopped")


if __name__ == "__main__":
    main()
