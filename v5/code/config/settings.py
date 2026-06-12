"""Frozen global configuration for the trading engine.

All thresholds (risk, cycle timing, resource caps) live here and are read
process-wide as immutable dataclasses. The engine does not mutate these at
runtime; any change requires editing this file and bumping the prompt version
so the configuration is captured in the registry hash.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Tuple

from dotenv import load_dotenv

# ──────────────────────────────────────────────────────────────────────
# 환경 변수 로드 — .env 가 있으면 우선
# ──────────────────────────────────────────────────────────────────────
_ENV_PATH = Path(__file__).resolve().parent.parent.parent / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH)


# ──────────────────────────────────────────────────────────────────────
# 운영 모드
# ──────────────────────────────────────────────────────────────────────
PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
# Accept either PAPER_INITIAL_BALANCE_USDT or PAPER_INITIAL_BALANCE for back-compat with older .env files.
PAPER_INITIAL_BALANCE_USDT: float = float(
    os.getenv("PAPER_INITIAL_BALANCE_USDT") or os.getenv("PAPER_INITIAL_BALANCE") or "400.0"
)

# Bybit real account seed (for P&L display in dashboard).
# Set this in .env: BYBIT_REAL_SEED_USDT=1000  (your total deposits minus withdrawals, USDT eq.)
# Optional: BYBIT_REAL_SEED_KRW=1370000  (KRW seed snapshot at deposit time, for KRW P&L)
BYBIT_REAL_SEED_USDT: float = float(os.getenv("BYBIT_REAL_SEED_USDT", "0") or "0")
BYBIT_REAL_SEED_KRW: float = float(os.getenv("BYBIT_REAL_SEED_KRW", "0") or "0")

# Bybit testnet vs mainnet — PAPER_MODE면 mainnet public 데이터 사용 (signed call X)
_USE_TESTNET: bool = (
    False if PAPER_MODE else os.getenv("BYBIT_USE_TESTNET", "false").lower() == "true"
)

DATA_DIR: Path = Path(os.getenv("METIS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
LOGS_DIR: Path = Path(os.getenv("METIS_LOGS_DIR", str(Path(__file__).resolve().parent.parent / "logs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ──────────────────────────────────────────────────────────────────────
# Trading / Symbol
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TradingConfig:
    """Universe + per-symbol exchange specs."""

    # Two symbols evaluated each cycle; only one position taken (winner-takes-all).
    SYMBOLS: Tuple[str, ...] = ("SOLUSDT", "ETHUSDT")
    CATEGORY: str = "linear"  # USDT perpetual
    QUOTE_CCY: str = "USDT"

    # Bybit instrument specs as of 2026-05. Lot size and tick affect order rounding.
    @property
    def symbol_specs(self) -> dict:
        return {
            "SOLUSDT": {
                "min_order_qty": 0.1,
                "qty_step": 0.1,
                "qty_precision": 1,
                "price_precision": 4,
                "tick_size": 0.0001,
            },
            "ETHUSDT": {
                "min_order_qty": 0.01,
                "qty_step": 0.01,
                "qty_precision": 2,
                "price_precision": 2,
                "tick_size": 0.01,
            },
        }


TRADING = TradingConfig()


# ──────────────────────────────────────────────────────────────────────
# Bybit API
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class BybitConfig:
    USE_TESTNET: bool = _USE_TESTNET
    API_KEY: str = field(default_factory=lambda: os.getenv("BYBIT_API_KEY", ""))
    SECRET: str = field(default_factory=lambda: os.getenv("BYBIT_SECRET", ""))

    # mainnet public (PAPER도 mainnet public 사용)
    BASE_URL: str = "https://api-testnet.bybit.com" if _USE_TESTNET else "https://api.bybit.com"
    WS_PUBLIC: str = (
        "wss://stream-testnet.bybit.com/v5/public/linear"
        if _USE_TESTNET
        else "wss://stream.bybit.com/v5/public/linear"
    )
    WS_PRIVATE: str = (
        "wss://stream-testnet.bybit.com/v5/private"
        if _USE_TESTNET
        else "wss://stream.bybit.com/v5/private"
    )
    RECV_WINDOW_MS: int = 5000


BYBIT = BybitConfig()


# ──────────────────────────────────────────────────────────────────────
# Gemini
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class GeminiConfig:
    """Gemini Flash decision call parameters."""

    API_KEY: str = field(default_factory=lambda: os.getenv("GEMINI_API_KEY", ""))
    # GA model as of 2026-05-19.
    MODEL_ID: str = os.getenv("GEMINI_MODEL_ID", "gemini-3.5-flash")
    # Low temperature and medium thinking keep outputs deterministic and inside the
    # FinCoT structure required by the prompt.
    TEMPERATURE: float = 0.2
    THINKING_LEVEL: str = "medium"
    # Sized to hold both the thinking trace and the final JSON object; smaller
    # values truncate the response on busy cycles.
    MAX_OUTPUT_TOKENS: int = 8192
    # One retry on schema/parse failure; thereafter the cycle is forced to NO_TRADE.
    MAX_RETRIES: int = 1
    # Hard latency ceiling — exceeding it forces NO_TRADE for the cycle.
    TIMEOUT_SEC: float = 45.0
    # Caching strategy: implicit (server-side, automatic on Gemini 2.5+). The client
    # sends the full prompt with a stable prefix; Gemini reports cache hits via
    # ``cached_content_token_count`` in usage metadata. No explicit cache resource
    # is managed — that avoids TTL/expiry/PermissionDenied failure modes entirely.


GEMINI = GeminiConfig()


# ──────────────────────────────────────────────────────────────────────
# Risk engine — three nested layers (trade / strategy / account)
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class RiskConfig:
    """All deterministic risk thresholds applied between the LLM decision and
    the exchange. Every ENTER must clear L1, L2 and L3."""

    # ── L1 Trade-level ──
    # Stop-loss distance must sit inside this ATR multiple band; tighter than
    # SL_ATR_MIN is whip risk, wider than SL_ATR_MAX is bad RR.
    SL_ATR_MIN: float = 0.6
    SL_ATR_MAX: float = 1.8
    # Absolute SL distance bounds (defends against bad ATR estimates).
    SL_DISTANCE_MIN_PCT: float = 0.0035  # 0.35%
    SL_DISTANCE_MAX_PCT: float = 0.0125  # 1.25%
    # Take-profit R bounds.
    TP_R_MIN: float = 1.2
    TP_R_MAX: float = 3.0
    # Capital at risk per trade. INITIAL applies until the live track record is verified.
    RISK_PER_TRADE_PCT_INITIAL: float = 0.0010  # 0.10%
    RISK_PER_TRADE_PCT_MAX: float = 0.0025      # 0.25%
    # Notional exposure cap as a fraction of equity.
    NOTIONAL_CAP_PCT_PAPER: float = 0.50
    NOTIONAL_CAP_PCT_LIVE_VERIFIED: float = 0.75
    # Leverage policy.
    LEVERAGE_DEFAULT: int = 2     # fallback if AI omits leverage
    LEVERAGE_MIN: int = 1
    LEVERAGE_MAX: int = 5         # hard cap: AI may choose 1..5 based on conviction
    # Time-stop disabled — exit only via structural SL or TP.
    # Trailing stop also off (small-R scalping: trailing kills the R:R).
    TRAILING_DEFAULT_ON: bool = False

    # ── L2 Strategy-level ──
    MAX_CONSECUTIVE_LOSSES: int = 3
    CONFIDENCE_THRESHOLD: float = 0.75
    COOLDOWN_AFTER_1_LOSS_MIN: int = 30
    COOLDOWN_AFTER_2_LOSS_MIN: int = 120
    # On the 3rd consecutive loss the engine kills until the next UTC day starts.

    # ── L3 Account-level ──
    DAILY_LOSS_LIMIT_PCT_INITIAL: float = 0.010  # -1%
    DAILY_LOSS_LIMIT_PCT_VERIFIED: float = 0.020  # -2%
    WEEKLY_LOSS_LIMIT_PCT: float = 0.030
    MAX_DRAWDOWN_KILL_PCT: float = 0.050
    MAX_DRAWDOWN_RETIRE_PCT: float = 0.080
    # Winner-takes-all design: at most one open position at any time.
    MAX_CONCURRENT_POSITIONS: int = 1

    # ── Fees / slippage (Bybit taker model) ──
    TAKER_FEE_PCT: float = 0.00055
    MAKER_FEE_PCT: float = 0.0002
    SLIPPAGE_BPS_ASSUMED: float = 2.0  # 0.02%
    # Spread filter (top-of-book).
    MAX_SPREAD_BPS: float = 5.0  # 0.05%

    # Protection order (SL/TP) must be attached within this window of the entry fill.
    # Exceeding it is a hard violation and triggers an emergency reduce-only close.
    PROTECTION_ORDER_MAX_DELAY_SEC: int = 30


RISK = RiskConfig()


# ──────────────────────────────────────────────────────────────────────
# Cycle / Scheduler
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CycleConfig:
    """Timing parameters for the cycle scheduler and background watchdogs."""

    PRIMARY_TF_MIN: int = 15
    # Fire each cycle a few seconds after the 15m candle closes so the bar is final.
    CYCLE_BUFFER_SEC: int = 10
    # Per-stage SLAs (the LLM has its own TIMEOUT_SEC in GEMINI).
    FEATURE_BUILD_TIMEOUT_SEC: float = 8.0
    GEMINI_CALL_TIMEOUT_SEC: float = 45.0
    # Position monitor polling interval.
    POSITION_POLL_INTERVAL_SEC: float = 15.0
    # WebSocket staleness threshold (ticker/orderbook age).
    WS_MAX_STALE_SEC: int = 90
    # NTP drift thresholds vs exchange server time.
    NTP_OFFSET_WARN_MS: int = 500
    NTP_OFFSET_BLOCK_MS: int = 1000
    # Daily aggregator fires at UTC 00:01.
    DAILY_AGG_UTC_HOUR: int = 0
    DAILY_AGG_UTC_MIN: int = 1


CYCLE = CycleConfig()


# ──────────────────────────────────────────────────────────────────────
# Memory / resource caps — tuned for a 1 GB micro VM
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class ResourceConfig:
    """In-process buffer and pool sizes. Tightened for low-memory hosts."""

    # SQLite WAL cache size.
    SQLITE_CACHE_KB: int = 4096  # 4 MB
    SQLITE_WAL_CHECKPOINT_INTERVAL_SEC: int = 300
    # WebSocket inbound queue cap.
    WS_QUEUE_MAXSIZE: int = 256
    # Stream-worker rolling windows.
    STREAM_KLINE_MAX_BARS: int = 256
    STREAM_TICKER_MAX_TICKS: int = 128
    # Outbound HTTP pool.
    HTTP_POOL_CONNECTIONS: int = 4
    HTTP_POOL_MAXSIZE: int = 8
    # Disk-free safety threshold — below this, new entries are blocked.
    DISK_FREE_MIN_PCT: float = 0.10
    # Run gc.collect() after each cycle to keep RSS flat.
    AGGRESSIVE_GC: bool = True


RES = ResourceConfig()


# ──────────────────────────────────────────────────────────────────────
# Telegram (선택)
# ──────────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class TelegramConfig:
    BOT_TOKEN: str = field(default_factory=lambda: os.getenv("TELEGRAM_BOT_TOKEN", ""))
    CHAT_ID: str = field(default_factory=lambda: os.getenv("TELEGRAM_CHAT_ID", ""))
    ENABLED: bool = field(default_factory=lambda: bool(os.getenv("TELEGRAM_BOT_TOKEN")))


TG = TelegramConfig()


# ──────────────────────────────────────────────────────────────────────
# DB paths
# ──────────────────────────────────────────────────────────────────────
STATE_DB_PATH: Path = DATA_DIR / "state.db"
TELEMETRY_DB_PATH: Path = DATA_DIR / "telemetry.db"
EVENT_YAML_PATH: Path = Path(__file__).resolve().parent / "events.yaml"
