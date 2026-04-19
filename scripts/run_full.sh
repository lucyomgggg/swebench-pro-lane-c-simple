#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

if [ ! -x "$ROOT_DIR/.venv/bin/python" ]; then
  echo ".venv is missing. Run scripts/install_ubuntu.sh first." >&2
  exit 1
fi

if [ -f "$ROOT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

exec "$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/run_lane_c.py" --config "$ROOT_DIR/config.yaml" "$@"

