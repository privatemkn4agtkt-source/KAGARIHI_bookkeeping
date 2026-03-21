#!/bin/bash
# KAGARIHI 簿記Bot - Compute Engine セットアップスクリプト
# 新しくVMを作った後、このスクリプトをVM上で実行してください
# 使い方: bash setup.sh

set -e

REPO_URL="https://github.com/privatemkn4agtkt-source/KAGARIHI_bookkeeping.git"
INSTALL_DIR="/opt/KAGARIHI_bookkeeping"
SERVICE_USER="botuser"

echo "=== 1. パッケージ更新 ==="
sudo apt-get update -q
sudo apt-get install -y python3 python3-pip python3-venv git

echo "=== 2. ユーザー作成 ==="
id "$SERVICE_USER" &>/dev/null || sudo useradd -r -s /usr/sbin/nologin "$SERVICE_USER"

echo "=== 3. リポジトリ取得 ==="
if [ -d "$INSTALL_DIR" ]; then
    echo "既存のディレクトリを更新します..."
    sudo -u "$SERVICE_USER" git -C "$INSTALL_DIR" pull
else
    sudo git clone "$REPO_URL" "$INSTALL_DIR"
    sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
fi

echo "=== 4. Python 仮想環境 & 依存パッケージ ==="
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"

echo "=== 5. .env ファイルの設定 ==="
if [ ! -f "$INSTALL_DIR/.env" ]; then
    echo ""
    echo "⚠️  .env ファイルを作成します。以下を入力してください。"
    read -rp "DISCORD_TOKEN: " DISCORD_TOKEN
    read -rp "GUILD_ID (不要なら空Enter): " GUILD_ID

    sudo tee "$INSTALL_DIR/.env" > /dev/null <<EOF
DISCORD_TOKEN=${DISCORD_TOKEN}
GUILD_ID=${GUILD_ID}
DB_PATH=${INSTALL_DIR}/bookkeeping.db
EOF
    sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
    sudo chmod 600 "$INSTALL_DIR/.env"
    echo "✅ .env を作成しました"
else
    echo ".env はすでに存在します。スキップ。"
fi

echo "=== 6. systemd サービス登録 ==="
sudo cp "$INSTALL_DIR/deploy/bookkeeping-bot.service" /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable bookkeeping-bot
sudo systemctl restart bookkeeping-bot

echo ""
echo "=== 完了 ==="
sudo systemctl status bookkeeping-bot --no-pager
echo ""
echo "ログ確認: sudo journalctl -u bookkeeping-bot -f"
echo "再起動:   sudo systemctl restart bookkeeping-bot"
echo "停止:     sudo systemctl stop bookkeeping-bot"
