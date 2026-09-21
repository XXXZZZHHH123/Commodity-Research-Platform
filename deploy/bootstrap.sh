#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "run this script as root" >&2; exit 1; }

DEPLOY_USER="${DEPLOY_USER:-${RUNNER_USER:-root}}"
APP_USER="${APP_USER:-commodity}"
APP_GROUP="${APP_GROUP:-commodity}"
APP_ROOT="${APP_ROOT:-/opt/commodity-research-platform}"
SHARED_ROOT="${SHARED_ROOT:-/var/lib/commodity-research-platform}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/commodity-research-platform}"
SYSTEMCTL="$(command -v systemctl)"
SOURCE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

id "$DEPLOY_USER" >/dev/null 2>&1 || useradd --create-home --shell /bin/bash "$DEPLOY_USER"

getent group "$APP_GROUP" >/dev/null || groupadd --system "$APP_GROUP"
id "$APP_USER" >/dev/null 2>&1 || useradd --system --gid "$APP_GROUP" --home-dir "$SHARED_ROOT" --shell /usr/sbin/nologin "$APP_USER"
if [[ "$DEPLOY_USER" != "root" ]]; then
  usermod -a -G "$APP_GROUP" "$DEPLOY_USER"
fi

install -d -o "$DEPLOY_USER" -g "$APP_GROUP" -m 2775 "$APP_ROOT" "$APP_ROOT/releases"
install -d -o "$APP_USER" -g "$APP_GROUP" -m 2775 "$SHARED_ROOT" "$SHARED_ROOT/data" "$SHARED_ROOT/exports"
install -d -o root -g "$APP_GROUP" -m 0750 "$CONFIG_ROOT"

if [[ ! -f "$CONFIG_ROOT/app.env" ]]; then
  install -o root -g "$APP_GROUP" -m 0640 "$SOURCE_ROOT/deploy/app.env.example" "$CONFIG_ROOT/app.env"
  echo "created $CONFIG_ROOT/app.env; review it before the first deployment"
fi

install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/commodity-research-platform.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/commodity-research-platform-daily.service" /etc/systemd/system/
install -o root -g root -m 0644 "$SOURCE_ROOT/deploy/systemd/commodity-research-platform-daily.timer" /etc/systemd/system/

if [[ "$DEPLOY_USER" != "root" ]]; then
  cat > /etc/sudoers.d/commodity-research-platform-deploy <<EOF
$DEPLOY_USER ALL=(root) NOPASSWD: $SYSTEMCTL restart commodity-research-platform.service
$DEPLOY_USER ALL=(root) NOPASSWD: $SYSTEMCTL start commodity-research-platform-daily.timer
EOF
  chmod 0440 /etc/sudoers.d/commodity-research-platform-deploy
  visudo -cf /etc/sudoers.d/commodity-research-platform-deploy >/dev/null
fi

systemctl daemon-reload
systemctl enable commodity-research-platform.service commodity-research-platform-daily.timer

cat <<EOF
Bootstrap complete.
1. Edit $CONFIG_ROOT/app.env.
2. Register the self-hosted runner when automatic native deployment is required.
3. Offline container deployment does not use this bootstrap script.
EOF
