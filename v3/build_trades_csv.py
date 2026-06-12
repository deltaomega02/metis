#!/usr/bin/env python3
"""METIS-F2 거래별 종합 데이터 CSV 빌더.

각 거래(청산 완료)당 1 row. DB + cycle log + raw market data 통합.
운영자 제공 Bybit 데이터는 별도 컬럼으로 merge 가능.

실행: ./venv/bin/python build_trades_csv.py
출력: trades_v2.csv (현재 디렉토리)
"""
import csv
import json
import sqlite3
from datetime import datetime, timezone, timedelta
from pathlib import Path

KST = timezone(timedelta(hours=9))

DB_PATH = Path.home() / "metis-f2" / "database" / "metis_f2.db"
CYCLE_DIR = Path.home() / "metis-f2" / "logs" / "analysis"
OUT_CSV = Path.home() / "metis-f2" / "trades_v2.csv"


# 운영자 제공 Bybit 실측 데이터 (2026-05-12 09:00 KST)
# Trade Time prefix로 DB record와 매칭
BYBIT_ACTUAL = {
    "2026-05-08T17:44": {  # #1 (DB id=3) BTC SHORT WIN
        "exit_price": 79859.90, "qty": 0.028,
        "closed_pnl": 12.4055,
        "opening_fee": 1.23787356, "closing_fee": 1.22984246, "funding_fee": -0.2714,
        "open_volume": 2250.67, "closed_volume": 2236.07,
        "trade_type": "Close Short", "result": "Win",
    },
    "2026-05-08T23:18": {  # #2 (DB id=4) BTC SHORT LOSS
        "exit_price": 80150.30, "qty": 0.029,
        "closed_pnl": -18.1560,
        "opening_fee": 1.269813, "closing_fee": 1.27839729, "funding_fee": 0.0,
        "open_volume": 2308.75, "closed_volume": 2324.35,
        "trade_type": "Close Short", "result": "Loss",
    },
    "2026-05-09T12:12": {  # #3 (DB id=5) XRP LONG WIN (우발)
        "exit_price": 1.4299, "qty": 1590,
        "closed_pnl": 18.1555,
        "opening_fee": 1.23899161, "closing_fee": 1.25044755, "funding_fee": 0.1840,
        "open_volume": 2252.71, "closed_volume": 2273.54,
        "trade_type": "Close Long", "result": "Win",
    },
    "2026-05-11T03:36": {  # #4 (DB id=6) BTC LONG WIN
        "exit_price": 81286.50, "qty": 0.029,
        "closed_pnl": 26.0467,
        "opening_fee": 1.28073077, "closing_fee": 1.29651968, "funding_fee": 0.0831,
        "open_volume": 2328.60, "closed_volume": 2357.30,
        "trade_type": "Close Long", "result": "Win",
    },
    "2026-05-11T05:44": {  # #5 (DB id=7) BTC LONG LOSS
        "exit_price": 80836.95, "qty": 0.03,
        "closed_pnl": -19.4751,
        "opening_fee": 1.34304885, "closing_fee": 1.3338098, "funding_fee": 0.0,
        "open_volume": 2441.90, "closed_volume": 2425.10,
        "trade_type": "Close Long", "result": "Loss",
    },
    "2026-05-11T12:36": {  # #6 (DB id=8) XRP LONG LOSS (체결 조회 실패)
        "exit_price": 1.4395, "qty": 1616,
        "closed_pnl": -40.4442,
        "opening_fee": 1.30020881, "closing_fee": 1.27951419, "funding_fee": 0.2379,
        "open_volume": 2364.01, "closed_volume": 2326.38,
        "trade_type": "Close Long", "result": "Loss",
    },
    "2026-05-11T14:31": {  # #7 (DB 삭제됨, 노이즈 처리) BTC SHORT 운영자 강제 청산
        "exit_price": 80911.10, "qty": 0.027,
        "closed_pnl": -11.1894,
        "opening_fee": 1.19669468, "closing_fee": 1.20152984, "funding_fee": 0.0,
        "open_volume": 2175.80, "closed_volume": 2184.59,
        "trade_type": "Close Short", "result": "Loss (Noise)",
    },
    "2026-05-12T02:01": {  # #9 (DB id=10) XRP LONG WIN (체결 조회 실패)
        "exit_price": 1.4695, "qty": 1175,
        "closed_pnl": 17.9947,
        "opening_fee": 0.93874275, "closing_fee": 0.94966439, "funding_fee": -0.0257,
        "open_volume": 1706.80, "closed_volume": 1726.66,
        "trade_type": "Close Long", "result": "Win",
    },
}


