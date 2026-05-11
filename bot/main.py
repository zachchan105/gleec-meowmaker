"""Gleec MEWC/LTC passive market maker.

One process, one seed. Every `refresh_seconds`:

  1. Pull MEWC/USDT and LTC/USDT spot mids from NonKYC.
  2. Compute mid (LTC per MEWC) and (optionally) sanity-check vs the NonKYC
     MEWC/LTC pool last price.
  3. Read MEWC + LTC spendable balances from KDF.
  4. Compute volumes from USD sizing targets (see `_usd_targets`), capped
     by available balance.
  5. cancel_all_orders for the pair (both directions).
  6. setprice twice:
       - sell MEWC for LTC at mid * (1 + spread)
       - sell LTC for MEWC at 1 / (mid * (1 - spread))   <- the buy-MEWC side

Transient errors (NonKYC down, KDF flaky) skip the cycle and try again
next tick. We do NOT cancel-on-error, to avoid yanking liquidity for a
30-second outage.

SIGTERM / SIGINT triggers a final cancel_all_orders before exit.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from decimal import Decimal, getcontext
from pathlib import Path

try:
    import tomllib  # py >= 3.11
except ModuleNotFoundError:
    import tomli as tomllib  # type: ignore[no-redef]

from .kdf_client import KdfClient, KdfRpcError
from .price_oracle import OracleError, drift_vs_pool, fetch_quote


# Plenty of precision for tiny MEWC prices (e.g. 0.00000093 LTC/MEWC).
getcontext().prec = 40


LOG = logging.getLogger("meowmaker")

BASE = "MEWC"
REL = "LTC"


def _load_config() -> dict:
    cfg_path = Path(__file__).with_name("config.toml")
    if not cfg_path.exists():
        sys.exit(
            f"ERROR: {cfg_path} not found. "
            f"Copy bot/config.example.toml to bot/config.toml and edit."
        )
    with cfg_path.open("rb") as f:
        return tomllib.load(f)


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter(
            fmt="%(asctime)s %(levelname)-5s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def _usd_targets(cfg: dict) -> tuple[Decimal, Decimal]:
    """Return (USD target for sell-MEWC side, USD target for sell-LTC side).

    `usd_per_side` is the symmetric default. Optional `usd_per_side_mewc` /
    `usd_per_side_ltc` override each leg when you hold uneven inventory.
    """
    base = Decimal(str(cfg["usd_per_side"]))
    usd_mewc = Decimal(str(cfg["usd_per_side_mewc"])) if "usd_per_side_mewc" in cfg else base
    usd_ltc = Decimal(str(cfg["usd_per_side_ltc"])) if "usd_per_side_ltc" in cfg else base
    return usd_mewc, usd_ltc


def _compute_orders(
    mewc_balance: Decimal,
    ltc_balance: Decimal,
    mewc_usd: Decimal,
    ltc_usd: Decimal,
    mid: Decimal,
    spread: Decimal,
    usd_sell_mewc: Decimal,
    usd_sell_ltc: Decimal,
) -> tuple[
    tuple[Decimal, Decimal, bool],
    tuple[Decimal, Decimal, bool],
]:
    """Return ((sell_price, sell_vol_mewc, sell_max),
                (buy_price,  buy_vol_ltc,   buy_max)).

    sell side: setprice base=MEWC rel=LTC price=sell_price volume=sell_vol_mewc
    buy  side: setprice base=LTC  rel=MEWC price=buy_price  volume=buy_vol_ltc

    Both `setprice` calls use the perspective of "I am SELLING `volume` of
    `base` for `rel` at this `price` (rel per base)". So the buy-MEWC side
    is expressed as "sell LTC for MEWC at MEWC-per-LTC".

    `usd_sell_mewc` / `usd_sell_ltc` are the USD notionals to post on each
    leg (sell MEWC, sell LTC), typically from `_usd_targets(cfg)`.

    `*_max` is True when the desired USD volume meets or exceeds the
    "everything spendable minus tx fees" instead of us having to budget
    for fees ourselves (which is what triggered the
    "Not enough <coin> for swap" errors in the first run).
    """
    sell_price = mid * (Decimal(1) + spread)             # LTC per MEWC, premium
    buy_mewc_per_ltc_mid = Decimal(1) / mid              # MEWC per LTC, mid
    buy_price = buy_mewc_per_ltc_mid * (Decimal(1) + spread)  # ask more MEWC per LTC

    desired_sell_vol_mewc = usd_sell_mewc / mewc_usd
    desired_buy_vol_ltc = usd_sell_ltc / ltc_usd

    sell_max = desired_sell_vol_mewc >= mewc_balance
    buy_max = desired_buy_vol_ltc >= ltc_balance

    sell_vol_mewc = mewc_balance if sell_max else desired_sell_vol_mewc
    buy_vol_ltc = ltc_balance if buy_max else desired_buy_vol_ltc

    return (sell_price, sell_vol_mewc, sell_max), (buy_price, buy_vol_ltc, buy_max)


async def _run_cycle(client: KdfClient, cfg: dict) -> None:
    spread = Decimal(str(cfg["spread"]))
    usd_mewc, usd_ltc = _usd_targets(cfg)
    max_drift = Decimal(str(cfg.get("max_drift_vs_pool", 0)))
    min_mewc = Decimal(str(cfg["min_post_volume_mewc"]))
    min_ltc = Decimal(str(cfg["min_post_volume_ltc"]))

    try:
        quote = await fetch_quote()
    except (OracleError, Exception) as e:  # noqa: BLE001
        LOG.warning("price fetch failed (%s); skipping cycle, leaving orders in place", e)
        return

    drift = drift_vs_pool(quote)
    drift_str = f"{drift:.4f}" if drift is not None else "n/a"
    LOG.info(
        "oracle: MEWC/USDT=%s  LTC/USDT=%s  mid=%s LTC/MEWC  pool=%s  drift=%s",
        quote.mewc_usd, quote.ltc_usd, quote.mid_ltc_per_mewc, quote.pool_mid, drift_str,
    )

    if max_drift > 0 and drift is not None and drift > max_drift:
        LOG.warning(
            "drift %s exceeds max_drift_vs_pool %s; skipping cycle", drift, max_drift,
        )
        return

    try:
        mewc_bal = await client.my_balance(BASE)
        ltc_bal = await client.my_balance(REL)
    except KdfRpcError as e:
        LOG.error("KDF balance error (%s); skipping cycle", e)
        return

    LOG.info(
        "balances: %s=%s (%s)  %s=%s (%s)",
        BASE, mewc_bal.spendable, mewc_bal.address,
        REL, ltc_bal.spendable, ltc_bal.address,
    )

    (sell_price, sell_vol, sell_max), (buy_price, buy_vol, buy_max) = _compute_orders(
        mewc_balance=mewc_bal.spendable,
        ltc_balance=ltc_bal.spendable,
        mewc_usd=quote.mewc_usd,
        ltc_usd=quote.ltc_usd,
        mid=quote.mid_ltc_per_mewc,
        spread=spread,
        usd_sell_mewc=usd_mewc,
        usd_sell_ltc=usd_ltc,
    )

    # cancel everything we have on this pair so each cycle is a clean replace.
    # (setprice's `cancel_previous=true` only handles same-direction orders.)
    try:
        cancel_resp = await client.cancel_all_for_pair(BASE, REL)
    except KdfRpcError as e:
        LOG.error("cancel_all_orders failed (%s); skipping cycle", e)
        return
    if cancel_resp["cancelled"]:
        LOG.info("cancelled %d existing order(s)", len(cancel_resp["cancelled"]))
    if cancel_resp["currently_matching"]:
        LOG.warning(
            "leaving %d order(s) untouched (currently_matching, in-flight swap): %s",
            len(cancel_resp["currently_matching"]), cancel_resp["currently_matching"],
        )

    # Sell MEWC for LTC.
    if sell_vol < min_mewc:
        LOG.warning(
            "sell volume %s MEWC below min_post_volume_mewc %s; skipping sell side",
            sell_vol, min_mewc,
        )
    else:
        try:
            r = await client.setprice(BASE, REL, sell_price, sell_vol, max=sell_max)
            uuid = (r.get("result") or {}).get("uuid", "?")
            LOG.info(
                "POSTED sell  %s -> %s  price=%s  vol=%s%s  uuid=%s",
                BASE, REL, sell_price, sell_vol,
                " (max=true)" if sell_max else "", uuid,
            )
        except KdfRpcError as e:
            LOG.error("setprice (sell %s) failed: %s", BASE, e)

    # Buy MEWC with LTC, expressed as "sell LTC for MEWC".
    if buy_vol < min_ltc:
        LOG.warning(
            "buy volume %s LTC below min_post_volume_ltc %s; skipping buy side",
            buy_vol, min_ltc,
        )
    else:
        try:
            r = await client.setprice(REL, BASE, buy_price, buy_vol, max=buy_max)
            uuid = (r.get("result") or {}).get("uuid", "?")
            LOG.info(
                "POSTED buy   %s -> %s  price=%s  vol=%s%s  uuid=%s",
                REL, BASE, buy_price, buy_vol,
                " (max=true)" if buy_max else "", uuid,
            )
        except KdfRpcError as e:
            LOG.error("setprice (sell %s) failed: %s", REL, e)


async def _main_async() -> int:
    _setup_logging()
    cfg = _load_config()

    refresh = float(cfg.get("refresh_seconds", 90))
    client = KdfClient(url=cfg["kdf_url"], userpass=cfg["rpc_password"])

    stop_event = asyncio.Event()

    def _handle_signal(signame: str) -> None:
        LOG.info("got %s; will cancel orders and exit after current cycle", signame)
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, _handle_signal, sig.name)

    u_m, u_l = _usd_targets(cfg)
    LOG.info(
        "started: pair=%s/%s  spread=%s  sizing=$%s MEWC / $%s LTC  refresh=%ss",
        BASE, REL, cfg["spread"], u_m, u_l, refresh,
    )

    try:
        while not stop_event.is_set():
            try:
                await _run_cycle(client, cfg)
            except Exception:
                # Catch-all so a single bad cycle never kills the loop.
                LOG.exception("unexpected error in cycle")

            try:
                await asyncio.wait_for(stop_event.wait(), timeout=refresh)
            except asyncio.TimeoutError:
                pass
    finally:
        LOG.info("shutdown: cancelling all %s/%s orders", BASE, REL)
        try:
            r = await client.cancel_all_for_pair(BASE, REL)
            LOG.info(
                "shutdown: cancelled=%s currently_matching=%s",
                r["cancelled"], r["currently_matching"],
            )
        except Exception as e:  # noqa: BLE001
            LOG.error("shutdown: cancel failed: %s (orders may be left open!)", e)
        await client.aclose()

    return 0


def main() -> None:
    try:
        sys.exit(asyncio.run(_main_async()))
    except KeyboardInterrupt:
        # Should be handled by signal handler, but just in case.
        sys.exit(130)


if __name__ == "__main__":
    main()
