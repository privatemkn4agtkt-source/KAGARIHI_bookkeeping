#!/bin/bash
# =============================================================================
# KAGARIHI 会計Bot — GCP VM セットアップスクリプト（VM 上で実行）
#
# 実行内容:
#   - Python / Nginx / Certbot のインストール
#   - リポジトリの取得・仮想環境構築
#   - .env の対話設定
#   - Discord Bot + Web ダッシュボード を systemd に登録・起動
#   - Nginx リバースプロキシ + Let's Encrypt SSL の設定
#
# 使い方（VM 上で）:
#   bash deploy/gcp_server_setup.sh
# =============================================================================
set -euo pipefail

REPO_URL="https://github.com/privatemkn4agtkt-source/KAGARIHI_bookkeeping.git"
INSTALL_DIR="/opt/KAGARIHI_bookkeeping"
SERVICE_USER="botuser"
BRANCH="main"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; NC='\033[0m'
info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
success() { echo -e "${GREEN}[OK]${NC}   $*"; }

# ─────────────────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════╗"
echo "║   KAGARIHI 会計Bot — GCP セットアップ           ║"
echo "╚══════════════════════════════════════════════════╝"
echo ""

# ── 1. システムパッケージ ──────────────────────────────────────────────────
info "1/8 パッケージ更新・インストール..."
sudo apt-get update -q
sudo apt-get install -y -q \
  python3 python3-pip python3-venv \
  git nginx certbot python3-certbot-nginx \
  curl ufw

# ── 2. ファイアウォール ────────────────────────────────────────────────────
info "2/8 ファイアウォール設定 (ufw)..."
sudo ufw --force enable
sudo ufw allow OpenSSH
sudo ufw allow 'Nginx Full'
success "ufw 設定完了"

# ── 3. サービスユーザー作成 ────────────────────────────────────────────────
info "3/8 サービスユーザー作成..."
id "$SERVICE_USER" &>/dev/null \
  && warn "$SERVICE_USER はすでに存在します（スキップ）" \
  || sudo useradd -r -m -s /usr/sbin/nologin "$SERVICE_USER"

# ── 4. リポジトリ取得 ──────────────────────────────────────────────────────
info "4/8 リポジトリ取得..."
if [ -d "$INSTALL_DIR/.git" ]; then
  warn "既存ディレクトリを更新します..."
  sudo git -C "$INSTALL_DIR" fetch origin
  sudo git -C "$INSTALL_DIR" checkout "$BRANCH"
  sudo git -C "$INSTALL_DIR" pull origin "$BRANCH"
else
  sudo git clone -b "$BRANCH" "$REPO_URL" "$INSTALL_DIR"
fi
sudo chown -R "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR"
success "リポジトリ: $INSTALL_DIR"

# ── 5. Python 仮想環境 ─────────────────────────────────────────────────────
info "5/8 Python 仮想環境・依存パッケージ..."
sudo -u "$SERVICE_USER" python3 -m venv "$INSTALL_DIR/venv"
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q --upgrade pip
sudo -u "$SERVICE_USER" "$INSTALL_DIR/venv/bin/pip" install -q -r "$INSTALL_DIR/requirements.txt"
success "仮想環境構築完了"

# ── 6. .env 設定 ───────────────────────────────────────────────────────────
info "6/8 .env 設定..."
if [ -f "$INSTALL_DIR/.env" ]; then
  warn ".env はすでに存在します（スキップ）"
else
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  .env の設定（Discord Developer Portal で確認）"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  read -rp "DISCORD_TOKEN (Bot Token): "           _TOKEN
  read -rp "CLIENT_ID (Application ID): "          _CLIENT_ID
  read -rp "GUILD_ID (省略可、空Enter でスキップ): " _GUILD_ID
  read -rp "DISCORD_CLIENT_SECRET (OAuth2 Secret): " _SECRET
  read -rp "ドメイン名 (例: dashboard.example.com): "  _DOMAIN
  read -rp "ALLOWED_DISCORD_IDS (カンマ区切りのID): "  _ALLOWED

  # SESSION_SECRET を自動生成
  _SESSION_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")

  sudo tee "$INSTALL_DIR/.env" > /dev/null <<EOF
