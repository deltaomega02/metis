"""Daily aggregator — closes the trading day and reports.

Runs once per UTC day at 00:01:

- Aggregates the previous day's ``trade_outcomes`` (count, wins, expectancy R,
  profit factor, max drawdown, ECE placeholder, per-setup breakdown).
- Writes the report into ``state_snapshot`` and the event journal.
- Invokes the optional Telegram callback so the operator sees a summary.
"""
from __future__ import annotations

import asyncio
import logging
import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Optional

from ai.prompt_registry import SCHEMA_VERSION
from config.settings import CYCLE
from core.state_store import EventRecord, get_state_store
from core.telemetry_writer import get_telemetry

logger = logging.getLogger("metis.daily_aggregator")


@dataclass
class DailyReport:
    utc_date: str
    n_trades: int
    wins: int
    losses: int
    win_rate: float
    avg_R: float
    avg_pnl: float
    sum_pnl: float
    profit_factor: float
    ECE: float  # rough; only if confidence buckets have ≥30
    setup_breakdown: dict = field(default_factory=dict)


class DailyAggregator:
    """Background task that closes each UTC day and emits a daily report."""

    def __init__(self):
        self.tele = get_telemetry()
        self.store = get_state_store()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._report_callback = None

    def set_report_callback(self, cb):
        self._report_callback = cb

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="daily_aggregator")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            now_utc = datetime.now(timezone.utc)
            # Sleep until the next UTC 00:01 boundary.
            target = now_utc.replace(
                hour=CYCLE.DAILY_AGG_UTC_HOUR,
                minute=CYCLE.DAILY_AGG_UTC_MIN,
                second=0, microsecond=0,
            )
            if target <= now_utc:
                target += timedelta(days=1)
            sleep_sec = max(60, (target - now_utc).total_seconds())
            try:
                await asyncio.sleep(sleep_sec)
                if not self._running:
                    return
                await self.run_once_for(now_utc.date() - timedelta(days=1) if now_utc.hour == 0 else now_utc.date())
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("daily_aggregator error")

    async def run_once_for(self, day):
        """Aggregate outcomes for the given UTC date and emit the report."""
        utc_date_str = day.isoformat() if hasattr(day, "isoformat") else str(day)
        # filter outcomes
        all_outcomes = self.tele.recent_outcomes(limit=1000)
        day_outcomes = []
        for o in all_outcomes:
            try:
                d = datetime.fromisoformat(o["closed_utc"].replace("Z", "+00:00")).date().isoformat()
                if d == utc_date_str:
                    day_outcomes.append(o)
            except Exception:
                continue

        if not day_outcomes:
            report = DailyReport(
                utc_date=utc_date_str, n_trades=0, wins=0, losses=0,
                win_rate=0.0, avg_R=0.0, avg_pnl=0.0, sum_pnl=0.0,
                profit_factor=0.0, ECE=0.0,
            )
        else:
            n = len(day_outcomes)
            wins = sum(1 for o in day_outcomes if o.get("won"))
            losses = n - wins
            Rs = [float(o.get("realized_R") or 0.0) for o in day_outcomes]
            pnls = [float(o.get("realized_pnl_usdt") or 0.0) for o in day_outcomes]
            win_pnls = [p for p in pnls if p > 0]
            loss_pnls = [-p for p in pnls if p < 0]
            pf = (sum(win_pnls) / sum(loss_pnls)) if loss_pnls else float("inf")
            setup_break = defaultdict(lambda: {"n": 0, "wins": 0, "sum_R": 0.0})
            for o in day_outcomes:
                k = o.get("setup_id") or "NONE"
                setup_break[k]["n"] += 1
                setup_break[k]["wins"] += 1 if o.get("won") else 0
                setup_break[k]["sum_R"] += float(o.get("realized_R") or 0.0)
            report = DailyReport(
                utc_date=utc_date_str, n_trades=n, wins=wins, losses=losses,
                win_rate=wins / n,
                avg_R=statistics.mean(Rs) if Rs else 0.0,
                avg_pnl=statistics.mean(pnls) if pnls else 0.0,
                sum_pnl=sum(pnls),
                profit_factor=pf if math.isfinite(pf) else 0.0,
                ECE=0.0,  # ECE is only meaningful once each confidence bucket has 30+ outcomes.
                setup_breakdown=dict(setup_break),
            )

        # persist + journal
        self.store.snapshot_set(f"daily_report:{utc_date_str}", report.__dict__)
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=None, kind="DAILY_REPORT", symbol=None, actor="AGGREGATOR",
                prompt_hash=None, schema_version=SCHEMA_VERSION,
                risk_config_hash=None, payload=report.__dict__,
            ))
        except Exception:
            logger.exception("daily journal failed")

        if self._report_callback is not None:
            try:
                await self._report_callback(report)
            except Exception:
                logger.exception("daily report callback failed")

        return report
