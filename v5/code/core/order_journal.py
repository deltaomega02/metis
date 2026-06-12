"""Order journal / idempotency ledger.

Every order request transitions through:

    PREPARED → SUBMITTED → ACKED → (PARTIAL → FILLED | CANCELLED | REJECTED)

Key design points:

- ``client_order_id`` is derived deterministically from ``(decision_id, purpose)``
  via SHA-256, so the same logical request always maps to the same exchange
  id. After a crash a restart can look up the exchange state by the same id
  rather than blindly resubmitting.
- Never resubmit blindly. On boot the reconciler walks ``find_stale_prepared``
  and queries the exchange to decide each stale row's fate.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

from config.settings import STATE_DB_PATH

logger = logging.getLogger("metis.order_journal")


class OrderState(str, Enum):
    PREPARED = "PREPARED"
    SUBMITTED = "SUBMITTED"
    ACKED = "ACKED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"


_DDL = """
CREATE TABLE IF NOT EXISTS order_journal (
    client_order_id   TEXT PRIMARY KEY,
    decision_id       TEXT NOT NULL,
    cycle_id          TEXT,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,    -- Buy | Sell
    order_type        TEXT NOT NULL,    -- Market | Limit | StopMarket | TPSL_REDUCE
    qty               REAL NOT NULL,
    price             REAL,
    reduce_only       INTEGER DEFAULT 0,
    state             TEXT NOT NULL,
    submitted_utc     TEXT,
    acked_utc         TEXT,
    filled_utc        TEXT,
    exchange_order_id TEXT,
    filled_qty        REAL DEFAULT 0,
    avg_fill_price    REAL,
    fees_usdt         REAL DEFAULT 0,
    error             TEXT,
    notes_json        TEXT,
    created_utc       TEXT NOT NULL,
    updated_utc       TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_oj_decision ON order_journal(decision_id);
CREATE INDEX IF NOT EXISTS idx_oj_state ON order_journal(state);
CREATE INDEX IF NOT EXISTS idx_oj_cycle ON order_journal(cycle_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_oj_exchange ON order_journal(exchange_order_id) WHERE exchange_order_id IS NOT NULL;
"""


@dataclass
class OrderIntent:
    decision_id: str
    cycle_id: Optional[str]
    symbol: str
    side: str  # Buy | Sell
    order_type: str  # Market | Limit | TPSL_REDUCE
    qty: float
    price: Optional[float]
    reduce_only: bool
    purpose: str  # ENTRY | TP | SL | EMERGENCY_CLOSE
    notes: dict


def make_client_order_id(decision_id: str, purpose: str) -> str:
    """Deterministic ``client_order_id`` from ``(decision_id, purpose)``.

    Same decision + same purpose → same id, so retries are idempotent. The
    format is ``m4-{purpose_short}-{hash16}`` (≤36 chars, the Bybit limit).
    """
    h = hashlib.sha256(f"{decision_id}|{purpose}".encode("utf-8")).hexdigest()[:16]
    short = {
        "ENTRY": "ent",
        "TP": "tp_",
        "SL": "sl_",
        "EMERGENCY_CLOSE": "emc",
        "REDUCE": "red",
    }.get(purpose, "ord")
    return f"m4-{short}-{h}"


class OrderJournal:
    """SQLite-backed order ledger. Shares the state.db file with StateStore."""

    _instance: Optional["OrderJournal"] = None
    _instance_lock = threading.Lock()

    def __new__(cls, *args, **kwargs):
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
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,
            timeout=15.0,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)

    def close(self):
        with self._lock:
            if self._conn:
                self._conn.close()
                self._conn = None

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

    # ── PREPARE ──
    def prepare(self, intent: OrderIntent) -> str:
        """Insert a PREPARED row. Idempotent — same (decision_id, purpose) reuses the row."""
        coid = make_client_order_id(intent.decision_id, intent.purpose)
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            existing = cur.execute(
                "SELECT client_order_id, state FROM order_journal WHERE client_order_id = ?",
                (coid,),
            ).fetchone()
            if existing:
                logger.warning(
                    f"prepare(): client_order_id {coid} already exists in state={existing['state']}"
                )
                return coid
            cur.execute(
                """
                INSERT INTO order_journal
                  (client_order_id, decision_id, cycle_id, symbol, side, order_type,
                   qty, price, reduce_only, state, notes_json, created_utc, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    coid,
                    intent.decision_id,
                    intent.cycle_id,
                    intent.symbol,
                    intent.side,
                    intent.order_type,
                    intent.qty,
                    intent.price,
                    1 if intent.reduce_only else 0,
                    OrderState.PREPARED.value,
                    json.dumps({"purpose": intent.purpose, **intent.notes}, ensure_ascii=False, default=str),
                    now,
                    now,
                ),
            )
        logger.info(f"prepare(): {coid} {intent.symbol} {intent.side} {intent.qty} ({intent.purpose})")
        return coid

    # ── state transitions ──
    def mark_submitted(self, client_order_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE order_journal SET state = ?, submitted_utc = ?, updated_utc = ? "
                "WHERE client_order_id = ? AND state = ?",
                (OrderState.SUBMITTED.value, now, now, client_order_id, OrderState.PREPARED.value),
            )
            if cur.rowcount == 0:
                logger.warning(f"mark_submitted: {client_order_id} not in PREPARED state")

    def mark_acked(self, client_order_id: str, exchange_order_id: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE order_journal SET state = ?, acked_utc = ?, exchange_order_id = ?, updated_utc = ? "
                "WHERE client_order_id = ? AND state IN (?, ?)",
                (
                    OrderState.ACKED.value,
                    now,
                    exchange_order_id,
                    now,
                    client_order_id,
                    OrderState.SUBMITTED.value,
                    OrderState.PREPARED.value,
                ),
            )

    def mark_filled(
        self,
        client_order_id: str,
        filled_qty: float,
        avg_fill_price: float,
        fees_usdt: float = 0.0,
        partial: bool = False,
    ):
        now = datetime.now(timezone.utc).isoformat()
        new_state = OrderState.PARTIAL.value if partial else OrderState.FILLED.value
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                UPDATE order_journal SET state = ?, filled_qty = ?, avg_fill_price = ?,
                  fees_usdt = ?, filled_utc = ?, updated_utc = ?
                WHERE client_order_id = ?
                """,
                (new_state, filled_qty, avg_fill_price, fees_usdt, now, now, client_order_id),
            )

    def mark_cancelled(self, client_order_id: str, error: Optional[str] = None):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE order_journal SET state = ?, error = ?, updated_utc = ? WHERE client_order_id = ?",
                (OrderState.CANCELLED.value, error, now, client_order_id),
            )

    def mark_rejected(self, client_order_id: str, error: str):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE order_journal SET state = ?, error = ?, updated_utc = ? WHERE client_order_id = ?",
                (OrderState.REJECTED.value, error, now, client_order_id),
            )

    # ── queries ──
    def get(self, client_order_id: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM order_journal WHERE client_order_id = ?", (client_order_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_by_decision(self, decision_id: str) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM order_journal WHERE decision_id = ? ORDER BY created_utc",
                (decision_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def find_stale_prepared(self, older_than_sec: int = 60) -> list[dict]:
        """Boot recovery: return rows stuck in PREPARED/SUBMITTED past the cutoff so
        the reconciler can reconcile each one against the exchange."""
        cutoff_ts = datetime.now(timezone.utc).timestamp() - older_than_sec
        cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM order_journal WHERE state IN (?, ?) AND created_utc < ?",
                (OrderState.PREPARED.value, OrderState.SUBMITTED.value, cutoff_iso),
            ).fetchall()
        return [dict(r) for r in rows]

    def active_position_orders(self, symbol: Optional[str] = None) -> list[dict]:
        """Return orders in ACKED / PARTIAL state — should mirror the exchange's open orders."""
        sql = "SELECT * FROM order_journal WHERE state IN (?, ?)"
        params: list = [OrderState.ACKED.value, OrderState.PARTIAL.value]
        if symbol:
            sql += " AND symbol = ?"
            params.append(symbol)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_order_journal() -> OrderJournal:
    return OrderJournal()
