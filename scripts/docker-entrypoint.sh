#!/bin/sh
# Start the app (no database, no migrations: data lives in JSON files).
set -eu

mkdir -p "${DATA_PATH:-/app/storage/data}" /app/storage/uploads 2>/dev/null || true

if [ "$#" -eq 0 ]; then
  # One worker on purpose: the store, scheduler and Telegram bot live in the app process.
  # PORT is set by Render (default 10000); 8000 otherwise.
  set -- uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --workers 1 \
    --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" \
    --timeout-graceful-shutdown 40 --no-server-header
fi

exec "$@"
