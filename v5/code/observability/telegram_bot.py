"""Telegram alert bot.

Lightweight async HTTP client that pushes the operator-visible event stream:
startup / shutdown, per-cycle decision (including NO_TRADE), entries, closes,
risk vetoes, kill-switch events, errors, daily reports and heartbeats.
Messages use HTML formatting and are throttled to stay under the bot API
rate limit.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import httpx

from config.settings import TG

logger = logging.getLogger("metis.telegram")


class TelegramBot:
    """Async Telegram sender with simple inter-message throttling."""

    def __init__(self):
        self.bot_token = TG.BOT_TOKEN
        self.chat_id = TG.CHAT_ID
        self.enabled = TG.ENABLED and bool(self.bot_token and self.chat_id)
        self._client: Optional[httpx.AsyncClient] = None
        self._lock = asyncio.Lock()
        self._last_send_ts: float = 0.0
        self._min_interval = 0.5  # 2 msg/sec ceiling — well under Telegram limits

    async def _ensure_client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))

    async def close(self):
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def send(self, text: str, parse_mode: str = "HTML"):
        if not self.enabled:
            logger.debug(f"[telegram disabled] {text[:80]}")
            return False
        await self._ensure_client()
        async with self._lock:
            elapsed = time.time() - self._last_send_ts
            if elapsed < self._min_interval:
                await asyncio.sleep(self._min_interval - elapsed)
            url = f"https://api.telegram.org/bot{self.bot_token}/sendMessage"
            payload = {
                "chat_id": self.chat_id,
                "text": text[:4096],
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                r = await self._client.post(url, json=payload)
                self._last_send_ts = time.time()
                if r.status_code != 200:
                    logger.warning(f"Telegram send failed: {r.status_code} {r.text[:200]}")
                    return False
                return True
            except Exception as e:
                logger.warning(f"Telegram exception: {e}")
                return False

    # ── canonical message templates ──
    async def notify_startup(self, mode: str, equity: float, prompt_hash: str):
        await self.send(
            f"<b>🟢 METIS 시작</b>\n"
            f"mode: <code>{mode}</code>  ·  equity: <b>${equity:.2f}</b>\n"
            f"prompt: <code>{prompt_hash}</code>"
        )

    async def notify_shutdown(self, reason: str):
        await self.send(f"<b>🔴 METIS 종료</b>\nreason: <code>{reason}</code>")

    async def notify_entry(self, symbol: str, side: str, qty: float, entry: float,
                           sl: float, tp: float, R: float, setup_id: str, confidence: float,
                           leverage: Optional[int] = None):
        emoji = "🟢" if side == "Buy" else "🔴"
        side_label = "LONG" if side == "Buy" else "SHORT"
        lev_str = f"  ·  lev <b>{leverage}x</b>" if leverage else ""
        await self.send(
            f"{emoji} <b>{symbol} {side_label}</b>  ·  {setup_id}  (실 주문 {side})\n"
            f"qty <b>{qty}</b>  ·  entry <b>{entry:.4f}</b>{lev_str}\n"
            f"SL <code>{sl:.4f}</code>  ·  TP <code>{tp:.4f}</code>  ·  R <b>{R:.2f}</b>\n"
            f"conf <b>{confidence:.2f}</b>"
        )

    async def notify_close(self, symbol: str, side: str, pnl: float, R: float,
                           reason: str, equity_after: float, won: bool,
                           leverage: Optional[int] = None):
        emoji = "✅" if won else "❌"
        side_label = "LONG" if side == "Buy" else "SHORT"
        lev_str = f"  ·  lev <b>{leverage}x</b>" if leverage else ""
        await self.send(
            f"{emoji} <b>{symbol} {side_label} CLOSE</b>  ·  {reason}{lev_str}\n"
            f"PnL <b>{pnl:+.2f}</b> USDT  ·  realized_R <b>{R:+.2f}</b>\n"
            f"equity → <b>${equity_after:.2f}</b>"
        )

    async def notify_risk_veto(self, symbol: str, reason: str, attempted: str):
        await self.send(
            f"⚠️ <b>RISK VETO {symbol}</b>\n"
            f"<code>{reason[:200]}</code>\n"
            f"attempted: {attempted}"
        )

    async def notify_kill(self, kind: str, reason: str, until_utc: Optional[str]):
        await self.send(
            f"🛑 <b>KILL SWITCH {kind}</b>\n"
            f"reason: <code>{reason}</code>\n"
            f"until: <code>{until_utc or 'manual'}</code>"
        )

    async def notify_error(self, component: str, error: str):
        await self.send(
            f"❗ <b>ERROR {component}</b>\n<code>{error[:600]}</code>"
        )

    async def notify_daily(self, report: dict):
        breakdown = ""
        sb = report.get("setup_breakdown") or {}
        for k, v in sb.items():
            breakdown += f"  · {k}: n={v.get('n', 0)} W={v.get('wins', 0)} ΣR={v.get('sum_R', 0):.2f}\n"
        await self.send(
            f"📊 <b>Daily {report.get('utc_date')}</b>\n"
            f"trades <b>{report.get('n_trades', 0)}</b>  ·  W/L <b>{report.get('wins', 0)}/{report.get('losses', 0)}</b>"
            f"  ·  WR <b>{(report.get('win_rate', 0) or 0)*100:.1f}%</b>\n"
            f"avg_R <b>{report.get('avg_R', 0):.2f}</b>  ·  PF <b>{report.get('profit_factor', 0):.2f}</b>\n"
            f"sum_PnL <b>{report.get('sum_pnl', 0):+.2f}</b> USDT\n"
            f"{breakdown}"
        )

    async def notify_heartbeat(self, equity: float, last_cycle: str, kill_active: bool):
        emoji = "💓" if not kill_active else "🛑"
        await self.send(
            f"{emoji} HB  equity <b>${equity:.2f}</b>  ·  last <code>{last_cycle}</code>"
        )

    async def notify_cycle(self, ev: dict):
        """Send a per-cycle decision summary (including NO_TRADE) for operator visibility."""
        symbol = ev.get("symbol", "?")
        decision = ev.get("decision", "?")
        setup = ev.get("setup_id") or "-"
        conf = ev.get("confidence")
        regime = ev.get("regime") or "-"
        vol = ev.get("volatility_regime") or "-"
        sup = ev.get("supervisor_action") or "-"
        rejects = ev.get("critical_rejects") or ""
        veto = ev.get("risk_veto_reason") or ""
        evidence = ev.get("technical_evidence") or ""
        supervisor_evidence = ev.get("supervisor_evidence") or ""
        reason = ev.get("reason_short") or ""
        latency = ev.get("llm_latency_ms")
        retry = ev.get("retry_count", 0)
        ai_lev = ev.get("ai_leverage")
        cached_tok = ev.get("cached_tokens", 0)

        # 헤더 결정
        if decision == "ENTER_LONG":
            head = f"🟢 <b>{symbol} LONG</b>"
        elif decision == "ENTER_SHORT":
            head = f"🔴 <b>{symbol} SHORT</b>"
        elif decision == "NO_TRADE":
            head = f"⚪️ <b>{symbol} NO_TRADE</b>"
        else:
            head = f"❔ <b>{symbol} {decision}</b>"

        lines = [head]
        # Setup + confidence + regime + leverage line.
        meta = []
        if setup != "-": meta.append(f"setup <code>{setup}</code>")
        if conf is not None: meta.append(f"conf <b>{conf:.2f}</b>")
        if ai_lev: meta.append(f"lev <b>{ai_lev}x</b>")
        meta.append(f"regime <code>{regime}</code>")
        meta.append(f"vol <code>{vol}</code>")
        lines.append("  ·  ".join(meta))
        # Supervisor verdict + rejects + risk veto.
        if sup != "-":
            lines.append(f"supervisor <code>{sup}</code>")
        if rejects:
            lines.append(f"rejects <code>{rejects}</code>")
        if veto:
            lines.append(f"risk_veto <code>{veto[:160]}</code>")
        # Evidence summaries from the technical proposal and the supervisor.
        if evidence:
            lines.append(f"📊 {evidence[:200]}")
        if supervisor_evidence and supervisor_evidence != evidence:
            lines.append(f"🛡 {supervisor_evidence[:200]}")
        if reason:
            lines.append(f"💬 {reason}")
        # Latency + retry + implicit cache footer.
        techline = []
        if latency is not None:
            techline.append(f"{latency/1000:.1f}s")
        if retry:
            techline.append(f"retry {retry}")
        if cached_tok:
            techline.append(f"cache {cached_tok}tok")
        if techline:
            lines.append(f"<i>{' · '.join(techline)}</i>")

        await self.send("\n".join(lines))


_singleton: Optional[TelegramBot] = None


def get_telegram() -> TelegramBot:
    global _singleton
    if _singleton is None:
        _singleton = TelegramBot()
    return _singleton
