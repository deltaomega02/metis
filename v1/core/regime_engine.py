# core/regime_engine.py
# 시장 레짐 판단 및 전략 시그널 생성
# Ver X: AI 방향 예측 → 코드 기반 레짐+전략으로 전환
# AI는 진입 전 최종 필터(PASS/REJECT)로만 사용

import gc
from enum import Enum
from typing import Dict, Any, Optional, Tuple
from dataclasses import dataclass

from config import TRADING, get_logger

logger = get_logger("regime_engine")


class MarketRegime(Enum):
    """시장 레짐 분류"""
    BULLISH = "BULLISH"         # 상승 추세
    BEARISH = "BEARISH"         # 하락 추세
    SIDEWAYS = "SIDEWAYS"       # 횡보
    HIGH_VOL = "HIGH_VOL"       # 고변동성 (방향 불명확)
    LOW_VOL = "LOW_VOL"         # 저변동성 (스퀴즈 대기)


class SignalType(Enum):
    """전략 시그널"""
    LONG = "LONG"
    SHORT = "SHORT"
    WAIT = "WAIT"


@dataclass
class RegimeResult:
    """레짐 판단 결과"""
    regime: MarketRegime
    confidence: int              # 1~10: 레짐 판단 확신도
    details: Dict[str, Any]      # 판단 근거


@dataclass
class StrategySignal:
    """전략 시그널 결과"""
    signal: SignalType
    regime: MarketRegime
    leverage: int                # 레짐+조건 기반 레버리지
    stop_loss_pct: float         # 손절 % (ATR 기반)
    take_profit_pct: float       # 익절 % (R:R 기반)
    reason: str                  # 진입/대기 사유
    score: int                   # 시그널 강도 (0~100)


# ============================================================
# 레짐 판단 파라미터
# ============================================================

# ADX 기반 추세 강도
ADX_TRENDING = 25              # 이 이상이면 추세 존재
ADX_STRONG_TREND = 35          # 강한 추세

# EMA 정배열/역배열 판단용
# (기존 technical_analysis.py에서 ema_20, ema_50, ema_120 사용)

# 볼린저밴드 폭 기반 변동성 판단
BB_WIDTH_LOW = 2.0             # 이 이하면 저변동성 (스퀴즈)
BB_WIDTH_HIGH = 6.0            # 이 이상이면 고변동성

# ATR% 기반 변동성 판단
ATR_PCT_LOW = 0.5              # 저변동성 임계
ATR_PCT_HIGH = 2.0             # 고변동성 임계

# 레짐별 레버리지 상한
REGIME_MAX_LEVERAGE = {
    MarketRegime.BULLISH: 7,
    MarketRegime.BEARISH: 7,
    MarketRegime.SIDEWAYS: 3,
    MarketRegime.HIGH_VOL: 3,
    MarketRegime.LOW_VOL: 2,
}

# 진입 조건 파라미터
RSI_OVERSOLD = 35              # 롱 진입 RSI 하한 영역
RSI_OVERBOUGHT = 65            # 숏 진입 RSI 상한 영역
RSI_EXTREME_OVERSOLD = 25      # 극단 과매도
RSI_EXTREME_OVERBOUGHT = 75    # 극단 과매수
MACD_CROSS_THRESHOLD = 0       # MACD 히스토그램 부호 전환
VOLUME_SURGE_RATIO = 1.5       # 거래량 급등 배수


# ============================================================
# Phase 2: 레짐 판단
# ============================================================

