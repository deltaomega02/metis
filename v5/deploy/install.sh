#!/usr/bin/env bash
# METIS v5 — One-shot installer for botuser@YOUR_SERVER_IP (GCP e2-micro).
# Run on the VM AFTER you rsync this repo to ~/metis-v5.
#
# Usage on VM:
#   cd ~/metis-v5
#   sudo bash deploy/install.sh
#
# Idempotent: rerunnable to refresh venv + reload systemd.

set -euo pipefail

INSTALL_DIR="/home/botuser/metis-v5"
USER_NAME="botuser"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ "$SRC_DIR" != "$INSTALL_DIR" ]; then
    echo "WARN: SRC_DIR=$SRC_DIR != INSTALL_DIR=$INSTALL_DIR"
    echo "      assuming you ran from the rsynced ~/metis-v5; continuing."
fi

echo "[1/7] system packages..."
apt-get update -qq
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip chrony rsync

echo "[2/7] ensure data/logs dirs..."
sudo -u "$USER_NAME" mkdir -p "$INSTALL_DIR/code/data" "$INSTALL_DIR/code/logs"

echo "[3/7] .env present?"
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo "  -> /home/$USER_NAME/metis-v5/.env not found."
    echo "     Copying from metis-f2 if available..."
    if [ -f "/home/$USER_NAME/metis-f2/.env" ]; then
        cp "/home/$USER_NAME/metis-f2/.env" "$INSTALL_DIR/.env"
        chown "$USER_NAME:$USER_NAME" "$INSTALL_DIR/.env"
        chmod 600 "$INSTALL_DIR/.env"
        echo "  -> copied. EDIT IT NOW (PAPER_MODE=true, PAPER_INITIAL_BALANCE_USDT=200, BYBIT_USE_TESTNET=false)"
    else
        cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
        chown "$USER_NAME:$USER_NAME" "$INSTALL_DIR/.env"
        chmod 600 "$INSTALL_DIR/.env"
        echo "  -> created from template. EDIT $INSTALL_DIR/.env"
    fi
fi

echo "[4/7] python venv..."
if [ ! -d "$INSTALL_DIR/venv" ]; then
    sudo -u "$USER_NAME" python3 -m venv "$INSTALL_DIR/venv"
fi
sudo -u "$USER_NAME" "$INSTALL_DIR/venv/bin/pip" install --upgrade pip wheel >/dev/null
sudo -u "$USER_NAME" "$INSTALL_DIR/venv/bin/pip" install -r "$INSTALL_DIR/code/requirements.txt"

echo "[5/7] systemd units..."
cp "$SRC_DIR/deploy/metis-v5.service" /etc/systemd/system/metis-v5.service
cp "$SRC_DIR/deploy/metis-v5-dashboard.service" /etc/systemd/system/metis-v5-dashboard.service
systemctl daemon-reload
systemctl enable metis-v5.service metis-v5-dashboard.service

echo "[6/7] chrony NTP..."
systemctl enable --now chrony 2>/dev/null || systemctl enable --now chronyd 2>/dev/null || true

echo "[7/7] check BTC trend bot status (will conflict)..."
if systemctl is-active --quiet metis-v4-bot.service; then
    echo "  -> metis-v4-bot.service (BTC SMA125 trend bot) is RUNNING."
    echo "     METIS v5 won't start while it's active (Conflicts= directive)."
    echo "     Stop with:   sudo systemctl stop metis-v4-bot"
fi

cat <<EOF

────────────────────────────────────────────────────────────
Install complete.

1. Edit secrets (if not already done):
     sudo -u $USER_NAME vi $INSTALL_DIR/.env
   Required:
     PAPER_MODE=true
     PAPER_INITIAL_BALANCE_USDT=200.0
     BYBIT_USE_TESTNET=false   # paper uses mainnet public; safer for live later
     GEMINI_API_KEY=...
   Optional:
     TELEGRAM_BOT_TOKEN=... / TELEGRAM_CHAT_ID=...

2. Verify event YAML freshness:
     sudo -u $USER_NAME vi $INSTALL_DIR/code/config/events.yaml
   Set last_updated_utc within 7 days.

3. (If running BTC trend bot) stop it first:
     sudo systemctl stop metis-v4-bot

4. Start v5 + dashboard:
     sudo systemctl start metis-v5
     sudo systemctl start metis-v5-dashboard

5. Tail logs:
     sudo journalctl -u metis-v5 -f
     sudo journalctl -u metis-v5-dashboard -f

6. Status:
     sudo systemctl status metis-v5 metis-v5-dashboard

7. Dashboard (from laptop, NOT on VM):
     gcloud compute ssh <vm-name> --zone=<zone> -- -L 8501:127.0.0.1:8501
   then http://localhost:8501
────────────────────────────────────────────────────────────
EOF
