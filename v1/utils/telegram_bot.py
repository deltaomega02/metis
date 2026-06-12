# utils/telegram_bot.py
# 텔레그램 알림 관리자 (HTTP 요청 방식)

import requests
from enum import Enum
from typing import Optional, List, Dict, Any

from config import TELEGRAM, get_logger

logger = get_logger("telegram")


class AlertPriority(Enum):
    """알림 우선순위"""
    P0_EMERGENCY = "emergency"  # 청산 근접, WebSocket 끊김, 시스템 오류
    P1_TRADE = "trade"          # 포지션 진입/청산
    P2_INFO = "info"            # AI 분석 결과, 중간 점검
    P3_STATUS = "status"        # 시스템 시작/종료, 일일 리포트


class TelegramNotifier:
    """
    텔레그램 알림 발송기 (Requests 사용 버전)
    """
    
    def __init__(self):
        self.bot_token = TELEGRAM.BOT_TOKEN
        self.chat_id = TELEGRAM.CHAT_ID
        self.base_url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
        
        if not self.bot_token or not self.chat_id:
            logger.warning("텔레그램 설정 누락: 토큰 또는 채팅 ID가 없습니다.")

    def _send_request(self, message: str, parse_mode: str = None) -> bool:
        """HTTP 요청으로 메시지 전송"""
        if not self.bot_token or not self.chat_id:
            logger.warning("텔레그램 봇이 설정되지 않음")
            return False
            
        try:
            payload = {
                "chat_id": self.chat_id,
                "text": message
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
                
            response = requests.post(self.base_url, json=payload, timeout=5)
            response.raise_for_status()
            return True
            
        except Exception as e:
            logger.error(f"텔레그램 전송 실패: {e}")
            return False

    def send(
        self,
        message: str,
        priority: AlertPriority = AlertPriority.P2_INFO,
        parse_mode: str = None
    ) -> bool:
        """알림 전송"""
        if priority == AlertPriority.P0_EMERGENCY:
            message = f"🚨🚨🚨\n{message}\n🚨🚨🚨"
        
        return self._send_request(message, parse_mode)
    
    # ========== 편의 메서드 ==========
    
    def emergency(self, message: str) -> bool:
        """P0 긴급 알림"""
        return self.send(message, AlertPriority.P0_EMERGENCY)
    
    def trade(self, message: str) -> bool:
        """P1 거래 알림"""
        return self.send(message, AlertPriority.P1_TRADE)
    
    def info(self, message: str) -> bool:
        """P2 정보 알림"""
        return self.send(message, AlertPriority.P2_INFO)
    
    def status(self, message: str) -> bool:
        """P3 상태 알림"""
        return self.send(message, AlertPriority.P3_STATUS)
    
    def _format_next_time(self, hours: float) -> str:
        """시간을 가독성 있게 포맷팅 + 예정 시각 표시"""
        from datetime import datetime, timedelta
        
        if hours < 1:
            duration_text = f"{int(hours * 60)}분"
        elif hours == int(hours):
            duration_text = f"{int(hours)}시간"
        else:
            duration_text = f"{hours:.1f}시간"
        
        next_time = datetime.now() + timedelta(hours=hours)
        time_text = next_time.strftime("%m/%d %H:%M")
        
        return f"{duration_text} ({time_text})"

    # ========== 시스템 알림 ==========
    
    def send_system_start(
        self, 
        balance: float,
        position_info: Optional[Dict[str, Any]] = None,
        is_restart: bool = False
    ) -> bool:
        """시스템 시작/재시작 알림"""
        from config import BYBIT
        
        env = "Testnet" if BYBIT.USE_TESTNET else "Production"
        title = "시스템 재시작" if is_restart else "시스템 시작"
        
        if position_info:
            direction = position_info.get("direction", "?")
            leverage = position_info.get("leverage", 0)
            entry_price = position_info.get("entry_price", 0)
            unrealized_pnl = position_info.get("unrealized_pnl", 0)
            
            position_text = f"""{direction} {leverage}x
- 진입가: {entry_price:,.2f} USDT
- 미실현 PnL: {unrealized_pnl:+.2f} USDT"""
            next_action = "Phase 4 감시 재개..."
        else:
            position_text = "없음"
            next_action = "Phase 1 시작..."
        
        message = f"""[METIS-F] {title}

버전: 1.0.0
환경: {env}

계정 상태:
- 잔고: {balance:.2f} USDT
- 활성 포지션: {position_text}

{next_action}"""
        return self.status(message)
    
    def send_system_error(self, error_type: str, error_msg: str, location: str) -> bool:
        """시스템 오류 알림"""
        message = f"""[METIS-F] [긴급] 시스템 오류 발생

오류 유형: {error_type}
발생 위치: {location}
오류 메시지: {error_msg}

수동 확인 필요!"""
        return self.emergency(message)
    
    # ========== Phase 2: AI 분석 ==========
    
    def send_analysis_result(
        self,
        decision: str,
        direction: Optional[str] = None,
        confidence: Optional[int] = None,
        leverage: Optional[int] = None,
        wait_hours: Optional[float] = None,
        reason: str = ""
    ) -> bool:
        """Phase 2 AI 분석 결과 알림"""
        if decision == "TRADE":
            # 레버리지는 Phase 3에서 AI가 결정하므로 Phase 2에서는 표시하지 않을 수 있음
            leverage_text = f"\n레버리지: {leverage}x" if leverage is not None else ""
            
            message = f"""[METIS-F] AI 분석 완료 - TRADE

방향: {direction}
확신도: {confidence}/10{leverage_text}

AI 판단 근거:
{reason}

Phase 3 전략 수립 중..."""
        else:
            if wait_hours is not None:
                wait_text = self._format_next_time(wait_hours)
            else:
                wait_text = "미정"
            
            message = f"""[METIS-F] AI 분석 완료 - WAIT

AI 판단 근거:
{reason}

다음 분석: {wait_text}
(지표 감시 활성화 - 트리거 시 조기 재분석)"""
        
        return self.info(message) 
    
    # ========== Phase 3: 전략 수립 ==========
    
    def send_strategy_complete(
        self,
        direction: str,
        leverage: int,
        entry_price: float,
        stop_loss: float,
        take_profit: float,
        liquidation: float,
        position_size: float,
        rr_ratio: float
    ) -> bool:
        """Phase 3 전략 수립 완료 알림"""
        sl_pct = abs((stop_loss - entry_price) / entry_price * 100)
        tp_pct = abs((take_profit - entry_price) / entry_price * 100)
        liq_pct = abs((liquidation - entry_price) / entry_price * 100)
        
        message = f"""[METIS-F] 전략 수립 완료

방향: {direction}
레버리지: {leverage}x

진입 예정가: {entry_price:,.0f} USDT
손절가: {stop_loss:,.0f} USDT ({'-' if direction == 'LONG' else '+'}{sl_pct:.1f}%)
익절가: {take_profit:,.0f} USDT ({'+' if direction == 'LONG' else '-'}{tp_pct:.1f}%)
청산가: {liquidation:,.0f} USDT ({liq_pct:.1f}%)

포지션 크기: {position_size:.2f} USDT
R:R 비율: 1:{rr_ratio:.2f}

시장가 진입 실행 중..."""
        return self.trade(message)
    
    # ========== Phase 4: 포지션 관리 ==========
    
    def send_position_opened(
        self,
        direction: str,
        leverage: int,
        entry_price: float,
        qty: float,
        stop_loss: float,
        take_profit: float,
        margin_used: float,
        first_recheck_hours: float = 6,
        entry_fee: float = 0
    ) -> bool:
        """Phase 4 포지션 진입 완료 알림"""
        message = f"""[METIS-F] 포지션 오픈 완료

방향: {direction}
레버리지: {leverage}x
체결가: {entry_price:,.2f} USDT
수량: {qty:.6f} BTC

손절가: {stop_loss:,.0f} USDT
익절가: {take_profit:,.0f} USDT

마진 사용: {margin_used:.2f} USDT
진입 수수료: {entry_fee:.4f} USDT

WebSocket 감시 시작...
첫 중간점검: {self._format_next_time(first_recheck_hours)}"""
        return self.trade(message)
    
    def send_position_closed(
        self,
        direction: str,
        reason: str,
        entry_price: float,
        exit_price: float,
        pnl: float,
        pnl_pct: float,
        hold_time: str,
        total_fee: float = 0,
        funding_fee: float = 0
    ) -> bool:
        """포지션 청산 완료 알림"""
        result_emoji = "✅" if pnl >= 0 else "❌"
        
        reason_map = {
            "TAKE_PROFIT": "익절",
            "STOP_LOSS": "손절",
            "AI_EXIT": "AI 청산",
            "DEAD_MANS_SWITCH": "긴급 청산",
            "PROFIT_GUARD": "수익 보호 청산",
            "LIQUIDATION": "강제청산"
        }
        result_text = reason_map.get(reason, reason)
        
        price_change_pct = ((exit_price - entry_price) / entry_price) * 100
        
        funding_text = ""
        if funding_fee != 0:
            funding_action = "수취" if funding_fee > 0 else "지불"
            funding_emoji = "📥" if funding_fee > 0 else "📤"
            funding_text = f"\n펀딩비 {funding_action}: {abs(funding_fee):.4f} USDT {funding_emoji}"
        
        message = f"""[METIS-F] 포지션 청산 - {result_text} {result_emoji}

방향: {direction}
보유 시간: {hold_time}

진입가: {entry_price:,.2f} USDT
청산가: {exit_price:,.2f} USDT
가격 변동: {price_change_pct:+.2f}%

거래 수수료: {total_fee - abs(funding_fee) if funding_fee < 0 else total_fee + funding_fee:.4f} USDT{funding_text}
총 비용: {total_fee:.4f} USDT
순 손익: {pnl:+.2f} USDT ({pnl_pct:+.2f}%)"""
        return self.trade(message)
    
    # ========== Phase 4: 중간 점검 ==========
    
    def send_recheck_hold(
        self,
        recheck_number: int,
        elapsed_hours: float,
        current_price: float,
        pnl_pct: float,
        reason: str,
        next_recheck_hours: float
    ) -> bool:
        """중간 점검 결과 - HOLD"""
        message = f"""[METIS-F] 중간 점검 #{recheck_number} - HOLD

경과 시간: {elapsed_hours:.1f}시간
현재가: {current_price:,.0f} USDT
미실현 손익: {pnl_pct:+.2f}%

AI 판단: 유지
{reason}

다음 점검: {self._format_next_time(next_recheck_hours)}"""
        return self.info(message)
    
    def send_recheck_modify(
        self,
        recheck_number: int,
        elapsed_hours: float,
        current_price: float,
        pnl_pct: float,
        reason: str,
        new_stop_loss: Optional[float] = None,
        new_take_profit: Optional[float] = None,
        next_recheck_hours: float = 6
    ) -> bool:
        """중간 점검 결과 - MODIFY"""
        changes = []
        if new_stop_loss is not None:
            changes.append(f"- 손절가: {new_stop_loss:,.0f} USDT")
        if new_take_profit is not None:
            changes.append(f"- 익절가: {new_take_profit:,.0f} USDT")
        
        changes_text = "\n".join(changes) if changes else "- 변경 없음"
        
        message = f"""[METIS-F] 중간 점검 #{recheck_number} - MODIFY

경과 시간: {elapsed_hours:.1f}시간
현재가: {current_price:,.0f} USDT
미실현 손익: {pnl_pct:+.2f}%

AI 판단: 전략 수정
{reason}

변경 사항:
{changes_text}

다음 점검: {self._format_next_time(next_recheck_hours)}"""
        return self.info(message)
    
    def send_recheck_exit(
        self,
        recheck_number: int,
        elapsed_hours: float,
        current_price: float,
        pnl_pct: float,
        reason: str
    ) -> bool:
        """중간 점검 결과 - EXIT"""
        message = f"""[METIS-F] 중간 점검 #{recheck_number} - EXIT

경과 시간: {elapsed_hours:.1f}시간
현재가: {current_price:,.0f} USDT
미실현 손익: {pnl_pct:+.2f}%

AI 판단: 즉시 청산
{reason}

시장가 청산 실행 중..."""
        return self.info(message)
    
    # ========== Profit Guard ==========
    
    def send_profit_guard_triggered(
        self,
        direction: str,
        unrealized_pnl_pct: float,
        current_price: float,
        reason: str
    ) -> bool:
        """Profit Guard 추세 반전 감지 알림"""
        message = f"""[METIS-F] Profit Guard 발동 🛡️

방향: {direction}
현재가: {current_price:,.2f} USDT
미실현 수익률: {unrealized_pnl_pct:+.2f}%

반전 신호:
{reason}

수익 보호 시장가 청산 실행 중..."""
        return self.trade(message)
    
    def send_profit_guard_status(self, active: bool, pnl_pct: float) -> bool:
        """Profit Guard 활성화/비활성화 상태 알림 (디버그용)"""
        status = "활성화" if active else "비활성화"
        message = f"""[METIS-F] Profit Guard {status}

미실현 수익률: {pnl_pct:+.2f}%"""
        return self.info(message)
    
    # ========== Trigger Monitor ==========
    
    def send_trigger_activated(
        self,
        trigger_reason: str,
        current_price: float = 0
    ) -> bool:
        """Trigger Monitor 트리거 발동 알림"""
        price_text = f"\n현재가: {current_price:,.0f} USDT" if current_price > 0 else ""
        
        message = f"""[METIS-F] 지표 트리거 발동 ⚡{price_text}

감지 신호:
{trigger_reason}

대기 조기 종료 → 즉시 재분석 시작..."""
        return self.info(message)
    
    # ========== 긴급 알림 ==========
    
    def send_liquidation_warning(
        self,
        current_price: float,
        liquidation_price: float,
        distance_pct: float
    ) -> bool:
        """청산 근접 경고"""
        message = f"""[METIS-F] [긴급] 청산 근접 경고

현재가가 청산가에 접근 중!

현재가: {current_price:,.2f} USDT
청산가: {liquidation_price:,.2f} USDT
거리: {distance_pct:.2f}%

수동 확인 필요!"""
        return self.emergency(message)
    
    def send_websocket_error(self, last_data_seconds: int) -> bool:
        """WebSocket 끊김 알림"""
        message = f"""[METIS-F] [긴급] WebSocket 연결 이상

마지막 데이터 수신: {last_data_seconds}초 전

REST API 폴백 실행 중..."""
        return self.emergency(message)
    
    def send_dead_mans_switch_result(
        self,
        current_price: float,
        position_ok: bool,
        action_taken: Optional[str] = None
    ) -> bool:
        """Dead Man's Switch 실행 결과"""
        if position_ok:
            message = f"""[METIS-F] Dead Man's Switch 결과

REST API 조회 완료

현재가: {current_price:,.0f} USDT
포지션 상태: 정상
손절/익절 조건: 미충족

WebSocket 재연결 시도 중..."""
            return self.info(message)
        else:
            message = f"""[METIS-F] Dead Man's Switch 작동

REST API 조회 완료

현재가: {current_price:,.0f} USDT
조치: {action_taken}

WebSocket 끊김 상태에서 조건 충족.
긴급 청산 실행."""
            return self.emergency(message)
    
    # ========== 일일 리포트 ==========
    
    def send_daily_report(
        self,
        date: str,
        total_trades: int,
        wins: int,
        losses: int,
        win_rate: float,
        total_pnl: float,
        total_fees: float,
        recent_trades: List[Dict[str, Any]],
        balance: float,
        position_status: str
    ) -> bool:
        """일일 리포트 발송"""
        recent_text = ""
        for i, trade in enumerate(recent_trades[:5], 1):
            pnl = trade.get("realized_pnl", 0)
            emoji = "✅" if pnl >= 0 else "❌"
            direction = trade.get("direction", "?")
            recent_text += f"  {i}. {direction} {pnl:+.2f} USDT {emoji}\n"
        
        if not recent_text:
            recent_text = "  거래 내역 없음\n"
        
        message = f"""[METIS-F] 일일 리포트 ({date})

📊 거래 요약 (7일):
- 총 거래: {total_trades}회
- 승/패: {wins}승 {losses}패 ({win_rate:.1f}%)
- 누적 PnL: {total_pnl:+.2f} USDT
- 총 수수료: {total_fees:.4f} USDT

📋 최근 거래:
{recent_text}
💰 현재 상태:
- 잔고: {balance:.2f} USDT
- 활성 포지션: {position_status}"""
        return self.status(message)
    
    # ========== 펀딩비 알림 ==========
    
    def send_funding_fee(
        self,
        direction: str,
        funding_rate: float,
        funding_fee: float,
        cumulative_fee: float
    ) -> bool:
        """펀딩비 정산 알림"""
        action = "지불" if funding_fee < 0 else "수취"
        emoji = "📤" if funding_fee < 0 else "📥"
        
        message = f"""[METIS-F] 펀딩비 정산 {emoji}

포지션: {direction}
펀딩비율: {funding_rate:.4f}%
{action}: {abs(funding_fee):.4f} USDT

누적 펀딩비: {cumulative_fee:+.4f} USDT"""
        return self.info(message)


# 싱글톤 인스턴스
telegram_notifier = TelegramNotifier()