def determine_regime(indicators: Dict[str, Any], tf_4h: Dict[str, Any] = None) -> RegimeResult:
    """
    시장 레짐 판단 (코드 기반, AI 불필요)
    
    판단 우선순위:
    1. 변동성 이상치 체크 (HIGH_VOL / LOW_VOL)
    2. 추세 존재 여부 (ADX 기반)
    3. 추세 방향 (EMA 배열 + DI 크로스)
    4. 횡보 판단 (나머지)
    
    Args:
        indicators: 1H 타임프레임 현재 지표 (get_current_indicators 결과의 'current' 필드)
        tf_4h: 4H 타임프레임 요약 (get_timeframe_summary 결과, 선택적)
    
    Returns:
        RegimeResult
    """
    adx = indicators.get("adx", 0)
    plus_di = indicators.get("plus_di", 0)
    minus_di = indicators.get("minus_di", 0)
    rsi = indicators.get("rsi", 50)
    bb_width = indicators.get("bb_width", 3.0)
    atr_pct = indicators.get("atr_pct", 1.0)
    ema_20 = indicators.get("ema_20", 0)
    ema_50 = indicators.get("ema_50", 0)
    ema_120 = indicators.get("ema_120", 0)
    macd_histogram = indicators.get("macd_histogram", 0)
    close = indicators.get("close", 0)
    
    details = {
        "adx": round(adx, 1),
        "plus_di": round(plus_di, 1),
        "minus_di": round(minus_di, 1),
        "bb_width": round(bb_width, 2),
        "atr_pct": round(atr_pct, 3),
        "ema_aligned": "bullish" if ema_20 > ema_50 > ema_120 else "bearish" if ema_20 < ema_50 < ema_120 else "mixed",
        "price_vs_ema20": "above" if close > ema_20 else "below"
    }
    
    # 1. 변동성 이상치 체크
    if atr_pct >= ATR_PCT_HIGH or bb_width >= BB_WIDTH_HIGH:
        # 고변동성: 추세 방향이 명확하면 추세로 분류, 불명확하면 HIGH_VOL
        if adx >= ADX_STRONG_TREND:
            # 방향이 명확한 고변동성 → 추세로 분류 (아래에서 처리)
            pass
        else:
            return RegimeResult(
                regime=MarketRegime.HIGH_VOL,
                confidence=7,
                details={**details, "reason": f"고변동성: ATR%={atr_pct:.3f}, BB폭={bb_width:.2f}, ADX={adx:.1f} (추세 불명확)"}
            )
    
    if atr_pct <= ATR_PCT_LOW and bb_width <= BB_WIDTH_LOW:
        return RegimeResult(
            regime=MarketRegime.LOW_VOL,
            confidence=8,
            details={**details, "reason": f"저변동성 스퀴즈: ATR%={atr_pct:.3f}, BB폭={bb_width:.2f}"}
        )
    
    # 2. 추세 존재 여부 (ADX 기반)
    if adx >= ADX_TRENDING:
        # 추세 존재 → 방향 판단
        ema_bullish = ema_20 > ema_50 > ema_120
        ema_bearish = ema_20 < ema_50 < ema_120
        di_bullish = plus_di > minus_di
        di_bearish = minus_di > plus_di
        price_above_ema = close > ema_20
        
        # 4H 타임프레임 보조 (있으면)
        tf_4h_aligned = True
        if tf_4h:
            tf_4h_bullish = tf_4h.get("ema_20_50_bullish", False)
            tf_4h_adx = tf_4h.get("adx", 0)
            if tf_4h_adx >= 20:
                tf_4h_aligned = tf_4h_bullish if di_bullish else not tf_4h_bullish
        
        # 상승 추세 판단
        bullish_score = sum([
            ema_bullish,
            di_bullish,
            price_above_ema,
            macd_histogram > 0,
            tf_4h_aligned if di_bullish else False
        ])
        
        # 하락 추세 판단
        bearish_score = sum([
            ema_bearish,
            di_bearish,
            not price_above_ema,
            macd_histogram < 0,
            tf_4h_aligned if di_bearish else False
        ])
        
        if bullish_score >= 3:
            conf = 6 + min(bullish_score - 3, 3)  # 6~9
            if adx >= ADX_STRONG_TREND:
                conf = min(conf + 1, 10)
            return RegimeResult(
                regime=MarketRegime.BULLISH,
                confidence=conf,
                details={**details, "reason": f"상승추세: ADX={adx:.1f}, +DI={plus_di:.1f}>-DI={minus_di:.1f}, EMA={'정배열' if ema_bullish else '부분'}"}
            )
        
        if bearish_score >= 3:
            conf = 6 + min(bearish_score - 3, 3)
            if adx >= ADX_STRONG_TREND:
                conf = min(conf + 1, 10)
            return RegimeResult(
                regime=MarketRegime.BEARISH,
                confidence=conf,
                details={**details, "reason": f"하락추세: ADX={adx:.1f}, -DI={minus_di:.1f}>+DI={plus_di:.1f}, EMA={'역배열' if ema_bearish else '부분'}"}
            )
    
    # 3. 횡보 (추세 미약하거나 방향 불명확)
    return RegimeResult(
        regime=MarketRegime.SIDEWAYS,
        confidence=5 if adx < 20 else 4,
        details={**details, "reason": f"횡보: ADX={adx:.1f} (<{ADX_TRENDING}), 방향 불명확"}
    )


# ============================================================
# Phase 3: 전략 시그널 생성
# ============================================================

