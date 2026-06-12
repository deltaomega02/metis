# METIS v1 (METIS-F)

암호화폐 무기한 선물 자동매매 시스템 — 1세대.

Bybit BTCUSDT 단일 심볼 대상. AI(Gemini) 기반 진입 결정 + 코드 기반 SL/TP 관리.


---

## 개요

METIS v1은 *futures 자동매매의 첫 번째 안정화 버전*. 2026년 3월 개발, 4월 운영 시작.

설계 철학:
- 단일 심볼(BTCUSDT) 집중
- 1H/4H/1D 멀티 타임프레임 분석
- Phase 1~4 순차 실행 (데이터 수집 → 레짐 → AI 판단 → 진입)
- SL/TP는 진입 시 고정, 청산은 거래소 자동

---

## 주요 기능

- AI 진입 판단 (LONG/SHORT/WAIT)
- 멀티 타임프레임 지표 (1H 주력 + 4H/1D 컨텍스트)
- 레짐 분류 (BULL/BEAR/NEUTRAL)
- WebSocket 기반 실시간 포지션 감시
- 텔레그램 알림
- Streamlit 대시보드

---

## 프로젝트 구조

```
v1/
├── main.py                 # 메인 이벤트 루프
├── ai/                     # Gemini 프롬프트 + 클라이언트
├── config/                 # 설정 (settings.py)
├── core/
│   ├── data_fetcher.py     # Bybit kline 수집
│   ├── technical_analysis.py
│   ├── regime_engine.py
│   ├── leverage_calculator.py
│   ├── position_manager.py
│   ├── websocket_watcher.py
│   └── scheduler.py
├── exchange/
│   ├── bybit_client.py
│   └── bybit_websocket.py
├── utils/
│   └── telegram_bot.py
└── requirements.txt
```

---

## v1의 한계

- 단일 심볼 (BTCUSDT만)
- 순차 처리 (한 심볼씩)
- 중간 점검(recheck) 시스템 없음
- Profit Guard 미구현 (SL/TP 고정)
- Paper trading 모드 미지원

---

## 종료 사유

2026년 5월 초 *ATHENA(spot 봇)*로 일시 전환. 이후 perpetual futures 검증 v2 (METIS_F2) 시작.

v1 코드는 *역사 기록*. 운영 X.

---

