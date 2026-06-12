"""Async Bybit V5 REST client — public market data + signed trading calls.

A process-wide semaphore caps concurrent in-flight requests well under Bybit's
per-IP limit, and rate-limit responses (HTTP 429, retCode 10006/10018) are
retried with exponential backoff. Signed calls use HMAC-SHA256 over the V5
`timestamp + api_key + recv_window + body` scheme.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import time
from typing import Optional

import httpx

from config.settings import BYBIT, RES, TRADING

logger = logging.getLogger("metis.bybit")

_sem: Optional[asyncio.Semaphore] = None
_client: Optional["BybitClient"] = None


def _semaphore() -> asyncio.Semaphore:
    global _sem
    if _sem is None:
        _sem = asyncio.Semaphore(RES.HTTP_SEMAPHORE)
    return _sem


class BybitClient:
    def __init__(self):
        self._http = httpx.AsyncClient(
            base_url=BYBIT.base_url,
            timeout=10.0,
            limits=httpx.Limits(max_connections=RES.HTTP_POOL_MAXSIZE, max_keepalive_connections=RES.HTTP_POOL_MAXSIZE),
        )

    async def close(self):
        await self._http.aclose()

    # ── low level ──
    async def _get(self, path: str, params: dict, signed: bool = False) -> dict:
        sem = _semaphore()
        for attempt in range(5):
            async with sem:
                headers = self._sign(params) if signed else {}
                r = await self._http.get(path, params=params, headers=headers)
            data = r.json()
            if r.status_code == 429 or str(data.get("retCode")) in ("10006", "10018"):
                await asyncio.sleep(0.3 * (2 ** attempt))
                continue
            return data
        return {"retCode": -1, "retMsg": "rate_limited"}

    async def _post(self, path: str, body: dict) -> dict:
        sem = _semaphore()
        raw = json.dumps(body, separators=(",", ":"))
        for attempt in range(5):
            async with sem:
                headers = self._sign(raw, post=True)
                r = await self._http.post(path, content=raw, headers=headers)
            data = r.json()
            if r.status_code == 429 or str(data.get("retCode")) in ("10006", "10018"):
                await asyncio.sleep(0.3 * (2 ** attempt))
                continue
            return data
        return {"retCode": -1, "retMsg": "rate_limited"}

    def _sign(self, payload, post: bool = False) -> dict:
        ts = str(int(time.time() * 1000))
        rw = str(BYBIT.RECV_WINDOW_MS)
        if post:
            origin = ts + BYBIT.API_KEY + rw + payload
        else:
            qs = "&".join(f"{k}={v}" for k, v in payload.items())
            origin = ts + BYBIT.API_KEY + rw + qs
        sign = hmac.new(BYBIT.SECRET.encode(), origin.encode(), hashlib.sha256).hexdigest()
        h = {"X-BAPI-API-KEY": BYBIT.API_KEY, "X-BAPI-TIMESTAMP": ts,
             "X-BAPI-RECV-WINDOW": rw, "X-BAPI-SIGN": sign}
        if post:
            h["Content-Type"] = "application/json"
        return h

    # ── public ──
    async def get_kline(self, symbol: str, interval: str, limit: int = 120) -> list[list]:
        d = await self._get("/v5/market/kline",
                            {"category": TRADING.CATEGORY, "symbol": symbol,
                             "interval": interval, "limit": min(limit, 1000)})
        return (d.get("result") or {}).get("list", [])

    async def get_last_price(self, symbol: str) -> Optional[float]:
        d = await self._get("/v5/market/tickers", {"category": TRADING.CATEGORY, "symbol": symbol})
        lst = (d.get("result") or {}).get("list", [])
        return float(lst[0]["lastPrice"]) if lst else None

    async def refresh_instrument_specs(self) -> dict:
        """Pull live tick/lot/min-qty per symbol; fall back to config on failure."""
        d = await self._get("/v5/market/instruments-info", {"category": TRADING.CATEGORY})
        out = {}
        for it in (d.get("result") or {}).get("list", []):
            sym = it.get("symbol")
            if sym not in TRADING.SYMBOLS:
                continue
            lf = it.get("lotSizeFilter", {}); pf = it.get("priceFilter", {})
            step = float(lf.get("qtyStep", 0) or 0)
            prec = len(str(step).split(".")[1]) if "." in str(step) and step < 1 else 0
            out[sym] = {
                "qty_step": step or TRADING.fallback_specs[sym]["qty_step"],
                "qty_precision": prec,
                "tick_size": float(pf.get("tickSize", 0) or TRADING.fallback_specs[sym]["tick_size"]),
                "min_order_qty": float(lf.get("minOrderQty", 0) or TRADING.fallback_specs[sym]["min_order_qty"]),
            }
        for sym in TRADING.SYMBOLS:
            out.setdefault(sym, TRADING.fallback_specs[sym])
        return out

    # ── signed ──
    async def get_wallet_equity(self) -> Optional[float]:
        d = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        try:
            return float((d["result"]["list"][0]["totalEquity"]))
        except Exception:
            return None

    async def get_positions(self) -> list[dict]:
        d = await self._get("/v5/position/list", {"category": TRADING.CATEGORY, "settleCoin": "USDT"}, signed=True)
        return (d.get("result") or {}).get("list", [])

    async def place_market(self, symbol: str, side: str, qty: str, reduce_only: bool = False) -> dict:
        return await self._post("/v5/order/create", {
            "category": TRADING.CATEGORY, "symbol": symbol, "side": side,
            "orderType": "Market", "qty": qty, "reduceOnly": reduce_only,
            "timeInForce": "IOC",
        })

    async def set_stop_loss(self, symbol: str, stop_price: str) -> dict:
        """Attach a full-position stop-loss (reduce-only, market trigger) — held
        exchange-side so the position is protected even if the bot is down."""
        return await self._post("/v5/position/trading-stop", {
            "category": TRADING.CATEGORY, "symbol": symbol,
            "stopLoss": stop_price, "tpslMode": "Full", "slTriggerBy": "LastPrice",
            "positionIdx": 0,
        })

    async def set_leverage(self, symbol: str, lev: int) -> dict:
        return await self._post("/v5/position/set-leverage", {
            "category": TRADING.CATEGORY, "symbol": symbol,
            "buyLeverage": str(lev), "sellLeverage": str(lev),
        })


def get_bybit_client() -> BybitClient:
    global _client
    if _client is None:
        _client = BybitClient()
    return _client


async def close_bybit_client():
    global _client
    if _client is not None:
        await _client.close()
        _client = None
