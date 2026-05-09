"""Thin async wrapper around KDF's HTTP RPC (mix of legacy and v2 / mmrpc 2.0).

Only the calls the maker bot needs:
  - account_balance  (v2 / mmrpc 2.0) - HD-aware, sums addresses in account 0
  - my_orders        (legacy)         - order-mgmt, works in HD mode
  - setprice         (legacy)         - works in HD mode (coin-level, not address)
  - cancel_all_orders(legacy)

Every call requires `userpass`; we inject it once at construction time.

Why account_balance instead of my_balance?
  With `enable_hd: true`, legacy `my_balance` errors with
  `'my_address' is deprecated for HD wallets`. The v2 `account_balance`
  RPC returns balances per address in the HD account; we sum the
  spendable amounts across the External chain (which is where KDF
  spends from for swaps).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx


class KdfRpcError(RuntimeError):
    """KDF returned a non-success body (legacy methods don't always use HTTP codes)."""


@dataclass(frozen=True)
class Balance:
    coin: str
    address: str           # primary HD address (m/44'/COIN'/0'/0/0)
    spendable: Decimal     # sum across all known External-chain addresses
    unspendable: Decimal


@dataclass(frozen=True)
class MakerOrder:
    uuid: str
    base: str
    rel: str
    price: Decimal
    available_amount: Decimal


class KdfClient:
    def __init__(self, url: str, userpass: str, timeout: float = 30.0):
        self._url = url.rstrip("/")
        self._userpass = userpass
        # One client reused across calls => keepalive connection to KDF.
        self._http = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _post(self, body: dict[str, Any]) -> dict[str, Any]:
        body = {"userpass": self._userpass, **body}
        r = await self._http.post(self._url, json=body)
        try:
            data = r.json()
        except ValueError as e:
            raise KdfRpcError(f"non-JSON response (HTTP {r.status_code}): {r.text[:200]}") from e
        # Legacy errors: top-level `{"error": "..."}` with no `result`.
        if isinstance(data, dict) and "error" in data and "result" not in data:
            raise KdfRpcError(f"{body['method']}: {data['error']}")
        # mmrpc 2.0 errors: `{"mmrpc":"2.0","error":"...","error_path":...}` (also no `result`).
        if isinstance(data, dict) and data.get("mmrpc") == "2.0" and "error" in data:
            raise KdfRpcError(f"{body['method']}: {data.get('error')}")
        return data

    async def my_balance(self, coin: str) -> Balance:
        """HD-aware balance lookup via mmrpc 2.0 `account_balance`.

        Returns the sum of spendable balances across the External chain of
        account 0. The single-address path m/44'/COIN'/0'/0/0 is reported
        as `address`; anything KDF gap-scanned past it is summed in too.
        """
        d = await self._post({
            "method": "account_balance",
            "mmrpc": "2.0",
            "params": {
                "coin": coin,
                "account_index": 0,
                "chain": "External",
                # 50 covers default gap-limit (20) plus headroom; a single bot
                # never expects more than the first address to be funded, but
                # we still sum in case the user has multiple receive addrs.
                "limit": 50,
            },
        })
        result = d["result"]
        addresses = result.get("addresses") or []
        if not addresses:
            return Balance(coin=coin, address="", spendable=Decimal(0), unspendable=Decimal(0))

        spendable = Decimal(0)
        unspendable = Decimal(0)
        for a in addresses:
            bal = (a.get("balance") or {}).get(coin) or {}
            spendable += Decimal(str(bal.get("spendable", "0")))
            unspendable += Decimal(str(bal.get("unspendable", "0")))

        # Primary address is the first entry returned, which corresponds to
        # m/44'/COIN'/0'/0/0 -- the path KDF prefers for swap addresses.
        primary = addresses[0]["address"]
        return Balance(coin=coin, address=primary, spendable=spendable, unspendable=unspendable)

    async def my_orders_for_pair(self, base: str, rel: str) -> list[MakerOrder]:
        """Return our maker orders that match (base, rel) in either direction."""
        d = await self._post({"method": "my_orders"})
        result = d.get("result", {})
        out: list[MakerOrder] = []
        for uuid, order in (result.get("maker_orders") or {}).items():
            ob, orl = order.get("base"), order.get("rel")
            if (ob == base and orl == rel) or (ob == rel and orl == base):
                out.append(
                    MakerOrder(
                        uuid=uuid,
                        base=ob,
                        rel=orl,
                        price=Decimal(str(order.get("price", "0"))),
                        available_amount=Decimal(str(order.get("available_amount", "0"))),
                    )
                )
        return out

    async def cancel_all_for_pair(self, base: str, rel: str) -> dict[str, Any]:
        """Cancels both directions of the pair so we never leave a stale side.

        Note: KDF's `Pair` filter is direction-sensitive, so we issue two
        cancellations — one for {base, rel} and one for {rel, base}.
        """
        cancelled_uuids: list[str] = []
        currently_matching: list[str] = []
        for b, r in [(base, rel), (rel, base)]:
            d = await self._post({
                "method": "cancel_all_orders",
                "cancel_by": {"type": "Pair", "data": {"base": b, "rel": r}},
            })
            res = d.get("result", {})
            cancelled_uuids.extend(res.get("cancelled", []))
            currently_matching.extend(res.get("currently_matching", []))
        return {"cancelled": cancelled_uuids, "currently_matching": currently_matching}

    async def setprice(
        self,
        base: str,
        rel: str,
        price: Decimal,
        volume: Decimal,
        *,
        max: bool = False,
        min_volume: Decimal | None = None,
        cancel_previous: bool = True,
    ) -> dict[str, Any]:
        """Post a maker order.

        If `max=True`, KDF posts the maximum sellable balance MINUS tx fees
        (the explicit `volume` is ignored). Use this whenever you want to
        sell "everything available" so you don't have to manually budget
        for the tx fee yourself.
        """
        body: dict[str, Any] = {
            "method": "setprice",
            "base": base,
            "rel": rel,
            "price": str(price),
            "max": max,
            "cancel_previous": cancel_previous,
        }
        if not max:
            body["volume"] = str(volume)
        if min_volume is not None:
            body["min_volume"] = str(min_volume)
        return await self._post(body)
