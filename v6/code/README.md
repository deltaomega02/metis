# METIS v6 — 4h 다중코인 돌파 포트폴리오 엔진

규칙 기반(LLM 없음) 암호화폐 무기한 선물 자동매매 봇. 8개 코인의 4시간봉 돌파를
포착해 구조적 손절과 비대칭 청산으로 운용하는 포트폴리오 엔진이다. 단일 asyncio
프로세스로 동작하며, GCP e2-micro(1GB) 위에서 상시 가동을 전제로 설계됐다.

엔진은 두 가지 모드로 같은 코드를 실행한다.

- **LIVE**: 실거래소(Bybit)에 단일 ls 암을 실체결로 운용.
- **PAPER**: 게이트/방향 변종(arm)들을 독립 페이퍼 포트폴리오로 동시 forward-test.

현재 운영은 LIVE(실전 ls) + PAPER ls(슬리피지 비교)를 **별도 프로세스 두 개**로
동시 가동한다.

---

## 1. 전략

### 진입 신호 (`core/breakout_signal.py`)

마지막 **종가 마감봉** 기준으로만 평가한다(미완성봉 제외).

- 대상: BTC, ETH, SOL, XRP, BNB, DOGE, ADA, AVAX (4h)
- 롱: 종가 > 직전 Donchian-20 신고가 **AND** EMA20 > EMA50 **AND** ADX(14) > 22
- 숏: 종가 < 직전 Donchian-20 신저가 **AND** EMA20 < EMA50 **AND** ADX(14) > 22
- 구조적 손절(SL): 진입가에서 1.5 × ATR 떨어진 가격
- 숏은 `allow_short=True`인 암에서만 진입한다.

### 청산 — 비대칭 mixed exit

방향에 따라 청산 규칙이 다르다. 백테스트(IS/OOS/walk-forward)로 확정한 구조다.

- **롱 = 추세종료 청산**: 종가가 EMA50 아래로 복귀하면 청산. 고정 익절 없음 —
  추세가 끝날 때까지 탄다.
- **숏 = 고정 2.5R 익절**: `SHORT_TP_R = 2.5`. 숏에 추세종료 청산을 쓰면 EMA50이
  진입가 위에 있어 "진입가 위로 반등"을 청산 조건으로 요구하게 되고, 이익을 다
  토한 뒤 손실로 청산된다. 따라서 숏은 고정 R 배수 익절로 받는다.

트레일링, scale-out, regime-exit 변형은 모두 백테스트에서 mixed exit보다 OOS가
낮아 기각됐다.

### 리스크

- 트레이드당 risk 0.75%, 동시 포지션 cap 4, 레버리지 3
- 사이징 = (자본 × risk%) ÷ 손절거리. 레버리지는 수익과 무관하며 청산밴드만
  바꾼다(청산밴드 33% ≫ 손절 최대 ~9.8% → 청산 사실상 불가).
- 게이트(BTC 레짐 필터): `none`(무필터) / `ema20_50`(빠름) / `ema50_200`(보수).
  LIVE ls는 무게이트(none) 운용.

---

## 2. 아키텍처

WebSocket을 쓰지 않는다. 4시간 저빈도 + 진입 시 원자적 SL/TP 위탁 + 매 사이클
reconcile로 충분하므로 REST 주기 폴링으로 단순화했다(micro VM 메모리 절약).

```
config/settings.py        설정 단일 출처(전략 파라미터, 암 정의, 경로, env)
core/
  indicators.py           EMA / ATR / ADX / Donchian (numpy)
  breakout_signal.py      진입 신호(EntryIntent) + 게이트 + 청산 판정
  risk_engine.py          사이징 + 동시 cap + manual_kill
  scheduler.py            4h anchored 사이클(봉마감 + 버퍼 후 발화)
  state_store.py          SQLite(WAL): 포지션/거래/자본곡선/저널/마일스톤
  market_snapshot.py      매 사이클 코인별 지표 스냅샷(market.json) 발행
exchange/
  bybit_client.py         Bybit V5 REST(재시도/백오프, orderLinkId 멱등)
  executor.py             LiveExecutor — 실주문 + closed-pnl/execution 실측 저장
  paper_executor.py       PaperExecutor — 자가 기장(net-R, 수수료 포함)
observability/
  telegram.py             진입/청산/사이클/에러/마일스톤 알림(한국어)
  dashboard.py            bare http.server 대시보드(:8501, 자동새로고침 없음)
main.py                   오케스트레이터(엔진)
```

### 사이클 흐름 (`main.py:_on_cycle`)

매 4h 사이클:

1. 8개 코인의 마감 kline을 0.5초 간격으로 한 번씩만 fetch(공유).
2. 각 암을 독립 실행: 청산(SL/추세종료/숏 TP) → 신규 돌파를 ADX 강한 순으로
   cap까지 진입.
3. 코인별 지표 스냅샷을 `data/market.json`에 원자적으로 기록(대시보드가 읽음).

### 정확값 저장

테스트가 의미를 가지려면 DB 저장값이 거래소 실제값과 일치해야 한다.

- 진입 체결가/수량/수수료: `/v5/execution/list`(부분체결 합산)
- 청산가: execution 합산 청산가, 손익/수수료: `/v5/position/closed-pnl`의
  closedPnl(net)·openFee·closeFee를 **바이비트 원본값 그대로** float64 저장
- 슬리피지: 신호 참고가(`ref_entry`/`ref_exit`) vs 실제 체결가를 함께 기록
- 주의: Bybit V5의 closed-pnl/execution은 `startTime`이 없으면 최근 7일만 조회된다.

### 실행 안정성

