#!/usr/bin/env bash
# Production start command (Render): apply pending migrations, then start the API.
# Render start command: bash scripts/start.sh   (with the service root directory set to Backend)
set -euo pipefail
cd "$(dirname "$0")/.."

alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips='*'
