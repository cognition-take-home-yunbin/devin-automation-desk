#!/usr/bin/env bash
# Isolated test run: backend pytest + frontend typecheck + production build.
set -euo pipefail
cd /app

export APP_MODE="${APP_MODE:-simulation}"
export DATABASE_PATH="${DATABASE_PATH:-/tmp/repairdesk-test.sqlite}"
rm -f "$DATABASE_PATH"

echo "== backend tests (pytest) =="
python -m pytest tests -q

echo "== frontend typecheck =="
(cd frontend && npx tsc --noEmit)

echo "== frontend production build =="
(cd frontend && npm run build)

echo "all checks passed"
