#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUTPUT_DIR="${1:-$ROOT/dist}"
SHA="${RELEASE_SHA:-${GITHUB_SHA:-$(git -C "$ROOT" rev-parse HEAD)}}"
VERSION="${SHA:0:12}"
BUNDLE_NAME="commodity-research-linux-x64-py312-${VERSION}"
WORK_DIR="$(mktemp -d)"
BUNDLE_DIR="$WORK_DIR/$BUNDLE_NAME"

cleanup() {
  rm -rf "$WORK_DIR"
}
trap cleanup EXIT

command -v python >/dev/null || { echo "python is required" >&2; exit 1; }
command -v rsync >/dev/null || { echo "rsync is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || { echo "offline bundle must be built on Linux" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "offline bundle must be built on x86_64" >&2; exit 1; }
[[ "$(python -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')" == "3.12" ]] \
  || { echo "offline bundle must be built with Python 3.12" >&2; exit 1; }

mkdir -p "$OUTPUT_DIR" "$BUNDLE_DIR/app" "$BUNDLE_DIR/wheelhouse"

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude 'data/' \
  --exclude 'exports/' \
  --exclude 'logs/' \
  --exclude 'dist/' \
  "$ROOT/" "$BUNDLE_DIR/app/"

python -m pip download --disable-pip-version-check --only-binary=:all: \
  --dest "$BUNDLE_DIR/wheelhouse" \
  -r "$ROOT/requirements-production.txt" \
  'pip>=24' 'setuptools>=68' wheel

printf '%s\n' "$SHA" > "$BUNDLE_DIR/VERSION"
printf 'linux-x86_64\npython-3.12\n' > "$BUNDLE_DIR/PLATFORM"

tar -C "$WORK_DIR" -czf "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz" "$BUNDLE_NAME"
(
  cd "$OUTPUT_DIR"
  sha256sum "$BUNDLE_NAME.tar.gz" > "$BUNDLE_NAME.tar.gz.sha256"
)

printf 'built %s\n' "$OUTPUT_DIR/$BUNDLE_NAME.tar.gz"
