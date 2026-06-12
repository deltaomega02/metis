# main.py
# METIS-F 엔트리 포인트
# Ver X: 레짐 기반 전략 + AI 필터
# Phase 1(데이터) → Phase 2(레짐 판단, 코드) → Phase 3(시그널+AI필터) → Phase 4(실행/감시)

import sys
import signal
import time
import gc
import json
import threading
import numpy as np
from datetime import datetime
from typing import Optional, Dict, Any

from config import (
    setup_logging,
    get_logger,
    TRADING,
    SCHEDULER,
    PROFIT_GUARD,
    TRIGGER_MONITOR,
)
from exchange import bybit_client
from core import (
    data_fetcher,
    position_manager,
    FuturesWatcher,
    PositionRecheckScheduler,  
    DailyReportScheduler       
)
from core.leverage_calculator import validate_ai_strategy
from core.regime_engine import determine_regime, generate_signal, SignalType
from ai import gemini_client
from database import db_manager
from utils import telegram_notifier
from core.trigger_monitor import TriggerMonitor
from core.technical_analysis import (
    calculate_profit_guard_indicators, detect_trend_reversal
)

# 로깅 설정
setup_logging()
logger = get_logger("main")


class NumpyEncoder(json.JSONEncoder):
    """NumPy 타입을 JSON 직렬화 가능하게 변환"""
    def default(self, obj):
        if isinstance(obj, np.bool_):
            return bool(obj)
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super().default(obj)


