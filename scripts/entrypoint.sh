#!/bin/bash
set -e

export UMP_SERVER_TIMEOUT="${UMP_SERVER_TIMEOUT:-30}"

# Set the correct Python path and Flask app
export FLASK_APP="ump.main:app"
export PATH="/app/.venv/bin:$PATH"

echo "Running database migrations..."
/app/.venv/bin/flask db upgrade

echo "Running API Server in production mode."
UMP_API_SERVER_WORKERS="${UMP_API_SERVER_WORKERS:-1}"
echo "Running gunicorn with ${UMP_API_SERVER_WORKERS} workers."
exec /app/.venv/bin/gunicorn \
    --workers=$UMP_API_SERVER_WORKERS \
    --timeout=$UMP_SERVER_TIMEOUT \
    --bind=0.0.0.0:5000 \
    ump.main:app
