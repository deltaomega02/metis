"""자체 Trailing Stop 모듈 (Fix #44 — 2026-05-21)

리서치 기반:
- 단타 trailing: peak +0.6% margin 도달 후 활성, peak에서 0.3pp 후퇴 시 trail
- One-way (LONG: 위로만 / SHORT: 아래로만)
- 30초 폴링 (PG와 같은 주기)
- Bybit API set_trading_stop로 SL 동적 업데이트

PG와 보완 관계:
- Trailing: peak 후퇴 시 *SL 이동* (체결은 거래소 자동)
- PG: drawdown 한계 도달 시 *즉시 시장가 EXIT*
- = trailing이 *적극 lock*, PG가 *비상 안전망*
"""
import threading
import time
import logging
from typing import Optional, Callable

from config import get_logger, PROFIT_GUARD
from exchange.bybit_client import bybit_client
from database import db_manager

logger = get_logger("trailing_stop")


class TrailingStopManager:
    """자체 trailing SL 자동화. PG 스레드 별도로 운영."""

    # 단타 임계 (Fix #44)
    ACTIVATION_PNL_PCT = 0.6  # peak +0.6% margin 도달 시 활성
    TRAIL_DISTANCE_PP = 0.3   # peak에서 0.3pp 후퇴 시 SL 이동
    CHECK_INTERVAL_SEC = 30   # PG와 같은 주기

    def __init__(
        self,
        position_uuid: str,
        symbol: str,
        side: str,        # "LONG" or "SHORT"
        entry_price: float,
        leverage: int,
        initial_sl: float,
    ):
        self.position_uuid = position_uuid
        self.symbol = symbol
        self.side = side
        self.entry_price = entry_price
        self.leverage = leverage
        self.current_sl = initial_sl

        self._running = True
        self._lock = threading.Lock()
        self._peak_pnl_pct = 0.0
        self._activated = False
        self._thread: Optional[threading.Thread] = None

    def start(self):
        """스레드 시작."""
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        logger.info(
            f"Trailing Stop 시작: {self.symbol} {self.side} entry={self.entry_price} "
            f"활성화 +{self.ACTIVATION_PNL_PCT}% / trail {self.TRAIL_DISTANCE_PP}pp"
        )

    def stop(self):
        """스레드 중지."""
        with self._lock:
            self._running = False
        logger.info(f"Trailing Stop 중지: {self.symbol}")

    def _calculate_pnl_pct(self, mark_price: float) -> float:
        """margin PnL % 계산 (leverage 적용)."""
        if self.side == "LONG":
            return (mark_price - self.entry_price) / self.entry_price * 100 * self.leverage
        else:  # SHORT
            return (self.entry_price - mark_price) / self.entry_price * 100 * self.leverage

    def _pnl_to_price(self, pnl_pct: float) -> float:
        """target margin PnL %에 해당하는 가격 계산."""
        delta = pnl_pct / 100 / self.leverage * self.entry_price
        if self.side == "LONG":
            return self.entry_price + delta
        else:
            return self.entry_price - delta

    def _is_new_sl_better(self, new_sl: float) -> bool:
        """새 SL이 현재보다 *유리한지* (LONG ↑ / SHORT ↓)."""
        if self.side == "LONG":
            return new_sl > self.current_sl
        else:
            return new_sl < self.current_sl

    def _loop(self):
        """매 30초 peak 추적 + trail SL 업데이트."""
        while True:
            with self._lock:
                if not self._running:
                    return

            try:
                # 현재 가격
                ticker = bybit_client.get_ticker(self.symbol)
                if not ticker:
                    time.sleep(self.CHECK_INTERVAL_SEC)
                    continue
                mark_price = float(ticker.get("mark_price") or ticker.get("last_price") or 0)
                if mark_price <= 0:
                    time.sleep(self.CHECK_INTERVAL_SEC)
                    continue

                current_pnl_pct = self._calculate_pnl_pct(mark_price)

                # Peak 갱신
                if current_pnl_pct > self._peak_pnl_pct:
                    self._peak_pnl_pct = current_pnl_pct

                # 활성화 체크
                if not self._activated:
                    if self._peak_pnl_pct >= self.ACTIVATION_PNL_PCT:
                        self._activated = True
                        logger.info(
                            f"Trail 활성화: {self.symbol} peak {self._peak_pnl_pct:.2f}% "
                            f"≥ {self.ACTIVATION_PNL_PCT}%"
                        )

                # Trail SL 업데이트 (활성화된 경우)
                if self._activated:
                    new_sl_pnl_pct = self._peak_pnl_pct - self.TRAIL_DISTANCE_PP
                    new_sl_price = self._pnl_to_price(new_sl_pnl_pct)

                    # 가격 precision 적용 (대략 — 정밀화 가능)
                    new_sl_price = round(new_sl_price, 4)

                    # 새 SL이 더 유리할 때만 업데이트
                    if self._is_new_sl_better(new_sl_price):
                        try:
                            bybit_client.set_trading_stop(
                                symbol=self.symbol,
                                stop_loss=new_sl_price,
                            )
                            old_sl = self.current_sl
                            self.current_sl = new_sl_price

                            # DB 업데이트
                            try:
                                db_manager.update_position_targets(
                                    position_uuid=self.position_uuid,
                                    new_stop_loss=new_sl_price,
                                    new_take_profit=None,
                                )
                            except Exception as e:
                                logger.warning(f"DB SL update 실패: {e}")

                            logger.info(
                                f"🔒 Trail SL: {self.symbol} {old_sl} → {new_sl_price} "
                                f"(peak {self._peak_pnl_pct:.2f}% - {self.TRAIL_DISTANCE_PP}pp = "
                                f"new SL margin {new_sl_pnl_pct:+.2f}%)"
                            )
                        except Exception as e:
                            logger.warning(f"Trail SL 업데이트 실패: {e}")

            except Exception as e:
                logger.warning(f"Trail loop 에러: {e}")

            time.sleep(self.CHECK_INTERVAL_SEC)
