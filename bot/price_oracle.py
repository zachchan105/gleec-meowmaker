"""NonKYC price oracle for the MEWC/LTC pair.

We derive the fair MEWC/LTC ratio from MEWC/USDT and LTC/USDT spot tickers
(both are active orderbooks on NonKYC). The dedicated MEWC/LTC market on
NonKYC is an AMM pool and frequently has 0 bids / 0 asks, so it's used only
as a sanity check to detect oracle drift.

All NonKYC tickers are fetched in parallel.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Awaitable, Callable

import httpx

NONKYC = "https://api.nonkyc.io/api/v2/market/getbysymbol"


class OracleError(RuntimeError):
    """Raised when we cannot produce a usable mid price."""


@dataclass(frozen=True)
class Quote:
    """Snapshot from the oracle.

    `mid_ltc_per_mewc` = how many LTC one MEWC is worth, derived from USDT pairs.
    `pool_mid` is the NonKYC AMM pool last price (also LTC per MEWC) or None.
    """

    mewc_usd: Decimal
    ltc_usd: Decimal
    mid_ltc_per_mewc: Decimal
    pool_mid: Decimal | None


async def _fetch_last(client: httpx.AsyncClient, symbol: str) -> Decimal | None:
    """Returns lastPriceNumber for `symbol`, or None if NonKYC has no fresh data."""
    r = await client.get(f"{NONKYC}/{symbol}")
    r.raise_for_status()
    data = r.json()
    last = data.get("lastPriceNumber")
    if last is None or last == 0:
        return None
    return Decimal(str(last))


async def fetch_quote(timeout: float = 10.0) -> Quote:
    async with httpx.AsyncClient(timeout=timeout) as client:
        # MEWC/LTC pool may legitimately be unavailable (isActive:false), so we
        # tolerate that one failing while requiring the two USDT pairs.
        import asyncio
        mewc_usd_t = asyncio.create_task(_fetch_last(client, "MEWC_USDT"))
        ltc_usd_t = asyncio.create_task(_fetch_last(client, "LTC_USDT"))
        pool_t = asyncio.create_task(_fetch_last(client, "MEWC_LTC"))

        mewc_usd = await mewc_usd_t
        ltc_usd = await ltc_usd_t
        try:
            pool_mid = await pool_t
        except Exception:
            pool_mid = None

    if mewc_usd is None:
        raise OracleError("MEWC/USDT lastPrice unavailable from NonKYC")
    if ltc_usd is None:
        raise OracleError("LTC/USDT lastPrice unavailable from NonKYC")

    mid = mewc_usd / ltc_usd
    return Quote(mewc_usd=mewc_usd, ltc_usd=ltc_usd, mid_ltc_per_mewc=mid, pool_mid=pool_mid)


def drift_vs_pool(quote: Quote) -> Decimal | None:
    """|derived - pool| / pool, or None if pool unavailable."""
    if quote.pool_mid is None or quote.pool_mid == 0:
        return None
    return abs(quote.mid_ltc_per_mewc - quote.pool_mid) / quote.pool_mid


# Tiny CLI for adhoc debugging: `python -m bot.price_oracle`
if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        q = await fetch_quote()
        d = drift_vs_pool(q)
        print(f"MEWC/USDT = {q.mewc_usd}")
        print(f"LTC/USDT  = {q.ltc_usd}")
        print(f"derived MEWC/LTC = {q.mid_ltc_per_mewc}")
        print(f"pool MEWC/LTC    = {q.pool_mid}")
        print(f"drift            = {d}")

    asyncio.run(_main())