- 진입 주문에 SL + TP를 원자적으로 첨부(tpslMode=Full, LastPrice 트리거) — 봇이
  죽어도 거래소가 손절/익절을 보호한다(naked 포지션 구간 0).
- REST 재시도 + 백오프(10006/16/18), 체결/closed-pnl 인내심 폴링.
- `orderLinkId`로 멱등성 확보(재시도 중복주문 방지).
- 매 사이클 및 부팅 시 reconcile: 거래소 자동청산 감지 + 사라진 포지션 정리 +
  미추적 포지션 입양.

---

## 3. 설정

전부 `config/settings.py`에서 관리하며 일부는 환경변수로 덮어쓴다(`.env.example` 참조).

| 환경변수 | 기본 | 설명 |
|---|---|---|
| `PAPER_MODE` | true | false면 LIVE(실거래). |
| `PAPER_ARMS_ONLY` | (없음) | PAPER 엔진이 돌릴 암 부분선택(쉼표 목록). 예: `ls`. |
| `CYCLE_BUFFER_SEC` | 45 | 봉마감 후 발화 지연(초). 두 엔진 동시 운영 시 어긋냄. |
| `PAPER_INITIAL_BALANCE_USDT` | 1000 | 페이퍼 시드. |
| `BYBIT_API_KEY` / `BYBIT_SECRET` | | LIVE에서만 필요(페이퍼는 공개 데이터만 사용). |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | 알림(미설정 시 비활성). |
| `DASHBOARD_PORT` | 8501 | 대시보드 포트. |

전략 파라미터(Donchian 20, EMA 20/50, ADX 22, ATR_K 1.5, SHORT_TP_R 2.5, risk
0.75%, cap 4, lev 3)는 `settings.py`의 `STRATEGY`/`RISK`에 정의돼 있다.

---

## 4. 실행

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp .env.example ../.env        # 값 채우기

# 엔진
python3 -u main.py

# 대시보드(별도 프로세스)
python3 -u observability/dashboard.py   # http://<host>:8501/
```

의존성은 `httpx`, `numpy`, `python-dotenv` 세 개뿐이다.

---

## 5. 운영 (GCP e2-micro)

systemd 유닛 정의는 `../deploy/`에 있다.

- `metis-v6.service` — LIVE 엔진(`.env`, PAPER_MODE=false, state.db)
- `metis-v6-paper.service` — PAPER ls 엔진(`paper.env`, PAPER_ARMS_ONLY=ls,
  CYCLE_BUFFER_SEC=150, state_ls.db)
- `metis-v6-dashboard.service` — 대시보드(:8501)

DB는 모두 `data/` 아래 WAL 모드. LIVE는 `state.db`, 페이퍼 암은 암별로
`state_<arm>.db`로 분리된다.

라이브와 페이퍼를 한 IP에서 동시에 돌릴 때는 두 엔진의 kline fetch가 봉마감
정각에 겹쳐 rate-limit(retCode 10006)을 유발할 수 있으므로 `CYCLE_BUFFER_SEC`를
서로 다르게 설정해 어긋낸다(라이브 45초 / 페이퍼 150초).

### 단일 파일 핫픽스

```bash
gcloud compute scp <file> botuser@metis-server:~/metis-v6/code/<subdir>/<file> \
  --zone=asia-northeast3-a
gcloud compute ssh botuser@metis-server --zone=asia-northeast3-a \
  --command="cd ~/metis-v6/code && python3 -m py_compile <subdir>/<file> && \
  sudo systemctl restart metis-v6 metis-v6-dashboard"
```

`settings.py`나 스냅샷 필드를 바꾸면 엔진과 대시보드를 **둘 다** 재시작한다
(대시보드가 부팅 시 settings를 캐시하고, 스냅샷 새 필드는 엔진 사이클 후 생성됨).

---

## 6. 대시보드

bare `http.server` 기반. e2-micro라 무거운 프레임워크(Streamlit/matplotlib 서버
렌더)를 쓰지 않고, 차트는 인라인 SVG로 그려 서버 메모리 점유를 0에 가깝게 둔다.
numpy는 지연 import해 대시보드 프로세스에서 배제한다(~25MB 유지).

데이터 소스는 엔진이 발행한 `market.json` 스냅샷(지표/게이트/롱숏 돌파선,
Bybit 호출 0)과 미실현 표시용 ticker 1회 호출의 하이브리드다.

구성: 실전 vs 페이퍼 비교표, 시장국면(regime) 카드, M2M 자본곡선, 코인별 시장현황
(롱/숏 돌파선과 진입까지 거리), 보유 포지션, 전체 거래내역 2열(슬리피지 포함),
시스템 상태. 자동 새로고침은 의도적으로 넣지 않는다(F5 수동).

---

## 7. 설계 원칙

- 엣지는 코드(결정론적 돌파 신호)에 있다. LLM은 결정자가 아니며 현재 미사용.
- 청산은 구조적 트리거만 사용한다. time-stop / 트레일링 / break-even shift 금지.
- 손실 연속 가드(cooldown / N연패 daily kill) 없음. 하드 가드는 `manual_kill`뿐.
- 페이퍼 손익은 양끝 수수료를 포함한 net-R로 기장해 자본과 정확히 일치시킨다.
- 코드 내부에 버전 라벨(v5/v6)을 넣지 않는다(외부 식별자인 systemd 유닛/DB
  파일명만 예외).
- e2-micro 1GB 전제로 메모리를 철저히 관리한다(raw 배열 즉시 해제, 인라인 SVG,
  numpy 지연 import).
- 엣지 판정은 항상 마찰(수수료/슬리피지) 후 net + walk-forward로 한다. 소표본
  단정 금지.

---

