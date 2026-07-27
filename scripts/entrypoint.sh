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
  # Multi-worker: gunicorn manages process lifecycle, uvicorn handles ASGI.
  # log-level is forwarded so gunicorn/uvicorn worker stderr reflects the
  # configured level; application logging is owned by configure_logging in
  # asgi.py and is not affected by this flag.
  exec gunicorn \
    --workers "$WORKERS" \
    --worker-class uvicorn.workers.UvicornWorker \
    --bind "${HOST}:${PORT}" \
    --log-level "${UMP_LOG_LEVEL:-info}" \
    ump.asgi:app
else
  # Single worker: invoke the `ump` Poetry entry point instead of uvicorn
  # directly.  The key difference is that `ump` calls uvicorn.run() with
  # log_config=None, which tells uvicorn to skip its own dictConfig call.
  # Without this, uvicorn's default log config replaces the handlers that
  # configure_logging already installed in asgi.py, causing application-level
  # DEBUG messages to be silently dropped even when UMP_LOG_LEVEL=debug.
  exec ump
fi
