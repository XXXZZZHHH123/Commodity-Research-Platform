#!/usr/bin/env bash
set -Eeuo pipefail

APP_IMAGE="commodity-research-platform"
CONTAINER_NAME="commodity-research-platform"
ARTIFACT_PREFIX="commodity-research-container-linux-amd64-"
SHARED_ROOT="${SHARED_ROOT:-/var/lib/commodity-research-platform}"
CONFIG_ROOT="${CONFIG_ROOT:-/etc/commodity-research-platform}"
SCRIPT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGING_DIR=""

fail() {
  printf 'deploy error: %s\n' "$*" >&2
  exit 1
}

cleanup_staging() {
  [[ -z "$STAGING_DIR" ]] || rm -rf -- "$STAGING_DIR"
}
trap cleanup_staging EXIT

[[ $EUID -eq 0 ]] || fail "run this script as root"
[[ $# -le 1 ]] || fail "usage: $0 [bundle.tar.gz]"

for command in docker sha256sum tar systemctl curl flock readlink; do
  command -v "$command" >/dev/null || fail "$command is required"
done
docker info >/dev/null 2>&1 || fail "docker daemon is not running"

if [[ $# -eq 1 ]]; then
  [[ -f "$1" ]] || fail "bundle does not exist: $1"
  ARCHIVE="$(readlink -f "$1")"
  DELIVERY_DIR="$(dirname "$ARCHIVE")"
else
  DELIVERY_DIR="$SCRIPT_ROOT"
  mapfile -t ARCHIVES < <(find "$DELIVERY_DIR" -maxdepth 1 -type f \
    -name "${ARTIFACT_PREFIX}*.tar.gz" -printf '%T@ %p\n' \
    | sort -nr | cut -d' ' -f2-)
  ARCHIVE="${ARCHIVES[0]:-}"
fi

[[ -n "${ARCHIVE:-}" && -f "$ARCHIVE" ]] || fail "no container bundle found"
ARCHIVE_NAME="$(basename "$ARCHIVE")"
[[ "$ARCHIVE_NAME" =~ ^${ARTIFACT_PREFIX}([0-9a-f]{12})\.tar\.gz$ ]] \
  || fail "unexpected bundle name: $ARCHIVE_NAME"
VERSION="${BASH_REMATCH[1]}"
CHECKSUM="$ARCHIVE.sha256"
[[ -f "$CHECKSUM" ]] || fail "missing checksum: $CHECKSUM"

exec 9>"$DELIVERY_DIR/.commodity-research-deploy.lock"
flock -n 9 || fail "another deployment is already running"

printf 'verifying %s\n' "$ARCHIVE_NAME"
(
  cd "$DELIVERY_DIR"
  sha256sum -c "$(basename "$CHECKSUM")"
)

BACKUP_ROOT="$SHARED_ROOT/backups"
STAMP="$(date +%Y%m%d-%H%M%S)"
DATABASE="$SHARED_ROOT/data/tin.db"
if [[ -f "$DATABASE" ]]; then
  install -d -o 10001 -g 10001 -m 0750 "$BACKUP_ROOT"
  DATABASE_BACKUP="$BACKUP_ROOT/tin-before-${VERSION}-${STAMP}.db"
  if [[ "$(docker inspect --format '{{.State.Running}}' "$CONTAINER_NAME" 2>/dev/null || true)" == "true" ]]; then
    docker exec --env "TIN_BACKUP_PATH=$DATABASE_BACKUP" "$CONTAINER_NAME" python -c \
      "import os, sqlite3; src=sqlite3.connect('/var/lib/commodity-research-platform/data/tin.db'); dst=sqlite3.connect(os.environ['TIN_BACKUP_PATH']); src.backup(dst); dst.close(); src.close()"
  else
    cp -a -- "$DATABASE" "$DATABASE_BACKUP"
  fi
  printf 'database backup: %s\n' "$DATABASE_BACKUP"
fi
if [[ -f "$CONFIG_ROOT/app.env" ]]; then
  install -d -o 10001 -g 10001 -m 0750 "$BACKUP_ROOT"
  install -m 0600 "$CONFIG_ROOT/app.env" "$BACKUP_ROOT/app.env-before-${VERSION}-${STAMP}"
fi

STAGING_DIR="$(mktemp -d "$DELIVERY_DIR/.commodity-release.XXXXXX")"
tar -xzf "$ARCHIVE" -C "$STAGING_DIR"
BUNDLE_DIR="$STAGING_DIR/${ARCHIVE_NAME%.tar.gz}"
[[ -x "$BUNDLE_DIR/install.sh" ]] || fail "bundle does not contain install.sh"

printf 'deploying release %s\n' "$VERSION"
bash "$BUNDLE_DIR/install.sh"

curl --fail --silent --show-error http://127.0.0.1:8765/healthz >/dev/null \
  || fail "application health check failed after installation"
systemctl is-active --quiet commodity-research-platform.service \
  || fail "application service is not active after installation"
systemctl is-active --quiet commodity-research-platform-daily.timer \
  || fail "daily timer is not active after installation"

CURRENT_ID="$(docker image inspect --format '{{.Id}}' "$APP_IMAGE:current")"
PREVIOUS_ID="$(docker image inspect --format '{{.Id}}' "$APP_IMAGE:previous" 2>/dev/null || true)"

# Drop immutable release tags after current/previous are established. This keeps
# exactly the two rollback roles without accumulating one tag per deployment.
while read -r repository tag; do
  [[ "$repository" == "$APP_IMAGE" ]] || continue
  [[ "$tag" == "current" || "$tag" == "previous" ]] && continue
  docker image rm "$repository:$tag" >/dev/null || \
    printf 'warning: could not remove image tag %s:%s\n' "$repository" "$tag" >&2
done < <(docker image ls --format '{{.Repository}} {{.Tag}}')

# Retagging previous leaves the former rollback image dangling. Labels let us
# remove only this application's old images without touching other projects.
while read -r image_id; do
  [[ -n "$image_id" ]] || continue
  [[ "$image_id" == "$CURRENT_ID" || "$image_id" == "$PREVIOUS_ID" ]] && continue
  docker image rm "$image_id" >/dev/null || \
    printf 'warning: could not remove old application image %s\n' "$image_id" >&2
done < <(docker image ls --all --quiet --no-trunc \
  --filter "label=org.opencontainers.image.title=Commodity Research Platform" | sort -u)

shopt -s nullglob
for path in \
  "$DELIVERY_DIR"/${ARTIFACT_PREFIX}*.tar.gz \
  "$DELIVERY_DIR"/${ARTIFACT_PREFIX}*.tar.gz.sha256; do
  [[ "$path" == "$ARCHIVE" || "$path" == "$CHECKSUM" ]] || rm -f -- "$path"
done
for path in "$DELIVERY_DIR"/${ARTIFACT_PREFIX}*/; do
  [[ -d "$path" ]] && rm -rf -- "$path"
done
shopt -u nullglob

prune_backups() {
  local pattern="$1"
  local -a files=()
  local index
  mapfile -t files < <(find "$BACKUP_ROOT" -maxdepth 1 -type f -name "$pattern" \
    -printf '%T@ %p\n' | sort -nr | cut -d' ' -f2-)
  for ((index = 3; index < ${#files[@]}; index++)); do
    rm -f -- "${files[$index]}"
  done
}
if [[ -d "$BACKUP_ROOT" ]]; then
  prune_backups 'tin-before-*.db'
  prune_backups 'app.env-before-*'
fi

printf 'deployment complete: %s\n' "$VERSION"
printf 'retained artifacts: %s and %s\n' "$ARCHIVE" "$CHECKSUM"
printf 'retained images:\n'
docker image ls "$APP_IMAGE" --format '  {{.Repository}}:{{.Tag}} {{.ID}} {{.Size}}'
