"""Paper trading 검증용 상세 분석 로거.

매 분석 사이클의 모든 결정과 시장 컨텍스트를 JSON 파일로 저장.
- 시장 데이터 (OHLCV, 지표, MTF 4H/15m, 선물 funding/OI)
- Phase 2 레짐 결정
- Phase 3 시그널 (WAIT/LONG/SHORT)
- AI 필터 결과 (PASS/REJECT + reason)
- Phase 3.5 전략 (SL/TP/leverage/position size)
- Phase 4 진입 / Recheck (HOLD/MODIFY/EXIT)

저장 위치: logs/analysis/<YYYY-MM-DD>/cycle-<timestamp>.json
"""

import json
import gc
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
    """현재 사이클의 모든 결정을 누적했다가 종결 시 JSON 1개로 저장."""

    def __init__(self):
        self._record: Dict[str, Any] = {}
        self._cycle_started_at: Optional[str] = None

    def start_cycle(self, cycle_type: str = "analysis"):
        self._record = {
            "cycle_type": cycle_type,  # 'analysis' | 'recheck'
            "symbol": None,  # set_symbol로 채움
            "started_at": datetime.utcnow().isoformat() + "Z",
            "phases": {},
            "final_decision": None,
        }
        self._cycle_started_at = self._record["started_at"]

    def set_symbol(self, symbol: str):
        if self._record:
            self._record["symbol"] = symbol

    def set_market_data(self, ai_input: Dict[str, Any]):
        # dataframe 등 직렬화 불가 항목 제외
        clean = {k: v for k, v in ai_input.items() if k != "dataframe"}
        self._record["market_data"] = clean

    def _ensure_phases(self):
        """defensive: _record 비어있으면 자동 init (race condition 방어)."""
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
            self._cycle_started_at = None
            gc.collect()


# 싱글톤
cycle_logger = CycleLogger()
