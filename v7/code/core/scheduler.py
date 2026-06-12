"""Anchored 4-hour cycle scheduler.

Fires shortly after each 4h bar closes (buffer configurable). cycle_id is the
bar-close UTC ISO timestamp so cycles are idempotent across restarts. On boot,
fire_on_start runs one cycle for the most recently closed bar so the operator
gets a fresh evaluation right after a restart.
"""
from __future__ import annotations

import asyncio
import gc
import logging
from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from config.settings import CYCLE

logger = logging.getLogger("metis.scheduler")


def next_bar_close(now: datetime, tf_min: int) -> datetime:
    epoch_min = int(now.timestamp() // 60)
    base = epoch_min - (epoch_min % tf_min)
    return datetime.fromtimestamp((base + tf_min) * 60, tz=timezone.utc)


def last_bar_close(now: datetime, tf_min: int) -> datetime:
    epoch_min = int(now.timestamp() // 60)
    base = epoch_min - (epoch_min % tf_min)
    return datetime.fromtimestamp(base * 60, tz=timezone.utc)


class CycleScheduler:
    def __init__(self, on_cycle: Callable[[str, datetime], Awaitable[None]], fire_on_start: bool = True):
        self.on_cycle = on_cycle
        self.fire_on_start = fire_on_start
        self._task = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="scheduler")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        tf = CYCLE.PRIMARY_TF_MIN
        if self.fire_on_start and self._running:
            close = last_bar_close(datetime.now(timezone.utc), tf)
            cid = close.strftime("%Y-%m-%dT%H:%M:%SZ")
            logger.info(f"fire-on-start cycle {cid}")
            try:
                await self.on_cycle(cid, close)
            except Exception:
                logger.exception("fire-on-start cycle error")
            gc.collect()

        while self._running:
            now = datetime.now(timezone.utc)
            close = next_bar_close(now, tf)
            target = close + timedelta(seconds=CYCLE.CYCLE_BUFFER_SEC)
            wait = (target - now).total_seconds()
            if wait > 0:
                logger.info(f"next cycle at {target.isoformat()} ({wait:.0f}s)")
                try:
                    await asyncio.sleep(wait)
                except asyncio.CancelledError:
                    return
            if not self._running:
                return
            cid = close.strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                await self.on_cycle(cid, close)
            except Exception:
                logger.exception(f"cycle error {cid}")
            gc.collect()
