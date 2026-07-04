"""UMP command-line entry points.

Registered as Poetry scripts in pyproject.toml:
  ump          → main()         start the API server
  ump-migrate  → migrate()      run Alembic migrations (upgrade head)
"""

import sys


def migrate() -> None:
    """Run ``alembic upgrade head`` using credentials from environment variables.

    Reads, in priority order:
      1. UMP_DATABASE_URL          full DSN (asyncpg prefix stripped automatically)
      2. UMP_DATABASE_HOST / PORT / USER / PASSWORD / NAME   individual vars
      3. alembic.ini placeholder   (will fail — only useful for dry-runs)
    """
    from alembic.config import Config
    from alembic.config import main as alembic_main

    # Pass any extra CLI args through (e.g. ump-migrate downgrade base)
    argv = sys.argv[1:] if len(sys.argv) > 1 else ["upgrade", "head"]
    alembic_main(argv=argv)


def main() -> None:
    from ump.main import main as _main

    _main()
