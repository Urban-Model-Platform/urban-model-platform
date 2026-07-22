#!/bin/bash
set -e

export PATH="/app/.venv/bin:$PATH"

echo "Running database migrations..."
ump-migrate upgrade head

echo "Starting API server..."
WORKERS="${UMP_API_SERVER_WORKERS:-1}"
HOST="${UMP_API_SERVER_HOST:-0.0.0.0}"
PORT="${UMP_API_SERVER_PORT:-8000}"

echo "uvicorn workers=${WORKERS} bind=${HOST}:${PORT}"

if [ "$WORKERS" -gt 1 ]; then
  # Multi-worker: gunicorn manages process lifecycle, uvicorn handles ASGI
  exec gunicorn \
    --workers "$WORKERS" \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind "${HOST}:${PORT}" \
    --log-level "${UMP_LOG_LEVEL:-info}" \
    ump.asgi:app
else
  # Single worker: uvicorn directly (simpler, same behaviour)
  exec uvicorn ump.asgi:app \
    --host "$HOST" \
    --port "$PORT" \
    --log-level "${UMP_LOG_LEVEL:-info}"
fi
