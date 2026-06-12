#!/bin/bash
# 페이퍼 레버리지 검증(paper_leverage.py) 실행 래퍼 — 실주문 없이 시뮬레이션만.
cd /home/botuser/metis-v4
set -a; source v4.env; set +a
exec /home/botuser/metis-f2/venv/bin/python -u paper_leverage.py
