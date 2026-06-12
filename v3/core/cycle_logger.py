"""Paper trading 검증용 상세 분석 로거 (Thread-Safe Fix #45)

threading.local 기반으로 멀티 스레드 동시 사용 가능.
- worker thread: _analyze_for_score 등 병렬 분석에서 사용
- main thread: recheck, 진입 실행
- take_and_reset() / load_record() 로 thread 간 _record 이동
"""

import json
import gc
import threading
import numpy as np
from pathlib import Path
from datetime import datetime
from typing import Dict, Any, Optional

from config import get_logger

logger = get_logger("cycle_logger")

LOG_DIR = Path(__file__).parent.parent / "logs" / "analysis"


class _NumpyEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if hasattr(obj, "isoformat"):
            return obj.isoformat()
        return super().default(obj)


class CycleLogger:
    """Thread-local _record. 동시 사용 안전."""

    def __init__(self):
        self._local = threading.local()

    @property
    def _record(self) -> Dict[str, Any]:
        if not hasattr(self._local, 'record'):
            self._local.record = {}
        return self._local.record

    @_record.setter
    def _record(self, value: Dict[str, Any]):
        self._local.record = value

    def take_and_reset(self) -> Dict[str, Any]:
        """현재 thread의 _record 반환 + reset (worker → main 이동 시)"""
        rec = dict(self._record)  # copy
        self._record = {}
        return rec

    def load_record(self, rec: Dict[str, Any]):
        """외부 _record 복원 (main thread에서 winner save 시)"""
        self._record = dict(rec) if rec else {}

    def start_cycle(self, cycle_type: str = "analysis"):
        self._record = {
            "cycle_type": cycle_type,
            "symbol": None,
            "started_at": datetime.utcnow().isoformat() + "Z",
            "phases": {},
            "final_decision": None,
        }

    def set_symbol(self, symbol: str):
        if self._record:
            self._record["symbol"] = symbol

    def set_market_data(self, ai_input: Dict[str, Any]):
        clean = {k: v for k, v in ai_input.items() if k != "dataframe"}
        self._record["market_data"] = clean

    def _ensure_phases(self):
        if not self._record:
            logger.warning("cycle_logger: _record empty when phase setter called → auto start")
            self.start_cycle("auto")
        if "phases" not in self._record:
            self._record["phases"] = {}

    def set_regime(self, regime_obj):
        self._ensure_phases()
        self._record["phases"]["regime"] = {
            "regime": getattr(regime_obj, "regime", None) and regime_obj.regime.value,
            "confidence": getattr(regime_obj, "confidence", None),
            "details": getattr(regime_obj, "details", None),
        }

    def set_signal(self, signal_obj):
        self._ensure_phases()
        self._record["phases"]["signal"] = {
            "signal": getattr(signal_obj, "signal", None) and signal_obj.signal.value,
            "score": getattr(signal_obj, "score", None),
            "reason": getattr(signal_obj, "reason", None),
            "leverage": getattr(signal_obj, "leverage", None),
            "stop_loss_pct": getattr(signal_obj, "stop_loss_pct", None),
            "take_profit_pct": getattr(signal_obj, "take_profit_pct", None),
        }

    def set_ai_filter(self, filter_result: Dict[str, Any]):
        self._ensure_phases()
        self._record["phases"]["ai_filter"] = filter_result

    def set_strategy(self, strategy: Dict[str, Any]):
        self._ensure_phases()
        self._record["phases"]["strategy"] = {
            k: v for k, v in strategy.items()
            if k != "raw_data"
        }

    def set_position_open(self, position_result: Dict[str, Any]):
        self._ensure_phases()
        self._record["phases"]["position_open"] = position_result

    def set_recheck_input(self, position_info: Dict[str, Any], elapsed_hours: float,
                          unrealized_pnl_pct: float, prev_pnl_pct: Optional[float],
                          peak_pnl_pct: float, prev_decision: Optional[str]):
        self._ensure_phases()
        self._record["phases"]["recheck_input"] = {
            "position_info": position_info,
            "elapsed_hours": elapsed_hours,
            "unrealized_pnl_pct": unrealized_pnl_pct,
            "prev_pnl_pct": prev_pnl_pct,
            "peak_pnl_pct": peak_pnl_pct,
            "prev_decision": prev_decision,
        }

    def set_recheck_result(self, recheck_result: Dict[str, Any]):
        self._ensure_phases()
        self._record["phases"]["recheck_result"] = recheck_result

    def set_final_decision(self, decision: str, extra: Optional[Dict[str, Any]] = None):
        self._record["final_decision"] = decision
        if extra:
            self._record["final_extra"] = extra

    def save(self) -> Optional[Path]:
        if not self._record:
            return None
        try:
            self._record["ended_at"] = datetime.utcnow().isoformat() + "Z"
            now = datetime.now()
            day_dir = LOG_DIR / now.strftime("%Y-%m-%d")
            day_dir.mkdir(parents=True, exist_ok=True)
            ts = now.strftime("%H%M%S-%f")[:-3]
            cycle_type = self._record.get("cycle_type", "cycle")
            symbol = self._record.get("symbol") or "noSym"
            decision = self._record.get("final_decision", "unknown")
            fname = f"{ts}_{cycle_type}_{symbol}_{decision}.json"
            fp = day_dir / fname
            fp.write_text(
                json.dumps(self._record, cls=_NumpyEncoder, ensure_ascii=False, indent=2),
                encoding="utf-8"
            )
            logger.info(f"[CYCLE LOG] saved: {fp.relative_to(LOG_DIR.parent.parent)}")
            return fp
        except Exception as e:
            logger.error(f"cycle_logger save 실패: {e}")
            return None
        finally:
            self._record = {}
            gc.collect()


# 싱글톤 (thread-local 기반)
cycle_logger = CycleLogger()
