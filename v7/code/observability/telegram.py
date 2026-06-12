"""Telegram notifier — one chat, immediate sends for trade/risk/system events.

Message format: one headline line + a detail line. Sends every cycle summary,
each ENTER/EXIT, risk vetoes that block a kill, errors, and a daily report.
Silently no-ops when no bot token is configured.
"""
from __future__ import annotations

import logging

import httpx

from config.settings import TG

logger = logging.getLogger("metis.telegram")


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
        await self.send(f"🟢 <b>METIS 가동</b> [{mode}]\n자산 ${equity:,.2f} · {n_symbols}코인 4h 돌파 포트폴리오")

    async def cycle(self, cid: str, entered: list[str], exited: list[str], open_n: int):
        head = f"🔄 <b>cycle</b> {cid[11:16]}"
        det = f"진입 {entered or '-'} · 청산 {exited or '-'} · 보유 {open_n}"
        await self.send(f"{head}\n{det}")

    async def enter(self, sym: str, side: str, qty: float, entry: float, stop: float, lev: int):
        kor = "롱" if side == "Buy" else "숏"
        await self.send(f"📈 <b>진입 {sym} {kor}</b> x{lev}\nqty {qty} · 진입 {entry} · SL {stop}")

    async def exit(self, sym: str, reason: str, realized: float, R: float):
        sign = "🟢" if realized >= 0 else "🔴"
        await self.send(f"{sign} <b>청산 {sym}</b> [{reason}]\n실현 ${realized:+,.2f} · {R:+.2f}R")

    async def error(self, where: str, msg: str):
        await self.send(f"⚠️ <b>오류 {where}</b>\n{msg[:300]}")

    async def daily(self, equity: float, day_realized: float, n_trades: int, open_n: int):
        await self.send(f"📊 <b>일일 리포트</b>\n자산 ${equity:,.2f} · 당일 ${day_realized:+,.2f} "
                        f"· 거래 {n_trades} · 보유 {open_n}")


_tg = None


def get_telegram() -> Telegram:
    global _tg
    if _tg is None:
        _tg = Telegram()
    return _tg
