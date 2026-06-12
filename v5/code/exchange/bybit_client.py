"""Lightweight async client for the Bybit V5 REST API.

Covers the subset of endpoints the engine needs: public market data, signed
account/position queries, and order placement / protection orders. A
process-wide semaphore caps concurrent in-flight requests to stay well under
Bybit's per-IP rate limits, and rate-limit responses (HTTP 429, retCode 10006
/ 10018) are retried with exponential backoff.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import random
import time
from typing import Any, Optional

import httpx

from config.settings import BYBIT, RES

logger = logging.getLogger("metis.bybit_client")


class BybitError(Exception):
    pass


# Process-wide concurrency cap — keeps burst well under Bybit IP-level limits.
# Bybit V5 market endpoints allow ~30-50 req/s, but burst over 10 in <1s
# can trip retCode 10006. Cap at 5 in-flight to leave headroom for other callers.
_REQ_SEMAPHORE: Optional[asyncio.Semaphore] = None


def _get_semaphore() -> asyncio.Semaphore:
    """Concurrent in-flight cap. Bybit V5 IP burst ~10 req/sec; we hold 3 to leave
    headroom for dashboard polls and watchdog checks sharing the same IP."""
    global _REQ_SEMAPHORE
    if _REQ_SEMAPHORE is None:
        _REQ_SEMAPHORE = asyncio.Semaphore(3)
    return _REQ_SEMAPHORE


class BybitClient:
    """Async client for Bybit V5 public market data and signed account endpoints."""

    def __init__(self, api_key: Optional[str] = None, secret: Optional[str] = None):
        self.api_key = api_key if api_key is not None else BYBIT.API_KEY
        self.secret = secret if secret is not None else BYBIT.SECRET
        self.base_url = BYBIT.BASE_URL
        # Reuse client (connection pool) — memory friendly
        self._client = httpx.AsyncClient(
            base_url=self.base_url,
            timeout=httpx.Timeout(15.0, connect=5.0),
            limits=httpx.Limits(
                max_connections=RES.HTTP_POOL_MAXSIZE,
                max_keepalive_connections=RES.HTTP_POOL_CONNECTIONS,
            ),
            headers={"User-Agent": "metis/1.0"},
        )

    async def close(self):
        await self._client.aclose()

    # ──────────────────────────────────────────────────────────────────
    # Internals
    # ──────────────────────────────────────────────────────────────────
    def _sign(self, params: dict) -> dict:
        ts = str(int(time.time() * 1000))
        recv = str(BYBIT.RECV_WINDOW_MS)
        sorted_qs = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        msg = ts + self.api_key + recv + sorted_qs
        sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": self.api_key,
            "X-BAPI-TIMESTAMP": ts,
            "X-BAPI-RECV-WINDOW": recv,
            "X-BAPI-SIGN": sig,
        }

    async def _get(self, path: str, params: Optional[dict] = None, signed: bool = False,
                   max_retries: int = 4) -> dict:
        """GET with concurrency cap + exponential backoff for rate limits (10006/429)."""
        params = params or {}
        sem = _get_semaphore()
        last_err: Optional[str] = None
        for attempt in range(max_retries):
            async with sem:
                try:
                    headers = self._sign(params) if signed else {}
                    r = await self._client.get(path, params=params, headers=headers)
                except httpx.TimeoutException as e:
                    last_err = f"timeout: {e}"
                    # Treat as transient
                    if attempt < max_retries - 1:
                        await asyncio.sleep((1.5 ** attempt) + random.random() * 0.3)
                        continue
                    raise BybitError(last_err)
            # Rate limit (HTTP-level)
            if r.status_code == 429:
                last_err = f"HTTP 429 on {path}"
                if attempt < max_retries - 1:
                    backoff = (2.0 ** attempt) + random.random() * 0.5
                    logger.warning(f"{last_err}; backoff {backoff:.1f}s (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(backoff)
                    continue
                raise BybitError(last_err)
            if r.status_code != 200:
                raise BybitError(f"HTTP {r.status_code} on {path}: {r.text[:200]}")
            data = r.json()
            ret_code = str(data.get("retCode"))
            if ret_code == "0":
                return data
            # Rate limit (Bybit application-level): retCode 10006
            if ret_code in ("10006", "10018"):  # 10018 = ip rate limit
                last_err = f"Bybit retCode={ret_code} {data.get('retMsg')}"
                if attempt < max_retries - 1:
                    backoff = (2.0 ** attempt) + random.random() * 0.5
                    logger.warning(f"{last_err}; backoff {backoff:.1f}s on {path} (attempt {attempt + 1}/{max_retries})")
                    await asyncio.sleep(backoff)
                    continue
                raise BybitError(last_err)
            # other non-zero retCode = permanent error
            raise BybitError(f"Bybit retCode={ret_code} {data.get('retMsg')}")
        raise BybitError(last_err or "max_retries_exceeded")

    async def _post(self, path: str, body: dict, signed: bool = True, max_retries: int = 3) -> dict:
        sem = _get_semaphore()
        body_str = json.dumps(body, separators=(",", ":"))
        for attempt in range(max_retries):
            async with sem:
                ts = str(int(time.time() * 1000))
                recv = str(BYBIT.RECV_WINDOW_MS)
                msg = ts + self.api_key + recv + body_str
                sig = hmac.new(self.secret.encode(), msg.encode(), hashlib.sha256).hexdigest()
                headers = {
                    "X-BAPI-API-KEY": self.api_key,
                    "X-BAPI-TIMESTAMP": ts,
                    "X-BAPI-RECV-WINDOW": recv,
                    "X-BAPI-SIGN": sig,
                    "Content-Type": "application/json",
                }
                r = await self._client.post(path, content=body_str, headers=headers)
            if r.status_code == 429:
                if attempt < max_retries - 1:
                    await asyncio.sleep((2.0 ** attempt) + random.random() * 0.5)
                    continue
                raise BybitError(f"HTTP 429 on POST {path}")
            if r.status_code != 200:
                raise BybitError(f"HTTP {r.status_code} on POST {path}: {r.text[:200]}")
            data = r.json()
            ret = str(data.get("retCode"))
            if ret == "0":
                return data
            if ret in ("10006", "10018") and attempt < max_retries - 1:
                await asyncio.sleep((2.0 ** attempt) + random.random() * 0.5)
                continue
            raise BybitError(f"Bybit retCode={ret} {data.get('retMsg')}")
        raise BybitError("max_retries_exceeded")

    # ──────────────────────────────────────────────────────────────────
    # Public market data
    # ──────────────────────────────────────────────────────────────────
    async def get_kline(
        self, symbol: str, interval: str, limit: int = 200, category: str = "linear"
    ) -> list[list]:
        """Return raw list of klines [openTime, open, high, low, close, volume, turnover].

        Bybit returns newest first; we reverse to oldest-first for indicator math.
        """
        data = await self._get(
            "/v5/market/kline",
            params={"category": category, "symbol": symbol, "interval": interval, "limit": min(limit, 1000)},
        )
        klines = data.get("result", {}).get("list", [])
        return list(reversed(klines))

    async def get_ticker(self, symbol: str, category: str = "linear") -> dict:
        data = await self._get(
            "/v5/market/tickers", params={"category": category, "symbol": symbol}
        )
        lst = data.get("result", {}).get("list", [])
        return lst[0] if lst else {}

    async def get_orderbook(self, symbol: str, depth: int = 25, category: str = "linear") -> dict:
        data = await self._get(
            "/v5/market/orderbook", params={"category": category, "symbol": symbol, "limit": depth}
        )
        return data.get("result", {})

    async def get_funding_history(self, symbol: str, limit: int = 24, category: str = "linear") -> list[dict]:
        """Recent funding rate snapshots (8h interval). limit=24 ≈ 8 days."""
        data = await self._get(
            "/v5/market/funding/history",
            params={"category": category, "symbol": symbol, "limit": min(limit, 200)},
        )
        return data.get("result", {}).get("list", [])

    async def get_open_interest(
        self, symbol: str, interval_time: str = "5min", limit: int = 60, category: str = "linear"
    ) -> list[dict]:
        """OI over time. interval_time ∈ {5min, 15min, 30min, 1h, 4h, 1d}."""
        data = await self._get(
            "/v5/market/open-interest",
            params={"category": category, "symbol": symbol, "intervalTime": interval_time, "limit": limit},
        )
        return data.get("result", {}).get("list", [])

    async def get_recent_trades(self, symbol: str, limit: int = 200, category: str = "linear") -> list[dict]:
        data = await self._get(
            "/v5/market/recent-trade",
            params={"category": category, "symbol": symbol, "limit": min(limit, 1000)},
        )
        return data.get("result", {}).get("list", [])

    async def get_instrument_info(self, symbol: str, category: str = "linear") -> dict:
        data = await self._get(
            "/v5/market/instruments-info", params={"category": category, "symbol": symbol}
        )
        lst = data.get("result", {}).get("list", [])
        return lst[0] if lst else {}

    async def get_server_time(self) -> int:
        r = await self._client.get("/v5/market/time")
        return int(r.json().get("result", {}).get("timeNano", "0")) // 1_000_000

    # ──────────────────────────────────────────────────────────────────
    # Signed endpoints — account / position / orders
    # ──────────────────────────────────────────────────────────────────
    async def get_wallet_balance(self, account_type: str = "UNIFIED") -> dict:
        return await self._get(
            "/v5/account/wallet-balance",
            params={"accountType": account_type, "coin": "USDT"},
            signed=True,
        )

    async def get_positions(self, symbol: Optional[str] = None, category: str = "linear") -> list[dict]:
        params: dict[str, Any] = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/v5/position/list", params=params, signed=True)
        return data.get("result", {}).get("list", [])

    async def get_open_orders(self, symbol: Optional[str] = None, category: str = "linear") -> list[dict]:
        params: dict[str, Any] = {"category": category, "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/v5/order/realtime", params=params, signed=True)
        return data.get("result", {}).get("list", [])

    async def get_executions(self, symbol: Optional[str] = None, limit: int = 50, category: str = "linear") -> list[dict]:
        params: dict[str, Any] = {"category": category, "limit": min(limit, 100), "settleCoin": "USDT"}
        if symbol:
            params["symbol"] = symbol
        data = await self._get("/v5/execution/list", params=params, signed=True)
        return data.get("result", {}).get("list", [])

    async def set_leverage(self, symbol: str, leverage: int, category: str = "linear") -> dict:
        body = {
            "category": category, "symbol": symbol,
            "buyLeverage": str(leverage), "sellLeverage": str(leverage),
        }
        try:
            return await self._post("/v5/position/set-leverage", body, signed=True)
        except BybitError as e:
            # 110043 leverage not modified
            if "110043" in str(e):
                return {"retCode": "0", "retMsg": "leverage_already_set"}
            raise

    async def place_order(
        self,
        symbol: str,
        side: str,   # Buy | Sell
        order_type: str,  # Market | Limit
        qty: float,
        price: Optional[float] = None,
        client_order_id: Optional[str] = None,
        reduce_only: bool = False,
        time_in_force: str = "IOC",
        category: str = "linear",
        position_idx: int = 0,
    ) -> dict:
        body: dict[str, Any] = {
            "category": category,
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": str(qty),
            "timeInForce": time_in_force,
            "positionIdx": position_idx,
        }
        if price is not None:
            body["price"] = str(price)
        if client_order_id:
            body["orderLinkId"] = client_order_id
        if reduce_only:
            body["reduceOnly"] = True
        return await self._post("/v5/order/create", body, signed=True)

    async def cancel_order(
        self,
        symbol: str,
        order_id: Optional[str] = None,
        client_order_id: Optional[str] = None,
        category: str = "linear",
    ) -> dict:
        body: dict[str, Any] = {"category": category, "symbol": symbol}
        if order_id:
            body["orderId"] = order_id
        if client_order_id:
            body["orderLinkId"] = client_order_id
        return await self._post("/v5/order/cancel", body, signed=True)

    async def set_trading_stop(
        self,
        symbol: str,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
        category: str = "linear",
        position_idx: int = 0,
        trigger_by: str = "MarkPrice",
    ) -> dict:
        """Native exchange-side reduce-only SL/TP attached to current position."""
        body: dict[str, Any] = {
            "category": category, "symbol": symbol,
            "positionIdx": position_idx,
            "tpslMode": "Full",
            "slTriggerBy": trigger_by,
            "tpTriggerBy": trigger_by,
        }
        if stop_loss is not None:
            body["stopLoss"] = str(stop_loss)
        if take_profit is not None:
            body["takeProfit"] = str(take_profit)
        return await self._post("/v5/position/trading-stop", body, signed=True)


# Singleton accessor — explicit lifecycle per process
_client_singleton: Optional[BybitClient] = None


def get_bybit_client() -> BybitClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = BybitClient()
    return _client_singleton


async def close_bybit_client():
    global _client_singleton
    if _client_singleton is not None:
        await _client_singleton.close()
        _client_singleton = None