class MetisFutures:
    """
    METIS-F 메인 컨트롤러
    
    4단계 순환 구조로 작동
    """
    
    def __init__(self):
        self.running = False
        self.watcher: Optional[FuturesWatcher] = None
        self.current_position_uuid: Optional[str] = None
        self.current_strategy: Optional[Dict[str, Any]] = None
        
        # 중간 점검 카운터
        self.recheck_count: int = 0
        
        # Profit Guard 스레드
        self._profit_guard_thread: Optional[threading.Thread] = None
        self._profit_guard_running = False

        # Trigger Monitor (WAIT 대기 중 지표 감시)
        self.trigger_monitor = TriggerMonitor()
        
        # 연속 WAIT 카운터 (반복 WAIT 시 텔레그램 알림 억제용)
        self._consecutive_wait_count: int = 0

        # 스케줄러 초기화
        self.recheck_scheduler = PositionRecheckScheduler(
            on_recheck_callback=self._run_position_recheck
        )
        self.daily_report_scheduler = DailyReportScheduler(
            on_report_callback=self._send_daily_report,
            hour=SCHEDULER.DAILY_REPORT_HOUR,
            minute=SCHEDULER.DAILY_REPORT_MINUTE
        )
        
        # 시그널 핸들러 등록
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
    
    def _signal_handler(self, signum, frame):
        """Graceful shutdown"""
        logger.info(f"시그널 수신: {signum}. 종료 중...")
        self.running = False
        
        if self.watcher:
            self.watcher.stop()
        
        self.trigger_monitor.stop()
        self._stop_profit_guard()
        self.recheck_scheduler.cancel()
        self.daily_report_scheduler.stop()
        
        telegram_notifier.status("[METIS-F Ver X] 시스템 종료")
        sys.exit(0)
    
    def start(self):
        """메인 루프 시작"""
        logger.info("=" * 50)
        logger.info("METIS-F Ver X 시작")
        logger.info("=" * 50)
        
        self.running = True
        
        # 잔고 확인
        balance_info = bybit_client.get_wallet_balance()
        balance = balance_info.get("available_balance", 0)
        
        logger.info(f"계정 잔고: {balance:.2f} USDT")
        
        # 일일 리포트 스케줄러 시작
        self.daily_report_scheduler.start()

        # 기존 포지션 확인
        position_info = None
        is_restart = False

        if position_manager.has_active_position():
            logger.info("기존 활성 포지션 발견. Phase 4로 진입.")
            position_info = position_manager.get_current_position()
            is_restart = True
            self._resume_monitoring()
        else:
            logger.info("활성 포지션 없음. Phase 1부터 시작.")

        # 시작 알림 (포지션 정보 포함)
        telegram_notifier.send_system_start(balance, position_info, is_restart)
        
        # 메인 루프
        while self.running:
            try:
                if not position_manager.has_active_position():
                    self._run_analysis_cycle()
                else:
                    # 포지션 있을 때는 대기 (WebSocket + Scheduler가 감시 중)
                    time.sleep(60)
            
            except KeyboardInterrupt:
                break
            
            except Exception as e:
                logger.error(f"메인 루프 오류: {e}", exc_info=True)
                telegram_notifier.send_system_error("MAIN_LOOP", str(e), "main.py")
                time.sleep(60)
    
    def _run_analysis_cycle(self):
        """Ver X: 레짐 기반 분석 사이클
        
        Phase 1: 데이터 수집 (기존 동일)
        Phase 2: 레짐 판단 (코드 기반, AI 불필요)
        Phase 3: 전략 시그널 생성 (코드) + AI 필터 (PASS/REJECT)
        Phase 4: 포지션 진입 (기존 동일)
        """
        
        # Phase 1: Data Collection
        logger.info("=" * 40)
        logger.info("Phase 1: 데이터 수집")
        logger.info("=" * 40)
        
        try:
            data = data_fetcher.collect_all_data()
            ai_input = data_fetcher.prepare_ai_input(data)
            
        except Exception as e:
            logger.error(f"Phase 1 실패: {e}")
            telegram_notifier.send_system_error("DATA_FETCH", str(e), "Phase 1")
            time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
            return
        
        # Phase 2: 레짐 판단 (코드 기반)
        logger.info("=" * 40)
        logger.info("Phase 2: 레짐 판단 (Ver X)")
        logger.info("=" * 40)
        
        try:
            # 1H 지표 추출
            indicators_1h = ai_input.get("indicators", {}).get("current", {})
            
            # 4H 요약 (있으면)
            tf_4h = ai_input.get("timeframe_4h", {})
            
            # 레짐 판단
            regime = determine_regime(indicators_1h, tf_4h)
            
            logger.info(f"레짐: {regime.regime.value} (확신도={regime.confidence})")
            logger.info(f"근거: {regime.details.get('reason', '')}")
            
        except Exception as e:
            logger.error(f"Phase 2 레짐 판단 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("REGIME", str(e), "Phase 2")
            time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
            return
        
        # Phase 3: 전략 시그널 + AI 필터
        logger.info("=" * 40)
        logger.info("Phase 3: 전략 시그널 (Ver X)")
        logger.info("=" * 40)
        
        try:
            # 시그널 생성 (코드 기반)
            signal = generate_signal(regime, indicators_1h)
            
            logger.info(f"시그널: {signal.signal.value} (점수={signal.score})")
            logger.info(f"사유: {signal.reason}")
            
            if signal.signal == SignalType.WAIT:
                self._consecutive_wait_count += 1
                wait_minutes = 5  # 5분 후 재스캔
                wait_hours = wait_minutes / 60
                
                # 첫 WAIT 또는 레짐 변경 시에만 텔레그램 알림
                if self._consecutive_wait_count == 1:
                    telegram_notifier.send_analysis_result(
                        decision="WAIT",
                        reason=f"[{regime.regime.value}] {signal.reason}",
                        wait_hours=wait_hours
                    )
                
                # 로그는 항상 (텔레그램은 억제)
                logger.info(
                    f"WAIT #{self._consecutive_wait_count}: [{regime.regime.value}] {signal.reason} "
                    f"(다음 스캔 {wait_minutes}분 후)"
                )
                
                gc.collect()
                
                # 트리거 모니터 대기 (5분, 급변 시 즉시 깨어남)
                trigger_result = self.trigger_monitor.wait_for_trigger(wait_hours)
                if trigger_result:
                    logger.info(f"트리거 발동: {trigger_result} → 즉시 재분석")
                    telegram_notifier.send_trigger_activated(trigger_result)
                return
            
            # LONG 또는 SHORT 시그널 → AI 필터 호출
            direction = signal.signal.value
            
            logger.info(f"AI 진입 필터 호출: {regime.regime.value} {direction}")
            
            filter_result = gemini_client.filter_entry(
                market_data=ai_input,
                regime=regime.regime.value,
                direction=direction,
                signal_reason=signal.reason,
                signal_score=signal.score
            )
            
            if filter_result.get("decision") == "REJECT":
                reject_reason = filter_result.get("reason", "AI 필터 거부")
                wait_hours = 0.25  # 15분 후 재시도
                
                logger.info(f"AI 필터 REJECT: {reject_reason} (15분 후 재스캔)")
                
                telegram_notifier.send_analysis_result(
                    decision="WAIT",
                    reason=f"[AI필터 거부] {reject_reason}",
                    wait_hours=wait_hours
                )
                
                gc.collect()
                
                trigger_result = self.trigger_monitor.wait_for_trigger(wait_hours)
                if trigger_result:
                    logger.info(f"트리거 발동: {trigger_result} → 즉시 재분석")
                    telegram_notifier.send_trigger_activated(trigger_result)
                return
            
            # AI 필터 PASS
            logger.info(f"AI 필터 PASS: {filter_result.get('reason', '')}")
            if filter_result.get("risk_note"):
                logger.info(f"AI 리스크 노트: {filter_result['risk_note']}")
            
            leverage = signal.leverage
            
            # 연속 WAIT 카운터 리셋
            self._consecutive_wait_count = 0
            
            # 텔레그램 알림 (TRADE 결정)
            telegram_notifier.send_analysis_result(
                decision="TRADE",
                direction=direction,
                confidence=regime.confidence,
                leverage=leverage,
                reason=f"[{regime.regime.value}] {signal.reason}"
            )
            
        except Exception as e:
            logger.error(f"Phase 3 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("SIGNAL", str(e), "Phase 3")
            
            gc.collect()
            
            time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
            return
        
        # Phase 3.5: 전략 검증 (기존 validate_ai_strategy 재활용)
        logger.info("=" * 40)
        logger.info("Phase 3.5: 전략 검증")
        logger.info("=" * 40)
        
        try:
            # 잔고 재확인
            balance_info = bybit_client.get_wallet_balance()
            balance = balance_info.get("available_balance", 0)
            
            if balance < 1:
                logger.warning(f"잔고 부족: {balance:.2f} USDT")
                telegram_notifier.info(f"[METIS-F] 잔고 부족: {balance:.2f} USDT")
                time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
                return
            
            # SL/TP 절대가 계산 (regime_engine이 비율(%)로 산출)
            current_price = ai_input["futures"]["last_price"]
            sl_pct = signal.stop_loss_pct / 100
            tp_pct = signal.take_profit_pct / 100
            
            if direction == "LONG":
                sl_price = current_price * (1 - sl_pct)
                tp_price = current_price * (1 + tp_pct)
            else:
                sl_price = current_price * (1 + sl_pct)
                tp_price = current_price * (1 - tp_pct)
            
            # 안전성 검증
            strategy = validate_ai_strategy(
                current_price=current_price,
                balance=balance,
                direction=direction,
                leverage=leverage,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
            )
            
            if not strategy.get("valid"):
                logger.warning(f"전략 검증 실패: {strategy.get('reason')}")
                telegram_notifier.info(f"[METIS-F] 전략 검증 실패: {strategy.get('reason')}")
                time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
                return
            
            # 추가 정보
            strategy["ai_reason"] = f"[Ver X] {regime.regime.value} | {signal.reason}"
            strategy["estimated_time_hours"] = 24
            strategy["first_recheck_hours"] = 2
            
            logger.info(
                f"전략 확정: {direction} {strategy['leverage']}x "
                f"SL={strategy['stop_loss_price']:.0f} TP={strategy['take_profit_price']:.0f} "
                f"({strategy['stop_loss_pct']:.1f}%/{strategy['take_profit_pct']:.1f}%)"
            )
            
            telegram_notifier.send_strategy_complete(
                direction=strategy["direction"],
                leverage=strategy["leverage"],
                entry_price=strategy["entry_price"],
                stop_loss=strategy["stop_loss_price"],
                take_profit=strategy["take_profit_price"],
                liquidation=strategy["liquidation_price"],
                position_size=strategy["position_size_usdt"],
                rr_ratio=strategy["risk_reward_ratio"]
            )
            
        except Exception as e:
            logger.error(f"전략 검증 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("STRATEGY", str(e), "Phase 3.5")
            time.sleep(SCHEDULER.ANALYSIS_INTERVAL_HOURS * 3600)
            return
        
        finally:
            gc.collect()
        
        # Phase 4: Execution (기존과 동일)
        logger.info("=" * 40)
        logger.info("Phase 4: 포지션 진입")
        logger.info("=" * 40)
        
        try:
            result = position_manager.open_position(strategy)
            
            if not result.get("success"):
                logger.error(f"포지션 진입 실패: {result}")
                return
            
            self.current_position_uuid = result["position_uuid"]
            self.current_strategy = strategy
            
            # 중간 점검 카운터 리셋
            self.recheck_count = 0
            
            # WebSocket 감시 시작
            self._start_monitoring(result)
            
            # 첫 중간 점검 예약
            first_recheck = strategy.get("first_recheck_hours", SCHEDULER.DEFAULT_RECHECK_HOURS)
            self.recheck_scheduler.schedule_recheck(first_recheck)
            
        except Exception as e:
            logger.error(f"Phase 4 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("EXECUTION", str(e), "Phase 4")
    
    def _start_monitoring(self, position_result: Dict[str, Any]):
        """포지션 감시 시작"""
        position_info = {
            "position_uuid": position_result["position_uuid"],
            "direction": position_result["direction"],
            "leverage": position_result["leverage"],
            "entry_price": position_result["entry_price"],
            "stop_loss": position_result["stop_loss"],
            "take_profit": position_result["take_profit"],
            "liquidation_price": position_result["liquidation"]
        }
        
        self.watcher = FuturesWatcher(
            position_info=position_info,
            on_close_triggered=self._on_position_close
        )
        self.watcher.start()
        
        # Profit Guard 시작
        self._start_profit_guard()
        
        logger.info("WebSocket 감시 시작")
    
    def _resume_monitoring(self):
        """기존 포지션 감시 재개"""
        position = position_manager.get_current_position()
        
        if not position:
            return
        
        self.current_position_uuid = position.get("position_uuid")
        
        # 기존 포지션 재개 시 점검 카운터는 DB에서 조회하여 복원
        try:
            self.recheck_count = db_manager.get_recheck_count(self.current_position_uuid)
        except Exception as e:
            logger.warning(f"점검 카운터 복원 실패: {e}")
            self.recheck_count = 0
        
        position_info = {
            "position_uuid": position.get("position_uuid"),
            "direction": position["direction"],
            "leverage": position["leverage"],
            "entry_price": position["entry_price"],
            "stop_loss": position.get("stop_loss", 0),
            "take_profit": position.get("take_profit", 0),
            "liquidation_price": position["liquidation_price"]
        }
        
        self.watcher = FuturesWatcher(
            position_info=position_info,
            on_close_triggered=self._on_position_close
        )
        self.watcher.start()
        
        # Profit Guard 시작
        self._start_profit_guard()
        
        # 중간 점검 예약 (기본 주기)
        self.recheck_scheduler.schedule_recheck(0.02)
        
        logger.info(f"기존 포지션 감시 재개 (이전 점검 횟수: {self.recheck_count})")
    
    # Profit Guard
    def _start_profit_guard(self):
        """Profit Guard 스레드 시작"""
        self._profit_guard_running = True
        self._profit_guard_thread = threading.Thread(
            target=self._profit_guard_loop,
            daemon=True
        )
        self._profit_guard_thread.start()
        logger.info("Profit Guard 스레드 시작")
    
    def _stop_profit_guard(self):
        """Profit Guard 스레드 중지"""
        self._profit_guard_running = False
        self._profit_guard_thread = None
        logger.info("Profit Guard 스레드 중지")

    def _profit_guard_loop(self):
        """
        Profit Guard 감시 루프 (독립 스레드, 60초 주기)
        
        WebSocket Watcher의 profit_guard_active 플래그가 True일 때만
        5분봉 데이터를 조회하여 추세 반전 감지.
        반전 감지 시 즉시 시장가 청산.
        """
        while self._profit_guard_running:
            try:
                time.sleep(PROFIT_GUARD.CHECK_INTERVAL_SEC)
                
                if not self._profit_guard_running:
                    break
                
                # Watcher가 없거나 플래그 미활성이면 스킵
                if not self.watcher or not self.watcher.profit_guard_active:
                    continue
                
                # 5분봉 데이터 조회
                df = data_fetcher.fetch_kline_for_profit_guard(
                    interval=PROFIT_GUARD.KLINE_INTERVAL,
                    limit=PROFIT_GUARD.KLINE_LIMIT
                )
                
                if df.empty:
                    logger.warning("Profit Guard: 5분봉 데이터 조회 실패, 다음 사이클 대기")
                    continue
                
                # 지표 계산
                indicators = calculate_profit_guard_indicators(
                    df,
                    macd_fast=PROFIT_GUARD.MACD_FAST,
                    macd_slow=PROFIT_GUARD.MACD_SLOW,
                    macd_signal=PROFIT_GUARD.MACD_SIGNAL,
                    rsi_period=PROFIT_GUARD.RSI_PERIOD
                )
                
                if not indicators:
                    continue
                
                # 추세 반전 감지
                direction = self.watcher.direction
                reversal = detect_trend_reversal(
                    indicators,
                    direction,
                    rsi_threshold=PROFIT_GUARD.RSI_REVERSAL_THRESHOLD
                )
                
                if reversal["reversal_detected"]:
                    pnl_pct = self.watcher._current_unrealized_pnl_pct * 100
                    current_price = data_fetcher.get_current_price()
                    
                    logger.info(
                        f"Profit Guard 반전 감지: {reversal['reason']} "
                        f"(PnL={pnl_pct:+.2f}%)"
                    )
                    
                    telegram_notifier.send_profit_guard_triggered(
                        direction=direction,
                        unrealized_pnl_pct=pnl_pct,
                        current_price=current_price,
                        reason=reversal["reason"]
                    )
                    
                    # 즉시 청산 트리거
                    self._on_position_close("PROFIT_GUARD")
                    break
                
            except Exception as e:
                logger.error(f"Profit Guard 루프 오류: {e}", exc_info=True)
            
            finally:
                gc.collect()

    def _on_position_close(self, reason: str):
        """포지션 청산 콜백"""
        if not self.current_position_uuid:
            return
        
        logger.info(f"포지션 청산 트리거: {reason}")
        
        # 중간 점검 취소
        self.recheck_scheduler.cancel()
        
        # Profit Guard 중지
        self._stop_profit_guard()
        
        try:
            result = position_manager.close_position(
                self.current_position_uuid,
                reason
            )
            
            logger.info(f"청산 완료: {result}")
            
        except Exception as e:
            logger.error(f"청산 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("CLOSE_POSITION", str(e), "on_position_close")
        
        finally:
            self.current_position_uuid = None
            self.current_strategy = None
            self.watcher = None
            self.recheck_count = 0
            gc.collect()
    
    # 중간 점검 메서드
    def _run_position_recheck(self):
        """Phase 4 중간 점검 실행"""
        if not self.current_position_uuid:
            logger.warning("중간 점검: 활성 포지션 없음")
            return
        
        # 점검 카운터 증가
        self.recheck_count += 1
        
        logger.info("=" * 40)
        logger.info(f"Phase 4: 중간 점검 #{self.recheck_count}")
        logger.info("=" * 40)
        
        try:
            # 1. 현재 데이터 수집
            data = data_fetcher.collect_all_data()
            ai_input = data_fetcher.prepare_ai_input(data)
            
            # 2. 현재 포지션 정보
            position = position_manager.get_current_position()
            if not position:
                logger.warning("중간 점검: 포지션 조회 실패")
                return
            
            # 3. 경과 시간 및 PnL 계산
            db_position = db_manager.get_active_position()
            entry_time = datetime.fromisoformat(db_position["entry_timestamp"])
            elapsed_hours = (datetime.now() - entry_time).total_seconds() / 3600
            
            entry_price = position["entry_price"]
            current_price = ai_input["futures"]["last_price"]
            direction = position["direction"]
            leverage = position["leverage"]
            
            if direction == "LONG":
                pnl_pct = ((current_price - entry_price) / entry_price) * leverage * 100
            else:
                pnl_pct = ((entry_price - current_price) / entry_price) * leverage * 100
            
            # 4. 직전 점검 기록 + 피크 PnL 조회
            last_recheck = db_manager.get_last_recheck(self.current_position_uuid)
            peak_pnl = db_manager.get_peak_pnl(self.current_position_uuid)
            
            prev_pnl_pct = last_recheck["unrealized_pnl_percentage"] if last_recheck else None
            prev_decision = last_recheck["ai_decision"] if last_recheck else None
            
            # 5. AI 재평가 (텍스트 데이터 전용)
            position_info = {
                "direction": direction,
                "leverage": leverage,
                "entry_price": entry_price,
                "stop_loss": position.get("stop_loss", 0),
                "take_profit": position.get("take_profit", 0),
                "liquidation_price": position["liquidation_price"]
            }
            
            recheck_result = gemini_client.recheck_position(
                market_data=ai_input,
                position_info=position_info,
                elapsed_hours=elapsed_hours,
                unrealized_pnl_pct=pnl_pct,
                prev_pnl_pct=prev_pnl_pct,
                peak_pnl_pct=peak_pnl,
                prev_decision=prev_decision
            )
            
            decision = recheck_result.get("decision", "HOLD")
            reason = recheck_result.get("reason", "")
            next_recheck_hours = recheck_result.get("next_recheck_hours", SCHEDULER.DEFAULT_RECHECK_HOURS)
            
            logger.info(f"중간 점검 #{self.recheck_count} 결과: {decision} (PnL={pnl_pct:+.2f}%)")
            
            # 5. 결정에 따른 처리
            if decision == "EXIT":
                telegram_notifier.send_recheck_exit(
                    recheck_number=self.recheck_count,
                    elapsed_hours=elapsed_hours,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    reason=reason
                )
                self._on_position_close("AI_EXIT")
                return
            
            elif decision == "MODIFY":
                new_sl = recheck_result.get("new_stop_loss")
                new_tp = recheck_result.get("new_take_profit")
                
                if new_sl or new_tp:
                    db_manager.update_position_targets(
                        self.current_position_uuid,
                        stop_loss_price=new_sl,
                        take_profit_price=new_tp
                    )
                    
                    if self.watcher:
                        self.watcher.update_targets(new_sl, new_tp)
                
                telegram_notifier.send_recheck_modify(
                    recheck_number=self.recheck_count,
                    elapsed_hours=elapsed_hours,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    reason=reason,
                    new_stop_loss=new_sl,
                    new_take_profit=new_tp,
                    next_recheck_hours=next_recheck_hours
                )
            
            else:  # HOLD
                telegram_notifier.send_recheck_hold(
                    recheck_number=self.recheck_count,
                    elapsed_hours=elapsed_hours,
                    current_price=current_price,
                    pnl_pct=pnl_pct,
                    reason=reason,
                    next_recheck_hours=next_recheck_hours
                )
            
            # 6. DB 로그
            db_manager.log_recheck(
                position_uuid=self.current_position_uuid,
                current_price=current_price,
                unrealized_pnl=position.get("unrealized_pnl", 0),
                unrealized_pnl_percentage=pnl_pct,
                ai_decision=decision,
                ai_reason=reason,
                modifications_json=json.dumps(recheck_result, cls=NumpyEncoder, ensure_ascii=False)
            )
            
            # 7. 다음 점검 예약
            if decision != "EXIT":
                self.recheck_scheduler.schedule_recheck(next_recheck_hours)
            
        except Exception as e:
            logger.error(f"중간 점검 #{self.recheck_count} 오류: {e}", exc_info=True)
            telegram_notifier.send_system_error("RECHECK", str(e), f"Phase 4 중간점검 #{self.recheck_count}")
            
            # 오류 시 기본 주기로 재예약
            self.recheck_scheduler.schedule_recheck(SCHEDULER.DEFAULT_RECHECK_HOURS)
        
        finally:
            gc.collect()
    
    # 일일 리포트 메서드
    def _send_daily_report(self):
        """일일 리포트 생성 및 발송"""
        logger.info("일일 리포트 생성")
        
        try:
            # 7일 통계
            stats = db_manager.get_trade_stats(days=7)
            
            # 최근 거래
            recent = db_manager.get_recent_trades(limit=5)
            
            # 현재 상태
            balance_info = bybit_client.get_wallet_balance()
            balance = balance_info.get("available_balance", 0)
            
            position = position_manager.get_current_position()
            position_status = "없음"
            if position:
                position_status = f"{position['direction']} {position['leverage']}x"
            
            # 메시지 구성
            today = datetime.now().strftime("%Y-%m-%d")
            
            recent_text = ""
            for i, trade in enumerate(recent, 1):
                pnl = trade.get("realized_pnl", 0)
                emoji = "✅" if pnl >= 0 else "❌"
                recent_text += f"{i}. {trade['direction']} {pnl:+.2f} USDT {emoji}\n"
            
            if not recent_text:
                recent_text = "거래 내역 없음"
            
            # 총 수수료 표시
            total_fees = stats.get("total_fees", 0)
            
            message = f"""[METIS-F] 일일 리포트 ({today})

거래 요약 (7일):
- 총 거래: {stats['total_trades']}회
- 승/패: {stats['wins']}승 {stats['losses']}패 ({stats['win_rate']:.1f}%)
- 누적 PnL: {stats['total_pnl']:+.2f} USDT
- 총 수수료: {total_fees:.4f} USDT

최근 거래:
{recent_text}
현재 상태:
- 잔고: {balance:.2f} USDT
- 활성 포지션: {position_status}"""
            
            telegram_notifier.status(message)
            logger.info("일일 리포트 발송 완료")
            
        except Exception as e:
            logger.error(f"일일 리포트 오류: {e}", exc_info=True)
            telegram_notifier.send_system_error("DAILY_REPORT", str(e), "일일 리포트")


def main():
    """엔트리 포인트"""
    bot = MetisFutures()
    bot.start()


if __name__ == "__main__":
    main()