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

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || { echo "container bundle must be built on Linux" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "container bundle must be built on x86_64" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR" "$BUNDLE_DIR"

docker build \
  --platform linux/amd64 \
  --build-arg "RELEASE_SHA=$SHA" \
  --tag "$IMAGE" \
  "$ROOT"
docker run --rm \
  --env TIN_DATABASE_URL=sqlite:////tmp/tin-container-smoke.db \
  "$IMAGE" \
  python -c "from tin.web.app import app; assert app.title"
docker image save --output "$BUNDLE_DIR/image.tar" "$IMAGE"

install -m 0755 "$ROOT/deploy/install-container.sh" "$BUNDLE_DIR/install.sh"
cp -R "$ROOT/deploy/container/systemd" "$BUNDLE_DIR/systemd"
install -m 0644 "$ROOT/deploy/app.env.example" "$BUNDLE_DIR/app.env.example"
printf '%s\n' "$SHA" > "$BUNDLE_DIR/VERSION"
printf 'linux-amd64\ncontainer-runtime=docker\n' > "$BUNDLE_DIR/PLATFORM"

tar -C "$WORK_DIR" -czf "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz" "$BUNDLE_NAME"
(
  cd "$OUTPUT_DIR"
  sha256sum "$BUNDLE_NAME.tar.gz" > "$BUNDLE_NAME.tar.gz.sha256"
)

printf 'built %s\n' "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz"