def generate_signal(
    regime: RegimeResult,
    indicators: Dict[str, Any],
    history: Dict[str, Any] = None
) -> StrategySignal:
    """
    레짐에 맞는 전략 시그널 생성
    
    각 레짐별 진입 조건:
    - BULLISH: EMA 지지 + RSI 비과매수 + MACD 양전환/유지
    - BEARISH: EMA 저항 + RSI 비과매도 + MACD 음전환/유지
    - SIDEWAYS: 볼린저밴드 이탈 시에만 (매우 보수적)
    - HIGH_VOL: 레버리지 축소, 넓은 SL, 명확 시그널만
    - LOW_VOL: WAIT (스퀴즈 해소 대기)
    
    Args:
        regime: 레짐 판단 결과
        indicators: 1H 현재 지표
        history: 시계열 히스토리 (선택적, 캔들 패턴 참조용)
    
    Returns:
        StrategySignal
    """
    r = regime.regime
    
    if r == MarketRegime.LOW_VOL:
        return _signal_wait(regime, "저변동성 스퀴즈 구간. 방향성 확보 후 진입.")
    
    if r == MarketRegime.BULLISH:
        return _strategy_bullish(regime, indicators, history)
    
    if r == MarketRegime.BEARISH:
        return _strategy_bearish(regime, indicators, history)
    
    if r == MarketRegime.SIDEWAYS:
        return _strategy_sideways(regime, indicators, history)
    
    if r == MarketRegime.HIGH_VOL:
        return _strategy_high_vol(regime, indicators, history)
    
    return _signal_wait(regime, "레짐 미분류")


def _strategy_bullish(
    regime: RegimeResult,
    ind: Dict[str, Any],
    history: Dict[str, Any] = None
) -> StrategySignal:
    """상승 추세 전략: 풀백 매수"""
    rsi = ind.get("rsi", 50)
    macd_hist = ind.get("macd_histogram", 0)
    close = ind.get("close", 0)
    ema_20 = ind.get("ema_20", 0)
    ema_50 = ind.get("ema_50", 0)
    atr_pct = ind.get("atr_pct", 1.0)
    volume_ratio = ind.get("volume_ratio", 1.0)
    
    score = 0
    reasons = []
    
    # 조건 1: 가격이 EMA20 근처 또는 위 (풀백 완료)
    price_to_ema20_pct = ((close - ema_20) / ema_20) * 100
    if -1.0 <= price_to_ema20_pct <= 3.0:
        score += 25
        reasons.append(f"EMA20 근접(거리 {price_to_ema20_pct:+.1f}%)")
    elif price_to_ema20_pct > 3.0:
        score += 10  # EMA20보다 많이 위 = 추격 매수 위험
        reasons.append(f"EMA20 상방 이탈({price_to_ema20_pct:+.1f}%)")
    
    # 조건 2: RSI 과매수 아님
    if RSI_OVERSOLD <= rsi <= RSI_OVERBOUGHT:
        score += 20
        reasons.append(f"RSI 적정({rsi:.1f})")
    elif rsi < RSI_OVERSOLD:
        score += 25  # 과매도 = 롱 기회
        reasons.append(f"RSI 과매도({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT:
        score -= 10  # 과매수 = 진입 위험
        reasons.append(f"RSI 과매수({rsi:.1f}) - 감점")
    
    # 조건 3: MACD 양전환 또는 양 유지
    if macd_hist > 0:
        score += 20
        reasons.append("MACD 양(+)")
    else:
        score -= 5
        reasons.append("MACD 음(-)")
    
    # 조건 4: 거래량 확인
    if volume_ratio >= VOLUME_SURGE_RATIO:
        score += 15
        reasons.append(f"거래량 급증({volume_ratio:.1f}x)")
    elif volume_ratio >= 1.0:
        score += 5
        reasons.append(f"거래량 정상({volume_ratio:.1f}x)")
    
    # 조건 5: 레짐 확신도 반영
    score += regime.confidence * 2  # 최대 +20
    
    # 진입 판단
    if score >= 60:
        leverage = _calculate_leverage(regime, score, atr_pct)
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "LONG", leverage)
        
        return StrategySignal(
            signal=SignalType.LONG,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"상승추세 롱 진입: {', '.join(reasons)}",
            score=min(score, 100)
        )
    
    return _signal_wait(regime, f"상승추세이나 진입 조건 미충족 (점수={score}): {', '.join(reasons)}")


