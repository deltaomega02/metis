"""METIS breakout portfolio engine — main orchestrator.

Single asyncio process. Each 4h cycle: fetch the closed klines for every symbol,
exit open positions on stop-loss or trend-end, then open the strongest new
breakouts up to the concurrency cap. Paper or live is chosen by PAPER_MODE; the
breakout signal and risk sizing are identical to the validated backtest.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (CYCLE, LOGS_DIR, PAPER_INITIAL_BALANCE_USDT,
                             PAPER_MODE, RISK, STRATEGY, TRADING)
from core import risk_engine
from core.breakout_signal import evaluate, should_exit
from core.scheduler import CycleScheduler
from core.state_store import get_state_store
from exchange.bybit_client import close_bybit_client, get_bybit_client
from exchange.executor import LiveExecutor
from exchange.paper_executor import PaperExecutor
from observability.telegram import get_telegram

LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOGS_DIR / "metis.log")],
)
logger = logging.getLogger("metis.main")


class Engine:
    def __init__(self):
        self.store = get_state_store()
        self.bybit = get_bybit_client()
        self.telegram = get_telegram()
        self.specs: dict = {}
        self.executor = None
        self.scheduler = CycleScheduler(on_cycle=self._on_cycle)
        self._first_cycle = True  # the boot fire-on-start cycle gets a distinct notice

    async def start(self):
        logger.info("=" * 56)
        logger.info(f"METIS breakout start  mode={'PAPER' if PAPER_MODE else 'LIVE'}")
        logger.info(f"symbols={TRADING.SYMBOLS}  tf={STRATEGY.INTERVAL}m  "
                    f"risk={RISK.RISK_PER_TRADE*100:.1f}%/trade cap={RISK.MAX_CONCURRENT}")
        logger.info("=" * 56)

        self.specs = await self.bybit.refresh_instrument_specs()

        if PAPER_MODE:
            self.store.equity_init(PAPER_INITIAL_BALANCE_USDT)
            self.executor = PaperExecutor(self.store)
        else:
            eq = await self.bybit.get_wallet_equity() or PAPER_INITIAL_BALANCE_USDT
            self.store.equity_init(eq)
            self.store.equity_set(eq)
            self.executor = LiveExecutor(self.store, self.bybit, self.specs)

        equity, _ = self.store.equity_get()
        await self.scheduler.start()
        try:
            await self.telegram.startup("PAPER" if PAPER_MODE else "LIVE", equity, len(TRADING.SYMBOLS))
        except Exception:
            pass

    async def shutdown(self, reason: str):
        logger.info(f"shutdown: {reason}")
        await self.scheduler.stop()
        try:
            await self.telegram.close()
        except Exception:
            pass
        await close_bybit_client()
        self.store.checkpoint()

    async def _fetch_closed(self, symbol: str) -> list[list]:
        """Closed klines ascending, excluding the still-forming last bar."""
        k = await self.bybit.get_kline(symbol, STRATEGY.INTERVAL, STRATEGY.KLINE_LOOKBACK)
        k = sorted(k, key=lambda r: int(r[0]))
        return k[:-1] if len(k) > 1 else k

    async def _on_cycle(self, cid: str, close_dt: datetime):
        logger.info(f"=== cycle {cid} ===")
        klines = {s: await self._fetch_closed(s) for s in TRADING.SYMBOLS}

        exited, entered = [], []

        # 1) exits — stop-loss (paper books it; live is exchange-held) then trend-end
        for pos in self.store.positions_open():
            sym = pos["symbol"]; k = klines.get(sym)
            if not k:
                continue
            last = k[-1]; hi = float(last[2]); lo = float(last[3]); cl = float(last[4])
            side = pos["side"]; stop = pos["stop_price"]
            hit_sl = (side == "Buy" and lo <= stop) or (side == "Sell" and hi >= stop)
            try:
                if PAPER_MODE and hit_sl:
                    r = await self.executor.close(pos, stop, "STOP_LOSS")
                    exited.append(sym); await self.telegram.exit(sym, "STOP_LOSS", r, _R(pos, stop))
                elif should_exit(side, k):
                    px = cl if PAPER_MODE else (await self.bybit.get_last_price(sym) or cl)
                    r = await self.executor.close(pos, px, "TREND_EXIT")
                    exited.append(sym); await self.telegram.exit(sym, "TREND_EXIT", r, _R(pos, px))
            except Exception:
                logger.exception(f"exit error {sym}")

        # 2) refresh equity (live reads the wallet; paper is already booked)
        if not PAPER_MODE:
            eq = await self.bybit.get_wallet_equity()
            if eq:
                self.store.equity_set(eq)
        equity, hw = self.store.equity_get()

        # 3) new entries — strongest breakouts first, up to the open-slot budget
        rf = self.store.risk_get(); mk = bool(rf.get("manual_kill"))
        day = self.store.daily_pnl_today()["realized_usdt"]
        open_n = self.store.open_count()
        slots = RISK.MAX_CONCURRENT - open_n
        if slots > 0 and not mk:
            cands = []
            for sym in TRADING.SYMBOLS:
                if self.store.has_position(sym):
                    continue
                intent = evaluate(sym, klines.get(sym, []))
                if not intent:
                    continue
                dec = risk_engine.evaluate(
                    intent, equity=equity, high_water=hw, open_count=open_n,
                    daily_realized=day, manual_kill=mk, specs=self.specs)
                if dec.ok:
                    cands.append((intent.adx, dec.order))
            cands.sort(key=lambda x: x[0], reverse=True)
            for _, order in cands[:slots]:
                try:
                    res = await self.executor.open(order)
                    if res.get("ok", True):
                        entered.append(order.symbol)
                        await self.telegram.enter(order.symbol, order.side, order.qty,
                                                  order.reference_price, order.stop_price, order.leverage)
                except Exception:
                    logger.exception(f"entry error {order.symbol}")

        try:
            if self._first_cycle:
                self._first_cycle = False
                await self.telegram.send(
                    f"🔍 <b>시작 점검 완료</b> · {len(TRADING.SYMBOLS)}코인 평가\n"
                    f"진입 {entered or '없음'} · 청산 {exited or '없음'} · 보유 {self.store.open_count()}")
            else:
                await self.telegram.cycle(cid, entered, exited, self.store.open_count())
            if close_dt.hour == CYCLE.DAILY_REPORT_UTC_HOUR:
                d = self.store.daily_pnl_today()
                await self.telegram.daily(equity, d["realized_usdt"], d["n_trades"], self.store.open_count())
        except Exception:
            pass

        klines.clear()
        gc.collect()


def _R(pos: dict, exit_price: float) -> float:
    d = 1 if pos["side"] == "Buy" else -1
    risk = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
    return (d * (exit_price - pos["entry_price"]) * pos["qty"]) / risk if risk > 0 else 0.0


async def _run():
    eng = Engine()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass
    await eng.start()
    await stop.wait()
    await eng.shutdown("signal")


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
