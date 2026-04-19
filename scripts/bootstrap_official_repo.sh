#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OFFICIAL_REPO_PATH="${SWE_BENCH_OFFICIAL_REPO_PATH:-$ROOT_DIR/vendor/SWE-bench_Pro-os}"

mkdir -p "$(dirname "$OFFICIAL_REPO_PATH")"

if [ -d "$OFFICIAL_REPO_PATH/.git" ]; then
  git -C "$OFFICIAL_REPO_PATH" pull --ff-only --depth 1
else
  git clone --depth 1 https://github.com/scaleapi/SWE-bench_Pro-os.git "$OFFICIAL_REPO_PATH"
fi

if [ -x "$ROOT_DIR/.venv/bin/pip" ]; then
  "$ROOT_DIR/.venv/bin/pip" install -r "$OFFICIAL_REPO_PATH/requirements.txt"
fi

echo "official repo ready at $OFFICIAL_REPO_PATH"