def _strategy_bearish(
    regime: RegimeResult,
    ind: Dict[str, Any],
    history: Dict[str, Any] = None
) -> StrategySignal:
    """하락 추세 전략: 반등 매도"""
    rsi = ind.get("rsi", 50)
    macd_hist = ind.get("macd_histogram", 0)
    close = ind.get("close", 0)
    ema_20 = ind.get("ema_20", 0)
    ema_50 = ind.get("ema_50", 0)
    atr_pct = ind.get("atr_pct", 1.0)
    volume_ratio = ind.get("volume_ratio", 1.0)
    
    score = 0
    reasons = []
    
    # 조건 1: 가격이 EMA20 근처 또는 아래 (반등 후 저항)
    price_to_ema20_pct = ((close - ema_20) / ema_20) * 100
    if -3.0 <= price_to_ema20_pct <= 1.0:
        score += 25
        reasons.append(f"EMA20 근접(거리 {price_to_ema20_pct:+.1f}%)")
    elif price_to_ema20_pct < -3.0:
        score += 10  # EMA20보다 많이 아래 = 추격 숏 위험
        reasons.append(f"EMA20 하방 이탈({price_to_ema20_pct:+.1f}%)")
    
    # 조건 2: RSI 과매도 아님
    if RSI_OVERSOLD <= rsi <= RSI_OVERBOUGHT:
        score += 20
        reasons.append(f"RSI 적정({rsi:.1f})")
    elif rsi > RSI_OVERBOUGHT:
        score += 25  # 과매수 = 숏 기회
        reasons.append(f"RSI 과매수({rsi:.1f})")
    elif rsi < RSI_OVERSOLD:
        score -= 10  # 과매도 = 숏 위험
        reasons.append(f"RSI 과매도({rsi:.1f}) - 감점")
    
    # 조건 3: MACD 음전환 또는 음 유지
    if macd_hist < 0:
        score += 20
        reasons.append("MACD 음(-)")
    else:
        score -= 5
        reasons.append("MACD 양(+)")
    
    # 조건 4: 거래량 확인
    if volume_ratio >= VOLUME_SURGE_RATIO:
        score += 15
        reasons.append(f"거래량 급증({volume_ratio:.1f}x)")
    elif volume_ratio >= 1.0:
        score += 5
        reasons.append(f"거래량 정상({volume_ratio:.1f}x)")
    
    # 조건 5: 레짐 확신도
    score += regime.confidence * 2
    
    if score >= 60:
        leverage = _calculate_leverage(regime, score, atr_pct)
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "SHORT", leverage)
        
        return StrategySignal(
            signal=SignalType.SHORT,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"하락추세 숏 진입: {', '.join(reasons)}",
            score=min(score, 100)
        )
    
    return _signal_wait(regime, f"하락추세이나 진입 조건 미충족 (점수={score}): {', '.join(reasons)}")


def _strategy_sideways(
    regime: RegimeResult,
    ind: Dict[str, Any],
    history: Dict[str, Any] = None
) -> StrategySignal:
    """횡보 전략: 볼린저밴드 이탈 시에만 (매우 보수적)"""
    close = ind.get("close", 0)
    bb_upper = ind.get("bb_upper", 0)
    bb_lower = ind.get("bb_lower", 0)
    rsi = ind.get("rsi", 50)
    atr_pct = ind.get("atr_pct", 1.0)
    
    # 하단 이탈 → 롱 (평균 회귀)
    if close <= bb_lower and rsi <= RSI_OVERSOLD:
        leverage = min(3, REGIME_MAX_LEVERAGE[MarketRegime.SIDEWAYS])
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "LONG", leverage)
        return StrategySignal(
            signal=SignalType.LONG,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"횡보 하단 이탈 롱: BB하단 터치 + RSI={rsi:.1f}",
            score=55
        )
    
    # 상단 이탈 → 숏 (평균 회귀)
    if close >= bb_upper and rsi >= RSI_OVERBOUGHT:
        leverage = min(3, REGIME_MAX_LEVERAGE[MarketRegime.SIDEWAYS])
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "SHORT", leverage)
        return StrategySignal(
            signal=SignalType.SHORT,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"횡보 상단 이탈 숏: BB상단 터치 + RSI={rsi:.1f}",
            score=55
        )
    
    return _signal_wait(regime, "횡보 구간. 볼밴 이탈 대기.")


