"""Risk-engine veto tests — synthetic loss injection drives the kill switch."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import pytest

from core.risk_engine import RiskEngine
from core.state_store import get_state_store


@pytest.fixture(autouse=True)
def _clean_state(tmp_path, monkeypatch):
    db = tmp_path / "test_state.db"
    monkeypatch.setattr("core.state_store.STATE_DB_PATH", db)
    # reset singleton
    from core.state_store import StateStore
    StateStore._instance = None
    yield
    StateStore._instance = None


def test_consecutive_loss_kill_switch():
    eng = RiskEngine(equity_usdt=1000.0)
    store = get_state_store()
    # simulate 3 consecutive losses
    eng.on_trade_closed(realized_usdt=-5, fees_usdt=0.5, won=False)
    rs1 = store.risk_get()
    assert rs1["loss_streak"] == 1
    assert rs1["cooldown_until_utc"] is not None
    assert rs1["kill_until_utc"] is None

    eng.on_trade_closed(realized_usdt=-5, fees_usdt=0.5, won=False)
    rs2 = store.risk_get()
    assert rs2["loss_streak"] == 2
    assert rs2["cooldown_until_utc"] is not None
    assert rs2["kill_until_utc"] is None

    eng.on_trade_closed(realized_usdt=-5, fees_usdt=0.5, won=False)
    rs3 = store.risk_get()
    assert rs3["loss_streak"] == 3
    assert rs3["kill_until_utc"] is not None


def test_win_resets_streak():
    eng = RiskEngine(equity_usdt=1000.0)
    eng.on_trade_closed(realized_usdt=-5, fees_usdt=0.5, won=False)
    eng.on_trade_closed(realized_usdt=8, fees_usdt=0.5, won=True)
    rs = get_state_store().risk_get()
    assert rs["loss_streak"] == 0


def test_daily_loss_limit_triggers():
    eng = RiskEngine(equity_usdt=1000.0)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    # accumulate beyond -1% (1000 * 0.01 = 10)
    eng.store.daily_pnl_increment(utc_date=today, realized_delta=-11.0, trade_delta=1, loss_delta=1)
    msg = eng._check_account_layer(datetime.now(timezone.utc))
    assert msg is not None and "daily_loss_limit" in msg


def test_manual_kill():
    eng = RiskEngine(equity_usdt=1000.0)
    eng.set_manual_kill(True, reason="user_panic_button")
    rs = get_state_store().risk_get()
    assert rs["manual_kill"] == 1
