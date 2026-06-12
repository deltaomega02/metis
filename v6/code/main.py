"""METIS breakout portfolio engine — main orchestrator.

Single asyncio process. Each 4h cycle: fetch the closed klines for every symbol
ONCE, then run each arm independently — exit on stop-loss / trend-end, then open
the strongest new breakouts up to the concurrency cap, gated by that arm's BTC
regime gate.

PAPER mode runs the A/B/C arms (config PAPER_ARMS) as independent paper portfolios
(separate state DBs) so we can forward-test which gate performs — the breakout
signal and risk sizing are identical across arms; only the gate differs. LIVE mode
runs a single arm (the deployed gate) against the real exchange with reconcile.
"""
from __future__ import annotations

import asyncio
import gc
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.settings import (CYCLE, LIVE_ARMS, LOGS_DIR, MARKET_SNAPSHOT_PATH, PAPER_ARMS,
                             PAPER_INITIAL_BALANCE_USDT, PAPER_MODE, RISK, STATE_DB_PATH,
                             STRATEGY, TRADING, Arm, arm_db_path, tf_minutes)
from core import risk_engine
from core.breakout_signal import evaluate, gate_open, should_exit
from core.market_snapshot import compute_coin, compute_gates, write_snapshot
from core.scheduler import CycleScheduler, last_bar_close
from core.state_store import StateStore, get_state_store
from exchange.bybit_client import close_bybit_client, get_bybit_client
from exchange.executor import LiveExecutor
from exchange.paper_executor import PaperExecutor
from observability.telegram import get_telegram

LOGS_DIR.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level="INFO",
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout), logging.FileHandler(LOGS_DIR / "metis.log")],
)
logger = logging.getLogger("metis.main")


class ArmRT:
    """Runtime state for one arm: its config, its own store, its own executor."""
    def __init__(self, cfg: Arm, store: StateStore, executor):
        self.cfg = cfg
        self.store = store
        self.executor = executor


