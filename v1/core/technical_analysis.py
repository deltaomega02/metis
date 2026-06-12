# core/technical_analysis.py
# 기술적 지표 계산
# EMA, RSI, MACD, Bollinger Bands, ATR, ADX, +DI/-DI
# Ver5.1: 시계열 히스토리, 고점/저점, 캔들 패턴 추가

import pandas as pd
import numpy as np
from typing import Dict, Any

from config import get_logger

logger = get_logger("technical_analysis")


def calculate_ema(series: pd.Series, period: int) -> pd.Series:
    """지수이동평균"""
    return series.ewm(span=period, adjust=False).mean()


def calculate_sma(series: pd.Series, period: int) -> pd.Series:
    """단순이동평균"""
    return series.rolling(window=period).mean()


def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """RSI (Relative Strength Index)"""
    delta = series.diff()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    
    avg_gain = gain.ewm(span=period, adjust=False).mean()
    avg_loss = loss.ewm(span=period, adjust=False).mean()
    
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    
    return rsi


def calculate_macd(
    series: pd.Series,
    fast: int = 12,
    slow: int = 26,
    signal: int = 9
) -> Dict[str, pd.Series]:
    """MACD (Moving Average Convergence Divergence)"""
    ema_fast = calculate_ema(series, fast)
    ema_slow = calculate_ema(series, slow)
    
    macd_line = ema_fast - ema_slow
    signal_line = calculate_ema(macd_line, signal)
    histogram = macd_line - signal_line
    
    return {
        "macd": macd_line,
        "signal": signal_line,
        "histogram": histogram
    }


def calculate_bollinger_bands(
    series: pd.Series,
    period: int = 20,
    std_dev: float = 2.0
) -> Dict[str, pd.Series]:
    """볼린저 밴드"""
    sma = calculate_sma(series, period)
    std = series.rolling(window=period).std()
    
    upper = sma + (std * std_dev)
    lower = sma - (std * std_dev)
    
    return {
        "middle": sma,
        "upper": upper,
        "lower": lower,
        "width": (upper - lower) / sma * 100  # 밴드 폭 (%)
    }


def calculate_atr(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> pd.Series:
    """ATR (Average True Range)"""
    prev_close = close.shift(1)
    
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    
    return atr

def calculate_adx(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    period: int = 14
) -> Dict[str, pd.Series]:
    """
    ADX + Directional Indicators
    
    Returns:
        {"adx": pd.Series, "plus_di": pd.Series, "minus_di": pd.Series}
    """
    # True Range
    prev_close = close.shift(1)
    tr1 = high - low
    tr2 = abs(high - prev_close)
    tr3 = abs(low - prev_close)
    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    
    # Directional Movement
    up_move = high - high.shift(1)
    down_move = low.shift(1) - low
    
    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0)
    
    plus_dm = pd.Series(plus_dm, index=high.index)
    minus_dm = pd.Series(minus_dm, index=high.index)
    
    # Smoothed averages
    atr = tr.ewm(span=period, adjust=False).mean()
    plus_di = 100 * (plus_dm.ewm(span=period, adjust=False).mean() / atr)
    minus_di = 100 * (minus_dm.ewm(span=period, adjust=False).mean() / atr)
    
    # ADX
    dx = 100 * abs(plus_di - minus_di) / (plus_di + minus_di)
    adx = dx.ewm(span=period, adjust=False).mean()
    
    return {
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di
    }


