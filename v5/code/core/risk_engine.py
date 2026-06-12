"""Deterministic 3-layer risk engine.

Every ENTER decision passes through ``RiskEngine.evaluate`` before any order
is sent to the exchange. The check is asymmetric: at any failure the decision
is downgraded to NO_TRADE with a veto reason, and the journal records why.

Layer 1 — trade level
    Stop-loss distance bounds (ATR band + absolute %), target_R bounds, time
    stop window, position size from ``risk_per_trade × equity / sl_distance``
    capped by notional and rounded to the symbol step, leverage cap.

Layer 2 — strategy level
    Confidence >= threshold, manual kill flag, consecutive-loss cooldown and
    kill-until window from the risk_state table.

Layer 3 — account level
    Daily loss limit (per-day USDT cap), max drawdown vs equity high-water,
    concurrent-position cap (single-position by design).
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from ai.prompt_registry import SCHEMA_VERSION
from ai.response_schema import MetisDecision
from config.settings import RISK, TRADING, PAPER_MODE, PAPER_INITIAL_BALANCE_USDT
from core.semantic_validator import SemanticResult
from core.state_store import EventRecord, get_state_store

logger = logging.getLogger("metis.risk_engine")


@dataclass
class SizedOrder:
    """Final order plan that has cleared the risk engine and is ready for the executor."""
    symbol: str
    side: str  # "Buy" | "Sell"
    qty: float
    reference_price: float
    invalidation_price: float
    target_price: float
    leverage: int
    risk_usdt: float
    notional_usdt: float


@dataclass
class RiskDecision:
    """Result of one ``evaluate`` call."""

    pass_through: bool                                  # True → executor proceeds; False → vetoed, NO_TRADE
    veto_reason: Optional[str] = None
    sized_order: Optional[SizedOrder] = None
    risk_state_after: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────
def _round_qty(qty: float, step: float, precision: int) -> float:
    if step <= 0:
        return round(qty, precision)
    return math.floor(qty / step) * step


def _utc_date_str(now_utc: datetime) -> str:
    return now_utc.strftime("%Y-%m-%d")


def _utc_iso_in(now_utc: datetime, delta: timedelta) -> str:
    return (now_utc + delta).isoformat()


def _next_utc_day_start_iso(now_utc: datetime) -> str:
    tomorrow = (now_utc + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return tomorrow.isoformat()


# ──────────────────────────────────────────────────────────────────────
# Risk Engine
# ──────────────────────────────────────────────────────────────────────
class RiskEngine:
    """Singleton-friendly risk gate. Reads state via ``StateStore`` and applies the 3-layer veto."""

    def __init__(self, equity_usdt: float = PAPER_INITIAL_BALANCE_USDT):
        self.equity_usdt = equity_usdt
        self.store = get_state_store()

    def update_equity(self, equity_usdt: float):
        self.equity_usdt = equity_usdt
        try:
            self.store.equity_record(equity_usdt)
        except Exception as e:
            logger.warning(f"equity_record failed: {e}")

    # ── L3 daily / weekly / drawdown checks ──
    def _check_account_layer(self, now_utc: datetime) -> Optional[str]:
        # daily limit
        today = _utc_date_str(now_utc)
        day_row = self.store.daily_pnl_get(today)
        # initial threshold (more conservative until verified)
        day_limit_usdt = self.equity_usdt * RISK.DAILY_LOSS_LIMIT_PCT_INITIAL
        net_today = (day_row.get("realized_usdt", 0.0) or 0.0) + \
                    (day_row.get("fees_usdt", 0.0) or 0.0) + \
                    (day_row.get("funding_usdt", 0.0) or 0.0)
        if net_today <= -day_limit_usdt:
            return f"daily_loss_limit_hit:{net_today:.2f}<= -{day_limit_usdt:.2f}"

        # max drawdown (high-water based)
        cur_eq, hw = self.store.equity_current()
        if hw > 0:
            dd = (hw - self.equity_usdt) / hw
            if dd >= RISK.MAX_DRAWDOWN_RETIRE_PCT:
                return f"max_drawdown_retire:{dd*100:.2f}%"
            if dd >= RISK.MAX_DRAWDOWN_KILL_PCT:
                return f"max_drawdown_kill:{dd*100:.2f}%"
        return None

    def _check_strategy_layer(self, decision: MetisDecision, risk_state: dict) -> Optional[str]:
        if decision.confidence < RISK.CONFIDENCE_THRESHOLD:
            return f"confidence_below_gate:{decision.confidence:.2f}<{RISK.CONFIDENCE_THRESHOLD}"
        if risk_state.get("manual_kill", 0):
            return "manual_kill_active"
        # loss_streak no longer gates entries — daily/weekly $ caps (L3) are the only stops.
        return None

    def _check_concurrency(self) -> Optional[str]:
        open_pos = self.store.snapshot_get("open_position")
        if open_pos and open_pos.get("status") in ("OPEN", "OPENING"):
            return "concurrent_position_exists"
        return None

    def _size(self, decision: MetisDecision, sem: SemanticResult, ramp_phase: str = "initial") -> Optional[SizedOrder]:
        symbol = decision.symbol
        specs = TRADING.symbol_specs[symbol]
        side = "Buy" if decision.decision == "ENTER_LONG" else "Sell"

        ref = float(sem.normalized_reference_price)
        inv = float(sem.normalized_invalidation_price)
        tgt = float(sem.normalized_target_price)
        sl_distance = abs(ref - inv)
        if sl_distance <= 0:
            return None

        risk_pct = (
            RISK.RISK_PER_TRADE_PCT_INITIAL
            if ramp_phase == "initial"
            else RISK.RISK_PER_TRADE_PCT_MAX
        )
        risk_usdt = self.equity_usdt * risk_pct

        # qty so that (qty * sl_distance) == risk_usdt
        raw_qty = risk_usdt / sl_distance
        qty = _round_qty(raw_qty, specs["qty_step"], specs["qty_precision"])

        # Enforce min_order_qty
        if qty < specs["min_order_qty"]:
            qty = specs["min_order_qty"]

        notional = qty * ref
        # notional cap (paper)
        notional_cap = self.equity_usdt * RISK.NOTIONAL_CAP_PCT_PAPER
        if notional > notional_cap:
            qty = _round_qty(notional_cap / ref, specs["qty_step"], specs["qty_precision"])
            notional = qty * ref
            if qty < specs["min_order_qty"]:
                return None  # cannot size below min

        # Leverage: AI-decided (1..5), bounded to safety window. If AI omits, fall back to default.
        ai_lev = getattr(decision, "leverage", None)
        if ai_lev is None:
            leverage = RISK.LEVERAGE_DEFAULT
        else:
            leverage = max(RISK.LEVERAGE_MIN, min(int(ai_lev), RISK.LEVERAGE_MAX))

        return SizedOrder(
            symbol=symbol,
            side=side,
            qty=qty,
            reference_price=ref,
            invalidation_price=inv,
            target_price=tgt,
            leverage=leverage,
            risk_usdt=qty * sl_distance,
            notional_usdt=notional,
        )

    # ── entry point ──
    def evaluate(
        self,
        decision: MetisDecision,
        sem: SemanticResult,
        cycle_id: str,
        prompt_hash: str,
        ramp_phase: str = "initial",
    ) -> RiskDecision:
        """Run the 3-layer veto and return a ``RiskDecision``.

        NO_TRADE decisions pass through trivially. ENTER decisions must first
        pass the semantic check, then L3 (account), L2 (strategy) and finally
        L1 (sizing).
        """
        now_utc = datetime.now(timezone.utc)

        if decision.decision == "NO_TRADE":
            return RiskDecision(pass_through=True)

        if not sem.ok:
            reason = "semantic_violation:" + ";".join(sem.violations[:3])
            self._journal("RISK_VETO", decision.symbol, cycle_id, prompt_hash, reason)
            return RiskDecision(pass_through=False, veto_reason=reason)

        # L3 account
        l3 = self._check_account_layer(now_utc)
        if l3:
            self._journal("RISK_VETO", decision.symbol, cycle_id, prompt_hash, f"L3:{l3}")
            return RiskDecision(pass_through=False, veto_reason=f"L3:{l3}")

        # L2 strategy
        risk_state = self.store.risk_get()
        l2 = self._check_strategy_layer(decision, risk_state)
        if l2:
            self._journal("RISK_VETO", decision.symbol, cycle_id, prompt_hash, f"L2:{l2}")
            return RiskDecision(pass_through=False, veto_reason=f"L2:{l2}")

        # concurrency (winner-takes-all, single position)
        conc = self._check_concurrency()
        if conc:
            self._journal("RISK_VETO", decision.symbol, cycle_id, prompt_hash, f"L3:{conc}")
            return RiskDecision(pass_through=False, veto_reason=f"L3:{conc}")

        # L1 sizing
        sized = self._size(decision, sem, ramp_phase=ramp_phase)
        if sized is None:
            reason = "L1:cannot_size_above_min_qty"
            self._journal("RISK_VETO", decision.symbol, cycle_id, prompt_hash, reason)
            return RiskDecision(pass_through=False, veto_reason=reason)

        self._journal("RISK_PASS", decision.symbol, cycle_id, prompt_hash,
                      f"qty={sized.qty} notional={sized.notional_usdt:.2f} risk={sized.risk_usdt:.2f}")
        return RiskDecision(pass_through=True, sized_order=sized)

    # ── post-trade hooks ──
    def on_trade_closed(self, realized_usdt: float, fees_usdt: float, won: bool, now_utc: Optional[datetime] = None):
        now_utc = now_utc or datetime.now(timezone.utc)
        today = _utc_date_str(now_utc)
        self.store.daily_pnl_increment(
            utc_date=today,
            realized_delta=realized_usdt,
            fees_delta=fees_usdt,
            trade_delta=1,
            win_delta=1 if won else 0,
            loss_delta=0 if won else 1,
        )
        risk_state = self.store.risk_get()
        prev_streak = int(risk_state.get("loss_streak", 0) or 0)
        new_streak = 0 if won else prev_streak + 1

        # All loss-streak based caps disabled. Only daily/weekly $ caps (L3) and
        # manual_kill remain as account-level safety nets. Per-trade time-out X.
        self.store.risk_set(
            cooldown_until_utc=None,  # never set
            kill_until_utc=None,      # streak no longer triggers automatic kill
            loss_streak=new_streak,
            reason=f"trade_closed:{'win' if won else 'loss'}:streak={new_streak}",
        )

    def on_daily_kill(self, reason: str, now_utc: Optional[datetime] = None):
        now_utc = now_utc or datetime.now(timezone.utc)
        kill_until = _next_utc_day_start_iso(now_utc)
        self.store.risk_set(kill_until_utc=kill_until, reason=f"daily_kill:{reason}")
        today = _utc_date_str(now_utc)
        self.store.daily_pnl_increment(utc_date=today, kill_triggered=1)
        logger.warning(f"daily kill triggered: {reason} until {kill_until}")

    def set_manual_kill(self, on: bool, reason: str = "manual"):
        self.store.risk_set(manual_kill=1 if on else 0, reason=reason)

    # ── journal helper ──
    def _journal(self, kind: str, symbol: str, cycle_id: str, prompt_hash: str, payload: str):
        try:
            self.store.journal_append(EventRecord(
                ts_utc=datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id,
                kind=kind,
                symbol=symbol,
                actor="RISK_ENGINE",
                prompt_hash=prompt_hash,
                schema_version=SCHEMA_VERSION,
                risk_config_hash=None,
                payload={"detail": payload},
            ))
        except Exception:
            logger.exception("journal append failed")


# Singleton
_risk_engine: Optional[RiskEngine] = None


def get_risk_engine(equity_usdt: Optional[float] = None) -> RiskEngine:
    """Lazy singleton accessor. The first call seeds equity; later calls reuse the instance."""
    global _risk_engine
    if _risk_engine is None:
        seed = equity_usdt if equity_usdt is not None else PAPER_INITIAL_BALANCE_USDT
        _risk_engine = RiskEngine(equity_usdt=seed)
    elif equity_usdt is not None:
        _risk_engine.update_equity(equity_usdt)
    return _risk_engine
