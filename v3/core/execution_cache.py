"""
core/execution_cache.py — Bybit Private WS execution 스트림 in-memory cache.

Fix #27: 청산 체결 폴링 대신 *실시간 WebSocket push* 수신.
청산 직후 ~ms 내 cache에 도착 — Fix #23 (10s+19s wait) 대비 압도적.

Thread-safe singleton.
"""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional


class ExecutionCache:
    """Thread-safe symbol별 체결 이벤트 buffer.

    Bybit V5 Private WS 'execution' topic은 *각 체결 건마다* 즉시 push.
    이걸 받아서 buffer에 쌓아두면 position_manager가 청산 처리 시 즉시 조회 가능.
    """

    # 체결 데이터 유지 시간 (초). 청산 처리는 보통 청산 후 ~30s 이내 완료.
    # 그 이후엔 메모리 절약 위해 정리.
    TTL_SECONDS = 600  # 10분

    def __init__(self) -> None:
        self._lock = threading.RLock()
        # symbol → list of execution dicts
        # 각 dict: {"exec_time": int (ms), "side": str, "exec_qty": float,
        #          "exec_price": float, "exec_fee": float, "exec_type": str,
        #          "order_id": str, "received_at": float (local ts)}
        self._cache: Dict[str, List[dict]] = {}

    def push(self, executions: List[dict]) -> None:
        """Bybit WS execution 메시지 수신 시 호출.

        executions = data list from WS event. 각 element는 Bybit V5 execution schema.
        """
        if not executions:
            return
        now = time.time()
        with self._lock:
            for ex in executions:
                symbol = ex.get("symbol")
                if not symbol:
                    continue
                # Normalize keys to match get_execution_list() return shape
                normalized = {
                    "exec_time": int(ex.get("execTime") or ex.get("exec_time") or 0),
                    "side": ex.get("side", ""),
                    "exec_qty": float(ex.get("execQty") or ex.get("exec_qty") or 0),
                    "exec_price": float(ex.get("execPrice") or ex.get("exec_price") or 0),
                    "exec_fee": float(ex.get("execFee") or ex.get("exec_fee") or 0),
                    "exec_type": ex.get("execType") or ex.get("exec_type") or "Trade",
                    "order_id": ex.get("orderId") or ex.get("order_id") or "",
                    "order_link_id": ex.get("orderLinkId") or ex.get("order_link_id") or "",
                    "received_at": now,
                }
                self._cache.setdefault(symbol, []).append(normalized)
            # GC stale entries
            cutoff = now - self.TTL_SECONDS
            for sym in list(self._cache.keys()):
                self._cache[sym] = [e for e in self._cache[sym] if e["received_at"] >= cutoff]

    def get_after(
        self,
        symbol: str,
        side: str,
        entry_time_ms: int,
    ) -> List[dict]:
        """진입 시각 이후 + 청산 방향 일치하는 체결 반환.

        Args:
            symbol: e.g. BTCUSDT
            side: close side, e.g. "Sell" for LONG 청산 / "Buy" for SHORT 청산
            entry_time_ms: 진입 시각 unix ms. 이 시각 이후 체결만 반환.

        Returns:
            list of normalized exec dicts (오름차순 exec_time).
        """
        with self._lock:
            execs = list(self._cache.get(symbol, []))
        matched = [
            e for e in execs
            if e.get("exec_time", 0) > entry_time_ms and e.get("side") == side
        ]
        matched.sort(key=lambda e: e["exec_time"])
        return matched

    def clear(self, symbol: Optional[str] = None) -> None:
        """Manual reset. None이면 전체 clear."""
        with self._lock:
            if symbol is None:
                self._cache.clear()
            else:
                self._cache.pop(symbol, None)

    def size(self) -> Dict[str, int]:
        """Diagnostic: per-symbol cache size."""
        with self._lock:
            return {s: len(v) for s, v in self._cache.items()}


# Module-level singleton
execution_cache = ExecutionCache()
