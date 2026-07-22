# main.py — CLI entry point for development.
#
# For production deployments use ump.asgi:app directly:
#   uvicorn ump.asgi:app --host 0.0.0.0 --port 8000 --workers 4
#   gunicorn -k uvicorn.workers.UvicornWorker -w 4 ump.asgi:app
#
# All adapter wiring lives in ump.asgi (the single composition root).
# This file only exists to provide the `ump` CLI command and to allow
# `python -m ump.main` for quick local starts.

import uvicorn

from ump.core.settings import app_settings


def main() -> None:
    """Start uvicorn pointing at the ASGI module (single composition root)."""
    uvicorn.run(
        "ump.asgi:app",
        host=app_settings.UMP_API_SERVER_HOST,
        port=app_settings.UMP_API_SERVER_PORT,
        workers=app_settings.UMP_API_SERVER_WORKERS,
        log_config=None,
        log_level=str(app_settings.UMP_LOG_LEVEL).lower(),
    )


if __name__ == "__main__":
    main()

