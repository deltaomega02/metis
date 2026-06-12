"""Background watchdogs.

Three independent loops, each writing its result into ``state_snapshot``:

- ``WSWatchdog`` reports per-symbol ticker/orderbook age and a global
  ``any_stale`` flag (key: ``ws_health``).
- ``TimeSyncGuard`` compares local time to Bybit server time, blocking new
  entries when the offset exceeds ``NTP_OFFSET_BLOCK_MS`` (key: ``time_sync``).
- ``HealthCheck`` records RSS, CPU% and free disk; below ``DISK_FREE_MIN_PCT``
  it writes a ``DISK_LOW`` journal entry (key: ``health``).
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime, timezone
from typing import Optional

try:
    import psutil
except ImportError:
    psutil = None

from ai.prompt_registry import SCHEMA_VERSION
from config.settings import CYCLE, RES, TRADING
from core.state_store import EventRecord, get_state_store
from core.stream_worker import StreamWorker
from exchange.bybit_client import get_bybit_client

logger = logging.getLogger("metis.watchdogs")


# ──────────────────────────────────────────────────────────────────────
# WS Watchdog
# ──────────────────────────────────────────────────────────────────────
class WSWatchdog:
    """Periodically inspects StreamWorker freshness per symbol."""

    def __init__(self, stream: StreamWorker, interval_sec: float = 15.0):
        self.stream = stream
        self.interval = interval_sec
        self.store = get_state_store()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="ws_watchdog")

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
            try:
                self._tick()
            except Exception:
                logger.exception("ws_watchdog error")
            await asyncio.sleep(self.interval)

    def _tick(self):
        report = {}
        any_stale = False
        for sym in TRADING.SYMBOLS:
            s = self.stream.summary(sym)
            t_age = s.last_ticker_age_sec
            b_age = s.last_book_age_sec
            stale = (t_age > CYCLE.WS_MAX_STALE_SEC) or (b_age > CYCLE.WS_MAX_STALE_SEC)
            report[sym] = {"ticker_age": t_age, "book_age": b_age, "stale": stale}
            if stale:
                any_stale = True
        self.store.snapshot_set("ws_health", {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "any_stale": any_stale,
            **report,
        })


# ──────────────────────────────────────────────────────────────────────
# Time Sync Guard
# ──────────────────────────────────────────────────────────────────────
class TimeSyncGuard:
    """Tracks the offset between local clock and Bybit server time."""

    def __init__(self, interval_sec: float = 60.0):
        self.interval = interval_sec
        self.bybit = get_bybit_client()
        self.store = get_state_store()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="time_sync_guard")

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
            try:
                await self._tick()
            except Exception:
                logger.exception("time_sync tick error")
            await asyncio.sleep(self.interval)

    async def _tick(self):
        try:
            t_send = int(time.time() * 1000)
            server_ms = await self.bybit.get_server_time()
            t_recv = int(time.time() * 1000)
        except Exception as e:
            self.store.snapshot_set("time_sync", {
                "updated_utc": datetime.now(timezone.utc).isoformat(),
                "offset_ms": None,
                "ok": False,
                "error": str(e)[:100],
            })
            return
        # Server time vs the midpoint of our send/recv timestamps.
        # ``get_server_time`` returns ms (it divides ns internally).
        rtt = t_recv - t_send
        local_mid_ms = (t_send + t_recv) // 2
        offset = local_mid_ms - server_ms

        ok = abs(offset) < CYCLE.NTP_OFFSET_BLOCK_MS
        warn = abs(offset) >= CYCLE.NTP_OFFSET_WARN_MS
        self.store.snapshot_set("time_sync", {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "offset_ms": offset,
            "rtt_ms": rtt,
            "ok": ok,
            "warn": warn,
        })
        if not ok:
            logger.warning(f"time drift {offset}ms — blocking new entries")


# ──────────────────────────────────────────────────────────────────────
# HealthCheck
# ──────────────────────────────────────────────────────────────────────
class HealthCheck:
    """Periodic process / disk health snapshot."""

    def __init__(self, interval_sec: float = 30.0):
        self.interval = interval_sec
        self.store = get_state_store()
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._proc = psutil.Process(os.getpid()) if psutil else None

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="health_check")

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
            try:
                self._tick()
            except Exception:
                logger.exception("health tick error")
            await asyncio.sleep(self.interval)

    def _tick(self):
        mem_mb = 0.0
        cpu_pct = 0.0
        if self._proc is not None:
            try:
                mem_mb = self._proc.memory_info().rss / 1024 / 1024
                cpu_pct = self._proc.cpu_percent(interval=None)
            except Exception:
                pass
        try:
            du = shutil.disk_usage("/")
            disk_free_pct = du.free / du.total
        except Exception:
            disk_free_pct = 1.0

        disk_ok = disk_free_pct >= RES.DISK_FREE_MIN_PCT

        self.store.snapshot_set("health", {
            "updated_utc": datetime.now(timezone.utc).isoformat(),
            "mem_rss_mb": round(mem_mb, 1),
            "cpu_pct": round(cpu_pct, 1),
            "disk_free_pct": round(disk_free_pct, 4),
            "disk_ok": disk_ok,
        })
        if not disk_ok:
            self._journal("DISK_LOW", f"disk_free={disk_free_pct*100:.1f}%")

    def _journal(self, kind: str, detail: str):
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=None, kind=kind, symbol=None, actor="HEALTH",
                prompt_hash=None, schema_version=SCHEMA_VERSION,
                risk_config_hash=None, payload={"detail": detail},
            ))
        except Exception:
            logger.exception("journal append failed")