class Engine:
    def __init__(self):
        self.bybit = get_bybit_client()
        self.telegram = get_telegram()
        self.specs: dict = {}
        self.arms: list[ArmRT] = []
        self.live = not PAPER_MODE
        self.scheduler = CycleScheduler(on_cycle=self._on_cycle)
        self._first_cycle = True

    async def start(self):
        logger.info("=" * 56)
        logger.info(f"METIS breakout start  mode={'PAPER (A/B/C arms)' if PAPER_MODE else 'LIVE'}")
        logger.info(f"symbols={TRADING.SYMBOLS}  tf={STRATEGY.INTERVAL}m  "
                    f"risk={RISK.RISK_PER_TRADE*100:.2f}%/trade cap={RISK.MAX_CONCURRENT}")
        self.specs = await self.bybit.refresh_instrument_specs()

        if PAPER_MODE:
            for cfg in PAPER_ARMS:
                store = StateStore(arm_db_path(cfg.name))
                store.equity_init(PAPER_INITIAL_BALANCE_USDT)
                self.arms.append(ArmRT(cfg, store, PaperExecutor(store)))
                logger.info(f"arm [{cfg.name}] gate={cfg.gate} db={arm_db_path(cfg.name).name}")
        else:
            # LIVE arms (settings.LIVE_ARMS; 기본 = 4h 단일암 — 현행과 동일).
            # 전 라이브 암이 같은 지갑을 공유: 사이징은 총자산 기준(백테스트 가정과 동일),
            # 심볼 충돌은 excl_group이 차단. name "live"만 레거시 state.db를 쓴다.
            eq = await self.bybit.get_wallet_equity() or PAPER_INITIAL_BALANCE_USDT
            for cfg in LIVE_ARMS:
                store = get_state_store() if cfg.name == "live" else StateStore(arm_db_path(cfg.name))
                store.equity_init(eq); store.equity_set(eq)
                ex = LiveExecutor(store, self.bybit, self.specs)
                self.arms.append(ArmRT(cfg, store, ex))
                logger.info(f"arm [{cfg.name}] tf={cfg.tf} cap={cfg.cap or RISK.MAX_CONCURRENT} "
                            f"db={'state.db' if cfg.name == 'live' else arm_db_path(cfg.name).name}")
            await self._reconcile_boot()

        logger.info("=" * 56)
        await self.scheduler.start()
        try:
            seed = self.arms[0].store.equity_get()[0]
            await self.telegram.startup("PAPER A/B/C" if PAPER_MODE else "LIVE", seed, len(TRADING.SYMBOLS))
        except Exception:
            pass

    async def shutdown(self, reason: str):
        logger.info(f"shutdown: {reason}")
        await self.scheduler.stop()
        try:
            await self.telegram.close()
        except Exception:
            pass
        await close_bybit_client()
        for arm in self.arms:
            try:
                arm.store.checkpoint()
            except Exception:
                pass

    async def _reconcile_boot(self):
        """LIVE only: sync stores with the exchange on startup. 멀티암: 어느 암도
        추적 안 하는 거래소 포지션만 입양(기본 4h 암으로) — 다른 암 소유분을 뺏지 않게
        전 암 union으로 판단. 사라진 포지션 정리는 암별."""
        try:
            exch = await self.bybit.get_positions_map()
        except Exception:
            logger.exception("boot reconcile: position fetch failed")
            return
        tracked = {p["symbol"] for arm in self.arms for p in arm.store.positions_open()}
        adopt_arm = self.arms[0]  # 기본(4h) 암이 미추적 포지션의 입양처
        for sym, p in exch.items():
            if sym in TRADING.SYMBOLS and sym not in tracked:
                stop = float(p.get("stopLoss") or 0) or 0.0
                adopt_arm.store.position_open(
                    symbol=sym, side=p.get("side"), qty=float(p.get("size") or 0),
                    entry_price=float(p.get("avgPrice") or 0), stop_price=stop,
                    leverage=int(float(p.get("leverage") or RISK.LEVERAGE)),
                    opened_utc=datetime.now(timezone.utc).isoformat(),
                    entry_order_id="adopted", sl_order_id="")
                logger.info(f"adopted exchange position {sym} size={p.get('size')} stop={stop}")
        for arm in self.arms:
            for pos in arm.store.positions_open():
                if pos["symbol"] not in exch:
                    try:
                        await arm.executor.close(pos, pos["stop_price"], "STOP_LOSS")
                        logger.info(f"[{arm.cfg.name}] reconciled vanished position {pos['symbol']}")
                    except Exception:
                        logger.exception(f"[{arm.cfg.name}] boot reconcile close {pos['symbol']}")

    async def _fetch_closed(self, symbol: str, interval: str = "") -> list[list]:
        k = await self.bybit.get_kline(symbol, interval or STRATEGY.INTERVAL, STRATEGY.KLINE_LOOKBACK)
        k = sorted(k, key=lambda r: int(r[0]))
        return k[:-1] if len(k) > 1 else k

    def _held_elsewhere(self, sym: str, arm: ArmRT) -> bool:
        """심볼 배타: 같은 excl_group의 다른 암이 sym을 보유 중인가. 한 계좌(원웨이)를
        공유하는 암들이 같은 심볼을 동시에 잡으면 넷 포지션 합산 + Full모드 TP/SL
        덮어쓰기가 일어나므로 진입 단계에서 차단한다."""
        g = arm.cfg.excl_group
        if not g:
            return False
        return any(o is not arm and o.cfg.excl_group == g and o.store.has_position(sym)
                   for o in self.arms)

    async def _run_arm(self, arm: ArmRT, klines: dict, btc_klines: list, cid: str) -> dict:
        """Run one arm's exits + entries for this cycle. Returns a summary dict."""
        store = arm.store; ex = arm.executor
        entered: list[str] = []; exited: list[str] = []
        exch = await self.bybit.get_positions_map() if self.live else None

        # exits
        for pos in store.positions_open():
            sym = pos["symbol"]; side = pos["side"]; stop = pos["stop_price"]
            try:
                hold = _hold(pos["opened_utc"])
                if self.live and sym not in exch:
                    # exchange closed it (server SL / short take-profit / liquidation);
                    # close() labels the reason by realized P&L sign.
                    r = await ex.close(pos, stop, "AUTO"); exited.append(sym)
                    rsn = "TAKE_PROFIT" if r > 0 else "STOP_LOSS"
                    risk = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
                    await self.telegram.exit(sym, side, rsn, pos["entry_price"], stop, r,
                                             r / risk if risk > 0 else 0.0, hold)
                    continue
                k = klines.get(sym)
                if not k:
                    continue
                last = k[-1]; hi = float(last[2]); lo = float(last[3]); cl = float(last[4])
                hit_sl = (side == "Buy" and lo <= stop) or (side == "Sell" and hi >= stop)
                tp = pos.get("take_profit") or 0.0
                hit_tp = side == "Sell" and tp > 0 and lo <= tp  # short fixed R-target
                if (not self.live) and hit_sl:
                    r = await ex.close(pos, stop, "STOP_LOSS"); exited.append(sym)
                elif (not self.live) and hit_tp:
                    r = await ex.close(pos, tp, "TAKE_PROFIT"); exited.append(sym)
                elif should_exit(side, k):
                    px = cl if not self.live else (await self.bybit.get_last_price(sym) or cl)
                    r = await ex.close(pos, px, "TREND_EXIT"); exited.append(sym)
                    if self.live:
                        await self.telegram.exit(sym, side, "TREND_EXIT", pos["entry_price"], px, r, _R(pos, px), hold)
            except Exception:
                logger.exception(f"[{arm.cfg.name}] exit error {sym}")

        # equity (live reads wallet; paper self-booked)
        if self.live:
            eq = await self.bybit.get_wallet_equity()
            if eq:
                store.equity_set(eq)
        equity, hw = store.equity_get()

        # entries
        rf = store.risk_get(); mk = bool(rf.get("manual_kill"))
        day = store.daily_pnl_today()["realized_usdt"]; open_n = store.open_count()
        cap = arm.cfg.cap or RISK.MAX_CONCURRENT
        slots = cap - open_n
        gate = gate_open(btc_klines, arm.cfg.gate)
        if slots > 0 and not mk and gate:
            cands = []
            for sym in TRADING.SYMBOLS:
                if store.has_position(sym) or self._held_elsewhere(sym, arm):
                    continue
                intent = evaluate(sym, klines.get(sym, []), allow_short=arm.cfg.allow_short)
                if not intent:
                    continue
                dec = risk_engine.evaluate(intent, equity=equity, high_water=hw, open_count=open_n,
                                           daily_realized=day, manual_kill=mk, specs=self.specs,
                                           max_concurrent=cap)
                if dec.ok:
                    cands.append((intent.adx, dec.order))
            cands.sort(key=lambda x: x[0], reverse=True)
            for _, order in cands[:slots]:
                try:
                    res = await ex.open(order)
                    if res.get("ok", True):
                        entered.append(order.symbol)
                        # signal snapshot — the indicators at entry, for post-hoc analysis
                        # of which conditions trade best (slippage/edge by regime).
                        try:
                            snap = compute_coin(order.symbol, klines.get(order.symbol, []))
                            store.journal("SIGNAL", order.symbol, {
                                "side": order.side, "ref_entry": order.reference_price,
                                "stop": order.stop_price, "tp": order.take_profit,
                                "gate": gate, "ind": snap})
                        except Exception:
                            logger.exception(f"[{arm.cfg.name}] signal snapshot {order.symbol}")
                        if self.live:
                            await self.telegram.enter(order.symbol, order.side, res.get("qty", order.qty),
                                                      res.get("entry_price", order.reference_price),
                                                      order.stop_price, order.leverage, order.risk_usdt)
                except Exception:
                    logger.exception(f"[{arm.cfg.name}] entry error {order.symbol}")

        # mark-to-market sample: unrealized from the last closed bar (paper books only
        # realized into equity; live wallet equity already includes unrealized).
        upnl = 0.0
        if not self.live:
            for pos in store.positions_open():
                k = klines.get(pos["symbol"])
                if not k:
                    continue
                mark = float(k[-1][4])
                d = 1 if pos["side"] == "Buy" else -1
                upnl += d * (mark - pos["entry_price"]) * pos["qty"]
        store.equity_curve_point(cid, equity, upnl, store.open_count())

        logger.info(f"[{arm.cfg.name}] gate={gate} slots={slots} 진입={entered or '-'} 청산={exited or '-'} "
                    f"보유={store.open_count()} eq={equity:,.2f} mark={equity + upnl:,.2f}")
        return {"arm": arm, "entered": entered, "exited": exited, "gate": gate,
                "open": store.open_count(), "equity": equity,
                "unrealized": upnl, "mark_equity": equity + upnl}

    async def _on_cycle(self, cid: str, close_dt: datetime):
        logger.info(f"=== cycle {cid} ===")
        # 어느 암이 이번 사이클에 도는가: 기본 TF(4h) 암은 매 사이클. 더 긴 TF 암은
        # "그 TF의 최신 닫힌 봉"이 아직 미처리일 때만(봉 마감 직후 사이클 + 다운타임
        # 캐치업 — 재기동이 봉 마감을 건너뛰었어도 다음 4h 사이클에 따라잡는다).
        due: list[tuple] = []  # (arm, slow_cid|None)
        for arm in self.arms:
            tfm = tf_minutes(arm.cfg.tf)
            if tfm <= CYCLE.PRIMARY_TF_MIN:
                due.append((arm, None))
                continue
            slow_close = last_bar_close(close_dt, tfm)
            slow_cid = slow_close.strftime("%Y-%m-%dT%H:%M:%SZ")
            if arm.store.last_cycle_done() != slow_cid:
                due.append((arm, slow_cid))
            else:
                logger.info(f"[{arm.cfg.name}] tf={arm.cfg.tf} 봉 미마감 — 대기 (last={slow_cid})")

        # space the per-coin kline calls apart so the requests don't burst at cycle
        # start and trip the per-IP rate limit (retCode 10006). TF별로 한 번씩만 페치.
        klines_by_tf: dict[str, dict] = {}
        need_tfs = sorted({arm.cfg.tf for arm, _ in due} | {STRATEGY.INTERVAL})
        first = True
        for tf in need_tfs:
            klines_by_tf[tf] = {}
            for s in TRADING.SYMBOLS:
                if not first:
                    await asyncio.sleep(CYCLE.KLINE_FETCH_SPACING_SEC)
                first = False
                klines_by_tf[tf][s] = await self._fetch_closed(s, tf)
        btc_klines = klines_by_tf[STRATEGY.INTERVAL].get("BTCUSDT", [])

        sums = []
        for arm, slow_cid in due:
            try:
                sums.append(await self._run_arm(arm, klines_by_tf[arm.cfg.tf], btc_klines, cid))
                if slow_cid:
                    arm.store.mark_cycle_done(slow_cid)
            except Exception:
                logger.exception(f"arm error {arm.cfg.name}")
        klines = klines_by_tf[STRATEGY.INTERVAL]  # snapshot/이하 로직은 기본 TF 기준

        # publish the market snapshot so the dashboard renders from this file and
        # makes zero Bybit calls (no per-IP rate-limit contention).
        try:
            coins = {}
            for s in TRADING.SYMBOLS:
                cm = compute_coin(s, klines.get(s, []))
                if cm:
                    coins[s] = cm
            write_snapshot(MARKET_SNAPSHOT_PATH, cid, compute_gates(btc_klines), coins)
        except Exception:
            logger.exception("market snapshot write failed")

        try:
            await self._notify(cid, close_dt, sums)
        except Exception:
            logger.exception("notify error")

        try:
            await self._check_validation(sums, btc_klines)
        except Exception:
            logger.exception("validation check error")

        klines_by_tf.clear()
        gc.collect()

    async def _check_validation(self, sums: list, btc_klines: list):
        """ls real-money readiness gates — fire a one-shot Telegram alert at each
        milestone so 운영자 is told the moment validation advances (no need to ask)."""
        ls = next((s for s in sums if s["arm"].cfg.name == "ls"), None)
        if not ls:
            return
        store = ls["arm"].store
        # M1: BTC uptrend gate opens → out of the downtrend, the key test regime begins
        if gate_open(btc_klines, "ema20_50") and not store.milestone_fired("uptrend"):
            store.milestone_mark("uptrend")
            await self.telegram.send(
                "🎯 <b>검증 마일스톤 · BTC 상승 전환</b>\n"
                "게이트 열림 = 하락장 탈출. 반등서 ls 숏 거동 + 롱 진입이 시작되는 검증 핵심 구간 진입.")
        # M2: first long ever taken by ls → the long half gets live validation
        has_long = (any(p["side"] == "Buy" for p in store.positions_open())
                    or any(t["side"] == "Buy" for t in store.recent_trades(100)))
        if has_long and not store.milestone_fired("first_long"):
            store.milestone_mark("first_long")
            await self.telegram.send(
                "🎯 <b>검증 마일스톤 · ls 롱 첫 진입</b>\nls 롱 사이클 라이브 검증 시작.")
        # M3: all gates passed → out of the single-regime bias, ready to consider real money
        if not store.milestone_fired("validated"):
            trades = store.recent_trades(300)
            n = len(trades); net = sum(t["realized_usdt"] for t in trades)
            if (store.milestone_fired("uptrend") and store.milestone_fired("first_long")
                    and n >= 10 and net > 0):
                store.milestone_mark("validated")
                await self.telegram.send(
                    f"✅ <b>ls 검증 게이트 통과 — 실전 투입 검토 가능</b>\n"
                    f"확정 {n}건 · net <b>${net:+.2f}</b> · 상승전환·롱진입 경험 완료.\n"
                    f"하락 한 국면 편향 벗음. 실전 전 숏TP 거래소위탁 구현 필요.")

    async def _notify(self, cid: str, close_dt: datetime, sums: list):
        t = cid[11:16]
        if self.live:
            if len(sums) == 1:
                s = sums[0]
                cap = s["arm"].cfg.cap or RISK.MAX_CONCURRENT
                gate = "🟢 열림" if s["gate"] else "⏸ 닫힘 (BTC 하락추세)"
                lines = [f"🔄 <b>4h 점검</b> · {t}"]
                if s["entered"]: lines.append(f"📈 진입: <b>{', '.join(s['entered'])}</b>")
                if s["exited"]: lines.append(f"📉 청산: <b>{', '.join(s['exited'])}</b>")
                if not s["entered"] and not s["exited"]:
                    lines.append("· 변동 없음" + ("" if s["gate"] else " · 게이트 닫혀 대기"))
                lines.append(f"보유 {s['open']}/{cap} · 게이트 {gate}")
            else:  # 멀티 라이브 암(4h+1D): 암별 한 줄
                lines = [f"🔄 <b>점검</b> · {t}  <i>(실전 멀티암)</i>"]
                for s in sums:
                    cap = s["arm"].cfg.cap or RISK.MAX_CONCURRENT
                    act = ""
                    if s["entered"]: act += f" 진입 {','.join(s['entered'])}"
                    if s["exited"]: act += f" 청산 {','.join(s['exited'])}"
                    lines.append(f"· <b>{s['arm'].cfg.label}</b> 보유{s['open']}/{cap}{act or ' 변동없음'}")
            await self.telegram.send("\n".join(lines))
        else:
            head = "🔍 <b>시작 점검</b>" if self._first_cycle else f"🔄 <b>4h 점검</b> · {t}"
            self._first_cycle = False
            rows = [head + "  <i>(페이퍼 A/B/C)</i>"]
            for s in sums:
                g = "🟢" if s["gate"] else "⏸"
                act = ""
                if s["entered"]: act += f" 진입 {','.join(s['entered'])}"
                if s["exited"]: act += f" 청산 {','.join(s['exited'])}"
                mk = s["mark_equity"]; pnl = (mk / PAPER_INITIAL_BALANCE_USDT - 1) * 100
                u = s["unrealized"]; utxt = f" <i>(미실현 {u:+.0f})</i>" if abs(u) >= 0.5 else ""
                rows.append(f"{g} <b>{s['arm'].cfg.label}</b> · 보유{s['open']} · "
                            f"${mk:,.0f}({pnl:+.1f}%){utxt}{act}")
            await self.telegram.send("\n".join(rows))

        if close_dt.hour == CYCLE.DAILY_REPORT_UTC_HOUR:
            if self.live:
                s = sums[0]; seed = s["arm"].store.equity_initial() or s["equity"]
                d = s["arm"].store.daily_pnl_today()
                await self.telegram.daily(s["equity"], seed, d["realized_usdt"], d["n_trades"], s["open"])
            else:
                rows = ["📊 <b>일일 리포트</b>  <i>(페이퍼 A/B/C 비교)</i>"]
                for s in sums:
                    d = s["arm"].store.daily_pnl_today()
                    mk = s["mark_equity"]; pnl = (mk / PAPER_INITIAL_BALANCE_USDT - 1) * 100
                    rows.append(f"· <b>{s['arm'].cfg.label}</b>: ${mk:,.0f} ({pnl:+.1f}%) "
                                f"· 당일실현 ${d['realized_usdt']:+.1f} · 미실현 ${s['unrealized']:+.1f} · 보유{s['open']}")
                await self.telegram.send("\n".join(rows))


def _R(pos: dict, exit_price: float) -> float:
    d = 1 if pos["side"] == "Buy" else -1
    risk = abs(pos["entry_price"] - pos["stop_price"]) * pos["qty"]
    return (d * (exit_price - pos["entry_price"]) * pos["qty"]) / risk if risk > 0 else 0.0


def _hold(opened_utc: str) -> str:
    try:
        dt = datetime.fromisoformat(opened_utc)
        sec = (datetime.now(timezone.utc) - dt).total_seconds()
        h, m = int(sec // 3600), int((sec % 3600) // 60)
        return f"{h}시간 {m}분" if h else f"{m}분"
    except Exception:
        return "-"


async def _run():
    eng = Engine()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(s, stop.set)
        except NotImplementedError:
            pass
    await eng.start()
    await stop.wait()
    await eng.shutdown("signal")


def main():
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
