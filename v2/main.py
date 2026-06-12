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
from datetime import datetime, timezone, timedelta
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
from core.regime_engine import (
    determine_regime, generate_signal, SignalType,
    _calculate_leverage, _calculate_sl_tp,
)
from ai import gemini_client
from database import db_manager
from utils import telegram_notifier
from utils.telegram_bot import format_price
from core.trigger_monitor import TriggerMonitor
from core.technical_analysis import (
    calculate_profit_guard_indicators, detect_trend_reversal
)
from core.cycle_logger import cycle_logger

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

        # Symbol별 독립 다음 분석 시각 (AI가 직접 결정한 next_recheck_hours 반영)
        self.next_check_at: Dict[str, datetime] = {}

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
        
        telegram_notifier.status("[METIS-F2 LIVE] 시스템 종료")
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
            # Bybit 포지션 없음 — DB에 stale ACTIVE row 있으면 외부 청산 추정 후 정리.
            # API 일시 장애로 인한 false positive 방지 위해 재확인 후 처리.
            db_active = db_manager.get_active_position()
            if db_active:
                time.sleep(2)
                if not position_manager.has_active_position():
                    stale_count = db_manager.reconcile_stale_active()
                    if stale_count > 0:
                        logger.warning(
                            f"외부 청산 감지: stale ACTIVE row {stale_count}건 → EXTERNAL_CLOSE 처리"
                        )
                        try:
                            telegram_notifier.send_system_error(
                                "DB_SYNC",
                                f"외부 청산 감지: stale {stale_count}건 정리",
                                "startup_reconcile"
                            )
                        except Exception:
                            pass
            logger.info("활성 포지션 없음. Phase 1부터 시작.")

        # 시작 알림 (포지션 정보 포함)
        telegram_notifier.send_system_start(balance, position_info, is_restart)
        
        # 메인 루프: Symbol별 독립 스케줄.
        # 각 symbol은 AI 권장 시간대로 자기 next_check_at 가짐.
        # 매 wake-up에 도달한 symbol만 분석. 2분 이내 차이는 묶어서 처리.
        # 진입 시 단일 포지션 정책으로 다른 symbol 분석 중단.
        from config import TRADING
        TOLERANCE = timedelta(minutes=2)
        while self.running:
            try:
                # 진입 시 — 분석 멈춤. recheck (Phase 4)만 별도 스케줄러가 동작.
                if position_manager.has_active_position():
                    time.sleep(60)
                    continue

                now = datetime.now()
                analyzed_any = False
                for symbol in TRADING.SYMBOLS:
                    if position_manager.has_active_position():
                        break  # 진입 발생 시 다른 sym 분석 X
                    next_at = self.next_check_at.get(symbol)
                    # 첫 사이클(미설정) 또는 도달(2분 tolerance)이면 분석
                    if next_at is None or now + TOLERANCE >= next_at:
                        h = self._run_analysis_cycle(symbol=symbol)
                        if h is not None:
                            self.next_check_at[symbol] = datetime.now() + timedelta(hours=h)
                        else:
                            # 진입 성공 — 포지션 감시로 전환
                            self.next_check_at[symbol] = datetime.now() + timedelta(hours=1)
                        analyzed_any = True

                if position_manager.has_active_position():
                    continue

                # 다음 wake-up: 가장 가까운 next_check_at 까지
                if self.next_check_at:
                    next_wake = min(self.next_check_at.values())
                    sleep_sec = (next_wake - datetime.now()).total_seconds()
                    sleep_sec = max(60, min(86400, sleep_sec))  # 1m ~ 24h 클램프
                    schedule_lines = [
                        f"  {sym}: {ts.strftime('%H:%M')}"
                        for sym, ts in sorted(self.next_check_at.items(), key=lambda x: x[1])
                    ]
                    logger.info(
                        f"다음 wake: {next_wake.strftime('%H:%M:%S')} "
                        f"({sleep_sec/60:.1f}분 후)\n" + "\n".join(schedule_lines)
                    )
                    time.sleep(sleep_sec)
                else:
                    time.sleep(60)

            except KeyboardInterrupt:
                break

            except Exception as e:
                logger.error(f"메인 루프 오류: {e}", exc_info=True)
                telegram_notifier.send_system_error("MAIN_LOOP", str(e), "main.py")
                time.sleep(60)

    def _run_analysis_cycle(self, symbol: str = None):
        """Ver X: 레짐 기반 분석 사이클.

        반환: 다음 재분석까지 권장 시간 (hours). 진입/에러 시 None.
        """

        from config import TRADING
        if symbol is None:
            symbol = TRADING.SYMBOL

        # Phase 1: Data Collection
        logger.info("=" * 40)
        logger.info(f"Phase 1: 데이터 수집 [{symbol}]")
        logger.info("=" * 40)

        cycle_logger.start_cycle("analysis")
        cycle_logger.set_symbol(symbol)

        try:
            data = data_fetcher.collect_all_data(symbol=symbol)
            ai_input = data_fetcher.prepare_ai_input(data)
            cycle_logger.set_market_data(ai_input)

        except Exception as e:
            logger.error(f"Phase 1 실패: {e}")
            telegram_notifier.send_system_error("DATA_FETCH", str(e), "Phase 1")
            return 1.0  # 에러 시 1H 후 재시도
        
        # Phase 2: 레짐 판단 (코드 기반)
        logger.info("=" * 40)
        logger.info("Phase 2: 레짐 판단 (Ver X)")
        logger.info("=" * 40)
        
        try:
            # 1H 지표 추출 — data_fetcher가 "indicators"에 직접 dict 반환 (current wrap 없음)
            indicators_1h = ai_input.get("indicators", {})

            # 4H 요약 — multi_timeframe 안의 4h 사용
            tf_4h = ai_input.get("multi_timeframe", {}).get("4h", {})
            
            # 레짐 판단
            regime = determine_regime(indicators_1h, tf_4h)
            cycle_logger.set_regime(regime)

            logger.info(f"레짐: {regime.regime.value} (확신도={regime.confidence})")
            logger.info(f"근거: {regime.details.get('reason', '')}")
            
        except Exception as e:
            logger.error(f"Phase 2 레짐 판단 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("REGIME", str(e), "Phase 2")
            return 1.0
        
        # Phase 3: AI 직접 판단 (모든 결정 — direction/lev/SL/TP)
        # 운영 정책 (2026-05-06): "시스템 레벨 차단 다 풀자, AI = 판단자 진정 구현"
        # - 코드 시그널 점수 룰 / conf_to_lev 매핑 / _calculate_sl_tp 모두 우회.
        # - AI가 direction + leverage + stop_loss_price + take_profit_price 모두 결정.
        # - 코드는 *데이터 전달자*만. (코드 보존, 호출만 차단).
        logger.info("=" * 40)
        logger.info("Phase 3: AI 직접 판단 (Ver X — full delegation)")
        logger.info("=" * 40)

        try:
            # [참고] 코드 시그널 계산은 *기록용*으로 보존. 진입 결정엔 사용 X.
            signal_for_log = generate_signal(regime, indicators_1h)
            cycle_logger.set_signal(signal_for_log)
            logger.info(f"[참고] 코드 시그널: {signal_for_log.signal.value} (점수={signal_for_log.score})")

            # 시장 컨텍스트 (알림 풍부화)
            price = ai_input.get("futures", {}).get("last_price", 0)
            rsi = indicators_1h.get("rsi", 0)
            adx = indicators_1h.get("adx", 0) or regime.details.get("adx", 0)
            tf1h_trend = ai_input.get("trend_analysis", {}).get("trend", "N/A")
            if tf_4h:
                tf4h_dir = "BULLISH" if tf_4h.get("ema_20_50_bullish") else "BEARISH"
                tf4h_summary = f"{tf4h_dir} (ADX {tf_4h.get('adx', 0):.0f}, RSI {tf_4h.get('rsi', 0):.0f})"
            else:
                tf4h_summary = "N/A"
            ctx_line = (
                f"가격: {format_price(price)} | RSI {rsi:.0f} | ADX {adx:.0f}\n"
                f"4H: {tf4h_summary} | 1H 추세: {tf1h_trend}"
            )

            # 2-prompt 시스템 (팩트 기반, 운영자 2026-05-07 두번째 복원)
            logger.info(f"AI 2-prompt 호출 (LONG + SHORT 양방향) [{symbol}]")

            long_result = gemini_client.analyze_long(ai_input, symbol)
            short_result = gemini_client.analyze_short(ai_input, symbol)

            cycle_logger.set_ai_filter({
                "system": "2-prompt-bidirectional",
                "long_result": long_result,
                "short_result": short_result,
            })

            long_score = int(long_result.get("long_score", 0))
            short_score = int(short_result.get("short_score", 0))
            long_enter = bool(long_result.get("should_enter", False))
            short_enter = bool(short_result.get("should_enter", False))
            long_pattern = long_result.get("pattern", "?")
            short_pattern = short_result.get("pattern", "?")
            long_story = long_result.get("market_story", "")
            short_story = short_result.get("market_story", "")
            long_reasoning = long_result.get("long_reasoning", "")
            short_reasoning = short_result.get("short_reasoning", "")

            logger.info(
                f"양방향 결정 [{symbol}] "
                f"LONG: score={long_score} enter={long_enter} ({long_pattern}) / "
                f"SHORT: score={short_score} enter={short_enter} ({short_pattern})"
            )

            # 진입 결정 — AI should_enter 직접. 둘 다 OK면 점수 더 높은 쪽.
            chosen_direction = None
            chosen_result = None

            if long_enter and not short_enter:
                chosen_direction = "LONG"
                chosen_result = long_result
            elif short_enter and not long_enter:
                chosen_direction = "SHORT"
                chosen_result = short_result
            elif long_enter and short_enter:
                if long_score > short_score:
                    chosen_direction = "LONG"
                    chosen_result = long_result
                elif short_score > long_score:
                    chosen_direction = "SHORT"
                    chosen_result = short_result
                # 동점 → NO_ENTRY

            if not chosen_direction:
                self._consecutive_wait_count += 1
                wait_hours = min(
                    float(long_result.get("next_recheck_hours", 4.0)),
                    float(short_result.get("next_recheck_hours", 4.0)),
                )

                if long_enter and short_enter:
                    no_entry_reason = "양쪽 다 진입 추천 + 동점 → 보류"
                elif not long_enter and not short_enter:
                    no_entry_reason = "AI 양쪽 모두 거부"
                else:
                    no_entry_reason = "단방향 추천 + 점수 부족"

                # next_recheck 이유 (짧은 쪽 사용 — 우세한 쪽 표시)
                long_recheck_reason = long_result.get("next_recheck_reason", "")
                short_recheck_reason = short_result.get("next_recheck_reason", "")
                long_recheck_h = float(long_result.get("next_recheck_hours", 4.0))
                short_recheck_h = float(short_result.get("next_recheck_hours", 4.0))
                if long_recheck_h <= short_recheck_h:
                    chosen_recheck_reason = f"LONG {long_recheck_h}h: {long_recheck_reason}"
                else:
                    chosen_recheck_reason = f"SHORT {short_recheck_h}h: {short_recheck_reason}"

                telegram_notifier.send_analysis_result(
                    decision="WAIT",
                    reason=(
                        f"[{regime.regime.value}] {no_entry_reason}\n\n"
                        f"━━━ 🟢 LONG ━━━\n"
                        f"점수 {long_score}/10  ·  enter={long_enter}  ·  패턴 {long_pattern}\n"
                        f"💬 {long_reasoning}\n"
                        f"📖 {long_story}\n"
                        f"⏰ next {long_recheck_h}h — {long_recheck_reason}\n\n"
                        f"━━━ 🔴 SHORT ━━━\n"
                        f"점수 {short_score}/10  ·  enter={short_enter}  ·  패턴 {short_pattern}\n"
                        f"💬 {short_reasoning}\n"
                        f"📖 {short_story}\n"
                        f"⏰ next {short_recheck_h}h — {short_recheck_reason}\n\n"
                        f"📍 채택: {chosen_recheck_reason}\n"
                        f"{ctx_line}"
                    ),
                    wait_hours=wait_hours,
                    symbol=symbol,
                    ai_used=True
                )

                cycle_logger.set_final_decision("AI_NO_ENTRY")
                cycle_logger.save()
                gc.collect()
                return wait_hours

            # 진입 흐름
            direction = chosen_direction
            if_taken = chosen_result.get("if_taken") or {}
            leverage = int(if_taken.get("leverage", 1))
            ai_sl_price = float(if_taken.get("stop_price", 0) or 0)
            ai_tp_price = float(if_taken.get("target_price", 0) or 0)
            ai_conf = long_score if direction == "LONG" else short_score
            ai_reason = long_reasoning if direction == "LONG" else short_reasoning
            ai_story = long_story if direction == "LONG" else short_story
            chosen_pattern = long_pattern if direction == "LONG" else short_pattern
            chosen_rr = if_taken.get("rr_ratio", 0)
            chosen_prob = if_taken.get("probability", "?")
            ai_premortem = ai_reason  # 호환
            ai_review = ai_reason

            logger.info(
                f"진입 결정: {direction} {leverage}x score={ai_conf}/10 "
                f"SL={ai_sl_price} TP={ai_tp_price} R:R={chosen_rr} prob={chosen_prob}"
            )

            # 안전 검증 — SL/TP 방향 (실행기 안전, AI 판단 X 영역)
            if ai_sl_price <= 0 or ai_tp_price <= 0:
                logger.warning(f"AI SL/TP 누락 → NO_ENTRY 처리")
                cycle_logger.set_final_decision("AI_INVALID_SLTP")
                cycle_logger.save()
                return 1.0

            current_price_now = price
            if direction == "LONG":
                if ai_sl_price >= current_price_now or ai_tp_price <= current_price_now:
                    logger.warning(
                        f"AI SL/TP 방향 오류 (LONG): SL {ai_sl_price} TP {ai_tp_price} "
                        f"vs 현재 {current_price_now} → NO_ENTRY 처리"
                    )
                    cycle_logger.set_final_decision("AI_INVALID_SLTP")
                    cycle_logger.save()
                    return 1.0
            else:
                if ai_sl_price <= current_price_now or ai_tp_price >= current_price_now:
                    logger.warning(
                        f"AI SL/TP 방향 오류 (SHORT): SL {ai_sl_price} TP {ai_tp_price} "
                        f"vs 현재 {current_price_now} → NO_ENTRY 처리"
                    )
                    cycle_logger.set_final_decision("AI_INVALID_SLTP")
                    cycle_logger.save()
                    return 1.0

            # SL/TP 거리 비율 계산 (Phase 3.5에서 사용)
            sl_pct = abs(current_price_now - ai_sl_price) / current_price_now * 100
            tp_pct = abs(ai_tp_price - current_price_now) / current_price_now * 100

            # WAIT 카운터 리셋
            self._consecutive_wait_count = 0

            # signal 객체 갱신 (Phase 3.5 호환)
            from core.regime_engine import StrategySignal
            signal = StrategySignal(
                signal=SignalType.LONG if direction == "LONG" else SignalType.SHORT,
                regime=regime.regime,
                leverage=leverage,
                stop_loss_pct=sl_pct,
                take_profit_pct=tp_pct,
                reason=f"[AI full delegation] {ai_review or ai_reason}",
                score=max(60, min(100, ai_conf * 10)),
            )
            cycle_logger.set_signal(signal)

            telegram_notifier.send_analysis_result(
                decision="TRADE",
                direction=direction,
                confidence=ai_conf,
                leverage=leverage,
                reason=(
                    f"[AI 팩트 기반 양방향]\n\n"
                    f"━━━ 🟢 LONG ━━━\n"
                    f"점수 {long_score}/10  ·  enter={long_enter}  ·  패턴 {long_pattern}\n"
                    f"💬 {long_reasoning}\n"
                    f"📖 {long_story}\n\n"
                    f"━━━ 🔴 SHORT ━━━\n"
                    f"점수 {short_score}/10  ·  enter={short_enter}  ·  패턴 {short_pattern}\n"
                    f"💬 {short_reasoning}\n"
                    f"📖 {short_story}\n\n"
                    f"━━━ 📍 선택: {direction} ━━━\n"
                    f"확률 {chosen_prob}  ·  R:R 1:{chosen_rr}\n"
                    f"SL {ai_sl_price} / TP {ai_tp_price}\n\n"
                    f"{ctx_line}"
                ),
                symbol=symbol,
                ai_used=True
            )

        except Exception as e:
            logger.error(f"Phase 3 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("SIGNAL", str(e), "Phase 3")
            gc.collect()
            return 1.0
        
        # Phase 3.5: 전략 검증 (기존 validate_ai_strategy 재활용)
        logger.info("=" * 40)
        logger.info("Phase 3.5: 전략 검증")
        logger.info("=" * 40)
        
        try:
            # ⚠️ 단일 포지션 정책: 다른 symbol 또는 같은 symbol active 있으면 진입 차단
            # (분석은 4 호출 다 돌렸지만 실제 거래는 한 번에 1 포지션만)
            if position_manager.has_active_position():
                logger.warning(f"단일 포지션 정책: 이미 active position 존재 → {symbol} 진입 차단")
                cycle_logger.set_final_decision("BLOCKED_OTHER_ACTIVE")
                cycle_logger.save()
                return 1.0

            # 잔고 재확인
            balance_info = bybit_client.get_wallet_balance()
            balance = balance_info.get("available_balance", 0)

            if balance < 1:
                logger.warning(f"잔고 부족: {balance:.2f} USDT")
                telegram_notifier.info(f"[METIS-F2 LIVE] 잔고 부족: {balance:.2f} USDT")
                return 4.0
            
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
            
            # 안전성 검증 (symbol별 qty precision 사용)
            strategy = validate_ai_strategy(
                current_price=current_price,
                balance=balance,
                direction=direction,
                leverage=leverage,
                stop_loss_price=sl_price,
                take_profit_price=tp_price,
                symbol=symbol,
            )
            
            if not strategy.get("valid"):
                logger.warning(f"전략 검증 실패: {strategy.get('reason')}")
                telegram_notifier.info(f"[METIS-F2 LIVE] 전략 검증 실패: {strategy.get('reason')}")
                return 1.0
            
            # 추가 정보
            strategy["symbol"] = symbol  # 멀티 symbol 지원
            strategy["ai_reason"] = f"[Ver X] {symbol} {regime.regime.value} | {signal.reason}"
            strategy["estimated_time_hours"] = 24
            # 운영자 의도 (1D/4H/1H 큰 틀) 따라 첫 recheck 4시간으로 변경
            strategy["first_recheck_hours"] = 4
            cycle_logger.set_strategy(strategy)
            
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
            return 1.0

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
                return 1.0
            
            self.current_position_uuid = result["position_uuid"]
            self.current_strategy = strategy

            cycle_logger.set_position_open(result)
            cycle_logger.set_final_decision(f"TRADE_{result.get('direction', 'UNKNOWN')}")
            cycle_logger.save()

            # 중간 점검 카운터 리셋
            self.recheck_count = 0
            
            # WebSocket 감시 시작
            self._start_monitoring(result)

            # 첫 중간 점검 예약
            first_recheck = strategy.get("first_recheck_hours", SCHEDULER.DEFAULT_RECHECK_HOURS)
            self.recheck_scheduler.schedule_recheck(first_recheck)

            # 진입 성공 → main loop가 has_active_position True 보고 60초 sleep으로 빠짐
            return None

        except Exception as e:
            logger.error(f"Phase 4 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("EXECUTION", str(e), "Phase 4")
            return 1.0
    
    def _start_monitoring(self, position_result: Dict[str, Any]):
        """포지션 감시 시작"""
        position_info = {
            "position_uuid": position_result["position_uuid"],
            "symbol": position_result.get("symbol", TRADING.SYMBOL),
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
            "symbol": position.get("symbol", TRADING.SYMBOL),
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

        # 청산된 symbol 캐싱 (cooldown 적용 위해 close_position 호출 전에 확보)
        closed_symbol = self.watcher.symbol if self.watcher else None

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

            # 재진입 cooldown: 방금 청산된 symbol은 1h 동안 진입 차단,
            # 다른 symbol은 즉시 재평가 가능 (None → main loop에서 분석)
            REENTRY_COOLDOWN_HOURS = 1.0
            cooldown_until = datetime.now() + timedelta(hours=REENTRY_COOLDOWN_HOURS)
            self.next_check_at = {
                sym: (cooldown_until if sym == closed_symbol else None)
                for sym in TRADING.SYMBOLS
            }
            if closed_symbol:
                logger.info(
                    f"재진입 cooldown: {closed_symbol} 차단 → "
                    f"{cooldown_until.strftime('%H:%M:%S')} 까지"
                )
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

        cycle_logger.start_cycle("recheck")

        try:
            # 1. 현재 데이터 수집
            data = data_fetcher.collect_all_data()
            ai_input = data_fetcher.prepare_ai_input(data)
            cycle_logger.set_market_data(ai_input)
            
            # 2. 현재 포지션 정보
            position = position_manager.get_current_position()
            if not position:
                logger.warning("중간 점검: 포지션 조회 실패")
                return

            cycle_logger.set_symbol(position.get("symbol", TRADING.SYMBOL))

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
            
            cycle_logger.set_recheck_input(
                position_info, elapsed_hours, pnl_pct, prev_pnl_pct, peak_pnl, prev_decision
            )

            recheck_result = gemini_client.recheck_position(
                market_data=ai_input,
                position_info=position_info,
                elapsed_hours=elapsed_hours,
                unrealized_pnl_pct=pnl_pct,
                prev_pnl_pct=prev_pnl_pct,
                peak_pnl_pct=peak_pnl,
                prev_decision=prev_decision
            )
            cycle_logger.set_recheck_result(recheck_result)

            decision = recheck_result.get("decision", "HOLD")
            reason = recheck_result.get("reason", "")
            next_recheck_hours = recheck_result.get("next_recheck_hours", SCHEDULER.DEFAULT_RECHECK_HOURS)
            cycle_logger.set_final_decision(f"RECHECK_{decision}")
            
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

                    # paper_state.db / Bybit trading-stop 영속 (재시작 안전)
                    try:
                        from exchange import bybit_client as _bc
                        _bc.set_trading_stop(
                            symbol=position.get("symbol", TRADING.SYMBOL),
                            stop_loss=new_sl,
                            take_profit=new_tp
                        )
                    except Exception as e:
                        logger.warning(f"recheck MODIFY set_trading_stop 실패: {e}")

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

            cycle_logger.save()

        except Exception as e:
            logger.error(f"중간 점검 #{self.recheck_count} 오류: {e}", exc_info=True)
            telegram_notifier.send_system_error("RECHECK", str(e), f"Phase 4 중간점검 #{self.recheck_count}")
            try:
                cycle_logger.set_final_decision("RECHECK_ERROR")
                cycle_logger.save()
            except Exception:
                pass
            
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
            
            message = f"""[METIS-F2 LIVE] 일일 리포트 ({today})

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