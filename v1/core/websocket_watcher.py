# core/websocket_watcher.py
# 실시간 가격 감시 + Dead Man's Switch
# 익절/손절/청산 근접 모니터링

import time
import threading
from typing import Dict, Any, Optional, Callable

from exchange import BybitWebSocket, bybit_client
from core.leverage_calculator import is_near_liquidation, get_liquidation_distance
from utils import telegram_notifier
from config import SCHEDULER, TRADING, get_logger

logger = get_logger("websocket_watcher")

# 청산 근접 경고 쿨다운 (초)
LIQUIDATION_WARNING_COOLDOWN = 60

# 트레일링 스탑 설정
TRAILING_ACTIVATION_PCT = 2.0  # 수익률 2% 이상 시 트레일링 활성화
TRAILING_DISTANCE_PCT = 1.5    # 현재가 대비 1.5% 거리로 손절 추적
TRAILING_UPDATE_COOLDOWN = 30  # API 호출 쿨다운 (초)


class DeadMansSwitch:
    """
    WebSocket 연결 상태 감시 및 REST API 폴백
    
    60초간 데이터 미수신 시 REST API로 긴급 조회
    """
    
    def __init__(
        self,
        position_info: Dict[str, Any],
        on_should_close: Callable[[str], None]
    ):
        self.position_info = position_info
        self.on_should_close = on_should_close
        self.telegram = telegram_notifier
        
        self.last_data_time = time.time()
        self.is_fallback_active = False
        self._lock = threading.Lock()
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
    
    def start(self):
        """감시 시작"""
        self._running = True
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            daemon=True
        )
        self._monitor_thread.start()
        logger.info("Dead Man's Switch 시작")
    
    def stop(self):
        """감시 중지"""
        self._running = False
        logger.info("Dead Man's Switch 중지")
    
    def heartbeat(self):
        """WebSocket 데이터 수신 시 호출하여 타이머 리셋"""
        with self._lock:
            self.last_data_time = time.time()
            self.is_fallback_active = False
    
    def _monitor_loop(self):
        """타임아웃 감시 루프"""
        while self._running:
            time.sleep(10)
            
            with self._lock:
                elapsed = time.time() - self.last_data_time
            
            timeout = SCHEDULER.DEAD_MANS_SWITCH_TIMEOUT_SEC
            
            if elapsed > timeout and not self.is_fallback_active:
                self._trigger_fallback()
    
    def _trigger_fallback(self):
        """REST API 폴백 실행"""
        self.is_fallback_active = True
        
        timeout = SCHEDULER.DEAD_MANS_SWITCH_TIMEOUT_SEC
        logger.warning(f"WebSocket 데이터 {timeout}초 미수신. REST 폴백 실행.")
        
        self.telegram.send_websocket_error(timeout)
        
        try:
            position = bybit_client.get_position(TRADING.SYMBOL)
            ticker = bybit_client.get_ticker(TRADING.SYMBOL)
            current_price = ticker.get("last_price", 0)
            
            if not position:
                logger.info("포지션 없음. 폴백 종료.")
                return
            
            if self._should_close(current_price):
                reason = self._get_close_reason(current_price)
                logger.warning(f"Dead Man's Switch: 청산 조건 충족 ({reason})")
                self.on_should_close(reason)
            else:
                self.telegram.info(
                    f"[METIS-F] 포지션 상태 정상\n"
                    f"현재가: {current_price:,.0f} USDT\n"
                    f"WebSocket 재연결 시도 중..."
                )
        
        except Exception as e:
            logger.error(f"REST 폴백 실패: {e}")
            self.telegram.emergency(
                f"[METIS-F] 긴급: REST 폴백도 실패!\n"
                f"오류: {str(e)}\n"
                f"수동 확인 필요!"
            )
    
    def _should_close(self, current_price: float) -> bool:
        """손절/익절 조건 충족 여부"""
        direction = self.position_info.get("direction")
        stop_loss = self.position_info.get("stop_loss")
        take_profit = self.position_info.get("take_profit")
        
        if direction == "LONG":
            return current_price <= stop_loss or current_price >= take_profit
        else:
            return current_price >= stop_loss or current_price <= take_profit
    
    def _get_close_reason(self, current_price: float) -> str:
        """청산 사유 반환"""
        direction = self.position_info.get("direction")
        stop_loss = self.position_info.get("stop_loss")
        take_profit = self.position_info.get("take_profit")
        
        if direction == "LONG":
            if current_price >= take_profit:
                return "TAKE_PROFIT"
            return "STOP_LOSS"
        else:
            if current_price <= take_profit:
                return "TAKE_PROFIT"
            return "STOP_LOSS"


