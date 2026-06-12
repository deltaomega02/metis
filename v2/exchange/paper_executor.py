"""Paper Trading Executor.

mainnet 가격 데이터 위에서 가상 거래 시뮬레이션.
- 가상 잔고 ($500 시드 + 누적 PnL)
- 가상 포지션 (단일, BTCUSDT)
- Taker fee 0.055% 양방향
- Slippage 0 (mainnet ticker 가격으로 즉시 체결)
- Funding fee 0 (단순화)

영속: database/paper_state.db (별도 DB).
실제 거래소 호출 0건 — bybit_client의 PAPER_MODE 분기가 모든 signed call을 이 클래스로 위임.
"""

import sqlite3
import uuid
from pathlib import Path
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

from config import TRADING, get_logger

logger = get_logger("paper_executor")

PAPER_DB = Path(__file__).parent.parent / "database" / "paper_state.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_balance (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wallet_balance REAL NOT NULL,
    updated_at TEXT NOT NULL,
    note TEXT
);

CREATE TABLE IF NOT EXISTS paper_positions (
    position_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    entry_price REAL NOT NULL,
    leverage INTEGER NOT NULL,
    liquidation_price REAL NOT NULL,
    stop_loss_price REAL,
    take_profit_price REAL,
    margin_used REAL NOT NULL,
    entry_fee REAL NOT NULL,
    opened_at TEXT NOT NULL,
    closed_at TEXT,
    exit_price REAL,
    exit_fee REAL,
    realized_pnl REAL,
    is_active INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS paper_orders (
    order_id TEXT PRIMARY KEY,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    qty REAL NOT NULL,
    fill_price REAL NOT NULL,
    fee REAL NOT NULL,
    reduce_only INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_paper_positions_active ON paper_positions(symbol, is_active);
CREATE INDEX IF NOT EXISTS idx_paper_orders_symbol_time ON paper_orders(symbol, created_at);
"""


class PaperExecutor:
    """Paper trading 실행기. mainnet 가격으로 가상 체결."""

    TAKER_FEE = 0.00055  # 0.055%
    MAINTENANCE_MARGIN = 0.004  # 0.4% (Bybit BTC 기준)

    def __init__(self, initial_balance: float = 500.0):
        self.initial_balance = initial_balance
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(str(PAPER_DB))
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        PAPER_DB.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            row = c.execute(
                "SELECT wallet_balance FROM paper_balance ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                c.execute(
                    "INSERT INTO paper_balance (wallet_balance, updated_at, note) VALUES (?, ?, ?)",
                    (self.initial_balance, datetime.now(timezone.utc).isoformat(), "INITIAL")
                )
                logger.info(f"Paper balance initialized: ${self.initial_balance:.2f}")
            else:
                logger.info(f"Paper balance loaded: ${row['wallet_balance']:.4f}")

    # ==================== READ ====================

    def get_wallet_balance(self, coin: str = "USDT") -> Dict[str, Any]:
        with self._conn() as c:
            row = c.execute(
                "SELECT wallet_balance FROM paper_balance ORDER BY id DESC LIMIT 1"
            ).fetchone()
        wallet = float(row["wallet_balance"]) if row else self.initial_balance

        # 어떤 symbol이든 active position 1건 (단일 포지션 정책)
        pos = self.get_any_active_position()
        margin_used = float(pos["margin_used"]) if pos else 0.0

        available = max(wallet - margin_used, 0.0)

        return {
            "coin": coin,
            "wallet_balance": wallet,
            "available_balance": available,
            "total_equity": wallet,
            "unrealized_pnl": 0.0  # caller가 mark_price로 계산
        }

    def _get_active_position_row(self, symbol: str) -> Optional[sqlite3.Row]:
        """특정 symbol의 active position."""
        with self._conn() as c:
            return c.execute(
                "SELECT * FROM paper_positions WHERE symbol=? AND is_active=1 LIMIT 1",
                (symbol,)
            ).fetchone()

    def get_any_active_position(self) -> Optional[Dict[str, Any]]:
        """어떤 symbol이든 active position 1건 반환. 단일 포지션 정책 검증용."""
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM paper_positions WHERE is_active=1 LIMIT 1"
            ).fetchone()
        return dict(row) if row else None

    def get_position(
        self,
        symbol: str = TRADING.SYMBOL,
        mark_price: Optional[float] = None
    ) -> Optional[Dict[str, Any]]:
        row = self._get_active_position_row(symbol)
        if not row:
            return None

        entry = float(row["entry_price"])
        size = float(row["size"])
        side = row["side"]
        leverage = int(row["leverage"])
        margin = float(row["margin_used"])

        # 미실현 PnL 계산 (mark_price 있으면)
        if mark_price and mark_price > 0:
            if side == "Buy":
                unrealized = (mark_price - entry) * size
            else:
                unrealized = (entry - mark_price) * size
        else:
            mark_price = entry
            unrealized = 0.0

        return {
            "symbol": row["symbol"],
            "side": side,
            "size": size,
            "entry_price": entry,
            "mark_price": mark_price,
            "liquidation_price": float(row["liquidation_price"]),
            "leverage": leverage,
            "unrealized_pnl": unrealized,
            "position_value": size * mark_price,
            "position_margin": margin,
            "stop_loss_price": float(row["stop_loss_price"]) if row["stop_loss_price"] else None,
            "take_profit_price": float(row["take_profit_price"]) if row["take_profit_price"] else None,
        }

    # ==================== WRITE ====================

    def set_leverage(self, leverage: int) -> bool:
        # paper에선 place_market_order 시 leverage 인자로 받음
        logger.debug(f"[PAPER] set_leverage({leverage}) — noop")
        return True

    def place_market_order(
        self,
        side: str,
        qty: float,
        current_price: float,
        leverage: int = 1,
        reduce_only: bool = False,
        symbol: str = TRADING.SYMBOL
    ) -> Dict[str, Any]:
        """가상 시장가 체결.

        - reduce_only=False: 신규 포지션. 단일 포지션 정책: 어떤 symbol이든 active 있으면 거부.
        - reduce_only=True: 해당 symbol 활성 포지션 청산
        """
        order_id = f"paper-{uuid.uuid4().hex[:20]}"
        fill_price = float(current_price)
        notional = qty * fill_price
        fee = notional * self.TAKER_FEE

        with self._conn() as c:
            c.execute(
                """INSERT INTO paper_orders
                   (order_id, symbol, side, qty, fill_price, fee, reduce_only, created_at)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (order_id, symbol, side, qty, fill_price, fee,
                 1 if reduce_only else 0, datetime.now(timezone.utc).isoformat())
            )

            if reduce_only:
                self._close_active_position(c, symbol, fill_price, fee, order_id)
            else:
                self._open_new_position(c, symbol, side, qty, fill_price, leverage, fee)

        return {
            "order_id": order_id,
            "order_link_id": "",
            "symbol": symbol,
            "side": side,
            "qty": qty
        }

    def _open_new_position(self, c, symbol: str, side: str, qty: float, fill_price: float, leverage: int, fee: float):
        # 단일 포지션 정책: 어떤 symbol이든 active position 있으면 신규 진입 거부
        existing = c.execute(
            "SELECT symbol FROM paper_positions WHERE is_active=1 LIMIT 1"
        ).fetchone()
        if existing:
            logger.warning(f"[PAPER] 단일 포지션 정책: 활성 포지션({existing['symbol']}) 존재 — 신규 진입({symbol}) 거부")
            return

        # 청산가 계산 (Bybit 공식, Cross margin)
        if side == "Buy":
            liq = fill_price * (1 - 1.0/leverage + self.MAINTENANCE_MARGIN)
        else:
            liq = fill_price * (1 + 1.0/leverage - self.MAINTENANCE_MARGIN)

        margin = (qty * fill_price) / leverage

        # 잔고 체크 (실거래 시뮬) — margin + fee가 가용잔고 초과 시 진입 거부
        bal_row = c.execute(
            "SELECT wallet_balance FROM paper_balance ORDER BY id DESC LIMIT 1"
        ).fetchone()
        wallet = float(bal_row["wallet_balance"]) if bal_row else 0.0
        required = margin + fee
        if required > wallet:
            logger.warning(
                f"[PAPER] 잔고 부족: 필요 ${required:.2f} > 가용 ${wallet:.2f} — 진입 거부"
            )
            from exchange.bybit_client import InsufficientBalanceError
            raise InsufficientBalanceError(
                f"PAPER: 잔고 ${wallet:.2f}로 margin ${margin:.2f}+fee ${fee:.2f} 진입 불가"
            )
        position_id = f"pos-{uuid.uuid4().hex[:20]}"

        c.execute(
            """INSERT INTO paper_positions
               (position_id, symbol, side, size, entry_price, leverage, liquidation_price,
                margin_used, entry_fee, opened_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (position_id, symbol, side, qty, fill_price, leverage, liq, margin, fee,
             datetime.now(timezone.utc).isoformat())
        )
        logger.info(
            f"[PAPER] Open {symbol} {side} qty={qty} @ {fill_price:.2f} lev={leverage}x "
            f"liq={liq:.2f} fee={fee:.4f} margin={margin:.2f}"
        )

    def _close_active_position(self, c, symbol: str, fill_price: float, exit_fee: float, _order_id: str):
        row = c.execute(
            "SELECT * FROM paper_positions WHERE symbol=? AND is_active=1 LIMIT 1",
            (symbol,)
        ).fetchone()
        if not row:
            logger.warning(f"[PAPER] 청산할 활성 포지션 없음 ({symbol})")
            return

        entry = float(row["entry_price"])
        size = float(row["size"])
        side = row["side"]
        entry_fee = float(row["entry_fee"])

        # PnL: 가격 차이 × 수량 (레버리지는 포지션 size로 이미 반영됨)
        if side == "Buy":
            gross_pnl = (fill_price - entry) * size
        else:
            gross_pnl = (entry - fill_price) * size

        net_pnl = gross_pnl - entry_fee - exit_fee

        bal_row = c.execute(
            "SELECT wallet_balance FROM paper_balance ORDER BY id DESC LIMIT 1"
        ).fetchone()
        new_balance = float(bal_row["wallet_balance"]) + net_pnl

        c.execute(
            "INSERT INTO paper_balance (wallet_balance, updated_at, note) VALUES (?, ?, ?)",
            (new_balance, datetime.now(timezone.utc).isoformat(),
             f"close pos={row['position_id'][:8]} pnl={net_pnl:+.4f}")
        )
        c.execute(
            """UPDATE paper_positions SET
               is_active=0, closed_at=?, exit_price=?, exit_fee=?, realized_pnl=?
               WHERE position_id=?""",
            (datetime.now(timezone.utc).isoformat(), fill_price, exit_fee, net_pnl, row["position_id"])
        )
        logger.info(
            f"[PAPER] Close {side} qty={size} @ {fill_price:.2f} "
            f"gross={gross_pnl:+.4f} fees={(entry_fee+exit_fee):.4f} "
            f"net={net_pnl:+.4f} → balance=${new_balance:.4f}"
        )

    def update_trading_stop(
        self,
        stop_loss: Optional[float],
        take_profit: Optional[float],
        symbol: str = TRADING.SYMBOL
    ) -> bool:
        with self._conn() as c:
            c.execute(
                """UPDATE paper_positions SET
                   stop_loss_price=COALESCE(?, stop_loss_price),
                   take_profit_price=COALESCE(?, take_profit_price)
                   WHERE symbol=? AND is_active=1""",
                (stop_loss, take_profit, symbol)
            )
        logger.info(f"[PAPER] update_trading_stop {symbol} SL={stop_loss} TP={take_profit}")
        return True

    # ==================== EXECUTION QUERY ====================

    def get_execution_detail(self, order_id: str) -> Optional[Dict[str, Any]]:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM paper_orders WHERE order_id=?", (order_id,)
            ).fetchone()
        if not row:
            return None
        return {
            "avg_price": float(row["fill_price"]),
            "exec_fee": float(row["fee"])
        }

    def get_execution_list(self, symbol: str = TRADING.SYMBOL, limit: int = 50) -> List[Dict[str, Any]]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM paper_orders WHERE symbol=? ORDER BY created_at DESC LIMIT ?",
                (symbol, limit)
            ).fetchall()
        return [
            {
                "exec_time": int(datetime.fromisoformat(r["created_at"]).timestamp() * 1000),
                "side": r["side"],
                "exec_qty": float(r["qty"]),
                "exec_price": float(r["fill_price"]),
                "exec_fee": float(r["fee"]),
                "exec_type": "Trade",
            }
            for r in rows
        ]

    def get_total_funding_fee_for_position(self, _symbol: str, _entry_time_ms: int) -> float:
        return 0.0

    # ==================== HELPER ====================

    def get_status_summary(self) -> Dict[str, Any]:
        """단일 포지션 정책: any active 1건 반환 (멀티심볼 호환)."""
        bal = self.get_wallet_balance()
        pos = self.get_any_active_position()
        return {
            "balance": bal["wallet_balance"],
            "available": bal["available_balance"],
            "active_position": pos
        }


# 싱글톤은 bybit_client에서 PAPER_MODE 시점에 생성
