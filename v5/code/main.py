"""METIS main orchestrator.

Single-process asyncio engine. On startup it boots the state store and prompt
registry, reconciles local state against Bybit, then launches the background
services (stream worker, watchdogs, position monitor, daily aggregator) and the
15-minute cycle scheduler.

Per cycle, each symbol is evaluated independently through the pipeline
(feature build → pre-filter → Gemini decide → semantic validate → risk engine).
The highest-confidence ENTER candidate that passes risk becomes the winner and
is executed; every evaluated symbol (winner and loser) is written to telemetry.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# allow `python main.py` from inside code/
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ai.gemini_client import get_gemini_client
from ai.prompt_registry import SCHEMA_VERSION, build_active_prompt, register_active_prompt
from config.settings import (
    CYCLE, GEMINI, LOGS_DIR, PAPER_INITIAL_BALANCE_USDT, PAPER_MODE, TRADING,
)
from core.cycle_scheduler import CycleScheduler
from core.daily_aggregator import DailyAggregator
from core.exchange_reconciler import ExchangeReconciler
from core.feature_builder import FeatureBuilder
from core.order_executor import get_order_executor
from core.position_monitor import PositionMonitor
from core.pre_filter import evaluate as pre_filter_evaluate
from core.risk_engine import get_risk_engine
from core.semantic_validator import validate as semantic_validate
from core.state_store import EventRecord, get_state_store
from core.stream_worker import StreamWorker
from core.telemetry_writer import (
    CycleEventLog, TelemetryWriter, confidence_bucket, get_telemetry,
)
from core.watchdogs import HealthCheck, TimeSyncGuard, WSWatchdog
from exchange.bybit_client import close_bybit_client, get_bybit_client
from exchange.paper_executor import get_paper_executor
from observability.telegram_bot import get_telegram


# ──────────────────────────────────────────────────────────────────────
# Logging
# ──────────────────────────────────────────────────────────────────────
LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOGS_DIR / "metis.log"),
    ],
)
logger = logging.getLogger("metis.main")


# ──────────────────────────────────────────────────────────────────────
# Main controller
# ──────────────────────────────────────────────────────────────────────
class MetisEngine:
    """Top-level trading engine. Owns every long-running component and routes
    each scheduler tick through the per-symbol decision pipeline."""

    def __init__(self):
        self.store = get_state_store()
        self.tele: TelemetryWriter = get_telemetry()
        self.bybit = get_bybit_client()
        self.paper = get_paper_executor() if PAPER_MODE else None
        self.gemini = get_gemini_client()
        self.risk = get_risk_engine(
            equity_usdt=(self.paper.get_equity() if PAPER_MODE else PAPER_INITIAL_BALANCE_USDT)
        )
        self.executor = get_order_executor()
        self.telegram = get_telegram()
        self.bundle = register_active_prompt()
        self.prompt_hash = self.bundle.content_hash

        self.stream = StreamWorker(list(TRADING.SYMBOLS))
        self.feature_builder = FeatureBuilder(stream_worker=self.stream)
        self.reconciler = ExchangeReconciler()
        self.position_monitor = PositionMonitor()
        self.ws_watchdog = WSWatchdog(self.stream, interval_sec=15.0)
        self.time_sync = TimeSyncGuard(interval_sec=60.0)
        self.health = HealthCheck(interval_sec=30.0)
        self.daily = DailyAggregator()
        self.daily.set_report_callback(self._on_daily_report)

        self.scheduler = CycleScheduler(on_cycle=self._on_cycle)
        self._running = False

    # ── lifecycle ──
    async def start(self):
        logger.info("=" * 60)
        logger.info(f"METIS start  mode={'PAPER' if PAPER_MODE else 'LIVE'}")
        logger.info(f"prompt={self.bundle.version} hash={self.prompt_hash}")
        logger.info(f"symbols={TRADING.SYMBOLS}")
        logger.info("=" * 60)

        # reconcile (boot)
        await self.reconciler.reconcile_at_startup()

        # background services
        await self.stream.start()
        await self.ws_watchdog.start()
        await self.time_sync.start()
        await self.health.start()
        await self.position_monitor.start()
        await self.daily.start()
        await self.scheduler.start()

        # equity init
        eq = self.paper.get_equity() if PAPER_MODE else PAPER_INITIAL_BALANCE_USDT
        self.risk.update_equity(eq)

        # telegram startup
        try:
            await self.telegram.notify_startup(
                mode="PAPER" if PAPER_MODE else "LIVE",
                equity=eq, prompt_hash=self.prompt_hash,
            )
        except Exception:
            pass

        self._running = True

    async def shutdown(self, reason: str = "signal"):
        logger.info(f"shutdown: {reason}")
        self._running = False
        for stopper in (self.scheduler.stop, self.daily.stop, self.position_monitor.stop,
                        self.health.stop, self.time_sync.stop, self.ws_watchdog.stop, self.stream.stop):
            try:
                await stopper()
            except Exception:
                logger.exception("shutdown stopper error")
        # Implicit cache is managed by Gemini server-side — nothing to invalidate.
        try:
            await self.telegram.notify_shutdown(reason)
            await self.telegram.close()
        except Exception:
            pass
        try:
            await close_bybit_client()
        except Exception:
            pass
        self.store.wal_checkpoint()
        logger.info("shutdown complete")

    # ── cycle ──
    async def _on_cycle(self, cycle_id: str, bar_close_utc: datetime):
        """Run one 15m cycle: evaluate every symbol, pick the winner, execute it."""
        # Skip the entire cycle when a position is already open — no fetches, no
        # LLM call, no telegram. PositionMonitor owns the open position via its
        # own 15s polling loop.
        position_state = self.store.snapshot_get("open_position") or {}
        if position_state.get("status") == "OPEN":
            logger.info(
                f"cycle {cycle_id} SKIPPED — position open: {position_state.get('symbol')} "
                f"{position_state.get('side')} @ {position_state.get('entry_price')}"
            )
            return

        logger.info(f"=== cycle {cycle_id} ===")

        # 2) time sync gate
        ts = self.store.snapshot_get("time_sync") or {}
        if ts and not ts.get("ok", True):
            logger.warning("time_sync NOT OK → skipping cycle")
            return

        # 3) equity refresh
        eq = self.paper.get_equity() if PAPER_MODE else PAPER_INITIAL_BALANCE_USDT
        self.risk.update_equity(eq)

        # Evaluate every symbol CONCURRENTLY; winner-takes-all.
        # Both symbols' pipelines (REST fan-out + Gemini call) are I/O-bound, so
        # asyncio.gather collapses the per-cycle wall-clock from sum(symbols) to
        # max(symbols) — roughly halves it for the two-symbol universe. The
        # feature_builder TTL cache still de-dups overlapping context endpoints
        # under concurrency via its own lock, and SQLite WAL serializes writes.
        winner: Optional[dict] = None

        position_state = self.store.snapshot_get("open_position") or {}
        position_open = position_state.get("status") == "OPEN"
        risk_state = self.store.risk_get()

        async def _eval_one(symbol: str) -> Optional[dict]:
            """Acquire the per-symbol cycle lock, run the pipeline, always release."""
            sym_cycle_id = f"{cycle_id}|{symbol}"
            if not self.store.cycle_lock_acquire(sym_cycle_id, symbol):
                logger.warning(f"cycle_lock busy for {sym_cycle_id} — skipping")
                return None
            try:
                return await self._evaluate_symbol(
                    cycle_id=sym_cycle_id, bar_close_utc=bar_close_utc,
                    symbol=symbol, position_open=position_open, risk_state=risk_state,
                )
            except Exception:
                logger.exception(f"cycle {symbol} error")
                return None
            finally:
                self.store.cycle_lock_release(sym_cycle_id, "COMPLETED")

        results = await asyncio.gather(*[_eval_one(s) for s in TRADING.SYMBOLS])
        evaluated = [ev for ev in results if ev is not None]

        # Winner candidate: highest-confidence ENTER that cleared risk (single position).
        for ev in evaluated:
            if (
                not position_open
                and ev.get("decision") in ("ENTER_LONG", "ENTER_SHORT")
                and ev.get("risk_pass")
                and ev.get("sized_order") is not None
            ):
                if winner is None or float(ev.get("confidence") or 0) > float(winner.get("confidence") or 0):
                    winner = ev

        # Execute winner if any
        if winner is not None:
            try:
                await self._execute_winner(winner)
            except Exception:
                logger.exception("winner execution failed")

        gc.collect()

    async def _evaluate_symbol(
        self,
        cycle_id: str,
        bar_close_utc: datetime,
        symbol: str,
        position_open: bool,
        risk_state: dict,
    ) -> dict:
        """Run the full per-symbol pipeline (features → pre-filter → LLM →
        semantic → risk) and return a summary dict used for winner selection
        and telemetry."""
        from core.feature_builder import FeatureSnapshot  # local import: avoid top-level cycle
        t_start = datetime.now(timezone.utc)
        snap: FeatureSnapshot = await self.feature_builder.build(symbol, bar_close_utc)

        # Phase 2: pre-filter
        pre = pre_filter_evaluate(snap, position_open=position_open, risk_state=risk_state)

        health = self.store.snapshot_get("health") or {}
        ws_state = self.store.snapshot_get("ws_health") or {}

        ev = {
            "cycle_id": cycle_id, "symbol": symbol, "snapshot_id": snap.snapshot_id,
            "asof_utc": snap.asof_utc, "decision": None, "setup_id": None, "direction": None,
            "confidence": None, "regime": None, "volatility_regime": None,
            "supervisor_action": None, "critical_rejects": None,
            "risk_pass": False, "risk_veto_reason": None, "sized_order": None,
            "fill_price": None, "fees_usdt": None, "protection_attached": False, "protection_delay_sec": None,
            "llm_input_tokens": 0, "llm_output_tokens": 0, "llm_latency_ms": 0,
            "cache_hit": 0, "retry_count": 0, "finish_reason": None, "llm_full_json": None,
            "schema_valid": 0, "semantic_valid": 0, "semantic_violations": None,
        }

        if not pre.pass_through:
            ev["decision"] = "NO_TRADE"
            ev["critical_rejects"] = pre.forced_critical_reject or ""
            ev["risk_veto_reason"] = pre.reason
            ev["reason_short"] = f"pre_filter blocked: {pre.reason or 'unknown'}"
            ev["feature_snapshot_json"] = snap.to_runtime_features_json()
            self._log_cycle(symbol, cycle_id, snap, ev, mode="PAPER" if PAPER_MODE else "LIVE",
                            ws_state=ws_state, health=health)
            await self._notify_cycle(ev)
            return ev

        # Phase 3: Gemini call
        runtime_anchor = self.feature_builder.runtime_anchor_json(cycle_id, symbol, bar_close_utc, snap.snapshot_id)
        runtime_features = snap.to_runtime_features_json()
        runtime_controls = self.feature_builder.runtime_controls_json(
            data_quality_ok=snap.data_quality_ok,
            event_filter_state=snap.event_filter.get("state", "CLEAR"),
            position_state=self.store.snapshot_get("open_position") or {},
            risk_state=risk_state,
        )
        ev["feature_snapshot_json"] = runtime_features
        ev["runtime_controls_json"] = runtime_controls

        result = await self.gemini.decide(runtime_anchor, runtime_features, runtime_controls)
        ev["llm_input_tokens"] = result.input_tokens
        ev["llm_output_tokens"] = result.output_tokens
        ev["llm_latency_ms"] = result.latency_ms
        ev["cache_hit"] = 1 if result.cache_hit else 0
        ev["cached_tokens"] = int(result.cached_tokens or 0)
        ev["retry_count"] = result.retry_count
        ev["finish_reason"] = result.finish_reason
        ev["llm_full_json"] = result.raw_text[:8000]
        ev["schema_valid"] = 1 if result.decision is not None else 0

        if result.decision is None:
            ev["decision"] = "NO_TRADE"
            ev["risk_veto_reason"] = f"LLM_OUTPUT_INVALID:{result.error or ''}"
            ev["reason_short"] = f"LLM 응답 무효: {result.error or 'unknown'}"
            self._log_cycle(symbol, cycle_id, snap, ev, mode="PAPER" if PAPER_MODE else "LIVE",
                            ws_state=ws_state, health=health)
            await self._notify_cycle(ev)
            return ev

        d = result.decision
        ev["decision"] = d.decision
        ev["setup_id"] = d.setup_id
        ev["direction"] = d.technical_proposal.direction
        ev["confidence"] = d.confidence
        ev["regime"] = d.regime
        ev["volatility_regime"] = d.volatility_regime
        ev["supervisor_action"] = d.supervisor_review.override_action
        ev["critical_rejects"] = ",".join(d.critical_reject_matched or [])
        ev["technical_evidence"] = d.technical_proposal.technical_evidence_summary
        ev["supervisor_evidence"] = d.supervisor_review.supervisor_opposing_evidence
        ev["reason_short"] = d.reason_short
        ev["ai_leverage"] = d.leverage

        # Phase 4: Semantic validator (only for ENTER)
        last_price = float(snap.tf_15m.get("close") or 0)
        atr_pct = float(snap.tf_15m.get("atr14_pct") or 0)
        spread_bps = float(snap.stream.get("spread_bps") or 0) if snap.stream else None
        sem = semantic_validate(d, last_price=last_price, atr_15m_pct=atr_pct, spread_bps=spread_bps)
        ev["semantic_valid"] = 1 if sem.ok else 0
        ev["semantic_violations"] = ";".join(sem.violations[:5]) if sem.violations else None

        # Phase 5: Risk engine
        risk_dec = self.risk.evaluate(d, sem, cycle_id=cycle_id, prompt_hash=self.prompt_hash)
        ev["risk_pass"] = bool(risk_dec.pass_through)
        ev["risk_veto_reason"] = risk_dec.veto_reason
        ev["sized_order"] = risk_dec.sized_order
        ev["_decision_obj"] = d  # internal — not telemetry

        self._log_cycle(symbol, cycle_id, snap, ev, mode="PAPER" if PAPER_MODE else "LIVE",
                        ws_state=ws_state, health=health)
        await self._notify_cycle(ev)
        return ev

    async def _notify_cycle(self, ev: dict):
        try:
            await self.telegram.notify_cycle(ev)
        except Exception:
            logger.exception("telegram notify_cycle failed")

    async def _execute_winner(self, winner: dict):
        d = winner["_decision_obj"]
        sized = winner["sized_order"]
        cycle_id = winner["cycle_id"]

        exec_result = await self.executor.execute_entry(d, sized, cycle_id=cycle_id, prompt_hash=self.prompt_hash)

        if not exec_result.success:
            try:
                await self.telegram.notify_error("EXECUTION", exec_result.error or "unknown")
            except Exception:
                pass
            return

        try:
            R = float(d.target_R or 0.0)
            await self.telegram.notify_entry(
                symbol=sized.symbol, side=sized.side, qty=sized.qty, entry=exec_result.fill_price or sized.reference_price,
                sl=sized.invalidation_price, tp=sized.target_price, R=R,
                setup_id=d.setup_id, confidence=d.confidence,
                leverage=sized.leverage,
            )
        except Exception:
            logger.exception("telegram entry notify failed")

    # ── telemetry helper ──
    def _log_cycle(self, symbol: str, cycle_id: str, snap, ev: dict, mode: str,
                   ws_state: dict, health: dict):
        try:
            self.tele.log_cycle(CycleEventLog(
                asof_utc=ev.get("asof_utc") or datetime.now(timezone.utc).isoformat(),
                cycle_id=cycle_id, symbol=symbol, mode=mode,
                prompt_version=self.bundle.version,
                prompt_hash=self.prompt_hash,
                schema_version=SCHEMA_VERSION,
                model_id=GEMINI.MODEL_ID,
                risk_config_hash=None,
                feature_snapshot_hash=ev.get("snapshot_id"),
                llm_input_tokens=ev.get("llm_input_tokens", 0),
                llm_output_tokens=ev.get("llm_output_tokens", 0),
                llm_latency_ms=ev.get("llm_latency_ms", 0),
                cache_hit=ev.get("cache_hit", 0),
                retry_count=ev.get("retry_count", 0),
                finish_reason=ev.get("finish_reason"),
                llm_full_json=ev.get("llm_full_json"),
                schema_valid=ev.get("schema_valid", 0),
                semantic_valid=ev.get("semantic_valid", 0),
                semantic_violations=ev.get("semantic_violations"),
                risk_pass=1 if ev.get("risk_pass") else 0,
                risk_veto_reason=ev.get("risk_veto_reason"),
                decision=ev.get("decision"),
                setup_id=ev.get("setup_id"),
                direction=ev.get("direction"),
                confidence=ev.get("confidence"),
                confidence_bucket=confidence_bucket(ev.get("confidence")) if ev.get("confidence") is not None else None,
                regime=ev.get("regime"),
                volatility_regime=ev.get("volatility_regime"),
                critical_rejects=ev.get("critical_rejects"),
                supervisor_action=ev.get("supervisor_action"),
                order_decision="ENTERED" if ev.get("risk_pass") and ev.get("decision") != "NO_TRADE" else None,
                order_intent_id=None,
                fill_price=None, fill_qty=None, fees_usdt=None,
                protection_attached=1 if ev.get("protection_attached") else 0,
                protection_delay_sec=ev.get("protection_delay_sec"),
                ws_stale=1 if ws_state.get("any_stale") else 0,
                ntp_offset_ms=(self.store.snapshot_get("time_sync") or {}).get("offset_ms"),
                mem_rss_mb=health.get("mem_rss_mb"),
                disk_free_pct=health.get("disk_free_pct"),
                notes_json=None,
                feature_snapshot_json=ev.get("feature_snapshot_json"),
                runtime_controls_json=ev.get("runtime_controls_json"),
            ))
        except Exception:
            logger.exception("telemetry log failed")

    async def _on_daily_report(self, report):
        try:
            await self.telegram.notify_daily(report.__dict__ if hasattr(report, "__dict__") else report)
        except Exception:
            logger.exception("daily telegram failed")


# ──────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────
async def _run_main():
    bot = MetisEngine()

    loop = asyncio.get_running_loop()
    stop_event = asyncio.Event()

    def _signal_handler(sig):
        logger.info(f"signal {sig}")
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler, sig)
        except NotImplementedError:
            pass

    await bot.start()
    await stop_event.wait()
    await bot.shutdown(reason="signal")


def main():
    try:
        asyncio.run(_run_main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