def _strategy_high_vol(
    regime: RegimeResult,
    ind: Dict[str, Any],
    history: Dict[str, Any] = None
) -> StrategySignal:
    """고변동성 전략: 극단 RSI에서만 역추세 진입, 낮은 레버리지"""
    rsi = ind.get("rsi", 50)
    atr_pct = ind.get("atr_pct", 1.0)
    plus_di = ind.get("plus_di", 0)
    minus_di = ind.get("minus_di", 0)
    
    # 극단 과매도 + DI 방향 확인
    if rsi <= RSI_EXTREME_OVERSOLD and plus_di > minus_di:
        leverage = min(2, REGIME_MAX_LEVERAGE[MarketRegime.HIGH_VOL])
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "LONG", leverage)
        return StrategySignal(
            signal=SignalType.LONG,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"고변동성 극단과매도 롱: RSI={rsi:.1f}, +DI>-DI",
            score=50
        )
    
    # 극단 과매수 + DI 방향 확인
    if rsi >= RSI_EXTREME_OVERBOUGHT and minus_di > plus_di:
        leverage = min(2, REGIME_MAX_LEVERAGE[MarketRegime.HIGH_VOL])
        sl_pct, tp_pct = _calculate_sl_tp(atr_pct, "SHORT", leverage)
        return StrategySignal(
            signal=SignalType.SHORT,
            regime=regime.regime,
            leverage=leverage,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
            reason=f"고변동성 극단과매수 숏: RSI={rsi:.1f}, -DI>+DI",
            score=50
        )
    
    return _signal_wait(regime, f"고변동성 구간. 극단 RSI 대기 (현재 RSI={rsi:.1f}).")


# ============================================================
# 유틸리티
# ============================================================

def _signal_wait(regime: RegimeResult, reason: str) -> StrategySignal:
    """WAIT 시그널 생성"""
    return StrategySignal(
        signal=SignalType.WAIT,
        regime=regime.regime,
        leverage=0,
        stop_loss_pct=0,
        take_profit_pct=0,
        reason=reason,
        score=0
    )


def _calculate_leverage(regime: RegimeResult, score: int, atr_pct: float) -> int:
    """
    레짐 + 시그널 강도 + 변동성 기반 레버리지 계산
    
    원칙: 넓은 SL(높은 ATR) → 낮은 레버리지
    """
    max_lev = REGIME_MAX_LEVERAGE.get(regime.regime, 3)
    
    # 시그널 강도 기반 기본 레버리지
    if score >= 80:
        base_lev = max_lev
    elif score >= 70:
        base_lev = max(max_lev - 1, 1)
    elif score >= 60:
        base_lev = max(max_lev - 2, 1)
    else:
        base_lev = 1
    
    # ATR% 기반 감산: 변동성 높으면 레버리지 낮춤
    if atr_pct >= 1.5:
        base_lev = max(base_lev - 2, 1)
    elif atr_pct >= 1.0:
        base_lev = max(base_lev - 1, 1)
    
    return min(base_lev, TRADING.MAX_LEVERAGE)


def _calculate_sl_tp(atr_pct: float, direction: str, leverage: int) -> Tuple[float, float]:
    """
    ATR 기반 SL/TP 계산
    
    SL: ATR의 1.5~2배 (변동성 대응)
    TP: SL의 2배 이상 (R:R 최소 1:2)
    
    수수료 감안: 왕복 0.11% × leverage
    """
    # SL: ATR% × 1.5 (최소 0.5%, 최대 5%)
    sl_pct = max(0.5, min(5.0, atr_pct * 1.5))
    
    # 레버리지 고려: 높은 레버리지에서는 SL을 좁게
    # (레버리지 × SL% = 실제 마진 손실)
    # 마진 손실이 10%를 넘지 않도록 조정
    max_sl_for_leverage = 10.0 / leverage  # 10% 마진 손실 한도
    sl_pct = min(sl_pct, max_sl_for_leverage)
    
    # TP: 최소 R:R 1:2
    round_trip_fee = 0.11 * leverage  # 왕복 수수료 (마진 기준 %)
    min_tp = sl_pct * 2.0 + round_trip_fee / leverage  # 수수료 보상 후 2:1
    tp_pct = max(min_tp, sl_pct * 2.0)
    
    # TP 상한: 현실적 범위 (ATR 기반)
    tp_pct = min(tp_pct, atr_pct * 5.0)
    
    # 최소 TP 보장 (수수료 감안 후 최소 1.5% 수익)
    min_target = TRADING.MIN_TARGET_PROFIT_PCT * 100  # 1.5%
    tp_pct = max(tp_pct, min_target)
    
    return round(sl_pct, 2), round(tp_pct, 2)