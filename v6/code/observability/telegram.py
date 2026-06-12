"""Telegram notifier — one chat, immediate sends for trade/risk/system events.

Messages are written to be scanned at a glance on a phone: a bold headline with
an emoji that encodes the outcome (🟢 profit / 🔴 loss / 📈 enter / 🔄 cycle), then
short labelled lines. The strategy is long-only + BTC uptrend gate, so the cycle
message always states the gate (the reason there are or aren't new entries).
Sends every cycle, each ENTER/EXIT, the daily report, and errors. No-ops when no
bot token is configured.
"""
from __future__ import annotations

import logging

import httpx

from config.settings import TG

logger = logging.getLogger("metis.telegram")

REASON_KO = {"STOP_LOSS": "손절", "TREND_EXIT": "추세종료", "TAKE_PROFIT": "익절",
             "LIQUIDATION": "강제청산", "MANUAL": "수동청산"}


class Telegram:
    def __init__(self):
        self._http = httpx.AsyncClient(timeout=10.0)

    async def close(self):
        await self._http.aclose()

    async def send(self, text: str):
        if not TG.ENABLED:
            return
        try:
            await self._http.post(
                f"https://api.telegram.org/bot{TG.BOT_TOKEN}/sendMessage",
                json={"chat_id": TG.CHAT_ID, "text": text, "parse_mode": "HTML"})
        except Exception:
            logger.exception("telegram send failed")

    async def startup(self, mode: str, equity: float, n_symbols: int):
        await self.send(
            f"🟢 <b>METIS 가동</b> · {mode}\n"
            f"자산 <b>${equity:,.2f}</b>\n"
            f"{n_symbols}코인 4h 돌파 · 롱온리 + BTC 상승게이트")

    async def cycle(self, cid: str, entered: list[str], exited: list[str],
                    open_n: int, cap: int, btc_up: bool):
        t = cid[11:16]  # HH:MM of the bar close
        gate = "🟢 열림" if btc_up else "⏸ 닫힘 (BTC 하락추세)"
        lines = [f"🔄 <b>4h 점검</b> · {t}"]
        if entered:
            lines.append(f"📈 신규 진입: <b>{', '.join(entered)}</b>")
        if exited:
            lines.append(f"📉 청산: <b>{', '.join(exited)}</b>")
        if not entered and not exited:
            lines.append("· 변동 없음" + ("" if btc_up else " · 게이트 닫혀 대기"))
        lines.append(f"보유 {open_n}/{cap} · 게이트 {gate}")
        await self.send("\n".join(lines))

    async def enter(self, sym: str, side: str, qty: float, entry: float, stop: float,
                    lev: int, risk_usdt: float):
        d = "롱" if side == "Buy" else "숏"
        stop_pct = abs(entry - stop) / entry * 100 if entry else 0.0
        await self.send(
            f"📈 <b>진입 · {sym} {d}</b>\n"
            f"진입가 <b>${entry:,.4f}</b> · 수량 {qty}\n"
            f"손절 ${stop:,.4f} (−{stop_pct:.1f}%) · {lev}x\n"
            f"이번 거래 리스크 ${risk_usdt:,.2f}")

    async def exit(self, sym: str, side: str, reason: str, entry: float, exit_price: float,
                   realized: float, R: float, hold: str):
        d = "롱" if side == "Buy" else "숏"
        sign = "🟢" if realized >= 0 else "🔴"
        rk = REASON_KO.get(reason, reason)
        await self.send(
            f"{sign} <b>청산 · {sym} {d}</b> [{rk}]\n"
            f"진입 ${entry:,.4f} → 청산 ${exit_price:,.4f}\n"
            f"실현 <b>${realized:+,.2f}</b> ({R:+.2f}R) · 보유 {hold}")

    async def error(self, where: str, msg: str):
        await self.send(f"⚠️ <b>오류 · {where}</b>\n{msg[:300]}")

    async def daily(self, equity: float, seed: float, day_realized: float,
                    n_trades: int, open_n: int):
        tot = equity - seed
        totpct = (tot / seed * 100) if seed > 0 else 0.0
        sign = "🟢" if tot >= 0 else "🔴"
        await self.send(
            f"📊 <b>일일 리포트</b>\n"
            f"자산 <b>${equity:,.2f}</b>\n"
            f"{sign} 누적 ${tot:+,.2f} ({totpct:+.1f}%) · 시드 ${seed:,.0f}\n"
            f"당일 ${day_realized:+,.2f} · 거래 {n_trades}건 · 보유 {open_n}")


_tg = None


def get_telegram() -> Telegram:
    global _tg
    if _tg is None:
        _tg = Telegram()
    return _tg
