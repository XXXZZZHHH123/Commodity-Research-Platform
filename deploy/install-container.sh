#!/usr/bin/env bash
set -Eeuo pipefail

[[ $EUID -eq 0 ]] || { echo "run this script as root" >&2; exit 1; }

BUNDLE_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION_FILE="$BUNDLE_ROOT/VERSION"
IMAGE_ARCHIVE="$BUNDLE_ROOT/image.tar"
SYSTEMD_DIR="$BUNDLE_ROOT/systemd"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/commodity-research-platform}"
SHARED_ROOT="${SHARED_ROOT:-/var/lib/commodity-research-platform}"
CONTAINER_UID="${CONTAINER_UID:-10001}"
CONTAINER_GID="${CONTAINER_GID:-10001}"

fail() {
  printf 'container install error: %s\n' "$*" >&2
  exit 1
}

command -v docker >/dev/null || fail "docker is not installed"
DOCKER_BIN="$(command -v docker)"
docker info >/dev/null 2>&1 || fail "docker daemon is not running"
[[ -f "$VERSION_FILE" ]] || fail "missing bundle VERSION"
[[ -f "$IMAGE_ARCHIVE" ]] || fail "missing image archive: $IMAGE_ARCHIVE"
[[ -d "$SYSTEMD_DIR" ]] || fail "missing systemd units"
[[ "$(uname -s)" == "Linux" ]] || fail "this bundle only supports Linux"
[[ "$(uname -m)" == "x86_64" ]] || fail "this bundle only supports x86_64"

HOST_TIMEZONE="$(timedatectl show --property=Timezone --value 2>/dev/null || true)"
if [[ -n "$HOST_TIMEZONE" && "$HOST_TIMEZONE" != "Asia/Shanghai" ]]; then
  printf 'warning: host timezone is %s; daily timers use host-local 08:00 and 16:30\n' "$HOST_TIMEZONE" >&2
fi

SHA="$(tr -d '[:space:]' < "$VERSION_FILE")"
VERSION="${SHA:0:12}"
VERSIONED_IMAGE="commodity-research-platform:$VERSION"
CURRENT_IMAGE="commodity-research-platform:current"
PREVIOUS_IMAGE="commodity-research-platform:previous"

install -d -o root -g root -m 0750 "$CONFIG_ROOT"
install -d -o "$CONTAINER_UID" -g "$CONTAINER_GID" -m 0750 \
  "$SHARED_ROOT" "$SHARED_ROOT/data" "$SHARED_ROOT/exports"
chown -R "$CONTAINER_UID:$CONTAINER_GID" "$SHARED_ROOT/data" "$SHARED_ROOT/exports"

if [[ ! -f "$CONFIG_ROOT/app.env" ]]; then
  install -o root -g root -m 0600 "$BUNDLE_ROOT/app.env.example" "$CONFIG_ROOT/app.env"
  echo "created $CONFIG_ROOT/app.env; review it before using production data"
fi

docker image load --input "$IMAGE_ARCHIVE"
docker image inspect "$VERSIONED_IMAGE" >/dev/null 2>&1 \
  || fail "archive did not contain expected image $VERSIONED_IMAGE"

HAD_PREVIOUS=false
if docker image inspect "$CURRENT_IMAGE" >/dev/null 2>&1; then
  docker image tag "$CURRENT_IMAGE" "$PREVIOUS_IMAGE"
  HAD_PREVIOUS=true
fi
docker image tag "$VERSIONED_IMAGE" "$CURRENT_IMAGE"

systemctl stop commodity-research-platform-daily.timer >/dev/null 2>&1 || true
systemctl stop commodity-research-platform.service >/dev/null 2>&1 || true

for unit in \
  commodity-research-platform.service \
  commodity-research-platform-daily.service \
  commodity-research-platform-daily.timer; do
  sed "s|@DOCKER_BIN@|$DOCKER_BIN|g" "$SYSTEMD_DIR/$unit" > "/etc/systemd/system/$unit"
  chmod 0644 "/etc/systemd/system/$unit"
done

systemctl daemon-reload
systemctl enable commodity-research-platform.service commodity-research-platform-daily.timer

rollback() {
  if [[ "$HAD_PREVIOUS" == true ]]; then
    echo "health check failed; restoring previous container image" >&2
    docker image tag "$PREVIOUS_IMAGE" "$CURRENT_IMAGE"
    systemctl restart commodity-research-platform.service || true
    systemctl start commodity-research-platform-daily.timer || true
  fi
}

if ! systemctl restart commodity-research-platform.service; then
  rollback
  fail "failed to start commodity-research-platform.service"
fi

app_healthy=false
for _ in {1..30}; do
  status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' commodity-research-platform 2>/dev/null || true)"
  if [[ "$status" == "healthy" ]]; then
    app_healthy=true
    break
  fi
  [[ "$status" != "exited" && "$status" != "dead" ]] || break
  sleep 2
done

if [[ "$app_healthy" != true ]]; then
  docker logs --tail 100 commodity-research-platform >&2 || true
  rollback
  fail "application container health check failed"
fi

systemctl start commodity-research-platform-daily.timer

printf 'deployed %s as %s\n' "$SHA" "$CURRENT_IMAGE"
printf 'application is available to the host at http://127.0.0.1:8765\n'
