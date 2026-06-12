# config/settings.py
# METIS-F 전역 설정
# 리스크 파라미터, API 엔드포인트 정의
# Ver1.0 confidence→leverage 매핑 복원 + Ver3 인프라 안전장치 유지

import os
from dotenv import load_dotenv
from dataclasses import dataclass, field
from typing import Dict, Tuple, Any

load_dotenv()

# 환경변수 평가
PAPER_MODE = os.getenv("PAPER_MODE", "false").lower() == "true"
PAPER_INITIAL_BALANCE = float(os.getenv("PAPER_INITIAL_BALANCE", "500.0"))

# PAPER_MODE에서는 mainnet public API 사용 (가격 데이터). signed call은 paper_executor가 처리.
if PAPER_MODE:
    _USE_TESTNET = False
else:
    _USE_TESTNET = os.getenv("BYBIT_USE_TESTNET", "true").lower() == "true"


@dataclass(frozen=True)
class BybitConfig:
    """Bybit API 설정"""
    # API 키
    API_KEY: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    SECRET: str = field(default_factory=lambda: os.getenv("BYBIT_SECRET", ""))
    TESTNET_API_KEY: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_API_KEY", ""))
    TESTNET_SECRET: str = field(default_factory=lambda: os.getenv("BYBIT_TESTNET_SECRET", ""))
    USE_TESTNET: bool = _USE_TESTNET
    
    # 엔드포인트 (미리 평가된 _USE_TESTNET 사용)
    BASE_URL: str = "https://api-testnet.bybit.com" if _USE_TESTNET else "https://api.bybit.com"
    WS_PUBLIC: str = "wss://stream-testnet.bybit.com/v5/public/linear" if _USE_TESTNET else "wss://stream.bybit.com/v5/public/linear"
    WS_PRIVATE: str = "wss://stream-testnet.bybit.com/v5/private" if _USE_TESTNET else "wss://stream.bybit.com/v5/private"


@dataclass(frozen=True)
class TradingConfig:
    """거래 파라미터"""
    SYMBOL: str = "BTCUSDT"  # primary (default, fallback)
    CATEGORY: str = "linear"  # USDT Perpetual

    # 멀티 심볼: BTC + XRP 동시 감시. 시그널 강한 쪽 진입 (단일 포지션).
    SYMBOLS: Tuple[str, ...] = ("BTCUSDT", "XRPUSDT")

    # symbol별 주문 spec (Bybit USDT perpetual)
    SYMBOL_SPECS: Dict[str, Dict[str, Any]] = None

    # 레버리지 범위 (confidence→leverage 매핑으로 결정, 최대 7x)
    MIN_LEVERAGE: int = 1
    MAX_LEVERAGE: int = 7
    
    # 확신도 → 레버리지 매핑
    CONFIDENCE_LEVERAGE_MAP: Dict[Tuple[int, int], int] = None
    
    # 리스크 파라미터
    STOP_LOSS_MARGIN_PCT: float = 0.02  # 손절가는 청산가보다 최소 2% 마진
    LIQUIDATION_WARN_PCT: float = 0.03  # 청산가 3% 이내 접근 시 경고
    MAX_LOSS_PER_TRADE_PCT: float = 0.10  # 1회 최대 손실: 시드의 10%
    
    # 수수료
    TAKER_FEE_PCT: float = 0.00055  # 0.055%
    MAKER_FEE_PCT: float = 0.0002   # 0.02%
    
    # 최소 목표 수익률 (수수료 감안)
    MIN_TARGET_PROFIT_PCT: float = 0.015  # 1.5%
    MIN_RR_RATIO: float = 1.5  # R:R 최소 1:1.5
    
    # 유지마진율 (Bybit BTC)
    MAINTENANCE_MARGIN_RATE: float = 0.004  # 0.4%
    
    # BTC 최소 주문 단위 (default, BTCUSDT)
    MIN_ORDER_QTY: float = 0.001
    QTY_PRECISION: int = 3  # 소수점 3자리

    def __post_init__(self):
        leverage_map = {
            (9, 10): 7,
            (7, 8): 7,
            (5, 6): 5,
            (3, 4): 2,
        }
        object.__setattr__(self, 'CONFIDENCE_LEVERAGE_MAP', leverage_map)

        # symbol별 주문 spec (Bybit USDT perp)
        symbol_specs = {
            "BTCUSDT": {"min_order_qty": 0.001, "qty_precision": 3, "price_precision": 2},
            "XRPUSDT": {"min_order_qty": 1.0, "qty_precision": 0, "price_precision": 4},
        }
        object.__setattr__(self, 'SYMBOL_SPECS', symbol_specs)


def get_symbol_spec(symbol: str) -> dict:
    """symbol별 주문 spec 반환. 없으면 default (BTC)."""
    return TRADING.SYMBOL_SPECS.get(symbol, {
        "min_order_qty": 0.001,
        "qty_precision": 3,
        "price_precision": 2
    })


