#!/bin/bash
# =============================================================================
# KAGARIHI 会計Bot — GCP VM 作成スクリプト（ローカルで実行）
#
# 前提:
#   - Google Cloud SDK (gcloud) がインストール済み
#   - gcloud auth login / gcloud config set project PROJECT_ID 済み
#
# 使い方:
#   1. 下の変数を編集
#   2. bash deploy/gcp_vm_create.sh
# =============================================================================
set -euo pipefail

# ── 変数（必要に応じて変更） ─────────────────────────────────────────────────
PROJECT_ID="project-252ce045-7241-4267-8ec"  # gcloud projects list で確認
VM_NAME="kagarihi-bot"
ZONE="us-central1-a"                       # 無料枠対象: us-central1 / us-west1 / us-east1
MACHINE_TYPE="e2-micro"                    # 無料枠: e2-micro
DISK_SIZE="30GB"                           # 無料枠: 30GB standard
IMAGE_FAMILY="debian-12"
IMAGE_PROJECT="debian-cloud"
# ─────────────────────────────────────────────────────────────────────────────

echo "=== プロジェクト設定 ==="
gcloud config set project "$PROJECT_ID"

echo "=== Compute Engine API 有効化 ==="
gcloud services enable compute.googleapis.com

echo "=== VM 作成 ==="
gcloud compute instances create "$VM_NAME" \
  --zone="$ZONE" \
  --machine-type="$MACHINE_TYPE" \
  --image-family="$IMAGE_FAMILY" \
  --image-project="$IMAGE_PROJECT" \
  --boot-disk-size="$DISK_SIZE" \
  --boot-disk-type="pd-standard" \
  --tags="http-server,https-server" \
  --scopes="default"

echo "=== ファイアウォール: HTTP/HTTPS 許可 ==="
# 既存ルールがあればスキップ
gcloud compute firewall-rules create allow-http \
  --allow=tcp:80 \
  --target-tags=http-server \
  --description="Allow HTTP" 2>/dev/null || echo "allow-http はすでに存在します（スキップ）"

gcloud compute firewall-rules create allow-https \
  --allow=tcp:443 \
  --target-tags=https-server \
  --description="Allow HTTPS" 2>/dev/null || echo "allow-https はすでに存在します（スキップ）"

echo ""
echo "=== VM 情報 ==="
gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format="table(name, status, networkInterfaces[0].accessConfigs[0].natIP)"

EXTERNAL_IP=$(gcloud compute instances describe "$VM_NAME" --zone="$ZONE" \
  --format="value(networkInterfaces[0].accessConfigs[0].natIP)")

echo ""
echo "✅ VM 作成完了！"
echo ""
echo "外部IP: ${EXTERNAL_IP}"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "次のステップ:"
echo ""
echo "1. ドメインの DNS A レコードを以下に設定:"
echo "   ${EXTERNAL_IP}"
echo ""
echo "2. VM に SSH でログイン:"
echo "   gcloud compute ssh ${VM_NAME} --zone=${ZONE}"
echo ""
echo "3. VM 上でセットアップスクリプトを実行:"
echo "   curl -fsSL https://raw.githubusercontent.com/privatemkn4agtkt-source/KAGARIHI_bookkeeping/main/deploy/gcp_server_setup.sh | bash"
echo "   または git clone してから: bash deploy/gcp_server_setup.sh"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
