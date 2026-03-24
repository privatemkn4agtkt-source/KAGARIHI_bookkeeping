#!/bin/bash
# Cloudflare Tunnel 起動 & .env URL 自動更新スクリプト
# cloudflared が割り当てた trycloudflare.com URL を検出し、
# .env の DISCORD_REDIRECT_URI と DASHBOARD_URL を自動更新する。

set -euo pipefail

INSTALL_DIR="/opt/KAGARIHI_bookkeeping"
ENV_FILE="$INSTALL_DIR/.env"
LOG_FILE="/tmp/cloudflared-tunnel.log"
WAIT_SEC=60

echo "[tunnel] cloudflared を起動します..."

# 前回ログを削除
rm -f "$LOG_FILE"

# cloudflared をバックグラウンドで起動
cloudflared tunnel --url http://localhost:8000 2>"$LOG_FILE" &
CF_PID=$!

# URL が現れるまで最大 WAIT_SEC 秒待機
URL=""
for i in $(seq 1 "$WAIT_SEC"); do
    URL=$(grep -oP 'https://[a-z0-9-]+\.trycloudflare\.com' "$LOG_FILE" 2>/dev/null | head -1 || true)
    if [ -n "$URL" ]; then
        break
    fi
    sleep 1
done

if [ -z "$URL" ]; then
    echo "[tunnel] ERROR: URL の取得に失敗しました (${WAIT_SEC}秒タイムアウト)"
    kill "$CF_PID" 2>/dev/null || true
    exit 1
fi

echo "[tunnel] URL 取得: $URL"

# .env を更新
sed -i "s|DISCORD_REDIRECT_URI=.*|DISCORD_REDIRECT_URI=${URL}/auth/callback|" "$ENV_FILE"
sed -i "s|DASHBOARD_URL=.*|DASHBOARD_URL=${URL}|" "$ENV_FILE"

echo "[tunnel] .env 更新完了"

# Discord Developer Portal の OAuth2 Redirect URI を自動更新
DISCORD_TOKEN=$(grep -oP '(?<=^DISCORD_TOKEN=).+' "$ENV_FILE" || true)
if [ -n "$DISCORD_TOKEN" ]; then
    echo "[tunnel] Discord OAuth2 Redirect URI を更新します..."
    RESPONSE=$(curl -s -o /tmp/discord-patch.json -w "%{http_code}" \
        -X PATCH "https://discord.com/api/v10/applications/@me" \
        -H "Authorization: Bot ${DISCORD_TOKEN}" \
        -H "Content-Type: application/json" \
        -d "{\"redirect_uris\": [\"${URL}/auth/callback\", \"http://localhost:8000/auth/callback\"]}")
    if [ "$RESPONSE" = "200" ]; then
        echo "[tunnel] Discord Redirect URI 更新成功: ${URL}/auth/callback"
    else
        echo "[tunnel] WARNING: Discord Redirect URI 更新失敗 (HTTP ${RESPONSE})"
        cat /tmp/discord-patch.json || true
    fi
else
    echo "[tunnel] WARNING: DISCORD_TOKEN が取得できませんでした。Portal を手動更新してください。"
fi

# ダッシュボードを再起動して新しい URL を反映
systemctl restart bookkeeping-dashboard 2>/dev/null || true
echo "[tunnel] bookkeeping-dashboard を再起動しました"

# cloudflared が終了するまで待機（サービスの生存に使う）
wait "$CF_PID"