@dataclass(frozen=True)
class TelegramConfig:
    """텔레그램 설정"""
    BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))


@dataclass(frozen=True)
class GeminiConfig:
    """Gemini AI 설정"""
    API_KEY: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    # Target Model: Gemini 3.1 Pro (Released Jan 2026)
    # Do not revert to gemini-1.5 or 2.5
    MODEL_ID: str = "gemini-3.1-pro-preview"


@dataclass(frozen=True)
class SchedulerConfig:
    """스케줄러 설정"""
    # Phase 1 주기 (포지션 없을 때)
    ANALYSIS_INTERVAL_HOURS: int = 1
    
    # Dead Man's Switch 타임아웃
    DEAD_MANS_SWITCH_TIMEOUT_SEC: int = 60
    
    # 중간 점검 기본 주기
    # 운영자 의도 (1D/4H/1H 큰 틀): 2H → 4H
    DEFAULT_RECHECK_HOURS: int = 4

    # 일일 리포트 시간 (KST)
    DAILY_REPORT_HOUR: int = 9
    DAILY_REPORT_MINUTE: int = 0


@dataclass(frozen=True)
class ProfitGuardConfig:
    """Profit Guard 설정 - 추세 반전 감지 시 수익 보호 청산"""
    # 활성화 임계치 (레버리지 포함 미실현 수익률)
    ACTIVATION_PCT: float = 0.06  # 6%
    
    # 감시 주기 (초)
    CHECK_INTERVAL_SEC: int = 60
    
    # 운영자 의도 (큰 틀): 5분봉 → 15분봉. 5분봉 노이즈 차단.
    KLINE_INTERVAL: str = "15"  # 15분봉
    KLINE_LIMIT: int = 50  # 최근 50개 캔들 (15m × 50 = 12.5시간)

    # MACD 파라미터 (표준값)
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9

    # RSI 파라미터
    RSI_PERIOD: int = 14
    RSI_REVERSAL_THRESHOLD: float = 5.0  # 역방향 5 이상 이동 시 반전 판정


@dataclass(frozen=True)
class ChartConfig:
    """차트 생성 설정 (메모리 최적화)"""
    FIGSIZE: Tuple[int, int] = (10, 6)
    DPI: int = 100  # 100 초과 금지
    FORMAT: str = "png"


@dataclass(frozen=True)
class TriggerMonitorConfig:
    """Trigger Monitor 설정.

    운영자 의도 (1D/4H/1H 큰 틀)에 맞춰 비활성화 (ENABLED=False).
    1H 정기 재분석으로 큰 변동 충분히 잡음. 5분봉 노이즈 트리거 X.
    """
    # 비활성 — wait_for_trigger가 단순 sleep으로 동작
    ENABLED: bool = False

    # 아래는 ENABLED=True 시에만 사용 (현재 미사용)
    CHECK_INTERVAL_SEC: int = 120
    COOLDOWN_SEC: int = 1800
    KLINE_INTERVAL: str = "5"
    KLINE_LIMIT: int = 50
    MACD_FAST: int = 12
    MACD_SLOW: int = 26
    MACD_SIGNAL: int = 9
    RSI_PERIOD: int = 14
    RSI_OVERBOUGHT: float = 65.0
    RSI_OVERSOLD: float = 35.0
    ATR_SPIKE_MULTIPLIER: float = 1.5
    PRICE_MOVE_LOOKBACK: int = 6
    PRICE_MOVE_THRESHOLD_PCT: float = 1.2
    
    # 단기 급변 감지 (더 짧은 윈도우, 더 낮은 임계)
    PRICE_SPIKE_LOOKBACK: int = 3      # 최근 3캔들 (5분봉 × 3 = 15분)
    PRICE_SPIKE_THRESHOLD_PCT: float = 0.8  # 15분 내 0.8% 이상 이동 시 단독 발동


# 싱글톤 인스턴스
BYBIT = BybitConfig()
TRADING = TradingConfig()
TELEGRAM = TelegramConfig()
GEMINI = GeminiConfig()
SCHEDULER = SchedulerConfig()
CHART = ChartConfig()
PROFIT_GUARD = ProfitGuardConfig()
TRIGGER_MONITOR = TriggerMonitorConfig()


def get_leverage_from_confidence(confidence: int) -> int:
    """
    확신도에 따른 레버리지 반환
    
    Args:
        confidence: AI 확신도 (1-10)
    
    Returns:
        레버리지 배수 (0이면 진입 거부)
    """
    if confidence >= 7:
        return 7
    elif confidence >= 5:
        return 5
    elif confidence >= 3:
        return 2
    else:
        return 0  # 2점 이하 진입 거부