class FuturesWatcher:
    """
    선물 포지션 실시간 감시
    
    WebSocket + Dead Man's Switch 통합
    """
    
    def __init__(
        self,
        position_info: Dict[str, Any],
        on_close_triggered: Callable[[str], None]
    ):
        """
        Args:
            position_info: {direction, entry_price, stop_loss, take_profit, liquidation_price, leverage, ...}
            on_close_triggered: 청산 트리거 시 콜백
        """
        self.position_info = position_info
        self.on_close_triggered = on_close_triggered
        self.telegram = telegram_notifier
        
        self.direction = position_info["direction"]
        self.entry_price = position_info["entry_price"]
        self.stop_loss = position_info["stop_loss"]
        self.take_profit = position_info["take_profit"]
        self.liquidation = position_info["liquidation_price"]
        
        self._closed = False
        self._lock = threading.Lock()
        
        # 청산 근접 경고 상태
        self._liquidation_warned = False
        self._last_warning_time = 0.0
        
        # 트레일링 스탑 상태
        self._trailing_active = False
        self._last_trail_update_time = 0.0
        self._last_sent_stop: Optional[float] = None
        self._highest_price = position_info["entry_price"]  # LONG용
        self._lowest_price = position_info["entry_price"]   # SHORT용
        
        # WebSocket (가격 콜백 + 강제청산 콜백 연결)
        self.ws = BybitWebSocket(
            on_price_callback=self._on_price_update,
            on_position_closed_callback=self._on_liquidation_detected
        )
        
        # Dead Man's Switch
        self.dead_mans_switch = DeadMansSwitch(
            position_info,
            on_should_close=self._trigger_close
        )
        
        # Profit Guard 상태
        self.profit_guard_active = False
        self._current_unrealized_pnl_pct = 0.0
        self._leverage = position_info.get("leverage", 1)
    
    def start(self):
        """감시 시작"""
        logger.info(f"포지션 감시 시작: {self.direction} 손절={self.stop_loss} 익절={self.take_profit}")
        
        # WebSocket에 포지션 추적 상태 설정
        self.ws.set_position_tracking(True)
        
        # 청산 판별을 위한 청산가/방향 추적 설정
        self.ws.set_liquidation_tracking(self.liquidation, self.direction)
        
        self.ws.start()
        self.dead_mans_switch.start()
    
    def stop(self):
        """감시 중지"""
        self.ws.stop()
        self.dead_mans_switch.stop()
        logger.info("포지션 감시 중지")
    
    def _on_price_update(self, mark_price: float):
        """가격 업데이트 콜백"""
        # Dead Man's Switch 생존 신호
        self.dead_mans_switch.heartbeat()
        
        with self._lock:
            if self._closed:
                return
        
        # 익절 체크
        if self._should_take_profit(mark_price):
            self._trigger_close("TAKE_PROFIT")
            return
        
        # 손절 체크
        if self._should_stop_loss(mark_price):
            self._trigger_close("STOP_LOSS")
            return
        
        # Profit Guard 수익률 계산 및 플래그 관리
        self._update_profit_guard_flag(mark_price)
        
        # 트레일링 스탑 처리
        self._process_trailing_stop(mark_price)
        
        # 청산 근접 경고 (쿨다운 적용)
        if is_near_liquidation(mark_price, self.liquidation, self.direction):
            self._send_liquidation_warning_throttled(mark_price)
        else:
            with self._lock:
                self._liquidation_warned = False
    
    def _on_liquidation_detected(self, reason: str):
        """
        Private WebSocket에서 포지션 청산 감지 시 콜백
        
        Args:
            reason: 청산 사유 (LIQUIDATION | SERVER_TRIGGERED)
        """
        with self._lock:
            if self._closed:
                logger.info(f"이미 청산 처리됨 - {reason} 콜백 무시")
                return
        
        logger.warning(f"Private WebSocket 포지션 청산 감지: {reason}")
        
        if reason == "SERVER_TRIGGERED":
            logger.info("SL/TP 서버 체결로 판정 - STOP_LOSS로 청산 처리")
            self._trigger_close("STOP_LOSS")
        else:
            self._trigger_close("LIQUIDATION")
    
    def _send_liquidation_warning_throttled(self, mark_price: float):
        """쿨다운 적용된 청산 근접 경고"""
        current_time = time.time()
        
        with self._lock:
            if self._liquidation_warned:
                if current_time - self._last_warning_time < LIQUIDATION_WARNING_COOLDOWN:
                    return
            
            self._liquidation_warned = True
            self._last_warning_time = current_time
        
        distance = get_liquidation_distance(mark_price, self.liquidation, self.direction)
        self.telegram.send_liquidation_warning(mark_price, self.liquidation, distance)
        logger.warning(f"청산 근접 경고 발송: 현재가={mark_price} 거리={distance:.2f}%")
    
    def _should_take_profit(self, price: float) -> bool:
        """익절 조건"""
        if self.direction == "LONG":
            return price >= self.take_profit
        else:
            return price <= self.take_profit
    
    def _should_stop_loss(self, price: float) -> bool:
        """손절 조건"""
        if self.direction == "LONG":
            return price <= self.stop_loss
        else:
            return price >= self.stop_loss
    
    def _update_profit_guard_flag(self, current_price: float):
        """Profit Guard 활성화 플래그 업데이트"""
        if self.direction == "LONG":
            price_pnl_pct = (current_price - self.entry_price) / self.entry_price
        else:
            price_pnl_pct = (self.entry_price - current_price) / self.entry_price
        
        # 레버리지 포함 미실현 수익률
        self._current_unrealized_pnl_pct = price_pnl_pct * self._leverage
        
        from config import PROFIT_GUARD
        was_active = self.profit_guard_active
        self.profit_guard_active = self._current_unrealized_pnl_pct >= PROFIT_GUARD.ACTIVATION_PCT
        
        if self.profit_guard_active and not was_active:
            logger.info(
                f"Profit Guard 활성화: 미실현 수익률 "
                f"{self._current_unrealized_pnl_pct * 100:.2f}% >= "
                f"{PROFIT_GUARD.ACTIVATION_PCT * 100:.1f}%"
            )

    def _process_trailing_stop(self, current_price: float):
        """트레일링 스탑 처리"""
        # 수익률 계산
        if self.direction == "LONG":
            pnl_pct = ((current_price - self.entry_price) / self.entry_price) * 100
            self._highest_price = max(self._highest_price, current_price)
            reference_price = self._highest_price
        else:
            pnl_pct = ((self.entry_price - current_price) / self.entry_price) * 100
            self._lowest_price = min(self._lowest_price, current_price)
            reference_price = self._lowest_price
        
        # 트레일링 활성화 조건
        if pnl_pct < TRAILING_ACTIVATION_PCT:
            return
        
        if not self._trailing_active:
            self._trailing_active = True
            logger.info(f"트레일링 스탑 활성화: PnL={pnl_pct:.2f}%")
        
        # 새 손절가 계산
        trail_distance = reference_price * (TRAILING_DISTANCE_PCT / 100)
        if self.direction == "LONG":
            new_stop = reference_price - trail_distance
        else:
            new_stop = reference_price + trail_distance
        
        new_stop = round(new_stop, 2)
        
        # 기존 손절가보다 유리한지 확인
        if not self._is_better_stop(new_stop):
            return
        
        # 이전 전송값과 동일하면 스킵 (API 중복 호출 방지)
        if self._last_sent_stop is not None and new_stop == self._last_sent_stop:
            return
        
        # API 호출 쿨다운 체크
        current_time = time.time()
        if current_time - self._last_trail_update_time < TRAILING_UPDATE_COOLDOWN:
            return
        
        # 거래소 API로 실제 TP/SL 정정
        try:
            success = bybit_client.set_trading_stop(
                symbol=TRADING.SYMBOL,
                stop_loss=new_stop
            )
            
            if success:
                with self._lock:
                    self.stop_loss = new_stop
                    self.position_info["stop_loss"] = new_stop
                    self._last_sent_stop = new_stop
                self._last_trail_update_time = current_time
                logger.info(f"트레일링 스탑 갱신: {new_stop:,.0f} (기준가={reference_price:,.0f})")
        except Exception as e:
            logger.error(f"트레일링 스탑 API 호출 실패: {e}")
    
    def _is_better_stop(self, new_stop: float) -> bool:
        """기존 손절가보다 유리한 방향인지 확인"""
        if self.direction == "LONG":
            return new_stop > self.stop_loss
        else:
            return new_stop < self.stop_loss
    
    def _trigger_close(self, reason: str):
        """청산 트리거"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
        
        logger.info(f"청산 트리거: {reason}")
        
        if reason != "LIQUIDATION":
            self.ws.set_external_close_in_progress(True)
        
        self.stop()
        self.on_close_triggered(reason)
    
    def update_targets(
        self,
        stop_loss: Optional[float] = None,
        take_profit: Optional[float] = None
    ):
        """손절/익절가 업데이트 (중간 점검 시)"""
        with self._lock:
            if stop_loss is not None:
                self.stop_loss = stop_loss
                self.position_info["stop_loss"] = stop_loss
                self._last_sent_stop = None  # 트레일링 재시작
            if take_profit is not None:
                self.take_profit = take_profit
                self.position_info["take_profit"] = take_profit
        
        logger.info(f"타겟 업데이트: 손절={self.stop_loss} 익절={self.take_profit}")