"""SQLite-backed state — open positions, equity, daily P&L, risk flags, journal.

Single WAL database (one writer process). Tables are tiny; rows are kept only
as long as needed (open positions one row per symbol, closed trades appended).
All timestamps are UTC ISO strings.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from typing import Optional

from config.settings import RES, STATE_DB_PATH

_SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
  symbol TEXT PRIMARY KEY, side TEXT, qty REAL, entry_price REAL, stop_price REAL,
  leverage INTEGER, opened_utc TEXT, status TEXT,
  entry_order_id TEXT, sl_order_id TEXT, take_profit REAL DEFAULT 0,
  ref_entry REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS equity (
  key TEXT PRIMARY KEY, equity_usdt REAL, high_water_usdt REAL, updated_utc TEXT,
  initial_usdt REAL
);
CREATE TABLE IF NOT EXISTS daily_pnl (
  utc_date TEXT PRIMARY KEY, realized_usdt REAL DEFAULT 0, fees_usdt REAL DEFAULT 0,
  n_trades INTEGER DEFAULT 0, n_wins INTEGER DEFAULT 0, n_losses INTEGER DEFAULT 0,
  kill_triggered INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS risk_flags (
  key TEXT PRIMARY KEY, manual_kill INTEGER DEFAULT 0, reason TEXT, updated_utc TEXT
);
CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, side TEXT, qty REAL,
  entry_price REAL, exit_price REAL, realized_usdt REAL, fees_usdt REAL,
  realized_R REAL, opened_utc TEXT, closed_utc TEXT, close_reason TEXT,
  ref_entry REAL DEFAULT 0, ref_exit REAL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts_utc TEXT, kind TEXT, symbol TEXT, payload TEXT
);
CREATE TABLE IF NOT EXISTS equity_curve (
  ts_utc TEXT PRIMARY KEY, realized_usdt REAL, unrealized_usdt REAL, mark_usdt REAL,
  open_n INTEGER
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Thread-safe-ish wrapper around one SQLite WAL connection."""

    def __init__(self, path=STATE_DB_PATH):
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute(f"PRAGMA cache_size=-{RES.SQLITE_CACHE_KB}")
        self._db.executescript(_SCHEMA)
        # migrate older DBs: add initial_usdt and backfill to the first equity seen
        try:
            self._db.execute("ALTER TABLE equity ADD COLUMN initial_usdt REAL")
        except sqlite3.OperationalError:
            pass
        for tbl, col in (("positions", "take_profit"), ("positions", "ref_entry"),
                         ("trades", "ref_entry"), ("trades", "ref_exit")):
            try:
                self._db.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} REAL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
        self._db.execute("UPDATE equity SET initial_usdt=equity_usdt WHERE initial_usdt IS NULL")
        self._db.commit()

    # ── equity ──
    def equity_init(self, seed: float):
        with self._lock:
            row = self._db.execute("SELECT equity_usdt FROM equity WHERE key='main'").fetchone()
            if row is None:
                self._db.execute(
                    "INSERT INTO equity(key,equity_usdt,high_water_usdt,updated_utc,initial_usdt) VALUES('main',?,?,?,?)",
                    (seed, seed, _now(), seed))
                self._db.commit()

    def equity_get(self) -> tuple[float, float]:
        row = self._db.execute("SELECT equity_usdt,high_water_usdt FROM equity WHERE key='main'").fetchone()
        return (row["equity_usdt"], row["high_water_usdt"]) if row else (0.0, 0.0)

    def equity_initial(self) -> float:
        """Capital captured at first boot — the baseline for cumulative P&L."""
        row = self._db.execute("SELECT initial_usdt FROM equity WHERE key='main'").fetchone()
        return row["initial_usdt"] if row and row["initial_usdt"] is not None else 0.0

    def equity_set(self, equity: float):
        with self._lock:
            eq, hw = self.equity_get()
            hw = max(hw, equity)
            self._db.execute("UPDATE equity SET equity_usdt=?,high_water_usdt=?,updated_utc=? WHERE key='main'",
                             (equity, hw, _now()))
            self._db.commit()

    # ── positions ──
    def position_open(self, **kw):
        kw.setdefault("take_profit", 0.0)
        kw.setdefault("ref_entry", 0.0)
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO positions"
                "(symbol,side,qty,entry_price,stop_price,leverage,opened_utc,status,entry_order_id,sl_order_id,take_profit,ref_entry)"
                " VALUES(:symbol,:side,:qty,:entry_price,:stop_price,:leverage,:opened_utc,'OPEN',:entry_order_id,:sl_order_id,:take_profit,:ref_entry)",
                kw)
            self._db.commit()

    def position_close(self, symbol: str):
        with self._lock:
            self._db.execute("DELETE FROM positions WHERE symbol=?", (symbol,))
            self._db.commit()

    def positions_open(self) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM positions WHERE status='OPEN'")]

    def open_count(self) -> int:
        return self._db.execute("SELECT COUNT(*) n FROM positions WHERE status='OPEN'").fetchone()["n"]

    def has_position(self, symbol: str) -> bool:
        return self._db.execute("SELECT 1 FROM positions WHERE symbol=? AND status='OPEN'", (symbol,)).fetchone() is not None

    # ── trades / daily ──
    def trade_record(self, **kw):
        kw.setdefault("ref_entry", 0.0)
        kw.setdefault("ref_exit", 0.0)
        with self._lock:
            self._db.execute(
                "INSERT INTO trades(symbol,side,qty,entry_price,exit_price,realized_usdt,fees_usdt,"
                "realized_R,opened_utc,closed_utc,close_reason,ref_entry,ref_exit) VALUES(:symbol,:side,:qty,:entry_price,"
                ":exit_price,:realized_usdt,:fees_usdt,:realized_R,:opened_utc,:closed_utc,:close_reason,:ref_entry,:ref_exit)", kw)
            d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            won = 1 if kw["realized_usdt"] > 0 else 0
            self._db.execute(
                "INSERT INTO daily_pnl(utc_date,realized_usdt,fees_usdt,n_trades,n_wins,n_losses) "
                "VALUES(?,?,?,1,?,?) ON CONFLICT(utc_date) DO UPDATE SET "
                "realized_usdt=realized_usdt+excluded.realized_usdt, fees_usdt=fees_usdt+excluded.fees_usdt, "
                "n_trades=n_trades+1, n_wins=n_wins+excluded.n_wins, n_losses=n_losses+excluded.n_losses",
                (d, kw["realized_usdt"], kw["fees_usdt"], won, 1 - won))
            self._db.commit()

    def daily_pnl_today(self) -> dict:
        d = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        row = self._db.execute("SELECT * FROM daily_pnl WHERE utc_date=?", (d,)).fetchone()
        return dict(row) if row else {"realized_usdt": 0.0, "fees_usdt": 0.0, "n_trades": 0}

    def recent_trades(self, limit: int = 20) -> list[dict]:
        return [dict(r) for r in self._db.execute("SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,))]

    # ── risk flags ──
    def risk_get(self) -> dict:
        row = self._db.execute("SELECT * FROM risk_flags WHERE key='global'").fetchone()
        return dict(row) if row else {"manual_kill": 0, "reason": None}

    def set_manual_kill(self, on: bool, reason: str = "manual"):
        with self._lock:
            self._db.execute(
                "INSERT INTO risk_flags(key,manual_kill,reason,updated_utc) VALUES('global',?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET manual_kill=excluded.manual_kill,reason=excluded.reason,updated_utc=excluded.updated_utc",
                (1 if on else 0, reason, _now()))
            self._db.commit()

    # ── journal ──
    def journal(self, kind: str, symbol: str = "", payload: Optional[dict] = None):
        with self._lock:
            self._db.execute("INSERT INTO journal(ts_utc,kind,symbol,payload) VALUES(?,?,?,?)",
                             (_now(), kind, symbol, json.dumps(payload or {}, ensure_ascii=False)))
            self._db.commit()

    # ── equity curve (mark-to-market time series) ──
    def equity_curve_point(self, ts_utc: str, realized: float, unrealized: float, open_n: int):
        """One mark-to-market sample per cycle: realized equity, open-position
        unrealized P&L (from the last closed bar), and their sum. Keyed by cycle
        timestamp so a re-run of the same cycle overwrites rather than duplicates."""
        with self._lock:
            self._db.execute(
                "INSERT OR REPLACE INTO equity_curve(ts_utc,realized_usdt,unrealized_usdt,mark_usdt,open_n)"
                " VALUES(?,?,?,?,?)",
                (ts_utc, realized, unrealized, realized + unrealized, open_n))
            self._db.commit()

    def equity_curve(self, limit: int = 180) -> list[dict]:
        rows = [dict(r) for r in self._db.execute(
            "SELECT * FROM equity_curve ORDER BY ts_utc DESC LIMIT ?", (limit,))]
        return rows[::-1]

    # ── per-arm cycle tracking (for slow-TF arms: run once per closed bar,
    #    catch up after downtime — journal-backed, no schema change) ──
    def last_cycle_done(self) -> str:
        row = self._db.execute(
            "SELECT symbol FROM journal WHERE kind='CYCLE_DONE' ORDER BY id DESC LIMIT 1").fetchone()
        return row["symbol"] if row else ""

    def mark_cycle_done(self, cid: str):
        self.journal("CYCLE_DONE", cid)

    # ── validation milestones (one-shot, dedup via journal) ──
    def milestone_fired(self, key: str) -> bool:
        return self._db.execute(
            "SELECT 1 FROM journal WHERE kind='MILESTONE' AND symbol=?", (key,)).fetchone() is not None

    def milestone_mark(self, key: str):
        self.journal("MILESTONE", key, {"fired": True})

    def checkpoint(self):
        with self._lock:
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            self._db.commit()


_store: Optional[StateStore] = None


def get_state_store() -> StateStore:
    global _store
    if _store is None:
        _store = StateStore()
    return _store
