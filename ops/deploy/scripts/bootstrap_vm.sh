#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/restaurant-split-bot}"
SERVICE_NAME="${SERVICE_NAME:-restaurant-split-bot}"
DOMAIN="${DOMAIN:-bot.example.com}"
RUN_USER="${RUN_USER:-ubuntu}"

echo "Installing system packages..."
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip git nginx certbot python3-certbot-nginx

echo "Preparing app directory..."
sudo mkdir -p "$APP_DIR"
sudo chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

echo "Installing systemd unit..."
sudo cp "$APP_DIR/ops/deploy/systemd/restaurant-split-bot.service" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|^User=.*|User=${RUN_USER}|" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|^Group=.*|Group=${RUN_USER}|" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|^WorkingDirectory=.*|WorkingDirectory=${APP_DIR}|" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|^EnvironmentFile=.*|EnvironmentFile=${APP_DIR}/.env|" "/etc/systemd/system/${SERVICE_NAME}.service"
sudo sed -i "s|^ExecStart=.*|ExecStart=${APP_DIR}/.venv/bin/python -m app.webhook|" "/etc/systemd/system/${SERVICE_NAME}.service"

echo "Installing nginx config..."
sudo cp "$APP_DIR/ops/deploy/nginx/restaurant-split-bot.conf" "/etc/nginx/sites-available/${SERVICE_NAME}.conf"
sudo sed -i "s|server_name .*;|server_name ${DOMAIN};|" "/etc/nginx/sites-available/${SERVICE_NAME}.conf"
sudo ln -sf "/etc/nginx/sites-available/${SERVICE_NAME}.conf" "/etc/nginx/sites-enabled/${SERVICE_NAME}.conf"
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx

echo "Enabling service..."
sudo systemctl daemon-reload
sudo systemctl enable "${SERVICE_NAME}"

echo "Bootstrap done."
echo "Next:"
echo "1) Fill ${APP_DIR}/.env"
echo "2) Run: ${APP_DIR}/ops/deploy/scripts/deploy_vm.sh"
echo "3) Issue TLS cert: sudo certbot --nginx -d ${DOMAIN}"
