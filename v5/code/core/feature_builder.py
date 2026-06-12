"""Feature builder — assembles the per-cycle FeatureSnapshot for one symbol.

For each symbol the builder fans out concurrent Bybit REST fetches (15m / 1h /
4h klines, funding history, open interest, recent trades, BTC + peer-major
context), derives T0 indicators, T1 derivatives, T2 structural levels and the
T4 context, merges in the live microstructure summary from StreamWorker, and
returns a frozen snapshot with a stable SHA-256 ``snapshot_id`` used by
telemetry.

Memory hygiene: raw kline arrays are dropped immediately after derived
features are computed, and ``gc.collect()`` runs at the end of each build so
RSS stays flat across cycles on a small VM.
"""
from __future__ import annotations

import asyncio
import gc
import hashlib
import json
import logging
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from config.settings import TRADING, CYCLE
from core.context_collector import (
    ContextAssetFeature,
    EventFilterResult,
    compute_context_asset,
    evaluate_event_filter,
)
from core.derivatives_extractor import T1Derivatives, compute_derivatives
from core.level_extractor import T2Levels, compute_levels
from core.stream_worker import StreamSummary, StreamWorker
from core.technical_indicators import OHLCV, T0Features, atr, compute_t0
from exchange.bybit_client import get_bybit_client

logger = logging.getLogger("metis.feature_builder")


# ──────────────────────────────────────────────────────────────────────
# Feature snapshot — the immutable runtime_features payload
# ──────────────────────────────────────────────────────────────────────
@dataclass
class FeatureSnapshot:
    """Frozen per-cycle feature snapshot. Serialized as the prompt's runtime_features block."""

    symbol: str
    asof_utc: str
    bar_close_utc: str
    # T0
    tf_15m: dict
    tf_1h: dict
    tf_4h: dict
    # T1
    derivatives: dict
    # T2
    levels: dict
    # T4
    btc_context: dict
    eth_or_sol_context: dict
    # event filter
    event_filter: dict
    # microstructure
    stream: dict
    # data quality
    data_quality_ok: bool
    quality_notes: list[str] = field(default_factory=list)
    # version
    snapshot_id: str = ""

    def compute_id(self) -> str:
        body = json.dumps(
            asdict(self), sort_keys=True, ensure_ascii=False, default=str,
            separators=(",", ":"),
        )
        self.snapshot_id = hashlib.sha256(body.encode("utf-8")).hexdigest()[:16]
        return self.snapshot_id

    def to_runtime_features_json(self) -> str:
        body = {
            "tf_15m": self.tf_15m,
            "tf_1h": self.tf_1h,
            "tf_4h": self.tf_4h,
            "derivatives": self.derivatives,
            "levels": self.levels,
            "btc_context": self.btc_context,
            "eth_or_sol_context": self.eth_or_sol_context,
            "stream": self.stream,
            "data_quality_ok": self.data_quality_ok,
            "quality_notes": self.quality_notes,
        }
        return json.dumps(body, ensure_ascii=False, default=str, separators=(",", ":"))


