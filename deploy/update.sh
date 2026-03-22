#!/bin/bash
# コードを更新して Bot + ダッシュボードを再起動するスクリプト
# 使い方: bash deploy/update.sh

set -euo pipefail
INSTALL_DIR="/opt/KAGARIHI_bookkeeping"
SERVICE_USER="botuser"

echo "=== 1. コード更新 ==="
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull

echo "=== 2. 依存パッケージ更新 ==="
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "=== 3. systemd リロード（サービスファイル変更に対応） ==="
sudo cp "$INSTALL_DIR/deploy/bookkeeping-bot.service"       /etc/systemd/system/
sudo cp "$INSTALL_DIR/deploy/bookkeeping-dashboard.service" /etc/systemd/system/
sudo systemctl daemon-reload

echo "=== 4. Bot 再起動 ==="
sudo systemctl restart bookkeeping-bot
sudo systemctl status bookkeeping-bot --no-pager -l | head -6

echo "=== 5. ダッシュボード再起動 ==="
sudo systemctl restart bookkeeping-dashboard
sudo systemctl status bookkeeping-dashboard --no-pager -l | head -6

echo ""
echo "✅ 更新完了"
