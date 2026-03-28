#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/termius_server_ed25519}"
DEPLOY_HOST="${DEPLOY_HOST:-root@101.43.25.136}"
REMOTE_PROJECT_DIR="${REMOTE_PROJECT_DIR:-/opt/Main_ChaoXingReserveSeat}"
REMOTE_STATIC_DIR="${REMOTE_STATIC_DIR:-/usr/share/nginx/seat_qianduan}"
SEAT_SERVICE="${SEAT_SERVICE:-seat-qianduan.service}"
DEPLOY_DRY_RUN="${DEPLOY_DRY_RUN:-0}"

LOCAL_QIANDUAN_DIR="$ROOT_DIR/qianduan"

if [[ ! -d "$LOCAL_QIANDUAN_DIR" ]]; then
  echo "Missing local qianduan directory: $LOCAL_QIANDUAN_DIR" >&2
  exit 1
fi

if [[ ! -f "$SSH_KEY" ]]; then
  echo "Missing SSH key: $SSH_KEY" >&2
  exit 1
fi

RSYNC_FLAGS=(-avz --delete)
if [[ "$DEPLOY_DRY_RUN" == "1" ]]; then
  RSYNC_FLAGS+=(--dry-run)
fi

SSH_RSH="ssh -o IdentitiesOnly=yes -i $SSH_KEY"

echo "[deploy_qianduan] sync local qianduan -> $DEPLOY_HOST:$REMOTE_PROJECT_DIR/qianduan/"
rsync "${RSYNC_FLAGS[@]}" -e "$SSH_RSH" \
  "$LOCAL_QIANDUAN_DIR/" \
  "$DEPLOY_HOST:$REMOTE_PROJECT_DIR/qianduan/"

if [[ "$DEPLOY_DRY_RUN" == "1" ]]; then
  echo "[deploy_qianduan] dry-run enabled, skip remote restart."
  exit 0
fi

echo "[deploy_qianduan] compile backend, sync static files, restart services"
ssh -o IdentitiesOnly=yes -i "$SSH_KEY" "$DEPLOY_HOST" "
set -euo pipefail
cd '$REMOTE_PROJECT_DIR'
python3 -m py_compile qianduan/server_api_example.py
rsync -av --delete \
  --include 'admin.html' \
  --include 'admin.js' \
  --include 'styles.css' \
  --exclude '*' \
  '$REMOTE_PROJECT_DIR/qianduan/' '$REMOTE_STATIC_DIR/'
systemctl restart '$SEAT_SERVICE'
nginx -t
systemctl reload nginx
"

echo "[deploy_qianduan] done."
