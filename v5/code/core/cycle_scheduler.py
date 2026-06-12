"""Cycle scheduler — anchored 15-minute loop.

Each cycle fires shortly after the 15m bar closes (the buffer is configurable
in ``CYCLE.CYCLE_BUFFER_SEC``). ``cycle_id`` is the bar-close timestamp in UTC
ISO format ("2026-05-28T12:15:00Z"), which keeps cycles idempotent across
restarts: the cycle_lock table sees the same id again and refuses to redo it.

If ``fire_on_start`` is set, on boot the scheduler runs one cycle for the
most recently closed bar so the operator gets a fresh decision immediately
after a restart.
"""
from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from config.settings import CYCLE, TRADING

logger = logging.getLogger("metis.cycle_scheduler")


def next_bar_close_utc(now_utc: datetime, tf_min: int = 15) -> datetime:
    """Return the next bar-close timestamp in UTC (rounded to ``tf_min`` minutes)."""
    epoch_min = int(now_utc.timestamp() // 60)
    rem = epoch_min % tf_min
    base_min = epoch_min - rem
    next_close_min = base_min + tf_min
    return datetime.fromtimestamp(next_close_min * 60, tz=timezone.utc)


def cycle_id_from_close(close_utc: datetime) -> str:
    return close_utc.strftime("%Y-%m-%dT%H:%M:%SZ")


class CycleScheduler:
    """Anchored 15-minute scheduler.

    Calls back per cycle with ``(cycle_id, bar_close_utc)``. ``fire_on_start``
    (default True) triggers one cycle on the most recently closed bar at boot
    so the operator gets a fresh decision after a restart; the cycle_lock
    table prevents that cycle from being redone if the previous process had
    already completed it.
    """

    def __init__(
        self,
        on_cycle: Callable[[str, datetime], "asyncio.Future"],
        fire_on_start: bool = True,
    ):
        self.on_cycle = on_cycle
        self.fire_on_start = fire_on_start
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="cycle_scheduler")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        # Boot path — fire immediately on the most recently closed bar.
        if self.fire_on_start and self._running:
            now = datetime.now(timezone.utc)
            # Round DOWN to the latest closed bar (not up to the next one).
            tf = CYCLE.PRIMARY_TF_MIN
            epoch_min = int(now.timestamp() // 60)
            base_min = epoch_min - (epoch_min % tf)
            last_close = datetime.fromtimestamp(base_min * 60, tz=timezone.utc)
            cid = cycle_id_from_close(last_close)
            logger.info(f"fire-on-start: immediate cycle {cid}")
            try:
                await self.on_cycle(cid, last_close)
            except Exception:
                logger.exception(f"fire-on-start cycle error (cycle={cid})")
            gc.collect()

        while self._running:
            now = datetime.now(timezone.utc)
            close = next_bar_close_utc(now, CYCLE.PRIMARY_TF_MIN)
            target = close + timedelta(seconds=CYCLE.CYCLE_BUFFER_SEC)
            sleep_for = (target - now).total_seconds()
            if sleep_for > 0:
                logger.info(f"next cycle at {target.isoformat()} ({sleep_for:.0f}s)")
                try:
                    await asyncio.sleep(sleep_for)
                except asyncio.CancelledError:
                    return
            if not self._running:
                return
            cid = cycle_id_from_close(close)
            try:
                await self.on_cycle(cid, close)
            except Exception:
                logger.exception(f"cycle_scheduler on_cycle error (cycle={cid})")
            gc.collect()
