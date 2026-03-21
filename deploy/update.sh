#!/bin/bash
# コードを更新してBotを再起動するスクリプト
# 使い方: bash update.sh

set -e
INSTALL_DIR="/opt/KAGARIHI_bookkeeping"
SERVICE_USER="botuser"

echo "=== コード更新 ==="
sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull

echo "=== 依存パッケージ更新 ==="
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "=== Bot 再起動 ==="
sudo systemctl restart bookkeeping-bot
sudo systemctl status bookkeeping-bot --no-pager