def add_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    DataFrame에 기술적 지표 추가
    
    Args:
        df: OHLCV DataFrame (open, high, low, close, volume)
    
    Returns:
        지표가 추가된 DataFrame
    """
    df = df.copy()
    
    # EMA
    df["ema_20"] = calculate_ema(df["close"], 20)
    df["ema_50"] = calculate_ema(df["close"], 50)
    df["ema_120"] = calculate_ema(df["close"], 120)
    df["ema_200"] = calculate_ema(df["close"], 200)
    
    # RSI
    df["rsi"] = calculate_rsi(df["close"], 14)
    
    # MACD
    macd = calculate_macd(df["close"])
    df["macd"] = macd["macd"]
    df["macd_signal"] = macd["signal"]
    df["macd_histogram"] = macd["histogram"]
    
    # Bollinger Bands
    bb = calculate_bollinger_bands(df["close"])
    df["bb_upper"] = bb["upper"]
    df["bb_middle"] = bb["middle"]
    df["bb_lower"] = bb["lower"]
    df["bb_width"] = bb["width"]
    
    # ATR
    df["atr"] = calculate_atr(df["high"], df["low"], df["close"])
    
    # ADX + DI
    adx_result = calculate_adx(df["high"], df["low"], df["close"])
    df["adx"] = adx_result["adx"]
    df["plus_di"] = adx_result["plus_di"]
    df["minus_di"] = adx_result["minus_di"]
    
    # 추가 파생 지표
    df["atr_pct"] = df["atr"] / df["close"] * 100  # ATR 비율
    df["volume_sma"] = calculate_sma(df["volume"], 20)
    df["volume_ratio"] = df["volume"] / df["volume_sma"]
    
    return df


def get_current_indicators(df: pd.DataFrame) -> Dict[str, Any]:
    """
    현재 지표 + 시계열 히스토리 + 구조적 레벨 추출
    
    이미지 제거로 손실되는 시각적 맥락을 텍스트 데이터로 보상.
    AI가 "방향성", "가속/감속", "지지/저항" 등을 판단할 수 있도록
    최근 N봉의 핵심 지표 추이를 포함.
    
    Returns:
        지표 딕셔너리 (스냅샷 + 히스토리 + 레벨)
    """
    if df.empty:
        return {}
    
    last = df.iloc[-1]
    
    # ========== 1. 현재 스냅샷 (기존) ==========
    snapshot = {
        "price": {
            "open": float(last["open"]),
            "high": float(last["high"]),
            "low": float(last["low"]),
            "close": float(last["close"]),
            "volume": float(last["volume"])
        },
        "ema": {
            "ema_20": float(last["ema_20"]),
            "ema_50": float(last["ema_50"]),
            "ema_120": float(last["ema_120"]),
            "ema_200": float(last["ema_200"])
        },
        "rsi": float(last["rsi"]),
        "macd": {
            "macd": float(last["macd"]),
            "signal": float(last["macd_signal"]),
            "histogram": float(last["macd_histogram"])
        },
        "bollinger": {
            "upper": float(last["bb_upper"]),
            "middle": float(last["bb_middle"]),
            "lower": float(last["bb_lower"]),
            "width": float(last["bb_width"])
        },
        "atr": {
            "value": float(last["atr"]),
            "percentage": float(last["atr_pct"])
        },
        "adx": float(last["adx"]),
        "plus_di": float(last["plus_di"]) if "plus_di" in df.columns else None,
        "minus_di": float(last["minus_di"]) if "minus_di" in df.columns else None,
        "volume": {
            "current": float(last["volume"]),
            "sma_20": float(last["volume_sma"]),
            "ratio": float(last["volume_ratio"])
        }
    }
    
    # ========== 2. 시계열 히스토리 (최근 10봉) ==========
    # AI가 추세 방향, 가속/감속, 전환점을 파악하기 위한 핵심 데이터
    hist_len = min(10, len(df))
    recent = df.tail(hist_len)
    
    history = {
        "close": [round(float(v), 2) for v in recent["close"].values],
        "rsi": [round(float(v), 1) for v in recent["rsi"].values],
        "macd_histogram": [round(float(v), 2) for v in recent["macd_histogram"].values],
        "volume_ratio": [round(float(v), 2) for v in recent["volume_ratio"].values],
        "adx": [round(float(v), 1) for v in recent["adx"].values],
        "atr_pct": [round(float(v), 3) for v in recent["atr_pct"].values],
    }
    
    # ========== 3. 가격 구조: 고점/저점 레벨 ==========
    # 차트에서 눈으로 확인하던 지지/저항 정보의 명시화
    levels = {}
    
    if len(df) >= 24:
        last_24 = df.tail(24)
        levels["high_24h"] = round(float(last_24["high"].max()), 2)
        levels["low_24h"] = round(float(last_24["low"].min()), 2)
    
    if len(df) >= 72:
        last_72 = df.tail(72)
        levels["high_3d"] = round(float(last_72["high"].max()), 2)
        levels["low_3d"] = round(float(last_72["low"].min()), 2)
    
    if len(df) >= 168:
        last_168 = df.tail(168)
        levels["high_7d"] = round(float(last_168["high"].max()), 2)
        levels["low_7d"] = round(float(last_168["low"].min()), 2)
    
    # 현재가의 최근 범위 내 위치 (0=저점, 100=고점)
    if len(df) >= 24:
        h24 = levels.get("high_24h", last["close"])
        l24 = levels.get("low_24h", last["close"])
        range_24h = h24 - l24
        if range_24h > 0:
            levels["position_in_24h_range_pct"] = round(
                (float(last["close"]) - l24) / range_24h * 100, 1
            )
    
    # ========== 4. 캔들 패턴 감지 (최근 3봉) ==========
    patterns = _detect_candle_patterns(df)
    
    # ========== 5. 다이버전스 감지 ==========
    divergence = _detect_divergence(df)
    
    # ========== 결합 ==========
    result = snapshot.copy()
    result["history"] = history
    result["price_levels"] = levels
    result["candle_patterns"] = patterns
    result["divergence"] = divergence
    
    return result


def _detect_candle_patterns(df: pd.DataFrame) -> Dict[str, Any]:
    """
    최근 캔들 패턴 감지
    
    코드 기반으로 정확하게 감지 — 이미지에서 AI가 "대충" 보던 것보다 정밀.
    """
    if len(df) < 3:
        return {"detected": []}
    
    patterns = []
    last = df.iloc[-1]
    prev = df.iloc[-2]
    
    body = abs(last["close"] - last["open"])
    upper_wick = last["high"] - max(last["close"], last["open"])
    lower_wick = min(last["close"], last["open"]) - last["low"]
    total_range = last["high"] - last["low"]
    
    if total_range == 0:
        return {"detected": []}
    
    body_ratio = body / total_range
    
    # 도지: 몸통이 전체 범위의 10% 미만
    if body_ratio < 0.1:
        patterns.append("DOJI")
    
    # 해머 (하락 후 긴 아래꼬리): 아래꼬리가 몸통의 2배 이상, 위꼬리 작음
    if lower_wick > body * 2 and upper_wick < body * 0.5 and body > 0:
        patterns.append("HAMMER")
    
    # 슈팅스타 (상승 후 긴 위꼬리): 위꼬리가 몸통의 2배 이상, 아래꼬리 작음
    if upper_wick > body * 2 and lower_wick < body * 0.5 and body > 0:
        patterns.append("SHOOTING_STAR")
    
    # 강한 양봉: 몸통이 전체의 70% 이상, 종가 > 시가
    if body_ratio > 0.7 and last["close"] > last["open"]:
        patterns.append("STRONG_BULLISH_CANDLE")
    
    # 강한 음봉: 몸통이 전체의 70% 이상, 종가 < 시가
    if body_ratio > 0.7 and last["close"] < last["open"]:
        patterns.append("STRONG_BEARISH_CANDLE")
    
    # 상승 잉골핑: 이전 음봉을 현재 양봉이 완전히 감싸는 경우
    if (prev["close"] < prev["open"] and  # 이전 음봉
        last["close"] > last["open"] and   # 현재 양봉
        last["open"] <= prev["close"] and  # 현재 시가 <= 이전 종가
        last["close"] >= prev["open"]):    # 현재 종가 >= 이전 시가
        patterns.append("BULLISH_ENGULFING")
    
    # 하락 잉골핑: 이전 양봉을 현재 음봉이 완전히 감싸는 경우
    if (prev["close"] > prev["open"] and  # 이전 양봉
        last["close"] < last["open"] and   # 현재 음봉
        last["open"] >= prev["close"] and  # 현재 시가 >= 이전 종가
        last["close"] <= prev["open"]):    # 현재 종가 <= 이전 시가
        patterns.append("BEARISH_ENGULFING")
    
    return {
        "detected": patterns,
        "last_candle": {
            "body_ratio": round(body_ratio, 3),
            "upper_wick_ratio": round(upper_wick / total_range, 3) if total_range > 0 else 0,
            "lower_wick_ratio": round(lower_wick / total_range, 3) if total_range > 0 else 0,
            "is_bullish": last["close"] > last["open"]
        }
    }


def _detect_divergence(df: pd.DataFrame) -> Dict[str, Any]:
    """
    가격-RSI 다이버전스 감지
    
    최근 20봉 내에서 가격 고점/저점과 RSI 고점/저점의 불일치를 탐지.
    """
    if len(df) < 20:
        return {"type": "NONE", "description": None}
    
    recent = df.tail(20)
    
    # 최근 고점 2개, 저점 2개 찾기 (단순화: 전반 10봉 vs 후반 10봉)
    first_half = recent.iloc[:10]
    second_half = recent.iloc[10:]
    
    # 고점 비교 (가격은 오르는데 RSI는 내리면 = 약세 다이버전스)
    price_high_1 = float(first_half["high"].max())
    price_high_2 = float(second_half["high"].max())
    rsi_at_high_1 = float(first_half["rsi"].iloc[first_half["high"].values.argmax()])
    rsi_at_high_2 = float(second_half["rsi"].iloc[second_half["high"].values.argmax()])
    
    # 저점 비교 (가격은 내리는데 RSI는 오르면 = 강세 다이버전스)
    price_low_1 = float(first_half["low"].min())
    price_low_2 = float(second_half["low"].min())
    rsi_at_low_1 = float(first_half["rsi"].iloc[first_half["low"].values.argmin()])
    rsi_at_low_2 = float(second_half["rsi"].iloc[second_half["low"].values.argmin()])
    
    # 약세 다이버전스: 가격 Higher High + RSI Lower High
    if price_high_2 > price_high_1 and rsi_at_high_2 < rsi_at_high_1 - 3:
        return {
            "type": "BEARISH",
            "description": f"가격 상승({price_high_1:.0f}→{price_high_2:.0f}) but RSI 하락({rsi_at_high_1:.1f}→{rsi_at_high_2:.1f})"
        }
    
    # 강세 다이버전스: 가격 Lower Low + RSI Higher Low
    if price_low_2 < price_low_1 and rsi_at_low_2 > rsi_at_low_1 + 3:
        return {
            "type": "BULLISH",
            "description": f"가격 하락({price_low_1:.0f}→{price_low_2:.0f}) but RSI 상승({rsi_at_low_1:.1f}→{rsi_at_low_2:.1f})"
        }
    
    return {"type": "NONE", "description": None}


def get_timeframe_summary(df: pd.DataFrame) -> Dict[str, Any]:
    """
    멀티 타임프레임용 요약 지표 생성
    
    4H, 15m 등 보조 타임프레임에서 핵심 정보만 추출.
    전체 지표를 보낼 필요 없이 방향/모멘텀/변동성만.
    
    Args:
        df: 해당 타임프레임의 OHLCV+지표 DataFrame
    
    Returns:
        요약 딕셔너리
    """
    if df.empty or len(df) < 50:
        return {}
    
    last = df.iloc[-1]
    
    # EMA 구조 판단
    ema_bullish = last["ema_20"] > last["ema_50"]
    price_vs_ema20 = "above" if last["close"] > last["ema_20"] else "below"
    
    # MACD 방향
    macd_direction = "bullish" if last["macd_histogram"] > 0 else "bearish"
    macd_accelerating = abs(last["macd_histogram"]) > abs(df.iloc[-2]["macd_histogram"])
    
    # ADX + DI
    adx_val = float(last["adx"]) if not pd.isna(last["adx"]) else 0
    plus_di_val = float(last["plus_di"]) if "plus_di" in df.columns and not pd.isna(last["plus_di"]) else 0
    minus_di_val = float(last["minus_di"]) if "minus_di" in df.columns and not pd.isna(last["minus_di"]) else 0
    
    return {
        "close": round(float(last["close"]), 2),
        "rsi": round(float(last["rsi"]), 1),
        "adx": round(adx_val, 1),
        "plus_di": round(plus_di_val, 1),
        "minus_di": round(minus_di_val, 1),
        "macd_histogram": round(float(last["macd_histogram"]), 2),
        "macd_direction": macd_direction,
        "macd_accelerating": macd_accelerating,
        "ema_20_50_bullish": ema_bullish,
        "price_vs_ema20": price_vs_ema20,
        "atr_pct": round(float(last["atr_pct"]), 3),
        "volume_ratio": round(float(last["volume_ratio"]), 2),
        "bb_width": round(float(last["bb_width"]), 2)
    }


def analyze_trend(df: pd.DataFrame) -> Dict[str, Any]:
    """
    추세 분석
    
    Returns:
        추세 정보 딕셔너리
    """
    if len(df) < 50:
        return {"trend": "UNKNOWN", "strength": 0}
    
    last = df.iloc[-1]
    
    # EMA 배열 분석
    ema_bullish = (
        last["ema_20"] > last["ema_50"] > last["ema_120"] > last["ema_200"]
    )
    ema_bearish = (
        last["ema_20"] < last["ema_50"] < last["ema_120"] < last["ema_200"]
    )
    
    # 가격 위치
    price_above_emas = (
        last["close"] > last["ema_20"] and
        last["close"] > last["ema_50"]
    )
    price_below_emas = (
        last["close"] < last["ema_20"] and
        last["close"] < last["ema_50"]
    )
    
    # RSI 상태
    rsi = last["rsi"]
    rsi_overbought = rsi > 70
    rsi_oversold = rsi < 30
    
    # MACD 상태
    macd_bullish = last["macd_histogram"] > 0
    macd_bearish = last["macd_histogram"] < 0
    
    # 추세 판단
    if ema_bullish and price_above_emas and macd_bullish:
        trend = "STRONG_BULLISH"
        strength = 9
    elif ema_bullish and price_above_emas:
        trend = "BULLISH"
        strength = 7
    elif ema_bearish and price_below_emas and macd_bearish:
        trend = "STRONG_BEARISH"
        strength = 9
    elif ema_bearish and price_below_emas:
        trend = "BEARISH"
        strength = 7
    elif price_above_emas:
        trend = "WEAK_BULLISH"
        strength = 5
    elif price_below_emas:
        trend = "WEAK_BEARISH"
        strength = 5
    else:
        trend = "NEUTRAL"
        strength = 3
    
    # ADX 기반 추세 강도 보정
    adx = float(last["adx"]) if not pd.isna(last["adx"]) else 0
    is_trending = adx > 25
    
    # +DI/-DI 포함
    plus_di = float(last["plus_di"]) if "plus_di" in df.columns and not pd.isna(last["plus_di"]) else 0
    minus_di = float(last["minus_di"]) if "minus_di" in df.columns and not pd.isna(last["minus_di"]) else 0
    
    return {
        "trend": trend,
        "strength": strength,
        "ema_aligned_bullish": ema_bullish,
        "ema_aligned_bearish": ema_bearish,
        "rsi": rsi,
        "rsi_overbought": rsi_overbought,
        "rsi_oversold": rsi_oversold,
        "macd_bullish": macd_bullish,
        "adx": adx,
        "plus_di": plus_di,
        "minus_di": minus_di,
        "is_trending": is_trending
    }


# ============================================================
# Profit Guard - 추세 반전 감지
# ============================================================

def calculate_profit_guard_indicators(
    df: pd.DataFrame,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14
) -> Dict[str, Any]:
    """
    Profit Guard용 지표 계산 (5분봉 기반)
    
    기존 add_technical_indicators와 분리하여 필요한 지표만 경량 계산.
    메모리 효율을 위해 MACD 히스토그램과 RSI만 산출.
    """
    if len(df) < macd_slow + macd_signal:
        logger.warning(f"Profit Guard: 데이터 부족 ({len(df)}개, 최소 {macd_slow + macd_signal}개 필요)")
        return {}
    
    close = df["close"]
    
    # MACD 히스토그램
    macd = calculate_macd(close, macd_fast, macd_slow, macd_signal)
    histogram = macd["histogram"]
    
    # RSI
    rsi = calculate_rsi(close, rsi_period)
    
    # 최근 2개 히스토그램 값 (반전 판단용)
    hist_current = float(histogram.iloc[-1])
    hist_previous = float(histogram.iloc[-2])
    
    # RSI 최근 값 + 직근 고/저점 (반전 폭 판단용)
    rsi_current = float(rsi.iloc[-1])
    rsi_window = rsi.iloc[-10:]
    rsi_recent_high = float(rsi_window.max())
    rsi_recent_low = float(rsi_window.min())
    
    return {
        "macd_hist_current": hist_current,
        "macd_hist_previous": hist_previous,
        "rsi_current": rsi_current,
        "rsi_recent_high": rsi_recent_high,
        "rsi_recent_low": rsi_recent_low
    }


def detect_trend_reversal(
    indicators: Dict[str, Any],
    direction: str,
    rsi_threshold: float = 5.0
) -> Dict[str, Any]:
    """
    Profit Guard 추세 반전 감지
    
    조건 1: MACD 히스토그램 방향 반전
    조건 2: RSI 역방향 이동 threshold 이상
    두 조건 동시 충족 시 반전 판정.
    """
    if not indicators:
        return {"reversal_detected": False, "reason": "지표 데이터 없음"}
    
    hist_curr = indicators["macd_hist_current"]
    hist_prev = indicators["macd_hist_previous"]
    rsi_curr = indicators["rsi_current"]
    rsi_high = indicators["rsi_recent_high"]
    rsi_low = indicators["rsi_recent_low"]
    
    if direction == "LONG":
        macd_reversed = hist_prev > 0 and hist_curr <= 0
        rsi_reversed = (rsi_high - rsi_curr) >= rsi_threshold
    elif direction == "SHORT":
        macd_reversed = hist_prev < 0 and hist_curr >= 0
        rsi_reversed = (rsi_curr - rsi_low) >= rsi_threshold
    else:
        return {"reversal_detected": False, "reason": f"잘못된 방향: {direction}"}
    
    reversal_detected = macd_reversed and rsi_reversed
    
    result = {
        "reversal_detected": reversal_detected,
        "macd_reversed": macd_reversed,
        "rsi_reversed": rsi_reversed,
        "macd_hist_previous": hist_prev,
        "macd_hist_current": hist_curr,
        "rsi_current": rsi_curr,
        "rsi_recent_high": rsi_high,
        "rsi_recent_low": rsi_low,
        "direction": direction
    }
    
    if reversal_detected:
        if direction == "LONG":
            result["reason"] = (
                f"MACD 양→음 ({hist_prev:.4f}→{hist_curr:.4f}) + "
                f"RSI 고점대비 {rsi_high - rsi_curr:.1f} 하락"
            )
        else:
            result["reason"] = (
                f"MACD 음→양 ({hist_prev:.4f}→{hist_curr:.4f}) + "
                f"RSI 저점대비 {rsi_curr - rsi_low:.1f} 상승"
            )
    else:
        result["reason"] = "반전 조건 미충족"
    
    return result


# ============================================================
# Loss Guard - 추세 악화 감지 (Profit Guard보다 민감)
# ============================================================

def calculate_loss_guard_indicators(
    df: pd.DataFrame,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14,
    ema_short: int = 20,
    ema_long: int = 50
) -> Dict[str, Any]:
    """
    Loss Guard용 지표 계산 (5분봉 기반)
    """
    min_required = macd_slow + macd_signal
    if len(df) < min_required:
        logger.warning(f"Loss Guard: 데이터 부족 ({len(df)}개, 최소 {min_required}개 필요)")
        return {}
    
    close = df["close"]
    
    # MACD
    macd = calculate_macd(close, macd_fast, macd_slow, macd_signal)
    histogram = macd["histogram"]
    hist_current = float(histogram.iloc[-1])
    hist_previous = float(histogram.iloc[-2])
    
    # RSI
    rsi = calculate_rsi(close, rsi_period)
    rsi_current = float(rsi.iloc[-1])
    rsi_window = rsi.iloc[-10:]
    rsi_recent_high = float(rsi_window.max())
    rsi_recent_low = float(rsi_window.min())
    
    # EMA
    ema_s = calculate_ema(close, ema_short)
    ema_l = calculate_ema(close, ema_long)
    
    return {
        "macd_hist_current": hist_current,
        "macd_hist_previous": hist_previous,
        "rsi_current": rsi_current,
        "rsi_recent_high": rsi_recent_high,
        "rsi_recent_low": rsi_recent_low,
        "ema_short_current": float(ema_s.iloc[-1]),
        "ema_long_current": float(ema_l.iloc[-1]),
        "ema_short_previous": float(ema_s.iloc[-2]),
        "ema_long_previous": float(ema_l.iloc[-2])
    }


def detect_loss_deterioration(
    indicators: Dict[str, Any],
    direction: str,
    rsi_threshold: float = 5.0
) -> Dict[str, Any]:
    """
    Loss Guard 추세 악화 감지
    
    A, B, C 중 2개 이상 충족 시 악화 판정.
    """
    if not indicators:
        return {"deterioration_detected": False, "reason": "지표 데이터 없음"}
    
    hist_curr = indicators["macd_hist_current"]
    hist_prev = indicators["macd_hist_previous"]
    rsi_curr = indicators["rsi_current"]
    rsi_high = indicators["rsi_recent_high"]
    rsi_low = indicators["rsi_recent_low"]
    ema_s_curr = indicators["ema_short_current"]
    ema_l_curr = indicators["ema_long_current"]
    ema_s_prev = indicators["ema_short_previous"]
    ema_l_prev = indicators["ema_long_previous"]
    
    if direction == "LONG":
        macd_against = hist_prev > 0 and hist_curr <= 0
        rsi_against = (rsi_high - rsi_curr) >= rsi_threshold
        ema_against = (ema_s_prev >= ema_l_prev) and (ema_s_curr < ema_l_curr)
    elif direction == "SHORT":
        macd_against = hist_prev < 0 and hist_curr >= 0
        rsi_against = (rsi_curr - rsi_low) >= rsi_threshold
        ema_against = (ema_s_prev <= ema_l_prev) and (ema_s_curr > ema_l_curr)
    else:
        return {"deterioration_detected": False, "reason": f"잘못된 방향: {direction}"}
    
    signals = [macd_against, rsi_against, ema_against]
    signal_count = sum(signals)
    deterioration_detected = signal_count >= 2
    
    result = {
        "deterioration_detected": deterioration_detected,
        "signal_count": signal_count,
        "macd_against": macd_against,
        "rsi_against": rsi_against,
        "ema_against": ema_against,
        "direction": direction
    }
    
    if deterioration_detected:
        triggered = []
        if macd_against:
            direction_label = "양→음" if direction == "LONG" else "음→양"
            triggered.append(f"MACD {direction_label} ({hist_prev:.4f}→{hist_curr:.4f})")
        if rsi_against:
            if direction == "LONG":
                triggered.append(f"RSI 하락 ({rsi_high:.1f}→{rsi_curr:.1f})")
            else:
                triggered.append(f"RSI 상승 ({rsi_low:.1f}→{rsi_curr:.1f})")
        if ema_against:
            cross_type = "데드크로스" if direction == "LONG" else "골든크로스"
            triggered.append(f"EMA {cross_type}")
        result["reason"] = " | ".join(triggered)
    else:
        result["reason"] = "악화 조건 미충족"
    
    return result


# ============================================================
# Trigger Monitor - WAIT 대기 중 진입 신호 감지
# ============================================================

def calculate_trigger_indicators(
    df: pd.DataFrame,
    macd_fast: int = 12,
    macd_slow: int = 26,
    macd_signal: int = 9,
    rsi_period: int = 14,
    atr_period: int = 14
) -> Dict[str, Any]:
    """
    Trigger Monitor용 지표 계산 (5분봉 기반)
    """
    min_required = macd_slow + macd_signal
    if len(df) < min_required:
        logger.warning(f"Trigger Monitor: 데이터 부족 ({len(df)}개, 최소 {min_required}개 필요)")
        return {}
    
    close = df["close"]
    high = df["high"]
    low = df["low"]
    
    macd = calculate_macd(close, macd_fast, macd_slow, macd_signal)
    histogram = macd["histogram"]
    rsi = calculate_rsi(close, rsi_period)
    atr = calculate_atr(high, low, close, atr_period)
    
    hist_current = float(histogram.iloc[-1])
    hist_previous = float(histogram.iloc[-2])
    rsi_current = float(rsi.iloc[-1])
    atr_current = float(atr.iloc[-1])
    
    atr_window = atr.iloc[-20:].dropna()
    atr_mean = float(atr_window.mean()) if len(atr_window) > 0 else atr_current
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


def detect_entry_trigger(
    indicators: Dict[str, Any],
    rsi_overbought: float = 70.0,
    rsi_oversold: float = 30.0,
    atr_spike_multiplier: float = 2.0
) -> Dict[str, Any]:
    """
    WAIT 대기 중 진입 트리거 감지
    
    3가지 독립 조건, 1개라도 충족 시 트리거 발동.
    """
    if not indicators:
        return {"triggered": False, "reason": "지표 데이터 없음"}
    
    hist_curr = indicators["macd_hist_current"]
    hist_prev = indicators["macd_hist_previous"]
    rsi_curr = indicators["rsi_current"]
    atr_ratio = indicators["atr_ratio"]
    
    macd_cross = (hist_prev > 0 and hist_curr <= 0) or \
                 (hist_prev < 0 and hist_curr >= 0)
    rsi_extreme = rsi_curr >= rsi_overbought or rsi_curr <= rsi_oversold
    atr_spike = atr_ratio >= atr_spike_multiplier
    
    triggered = macd_cross or rsi_extreme or atr_spike
    
    result = {
        "triggered": triggered,
        "macd_cross": macd_cross,
        "rsi_extreme": rsi_extreme,
        "atr_spike": atr_spike,
        "macd_hist_previous": hist_prev,
        "macd_hist_current": hist_curr,
        "rsi_current": rsi_curr,
        "atr_ratio": atr_ratio,
        "atr_current": indicators["atr_current"],
        "atr_mean": indicators["atr_mean"],
        "close": indicators["close"]
    }
    
    if triggered:
        reasons = []
        if macd_cross:
            direction = "양→음" if hist_prev > 0 else "음→양"
            reasons.append(f"MACD {direction} ({hist_prev:.4f}→{hist_curr:.4f})")
        if rsi_extreme:
            zone = "과매수" if rsi_curr >= rsi_overbought else "과매도"
            reasons.append(f"RSI {zone} ({rsi_curr:.1f})")
        if atr_spike:
            reasons.append(f"ATR 급증 {atr_ratio:.2f}x")
        result["reason"] = " | ".join(reasons)
    else:
        result["reason"] = "트리거 조건 미충족"
    
    return result