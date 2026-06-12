# core/trigger_monitor.py
# WAIT 대기 중 5분봉 지표 + 가격변동률 감시
# 트리거 조건 충족 시 대기를 조기 종료하여 AI 재분석 유도
#
# v4 강화사항:
# - 가격변동률 단독 트리거: 30분 내 1.2% 또는 15분 내 0.8% 이동 시 즉시 발동
# - 기존 복합 조건 감도 완화: RSI 65/35, ATR 1.5x
# - 체크 주기: 300s → 120s (가격변동률은 5분봉 갱신 불필요)
# - 쿨다운: 1시간 → 30분

import time
import threading
import gc
import pandas as pd
from typing import Optional, Dict, Any

from config import TRIGGER_MONITOR, TRADING, get_logger
from exchange import bybit_client
from core.technical_analysis import (
    calculate_macd, calculate_rsi, calculate_atr
)

logger = get_logger("trigger_monitor")


class TriggerMonitor:
    """
    WAIT 상태 지표 + 가격변동률 감시기

    AI가 WAIT 결정 후 대기하는 동안 2분 주기로 체크.
    두 가지 독립적 트리거 경로:

    경로 A (가격변동률 단독): 급등/급락 자체를 감지. 지표 조합 불필요.
      - 30분 내 1.2% 이상 이동
      - 15분 내 0.8% 이상 이동
      → 어느 하나만 충족해도 즉시 발동

    경로 B (지표 복합): 기존 MACD/RSI/ATR 중 2개 이상 동시 충족.
      - RSI 65/35 (완화)
      - ATR 1.5x (완화)
      - MACD 크로스 + 잡음 필터

    쿨다운: 발동 후 30분 재발동 차단.
    """

    # 쿨다운: 마지막 트리거 발동 시각 (클래스 레벨)
    _last_trigger_time: float = 0.0

    def __init__(self):
        self._trigger_event = threading.Event()
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None
        self._trigger_reason: Optional[str] = None
        self._previous_indicators: Optional[Dict[str, Any]] = None

    def wait_for_trigger(self, wait_hours: float) -> Optional[str]:
        """
        지표 + 가격변동률 감시하며 대기. 트리거 발동 시 즉시 리턴.

        Args:
            wait_hours: AI가 제안한 대기 시간

        Returns:
            트리거 사유 문자열 (발동 시) 또는 None (시간 만료)
        """
        self._trigger_event.clear()
        self._stop_event.clear()
        self._trigger_reason = None
        self._previous_indicators = None

        # 쿨다운 잔여 시간 계산
        elapsed = time.time() - TriggerMonitor._last_trigger_time
        cooldown_remaining = max(0, TRIGGER_MONITOR.COOLDOWN_SEC - elapsed)

        if cooldown_remaining > 0:
            logger.info(
                f"트리거 쿨다운 활성: {cooldown_remaining:.0f}s 남음 "
                f"-> 쿨다운 해제 후 감시 시작"
            )

        # 감시 스레드 시작
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop,
            args=(cooldown_remaining,),
            daemon=True
        )
        self._monitor_thread.start()

        logger.info(
            f"Trigger Monitor v4 시작: {wait_hours}h 대기 "
            f"(체크={TRIGGER_MONITOR.CHECK_INTERVAL_SEC}s, "
            f"쿨다운={TRIGGER_MONITOR.COOLDOWN_SEC}s, "
            f"가격트리거={TRIGGER_MONITOR.PRICE_MOVE_THRESHOLD_PCT}%/"
            f"{TRIGGER_MONITOR.PRICE_MOVE_LOOKBACK}캔들, "
            f"{TRIGGER_MONITOR.PRICE_SPIKE_THRESHOLD_PCT}%/"
            f"{TRIGGER_MONITOR.PRICE_SPIKE_LOOKBACK}캔들)"
        )

        # 메인 스레드 블로킹 대기
        timeout_sec = wait_hours * 3600
        triggered = self._trigger_event.wait(timeout=timeout_sec)

        # 감시 스레드 정리
        self.stop()

        if triggered:
            TriggerMonitor._last_trigger_time = time.time()
            logger.info(f"트리거 발동으로 대기 조기 종료: {self._trigger_reason}")
            return self._trigger_reason
        else:
            logger.info("대기 시간 만료 -> 정상 재분석")
            return None

    def stop(self):
        """감시 중지"""
        self._stop_event.set()
        if self._monitor_thread and self._monitor_thread.is_alive():
            self._monitor_thread.join(timeout=10)
        self._monitor_thread = None

    def _monitor_loop(self, cooldown_wait: float = 0):
        """주기적 감시 루프"""
        check_count = 0

        # 쿨다운 대기
        if cooldown_wait > 0:
            if self._stop_event.wait(timeout=cooldown_wait):
                return
            logger.info("트리거 쿨다운 해제 -> 감시 시작")

        while not self._stop_event.is_set():
            if self._stop_event.wait(timeout=TRIGGER_MONITOR.CHECK_INTERVAL_SEC):
                break

            check_count += 1

            try:
                trigger_reason = self._check_triggers()

                if trigger_reason:
                    self._trigger_reason = trigger_reason
                    self._trigger_event.set()
                    return

            except Exception as e:
                logger.error(f"트리거 체크 #{check_count} 오류: {e}")

    def _check_triggers(self) -> Optional[str]:
        """
        5분봉 데이터 기반 트리거 판단.
        경로 A (가격변동률) -> 경로 B (지표복합) 순서로 평가.
        경로 A 발동 시 경로 B 평가 생략.

        Returns:
            트리거 사유 문자열 (발동 시) 또는 None
        """
        df = None

        try:
            candles = bybit_client.get_kline(
                TRADING.SYMBOL,
                TRIGGER_MONITOR.KLINE_INTERVAL,
                TRIGGER_MONITOR.KLINE_LIMIT
            )

            if not candles or len(candles) < 35:
                return None

            df = pd.DataFrame(candles)

            # 경로 A: 가격변동률 단독 트리거 (최우선)
            price_trigger = self._check_price_move(df)
            if price_trigger:
                return price_trigger

            # 경로 B: 지표 복합 트리거
            indicators = self._calculate_indicators(df)
            if not indicators:
                return None

            trigger = self._evaluate_triggers(indicators)
            self._previous_indicators = indicators

            return trigger

        finally:
            if df is not None:
                del df
            gc.collect()

    def _check_price_move(self, df: pd.DataFrame) -> Optional[str]:
        """
        가격변동률 단독 트리거 (경로 A)

        5분봉 종가 기준으로 최근 N캔들 대비 현재가 변동률 계산.
        임계값 초과 시 지표 조합 없이 즉시 발동.

        두 가지 윈도우 동시 체크:
        - 30분 윈도우 (6캔들): 1.2% 이상
        - 15분 윈도우 (3캔들): 0.8% 이상

        Args:
            df: 5분봉 OHLCV DataFrame

        Returns:
            트리거 사유 문자열 또는 None
        """
        try:
            close = df["close"]
            current_price = float(close.iloc[-1])

            triggers = []

            # 30분 윈도우 체크
            lookback = TRIGGER_MONITOR.PRICE_MOVE_LOOKBACK
            threshold = TRIGGER_MONITOR.PRICE_MOVE_THRESHOLD_PCT

            if len(close) > lookback:
                ref_price = float(close.iloc[-(lookback + 1)])
                move_pct = abs((current_price - ref_price) / ref_price) * 100
                direction = "▲" if current_price > ref_price else "▼"

                if move_pct >= threshold:
                    triggers.append(
                        f"PRICE_MOVE_{direction}{move_pct:.2f}%/"
                        f"{lookback * 5}min(임계{threshold}%)"
                    )
                else:
                    logger.debug(
                        f"가격변동(30m): {direction}{move_pct:.2f}% "
                        f"(임계 {threshold}% 미달)"
                    )

            # 15분 윈도우 체크 (더 민감)
            spike_lookback = TRIGGER_MONITOR.PRICE_SPIKE_LOOKBACK
            spike_threshold = TRIGGER_MONITOR.PRICE_SPIKE_THRESHOLD_PCT

            if len(close) > spike_lookback:
                ref_price = float(close.iloc[-(spike_lookback + 1)])
                move_pct = abs((current_price - ref_price) / ref_price) * 100
                direction = "▲" if current_price > ref_price else "▼"

                if move_pct >= spike_threshold:
                    triggers.append(
                        f"PRICE_SPIKE_{direction}{move_pct:.2f}%/"
                        f"{spike_lookback * 5}min(임계{spike_threshold}%)"
                    )
                else:
                    logger.debug(
                        f"가격변동(15m): {direction}{move_pct:.2f}% "
                        f"(임계 {spike_threshold}% 미달)"
                    )

            if triggers:
                reason = f"가격변동트리거: {' + '.join(triggers)}"
                logger.info(f"트리거 발동 - {reason}")
                return reason

            return None

        except Exception as e:
            logger.error(f"가격변동률 체크 오류: {e}")
            return None

    def _calculate_indicators(self, df: pd.DataFrame) -> Optional[Dict[str, Any]]:
        """
        트리거 판단용 지표 계산

        Args:
            df: 5분봉 OHLCV DataFrame

        Returns:
            지표 딕셔너리 또는 None
        """
        try:
            close = df["close"]
            high = df["high"]
            low = df["low"]

            # MACD
            macd = calculate_macd(
                close,
                TRIGGER_MONITOR.MACD_FAST,
                TRIGGER_MONITOR.MACD_SLOW,
                TRIGGER_MONITOR.MACD_SIGNAL
            )
            histogram = macd["histogram"]

            # RSI
            rsi = calculate_rsi(close, TRIGGER_MONITOR.RSI_PERIOD)

            # ATR
            atr = calculate_atr(high, low, close, period=14)

            # 현재값 추출
            hist_current = float(histogram.iloc[-1])
            hist_previous = float(histogram.iloc[-2])
            rsi_current = float(rsi.iloc[-1])
            atr_current = float(atr.iloc[-1])

            # ATR 평균 (최근 20개 기준)
            atr_window = atr.iloc[-20:]
            atr_mean = float(atr_window.mean())
            atr_ratio = atr_current / atr_mean if atr_mean > 0 else 1.0

            return {
                "macd_hist_current": hist_current,
                "macd_hist_previous": hist_previous,
                "rsi_current": rsi_current,
                "atr_current": atr_current,
                "atr_mean": atr_mean,
                "atr_ratio": atr_ratio,
                "close": float(close.iloc[-1])
            }

        except Exception as e:
            logger.error(f"지표 계산 오류: {e}")
            return None

    def _evaluate_triggers(self, indicators: Dict[str, Any]) -> Optional[str]:
        """
        지표 복합 트리거 평가 (경로 B)

        3가지 지표 중 2개 이상 동시 충족 시 발동.

        조건 1: MACD 히스토그램 부호 전환 + 절대값 필터
        조건 2: RSI 과매수(>=65)/과매도(<=35)
        조건 3: ATR 급증 (평균 대비 1.5x 이상)

        Args:
            indicators: _calculate_indicators 결과

        Returns:
            트리거 사유 문자열 또는 None
        """
        hist_curr = indicators["macd_hist_current"]
        hist_prev = indicators["macd_hist_previous"]
        rsi_curr = indicators["rsi_current"]
        atr_ratio = indicators["atr_ratio"]

        signals = []

        # 조건 1: MACD 히스토그램 부호 전환 + 잡음 필터
        macd_cross = (hist_prev > 0 and hist_curr <= 0) or \
                     (hist_prev < 0 and hist_curr >= 0)
        macd_magnitude = abs(hist_curr) + abs(hist_prev)
        macd_threshold = indicators["close"] * 0.0003

        if macd_cross and macd_magnitude >= macd_threshold:
            direction = "양->음" if hist_prev > 0 else "음->양"
            signals.append(
                f"MACD_CROSS({direction}, {hist_prev:.4f}->{hist_curr:.4f})"
            )

        # 조건 2: RSI 과매수/과매도
        if rsi_curr >= TRIGGER_MONITOR.RSI_OVERBOUGHT:
            signals.append(f"RSI_OB({rsi_curr:.1f})")
        elif rsi_curr <= TRIGGER_MONITOR.RSI_OVERSOLD:
            signals.append(f"RSI_OS({rsi_curr:.1f})")

        # 조건 3: ATR 급증
        if atr_ratio >= TRIGGER_MONITOR.ATR_SPIKE_MULTIPLIER:
            signals.append(f"ATR_SPIKE({atr_ratio:.2f}x)")

        # 복합 조건: 2개 이상 충족 시 발동
        if len(signals) >= 2:
            reason = f"복합트리거[{len(signals)}/3]: {' + '.join(signals)}"
            logger.info(f"트리거 발동 - {reason}")
            return reason

        # 단일 신호만 감지
        if signals:
            logger.debug(f"단일 신호 감지 (미발동): {signals[0]}")

        return None