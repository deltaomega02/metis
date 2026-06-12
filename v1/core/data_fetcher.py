# core/data_fetcher.py
# Bybit 데이터 수집 및 처리
# OHLCV + 기술적 지표 + 선물 특화 데이터
# Ver5.1: 멀티 타임프레임 수집 추가

import pandas as pd
from typing import Dict, Any, Optional
import gc

from exchange import bybit_client
from core.technical_analysis import (
    add_technical_indicators,
    get_current_indicators,
    get_timeframe_summary,
    analyze_trend
)
from config import TRADING, get_logger

logger = get_logger("data_fetcher")


class DataFetcher:
    """
    Bybit 데이터 수집기
    
    Phase 1: OHLCV + 기술적 지표 + 선물 특화 데이터 수집
    멀티 타임프레임(4H, 1H, 15m) 지원
    """
    
    TIMEFRAMES = {
        "4h": "240",
        "1h": "60",
        "15m": "15"
    }
    
    def __init__(self):
        self.client = bybit_client
    
    def fetch_ohlcv(
        self,
        symbol: str = TRADING.SYMBOL,
        interval: str = "240",  # 4시간
        limit: int = 200
    ) -> pd.DataFrame:
        """
        OHLCV 데이터 조회
        """
        candles = self.client.get_kline(symbol, interval, limit)
        
        if not candles:
            logger.warning(f"캔들 데이터 없음: {symbol} {interval}")
            return pd.DataFrame()
        
        df = pd.DataFrame(candles)
        df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
        df.set_index("datetime", inplace=True)
        
        return df
    
    def fetch_kline_for_profit_guard(
        self,
        symbol: str = TRADING.SYMBOL,
        interval: str = "5",
        limit: int = 50
    ) -> pd.DataFrame:
        """
        Profit Guard용 경량 OHLCV 조회
        """
        try:
            candles = self.client.get_kline(symbol, interval, limit)
            
            if not candles:
                logger.warning(f"Profit Guard: 캔들 데이터 없음 ({symbol} {interval}m)")
                return pd.DataFrame()
            
            df = pd.DataFrame(candles)
            df["datetime"] = pd.to_datetime(df["timestamp"], unit="ms")
            df.set_index("datetime", inplace=True)
            
            return df
        
        except Exception as e:
            logger.error(f"Profit Guard 데이터 조회 실패: {e}")
            return pd.DataFrame()
    
    def fetch_futures_data(self, symbol: str = TRADING.SYMBOL) -> Dict[str, Any]:
        """
        선물 특화 데이터 조회
        """
        ticker = self.client.get_ticker(symbol)
        
        return {
            "funding_rate": ticker.get("funding_rate", 0),
            "funding_rate_pct": ticker.get("funding_rate", 0) * 100,
            "next_funding_time": ticker.get("next_funding_time", 0),
            "open_interest": ticker.get("open_interest", 0),
            "mark_price": ticker.get("mark_price", 0),
            "index_price": ticker.get("index_price", 0),
            "last_price": ticker.get("last_price", 0),
            "volume_24h": ticker.get("volume_24h", 0),
            "price_change_24h_pct": ticker.get("price_change_24h_pct", 0) * 100
        }
    
    def _fetch_timeframe_summary(
        self,
        symbol: str,
        interval: str,
        limit: int = 200
    ) -> Dict[str, Any]:
        """
        보조 타임프레임 요약 데이터 수집
        
        4H, 15m 등에서 방향/모멘텀/변동성 요약만 추출.
        실패 시 빈 딕셔너리 반환 (메인 분석에 영향 없음).
        """
        try:
            df = self.fetch_ohlcv(symbol, interval, limit)
            if df.empty:
                return {}
            
            df = add_technical_indicators(df)
            summary = get_timeframe_summary(df)
            
            # 메모리 해제
            del df
            gc.collect()
            
            return summary
        
        except Exception as e:
            logger.warning(f"보조 타임프레임 ({interval}) 수집 실패: {e}")
            return {}
    
    def collect_all_data(
        self,
        symbol: str = TRADING.SYMBOL,
        primary_tf: str = "1h"
    ) -> Dict[str, Any]:
        """
        Phase 1 데이터 전체 수집 (멀티 타임프레임 포함)
        
        Returns:
            분석용 전체 데이터 패키지
        """
        logger.info(f"데이터 수집 시작: {symbol}")
        
        try:
            # 1. 주요 타임프레임 OHLCV 수집
            interval = self.TIMEFRAMES.get(primary_tf, "60")
            df = self.fetch_ohlcv(symbol, interval, 200)
            
            if df.empty:
                raise ValueError("OHLCV 데이터 수집 실패")
            
            # 2. 기술적 지표 계산
            df = add_technical_indicators(df)
            
            # 3. 현재 지표 추출 (시계열 + 레벨 + 패턴 포함)
            indicators = get_current_indicators(df)
            
            # 4. 추세 분석
            trend = analyze_trend(df)
            
            # 5. 선물 특화 데이터
            futures_data = self.fetch_futures_data(symbol)
            
            # 6. 멀티 타임프레임 요약 (4H, 15m)
            mtf = {}
            
            tf_4h = self._fetch_timeframe_summary(symbol, "240", 200)
            if tf_4h:
                mtf["4h"] = tf_4h
            
            tf_15m = self._fetch_timeframe_summary(symbol, "15", 200)
            if tf_15m:
                mtf["15m"] = tf_15m
            
            # 7. 결과 패키징
            result = {
                "symbol": symbol,
                "timeframe": primary_tf,
                "candle_count": len(df),
                "latest_candle": {
                    "timestamp": str(df.index[-1]),
                    "open": float(df.iloc[-1]["open"]),
                    "high": float(df.iloc[-1]["high"]),
                    "low": float(df.iloc[-1]["low"]),
                    "close": float(df.iloc[-1]["close"]),
                    "volume": float(df.iloc[-1]["volume"])
                },
                "indicators": indicators,
                "trend_analysis": trend,
                "futures": futures_data,
                "multi_timeframe": mtf,
                "dataframe": df  # 내부 용도 (AI에 전달 안 됨)
            }
            
            logger.info(
                f"데이터 수집 완료: {symbol} 가격={futures_data['last_price']:,.0f} "
                f"RSI={indicators['rsi']:.1f} 추세={trend['trend']} "
                f"MTF=[4H:{'O' if tf_4h else 'X'}, 15m:{'O' if tf_15m else 'X'}]"
            )
            
            return result
        
        except Exception as e:
            logger.error(f"데이터 수집 실패: {e}")
            raise
    
    def get_current_price(self, symbol: str = TRADING.SYMBOL) -> float:
        """현재가 조회"""
        ticker = self.client.get_ticker(symbol)
        return ticker.get("last_price", 0)
    
    def prepare_ai_input(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """
        AI 입력용 데이터 정제
        
        DataFrame 제외하고 JSON 직렬화 가능한 형태로 변환
        """
        # DataFrame 제외
        ai_data = {k: v for k, v in data.items() if k != "dataframe"}
        
        gc.collect()
        
        return ai_data


# 싱글톤 인스턴스
data_fetcher = DataFetcher()