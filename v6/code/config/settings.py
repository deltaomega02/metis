"""Frozen global configuration for the breakout portfolio engine.

All thresholds — universe, breakout/regime parameters, risk sizing, fees,
resource caps — live here as immutable dataclasses and are read process-wide.
The strategy parameters mirror the values validated in research/ (Donchian-20
break + EMA20/50 trend + ADX>22, structural ATR stop, ride-until-trend-ends);
changing them invalidates that validation, so they are not tuned at runtime.
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


# ── mode ────────────────────────────────────────────────────────────
PAPER_MODE: bool = os.getenv("PAPER_MODE", "true").lower() == "true"
PAPER_INITIAL_BALANCE_USDT: float = float(os.getenv("PAPER_INITIAL_BALANCE_USDT", "1000"))

DATA_DIR: Path = Path(os.getenv("METIS_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")))
LOGS_DIR: Path = Path(os.getenv("METIS_LOGS_DIR", str(Path(__file__).resolve().parent.parent / "logs")))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class TradingConfig:
    """Universe and exchange specs. Specs are fallback defaults; the live
    instrument filters (tick/lot/min-qty) are refreshed from Bybit at boot."""

    SYMBOLS: Tuple[str, ...] = (
        "BTCUSDT", "ETHUSDT", "SOLUSDT", "XRPUSDT",
        "BNBUSDT", "DOGEUSDT", "ADAUSDT", "AVAXUSDT",
    )
    CATEGORY: str = "linear"
    QUOTE_CCY: str = "USDT"

    # Fallback specs (qty_step / qty_precision / tick_size / min_order_qty).
    # Overwritten by bybit_client.refresh_instrument_specs() on startup.
    @property
    def fallback_specs(self) -> dict:
        return {
            "BTCUSDT":  {"qty_step": 0.001, "qty_precision": 3, "tick_size": 0.1,    "min_order_qty": 0.001},
            "ETHUSDT":  {"qty_step": 0.01,  "qty_precision": 2, "tick_size": 0.01,   "min_order_qty": 0.01},
            "SOLUSDT":  {"qty_step": 0.1,   "qty_precision": 1, "tick_size": 0.001,  "min_order_qty": 0.1},
            "XRPUSDT":  {"qty_step": 1.0,   "qty_precision": 0, "tick_size": 0.0001, "min_order_qty": 1.0},
            "BNBUSDT":  {"qty_step": 0.01,  "qty_precision": 2, "tick_size": 0.01,   "min_order_qty": 0.01},
            "DOGEUSDT": {"qty_step": 1.0,   "qty_precision": 0, "tick_size": 0.00001,"min_order_qty": 1.0},
            "ADAUSDT":  {"qty_step": 1.0,   "qty_precision": 0, "tick_size": 0.0001, "min_order_qty": 1.0},
            "AVAXUSDT": {"qty_step": 0.1,   "qty_precision": 1, "tick_size": 0.001,  "min_order_qty": 0.1},
        }


TRADING = TradingConfig()


@dataclass(frozen=True)
class StrategyConfig:
    """Breakout signal parameters — locked to the research-validated values."""

    INTERVAL: str = os.getenv("METIS_INTERVAL", "240")  # primary TF (Bybit code: "240"=4h, "D"=1d); env로 1D 페이퍼 암 구동
    KLINE_LOOKBACK: int = 260      # bars/cycle — enough for the BTC ema200 gate
    EMA_FAST: int = 20
    EMA_SLOW: int = 50
    ADX_LEN: int = 14
    ADX_MIN: float = 22.0          # below → no trend, stand aside
    DONCHIAN_N: int = 20           # breakout lookback (prior N-bar high/low)
    ATR_LEN: int = 14
    ATR_K: float = 1.5             # structural stop distance = K · ATR
    SHORT_TP_R: float = 2.5        # SHORT-only fixed take-profit = R · stop_distance.
    # Asymmetric exit (research/exit_redesign.py): a short's trend-exit (EMA50 re-cross)
    # fires only after price rebounds past entry → gives back the whole gain (OOS expR
    # -0.28). A fixed R-target realizes it instead (OOS +0.10). Longs keep the trend
    # exit (ride the trend, OOS +0.25). So: LONG = trend exit, SHORT = fixed R-target.
    GATE_EMA_LONG: int = 200       # (legacy/unused) — gate switched to ema20>ema50
    # Long-only + BTC uptrend gate = **ema20 > ema50** (btc_uptrend()). Faster than
    # ema50>200; chosen for compounding (higher total return/Sharpe in gate_sweep,
    # thinner per-trade edge — watch live slippage). Shorts net-negative in every
    # realtime regime, so not taken.
    # Exit = ride until the close crosses back through the slow EMA (trend end).
    # No fixed take-profit, no time-stop, no trailing.


STRATEGY = StrategyConfig()


@dataclass(frozen=True)
class RiskConfig:
    """Deterministic risk gate. Long-only + BTC **ema20>50** gate (research/gate_sweep.py:
    higher total return & Sharpe 1.05 than ema50>200, thinner per-trade edge), 0.75%/trade
    + 4 concurrent. Monte-Carlo P(ruin)≈0, liquidation impossible (max stop 9.8% ≪ 33%
    lev-3 band). Per-trade edge thinner → live slippage matters; revisit if fills are bad."""

    RISK_PER_TRADE: float = 0.0075     # fraction of equity risked per trade
    MAX_CONCURRENT: int = 4            # cap on simultaneously open positions
    # Account guards (only hard stop is manual_kill; no cooldown/streak/time-stop).
    DAILY_LOSS_LIMIT_PCT: float = 0.04
    MAX_DRAWDOWN_KILL_PCT: float = 0.25
    # Fees / slippage (Bybit linear taker).
    TAKER_FEE_PCT: float = 0.00055
    SLIPPAGE_BPS_ASSUMED: float = 2.0
    LEVERAGE: int = 3                  # fixed; risk is governed by size, not leverage


RISK = RiskConfig()


@dataclass(frozen=True)
class CycleConfig:
    PRIMARY_TF_MIN: int = int(os.getenv("METIS_TF_MIN", "240"))  # 분 단위 (240=4h, 1440=1d) — INTERVAL과 짝 맞출 것
    CYCLE_BUFFER_SEC: int = int(os.getenv("CYCLE_BUFFER_SEC", "45"))  # fire Ns after the 4h bar closes — avoids the
    # exact-bar-close moment when every 4h bot worldwide hammers Bybit kline at once
    # (measured: our 8-call pattern never trips 10006 in isolation, but cycle-time does;
    # the bar-close stampede tightens the per-IP limit). Signal is unchanged (closed bar).
    FETCH_TIMEOUT_SEC: float = 10.0
    KLINE_FETCH_SPACING_SEC: float = 0.5   # gap between per-coin kline calls so the 8
    # requests don't burst out together. 8 × 0.5s ≈ 4s spread — negligible on a 4h cycle.
    NTP_OFFSET_BLOCK_MS: int = 1000
    DAILY_REPORT_UTC_HOUR: int = 0
    DAILY_REPORT_UTC_MIN: int = 5


CYCLE = CycleConfig()


@dataclass(frozen=True)
class ResourceConfig:
    """In-process caps tuned for a 1 GB micro VM."""

    SQLITE_CACHE_KB: int = 2048
    WAL_CHECKPOINT_INTERVAL_SEC: int = 300
    HTTP_POOL_MAXSIZE: int = 8
    HTTP_SEMAPHORE: int = 3            # concurrent in-flight REST requests
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

# Real-account seed for dashboard P&L display (USD / KRW), optional.
REAL_SEED_USDT: float = float(os.getenv("BYBIT_REAL_SEED_USDT", "0") or "0")
REAL_SEED_KRW: float = float(os.getenv("BYBIT_REAL_SEED_KRW", "0") or "0")

@dataclass(frozen=True)
class Arm:
    """One arm: same breakout signal, different gate / direction / timeframe."""
    name: str             # short id → DB file state_<name>.db ("live"는 예외: state.db)
    gate: str             # "none" | "ema20_50" | "ema50_200"
    label: str            # display
    allow_short: bool = False  # True → also take short breakouts (both-ways)
    tf: str = "240"       # kline interval code ("240"=4h, "D"=1d). 4h보다 긴 TF 암은
    #                       해당 봉 마감 사이클에만 평가(+재기동 시 미처리 봉 캐치업).
    cap: int = 0          # 동시 보유 cap (0 → RISK.MAX_CONCURRENT). 1D 암은 cap1이
    #                       백테스트 최적(research/daily_ls.py: cap2는 겹침 약신호로 총수익↓).
    excl_group: str = ""  # 심볼 배타 그룹: 같은 non-empty 그룹의 암들은 한 심볼을 동시
    #                       보유 못함. 한 계좌(원웨이 positionIdx=0)를 공유할 암 쌍에 필수 —
    #                       넷 포지션 합산+Full모드 TP/SL 덮어쓰기 충돌의 구조적 차단.
    #                       빈 문자열 = 배타 없음(독립 페이퍼 실험 암들).


def tf_minutes(tf: str) -> int:
    """Bybit interval code → minutes ("240"→240, "D"→1440)."""
    return {"D": 1440, "W": 10080, "M": 43200}.get(tf) or int(tf)


# Paper arms — forward-test which configuration compounds. `ls` (long+short, mixed
# exit) is the validated-edge model; none/fast/slow are long-only gate variants.
# LIVE mode runs a single ls arm (the live edge). A paper engine can run a SUBSET via
# PAPER_ARMS_ONLY (comma-separated names) — e.g. PAPER_ARMS_ONLY=ls runs only the ls
# paper arm alongside the separate live process for direct slippage comparison
# (same signal: live = real fill, paper = assumed fill), without re-running the others.
_ALL_PAPER_ARMS: Tuple[Arm, ...] = (
    Arm("none", "none",       "필터없음 (최공격)"),
    Arm("fast", "ema20_50",   "ema20>50 (빠름)"),
    Arm("slow", "ema50_200",  "ema50>200 (보수)"),
    Arm("ls",   "none",       "롱+숏 4h", allow_short=True, excl_group="acct"),
    # 1D 암 — 4h와 월수익 ρ=−0.08 무상관, 결합 시 CAGR↑·MDD동일 (research/daily_ls.py).
    # excl_group="acct": 미래 라이브(한 계좌)와 동형 거동으로 forward-test하기 위해
    # 페이퍼에서도 ls와 심볼 배타.
    Arm("ls1d", "none",       "롱+숏 1D", allow_short=True, tf="D", cap=1, excl_group="acct"),
)
_paper_only = [s.strip() for s in os.getenv("PAPER_ARMS_ONLY", "").split(",") if s.strip()]
PAPER_ARMS: Tuple[Arm, ...] = (
    tuple(a for a in _ALL_PAPER_ARMS if a.name in _paper_only) if _paper_only else _ALL_PAPER_ARMS
)

# LIVE arms — 기본은 4h 단일암(현행과 동일 거동). 1D 라이브 합류는 LIVE_ARMS_ONLY=live,live1d
# 로 활성화(같은 지갑 공유, excl_group이 심볼 충돌 차단). name "live"만 state.db 사용.
_ALL_LIVE_ARMS: Tuple[Arm, ...] = (
    Arm("live",   "none", "실전 4h", allow_short=True, excl_group="acct"),
    Arm("live1d", "none", "실전 1D", allow_short=True, tf="D", cap=1, excl_group="acct"),
)
_live_only = [s.strip() for s in os.getenv("LIVE_ARMS_ONLY", "live").split(",") if s.strip()]
LIVE_ARMS: Tuple[Arm, ...] = tuple(a for a in _ALL_LIVE_ARMS if a.name in _live_only)


def arm_db_path(name: str) -> Path:
    return DATA_DIR / f"state_{name}.db"


STATE_DB_PATH: Path = DATA_DIR / "state.db"
MARKET_SNAPSHOT_PATH: Path = DATA_DIR / "market.json"  # engine writes, dashboard reads
DASHBOARD_PORT: int = int(os.getenv("DASHBOARD_PORT", "8501"))
