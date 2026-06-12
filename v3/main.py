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
from concurrent.futures import ThreadPoolExecutor
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
from core.trailing_stop import TrailingStopManager

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
        self.trailing_stop: Optional[TrailingStopManager] = None
        self.current_position_uuid: Optional[str] = None
        self.current_strategy: Optional[Dict[str, Any]] = None

        # Fix #34: 청산 경합 차단 lock (PG 청산 + WS Position size=0 동시 트리거 방지)
        self._close_lock = threading.Lock()

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
        if hasattr(self, 'trailing_stop'):
            self._stop_trailing_stop()
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
        
        # 메인 루프: 완전 병렬 분석 + 점수 비교 진입.
        # 1. due_symbols 모음 (next_check_at 도달한 심볼)
        # 2. ThreadPoolExecutor로 동시 분석 (allow_entry=False — 진입 직전까지)
        # 3. winner 선정: 점수 desc, 동점 시 TRADING.SYMBOLS 순서 우선 (ETH > SOL > XRP)
        # 4. winner 있으면 _execute_entry, 나머지 log_record 저장
        # 5. 후보 없으면 모든 log_record 저장 + 각 wait_hours로 next_check_at 갱신
        from config import TRADING
        TOLERANCE = timedelta(minutes=2)
        while self.running:
            try:
                # 진입 시 — 분석 멈춤. recheck (Phase 4)만 별도 스케줄러가 동작.
                if position_manager.has_active_position():
                    time.sleep(60)
                    continue

                now = datetime.now()
                # due_symbols: 첫 사이클(미설정) 또는 도달(2분 tolerance)
                due_symbols = []
                for symbol in TRADING.SYMBOLS:
                    next_at = self.next_check_at.get(symbol)
                    if next_at is None or now + TOLERANCE >= next_at:
                        due_symbols.append(symbol)

                if due_symbols:
                    logger.info(
                        f"병렬 분석 시작: {due_symbols} "
                        f"(allow_entry=False, max_workers={len(due_symbols)})"
                    )
                    results = []
                    with ThreadPoolExecutor(max_workers=len(due_symbols)) as ex:
                        future_to_sym = {
                            ex.submit(self._run_analysis_cycle, sym, False): sym
                            for sym in due_symbols
                        }
                        for fut in future_to_sym:
                            sym = future_to_sym[fut]
                            try:
                                res = fut.result()
                                if isinstance(res, dict):
                                    results.append(res)
                                else:
                                    # 정상이라면 allow_entry=False는 항상 dict 반환.
                                    # 방어 코드: 다른 타입이면 wait dict로 변환.
                                    logger.warning(
                                        f"[{sym}] 비정상 반환 (dict 기대, got {type(res)}): {res}"
                                    )
                                    results.append({
                                        "symbol": sym,
                                        "should_enter": False,
                                        "score": 0,
                                        "wait_hours": 1.0,
                                        "log_record": None,
                                    })
                            except Exception as e:
                                logger.error(f"[{sym}] 병렬 분석 예외: {e}", exc_info=True)
                                results.append({
                                    "symbol": sym,
                                    "should_enter": False,
                                    "score": 0,
                                    "wait_hours": 1.0,
                                    "log_record": None,
                                })

                    # 진입 후보 추출 + 정렬 (점수 desc, 동점 시 SYMBOLS 순서 asc)
                    candidates = [r for r in results if r.get("should_enter")]

                    def _symbol_rank(sym):
                        try:
                            return TRADING.SYMBOLS.index(sym)
                        except ValueError:
                            return 999

                    candidates.sort(
                        key=lambda r: (-int(r.get("score", 0)), _symbol_rank(r["symbol"]))
                    )

                    if candidates:
                        winner = candidates[0]
                        loser_syms = [r["symbol"] for r in results if r is not winner]
                        logger.info(
                            f"Winner: {winner['symbol']} {winner.get('direction', '?')} "
                            f"score={winner.get('score', 0)} "
                            f"(losers: {loser_syms})"
                        )

                        # 패자들 cycle_log 보존 (winner는 _execute_entry 안에서 save)
                        for r in results:
                            if r is winner:
                                continue
                            rec = r.get("log_record")
                            if rec is None:
                                continue
                            try:
                                cycle_logger.load_record(rec)
                                cycle_logger.save()
                            except Exception as e:
                                logger.warning(
                                    f"[{r['symbol']}] 패자 log_record save 실패: {e}"
                                )

                        # winner 진입 실행
                        entered = self._execute_entry(winner)
                        # 패자 next_check_at: AI 권장 wait_hours
                        for r in results:
                            if r is winner:
                                continue
                            wait_h = float(r.get("wait_hours", 1.0) or 1.0)
                            self.next_check_at[r["symbol"]] = (
                                datetime.now() + timedelta(hours=wait_h)
                            )
                        # winner next_check_at: 1H (감시 중이지만 main loop는
                        # has_active_position True로 60s sleep만 함)
                        self.next_check_at[winner["symbol"]] = (
                            datetime.now() + timedelta(hours=1)
                        )

                        if not entered:
                            logger.warning(
                                f"_execute_entry 실패 → winner {winner['symbol']} "
                                f"next_check_at 1H 후로 재시도"
                            )
                    else:
                        # 후보 없음: 모든 log_record save + wait_hours로 갱신
                        for r in results:
                            rec = r.get("log_record")
                            if rec is not None:
                                try:
                                    cycle_logger.load_record(rec)
                                    cycle_logger.save()
                                except Exception as e:
                                    logger.warning(
                                        f"[{r['symbol']}] log_record save 실패: {e}"
                                    )
                            wait_h = float(r.get("wait_hours", 1.0) or 1.0)
                            self.next_check_at[r["symbol"]] = (
                                datetime.now() + timedelta(hours=wait_h)
                            )
                        gc.collect()

                # 진입 발생 시 다음 iteration에서 has_active_position 처리
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

    def _run_analysis_cycle(self, symbol: str = None, allow_entry: bool = True):
        """Ver X: 레짐 기반 분석 사이클.

        allow_entry=True (default): 기존과 동일 (Phase 4 진입까지 실행).
        allow_entry=False: 진입 직전에 dict 반환 (cycle_log save 안 함).
            반환 dict 키: symbol, should_enter, score, [direction, strategy,
            chosen_first_recheck_hours,] wait_hours, log_record

        반환:
          - allow_entry=True: 다음 재분석까지 권장 시간 (hours). 진입/에러 시 None.
          - allow_entry=False: dict (위 스펙) 또는 에러 시 다음과 같은 dict
            {"symbol", "should_enter": False, "score": 0, "wait_hours": 1.0,
             "log_record": cycle_logger.take_and_reset()}
        """

        from config import TRADING
        if symbol is None:
            symbol = TRADING.SYMBOL

        def _error_return(wait_h: float):
            """allow_entry=False 모드에서 에러/early-exit 시 dict 반환 헬퍼.
            cycle_log save 하지 않고 log_record로 떠넘김."""
            try:
                rec = cycle_logger.take_and_reset()
            except Exception:
                rec = None
            return {
                "symbol": symbol,
                "should_enter": False,
                "score": 0,
                "wait_hours": wait_h,
                "log_record": rec,
            }

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
            if not allow_entry:
                return _error_return(1.0)
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
            if not allow_entry:
                return _error_return(1.0)
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

            # 병렬 호출 (ThreadPoolExecutor) — 시간 절반
            with ThreadPoolExecutor(max_workers=2) as _ex:
                _long_fut = _ex.submit(gemini_client.analyze_long, ai_input, symbol)
                _short_fut = _ex.submit(gemini_client.analyze_short, ai_input, symbol)
                long_result = _long_fut.result()
                short_result = _short_fut.result()

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
                if not allow_entry:
                    rec = cycle_logger.take_and_reset()
                    gc.collect()
                    return {
                        "symbol": symbol,
                        "should_enter": False,
                        "score": max(long_score, short_score),
                        "wait_hours": wait_hours,
                        "log_record": rec,
                    }
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
            # AI 권장 첫 recheck 시간 (chosen direction의 next_recheck_hours)
            chosen_first_recheck_hours = max(1.0, min(24.0, float(chosen_result.get("next_recheck_hours", 4.0) or 4.0)))

            logger.info(
                f"진입 결정: {direction} {leverage}x score={ai_conf}/10 "
                f"SL={ai_sl_price} TP={ai_tp_price} R:R={chosen_rr} prob={chosen_prob}"
            )

            # 안전 검증 — SL/TP 방향 (실행기 안전, AI 판단 X 영역)
            if ai_sl_price <= 0 or ai_tp_price <= 0:
                logger.warning(f"AI SL/TP 누락 → NO_ENTRY 처리")
                cycle_logger.set_final_decision("AI_INVALID_SLTP")
                if not allow_entry:
                    rec = cycle_logger.take_and_reset()
                    return {
                        "symbol": symbol,
                        "should_enter": False,
                        "score": ai_conf,
                        "wait_hours": 1.0,
                        "log_record": rec,
                    }
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
                    if not allow_entry:
                        rec = cycle_logger.take_and_reset()
                        return {
                            "symbol": symbol,
                            "should_enter": False,
                            "score": ai_conf,
                            "wait_hours": 1.0,
                            "log_record": rec,
                        }
                    cycle_logger.save()
                    return 1.0
            else:
                if ai_sl_price <= current_price_now or ai_tp_price >= current_price_now:
                    logger.warning(
                        f"AI SL/TP 방향 오류 (SHORT): SL {ai_sl_price} TP {ai_tp_price} "
                        f"vs 현재 {current_price_now} → NO_ENTRY 처리"
                    )
                    cycle_logger.set_final_decision("AI_INVALID_SLTP")
                    if not allow_entry:
                        rec = cycle_logger.take_and_reset()
                        return {
                            "symbol": symbol,
                            "should_enter": False,
                            "score": ai_conf,
                            "wait_hours": 1.0,
                            "log_record": rec,
                        }
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
            if not allow_entry:
                return _error_return(1.0)
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
                if not allow_entry:
                    rec = cycle_logger.take_and_reset()
                    return {
                        "symbol": symbol,
                        "should_enter": False,
                        "score": ai_conf,
                        "wait_hours": 1.0,
                        "log_record": rec,
                    }
                cycle_logger.save()
                return 1.0

            # 잔고 재확인
            balance_info = bybit_client.get_wallet_balance()
            balance = balance_info.get("available_balance", 0)

            if balance < 1:
                logger.warning(f"잔고 부족: {balance:.2f} USDT")
                telegram_notifier.info(f"[METIS-F2 LIVE] 잔고 부족: {balance:.2f} USDT")
                if not allow_entry:
                    return _error_return(4.0)
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
                if not allow_entry:
                    return _error_return(1.0)
                return 1.0
            
            # 추가 정보
            strategy["symbol"] = symbol  # 멀티 symbol 지원
            strategy["ai_reason"] = f"[Ver X] {symbol} {regime.regime.value} | {signal.reason}"
            strategy["estimated_time_hours"] = 24
            # AI가 진입 분석 시 권장한 next_recheck_hours를 첫 점검 시간으로 사용
            # (이전 hardcoded 4h → AI 판단. 변동성/모멘텀 따라 1-24h 동적)
            strategy["first_recheck_hours"] = chosen_first_recheck_hours
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
            if not allow_entry:
                return _error_return(1.0)
            return 1.0

        finally:
            gc.collect()
        
        # Phase 4: Execution (기존과 동일)
        # allow_entry=False: 진입 실행 안 함. winner 후보 dict 반환.
        # cycle_log는 save 안 하고 take_and_reset()으로 호출자에 넘김.
        if not allow_entry:
            rec = cycle_logger.take_and_reset()
            return {
                "symbol": symbol,
                "should_enter": True,
                "score": ai_conf,
                "direction": direction,
                "strategy": strategy,
                "chosen_first_recheck_hours": chosen_first_recheck_hours,
                "wait_hours": chosen_first_recheck_hours,
                "log_record": rec,
            }

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
    
    def _execute_entry(self, result: Dict[str, Any]) -> bool:
        """Winner 후보 진입 실행. AI 재호출 X.

        result는 _run_analysis_cycle(allow_entry=False)가 반환한 winner dict:
          - symbol, strategy, chosen_first_recheck_hours, log_record (필수)

        cycle_logger를 winner의 log_record로 복원 → 진입 실행 → save.
        """
        cycle_logger.load_record(result["log_record"])
        symbol = result["symbol"]
        strategy = result["strategy"]
        chosen_first_recheck_hours = result["chosen_first_recheck_hours"]

        if position_manager.has_active_position():
            logger.warning(f"_execute_entry: 이미 active position 존재 → {symbol} 진입 차단")
            cycle_logger.set_final_decision("BLOCKED_OTHER_ACTIVE")
            cycle_logger.save()
            return False

        try:
            open_result = position_manager.open_position(strategy)
            if not open_result.get("success"):
                logger.error(f"_execute_entry: open_position 실패: {open_result}")
                cycle_logger.set_final_decision("AI_INVALID_SLTP")
                cycle_logger.save()
                return False

            self.current_position_uuid = open_result["position_uuid"]
            self.current_strategy = strategy
            cycle_logger.set_position_open(open_result)
            cycle_logger.set_final_decision(f"TRADE_{open_result.get('direction', 'UNKNOWN')}")
            cycle_logger.save()

            self.recheck_count = 0
            self._start_monitoring(open_result)
            first_recheck = strategy.get("first_recheck_hours", chosen_first_recheck_hours)
            self.recheck_scheduler.schedule_recheck(first_recheck)
            return True
        except Exception as e:
            logger.error(f"_execute_entry 실패: {e}", exc_info=True)
            telegram_notifier.send_system_error("EXECUTION", str(e), "_execute_entry")
            return False

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
        self._start_trailing_stop()

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
        self._start_trailing_stop()
        
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

    def _start_trailing_stop(self):
        """Trailing Stop 시작 (Fix #44)"""
        if not self.current_strategy or not self.watcher:
            return
        try:
            self.trailing_stop = TrailingStopManager(
                position_uuid=self.current_position_uuid,
                symbol=self.watcher.symbol,
                side=self.current_strategy["direction"],
                entry_price=self.current_strategy["entry_price"],
                leverage=self.current_strategy["leverage"],
                initial_sl=self.current_strategy["stop_loss"],
            )
            self.trailing_stop.start()
        except Exception as e:
            logger.warning(f"Trailing 시작 실패: {e}")
            self.trailing_stop = None

    def _stop_trailing_stop(self):
        """Trailing Stop 중지"""
        if self.trailing_stop:
            try:
                self.trailing_stop.stop()
            except Exception:
                pass
            self.trailing_stop = None

    def _profit_guard_loop(self):
        """
        Profit Guard v2 (Fix #32) — 3-Trigger 시스템, 30초 주기.

        Trigger 1: Multi-TF Reversal (5m + 15m 동시 MACD/RSI 반전)
        Trigger 2: Peak Drawdown (피크 6%+ 후 피크 일정 비율 후퇴)
        Trigger 3: Hard Drawdown (피크 10%+ 후 절대 5pp 후퇴)

        피크 흑자 후퇴 회피 + 큰 흑자 보호 재설계.
        """
        while self._profit_guard_running:
            try:
                time.sleep(PROFIT_GUARD.CHECK_INTERVAL_SEC)

                if not self._profit_guard_running:
                    break

                if not self.watcher or not self.watcher.profit_guard_active:
                    continue

                symbol = self.watcher.symbol
                direction = self.watcher.direction
                current_pnl_pct = self.watcher._current_unrealized_pnl_pct
                peak_pnl_pct = self.watcher._session_peak_pnl_pct
                drawdown_pct = peak_pnl_pct - current_pnl_pct
                is_alt = ("SOL" in symbol) or ("ETH" in symbol) or ("XRP" in symbol)

                # Trigger 3: Hard Drawdown (큰 흑자 보호 — 최우선)
                if peak_pnl_pct >= PROFIT_GUARD.HARD_DRAWDOWN_MIN_PEAK:
                    if drawdown_pct >= PROFIT_GUARD.HARD_DRAWDOWN_ABS_PP:
                        reason = (
                            f"HARD_DRAWDOWN: 피크 +{peak_pnl_pct*100:.2f}% 후 "
                            f"{drawdown_pct*100:.2f}pp 후퇴 (현재 +{current_pnl_pct*100:.2f}%)"
                        )
                        logger.info(f"Profit Guard v2 트리거: {reason}")
                        try:
                            telegram_notifier.send_profit_guard_triggered(
                                direction=direction,
                                unrealized_pnl_pct=current_pnl_pct*100,
                                current_price=data_fetcher.get_current_price(),
                                reason=reason
                            )
                        except Exception:
                            pass
                        self._on_position_close("PROFIT_GUARD")
                        break

                # Trigger 2: Peak Drawdown (피크 일정 비율 후퇴)
                if peak_pnl_pct >= PROFIT_GUARD.MIN_PEAK_FOR_DRAWDOWN:
                    ratio_threshold = (
                        PROFIT_GUARD.DRAWDOWN_RATIO_ALT if is_alt
                        else PROFIT_GUARD.DRAWDOWN_RATIO_BTC
                    )
                    if drawdown_pct >= peak_pnl_pct * ratio_threshold:
                        actual_ratio = drawdown_pct / peak_pnl_pct if peak_pnl_pct > 0 else 0
                        reason = (
                            f"PEAK_DRAWDOWN ({symbol}): 피크 +{peak_pnl_pct*100:.2f}% → "
                            f"현재 +{current_pnl_pct*100:.2f}% (드로우다운 {actual_ratio*100:.0f}% "
                            f"≥ 임계 {ratio_threshold*100:.0f}%)"
                        )
                        logger.info(f"Profit Guard v2 트리거: {reason}")
                        try:
                            telegram_notifier.send_profit_guard_triggered(
                                direction=direction,
                                unrealized_pnl_pct=current_pnl_pct*100,
                                current_price=data_fetcher.get_current_price(),
                                reason=reason
                            )
                        except Exception:
                            pass
                        self._on_position_close("PROFIT_GUARD")
                        break

                # Trigger 1: Multi-TF Reversal (5m + 15m 동시)
                df_primary = data_fetcher.fetch_kline_for_profit_guard(
                    interval=PROFIT_GUARD.KLINE_INTERVAL,
                    limit=PROFIT_GUARD.KLINE_LIMIT
                )
                df_secondary = data_fetcher.fetch_kline_for_profit_guard(
                    interval=PROFIT_GUARD.KLINE_INTERVAL_SECONDARY,
                    limit=PROFIT_GUARD.KLINE_LIMIT
                )

                if df_primary.empty or df_secondary.empty:
                    logger.warning("Profit Guard v2: 캔들 데이터 부족")
                    continue

                ind_primary = calculate_profit_guard_indicators(
                    df_primary, PROFIT_GUARD.MACD_FAST, PROFIT_GUARD.MACD_SLOW,
                    PROFIT_GUARD.MACD_SIGNAL, PROFIT_GUARD.RSI_PERIOD
                )
                ind_secondary = calculate_profit_guard_indicators(
                    df_secondary, PROFIT_GUARD.MACD_FAST, PROFIT_GUARD.MACD_SLOW,
                    PROFIT_GUARD.MACD_SIGNAL, PROFIT_GUARD.RSI_PERIOD
                )

                rev_primary = (
                    detect_trend_reversal(ind_primary, direction, PROFIT_GUARD.RSI_REVERSAL_THRESHOLD)
                    if ind_primary else {"reversal_detected": False, "reason": "no data"}
                )
                rev_secondary = (
                    detect_trend_reversal(ind_secondary, direction, PROFIT_GUARD.RSI_REVERSAL_THRESHOLD)
                    if ind_secondary else {"reversal_detected": False, "reason": "no data"}
                )

                if PROFIT_GUARD.DUAL_TF_CONFIRMATION:
                    triggered = rev_primary.get("reversal_detected") and rev_secondary.get("reversal_detected")
                    label = "REVERSAL_DUAL_TF"
                else:
                    triggered = rev_primary.get("reversal_detected") or rev_secondary.get("reversal_detected")
                    label = "REVERSAL_SINGLE_TF"

                if triggered:
                    reason = (
                        f"{label} ({symbol}): "
                        f"15m={rev_primary.get('reason','?')} | "
                        f"5m={rev_secondary.get('reason','?')} | "
                        f"PnL=+{current_pnl_pct*100:.2f}% (peak +{peak_pnl_pct*100:.2f}%)"
                    )
                    logger.info(f"Profit Guard v2 트리거: {reason}")
                    try:
                        telegram_notifier.send_profit_guard_triggered(
                            direction=direction,
                            unrealized_pnl_pct=current_pnl_pct*100,
                            current_price=data_fetcher.get_current_price(),
                            reason=reason
                        )
                    except Exception:
                        pass
                    self._on_position_close("PROFIT_GUARD")
                    break

            except Exception as e:
                logger.error(f"Profit Guard v2 루프 오류: {e}", exc_info=True)

            finally:
                gc.collect()

    def _on_position_close(self, reason: str):
        """포지션 청산 콜백 (Fix #34: Lock + WS external flag 즉시 설정)"""
        # Fix #34: 경합 차단 — 청산 진행 중 두 번째 호출 무시
        if not self._close_lock.acquire(blocking=False):
            logger.warning(f"청산 이미 진행 중 → 두 번째 호출 무시 (reason={reason})")
            return

        try:
            if not self.current_position_uuid:
                return

            # Fix #34: WS Position size=0 push로 인한 _trigger_close 발동 방지
            # PG 청산이 직접 main._on_position_close 호출 시 watcher._trigger_close를
            # 거치지 않으므로 set_external_close_in_progress가 설정 안 됨.
            # → 진입 즉시 명시적으로 설정.
            if self.watcher and self.watcher.ws and reason != "LIQUIDATION":
                try:
                    self.watcher.ws.set_external_close_in_progress(True)
                    logger.info(f"외부 청산 플래그 즉시 설정 (reason={reason}) — WS Position 콜백 차단")
                except Exception:
                    pass

            logger.info(f"포지션 청산 트리거: {reason}")

            # 청산된 symbol 캐싱
            closed_symbol = self.watcher.symbol if self.watcher else None

            # 중간 점검 취소
            self.recheck_scheduler.cancel()

            # Profit Guard 중지
            self._stop_profit_guard()
            self._stop_trailing_stop()

            try:
                result = position_manager.close_position(
                    self.current_position_uuid,
                    reason
                )

                logger.info(f"청산 완료: {result}")

            except Exception as e:
                logger.error(f"청산 실패: {e}", exc_info=True)
                telegram_notifier.send_system_error("CLOSE_POSITION", str(e), "on_position_close")

            self.current_position_uuid = None
            self.current_strategy = None
            self.watcher = None
            self.recheck_count = 0

            # 청산 후 3 심볼 즉시 분석 (운영자 5/21 명시 — cooldown 제거)
            now = datetime.now()
            self.next_check_at = {sym: now for sym in TRADING.SYMBOLS}
            logger.info(f'청산 후 즉시 분석: {list(TRADING.SYMBOLS)} (cooldown 없음)')
            gc.collect()
        finally:
            # Fix #34: Lock 해제 (경합 차단 종료)
            self._close_lock.release()
    
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
            # 1. 현재 포지션 조회 (symbol 파악 — 멀티심볼 환경에서 필수)
            position = position_manager.get_current_position()
            if not position:
                logger.warning("중간 점검: 포지션 조회 실패")
                return

            symbol = position["symbol"]
            cycle_logger.set_symbol(symbol)

            # 2. 해당 symbol 데이터 수집
            data = data_fetcher.collect_all_data(symbol=symbol)
            ai_input = data_fetcher.prepare_ai_input(data)
            cycle_logger.set_market_data(ai_input)

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