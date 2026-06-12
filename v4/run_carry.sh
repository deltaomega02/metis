#!/bin/bash
# Funding carry 봇(carry_bot.py) 실행 래퍼 — systemd unit ExecStart에서 호출.
cd /home/botuser/metis-v4
set -a; source v4.env; set +a
exec /home/botuser/metis-f2/venv/bin/python -u carry_bot.py
