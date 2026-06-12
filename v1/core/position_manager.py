# core/position_manager.py
# 포지션 오픈/클로즈 관리
# 청산가 검증, 시장가 주문 실행, 펀딩비 차감

import time
from typing import Dict, Any, Optional, Tuple
from datetime import datetime

from exchange import bybit_client, InsufficientBalanceError
from core.leverage_calculator import (
    calculate_liquidation_price,
    validate_stop_loss_margin,
    calculate_pnl,
    InvalidStopLossError
)
from database import db_manager
from utils import telegram_notifier
from config import TRADING, get_logger

logger = get_logger("position_manager")

# 체결 조회 재시도 설정
EXECUTION_QUERY_MAX_RETRIES = 3
EXECUTION_QUERY_RETRY_DELAY = 1.0


class PositionManager:
    """
    포지션 관리자
    
    포지션 진입, 청산, 상태 조회
    """
    
    def __init__(self):
        self.client = bybit_client
        self.db = db_manager
        self.telegram = telegram_notifier
    
    def _get_execution_with_retry(
        self,
        order_id: str,
        max_retries: int = EXECUTION_QUERY_MAX_RETRIES
    ) -> Optional[Dict[str, Any]]:
        """
        체결 내역 조회 (재시도 로직 포함)
        
        Args:
            order_id: 주문 ID
            max_retries: 최대 재시도 횟수
        
        Returns:
            체결 상세 또는 None
        """
        for attempt in range(max_retries):
            time.sleep(EXECUTION_QUERY_RETRY_DELAY)
            
            execution = self.client.get_execution_detail(order_id)
            
            if execution and execution.get("avg_price", 0) > 0:
                logger.info(f"체결 내역 조회 성공 (시도 {attempt + 1}/{max_retries})")
                return execution
            
            logger.warning(f"체결 내역 미확인 (시도 {attempt + 1}/{max_retries})")
        
        logger.error(f"체결 내역 조회 실패: order_id={order_id}")
        return None
    
    def _get_server_triggered_execution(
        self,
        direction: str,
        entry_timestamp: str
    ) -> Tuple[Optional[float], float]:
        """
        서버에서 체결된 SL/TP 내역 조회 (부분 체결 합산)
        
        거래소에서 SL/TP가 자동 체결된 경우,
        최근 체결 내역에서 해당 거래를 찾아 가격과 수수료 반환.
        부분 체결된 경우 모든 체결 건의 수수료를 합산.
        
        Args:
            direction: 포지션 방향 ("LONG" | "SHORT")
            entry_timestamp: 포지션 진입 시각 (ISO format)
        
        Returns:
            (체결가, 수수료) 튜플. 조회 실패 시 (None, 0.0)
        """
        logger.info(f"서버 SL/TP 체결 내역 조회 시작: direction={direction}")
        
        # 체결 직후 API 반영 지연 대응 - 초기 대기
        time.sleep(2.0)
        
        # 재시도 로직: API 반영 지연 대응
        for attempt in range(3):
            if attempt > 0:
                time.sleep(1.0)
                logger.info(f"체결 내역 조회 재시도 ({attempt + 1}/3)")
            
            try:
                # 최근 체결 내역 조회 (limit 증가)
                executions = self.client.get_execution_list(
                    symbol=TRADING.SYMBOL,
                    limit=50
                )
                
                if not executions:
                    logger.warning(f"체결 내역 없음 (시도 {attempt + 1}/3)")
                    continue
                
                # 진입 시각 파싱
                try:
                    entry_time_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000)
                except ValueError as e:
                    logger.error(f"entry_timestamp 파싱 실패: {entry_timestamp} - {e}")
                    entry_time_ms = 0  # 시간 필터 무시
                
                # 방향에 맞는 청산 side
                close_side = "Sell" if direction == "LONG" else "Buy"
                
                logger.info(f"체결 내역 검색: entry_time_ms={entry_time_ms}, close_side={close_side}, 총 {len(executions)}건")
                
                # 진입 이후의 청산 체결 수집 (부분 체결 합산)
                matched_executions = []
                
                for ex in executions:
                    exec_time = int(ex.get("exec_time", 0))
                    exec_side = ex.get("side")
                    
                    # 진입 이전 체결은 무시 (단, entry_time_ms가 0이면 시간 필터 무시)
                    if entry_time_ms > 0 and exec_time < entry_time_ms:
                        continue
                    
                    # 방향 일치 확인
                    if exec_side == close_side:
                        matched_executions.append(ex)
                
                if not matched_executions:
                    logger.warning(f"진입 이후 청산 체결 내역 없음 (시도 {attempt + 1}/3)")
                    continue
                
                # 부분 체결 합산: 가격은 가중평균, 수수료는 합산
                total_qty = 0.0
                total_value = 0.0  # qty * price
                total_fee = 0.0
                
                for ex in matched_executions:
                    exec_qty = float(ex.get("exec_qty", 0))
                    exec_price = float(ex.get("exec_price", 0))
                    exec_fee = float(ex.get("exec_fee", 0))
                    exec_type = ex.get("exec_type", "")
                    
                    total_qty += exec_qty
                    total_value += exec_qty * exec_price
                    total_fee += exec_fee
                    
                    logger.debug(
                        f"체결 건: qty={exec_qty} price={exec_price} fee={exec_fee} type={exec_type}"
                    )
                
                if total_qty > 0 and total_value > 0:
                    avg_exit_price = total_value / total_qty
                    
                    logger.info(
                        f"서버 SL/TP 체결 확인: 체결건수={len(matched_executions)} "
                        f"총수량={total_qty} 평균가={avg_exit_price:.2f} "
                        f"총수수료={total_fee:.6f}"
                    )
                    return avg_exit_price, total_fee
                
                logger.warning(f"유효한 체결 데이터 없음 (시도 {attempt + 1}/3)")
                
            except Exception as e:
                logger.error(f"서버 체결 내역 조회 실패 (시도 {attempt + 1}/3): {e}")
        
        logger.error("서버 SL/TP 체결 내역 조회 최종 실패")
        return None, 0.0
    
    def open_position(self, strategy: Dict[str, Any]) -> Dict[str, Any]:
        """
        포지션 진입
        
        1. 청산가 검증
        2. 레버리지 설정
        3. 시장가 진입
        4. DB 기록 (진입 수수료 포함)
        
        Args:
            strategy: 전략 딕셔너리
        
        Returns:
            진입 결과
        
        Raises:
            InvalidStopLossError: 손절가 검증 실패
            InsufficientBalanceError: 잔고 부족
        """
        direction = strategy["direction"]
        leverage = strategy["leverage"]
        entry_price = strategy["entry_price"]
        stop_loss = strategy["stop_loss_price"]
        take_profit = strategy["take_profit_price"]
        quantity = strategy["quantity_btc"]
        
        logger.info(f"포지션 진입 시작: {direction} {leverage}x @ {entry_price}")
        
        # 1. 청산가 계산
        liquidation = calculate_liquidation_price(entry_price, leverage, direction)
        
        # 2. 손절가 마진 검증
        if not validate_stop_loss_margin(stop_loss, liquidation, direction):
            raise InvalidStopLossError(
                f"손절가({stop_loss:.0f})가 청산가({liquidation:.0f})에 너무 가깝습니다"
            )
        
        # 3. 레버리지 설정
        self.client.set_leverage(TRADING.SYMBOL, leverage)
        
        # 4. 시장가 진입
        side = "Buy" if direction == "LONG" else "Sell"
        order_result = self.client.place_market_order(
            symbol=TRADING.SYMBOL,
            side=side,
            qty=quantity
        )
        
        # 5. 체결 완료 대기 후 실제 데이터 조회 (재시도 로직)
        time.sleep(1.0)
        
        position = None
        for attempt in range(EXECUTION_QUERY_MAX_RETRIES):
            position = self.client.get_position(TRADING.SYMBOL)
            if position and position.get("size", 0) > 0:
                break
            logger.warning(f"포지션 조회 재시도 ({attempt + 1}/{EXECUTION_QUERY_MAX_RETRIES})")
            time.sleep(EXECUTION_QUERY_RETRY_DELAY)
        
        if not position or position.get("size", 0) == 0:
            raise Exception("포지션 조회 실패: 체결 미확인")
        
        actual_entry = position["entry_price"]
        actual_quantity = position["size"]
        actual_liquidation = position["liquidation_price"]
        
        # 6. 체결 수수료 조회 (재시도 로직)
        order_id = order_result.get("order_id")
        entry_fee = 0.0
        
        if order_id:
            execution = self._get_execution_with_retry(order_id)
            if execution:
                entry_fee = execution.get("exec_fee", 0)
        
        # 7. DB 기록 (진입 수수료 포함)
        position_uuid = self.db.create_position(
            direction=direction,
            leverage=leverage,
            entry_price=actual_entry,
            entry_quantity=actual_quantity,
            stop_loss_price=stop_loss,
            take_profit_price=take_profit,
            liquidation_price=actual_liquidation,
            confidence_score=strategy.get("confidence", 0),
            ai_reason=strategy.get("ai_reason", ""),
            strategy_json=str(strategy),
            entry_fee=entry_fee
        )
        
        # 8. 텔레그램 알림
        self.telegram.send_position_opened(
            direction=direction,
            leverage=leverage,
            entry_price=actual_entry,
            qty=actual_quantity,
            stop_loss=stop_loss,
            take_profit=take_profit,
            margin_used=strategy.get("margin_used", 0),
            first_recheck_hours=strategy.get("first_recheck_hours", 6),
            entry_fee=entry_fee
        )
        
        logger.info(
            f"포지션 진입 완료: {position_uuid[:8]}... "
            f"체결가={actual_entry} 수량={actual_quantity} 수수료={entry_fee:.4f}"
        )
        
        return {
            "success": True,
            "position_uuid": position_uuid,
            "direction": direction,
            "leverage": leverage,
            "entry_price": actual_entry,
            "quantity": actual_quantity,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "liquidation": actual_liquidation,
            "order_id": order_id,
            "entry_fee": entry_fee
        }
    
    def close_position(
        self,
        position_uuid: str,
        reason: str,
        exit_price: Optional[float] = None
    ) -> Dict[str, Any]:
        """
        포지션 청산
        
        Args:
            position_uuid: 포지션 UUID
            reason: 청산 사유 (TAKE_PROFIT, STOP_LOSS, AI_EXIT, DEAD_MANS_SWITCH, PROFIT_GUARD, LIQUIDATION)
            exit_price: 청산가 (None이면 체결 내역에서 조회)
        
        Returns:
            청산 결과
        """
        # 1. DB에서 포지션 조회
        db_position = self.db.get_active_position()
        if not db_position or db_position["position_uuid"] != position_uuid:
            logger.warning(f"활성 포지션 없음: {position_uuid}")
            return {"success": False, "error": "포지션 없음"}
        
        direction = db_position["direction"]
        entry_price = db_position["entry_price"]
        quantity = db_position["entry_quantity"]
        leverage = db_position["leverage"]
        entry_timestamp = db_position["entry_timestamp"]
        entry_fee = db_position.get("entry_fee", 0)
        
        logger.info(f"포지션 청산 시작: {position_uuid[:8]}... 사유={reason}")
        
        # 2. 강제청산인 경우 별도 처리
        if reason == "LIQUIDATION":
            return self._handle_liquidation_close(
                position_uuid=position_uuid,
                db_position=db_position
            )
        
        # 3. 거래소에 포지션이 남아있는지 확인
        exchange_position = self.client.get_position(TRADING.SYMBOL)
        
        actual_exit_price = exit_price
        exit_fee = 0.0
        
        if exchange_position and exchange_position.get("size", 0) > 0:
            # 포지션이 아직 있음 -> 봇이 직접 청산
            logger.info("거래소에 포지션 존재 - 직접 청산 실행")
            try:
                close_result = self.client.close_position(TRADING.SYMBOL, direction)
            except Exception as e:
                error_msg = str(e).lower()
                # 이미 청산된 경우 (중복 청산 시도) - 서버 체결로 전환
                if "position not found" in error_msg or "not exist" in error_msg or "no position" in error_msg:
                    logger.warning(f"포지션 이미 청산됨 (중복 청산 시도): {e}")
                    # 서버 SL/TP 체결 경로로 전환
                    actual_exit_price, exit_fee = self._get_server_triggered_execution(
                        direction=direction,
                        entry_timestamp=entry_timestamp
                    )
                    if actual_exit_price is None:
                        ticker = self.client.get_ticker(TRADING.SYMBOL)
                        actual_exit_price = ticker.get("last_price", entry_price)
                else:
                    logger.error(f"청산 주문 실패: {e}")
                    self.telegram.send_system_error("ORDER_ERROR", str(e), "close_position")
                    return {"success": False, "error": str(e)}
            
            # 체결 완료 대기 후 실제 체결가 조회 (재시도 로직)
            if actual_exit_price is None:
                order_id = close_result.get("order_id")
                
                if order_id:
                    execution = self._get_execution_with_retry(order_id)
                    if execution:
                        actual_exit_price = execution.get("avg_price", 0)
                        exit_fee = execution.get("exec_fee", 0)
                
                # 재시도 후에도 실패 시 ticker 폴백
                if not actual_exit_price or actual_exit_price == 0:
                    ticker = self.client.get_ticker(TRADING.SYMBOL)
                    actual_exit_price = ticker.get("last_price", entry_price)
                    logger.warning(f"체결가 조회 최종 실패, ticker 폴백: {actual_exit_price}")
                    
                    self.telegram.info(
                        f"[METIS-F] 체결가 조회 실패\n"
                        f"ticker 폴백 사용: {actual_exit_price:,.2f} USDT\n"
                        f"실제 체결가와 차이 발생 가능"
                    )
        else:
            # 포지션이 이미 없음 -> 서버에서 SL/TP 체결됨
            logger.info("거래소에 포지션 없음 - 서버 SL/TP 체결로 처리")
            actual_exit_price, exit_fee = self._get_server_triggered_execution(
                direction=direction,
                entry_timestamp=entry_timestamp
            )
            
            if actual_exit_price is None:
                ticker = self.client.get_ticker(TRADING.SYMBOL)
                actual_exit_price = ticker.get("last_price", entry_price)
                logger.warning(f"서버 체결 내역 조회 실패, ticker 폴백: {actual_exit_price}")
                
                self.telegram.info(
                    f"[METIS-F] 서버 SL/TP 체결 내역 조회 실패\n"
                    f"ticker 폴백 사용: {actual_exit_price:,.2f} USDT\n"
                    f"실제 체결가와 차이 발생 가능"
                )
        
        # 5. 펀딩비 조회 (진입 시점 이후 발생분)
        funding_fee = 0.0
        try:
            entry_time_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000)
            funding_fee = self.client.get_total_funding_fee_for_position(
                symbol=TRADING.SYMBOL,
                entry_time_ms=entry_time_ms
            )
        except Exception as e:
            logger.warning(f"펀딩비 조회 실패: {e}")
        
        # 6. 총 비용 계산 (거래 수수료 + 펀딩비)
        # funding_fee: 양수=수취(이익), 음수=지불(비용)
        trade_fee = entry_fee + exit_fee
        total_cost = trade_fee - funding_fee  # 펀딩비 수취 시 비용 감소
        
        # 7. 손익 계산 (비용 차감)
        gross_pnl, gross_pnl_pct = calculate_pnl(
            entry_price, actual_exit_price, quantity, direction, leverage
        )
        net_pnl = gross_pnl - total_cost
        
        margin_used = (quantity * entry_price) / leverage
        net_pnl_pct = (net_pnl / margin_used) * 100 if margin_used > 0 else 0
        
        # 8. DB 업데이트
        self.db.close_position(
            position_uuid=position_uuid,
            exit_price=actual_exit_price,
            exit_reason=reason,
            realized_pnl=net_pnl,
            realized_pnl_percentage=round(net_pnl_pct, 2),
            exit_fee=exit_fee
        )
        
        # 9. 펀딩비 DB 기록 (이합으로 1건 기록)
        if funding_fee != 0:
            self.db.log_funding(
                position_uuid=position_uuid,
                funding_rate=0,  # 이합이므로 개별 rate 없음
                funding_fee=funding_fee
            )
        
        # 10. 보유 시간 계산
        entry_dt = datetime.fromisoformat(entry_timestamp)
        hold_time = datetime.now() - entry_dt
        hours = hold_time.total_seconds() / 3600
        hold_time_str = f"{int(hours)}시간 {int((hours % 1) * 60)}분"
        
        # 11. 텔레그램 알림
        self.telegram.send_position_closed(
            direction=direction,
            reason=reason,
            entry_price=entry_price,
            exit_price=actual_exit_price,
            pnl=net_pnl,
            pnl_pct=net_pnl_pct,
            hold_time=hold_time_str,
            total_fee=total_cost,
            funding_fee=funding_fee
        )
        
        logger.info(
            f"포지션 청산 완료: 체결가={actual_exit_price} "
            f"순PnL={net_pnl:+.2f} USDT ({net_pnl_pct:+.2f}%) "
            f"거래수수료={trade_fee:.4f} 펀딩비={funding_fee:+.4f}"
        )
        
        return {
            "success": True,
            "position_uuid": position_uuid,
            "exit_price": actual_exit_price,
            "reason": reason,
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "pnl_pct": net_pnl_pct,
            "hold_time": hold_time_str,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "funding_fee": funding_fee,
            "total_cost": total_cost
        }
    
    def _handle_liquidation_close(
        self,
        position_uuid: str,
        db_position: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        강제청산 처리 (부분 체결 합산)
        
        거래소에서 이미 청산되었으므로 체결 내역에서 정보 조회
        """
        direction = db_position["direction"]
        entry_price = db_position["entry_price"]
        quantity = db_position["entry_quantity"]
        leverage = db_position["leverage"]
        entry_timestamp = db_position["entry_timestamp"]
        entry_fee = db_position.get("entry_fee", 0)
        liquidation_price = db_position["liquidation_price"]
        
        logger.warning(f"강제청산 처리: {position_uuid[:8]}...")
        
        # 1. 최근 체결 내역에서 청산 정보 조회 (부분 체결 합산)
        actual_exit_price = liquidation_price
        exit_fee = 0.0
        
        try:
            executions = self.client.get_execution_list(
                symbol=TRADING.SYMBOL,
                limit=50
            )
            
            # 진입 시각 파싱
            try:
                entry_time_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000)
            except ValueError:
                entry_time_ms = 0
            
            close_side = "Sell" if direction == "LONG" else "Buy"
            
            # 부분 체결 합산
            total_qty = 0.0
            total_value = 0.0
            total_fee = 0.0
            
            for ex in executions:
                exec_time = int(ex.get("exec_time", 0))
                exec_side = ex.get("side")
                
                if entry_time_ms > 0 and exec_time < entry_time_ms:
                    continue
                
                if exec_side == close_side:
                    exec_qty = float(ex.get("exec_qty", 0))
                    exec_price = float(ex.get("exec_price", 0))
                    exec_fee_val = float(ex.get("exec_fee", 0))
                    
                    total_qty += exec_qty
                    total_value += exec_qty * exec_price
                    total_fee += exec_fee_val
            
            if total_qty > 0 and total_value > 0:
                actual_exit_price = total_value / total_qty
                exit_fee = total_fee
                logger.info(
                    f"강제청산 체결 확인: 체결건수 합산, "
                    f"평균가={actual_exit_price:.2f} 총수수료={exit_fee:.6f}"
                )
                
        except Exception as e:
            logger.error(f"강제청산 체결 내역 조회 실패: {e}")
        
        # 2. 펀딩비 조회
        funding_fee = 0.0
        try:
            entry_time_ms = int(datetime.fromisoformat(entry_timestamp).timestamp() * 1000)
            funding_fee = self.client.get_total_funding_fee_for_position(
                symbol=TRADING.SYMBOL,
                entry_time_ms=entry_time_ms
            )
        except Exception as e:
            logger.warning(f"펀딩비 조회 실패: {e}")
        
        # 3. 총 비용 계산
        trade_fee = entry_fee + exit_fee
        total_cost = trade_fee - funding_fee
        
        # 4. 손익 계산
        gross_pnl, gross_pnl_pct = calculate_pnl(
            entry_price, actual_exit_price, quantity, direction, leverage
        )
        net_pnl = gross_pnl - total_cost
        
        margin_used = (quantity * entry_price) / leverage
        net_pnl_pct = (net_pnl / margin_used) * 100 if margin_used > 0 else 0
        
        # 5. DB 업데이트
        self.db.close_position(
            position_uuid=position_uuid,
            exit_price=actual_exit_price,
            exit_reason="LIQUIDATION",
            realized_pnl=net_pnl,
            realized_pnl_percentage=round(net_pnl_pct, 2),
            exit_fee=exit_fee
        )
        
        # 6. 펀딩비 DB 기록
        if funding_fee != 0:
            self.db.log_funding(
                position_uuid=position_uuid,
                funding_rate=0,
                funding_fee=funding_fee
            )
        
        # 7. 보유 시간 계산
        entry_dt = datetime.fromisoformat(entry_timestamp)
        hold_time = datetime.now() - entry_dt
        hours = hold_time.total_seconds() / 3600
        hold_time_str = f"{int(hours)}시간 {int((hours % 1) * 60)}분"
        
        # 8. 긴급 텔레그램 알림
        funding_text = f"펀딩비: {funding_fee:+.4f} USDT\n" if funding_fee != 0 else ""
        
        self.telegram.emergency(
            f"[METIS-F] 강제청산 발생!\n\n"
            f"방향: {direction}\n"
            f"진입가: {entry_price:,.2f} USDT\n"
            f"청산가: {actual_exit_price:,.2f} USDT\n"
            f"보유 시간: {hold_time_str}\n\n"
            f"실현 손실: {net_pnl:,.2f} USDT ({net_pnl_pct:+.2f}%)\n"
            f"거래 수수료: {trade_fee:.4f} USDT\n"
            f"{funding_text}\n"
            f"리스크 관리 점검 필요!"
        )
        
        logger.error(
            f"강제청산 완료: 체결가={actual_exit_price} "
            f"순PnL={net_pnl:+.2f} USDT ({net_pnl_pct:+.2f}%) "
            f"펀딩비={funding_fee:+.4f}"
        )
        
        return {
            "success": True,
            "position_uuid": position_uuid,
            "exit_price": actual_exit_price,
            "reason": "LIQUIDATION",
            "gross_pnl": gross_pnl,
            "net_pnl": net_pnl,
            "pnl_pct": net_pnl_pct,
            "hold_time": hold_time_str,
            "entry_fee": entry_fee,
            "exit_fee": exit_fee,
            "funding_fee": funding_fee,
            "total_cost": total_cost
        }
    
    def get_current_position(self) -> Optional[Dict[str, Any]]:
        """
        현재 활성 포지션 조회 (거래소 + DB 통합)
        """
        exchange_pos = self.client.get_position(TRADING.SYMBOL)
        db_pos = self.db.get_active_position()
        
        if not exchange_pos:
            if db_pos:
                logger.warning("DB에는 활성 포지션이 있지만 거래소에 없음")
            return None
        
        return {
            "position_uuid": db_pos["position_uuid"] if db_pos else None,
            "direction": "LONG" if exchange_pos["side"] == "Buy" else "SHORT",
            "leverage": exchange_pos["leverage"],
            "entry_price": exchange_pos["entry_price"],
            "quantity": exchange_pos["size"],
            "mark_price": exchange_pos["mark_price"],
            "liquidation_price": exchange_pos["liquidation_price"],
            "unrealized_pnl": exchange_pos["unrealized_pnl"],
            "stop_loss": db_pos["stop_loss_price"] if db_pos else None,
            "take_profit": db_pos["take_profit_price"] if db_pos else None,
            "entry_fee": db_pos.get("entry_fee", 0) if db_pos else 0
        }
    
    def has_active_position(self) -> bool:
        """활성 포지션 존재 여부"""
        return self.client.get_position(TRADING.SYMBOL) is not None


# 싱글톤 인스턴스
position_manager = PositionManager()