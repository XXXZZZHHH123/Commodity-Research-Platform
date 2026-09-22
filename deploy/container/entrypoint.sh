#!/usr/bin/env bash
set -Eeuo pipefail

python -m tin.jobs init
exec "$@"
