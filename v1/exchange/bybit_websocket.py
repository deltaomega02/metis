# exchange/bybit_websocket.py
# Bybit WebSocket 실시간 데이터 스트림 핸들러
# Public(시세) + Private(포지션) 스트림 처리

import json
import time
import hmac
import hashlib
import threading
from typing import Callable, Optional, Dict, Any

import websocket

from config import BYBIT, TRADING, get_logger

logger = get_logger("bybit_websocket")


class BybitWebSocket:
    """
    Bybit WebSocket 클라이언트
    
    Public: ticker, trade 스트림
    Private: position, execution 스트림
    
    자동 재연결 및 ping/pong heartbeat 지원.
    """
    
    PING_INTERVAL = 20  # 20초마다 ping
    RECONNECT_DELAY = 5  # 재연결 대기 시간
    
    def __init__(
        self,
        on_price_callback: Optional[Callable[[float], None]] = None,
        on_position_closed_callback: Optional[Callable[[str], None]] = None
    ):
        """
        Args:
            on_price_callback: 가격 업데이트 콜백 (mark_price: float)
            on_position_closed_callback: 포지션 청산 감지 콜백 (reason: str)
        """
        self.on_price_callback = on_price_callback
        self.on_position_closed_callback = on_position_closed_callback
        
        if BYBIT.USE_TESTNET:
            self.ws_public_url = "wss://stream-testnet.bybit.com/v5/public/linear"
            self.ws_private_url = "wss://stream-testnet.bybit.com/v5/private"
            self.api_key = BYBIT.TESTNET_API_KEY
            self.secret = BYBIT.TESTNET_SECRET
        else:
            self.ws_public_url = "wss://stream.bybit.com/v5/public/linear"
            self.ws_private_url = "wss://stream.bybit.com/v5/private"
            self.api_key = BYBIT.API_KEY
            self.secret = BYBIT.SECRET
        
        self.ws_public: Optional[websocket.WebSocketApp] = None
        self.ws_private: Optional[websocket.WebSocketApp] = None
        
        self._running = False
        self._lock = threading.Lock()
        self._last_price: float = 0.0
        self._last_data_time: float = 0.0
        
        # 포지션 상태 추적
        self._had_position = False
        self._position_closed_handled = False
        
        # 외부 청산 처리 플래그 (FuturesWatcher가 익절/손절 처리 시 설정)
        self._external_close_in_progress = False
        
        # 청산 판별용 추적 변수
        self._tracked_liquidation_price: Optional[float] = None
        self._tracked_direction: Optional[str] = None
        
        # 스레드
        self._public_thread: Optional[threading.Thread] = None
        self._private_thread: Optional[threading.Thread] = None
        self._ping_thread: Optional[threading.Thread] = None
    
    def _generate_auth_signature(self) -> tuple:
        """Private 스트림 인증 서명 생성"""
        expires = int((time.time() + 10) * 1000)
        sign_str = f"GET/realtime{expires}"
        signature = hmac.new(
            self.secret.encode("utf-8"),
            sign_str.encode("utf-8"),
            hashlib.sha256
        ).hexdigest()
        return expires, signature
    
    # ========== Public Stream ==========
    
    def _on_public_open(self, ws):
        logger.info("Public WebSocket 연결됨")
        
        # ticker 구독 (mark_price 포함)
        subscribe_msg = {
            "op": "subscribe",
            "args": [f"tickers.{TRADING.SYMBOL}"]
        }
        ws.send(json.dumps(subscribe_msg))
    
    def _on_public_message(self, ws, message):
            try:
                data = json.loads(message)
                
                # 1. 핑퐁 응답 처리
                if data.get("op") == "pong":
                    return
                
                # 2. 구독 응답 처리
                if data.get("op") == "subscribe":
                    if data.get("success"):
                        logger.info(f"Public 구독 성공: {data.get('conn_id')}")
                    return
                
                # 3. Ticker 데이터 처리 (이 부분이 핵심 수정됨)
                topic = data.get("topic", "")
                if topic.startswith("tickers."):
                    ticker_data = data.get("data", {})
                    
                    # 새 가격 정보가 있는지 확인 (없으면 0)
                    new_price = float(ticker_data.get("markPrice", 0))
                    if new_price == 0:
                        new_price = float(ticker_data.get("lastPrice", 0))
                    
                    with self._lock:
                        # 가격 변동이 있을 때만 내부 변수 업데이트
                        if new_price > 0:
                            self._last_price = new_price
                        
                        # ★ 중요: 가격이 없어도(Delta update), 데이터가 왔으니 시간은 무조건 갱신
                        self._last_data_time = time.time()
                        
                        # 콜백에는 저장해둔 '최신 가격'을 사용
                        current_price = self._last_price

                    # 가격이 포함되지 않은 데이터라도, 연결 생존 신호를 보내기 위해 콜백 호출
                    if self.on_price_callback and current_price > 0:
                        self.on_price_callback(current_price)
            
            except json.JSONDecodeError:
                logger.warning(f"JSON 파싱 실패: {message[:100]}")
            except Exception as e:
                logger.error(f"Public 메시지 처리 오류: {e}")
    
    def _on_public_error(self, ws, error):
        logger.error(f"Public WebSocket 에러: {error}")
    
    def _on_public_close(self, ws, close_status_code, close_msg):
        logger.warning(f"Public WebSocket 종료: {close_status_code} - {close_msg}")
        
        # 재연결 시도
        if self._running:
            time.sleep(self.RECONNECT_DELAY)
            self._connect_public()
    
    def _connect_public(self):
        """Public WebSocket 연결"""
        self.ws_public = websocket.WebSocketApp(
            self.ws_public_url,
            on_open=self._on_public_open,
            on_message=self._on_public_message,
            on_error=self._on_public_error,
            on_close=self._on_public_close
        )
        
        self._public_thread = threading.Thread(
            target=self.ws_public.run_forever,
            daemon=True
        )
        self._public_thread.start()
    
    # ========== Private Stream ==========
    
    def _on_private_open(self, ws):
        logger.info("Private WebSocket 연결됨")
        
        # 인증
        expires, signature = self._generate_auth_signature()
        auth_msg = {
            "op": "auth",
            "args": [self.api_key, expires, signature]
        }
        ws.send(json.dumps(auth_msg))
    
    def _on_private_message(self, ws, message):
        try:
            data = json.loads(message)
            
            # pong 응답
            if data.get("op") == "pong":
                return
            
            # 인증 응답
            if data.get("op") == "auth":
                if data.get("success"):
                    logger.info("Private 인증 성공")
                    # position 구독
                    subscribe_msg = {
                        "op": "subscribe",
                        "args": ["position"]
                    }
                    ws.send(json.dumps(subscribe_msg))
                else:
                    logger.error(f"Private 인증 실패: {data}")
                return
            
            # 구독 응답
            if data.get("op") == "subscribe":
                if data.get("success"):
                    logger.info("Position 구독 성공")
                return
            
            # position 업데이트
            topic = data.get("topic", "")
            if topic == "position":
                self._handle_position_update(data.get("data", []))
        
        except json.JSONDecodeError:
            logger.warning(f"JSON 파싱 실패: {message[:100]}")
        except Exception as e:
            logger.error(f"Private 메시지 처리 오류: {e}")
    
    def _handle_position_update(self, positions: list):
        """포지션 업데이트 처리"""
        for pos in positions:
            if pos.get("symbol") != TRADING.SYMBOL:
                continue
            
            size = float(pos.get("size", 0))
            side = pos.get("side", "")
            unrealized_pnl = float(pos.get("unrealisedPnl", 0))
            
            logger.info(f"Position 업데이트: {side} size={size} pnl={unrealized_pnl}")
            
            with self._lock:
                # 외부에서 이미 청산 처리 중이면 콜백 무시
                if self._external_close_in_progress:
                    logger.info("외부 청산 처리 중 - Private WS 콜백 무시")
                    if size == 0:
                        self._had_position = False
                        self._position_closed_handled = True
                    continue
                
                # 포지션 존재 → 없음 전환 감지
                if self._had_position and size == 0:
                    if not self._position_closed_handled:
                        self._position_closed_handled = True
                        
                        # 현재 시장가와 청산가 비교로 진짜 강제청산 여부 판별
                        close_reason = self._determine_close_reason()
                        logger.warning(f"Private WS: 포지션 청산 감지 - 판정: {close_reason}")
                        
                        if self.on_position_closed_callback:
                            threading.Thread(
                                target=self.on_position_closed_callback,
                                args=(close_reason,),
                                daemon=True
                            ).start()
                
                # 포지션 상태 업데이트
                self._had_position = size > 0
                if size > 0:
                    self._position_closed_handled = False
    
    def _determine_close_reason(self) -> str:
        """
        포지션 종료 사유 판별
        
        현재 시장가와 저장된 청산가를 비교하여
        진짜 강제청산인지, SL/TP 체결인지 판별
        
        Returns:
            "LIQUIDATION" | "SERVER_TRIGGERED" (SL/TP 서버 체결)
        """
        # 청산가 정보가 없으면 안전하게 SERVER_TRIGGERED 반환
        if not hasattr(self, '_tracked_liquidation_price') or self._tracked_liquidation_price is None:
            logger.warning("청산가 정보 없음 - SERVER_TRIGGERED로 판정")
            return "SERVER_TRIGGERED"
        
        # 현재 시장가 가져오기
        current_price = self._last_price
        if current_price <= 0:
            logger.warning("현재 시장가 없음 - SERVER_TRIGGERED로 판정")
            return "SERVER_TRIGGERED"
        
        liq_price = self._tracked_liquidation_price
        direction = self._tracked_direction
        
        # 괴리율 계산 (1% 기준)
        diff_pct = abs(current_price - liq_price) / liq_price * 100
        
        logger.info(f"청산 판별: 현재가={current_price:.2f} 청산가={liq_price:.2f} 괴리율={diff_pct:.2f}%")
        
        # LONG: 현재가가 청산가 근처(1% 이내)이면 강제청산
        # SHORT: 현재가가 청산가 근처(1% 이내)이면 강제청산
        if diff_pct < 1.0:
            # 추가 검증: LONG은 현재가 < 청산가, SHORT은 현재가 > 청산가일 때만 강제청산
            if direction == "LONG" and current_price <= liq_price * 1.01:
                logger.warning(f"강제청산 확정: LONG 포지션, 현재가({current_price:.2f}) ≤ 청산가({liq_price:.2f})")
                return "LIQUIDATION"
            elif direction == "SHORT" and current_price >= liq_price * 0.99:
                logger.warning(f"강제청산 확정: SHORT 포지션, 현재가({current_price:.2f}) ≥ 청산가({liq_price:.2f})")
                return "LIQUIDATION"
        
        # 괴리율이 크면 서버 주문(SL/TP) 체결로 판정
        logger.info(f"SL/TP 서버 체결로 판정: 괴리율 {diff_pct:.2f}% > 1%")
        return "SERVER_TRIGGERED"
    
    def set_liquidation_tracking(self, liquidation_price: float, direction: str):
        """
        청산 판별을 위한 청산가/방향 추적 설정
        
        FuturesWatcher.start() 시 호출하여 청산가 정보 저장
        
        Args:
            liquidation_price: 예상 청산가
            direction: 포지션 방향 ("LONG" | "SHORT")
        """
        with self._lock:
            self._tracked_liquidation_price = liquidation_price
            self._tracked_direction = direction
        logger.info(f"청산 추적 설정: 청산가={liquidation_price:.2f} 방향={direction}")
    
    def _on_private_error(self, ws, error):
        logger.error(f"Private WebSocket 에러: {error}")
    
    def _on_private_close(self, ws, close_status_code, close_msg):
        logger.warning(f"Private WebSocket 종료: {close_status_code} - {close_msg}")
        
        # 재연결 시도
        if self._running:
            time.sleep(self.RECONNECT_DELAY)
            self._connect_private()
    
    def _connect_private(self):
        """Private WebSocket 연결"""
        self.ws_private = websocket.WebSocketApp(
            self.ws_private_url,
            on_open=self._on_private_open,
            on_message=self._on_private_message,
            on_error=self._on_private_error,
            on_close=self._on_private_close
        )
        
        self._private_thread = threading.Thread(
            target=self.ws_private.run_forever,
            daemon=True
        )
        self._private_thread.start()
    
    # ========== Ping/Pong ==========
    
    def _ping_loop(self):
        """주기적 ping 전송"""
        while self._running:
            time.sleep(self.PING_INTERVAL)
            
            ping_msg = json.dumps({"op": "ping"})
            
            try:
                if self.ws_public and self.ws_public.sock:
                    self.ws_public.send(ping_msg)
                
                if self.ws_private and self.ws_private.sock:
                    self.ws_private.send(ping_msg)
            except Exception as e:
                logger.warning(f"Ping 전송 실패: {e}")
    
    # ========== Public API ==========
    
    def start(self):
        """WebSocket 연결 시작"""
        if self._running:
            return
        
        self._running = True
        
        self._connect_public()
        self._connect_private()
        
        # Ping 스레드
        self._ping_thread = threading.Thread(
            target=self._ping_loop,
            daemon=True
        )
        self._ping_thread.start()
        
        logger.info("WebSocket 시작됨")
    
    def stop(self):
        """WebSocket 연결 종료"""
        self._running = False
        
        if self.ws_public:
            self.ws_public.close()
        if self.ws_private:
            self.ws_private.close()
        
        logger.info("WebSocket 종료됨")
    
    def get_last_price(self) -> float:
        """마지막 수신 가격 반환"""
        with self._lock:
            return self._last_price
    
    def get_last_data_time(self) -> float:
        """마지막 데이터 수신 시각 반환"""
        with self._lock:
            return self._last_data_time
    
    def is_connected(self) -> bool:
        """연결 상태 확인"""
        return (
            self._running and
            self.ws_public and
            self.ws_public.sock and
            self.ws_public.sock.connected
        )
    
    def set_position_tracking(self, has_position: bool):
        """포지션 추적 상태 설정 (외부에서 초기화 시 호출)"""
        with self._lock:
            self._had_position = has_position
            self._position_closed_handled = False
            self._external_close_in_progress = False
    
    def set_external_close_in_progress(self, in_progress: bool):
        """
        외부 청산 처리 상태 설정
        
        FuturesWatcher가 익절/손절 트리거 시 True로 설정하여
        Private WS의 position 콜백이 중복 처리하지 않도록 함
        
        Args:
            in_progress: 외부 청산 처리 중 여부
        """
        with self._lock:
            self._external_close_in_progress = in_progress
            if in_progress:
                logger.info("외부 청산 처리 시작 - Private WS 콜백 비활성화")
            else:
                logger.info("외부 청산 처리 완료 - Private WS 콜백 활성화")