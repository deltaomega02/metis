"""Telemetry writer — append-only analytics ledger.

Lives in its own SQLite database (``telemetry.db``) so analytics queries do
not contend with the state store. Two tables:

- ``cycle_events`` — one row per (cycle, symbol), including NO_TRADE; captures
  full prompt / decision metadata, latency, validation outcome and the raw
  feature snapshot JSON for replay.
- ``trade_outcomes`` — one row per closed position with realized PnL, R, MAE,
  MFE, time in trade and exit reason.

The writer never deletes rows; rolling reports are derived by query.
"""
from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import threading
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config.settings import TELEMETRY_DB_PATH

logger = logging.getLogger("metis.telemetry_writer")


_DDL = """
CREATE TABLE IF NOT EXISTS cycle_events (
    cycle_event_id    INTEGER PRIMARY KEY AUTOINCREMENT,
    asof_utc          TEXT NOT NULL,
    cycle_id          TEXT NOT NULL,
    symbol            TEXT NOT NULL,
    mode              TEXT NOT NULL,
    -- versions
    prompt_version    TEXT,
    prompt_hash       TEXT,
    schema_version    TEXT,
    model_id          TEXT,
    risk_config_hash  TEXT,
    -- input
    feature_snapshot_hash TEXT,
    -- gemini
    llm_input_tokens   INTEGER,
    llm_output_tokens  INTEGER,
    llm_latency_ms     INTEGER,
    cache_hit          INTEGER,
    retry_count        INTEGER,
    finish_reason      TEXT,
    llm_full_json      TEXT,
    -- validation
    schema_valid       INTEGER,
    semantic_valid     INTEGER,
    semantic_violations TEXT,
    -- risk
    risk_pass          INTEGER,
    risk_veto_reason   TEXT,
    -- decision
    decision           TEXT,
    setup_id           TEXT,
    direction          TEXT,
    confidence         REAL,
    confidence_bucket  TEXT,
    regime             TEXT,
    volatility_regime  TEXT,
    critical_rejects   TEXT,
    supervisor_action  TEXT,
    -- execution
    order_decision     TEXT,
    order_intent_id    TEXT,
    fill_price         REAL,
    fill_qty           REAL,
    fees_usdt          REAL,
    protection_attached INTEGER,
    protection_delay_sec REAL,
    -- env meta
    ws_stale           INTEGER,
    ntp_offset_ms      INTEGER,
    mem_rss_mb         REAL,
    disk_free_pct      REAL,
    notes_json         TEXT,
    -- full feature snapshot (compressed JSON of all inputs sent to LLM)
    feature_snapshot_json TEXT,
    runtime_controls_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_ce_cycle ON cycle_events(cycle_id);
CREATE INDEX IF NOT EXISTS idx_ce_asof ON cycle_events(asof_utc);
CREATE INDEX IF NOT EXISTS idx_ce_decision ON cycle_events(decision);
CREATE INDEX IF NOT EXISTS idx_ce_symbol ON cycle_events(symbol);

CREATE TABLE IF NOT EXISTS trade_outcomes (
    outcome_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    closed_utc        TEXT NOT NULL,
    cycle_id          TEXT NOT NULL,
    decision_id       TEXT,
    symbol            TEXT NOT NULL,
    side              TEXT NOT NULL,
    setup_id          TEXT,
    entry_price       REAL,
    exit_price        REAL,
    stop_loss         REAL,
    take_profit       REAL,
    qty               REAL,
    leverage          INTEGER,
    realized_pnl_usdt REAL,
    realized_R        REAL,
    fees_usdt         REAL,
    funding_usdt      REAL,
    max_adverse_excursion_R REAL,
    max_favorable_excursion_R REAL,
    time_in_trade_min REAL,
    close_reason      TEXT,
    won               INTEGER,
    notes_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_to_closed ON trade_outcomes(closed_utc);
CREATE INDEX IF NOT EXISTS idx_to_symbol ON trade_outcomes(symbol);
CREATE INDEX IF NOT EXISTS idx_to_setup ON trade_outcomes(setup_id);
"""


def confidence_bucket(c: float) -> str:
    if c < 0.60:
        return "<0.60"
    if c < 0.70:
        return "0.60-0.70"
    if c < 0.80:
        return "0.70-0.80"
    return "0.80+"


