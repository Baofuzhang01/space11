#!/usr/bin/env bash

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"

SSH_KEY="${SSH_KEY:-$HOME/.ssh/termius_server_ed25519}"
DEPLOY_HOST="${DEPLOY_HOST:-root@101.43.25.136}"
REMOTE_PROJECT_DIR="${REMOTE_PROJECT_DIR:-/opt/Main_ChaoXingReserveSeat}"
REMOTE_STATIC_DIR="${REMOTE_STATIC_DIR:-/usr/share/nginx/seat_qianduan}"
SEAT_SERVICE="${SEAT_SERVICE:-seat-qianduan.service}"
DISPATCH_SERVICE="${DISPATCH_SERVICE:-server-dispatch.service}"
DEPLOY_DRY_RUN="${DEPLOY_DRY_RUN:-0}"

if [[ ! -f "$SSH_KEY" ]]; then
  echo "Missing SSH key: $SSH_KEY" >&2
  exit 1
fi

RSYNC_FLAGS=(-avz --delete)
if [[ "$DEPLOY_DRY_RUN" == "1" ]]; then
  RSYNC_FLAGS+=(--dry-run)
fi

SSH_RSH="ssh -o IdentitiesOnly=yes -i $SSH_KEY"

echo "[deploy_server] sync project -> $DEPLOY_HOST:$REMOTE_PROJECT_DIR/"
rsync "${RSYNC_FLAGS[@]}" \
  --exclude '.git' \
  --exclude '.idea' \
  --exclude '.venv' \
  --exclude '.DS_Store' \
  --exclude '.sync_shared_files_cache' \
  --exclude '__MACOSX' \
  --exclude '__pycache__' \
  --exclude '*.pyc' \
  --exclude 'html_debug' \
  --exclude 'logs' \
  --exclude 'server_runs' \
  --exclude 'server_store/*.sqlite3' \
  --exclude 'server_store/*.sqlite3-*' \
  --exclude 'workers/tongyi/node_modules' \
  --exclude 'worker2/node_modules' \
  -e "$SSH_RSH" \
  "$ROOT_DIR/" \
  "$DEPLOY_HOST:$REMOTE_PROJECT_DIR/"

if [[ "$DEPLOY_DRY_RUN" == "1" ]]; then
  echo "[deploy_server] dry-run enabled, skip remote restart."
  exit 0
fi

echo "[deploy_server] compile backend, sync static files, restart services"
ssh -o IdentitiesOnly=yes -i "$SSH_KEY" "$DEPLOY_HOST" "
set -euo pipefail
cd '$REMOTE_PROJECT_DIR'
python3 -m py_compile main.py server_dispatch.py qianduan/server_api_example.py
rsync -av --delete \
  --include 'admin.html' \
  --include 'admin.js' \
  --include 'styles.css' \
  --exclude '*' \
  '$REMOTE_PROJECT_DIR/qianduan/' '$REMOTE_STATIC_DIR/'
systemctl restart '$SEAT_SERVICE'
systemctl restart '$DISPATCH_SERVICE' || true
nginx -t
systemctl reload nginx
"

echo "[deploy_server] done."
