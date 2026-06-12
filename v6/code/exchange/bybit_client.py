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
        """Signed/public GET with retry on network errors, rate limits, and Bybit
        server errors (retCode 10006/10016/10018) — important on a small/laggy VM."""
        sem = _semaphore()
        sym = params.get("symbol", "")
        for attempt in range(5):
            try:
                async with sem:
                    headers = self._sign(params) if signed else {}
                    r = await self._http.get(path, params=params, headers=headers)
                data = r.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 4:
                    logger.error(f"GET {path} {sym} network error: {e}")
                    return {"retCode": -1, "retMsg": f"network:{e}"}
                logger.warning(f"GET {path} {sym} net retry {attempt}: {type(e).__name__} {e}")
                await asyncio.sleep(0.4 * (2 ** attempt))
                continue
            rc = str(data.get("retCode"))
            if r.status_code == 429 or rc in ("10006", "10016", "10018"):
                logger.warning(f"GET {path} {sym} retry {attempt}: http={r.status_code} retCode={rc} retMsg={data.get('retMsg')!r}")
                await asyncio.sleep(0.4 * (2 ** attempt))
                continue
            return data
        return {"retCode": -1, "retMsg": "retries_exhausted"}

    async def _post(self, path: str, body: dict) -> dict:
        """Signed POST. Retries on rate limit and on pre-response network errors —
        safe to retry because every order carries an orderLinkId (Bybit dedupes a
        duplicate id rather than placing a second order). Business retCodes (param
        errors, insufficient balance, etc.) are returned to the caller, not retried."""
        sem = _semaphore()
        raw = json.dumps(body, separators=(",", ":"))
        for attempt in range(5):
            try:
                async with sem:
                    headers = self._sign(raw, post=True)
                    r = await self._http.post(path, content=raw, headers=headers)
                data = r.json()
            except (httpx.TimeoutException, httpx.TransportError) as e:
                if attempt == 4:
                    logger.error(f"POST {path} network error: {e}")
                    return {"retCode": -1, "retMsg": f"network:{e}"}
                await asyncio.sleep(0.4 * (2 ** attempt))
                continue
            if r.status_code == 429 or str(data.get("retCode")) in ("10006", "10016", "10018"):
                await asyncio.sleep(0.4 * (2 ** attempt))
                continue
            return data
        return {"retCode": -1, "retMsg": "retries_exhausted"}

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

    async def get_positions_map(self) -> dict:
        """symbol → live position row, only entries with a non-zero size."""
        return {p["symbol"]: p for p in await self.get_positions()
                if float(p.get("size") or 0) != 0}

    async def get_available_usdt(self) -> Optional[float]:
        """Account-level margin available for new positions (UNIFIED, in USD)."""
        d = await self._get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}, signed=True)
        try:
            return float(d["result"]["list"][0].get("totalAvailableBalance") or 0)
        except Exception:
            return None

    async def get_closed_pnl(self, symbol: str, start_ms: Optional[int] = None, limit: int = 50) -> list[dict]:
        """Authoritative realized records for a symbol (exit price, closedPnl, fees),
        used to book exits the exchange filled — bot-closed, server SL, or liquidation."""
        params = {"category": TRADING.CATEGORY, "symbol": symbol, "limit": limit}
        if start_ms:
            params["startTime"] = int(start_ms)
        d = await self._get("/v5/position/closed-pnl", params, signed=True)
        return (d.get("result") or {}).get("list", [])

    async def get_execution(self, symbol: str, order_id: str) -> list[dict]:
        """Fills for one order — used to read the actual entry fee."""
        d = await self._get("/v5/execution/list",
                            {"category": TRADING.CATEGORY, "symbol": symbol,
                             "orderId": order_id, "limit": 50}, signed=True)
        return (d.get("result") or {}).get("list", [])

    async def get_executions(self, symbol: str, start_ms: Optional[int] = None, limit: int = 100) -> list[dict]:
        """All fills for a symbol since start_ms (entry fills have closedSize=0, exit
        fills closedSize>0). Used for the EXACT exit price (avg over partial fills) —
        more precise than closed-pnl's avgExitPrice, which is cost-based and diverges
        on partial fills. Note: V5 caps the window at ~7d when startTime is given."""
        params = {"category": TRADING.CATEGORY, "symbol": symbol, "limit": limit}
        if start_ms:
            params["startTime"] = int(start_ms)
        d = await self._get("/v5/execution/list", params, signed=True)
        return (d.get("result") or {}).get("list", [])

    async def place_market(self, symbol: str, side: str, qty: str, reduce_only: bool = False,
                           stop_loss: Optional[str] = None, take_profit: Optional[str] = None,
                           sl_trigger: str = "LastPrice", tp_trigger: str = "LastPrice",
                           order_link_id: Optional[str] = None) -> dict:
        """Market order (always IOC). An entry can carry its stop-loss AND take-profit
        atomically in the same request (reduce_only False) — so the position is never
        unprotected even if the bot dies before a follow-up call (the SL/TP live on the
        exchange). Verified mainnet pattern (METIS v4 xrp_live): entry order with
        takeProfit/stopLoss/tpslMode=Full, LastPrice trigger. For a short, take_profit is
        BELOW entry → Bybit fires a reduce-only Buy there. Bybit forbids SL/TP on a
        reduce-only order, so closes omit them. order_link_id makes the call idempotent."""
        body = {
            "category": TRADING.CATEGORY, "symbol": symbol, "side": side,
            "orderType": "Market", "qty": qty, "reduceOnly": reduce_only,
            "timeInForce": "IOC",
        }
        if order_link_id:
            body["orderLinkId"] = order_link_id[:36]
        if not reduce_only and (stop_loss is not None or take_profit is not None):
            body["tpslMode"] = "Full"
            body["positionIdx"] = 0
            if stop_loss is not None:
                body["stopLoss"] = stop_loss
                body["slTriggerBy"] = sl_trigger
                body["slOrderType"] = "Market"
            if take_profit is not None:
                body["takeProfit"] = take_profit
                body["tpTriggerBy"] = tp_trigger
                body["tpOrderType"] = "Market"
        return await self._post("/v5/order/create", body)

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
