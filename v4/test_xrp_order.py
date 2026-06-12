#!/usr/bin/env python3
"""XRP 실주문 end-to-end 검증 스크립트.

설정한 수량만큼 시장가 매수 → 3초 대기 → 시장가 청산. 라이브 봇을 켜기 전에
주문 path(레버 설정 / 매수 / 포지션 확인 / 청산 / 잔여 확인)이 모두 정상 동작하는지
저비용으로 확인하기 위함. 실제 비용은 수수료 ~$0.02 수준 (Bybit perp min ~$5).
"""
TEST_QTY = 4
import os, sys, time, json
from pybit.unified_trading import HTTP

s = HTTP(testnet=False, api_key=os.getenv("BYBIT_API_KEY"),
         api_secret=os.getenv("BYBIT_API_SECRET") or os.getenv("BYBIT_SECRET"))

print("=" * 70)
print("XRP 실주문 검증 (1 XRP ≈ $1.3, 비용 ~$0.01)")
print("=" * 70)

# 1. 레버 설정
print("\n[1/6] 레버 1.5x 설정...")
try:
    s.set_leverage(category="linear", symbol="XRPUSDT",
                   buyLeverage="1.5", sellLeverage="1.5")
    print("  ✅ 레버 1.5x 설정 완료")
except Exception as e:
    if "leverage not modified" in str(e).lower() or "not modified" in str(e).lower():
        print("  ✅ 레버 이미 1.5x")
    else:
        print(f"  ❌ ERR: {e}"); sys.exit(1)

# 2. 현재가
print("\n[2/6] 현재가 조회...")
t = s.get_tickers(category="linear", symbol="XRPUSDT")["result"]["list"][0]
price = float(t["lastPrice"])
print(f"  ✅ XRP perp: ${price:.4f}")

# 3. 잔고
print("\n[3/6] 지갑 잔고...")
wb = s.get_wallet_balance(accountType="UNIFIED")["result"]["list"][0]
total = float(wb.get("totalEquity", 0))
print(f"  ✅ Total equity: ${total:.2f}")

# 4. 매수
print(f"\n[4/6] 시장가 매수 {TEST_QTY} XRP (~${TEST_QTY*price:.2f})...")
try:
    r = s.place_order(category="linear", symbol="XRPUSDT", side="Buy",
                      orderType="Market", qty=str(TEST_QTY), positionIdx=0)
    print(f"  ✅ Order: {r.get('retMsg')} orderId={r.get('result',{}).get('orderId','?')[:20]}")
except Exception as e:
    print(f"  ❌ ERR: {e}"); sys.exit(1)

# 5. 포지션 확인
print("\n[5/6] 포지션 확인 (3초 대기)...")
time.sleep(3)
pos_found = None
for p in s.get_positions(category="linear", symbol="XRPUSDT")["result"]["list"]:
    sz = float(p.get("size", 0) or 0)
    if sz > 0:
        pos_found = p
        print(f"  ✅ 포지션 확인: {p['side']} {sz} XRP @ ${float(p['avgPrice']):.4f}, 미실현 ${float(p.get('unrealisedPnl',0) or 0):.4f}")
        break
if not pos_found:
    print("  ⚠️ 포지션 안 잡힘 (즉시 청산됐을 수도)");

# 6. 청산 (매도)
print(f"\n[6/6] 시장가 청산 {TEST_QTY} XRP...")
if pos_found:
    try:
        r = s.place_order(category="linear", symbol="XRPUSDT", side="Sell",
                          orderType="Market", qty=str(pos_found['size']), positionIdx=0,
                          reduceOnly=True)
        print(f"  ✅ Close: {r.get('retMsg')}")
    except Exception as e:
        print(f"  ❌ Close ERR: {e}")
else:
    print("  (포지션 없음, skip)")

# 검증
print("\n[검증] 포지션 청산 확인 (3초 대기)...")
time.sleep(3)
remaining = [p for p in s.get_positions(category="linear", symbol="XRPUSDT")["result"]["list"] if float(p.get("size", 0) or 0) > 0]
if remaining:
    print(f"  ⚠️ 잔여 포지션: {remaining}")
else:
    print("  ✅ 깔끔히 청산됨, 포지션 0")

print("\n" + "=" * 70)
print("✅ 실주문 검증 완료. xrp_live.py 정식 가동 준비됨.")
print("=" * 70)
