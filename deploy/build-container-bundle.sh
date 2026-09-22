#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT/dist}"
SHA="${RELEASE_SHA:-${GITHUB_SHA:-$(git -C "$ROOT" rev-parse HEAD)}}"
VERSION="${SHA:0:12}"
BUNDLE_NAME="commodity-research-container-linux-amd64-${VERSION}"
IMAGE="commodity-research-platform:${VERSION}"
WORK_DIR="$(mktemp -d)"
BUNDLE_DIR="$WORK_DIR/$BUNDLE_NAME"
SMOKE_APP="commodity-build-smoke-app-${VERSION}"

cleanup() {
  docker rm --force "$SMOKE_APP" >/dev/null 2>&1 || true
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

wait_for_healthy() {
  local container="$1"
  local status=""
  for _ in {1..30}; do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$container" 2>/dev/null || true)"
    [[ "$status" == "healthy" ]] && return 0
    [[ "$status" != "exited" && "$status" != "dead" ]] || break
    sleep 2
  done
  docker logs "$container" >&2 || true
  echo "$container did not become healthy (last status: $status)" >&2
  return 1
}

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || { echo "container bundle must be built on Linux" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "container bundle must be built on x86_64" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR" "$BUNDLE_DIR"
install -m 0755 "$ROOT/deploy/deploy-offline-release.sh" "$OUTPUT_DIR/deploy-release.sh"

docker build \
  --platform linux/amd64 \
  --build-arg "RELEASE_SHA=$SHA" \
  --tag "$IMAGE" \
  "$ROOT"
docker run --detach \
  --name "$SMOKE_APP" \
  --env TIN_DATABASE_URL=sqlite:////tmp/tin-container-smoke.db \
  --read-only \
  --tmpfs /tmp:rw,noexec,nosuid,size=64m \
  --security-opt no-new-privileges \
  --cap-drop ALL \
  "$IMAGE" \
  >/dev/null
wait_for_healthy "$SMOKE_APP"
docker rm --force "$SMOKE_APP" >/dev/null
docker image save --output "$BUNDLE_DIR/image.tar" "$IMAGE"

install -m 0755 "$ROOT/deploy/install-container.sh" "$BUNDLE_DIR/install.sh"
cp -R "$ROOT/deploy/container/systemd" "$BUNDLE_DIR/systemd"
mkdir -p "$BUNDLE_DIR/nginx"
install -m 0644 "$ROOT/deploy/nginx/commodity-research-platform.conf" "$BUNDLE_DIR/nginx/"
install -m 0644 "$ROOT/deploy/app.env.example" "$BUNDLE_DIR/app.env.example"
printf '%s\n' "$SHA" > "$BUNDLE_DIR/VERSION"
printf 'linux-amd64\ncontainer-runtime=docker\nreverse-proxy=host-nginx\n' > "$BUNDLE_DIR/PLATFORM"

tar -C "$WORK_DIR" -czf "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz" "$BUNDLE_NAME"
(
  cd "$OUTPUT_DIR"
  sha256sum "$BUNDLE_NAME.tar.gz" > "$BUNDLE_NAME.tar.gz.sha256"
)

printf 'built %s\n' "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz"
