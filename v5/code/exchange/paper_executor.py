"""Paper-trading executor — simulates fills against live mainnet prices.

Used when ``PAPER_MODE=true``. Never calls signed Bybit endpoints; instead it
mirrors the live executor's interface but updates an in-process equity balance
and SQLite tables. Useful for shadow-trading the live engine in parallel.

- ``open_position``: entry filled instantly at ``reference_price`` plus the
  configured slippage assumption; entry fee is recorded on the row but only
  realised on close so equity == realized PnL.
- ``set_protection``: stores SL/TP on the paper row (no exchange call).
- ``close_position``: invoked by PositionMonitor when mark hits SL/TP/time-stop;
  pays exit slippage + entry+exit fees, then adjusts equity.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import RISK, STATE_DB_PATH, TRADING, PAPER_INITIAL_BALANCE_USDT

logger = logging.getLogger("metis.paper_executor")


_DDL = """
CREATE TABLE IF NOT EXISTS paper_balance (
    key            TEXT PRIMARY KEY,
    equity_usdt    REAL NOT NULL,
    updated_utc    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id      TEXT PRIMARY KEY,
    decision_id      TEXT,
    symbol           TEXT NOT NULL,
    side             TEXT NOT NULL,    -- Buy | Sell
    qty              REAL NOT NULL,
    entry_price      REAL NOT NULL,
    stop_loss        REAL,
    take_profit      REAL,
    leverage         INTEGER NOT NULL,
    status           TEXT NOT NULL,    -- OPEN | CLOSED
    opened_utc       TEXT NOT NULL,
    closed_utc       TEXT,
    exit_price       REAL,
    close_reason     TEXT,
    realized_pnl_usdt REAL,
    fees_usdt        REAL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_pp_status ON paper_positions(status);
CREATE INDEX IF NOT EXISTS idx_pp_symbol_status ON paper_positions(symbol, status);
"""


@dataclass
class PaperFill:
    position_id: str
    symbol: str
    side: str
    qty: float
    fill_price: float
    fees_usdt: float


class PaperExecutor:
    _instance: Optional["PaperExecutor"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *a, **kw):
        if cls._instance is None:
            with cls._instance_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[Path] = None):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.db_path = db_path or STATE_DB_PATH
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None, timeout=15.0
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        self._ensure_balance()

    @contextlib.contextmanager
    def _txn(self):
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.cursor()
            yield cur
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    def _ensure_balance(self):
        with self._lock:
            row = self._conn.execute(
                "SELECT equity_usdt FROM paper_balance WHERE key = 'main'"
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO paper_balance (key, equity_usdt, updated_utc) VALUES (?, ?, ?)",
                    ("main", PAPER_INITIAL_BALANCE_USDT, datetime.now(timezone.utc).isoformat()),
                )

    # ── balance ──
    def get_equity(self) -> float:
        with self._lock:
            row = self._conn.execute(
                "SELECT equity_usdt FROM paper_balance WHERE key = 'main'"
            ).fetchone()
            return float(row["equity_usdt"]) if row else 0.0

    def _add_to_equity(self, delta: float):
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE paper_balance SET equity_usdt = equity_usdt + ?, updated_utc = ? WHERE key = 'main'",
                (delta, datetime.now(timezone.utc).isoformat()),
            )

    # ── entry ──
    def open_position(
        self,
        position_id: str,
        decision_id: str,
        symbol: str,
        side: str,
        qty: float,
        reference_price: float,
        leverage: int,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None,
    ) -> PaperFill:
        """Simulate a market entry. Applies the configured slippage to the reference price."""
        slip = RISK.SLIPPAGE_BPS_ASSUMED / 10000.0
        if side == "Buy":
            fill_price = reference_price * (1 + slip)
        else:
            fill_price = reference_price * (1 - slip)
        notional = qty * fill_price
        fees = notional * RISK.TAKER_FEE_PCT

        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                INSERT INTO paper_positions
                  (position_id, decision_id, symbol, side, qty, entry_price,
                   stop_loss, take_profit, leverage, status, opened_utc, fees_usdt)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'OPEN', ?, ?)
                """,
                (position_id, decision_id, symbol, side, qty, fill_price,
                 stop_loss, take_profit, leverage, now, fees),
            )
        # Entry fee is recorded but not deducted from equity until close, so
        # the equity delta always equals the row's realized PnL.
        return PaperFill(position_id=position_id, symbol=symbol, side=side,
                         qty=qty, fill_price=fill_price, fees_usdt=fees)

    def set_protection(self, position_id: str, stop_loss: Optional[float], take_profit: Optional[float]):
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE paper_positions SET stop_loss = ?, take_profit = ? WHERE position_id = ? AND status = 'OPEN'",
                (stop_loss, take_profit, position_id),
            )

    def get_open_position(self, symbol: Optional[str] = None) -> Optional[dict]:
        with self._lock:
            if symbol:
                row = self._conn.execute(
                    "SELECT * FROM paper_positions WHERE status = 'OPEN' AND symbol = ? LIMIT 1",
                    (symbol,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM paper_positions WHERE status = 'OPEN' LIMIT 1"
                ).fetchone()
        return dict(row) if row else None

    # ── close (invoked by PositionMonitor on SL/TP/time-stop) ──
    def close_position(
        self,
        position_id: str,
        exit_price: float,
        close_reason: str,
    ) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM paper_positions WHERE position_id = ? AND status = 'OPEN'",
                (position_id,),
            ).fetchone()
            if row is None:
                return {"closed": False, "reason": "not_open"}

        pos = dict(row)
        slip = RISK.SLIPPAGE_BPS_ASSUMED / 10000.0
        if pos["side"] == "Buy":
            fill_price = exit_price * (1 - slip)
            pnl = (fill_price - pos["entry_price"]) * pos["qty"]
        else:
            fill_price = exit_price * (1 + slip)
            pnl = (pos["entry_price"] - fill_price) * pos["qty"]

        notional = pos["qty"] * fill_price
        close_fee = notional * RISK.TAKER_FEE_PCT
        entry_fee = pos.get("fees_usdt") or 0.0
        total_fees = entry_fee + close_fee
        # Net realized = gross PnL - (entry + close fees). Equity is moved by
        # this amount only, so equity_delta == realized for the trade row.
        realized = pnl - total_fees

        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                UPDATE paper_positions
                SET status = 'CLOSED', closed_utc = ?, exit_price = ?,
                    close_reason = ?, realized_pnl_usdt = ?, fees_usdt = ?
                WHERE position_id = ?
                """,
                (now, fill_price, close_reason, realized, total_fees, position_id),
            )
        self._add_to_equity(realized)
        return {
            "closed": True,
            "position_id": position_id,
            "exit_price": fill_price,
            "realized_pnl_usdt": realized,
            "fees_usdt": total_fees,
            "gross_pnl_usdt": pnl,
            "close_reason": close_reason,
            "won": realized > 0,
        }


def get_paper_executor() -> PaperExecutor:
    return PaperExecutor()
