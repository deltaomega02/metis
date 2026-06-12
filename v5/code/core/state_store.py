"""Durable state store backed by SQLite WAL.

Event-sourced design:

- ``event_journal`` is the append-only source of truth (decisions, fills,
  vetoes, kills, reconciler events, ...).
- ``state_snapshot`` is a derived key/value table for fast lookups (open
  position, ws_health, time_sync, ...); it can be rebuilt from the journal.
- ``risk_state``, ``cycle_lock``, ``daily_pnl``, ``equity_marker`` and
  ``version_registry`` carry purpose-specific durable state.

The store is a process-wide singleton with a single connection. WAL mode
allows concurrent readers while writes are serialized through ``_txn``.
Memory tuning (4 MB cache, NORMAL sync, MEMORY temp store) is chosen for
small VMs.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from config.settings import STATE_DB_PATH, RES

logger = logging.getLogger("metis.state_store")


# ──────────────────────────────────────────────────────────────────────
# Schema migrations — bumped per breaking change
# ──────────────────────────────────────────────────────────────────────
SCHEMA_VERSION = 1

_MIGRATIONS = [
    # v1 — initial
    """
    CREATE TABLE IF NOT EXISTS schema_meta (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS event_journal (
        seq           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts_utc        TEXT NOT NULL,
        cycle_id      TEXT,
        kind          TEXT NOT NULL,      -- e.g. DECISION, ORDER_INTENT, ORDER_ACK, FILL, RISK_VETO, KILL
        symbol        TEXT,
        actor         TEXT,               -- LLM | RISK_ENGINE | EXECUTOR | RECONCILER | MONITOR | MANUAL
        prompt_hash   TEXT,
        schema_version TEXT,
        risk_config_hash TEXT,
        payload_json  TEXT NOT NULL,      -- JSON blob (decision, fill, etc.)
        notes         TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_journal_ts ON event_journal(ts_utc);
    CREATE INDEX IF NOT EXISTS idx_journal_cycle ON event_journal(cycle_id);
    CREATE INDEX IF NOT EXISTS idx_journal_kind ON event_journal(kind);

    CREATE TABLE IF NOT EXISTS state_snapshot (
        key           TEXT PRIMARY KEY,
        value_json    TEXT NOT NULL,
        updated_utc   TEXT NOT NULL
    );

    -- Cooldown / kill state (durable across restart)
    CREATE TABLE IF NOT EXISTS risk_state (
        key                TEXT PRIMARY KEY,
        cooldown_until_utc TEXT,
        kill_until_utc     TEXT,
        loss_streak        INTEGER DEFAULT 0,
        manual_kill        INTEGER DEFAULT 0,
        reason             TEXT,
        updated_utc        TEXT NOT NULL
    );

    -- Cycle lock (CycleScheduler/CycleLock)
    CREATE TABLE IF NOT EXISTS cycle_lock (
        cycle_id   TEXT PRIMARY KEY,
        symbol     TEXT NOT NULL,
        started_utc TEXT NOT NULL,
        status     TEXT NOT NULL  -- IN_PROGRESS | COMPLETED | ABORTED
    );

    -- Daily PnL bucket
    CREATE TABLE IF NOT EXISTS daily_pnl (
        utc_date         TEXT PRIMARY KEY,
        realized_usdt    REAL DEFAULT 0,
        fees_usdt        REAL DEFAULT 0,
        funding_usdt     REAL DEFAULT 0,
        n_trades         INTEGER DEFAULT 0,
        n_wins           INTEGER DEFAULT 0,
        n_losses         INTEGER DEFAULT 0,
        max_dd_pct       REAL DEFAULT 0,
        kill_triggered   INTEGER DEFAULT 0
    );

    -- Equity high-water (for max drawdown calc)
    CREATE TABLE IF NOT EXISTS equity_marker (
        ts_utc          TEXT PRIMARY KEY,
        equity_usdt     REAL NOT NULL,
        high_water_usdt REAL NOT NULL
    );

    -- Prompt / model / risk_config / schema registry — every active version
    -- is hashed so journal rows can be linked back to exact source bytes.
    CREATE TABLE IF NOT EXISTS version_registry (
        kind            TEXT NOT NULL,     -- prompt | schema | model | risk_config
        version         TEXT NOT NULL,
        content_hash    TEXT NOT NULL,
        active          INTEGER DEFAULT 0,
        created_utc     TEXT NOT NULL,
        promoted_utc    TEXT,
        notes           TEXT,
        PRIMARY KEY (kind, version)
    );
    CREATE INDEX IF NOT EXISTS idx_registry_active ON version_registry(kind, active);
    """,
]


@dataclass(frozen=True)
class EventRecord:
    ts_utc: str
    cycle_id: Optional[str]
    kind: str
    symbol: Optional[str]
    actor: str
    prompt_hash: Optional[str]
    schema_version: Optional[str]
    risk_config_hash: Optional[str]
    payload: dict
    notes: Optional[str] = None


class StateStore:
    """Thread-safe SQLite WAL state store. One instance per process."""

    _instance: Optional["StateStore"] = None
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
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        # Single shared connection — WAL allows concurrent reads, and writes are
        # serialised through _lock + BEGIN IMMEDIATE. check_same_thread=False
        # lets asyncio + the threadpool both reach the connection.
        self._conn = sqlite3.connect(
            str(self.db_path),
            check_same_thread=False,
            isolation_level=None,  # autocommit mode; we BEGIN explicitly
            timeout=15.0,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = NORMAL")
        self._conn.execute(f"PRAGMA cache_size = -{RES.SQLITE_CACHE_KB}")
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA temp_store = MEMORY")
        self._conn.row_factory = sqlite3.Row

        self._migrate()
        logger.info(f"StateStore opened: {self.db_path} (schema v{SCHEMA_VERSION})")

    # ── lifecycle ──
    def close(self):
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(Exception):
                    self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                self._conn.close()
                self._conn = None

    def integrity_check(self) -> bool:
        with self._lock:
            cur = self._conn.execute("PRAGMA integrity_check")
            row = cur.fetchone()
            ok = row is not None and row[0] == "ok"
            if not ok:
                logger.error(f"SQLite integrity_check failed: {row[0] if row else 'no result'}")
            return ok

    def wal_checkpoint(self):
        with self._lock:
            with contextlib.suppress(Exception):
                self._conn.execute("PRAGMA wal_checkpoint(PASSIVE)")

    # ── migration ──
    def _migrate(self):
        # executescript commits internally, so we must not wrap it in BEGIN IMMEDIATE.
        with self._lock:
            for ddl in _MIGRATIONS:
                self._conn.executescript(ddl)
            self._conn.execute(
                "INSERT OR REPLACE INTO schema_meta (key, value) VALUES (?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    # ── transactions ──
    @contextlib.contextmanager
    def _txn(self):
        """Open a write transaction with BEGIN IMMEDIATE so writes serialize cleanly."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            cur = self._conn.cursor()
            yield cur
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise

    # ── event journal (append-only) ──
    def journal_append(self, rec: EventRecord) -> int:
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                INSERT INTO event_journal
                  (ts_utc, cycle_id, kind, symbol, actor, prompt_hash,
                   schema_version, risk_config_hash, payload_json, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rec.ts_utc,
                    rec.cycle_id,
                    rec.kind,
                    rec.symbol,
                    rec.actor,
                    rec.prompt_hash,
                    rec.schema_version,
                    rec.risk_config_hash,
                    json.dumps(rec.payload, ensure_ascii=False, default=str, separators=(",", ":")),
                    rec.notes,
                ),
            )
            return cur.lastrowid

    def journal_query(
        self,
        kind: Optional[str] = None,
        cycle_id: Optional[str] = None,
        since_utc: Optional[str] = None,
        limit: int = 100,
    ) -> list[dict]:
        clauses, params = [], []
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        if since_utc:
            clauses.append("ts_utc >= ?")
            params.append(since_utc)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        sql = f"SELECT * FROM event_journal{where} ORDER BY seq DESC LIMIT ?"
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    # ── state_snapshot KV ──
    def snapshot_get(self, key: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT value_json FROM state_snapshot WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return None
        return json.loads(row["value_json"])

    def snapshot_set(self, key: str, value: dict):
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                INSERT INTO state_snapshot (key, value_json, updated_utc)
                VALUES (?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, updated_utc=excluded.updated_utc
                """,
                (key, json.dumps(value, ensure_ascii=False, default=str), now),
            )

    # ── cycle lock ──
    def cycle_lock_acquire(self, cycle_id: str, symbol: str) -> bool:
        """Acquire the per-cycle lock.

        Reuses the row if the previous status was ABORTED or COMPLETED, and
        returns False only when a concurrent instance is already IN_PROGRESS.
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            row = cur.execute(
                "SELECT status FROM cycle_lock WHERE cycle_id = ?", (cycle_id,)
            ).fetchone()
            if row and row["status"] == "IN_PROGRESS":
                return False  # another worker is in the cycle — back off
            # New row, or previous attempt finished — overwrite and proceed.
            cur.execute(
                "INSERT OR REPLACE INTO cycle_lock (cycle_id, symbol, started_utc, status) "
                "VALUES (?, ?, ?, ?)",
                (cycle_id, symbol, now, "IN_PROGRESS"),
            )
            return True

    def cycle_lock_release(self, cycle_id: str, status: str = "COMPLETED"):
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE cycle_lock SET status = ? WHERE cycle_id = ?",
                (status, cycle_id),
            )

    def cycle_lock_abort_stale(self, max_age_sec: int = 300):
        """Boot recovery: mark IN_PROGRESS locks older than max_age as ABORTED.

        Called by the reconciler on startup so a crashed previous process
        does not permanently block new cycles.
        """
        cutoff_ts = datetime.now(timezone.utc).timestamp() - max_age_sec
        cutoff_iso = datetime.fromtimestamp(cutoff_ts, tz=timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            cur.execute(
                "UPDATE cycle_lock SET status = 'ABORTED' WHERE status = 'IN_PROGRESS' AND started_utc < ?",
                (cutoff_iso,),
            )

    # ── risk state ──
    def risk_get(self, key: str = "global") -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM risk_state WHERE key = ?", (key,)
            ).fetchone()
        if row is None:
            return {
                "key": key,
                "cooldown_until_utc": None,
                "kill_until_utc": None,
                "loss_streak": 0,
                "manual_kill": 0,
                "reason": None,
                "updated_utc": None,
            }
        return dict(row)

    def risk_set(
        self,
        key: str = "global",
        cooldown_until_utc: Optional[str] = None,
        kill_until_utc: Optional[str] = None,
        loss_streak: Optional[int] = None,
        manual_kill: Optional[int] = None,
        reason: Optional[str] = None,
    ):
        now = datetime.now(timezone.utc).isoformat()
        current = self.risk_get(key)
        merged = {
            "cooldown_until_utc": cooldown_until_utc if cooldown_until_utc is not None else current.get("cooldown_until_utc"),
            "kill_until_utc": kill_until_utc if kill_until_utc is not None else current.get("kill_until_utc"),
            "loss_streak": loss_streak if loss_streak is not None else current.get("loss_streak", 0),
            "manual_kill": manual_kill if manual_kill is not None else current.get("manual_kill", 0),
            "reason": reason if reason is not None else current.get("reason"),
        }
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                INSERT INTO risk_state (key, cooldown_until_utc, kill_until_utc,
                  loss_streak, manual_kill, reason, updated_utc)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                  cooldown_until_utc=excluded.cooldown_until_utc,
                  kill_until_utc=excluded.kill_until_utc,
                  loss_streak=excluded.loss_streak,
                  manual_kill=excluded.manual_kill,
                  reason=excluded.reason,
                  updated_utc=excluded.updated_utc
                """,
                (
                    key,
                    merged["cooldown_until_utc"],
                    merged["kill_until_utc"],
                    merged["loss_streak"],
                    merged["manual_kill"],
                    merged["reason"],
                    now,
                ),
            )

    # ── daily pnl ──
    def daily_pnl_get(self, utc_date: str) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM daily_pnl WHERE utc_date = ?", (utc_date,)
            ).fetchone()
        return dict(row) if row else {
            "utc_date": utc_date,
            "realized_usdt": 0.0,
            "fees_usdt": 0.0,
            "funding_usdt": 0.0,
            "n_trades": 0,
            "n_wins": 0,
            "n_losses": 0,
            "max_dd_pct": 0.0,
            "kill_triggered": 0,
        }

    def daily_pnl_increment(
        self,
        utc_date: str,
        realized_delta: float = 0.0,
        fees_delta: float = 0.0,
        funding_delta: float = 0.0,
        trade_delta: int = 0,
        win_delta: int = 0,
        loss_delta: int = 0,
        max_dd_pct: Optional[float] = None,
        kill_triggered: Optional[int] = None,
    ):
        with self._lock, self._txn() as cur:
            cur.execute(
                """
                INSERT INTO daily_pnl (utc_date, realized_usdt, fees_usdt, funding_usdt,
                  n_trades, n_wins, n_losses, max_dd_pct, kill_triggered)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(utc_date) DO UPDATE SET
                  realized_usdt = realized_usdt + excluded.realized_usdt,
                  fees_usdt = fees_usdt + excluded.fees_usdt,
                  funding_usdt = funding_usdt + excluded.funding_usdt,
                  n_trades = n_trades + excluded.n_trades,
                  n_wins = n_wins + excluded.n_wins,
                  n_losses = n_losses + excluded.n_losses,
                  max_dd_pct = MAX(max_dd_pct, COALESCE(excluded.max_dd_pct, 0)),
                  kill_triggered = MAX(kill_triggered, COALESCE(excluded.kill_triggered, 0))
                """,
                (
                    utc_date,
                    realized_delta,
                    fees_delta,
                    funding_delta,
                    trade_delta,
                    win_delta,
                    loss_delta,
                    max_dd_pct or 0.0,
                    kill_triggered or 0,
                ),
            )

    # ── equity ──
    def equity_record(self, equity_usdt: float):
        with self._lock, self._txn() as cur:
            row = cur.execute(
                "SELECT high_water_usdt FROM equity_marker ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
            prev_hw = row["high_water_usdt"] if row else 0.0
            new_hw = max(prev_hw, equity_usdt)
            ts = datetime.now(timezone.utc).isoformat()
            cur.execute(
                "INSERT INTO equity_marker (ts_utc, equity_usdt, high_water_usdt) VALUES (?, ?, ?)",
                (ts, equity_usdt, new_hw),
            )

    def equity_current(self) -> tuple[float, float]:
        with self._lock:
            row = self._conn.execute(
                "SELECT equity_usdt, high_water_usdt FROM equity_marker ORDER BY ts_utc DESC LIMIT 1"
            ).fetchone()
        if row is None:
            return (0.0, 0.0)
        return (row["equity_usdt"], row["high_water_usdt"])

    # ── version registry ──
    def registry_register(
        self,
        kind: str,
        version: str,
        content: str,
        activate: bool = False,
        notes: Optional[str] = None,
    ) -> str:
        """Insert (or update) a prompt / schema / model / risk_config version.

        ``content`` is hashed with SHA-256 and stored alongside the row. When
        ``activate=True`` any previously-active row of the same kind is cleared
        so only one is marked active at a time. Returns the content hash.
        """
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()[:16]
        now = datetime.now(timezone.utc).isoformat()
        with self._lock, self._txn() as cur:
            if activate:
                cur.execute(
                    "UPDATE version_registry SET active = 0 WHERE kind = ?", (kind,)
                )
            cur.execute(
                """
                INSERT INTO version_registry
                  (kind, version, content_hash, active, created_utc, promoted_utc, notes)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(kind, version) DO UPDATE SET
                  content_hash = excluded.content_hash,
                  active = excluded.active,
                  promoted_utc = CASE WHEN excluded.active = 1 THEN excluded.promoted_utc ELSE promoted_utc END,
                  notes = excluded.notes
                """,
                (
                    kind,
                    version,
                    content_hash,
                    1 if activate else 0,
                    now,
                    now if activate else None,
                    notes,
                ),
            )
        return content_hash

    def registry_active(self, kind: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM version_registry WHERE kind = ? AND active = 1 LIMIT 1",
                (kind,),
            ).fetchone()
        return dict(row) if row else None


# Singleton accessor
def get_state_store() -> StateStore:
    return StateStore()