def match_bybit_actual(exit_ts: str):
    """exit_timestamp로 Bybit 실측 데이터 매칭. ±2분 fuzzy match."""
    if not exit_ts:
        return None
    try:
        # DB exit_ts는 KST naive
        exit_dt = datetime.fromisoformat(exit_ts).replace(tzinfo=KST)
        exit_posix = exit_dt.timestamp()
    except Exception:
        return None

    best = None
    best_diff = float("inf")
    for key, data in BYBIT_ACTUAL.items():
        try:
            # Bybit Trade Time 형식: "2026-05-12T02:01" (KST 가정)
            key_dt = datetime.fromisoformat(key + ":00").replace(tzinfo=KST)
            key_posix = key_dt.timestamp()
            diff = abs(exit_posix - key_posix)
        except Exception:
            continue
        if diff < best_diff:
            best_diff = diff
            best = data
    # ±120초 (2분) 이내만 매칭
    if best and best_diff < 120:
        return best
    return None


def find_entry_cycle(symbol: str, entry_timestamp: str):
    """Cycle log 디렉토리에서 진입 시각과 가장 가까운 TRADE cycle 찾기."""
    try:
        # DB entry_timestamp는 KST naive — KST timezone 명시
        entry_dt = datetime.fromisoformat(entry_timestamp).replace(tzinfo=KST)
        entry_posix = entry_dt.timestamp()
    except Exception:
        return None

    if not CYCLE_DIR.exists():
        return None

    best = None
    best_diff = float("inf")
    for fp in CYCLE_DIR.rglob("*_TRADE_*.json"):
        try:
            d = json.loads(fp.read_text())
        except Exception:
            continue
        if d.get("symbol") != symbol:
            continue
        started = d.get("started_at", "")
        try:
            # cycle started_at은 UTC ISO (Z 또는 +00:00)
            dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                # naive면 UTC로 가정
                dt = dt.replace(tzinfo=timezone.utc)
            cycle_posix = dt.timestamp()
            diff = abs(cycle_posix - entry_posix)
        except Exception:
            continue
        if diff < best_diff:
            best_diff = diff
            best = (fp, d)
    # 진입 시점과 120초 이내 cycle만 매칭
    if best and best_diff < 120:
        return best
    return None


def count_rechecks(uuid: str):
    """포지션 UUID의 recheck 수와 결정 분포 반환."""
    con = sqlite3.connect(str(DB_PATH))
    try:
        cur = con.execute(
            "SELECT ai_decision FROM position_rechecks WHERE position_uuid=?",
            (uuid,)
        )
        decisions = [r[0] for r in cur.fetchall()]
        return {
            "recheck_count": len(decisions),
            "hold_count": decisions.count("HOLD"),
            "modify_count": decisions.count("MODIFY"),
            "exit_count": decisions.count("EXIT"),
        }
    finally:
        con.close()


