#!/usr/bin/env bash
set -Eeuo pipefail

umask 0002

APP_ROOT="${APP_ROOT:-/opt/commodity-research-platform}"
SHARED_ROOT="${SHARED_ROOT:-/var/lib/commodity-research-platform}"
CONFIG_FILE="${CONFIG_FILE:-/etc/commodity-research-platform/app.env}"
SERVICE_NAME="${SERVICE_NAME:-commodity-research-platform.service}"
TIMER_NAME="${TIMER_NAME:-commodity-research-platform-daily.timer}"
PYTHON_BIN="${PYTHON_BIN:-python3.12}"
HEALTH_URL="${HEALTH_URL:-http://127.0.0.1:8765/healthz}"
SOURCE_DIR="${GITHUB_WORKSPACE:-$(pwd)}"
SHA="${RELEASE_SHA:-${GITHUB_SHA:-manual}}"
RUN_ID="${GITHUB_RUN_ID:-$(date -u +%Y%m%d%H%M%S)}"
ATTEMPT="${GITHUB_RUN_ATTEMPT:-1}"
RELEASE_ID="${SHA:0:12}-${RUN_ID}-${ATTEMPT}"
RELEASE_DIR="${APP_ROOT}/releases/${RELEASE_ID}"
CURRENT_LINK="${APP_ROOT}/current"

fail() {
  printf 'deploy error: %s\n' "$*" >&2
  exit 1
}

command -v "$PYTHON_BIN" >/dev/null || fail "$PYTHON_BIN is not installed"
command -v rsync >/dev/null || fail "rsync is not installed"
command -v curl >/dev/null || fail "curl is not installed"
[[ -f "$CONFIG_FILE" ]] || fail "missing $CONFIG_FILE; run deploy/bootstrap.sh first"
[[ -f "$SOURCE_DIR/pyproject.toml" ]] || fail "$SOURCE_DIR is not a project checkout"

mkdir -p "$APP_ROOT/releases" "$SHARED_ROOT/data" "$SHARED_ROOT/exports"
[[ ! -e "$RELEASE_DIR" ]] || fail "release already exists: $RELEASE_DIR"
mkdir -p "$RELEASE_DIR"

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude 'exports/' \
  --exclude 'logs/' \
  "$SOURCE_DIR/" "$RELEASE_DIR/"

ln -s "$SHARED_ROOT/data" "$RELEASE_DIR/data"
ln -s "$SHARED_ROOT/exports" "$RELEASE_DIR/exports"

"$PYTHON_BIN" -m venv "$RELEASE_DIR/.venv"
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --upgrade pip
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check \
  -r "$RELEASE_DIR/requirements-production.txt"
"$RELEASE_DIR/.venv/bin/python" -m pip install --disable-pip-version-check --no-deps -e "$RELEASE_DIR"

set -a
# shellcheck disable=SC1090
. "$CONFIG_FILE"
set +a

(
  cd "$RELEASE_DIR"
  "$RELEASE_DIR/.venv/bin/python" -m tin.jobs init
  "$RELEASE_DIR/.venv/bin/python" -c "from tin.web.app import app; assert app.title"
)

PREVIOUS_RELEASE="$(readlink -f "$CURRENT_LINK" 2>/dev/null || true)"
NEXT_LINK="${APP_ROOT}/.current-${RELEASE_ID}"
ln -s "$RELEASE_DIR" "$NEXT_LINK"
mv -Tf "$NEXT_LINK" "$CURRENT_LINK"

rollback() {
  if [[ -n "$PREVIOUS_RELEASE" && -d "$PREVIOUS_RELEASE" ]]; then
    printf 'health check failed; rolling back to %s\n' "$PREVIOUS_RELEASE" >&2
    ln -sfn "$PREVIOUS_RELEASE" "$NEXT_LINK"
    mv -Tf "$NEXT_LINK" "$CURRENT_LINK"
    sudo systemctl restart "$SERVICE_NAME"
  fi
}

if ! sudo systemctl restart "$SERVICE_NAME"; then
  rollback
  fail "failed to restart $SERVICE_NAME"
fi
sudo systemctl start "$TIMER_NAME"

healthy=false
for _ in {1..20}; do
  if curl --fail --silent --show-error --max-time 3 "$HEALTH_URL" >/dev/null; then
    healthy=true
    break
  fi
  sleep 1
done

if [[ "$healthy" != true ]]; then
  rollback
  fail "health check failed: $HEALTH_URL"
fi

find "$APP_ROOT/releases" -mindepth 1 -maxdepth 1 -type d -printf '%T@ %p\n' \
  | sort -nr \
  | tail -n +6 \
  | cut -d' ' -f2- \
  | while IFS= read -r old_release; do
      [[ -z "$old_release" || "$old_release" == "$PREVIOUS_RELEASE" ]] || rm -rf "$old_release"
    done

printf 'deployed %s to %s\n' "$SHA" "$RELEASE_DIR"
