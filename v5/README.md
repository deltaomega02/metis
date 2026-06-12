# METIS v4 — Production Setup

> R1~R5 합의 + GPT-5.5 Pro adversarial review 통과한 prompt freeze v1.
> 운영자 — Programming.

## 한 줄 요약

Bybit USDT-Perpetual (SOLUSDT + ETHUSDT) winner-takes-all 15분 단타. Gemini 3.5 Flash 단일. GCP e2-micro 운용 가능.

---

## 1. 사전 준비

### Python
- 3.9 이상

### Secrets (.env)
```bash
cd /Users/sue/Projects/METIS/v4
cp .env.example .env
# 편집
```

필수:
- `GEMINI_API_KEY` — paper/live 모두 필요
- `PAPER_MODE=true` — 페이퍼는 이거면 충분 (mainnet public 데이터만 사용)

선택:
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — 알림 받으려면
- `BYBIT_API_KEY` + `BYBIT_SECRET` — *live* 모드 시 필수 (trade-only, withdrawal disabled, IP allowlist)

### Dependencies
```bash
cd /Users/sue/Projects/METIS/v4
pip3 install --user -r code/requirements.txt
```

### Event YAML
`code/config/events.yaml` 의 `last_updated_utc`를 *오늘 ± 7일 이내*로 유지.
> 7일 초과 = conservative block (안전 모드, no trade)

---

## 2. 실행

### 봇 본체
```bash
cd /Users/sue/Projects/METIS/v4/code
python3 main.py
```

로그: `code/logs/metis_v4.log` 동시에 stdout.

### 대시보드 (별도 터미널, 다른 process)
```bash
cd /Users/sue/Projects/METIS/v4/code
streamlit run observability/dashboard.py
```
브라우저 자동 오픈 — 흰색 light theme, 한 화면.

### 종료
`Ctrl+C` — graceful shutdown (open positions 보존, WS close, telegram notify).

---

## 3. Stop (running)
```bash
# graceful
kill -SIGTERM <pid>
# or
pkill -SIGTERM -f "python3 main.py"
```

---

## 4. 테스트 (선택)
```bash
cd /Users/sue/Projects/METIS/v4/code
python3 -m pytest tests/ -v
```

---

## 5. 디렉토리 구조

```
v4/
├── README.md            # 이 파일
├── .env.example
├── .env                 # 사용자 작성 (gitignore)
├── design/              # R5 freeze 문서 + prompt v1 active
├── meetings/            # R1~R5 회의록
├── research/            # 외부 출처 정리
└── code/
    ├── main.py
    ├── requirements.txt
    ├── config/
    │   ├── settings.py
    │   └── events.yaml
    ├── core/            # state_store / order_journal / risk_engine / feature_builder / ...
    ├── ai/              # gemini_client / prompt_registry / response_schema
    ├── exchange/        # bybit_client / paper_executor
    ├── observability/   # telegram_bot / dashboard
    └── tests/
```

---

## 6. Paper 운영 가이드라인 (R5 합의)

- **검증 기준**: 90일 AND 150 trades, post-cost expectancy_R > 0.10, profit factor > 1.3, max DD < 4%, schema fail rate < 0.5%, hard risk invariant 위반 0회
- **며칠 돌리기**: *시스템 작동 smoke test*로 OK. 단 표본 부족 (n<30) → edge 결론 X
- 실전 전환 시 risk/trade 0.10-0.15% 시작 → 2-4주 ramp 후 0.25%

---

## 7. 무엇이 *없는*가 (의도적)

- AI Full Delegation (v2 실패)
- 자기수정 / lessons / 회고 (v3 누적 fix 45 실패 패턴)
- BTC (v3 5/6 패배 데이터)
- 멀티 코인 분석 (SOL+ETH winner-takes-all, 동시 다중 포지션 X)
- Trailing stop 기본 활성 (R5: 검증 후 조건부만)