def safe(d, *keys, default=None):
    """안전한 nested dict 접근."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k)
        if cur is None:
            return default
    return cur


def build_row(pos: dict):
    """거래 1건의 CSV row 생성."""
    row = {}

    # === 메타 ===
    row["trade_id"] = pos["id"]
    row["symbol"] = pos["symbol"]
    row["direction"] = pos["direction"]
    row["leverage"] = pos["leverage"]
    row["entry_ts"] = pos["entry_timestamp"]
    row["exit_ts"] = pos.get("exit_timestamp", "")

    # 보유시간
    try:
        entry_dt = datetime.fromisoformat(pos["entry_timestamp"])
        if pos.get("exit_timestamp"):
            exit_dt = datetime.fromisoformat(pos["exit_timestamp"])
            row["hold_hours"] = round((exit_dt - entry_dt).total_seconds() / 3600, 2)
        else:
            row["hold_hours"] = ""
    except Exception:
        row["hold_hours"] = ""

    # === 진입가/청산가/PnL (DB) ===
    row["entry_price"] = pos.get("entry_price", "")
    row["exit_price"] = pos.get("exit_price", "")
    row["realized_pnl_usd"] = pos.get("realized_pnl", "")
    row["realized_pnl_pct"] = pos.get("realized_pnl_percentage", "")
    row["exit_reason"] = pos.get("exit_reason", "")
    row["entry_fee"] = pos.get("entry_fee", "")
    row["exit_fee"] = pos.get("exit_fee", "")
    row["total_fee"] = pos.get("total_fee", "")
    row["status"] = pos.get("status", "")

    # === 진입 cycle log ===
    cycle = find_entry_cycle(pos["symbol"], pos["entry_timestamp"])
    if cycle:
        fp, d = cycle
        af = d.get("phases", {}).get("ai_filter", {})
        is_long = pos["direction"] == "LONG"
        result_key = "long_result" if is_long else "short_result"
        result = af.get(result_key, {})
        score_key = "long_score" if is_long else "short_score"
        reasoning_key = "long_reasoning" if is_long else "short_reasoning"

        row["ai_score"] = result.get(score_key, "")
        row["ai_pattern"] = result.get("pattern", "")
        if_taken = result.get("if_taken", {})
        row["ai_sl"] = if_taken.get("stop_price", "")
        row["ai_tp"] = if_taken.get("target_price", "")
        row["ai_rr"] = if_taken.get("rr_ratio", "")
        row["ai_probability"] = if_taken.get("probability", "")
        row["ai_market_story"] = (result.get("market_story") or "").replace("\n", " ")
        row["ai_reasoning"] = (result.get(reasoning_key) or "").replace("\n", " ")
        row["ai_next_recheck_h"] = result.get("next_recheck_hours", "")

        # 시장 데이터
        md = d.get("market_data", {})
        ind = md.get("indicators", {})
        mtf = md.get("multi_timeframe", {})
        fut = md.get("futures", {})
        pl = ind.get("price_levels", {})

        # SL ATR배수 계산
        try:
            ep = float(pos["entry_price"])
            sp = float(if_taken.get("stop_price", 0))
            atr_pct = float(ind.get("atr_pct", 0))
            sl_dist_pct = abs(ep - sp) / ep * 100
            row["sl_distance_pct"] = round(sl_dist_pct, 3)
            tp_p = float(if_taken.get("target_price", 0))
            tp_dist_pct = abs(tp_p - ep) / ep * 100
            row["tp_distance_pct"] = round(tp_dist_pct, 3)
            if atr_pct > 0:
                row["sl_atr_multiplier"] = round(sl_dist_pct / atr_pct, 2)
            else:
                row["sl_atr_multiplier"] = ""
        except Exception:
            row["sl_distance_pct"] = ""
            row["sl_atr_multiplier"] = ""
            row["tp_distance_pct"] = ""

        # 1H 지표
        row["entry_price_cycle"] = fut.get("last_price", "")
        row["rsi_1h"] = round(ind.get("rsi", 0), 1) if ind.get("rsi") else ""
        row["adx_1h"] = round(ind.get("adx", 0), 1) if ind.get("adx") else ""
        row["plus_di_1h"] = round(ind.get("plus_di", 0), 1) if ind.get("plus_di") else ""
        row["minus_di_1h"] = round(ind.get("minus_di", 0), 1) if ind.get("minus_di") else ""
        row["atr_pct_1h"] = round(ind.get("atr_pct", 0), 3) if ind.get("atr_pct") else ""
        row["bb_width_1h"] = round(ind.get("bb_width", 0), 2) if ind.get("bb_width") else ""
        row["volume_ratio_1h"] = round(ind.get("volume_ratio", 0), 2) if ind.get("volume_ratio") else ""
        row["macd_hist_1h"] = round(ind.get("macd_histogram", 0), 4) if ind.get("macd_histogram") else ""
        row["candle_patterns"] = "|".join(ind.get("candle_patterns", {}).get("detected", [])) if isinstance(ind.get("candle_patterns"), dict) else ""
        row["divergence"] = ind.get("divergence", {}).get("type", "") if isinstance(ind.get("divergence"), dict) else ""

        # MTF
        for tf in ["1d", "4h", "15m"]:
            t = mtf.get(tf, {})
            row[f"rsi_{tf}"] = round(t.get("rsi", 0), 1) if t.get("rsi") else ""
            row[f"adx_{tf}"] = round(t.get("adx", 0), 1) if t.get("adx") else ""
            row[f"macd_dir_{tf}"] = t.get("macd_direction", "")
            row[f"macd_accel_{tf}"] = "Y" if t.get("macd_accelerating") else "N"
            row[f"ema_aligned_{tf}"] = "Y" if t.get("ema_20_50_bullish") else "N"
            row[f"px_vs_ema20_{tf}"] = t.get("price_vs_ema20", "")

        # 선물 데이터
        row["funding_rate_pct"] = round(fut.get("funding_rate_pct", 0), 4) if fut.get("funding_rate_pct") is not None else ""
        row["open_interest"] = fut.get("open_interest", "")
        row["high_24h"] = pl.get("high_24h", "")
        row["low_24h"] = pl.get("low_24h", "")
        row["position_in_24h_range_pct"] = pl.get("position_in_24h_range_pct", "")
        row["high_7d"] = pl.get("high_7d", "")
        row["low_7d"] = pl.get("low_7d", "")

        # 레짐
        regime = d.get("phases", {}).get("regime", {})
        row["regime"] = regime.get("regime", "")
        row["regime_confidence"] = regime.get("confidence", "")

        row["cycle_log_file"] = fp.name
    else:
        # cycle log 못 찾으면 빈 컬럼
        for key in [
            "ai_score", "ai_pattern", "ai_sl", "ai_tp", "ai_rr", "ai_probability",
            "ai_market_story", "ai_reasoning", "ai_next_recheck_h",
            "sl_distance_pct", "sl_atr_multiplier", "tp_distance_pct",
            "entry_price_cycle", "rsi_1h", "adx_1h", "plus_di_1h", "minus_di_1h",
            "atr_pct_1h", "bb_width_1h", "volume_ratio_1h", "macd_hist_1h",
            "candle_patterns", "divergence",
            "rsi_1d", "adx_1d", "macd_dir_1d", "macd_accel_1d", "ema_aligned_1d", "px_vs_ema20_1d",
            "rsi_4h", "adx_4h", "macd_dir_4h", "macd_accel_4h", "ema_aligned_4h", "px_vs_ema20_4h",
            "rsi_15m", "adx_15m", "macd_dir_15m", "macd_accel_15m", "ema_aligned_15m", "px_vs_ema20_15m",
            "funding_rate_pct", "open_interest",
            "high_24h", "low_24h", "position_in_24h_range_pct", "high_7d", "low_7d",
            "regime", "regime_confidence", "cycle_log_file",
        ]:
            row[key] = ""

    # === Recheck ===
    rc = count_rechecks(pos["position_uuid"])
    row.update(rc)

    # === 운영자 Bybit 실측 데이터 매칭 ===
    bybit = match_bybit_actual(pos.get("exit_timestamp", ""))
    if bybit:
        row["bybit_actual_exit_price"] = bybit["exit_price"]
        row["bybit_actual_closed_pnl"] = bybit["closed_pnl"]
        row["bybit_actual_opening_fee"] = bybit["opening_fee"]
        row["bybit_actual_closing_fee"] = bybit["closing_fee"]
        row["bybit_actual_funding_fee"] = bybit["funding_fee"]
        row["bybit_actual_total_fee"] = round(bybit["opening_fee"] + bybit["closing_fee"], 6)
        row["bybit_open_volume"] = bybit["open_volume"]
        row["bybit_closed_volume"] = bybit["closed_volume"]
        row["bybit_result"] = bybit["result"]

        # 차이 계산
        try:
            db_pnl = float(pos.get("realized_pnl", 0) or 0)
            row["pnl_discrepancy_usd"] = round(bybit["closed_pnl"] - db_pnl, 4)
        except Exception:
            row["pnl_discrepancy_usd"] = ""

        try:
            db_exit = float(pos.get("exit_price", 0) or 0)
            if db_exit > 0:
                row["price_discrepancy_pct"] = round((bybit["exit_price"] - db_exit) / db_exit * 100, 4)
            else:
                row["price_discrepancy_pct"] = ""
        except Exception:
            row["price_discrepancy_pct"] = ""
    else:
        row["bybit_actual_exit_price"] = ""
        row["bybit_actual_closed_pnl"] = ""
        row["bybit_actual_opening_fee"] = ""
        row["bybit_actual_closing_fee"] = ""
        row["bybit_actual_funding_fee"] = ""
        row["bybit_actual_total_fee"] = ""
        row["bybit_open_volume"] = ""
        row["bybit_closed_volume"] = ""
        row["bybit_result"] = ""
        row["pnl_discrepancy_usd"] = ""
        row["price_discrepancy_pct"] = ""

    return row


def build_noise_row():
    """노이즈 거래 #7 (DB에서 삭제됨, Bybit 실측만 있음) row 생성."""
    row = {}
    bybit_key = "2026-05-11T14:31"
    bybit = BYBIT_ACTUAL.get(bybit_key, {})

    # 기본 메타 (DB 삭제됨)
    row["trade_id"] = "NOISE"
    row["symbol"] = "BTCUSDT"
    row["direction"] = "SHORT"
    row["leverage"] = 5
    row["entry_ts"] = "2026-05-11T12:38:56 (DB 삭제됨)"
    row["exit_ts"] = "2026-05-11T14:31:42"
    row["hold_hours"] = round((14*60+31 - (12*60+38)) / 60, 2)  # ≈ 1.88h
    row["status"] = "DELETED (운영자 강제 청산, 노이즈 처리)"

    # DB 정보 없음 (삭제됨)
    row["entry_price"] = 80585.50
    row["exit_price"] = bybit.get("exit_price", "")
    row["realized_pnl_usd"] = "(DB 삭제)"
    row["realized_pnl_pct"] = ""
    row["exit_reason"] = "FORCED_CLOSE_NOISE"

    # cycle log에서 진입 정보 추출 (5/11 12:38 entry log)
    entry_cycle_fp = CYCLE_DIR / "2026-05-11" / "123857-545_analysis_BTCUSDT_TRADE_SHORT.json"
    if entry_cycle_fp.exists():
        d = json.loads(entry_cycle_fp.read_text())
        af = d.get("phases", {}).get("ai_filter", {})
        result = af.get("short_result", {})
        if_taken = result.get("if_taken", {})

        row["ai_score"] = result.get("short_score", "")
        row["ai_pattern"] = result.get("pattern", "")
        row["ai_sl"] = if_taken.get("stop_price", "")
        row["ai_tp"] = if_taken.get("target_price", "")
        row["ai_rr"] = if_taken.get("rr_ratio", "")
        row["ai_probability"] = if_taken.get("probability", "")
        row["ai_market_story"] = (result.get("market_story") or "").replace("\n", " ")
        row["ai_reasoning"] = (result.get("short_reasoning") or "").replace("\n", " ")

        md = d.get("market_data", {})
        ind = md.get("indicators", {})
        mtf = md.get("multi_timeframe", {})
        fut = md.get("futures", {})
        pl = ind.get("price_levels", {})

        try:
            ep = 80585.50
            sp = float(if_taken.get("stop_price", 0))
            atr_pct = float(ind.get("atr_pct", 0))
            sl_dist_pct = abs(ep - sp) / ep * 100
            row["sl_distance_pct"] = round(sl_dist_pct, 3)
            row["sl_atr_multiplier"] = round(sl_dist_pct / atr_pct, 2) if atr_pct else ""
            tp_p = float(if_taken.get("target_price", 0))
            row["tp_distance_pct"] = round(abs(tp_p - ep) / ep * 100, 3)
        except Exception:
            pass

        row["rsi_1h"] = round(ind.get("rsi", 0), 1) if ind.get("rsi") else ""
        row["adx_1h"] = round(ind.get("adx", 0), 1) if ind.get("adx") else ""
        row["plus_di_1h"] = round(ind.get("plus_di", 0), 1) if ind.get("plus_di") else ""
        row["minus_di_1h"] = round(ind.get("minus_di", 0), 1) if ind.get("minus_di") else ""
        row["atr_pct_1h"] = round(ind.get("atr_pct", 0), 3) if ind.get("atr_pct") else ""
        row["bb_width_1h"] = round(ind.get("bb_width", 0), 2) if ind.get("bb_width") else ""
        row["volume_ratio_1h"] = round(ind.get("volume_ratio", 0), 2) if ind.get("volume_ratio") else ""
        row["candle_patterns"] = "|".join(ind.get("candle_patterns", {}).get("detected", [])) if isinstance(ind.get("candle_patterns"), dict) else ""
        row["divergence"] = ind.get("divergence", {}).get("type", "") if isinstance(ind.get("divergence"), dict) else ""

        for tf in ["1d", "4h", "15m"]:
            t = mtf.get(tf, {})
            row[f"rsi_{tf}"] = round(t.get("rsi", 0), 1) if t.get("rsi") else ""
            row[f"adx_{tf}"] = round(t.get("adx", 0), 1) if t.get("adx") else ""
            row[f"macd_dir_{tf}"] = t.get("macd_direction", "")
            row[f"macd_accel_{tf}"] = "Y" if t.get("macd_accelerating") else "N"

        row["funding_rate_pct"] = round(fut.get("funding_rate_pct", 0), 4) if fut.get("funding_rate_pct") is not None else ""
        row["position_in_24h_range_pct"] = pl.get("position_in_24h_range_pct", "")
        row["cycle_log_file"] = "123857-545_analysis_BTCUSDT_TRADE_SHORT.json (NOISE)"

    # Bybit 실측
    if bybit:
        row["bybit_actual_exit_price"] = bybit["exit_price"]
        row["bybit_actual_closed_pnl"] = bybit["closed_pnl"]
        row["bybit_actual_opening_fee"] = bybit["opening_fee"]
        row["bybit_actual_closing_fee"] = bybit["closing_fee"]
        row["bybit_actual_funding_fee"] = bybit["funding_fee"]
        row["bybit_actual_total_fee"] = round(bybit["opening_fee"] + bybit["closing_fee"], 6)
        row["bybit_result"] = bybit["result"]

    return row


