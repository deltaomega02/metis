"""Position monitor — supervises the single open position.

Runs on a 15-second polling loop. Responsibilities:

- Enforce the per-position time stop (default 90 min, hard cap 120 min).
- In paper mode, simulate SL/TP fills against the latest mark price.
- In live mode, detect exchange-side closes (native SL/TP fired) and
  reconstruct exit details from the execution list.
- On close: update ``open_position`` state, write the outcome row to
  telemetry, advance ``RiskEngine.on_trade_closed`` (streak/cooldown), and
  notify Telegram.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from ai.prompt_registry import SCHEMA_VERSION
from config.settings import CYCLE, PAPER_MODE, RISK
from core.risk_engine import get_risk_engine
from core.state_store import EventRecord, get_state_store
from core.telemetry_writer import TradeOutcomeLog, get_telemetry
from exchange.bybit_client import get_bybit_client
from exchange.paper_executor import get_paper_executor
from observability.telegram_bot import get_telegram

logger = logging.getLogger("metis.position_monitor")


class PositionMonitor:
    """Polls the active position and finalises it when SL / TP / time stop fires."""

    def __init__(self):
        self.bybit = get_bybit_client()
        self.paper = get_paper_executor() if PAPER_MODE else None
        self.store = get_state_store()
        self.risk = get_risk_engine()
        self.tele = get_telemetry()
        self.telegram = get_telegram()
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._loop(), name="position_monitor")

    async def stop(self):
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self):
        while self._running:
            try:
                await self._tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("position_monitor tick error")
            await asyncio.sleep(CYCLE.POSITION_POLL_INTERVAL_SEC)

    async def _tick(self):
        pos = self.store.snapshot_get("open_position") or {}
        if pos.get("status") != "OPEN":
            return

        symbol = pos["symbol"]
        side = pos["side"]
        entry = float(pos["entry_price"])
        sl = float(pos.get("stop_loss") or 0)
        tp = float(pos.get("take_profit") or 0)
        opened_iso = pos["opened_utc"]
        position_id = pos["position_id"]

        # Latest mark / last-trade price
        try:
            ticker = await self.bybit.get_ticker(symbol)
            mark = float(ticker.get("markPrice") or ticker.get("lastPrice") or 0)
        except Exception as e:
            logger.warning(f"position_monitor ticker fetch failed: {e}")
            return

        if mark <= 0:
            return

        # Time elapsed since entry
        try:
            opened_dt = datetime.fromisoformat(opened_iso.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            opened_dt = datetime.now(timezone.utc)
        now_utc = datetime.now(timezone.utc)
        elapsed_min = (now_utc - opened_dt).total_seconds() / 60.0

        close_reason: Optional[str] = None

        # In paper mode the monitor simulates SL/TP fills; in live mode the
        # exchange-native SL/TP fires directly and we only finalize the local
        # state afterwards (see the ``not PAPER_MODE`` branch below).
        if PAPER_MODE:
            if side == "Buy":
                if sl > 0 and mark <= sl:
                    close_reason = "STOP_LOSS"
                elif tp > 0 and mark >= tp:
                    close_reason = "TAKE_PROFIT"
            else:
                if sl > 0 and mark >= sl:
                    close_reason = "STOP_LOSS"
                elif tp > 0 and mark <= tp:
                    close_reason = "TAKE_PROFIT"

        # No time-stop. Exit only when structural SL or TP is hit (per design — time-stop kills R:R in small-R scalping).

        if close_reason is None:
            # Live mode: if the exchange closed the position (SL/TP hit), we
            # reconstruct fill details from the execution list. Paper mode has
            # already decided via the SL/TP price check above.
            if not PAPER_MODE:
                try:
                    positions = await self.bybit.get_positions(symbol=symbol)
                    still_open = any(float(p.get("size", 0)) > 0 for p in positions)
                    if not still_open:
                        await self._finalize_live_close(symbol, position_id, pos)
                except Exception as e:
                    logger.warning(f"position_monitor live check: {e}")
            return

        # Close
        await self._close(symbol, side, position_id, pos, mark, close_reason)

    async def _close(self, symbol: str, side: str, position_id: str, pos: dict, mark: float, reason: str):
        if PAPER_MODE:
            result = self.paper.close_position(position_id, mark, reason)
        else:
            opposite = "Sell" if side == "Buy" else "Buy"
            qty = float(pos["qty"])
            try:
                await self.bybit.place_order(
                    symbol=symbol, side=opposite, order_type="Market",
                    qty=qty, client_order_id=f"m4-cls-{position_id[:20]}",
                    reduce_only=True, time_in_force="IOC",
                )
            except Exception as e:
                logger.exception(f"live close failed: {e}")
                return
            # finalize via execution
            await self._finalize_live_close(symbol, position_id, pos, override_reason=reason)
            return

        # paper finalization
        won = result.get("won", False)
        realized = float(result.get("realized_pnl_usdt", 0))
        fees = float(result.get("fees_usdt", 0))
        exit_price = float(result.get("exit_price", mark) or mark)

        self.store.snapshot_set("open_position", {"status": "NONE"})
        self.risk.on_trade_closed(realized_usdt=realized, fees_usdt=fees, won=won)
        equity = self.paper.get_equity()
        self.risk.update_equity(equity)

        self._journal("POSITION_CLOSE", symbol,
                      f"reason={reason} pnl={realized:.2f} fees={fees:.2f} won={won} equity={equity:.2f}")
        # ── Telemetry outcome + Telegram alert ──
        await self._record_outcome(pos, exit_price, realized, fees, reason, won)

    async def _finalize_live_close(self, symbol: str, position_id: str, pos: dict,
                                   override_reason: Optional[str] = None):
        # Fetch recent executions for this position
        try:
            execs = await self.bybit.get_executions(symbol=symbol, limit=50)
        except Exception as e:
            logger.warning(f"finalize_live: get_executions failed: {e}")
            execs = []
        # Sum reduce-only sells (or buys for short)
        side = pos["side"]
        opposite = "Sell" if side == "Buy" else "Buy"
        related = [e for e in execs if e.get("side") == opposite]
        qty = sum(float(e.get("execQty", 0)) for e in related)
        if qty <= 0:
            logger.warning(f"finalize_live: no opposite exec found for {symbol}")
            self.store.snapshot_set("open_position", {"status": "NONE"})
            return
        notional = sum(float(e.get("execQty", 0)) * float(e.get("execPrice", 0)) for e in related)
        avg_exit = notional / qty
        fees_close = sum(float(e.get("execFee", 0)) for e in related)
        entry = float(pos["entry_price"])
        pnl_gross = (avg_exit - entry) * qty if side == "Buy" else (entry - avg_exit) * qty
        realized = pnl_gross - fees_close
        won = realized > 0
        reason = override_reason or "EXCHANGE_NATIVE"

        self.store.snapshot_set("open_position", {"status": "NONE"})
        self.risk.on_trade_closed(realized_usdt=realized, fees_usdt=fees_close, won=won)
        self._journal("POSITION_CLOSE", symbol,
                      f"reason={reason} exit={avg_exit:.4f} pnl={realized:.2f} won={won}")
        await self._record_outcome(pos, avg_exit, realized, fees_close, reason, won)

    async def _record_outcome(self, pos: dict, exit_price: float, realized: float, fees: float,
                              reason: str, won: bool):
        """Persist the trade outcome to telemetry and send the close alert."""
        try:
            entry = float(pos.get("entry_price", 0) or 0)
            qty = float(pos.get("qty", 0) or 0)
            side = pos.get("side", "?")
            sl = float(pos.get("stop_loss", 0) or 0)
            tp = float(pos.get("take_profit", 0) or 0)
            sl_dist = abs(entry - sl) if sl > 0 else 0
            # realized R: (exit - entry signed) / sl_dist
            if side == "Buy":
                realized_R = ((exit_price - entry) / sl_dist) if sl_dist > 0 else 0
            else:
                realized_R = ((entry - exit_price) / sl_dist) if sl_dist > 0 else 0
            # time in trade
            time_in_trade = 0.0
            try:
                opened = datetime.fromisoformat((pos.get("opened_utc") or "").replace("Z", "+00:00"))
                time_in_trade = (datetime.now(timezone.utc) - opened).total_seconds() / 60.0
            except Exception:
                pass

            self.tele.log_outcome(TradeOutcomeLog(
                closed_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=pos.get("cycle_id") or "",
                decision_id=pos.get("decision_id"),
                symbol=pos.get("symbol", "?"),
                side=side,
                setup_id=pos.get("setup_id"),
                entry_price=entry,
                exit_price=float(exit_price),
                stop_loss=sl, take_profit=tp,
                qty=qty,
                leverage=int(pos.get("leverage", 1) or 1),
                realized_pnl_usdt=float(realized),
                realized_R=float(realized_R),
                fees_usdt=float(fees),
                funding_usdt=0.0,
                max_adverse_excursion_R=0.0,
                max_favorable_excursion_R=0.0,
                time_in_trade_min=time_in_trade,
                close_reason=reason,
                won=1 if won else 0,
                notes_json=None,
            ))
        except Exception:
            logger.exception("log_outcome failed")

        # Telegram
        try:
            equity_after = self.paper.get_equity() if PAPER_MODE else 0.0
            await self.telegram.notify_close(
                symbol=pos.get("symbol", "?"),
                side=pos.get("side", "?"),
                pnl=realized,
                R=float(realized_R) if 'realized_R' in locals() else 0.0,
                reason=reason,
                equity_after=equity_after,
                won=won,
                leverage=pos.get("leverage"),
            )
        except Exception:
            logger.exception("notify_close failed")

    def _journal(self, kind: str, symbol: str, detail: str):
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=None, kind=kind, symbol=symbol, actor="MONITOR",
                prompt_hash=None, schema_version=SCHEMA_VERSION,
                risk_config_hash=None, payload={"detail": detail},
            ))
        except Exception:
            logger.exception("journal append failed")