# ──────────────────────────────────────────────────────────────────────
# Builder
# ──────────────────────────────────────────────────────────────────────
class FeatureBuilder:
    """Fetches Bybit data for one symbol per cycle and assembles a FeatureSnapshot.

    Rate-limit aware: a TTL cache shared across symbols-in-the-same-cycle re-uses
    BTC/ETH/SOL 1h kline, funding history, OI series, and recent_trades. This cuts
    REST calls roughly in half (the two symbols in winner-takes-all share most of
    the same context endpoints).
    """

    # Per-endpoint TTL (seconds). 15m primary cycle → most endpoints can live ~5 min.
    _CACHE_TTL = {
        "kline_60": 240,           # 4 min — refresh once per cycle is fine
        "kline_240": 300,          # 5 min
        "funding_history": 1800,   # 30 min — funding settles every 8h
        "open_interest_5min": 240, # 4 min — 5-min granularity, refresh per cycle
        "recent_trades": 30,       # 30 sec — taker-flow advisory only
    }

    def __init__(self, stream_worker: Optional[StreamWorker] = None):
        self.bybit = get_bybit_client()
        self.stream = stream_worker
        # cache: key=(endpoint, symbol[, extra]) → (ts_epoch, value)
        self._cache: dict[tuple, tuple[float, object]] = {}
        self._cache_lock = asyncio.Lock()

    async def _cached(self, ttl: int, key: tuple, fetch_coro):
        """Generic TTL cache wrapper. fetch_coro is a *callable* returning a coroutine."""
        import time as _time
        now = _time.time()
        async with self._cache_lock:
            hit = self._cache.get(key)
            if hit and (now - hit[0]) < ttl:
                return hit[1]
        # cache miss outside the lock — fetch, then store
        value = await fetch_coro()
        async with self._cache_lock:
            self._cache[key] = (now, value)
        return value

    async def build(self, symbol: str, bar_close_utc: datetime) -> FeatureSnapshot:
        """Build the snapshot for one symbol (SOLUSDT or ETHUSDT)."""
        notes: list[str] = []
        now_utc = datetime.now(timezone.utc)
        asof = now_utc.isoformat()
        other_symbol = "ETHUSDT" if symbol == "SOLUSDT" else "SOLUSDT"

        # ── Fan out REST fetches. Symbol-specific endpoints are *uncached*
        # (15m primary tf is the analysis driver — must be fresh). Slower-moving
        # context endpoints (1h kline, funding, OI, cross-asset 1h) are TTL-cached
        # so the second symbol in the same cycle hits the cache.
        try:
            tasks = {
                "k15":   asyncio.create_task(self.bybit.get_kline(symbol, "15", limit=400)),
                "k1h":   asyncio.create_task(self._cached(
                    self._CACHE_TTL["kline_60"], ("kline_60", symbol),
                    lambda: self.bybit.get_kline(symbol, "60", limit=200),
                )),
                "k4h":   asyncio.create_task(self._cached(
                    self._CACHE_TTL["kline_240"], ("kline_240", symbol),
                    lambda: self.bybit.get_kline(symbol, "240", limit=120),
                )),
                "fund":  asyncio.create_task(self._cached(
                    self._CACHE_TTL["funding_history"], ("funding_history", symbol),
                    lambda: self.bybit.get_funding_history(symbol, limit=24),
                )),
                "oi":    asyncio.create_task(self._cached(
                    self._CACHE_TTL["open_interest_5min"], ("open_interest_5min", symbol),
                    lambda: self.bybit.get_open_interest(symbol, "5min", limit=60),
                )),
                "trd":   asyncio.create_task(self._cached(
                    self._CACHE_TTL["recent_trades"], ("recent_trades", symbol),
                    lambda: self.bybit.get_recent_trades(symbol, limit=200),
                )),
                "btc":   asyncio.create_task(self._cached(
                    self._CACHE_TTL["kline_60"], ("kline_60", "BTCUSDT"),
                    lambda: self.bybit.get_kline("BTCUSDT", "60", limit=80),
                )),
                "other": asyncio.create_task(self._cached(
                    self._CACHE_TTL["kline_60"], ("kline_60_ctx", other_symbol),
                    lambda: self.bybit.get_kline(other_symbol, "60", limit=80),
                )),
            }
            results = await asyncio.gather(*tasks.values())
            raw_15m, raw_1h, raw_4h, funding, oi_hist, trades, btc_raw, other_raw = results
        except Exception as e:
            logger.error(f"feature fetch failed for {symbol}: {e}")
            notes.append(f"fetch_error:{str(e)[:80]}")
            raw_15m = raw_1h = raw_4h = []
            funding = oi_hist = trades = btc_raw = other_raw = []

        # ── Build OHLCVs ──
        o15 = OHLCV.from_bybit_kline(raw_15m)
        o1h = OHLCV.from_bybit_kline(raw_1h)
        o4h = OHLCV.from_bybit_kline(raw_4h)
        btc1h = OHLCV.from_bybit_kline(btc_raw)
        other1h = OHLCV.from_bybit_kline(other_raw)

        # Data quality checks
        if len(o15) < 200 or len(o1h) < 50 or len(o4h) < 50:
            notes.append(f"insufficient_bars:15m={len(o15)},1h={len(o1h)},4h={len(o4h)}")

        # ── T0 indicators ──
        t0_15 = compute_t0(o15)
        t0_1h = compute_t0(o1h)
        t0_4h = compute_t0(o4h)

        # ── T2 levels ──
        atr15_arr = atr(o15.high, o15.low, o15.close, 14) if len(o15) > 15 else None
        atr15_last = float(atr15_arr[-1]) if atr15_arr is not None and len(atr15_arr) > 0 and math.isfinite(atr15_arr[-1]) else float("nan")
        t2 = compute_levels(o15, atr15_last)

        # ── T1 derivatives ──
        # Bybit V5 does not expose an aggregate 24h liquidation figure via REST.
        # When live, ``stream_worker`` may subscribe to ``liquidation.{symbol}``
        # and accumulate the running total; until then we pass 0.
        t1 = compute_derivatives(
            funding_history=funding,
            oi_history=oi_hist,
            recent_trades=trades,
            liq_long_24h_usd=0.0,
            liq_short_24h_usd=0.0,
        )

        # ── T4 context ──
        def _ctx_from_1h(sym: str, oc: OHLCV) -> ContextAssetFeature:
            t0_ctx = compute_t0(oc)
            return compute_context_asset(
                symbol=sym,
                ret_1h_pct=t0_ctx.ret_1,
                ret_4h_pct=t0_ctx.ret_4,
                ema20=t0_ctx.ema20,
                ema50=t0_ctx.ema50,
                rsi14=t0_ctx.rsi14,
                adx14=t0_ctx.adx14,
                atr14_pct=t0_ctx.atr14_pct,
                last_close=t0_ctx.close,
            )

        btc_ctx = _ctx_from_1h("BTCUSDT", btc1h)
        other_ctx = _ctx_from_1h(other_symbol, other1h)

        # ── Event filter ──
        ev = evaluate_event_filter(now_utc=now_utc)

        # ── Microstructure summary (advisory, never gating) ──
        # If the WebSocket is still initializing or stale, send the LLM a small
        # "unavailable" object instead of NaN-laden fields. NaNs in the prompt
        # confuse the model into raising DATA_QUALITY_FAIL.
        if self.stream is not None:
            stream_summary = self.stream.summary(symbol)
            init_ticker = stream_summary.last_ticker_age_sec >= 9000
            init_book = stream_summary.last_book_age_sec >= 9000
            stale_ticker = (not init_ticker) and stream_summary.last_ticker_age_sec > CYCLE.WS_MAX_STALE_SEC
            stale_book = (not init_book) and stream_summary.last_book_age_sec > CYCLE.WS_MAX_STALE_SEC

            if init_ticker or init_book or stale_ticker or stale_book:
                # Suppress NaN-laden raw dict; tell LLM the stream is simply not yet ready.
                state = "initializing" if (init_ticker or init_book) else "stale"
                stream_dict = {
                    "symbol": symbol,
                    "available": False,
                    "state": state,
                    "note": "Microstructure (ticker/orderbook/taker) not available this cycle; OHLCV+derivatives only.",
                }
                if init_ticker: notes.append("ws_ticker_init")
                if init_book: notes.append("ws_book_init")
                if stale_ticker: notes.append(f"ws_ticker_stale:{stream_summary.last_ticker_age_sec:.0f}s")
                if stale_book: notes.append(f"ws_book_stale:{stream_summary.last_book_age_sec:.0f}s")
            else:
                stream_dict = asdict(stream_summary)
                stream_dict["available"] = True
        else:
            stream_dict = {"symbol": symbol, "available": False, "state": "no_worker"}
            notes.append("stream_worker_unavailable")

        # ── Final data_quality flag ──
        # Microstructure is advisory; the core analysis only needs REST OHLCV.
        # WS init/stale is logged to ``notes`` but does not flip data_quality_ok.
        data_quality_ok = (
            len(o15) >= 200
            and len(o1h) >= 50
            and math.isfinite(t0_15.close)
            and math.isfinite(t0_15.atr14_pct)
            and not any(n.startswith("fetch_error") for n in notes)
        )

        snap = FeatureSnapshot(
            symbol=symbol,
            asof_utc=asof,
            bar_close_utc=bar_close_utc.isoformat(),
            tf_15m=asdict(t0_15),
            tf_1h=asdict(t0_1h),
            tf_4h=asdict(t0_4h),
            derivatives=asdict(t1),
            levels=asdict(t2),
            btc_context=asdict(btc_ctx),
            eth_or_sol_context=asdict(other_ctx),
            event_filter=asdict(ev),
            stream=stream_dict,
            data_quality_ok=data_quality_ok,
            quality_notes=notes,
        )
        snap.compute_id()

        # ── memory hygiene ──
        del raw_15m, raw_1h, raw_4h, btc_raw, other_raw, trades, funding, oi_hist
        del o15, o1h, o4h, btc1h, other1h
        if atr15_arr is not None:
            del atr15_arr
        gc.collect()

        return snap

    def runtime_anchor_json(self, cycle_id: str, symbol: str, bar_close_utc: datetime, snapshot_id: str) -> str:
        body = {
            "cycle_id": cycle_id,
            "symbol": symbol,
            "venue": "bybit_usdt_perpetual",
            "bar_close_utc": bar_close_utc.isoformat(),
            "asof_utc": datetime.now(timezone.utc).isoformat(),
            "input_snapshot_id": snapshot_id,
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

    def runtime_controls_json(
        self,
        data_quality_ok: bool,
        event_filter_state: str,
        position_state: dict,
        risk_state: dict,
    ) -> str:
        body = {
            "data_quality_ok": data_quality_ok,
            "event_filter_state": event_filter_state,
            "current_position_state": position_state,
            "read_only_risk_state": risk_state,
        }
        return json.dumps(body, ensure_ascii=False, separators=(",", ":"))