def main():
    if not DB_PATH.exists():
        print(f"DB 없음: {DB_PATH}")
        return

    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    cur = con.execute(
        "SELECT * FROM futures_positions ORDER BY id"
    )
    positions = [dict(r) for r in cur.fetchall()]
    con.close()

    print(f"총 {len(positions)}개 포지션 (ACTIVE 포함)")

    rows = [build_row(p) for p in positions]
    # 노이즈 거래 (#7, DB 삭제) 추가 — 시간순 삽입 (id=8과 10 사이)
    noise_row = build_noise_row()
    # 시간순 정렬: entry_ts 기준
    rows.append(noise_row)
    rows.sort(key=lambda r: str(r.get("entry_ts", "")))

    # 컬럼 순서 (보기 좋게)
    columns = [
        "trade_id", "symbol", "direction", "leverage",
        "entry_ts", "exit_ts", "hold_hours", "status",
        # 진입/청산가/PnL
        "entry_price", "exit_price", "exit_reason",
        "realized_pnl_usd", "realized_pnl_pct",
        "entry_fee", "exit_fee", "total_fee",
        # AI 결정
        "ai_score", "ai_pattern", "ai_probability", "ai_rr",
        "ai_sl", "ai_tp",
        "sl_distance_pct", "tp_distance_pct", "sl_atr_multiplier",
        # 시장 1H
        "entry_price_cycle", "rsi_1h", "adx_1h", "plus_di_1h", "minus_di_1h",
        "atr_pct_1h", "bb_width_1h", "volume_ratio_1h", "macd_hist_1h",
        "candle_patterns", "divergence",
        # MTF
        "rsi_1d", "adx_1d", "macd_dir_1d", "macd_accel_1d", "ema_aligned_1d", "px_vs_ema20_1d",
        "rsi_4h", "adx_4h", "macd_dir_4h", "macd_accel_4h", "ema_aligned_4h", "px_vs_ema20_4h",
        "rsi_15m", "adx_15m", "macd_dir_15m", "macd_accel_15m", "ema_aligned_15m", "px_vs_ema20_15m",
        # 선물
        "funding_rate_pct", "open_interest",
        "high_24h", "low_24h", "position_in_24h_range_pct", "high_7d", "low_7d",
        # 레짐
        "regime", "regime_confidence",
        # Recheck
        "recheck_count", "hold_count", "modify_count", "exit_count",
        # Bybit 실측
        "bybit_actual_exit_price", "bybit_actual_closed_pnl",
        "bybit_actual_opening_fee", "bybit_actual_closing_fee", "bybit_actual_funding_fee",
        "bybit_actual_total_fee", "bybit_open_volume", "bybit_closed_volume", "bybit_result",
        "pnl_discrepancy_usd", "price_discrepancy_pct",
        # AI reasoning (긴 텍스트, 마지막에)
        "ai_market_story", "ai_reasoning", "ai_next_recheck_h",
        # 메타
        "cycle_log_file",
    ]

    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=columns)
        w.writeheader()
        for r in rows:
            # 누락된 컬럼은 빈 문자열
            for c in columns:
                if c not in r:
                    r[c] = ""
            w.writerow(r)

    print(f"CSV 저장: {OUT_CSV}")
    print(f"총 {len(rows)} 거래, {len(columns)} 컬럼")


if __name__ == "__main__":
    main()