# Discord Bot
DISCORD_TOKEN=${_TOKEN}
CLIENT_ID=${_CLIENT_ID}
GUILD_ID=${_GUILD_ID}

# Web ダッシュボード OAuth2
DISCORD_CLIENT_ID=${_CLIENT_ID}
DISCORD_CLIENT_SECRET=${_SECRET}
DISCORD_REDIRECT_URI=https://${_DOMAIN}/auth/callback
ALLOWED_DISCORD_IDS=${_ALLOWED}
SESSION_SECRET=${_SESSION_SECRET}

# 共通
DB_PATH=${INSTALL_DIR}/bookkeeping.db
DASHBOARD_PORT=8000
EOF
  sudo chown "$SERVICE_USER:$SERVICE_USER" "$INSTALL_DIR/.env"
  sudo chmod 600 "$INSTALL_DIR/.env"
  success ".env 作成完了"
fi

# ── 7. systemd サービス登録 ────────────────────────────────────────────────
info "7/8 systemd サービス登録・起動..."

# Bot サービス
sudo cp "$INSTALL_DIR/deploy/bookkeeping-bot.service" /etc/systemd/system/
# ダッシュボードサービス
sudo cp "$INSTALL_DIR/deploy/bookkeeping-dashboard.service" /etc/systemd/system/

sudo systemctl daemon-reload
sudo systemctl enable bookkeeping-bot bookkeeping-dashboard
sudo systemctl restart bookkeeping-bot bookkeeping-dashboard

success "Bot・ダッシュボード起動完了"

# ── 8. Nginx + Let's Encrypt SSL ──────────────────────────────────────────
info "8/8 Nginx + SSL 設定..."

# .env からドメインを取得
_DOMAIN=$(sudo grep DISCORD_REDIRECT_URI "$INSTALL_DIR/.env" \
  | sed 's|.*https://||;s|/auth.*||')

if [ -z "$_DOMAIN" ]; then
  warn "ドメインが取得できませんでした。Nginx 設定をスキップします。"
  warn "手動で deploy/nginx.conf.template を参考に設定してください。"
else
  # Nginx 設定を生成
  sudo sed "s/YOUR_DOMAIN/${_DOMAIN}/g" \
    "$INSTALL_DIR/deploy/nginx.conf.template" \
    | sudo tee /etc/nginx/sites-available/kagarihi > /dev/null

  sudo ln -sf /etc/nginx/sites-available/kagarihi /etc/nginx/sites-enabled/
  sudo rm -f /etc/nginx/sites-enabled/default
  sudo nginx -t
  sudo systemctl reload nginx

  # Let's Encrypt 証明書取得（Certbot）
  echo ""
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  echo "  Let's Encrypt SSL 証明書を取得します"
  echo "  ※ ドメインが既にこのサーバーのIPを向いている必要があります"
  echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
  read -rp "メールアドレス (証明書更新通知用): " _EMAIL
  sudo certbot --nginx -d "$_DOMAIN" --email "$_EMAIL" --agree-tos --non-interactive
  sudo systemctl enable certbot.timer
  success "SSL 証明書取得・自動更新設定完了"
fi

# ── 完了 ───────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════════╗"
echo "║  ✅  セットアップ完了！                                         ║"
echo "╚══════════════════════════════════════════════════════════════════╝"
echo ""
sudo systemctl status bookkeeping-bot --no-pager -l | head -8
echo ""
sudo systemctl status bookkeeping-dashboard --no-pager -l | head -8
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  ダッシュボード: https://${_DOMAIN:-your-domain}"
echo ""
echo "  ログ確認:"
echo "    sudo journalctl -u bookkeeping-bot -f"
echo "    sudo journalctl -u bookkeeping-dashboard -f"
echo ""
echo "  アップデート:"
echo "    bash ${INSTALL_DIR}/deploy/update.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