@dataclass
class CycleEventLog:
    asof_utc: str
    cycle_id: str
    symbol: str
    mode: str
    prompt_version: Optional[str] = None
    prompt_hash: Optional[str] = None
    schema_version: Optional[str] = None
    model_id: Optional[str] = None
    risk_config_hash: Optional[str] = None
    feature_snapshot_hash: Optional[str] = None
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_latency_ms: int = 0
    cache_hit: int = 0
    retry_count: int = 0
    finish_reason: Optional[str] = None
    llm_full_json: Optional[str] = None
    schema_valid: int = 0
    semantic_valid: int = 0
    semantic_violations: Optional[str] = None
    risk_pass: int = 0
    risk_veto_reason: Optional[str] = None
    decision: Optional[str] = None
    setup_id: Optional[str] = None
    direction: Optional[str] = None
    confidence: Optional[float] = None
    confidence_bucket: Optional[str] = None
    regime: Optional[str] = None
    volatility_regime: Optional[str] = None
    critical_rejects: Optional[str] = None
    supervisor_action: Optional[str] = None
    order_decision: Optional[str] = None
    order_intent_id: Optional[str] = None
    fill_price: Optional[float] = None
    fill_qty: Optional[float] = None
    fees_usdt: Optional[float] = None
    protection_attached: int = 0
    protection_delay_sec: Optional[float] = None
    ws_stale: int = 0
    ntp_offset_ms: Optional[int] = None
    mem_rss_mb: Optional[float] = None
    disk_free_pct: Optional[float] = None
    notes_json: Optional[str] = None
    feature_snapshot_json: Optional[str] = None
    runtime_controls_json: Optional[str] = None


@dataclass
class TradeOutcomeLog:
    closed_utc: str
    cycle_id: str
    decision_id: Optional[str]
    symbol: str
    side: str
    setup_id: Optional[str]
    entry_price: float
    exit_price: float
    stop_loss: float
    take_profit: float
    qty: float
    leverage: int
    realized_pnl_usdt: float
    realized_R: float
    fees_usdt: float
    funding_usdt: float
    max_adverse_excursion_R: float
    max_favorable_excursion_R: float
    time_in_trade_min: float
    close_reason: str
    won: int
    notes_json: Optional[str] = None


class TelemetryWriter:
    """Append-only writer for cycle events and closed-trade outcomes."""

    _instance: Optional["TelemetryWriter"] = None
    _lock = threading.Lock()

    def __new__(cls, *a, **kw):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self, db_path: Optional[Path] = None):
        if getattr(self, "_initialized", False):
            return
        self._initialized = True
        self.db_path = db_path or TELEMETRY_DB_PATH
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._txn_lock = threading.RLock()
        self._conn = sqlite3.connect(
            str(self.db_path), check_same_thread=False, isolation_level=None, timeout=15.0,
        )
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_DDL)
        # Idempotent migration: older telemetry.db files predate these columns,
        # so add them in place rather than forcing a rebuild.
        for col in ("feature_snapshot_json TEXT", "runtime_controls_json TEXT"):
            cname = col.split()[0]
            try:
                self._conn.execute(f"ALTER TABLE cycle_events ADD COLUMN {col}")
            except sqlite3.OperationalError:
                pass  # already exists

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

    def log_cycle(self, ev: CycleEventLog) -> int:
        if ev.confidence is not None and ev.confidence_bucket is None:
            ev.confidence_bucket = confidence_bucket(ev.confidence)
        d = asdict(ev)
        cols = list(d.keys())
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO cycle_events ({','.join(cols)}) VALUES ({placeholders})"
        with self._txn_lock, self._txn() as cur:
            cur.execute(sql, [d[k] for k in cols])
            return cur.lastrowid

    def log_outcome(self, out: TradeOutcomeLog) -> int:
        d = asdict(out)
        cols = list(d.keys())
        placeholders = ",".join("?" for _ in cols)
        sql = f"INSERT INTO trade_outcomes ({','.join(cols)}) VALUES ({placeholders})"
        with self._txn_lock, self._txn() as cur:
            cur.execute(sql, [d[k] for k in cols])
            return cur.lastrowid

    def recent_cycles(self, limit: int = 20) -> list[dict]:
        with self._txn_lock:
            rows = self._conn.execute(
                "SELECT * FROM cycle_events ORDER BY cycle_event_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_outcomes(self, limit: int = 100) -> list[dict]:
        with self._txn_lock:
            rows = self._conn.execute(
                "SELECT * FROM trade_outcomes ORDER BY outcome_id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    def setup_stats(self, days: int = 30) -> list[dict]:
        since = (datetime.now(timezone.utc).timestamp() - days * 86400)
        since_iso = datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
        with self._txn_lock:
            rows = self._conn.execute("""
                SELECT setup_id,
                       COUNT(*) AS n,
                       SUM(won) AS wins,
                       AVG(realized_R) AS avg_R,
                       AVG(realized_pnl_usdt) AS avg_pnl,
                       SUM(realized_pnl_usdt) AS sum_pnl
                FROM trade_outcomes
                WHERE closed_utc >= ?
                GROUP BY setup_id
            """, (since_iso,)).fetchall()
        return [dict(r) for r in rows]


def get_telemetry() -> TelemetryWriter:
    return TelemetryWriter()
