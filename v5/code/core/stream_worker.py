"""WebSocket stream worker — live microstructure aggregates.

Subscribes to Bybit V5 public streams (``tickers``, ``orderbook.1``,
``publicTrade``) and maintains in-memory rolling aggregates per symbol.

Memory-conscious design:
- Every buffer is bounded with ``deque(maxlen=...)``; no unbounded growth.
- Raw orderbook deltas are never stored — only the derived top-of-book spread
  and order-book imbalance flow into 30-second rolling stats.
- The cycle code reads ``summary(symbol)`` once per cycle for a compact
  ``StreamSummary`` snapshot; the LLM never sees the raw tick stream.

The loop auto-reconnects with exponential backoff and respects websockets'
ping/pong heartbeat to detect dead connections.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Optional

import websockets

from config.settings import BYBIT, RES, CYCLE

logger = logging.getLogger("metis.stream_worker")


@dataclass
class StreamSummary:
    """Compact per-symbol microstructure snapshot consumed by the cycle code."""

    symbol: str
    last_update_ms: int = 0
    last_mark_price: float = float("nan")
    last_bid: float = float("nan")
    last_ask: float = float("nan")
    spread_bps: float = float("nan")
    spread_bps_30s_avg: float = float("nan")
    obi_5s: float = float("nan")     # (bidQty - askQty) / (bidQty + askQty) at top levels
    obi_30s_median: float = float("nan")
    taker_buy_vol_5s: float = 0.0
    taker_sell_vol_5s: float = 0.0
    taker_buy_vol_30s: float = 0.0
    taker_sell_vol_30s: float = 0.0
    # data freshness
    seq_ok: bool = True
    last_ticker_age_sec: float = 0.0
    last_book_age_sec: float = 0.0


class _SymbolBuffers:
    """Per-symbol in-memory rolling buffers. All deques are length-bounded."""

    def __init__(self, symbol: str):
        self.symbol = symbol
        self.spread_bps_30s: deque[tuple[float, float]] = deque(maxlen=RES.STREAM_TICKER_MAX_TICKS)
        self.obi_30s: deque[tuple[float, float]] = deque(maxlen=RES.STREAM_TICKER_MAX_TICKS)
        self.trades_30s: deque[tuple[float, str, float]] = deque(maxlen=RES.STREAM_KLINE_MAX_BARS * 2)
        self.last_ticker_ms: int = 0
        self.last_book_ms: int = 0
        self.last_mark_price: float = float("nan")
        self.last_bid: float = float("nan")
        self.last_ask: float = float("nan")
        self.last_obi: float = float("nan")


class StreamWorker:
    """Single-process WebSocket consumer for one or more symbols."""

    def __init__(self, symbols: list[str]):
        self.symbols = symbols
        self._bufs: dict[str, _SymbolBuffers] = {s: _SymbolBuffers(s) for s in symbols}
        self._running = False
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._task: Optional[asyncio.Task] = None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="stream_worker")

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass

    def summary(self, symbol: str) -> StreamSummary:
        b = self._bufs.get(symbol)
        if b is None:
            return StreamSummary(symbol=symbol)

        now = time.time()
        last_book_age = now - (b.last_book_ms / 1000) if b.last_book_ms > 0 else 9999.0
        last_ticker_age = now - (b.last_ticker_ms / 1000) if b.last_ticker_ms > 0 else 9999.0

        spread_now = float("nan")
        if b.last_bid > 0 and b.last_ask > 0 and b.last_bid < b.last_ask:
            spread_now = (b.last_ask - b.last_bid) / b.last_bid * 10000

        # 30s avg
        cutoff_30s = now - 30
        recent_spread = [v for (t, v) in b.spread_bps_30s if t >= cutoff_30s]
        avg_30 = sum(recent_spread) / len(recent_spread) if recent_spread else float("nan")
        recent_obi = sorted([v for (t, v) in b.obi_30s if t >= cutoff_30s])
        obi_median = (
            recent_obi[len(recent_obi) // 2] if recent_obi else float("nan")
        )
        # taker volumes
        buy5 = sell5 = buy30 = sell30 = 0.0
        cutoff_5s = now - 5
        for ts, side, sz in b.trades_30s:
            if side == "buy":
                if ts >= cutoff_30s: buy30 += sz
                if ts >= cutoff_5s: buy5 += sz
            elif side == "sell":
                if ts >= cutoff_30s: sell30 += sz
                if ts >= cutoff_5s: sell5 += sz

        return StreamSummary(
            symbol=symbol,
            last_update_ms=max(b.last_ticker_ms, b.last_book_ms),
            last_mark_price=b.last_mark_price,
            last_bid=b.last_bid,
            last_ask=b.last_ask,
            spread_bps=round(spread_now, 3) if spread_now == spread_now else float("nan"),
            spread_bps_30s_avg=round(avg_30, 3) if avg_30 == avg_30 else float("nan"),
            obi_5s=round(b.last_obi, 3) if b.last_obi == b.last_obi else float("nan"),
            obi_30s_median=round(obi_median, 3) if obi_median == obi_median else float("nan"),
            taker_buy_vol_5s=round(buy5, 4),
            taker_sell_vol_5s=round(sell5, 4),
            taker_buy_vol_30s=round(buy30, 4),
            taker_sell_vol_30s=round(sell30, 4),
            seq_ok=True,
            last_ticker_age_sec=round(last_ticker_age, 2),
            last_book_age_sec=round(last_book_age, 2),
        )

    # ─────────────────────── WS loop ───────────────────────
    async def _loop(self):
        backoff = 2.0
        while self._running:
            try:
                async with websockets.connect(
                    BYBIT.WS_PUBLIC,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                    max_queue=RES.WS_QUEUE_MAXSIZE,
                ) as ws:
                    self._ws = ws
                    await self._subscribe(ws)
                    backoff = 2.0
                    async for raw in ws:
                        if not self._running:
                            break
                        await self._handle(raw)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"WS connection broke: {e}; reconnecting in {backoff:.1f}s")
                await asyncio.sleep(min(backoff, 30.0))
                backoff = min(backoff * 1.7, 30.0)

    async def _subscribe(self, ws):
        args = []
        for s in self.symbols:
            args.append(f"tickers.{s}")
            args.append(f"orderbook.1.{s}")  # top-of-book only — light bandwidth
            args.append(f"publicTrade.{s}")
        msg = json.dumps({"op": "subscribe", "args": args})
        await ws.send(msg)

    async def _handle(self, raw: str | bytes):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        topic: str = msg.get("topic", "")
        data = msg.get("data")
        ts_ms: int = int(msg.get("ts", 0)) or int(time.time() * 1000)

        if not topic or data is None:
            return

        symbol = topic.rsplit(".", 1)[-1]
        if symbol not in self._bufs:
            return
        b = self._bufs[symbol]

        if topic.startswith("tickers."):
            try:
                if isinstance(data, list):
                    data = data[0]
                mark = float(data.get("markPrice") or data.get("lastPrice") or 0)
                if mark > 0:
                    b.last_mark_price = mark
                b.last_ticker_ms = ts_ms
            except (TypeError, ValueError):
                pass
        elif topic.startswith("orderbook.1."):
            try:
                if isinstance(data, list):
                    data = data[0]
                bids = data.get("b") or []
                asks = data.get("a") or []
                if bids and asks:
                    bid = float(bids[0][0])
                    bid_q = float(bids[0][1])
                    ask = float(asks[0][0])
                    ask_q = float(asks[0][1])
                    if bid > 0 and ask > 0 and bid < ask:
                        b.last_bid = bid
                        b.last_ask = ask
                        spread_bps = (ask - bid) / bid * 10000
                        b.spread_bps_30s.append((time.time(), spread_bps))
                        total = bid_q + ask_q
                        if total > 0:
                            obi = (bid_q - ask_q) / total
                            b.last_obi = obi
                            b.obi_30s.append((time.time(), obi))
                b.last_book_ms = ts_ms
            except (TypeError, ValueError, IndexError):
                pass
        elif topic.startswith("publicTrade."):
            trades = data if isinstance(data, list) else [data]
            now_s = time.time()
            for t in trades:
                try:
                    side = (t.get("S") or t.get("side") or "").lower()
                    size = float(t.get("v") or t.get("size") or 0)
                    if size <= 0:
                        continue
                    b.trades_30s.append((now_s, side, size))
                except (TypeError, ValueError):
                    continue
