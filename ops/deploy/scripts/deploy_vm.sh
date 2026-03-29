#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/restaurant-split-bot}"
BRANCH="${BRANCH:-main}"
SERVICE_NAME="${SERVICE_NAME:-restaurant-split-bot}"

if [[ ! -d "$APP_DIR/.git" ]]; then
  echo "Repo not found in $APP_DIR"
  exit 1
fi

cd "$APP_DIR"

echo "Updating repo..."
git fetch --all --prune
git checkout "$BRANCH"
git pull --ff-only origin "$BRANCH"

echo "Installing dependencies..."
python3 -m venv .venv
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r requirements.txt

echo "Restarting service..."
sudo systemctl restart "$SERVICE_NAME"
sudo systemctl status "$SERVICE_NAME" --no-pager --lines=30

echo "Done."
