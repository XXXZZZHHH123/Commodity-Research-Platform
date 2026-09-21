#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BUNDLE_ROOT="$(cd "$APP_DIR/.." && pwd)"
WHEELHOUSE="$BUNDLE_ROOT/wheelhouse"
VERSION_FILE="$BUNDLE_ROOT/VERSION"
PLATFORM_FILE="$BUNDLE_ROOT/PLATFORM"

[[ -d "$WHEELHOUSE" ]] || { echo "missing wheelhouse: $WHEELHOUSE" >&2; exit 1; }
[[ -f "$VERSION_FILE" ]] || { echo "missing bundle VERSION" >&2; exit 1; }
[[ -f "$PLATFORM_FILE" ]] || { echo "missing bundle PLATFORM" >&2; exit 1; }
[[ "$(uname -s)" == "Linux" ]] || { echo "this bundle only supports Linux" >&2; exit 1; }
[[ "$(uname -m)" == "x86_64" ]] || { echo "this bundle only supports x86_64" >&2; exit 1; }

export GITHUB_WORKSPACE="$APP_DIR"
export WHEELHOUSE
export RELEASE_SHA="$(tr -d '[:space:]' < "$VERSION_FILE")"
export GITHUB_RUN_ID="offline-$(date -u +%Y%m%d%H%M%S)"
export GITHUB_RUN_ATTEMPT=1

exec bash "$APP_DIR/deploy/deploy.sh"
