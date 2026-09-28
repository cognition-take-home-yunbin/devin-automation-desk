#!/usr/bin/env bash
# Optional Playwright smoke against a running simulation desk on :8000.
# Not part of scripts/test.sh / Docker test image (Chromium is opt-in).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/frontend"

if ! curl -sf http://127.0.0.1:8000/healthz >/dev/null; then
  echo "error: nothing healthy at http://127.0.0.1:8000/healthz" >&2
  echo "start simulation first: docker compose up --build -d" >&2
  exit 1
fi

MODE="$(curl -sf http://127.0.0.1:8000/healthz | python3 -c 'import sys,json; print(json.load(sys.stdin).get("mode",""))')"
if [[ "$MODE" != "simulation" ]]; then
  echo "error: Playwright smoke expects APP_MODE=simulation (got mode=$MODE)" >&2
  exit 1
fi

npm ci --no-audit --no-fund
npx playwright install chromium
PLAYWRIGHT_BASE_URL="${PLAYWRIGHT_BASE_URL:-http://127.0.0.1:8000}" npx playwright test
