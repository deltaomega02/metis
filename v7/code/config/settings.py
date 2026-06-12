"""Frozen global configuration for the AI-judgment engine (METIS revival).

Unlike the rule-based sibling, here the LLM decides direction, stop, target,
and leverage; this file holds the universe, the indicator inputs fed to the AI,
the model parameters, and the deterministic sanity bounds the AI output must
clear (leverage clamp, liquidation margin, net-R gate). Strategy "judgment"
lives in the prompt, not here.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV.exists():
    load_dotenv(_ENV)

PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
PAPER_INITIAL_BALANCE_USDT: float = float(os.getenv("PAPER_INITIAL_BALANCE_USDT", "1000"))

DATA_DIR: Path = Path(os.getenv("METIS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
LOGS_DIR: Path = Path(os.getenv("METIS_LOGS_DIR", str(Path(__file__).resolve().parent.parent / "logs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class TradingConfig:
    SYMBOLS: Tuple[str, ...] = ("BTCUSDT", "SOLUSDT")
    CATEGORY: str = "linear"
    QUOTE_CCY: str = "USDT"

    @property
    def fallback_specs(self) -> dict:
        return {
            "BTCUSDT": {"qty_step": 0.001, "qty_precision": 3, "tick_size": 0.1,   "min_order_qty": 0.001},
            "SOLUSDT": {"qty_step": 0.1,   "qty_precision": 1, "tick_size": 0.001, "min_order_qty": 0.1},
        }


TRADING = TradingConfig()


@dataclass(frozen=True)
class AnalysisConfig:
    """Indicator inputs fed to the AI. 1H primary + 4H/1D context (METIS MTF)."""

    PRIMARY_INTERVAL: str = "60"        # 1H entry timing
    CONTEXT_INTERVALS: Tuple[str, ...] = ("240", "D")  # 4H, 1D regime context
    KLINE_LOOKBACK: int = 200
    EMA_PERIODS: Tuple[int, ...] = (20, 50, 200)
    RSI_LEN: int = 14
    ADX_LEN: int = 14
    ATR_LEN: int = 14
    DONCHIAN_N: int = 20               # breakout reference fed to the AI


ANALYSIS = AnalysisConfig()


@dataclass(frozen=True)
class GeminiConfig:
    API_KEY: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    MODEL_ID: str = os.getenv("GEMINI_MODEL_ID", "gemini-3.5-flash")
    TEMPERATURE: float = 0.3
    THINKING_LEVEL: str = "medium"
    MAX_OUTPUT_TOKENS: int = 4096
    MAX_RETRIES: int = 1
    TIMEOUT_SEC: float = 45.0


GEMINI = GeminiConfig()


@dataclass(frozen=True)
class RiskConfig:
    """Deterministic guards the AI's proposal must clear. The AI sets stop/target/
    leverage; these bound it. Net-R gate is the edge guard (cost-aware)."""

    RISK_PER_TRADE: float = 0.005      # equity fraction risked per trade (sizing)
    MAX_CONCURRENT: int = 2            # BTC + SOL → at most 2 open
    LEVERAGE_MIN: int = 1
    LEVERAGE_MAX: int = 7              # AI may pick 1..7 by conviction
    MIN_NET_R: float = 1.5            # reject if (target-entry)/(entry-stop) net of fees < this
    SL_MIN_PCT: float = 0.004          # stop at least 0.4% from entry
    SL_MAX_PCT: float = 0.06           # and at most 6%
    LIQ_MARGIN_PCT: float = 0.02       # stop must sit ≥2% inside the liquidation price
    MAINTENANCE_MARGIN: float = 0.004
    TAKER_FEE_PCT: float = 0.00055
    SLIPPAGE_BPS_ASSUMED: float = 2.0
    # account guards
    DAILY_LOSS_LIMIT_PCT: float = 0.05
    MAX_DRAWDOWN_KILL_PCT: float = 0.25


RISK = RiskConfig()


@dataclass(frozen=True)
class CycleConfig:
    PRIMARY_TF_MIN: int = 60           # 1H anchored cycle
    CYCLE_BUFFER_SEC: int = 15
    # AI-suggested recheck window, clamped:
    RECHECK_MIN_HOURS: float = 0.5
    RECHECK_MAX_HOURS: float = 12.0
    RECHECK_DEFAULT_HOURS: float = 2.0
    DAILY_REPORT_UTC_HOUR: int = 0
    DAILY_REPORT_UTC_MIN: int = 5


CYCLE = CycleConfig()


@dataclass(frozen=True)
class ResourceConfig:
    SQLITE_CACHE_KB: int = 2048
    WAL_CHECKPOINT_INTERVAL_SEC: int = 300
    HTTP_POOL_MAXSIZE: int = 8
    HTTP_SEMAPHORE: int = 3
    DISK_FREE_MIN_PCT: float = 0.10


RES = ResourceConfig()


@dataclass(frozen=True)
class BybitConfig:
    API_KEY: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    SECRET: str = field(default_factory=lambda: os.getenv("BYBIT_SECRET", ""))
    USE_TESTNET: bool = field(default_factory=lambda: (not PAPER_MODE) and os.getenv("BYBIT_USE_TESTNET", "false").lower() == "true")
    RECV_WINDOW_MS: int = 5000

    @property
    def base_url(self) -> str:
        return "https://api-testnet.bybit.com" if self.USE_TESTNET else "https://api.bybit.com"


BYBIT = BybitConfig()


@dataclass(frozen=True)
class TelegramConfig:
    BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    ENABLED: bool = field(default_factory=lambda: bool(os.getenv("TELEGRAM_BOT_TOKEN")))


TG = TelegramConfig()

REAL_SEED_USDT: float = float(os.getenv("BYBIT_REAL_SEED_USDT", "0") or "0")
REAL_SEED_KRW: float = float(os.getenv("BYBIT_REAL_SEED_KRW", "0") or "0")
STATE_DB_PATH: Path = DATA_DIR / "state.db"
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8502"))
