"""Core settings module.

Hexagonal note: the core must not depend on concrete adapter implementations.
Previously this module imported `LoggingAdapter` (an infrastructure adapter)
directly, creating an inward dependency. We now provide only a `LoggingPort`
placeholder and a setter used by the composition root (web adapter) to inject
an implementation at startup. A lightweight NoOpLogger is used until then so
imports do not fail when modules call `logger.info` during initialization.
"""

from pathlib import Path

from pydantic import FilePath, HttpUrl, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings
from rich import print

from ump.core.interfaces.logging import LoggingPort
import logging

# using pydantic_settings to manage environment variables
# and do automatic type casting in a central place
class UmpSettings(BaseSettings):
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore"  # Ignoriere unbekannte Umgebungsvariablen
    }
    UMP_LOG_LEVEL: str = "INFO"
    UMP_PROVIDERS_FILE: FilePath = Path("providers.yaml")
    UMP_API_SERVER_URL: str = "http://localhost:3000"
    UMP_API_SERVER_WORKERS: int = 4
    UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL: int = 5
    UMP_REMOTE_JOB_TTW: int | None = None  # Time-to-wait timeout for remote jobs (seconds)
    # Job store adapter selection: "memory" (default, no DB required) or "postgres"
    UMP_JOB_STORE: str = "memory"
    # Async PostgreSQL DSN for SQLModel adapter, e.g.
    # postgresql+asyncpg://user:password@host:5432/dbname
    # Required when UMP_JOB_STORE=postgres; ignored otherwise.
    UMP_DATABASE_URL: str | None = None
    UMP_DATABASE_NAME: str = "ump"
    UMP_DATABASE_HOST: str = "postgres"
    UMP_DATABASE_PORT: int = 5432
    UMP_DATABASE_USER: str = "postgres"
    UMP_DATABASE_PASSWORD: SecretStr = SecretStr("postgres")
    UMP_GEOSERVER_URL: HttpUrl | None = HttpUrl("http://geoserver:8080/geoserver")
    UMP_GEOSERVER_DB_HOST: str = "postgis"
    UMP_GEOSERVER_DB_PORT: int = 5432
    UMP_GEOSERVER_DB_NAME: str = "ump"
    UMP_GEOSERVER_DB_USER: str = "ump"
    UMP_GEOSERVER_DB_PASSWORD: SecretStr = SecretStr("ump")
    # Internal Geoserver datastore configuration (used by Geoserver container for internal datastores)
    UMP_GEOSERVER_INTERNAL_DB_HOST: str = "geoserver-db"
    UMP_GEOSERVER_INTERNAL_DB_PORT: int = 5432
    UMP_GEOSERVER_WORKSPACE_NAME: str = "UMP"
    UMP_GEOSERVER_USER: str = "geoserver"
    UMP_GEOSERVER_PASSWORD: SecretStr = SecretStr("geoserver")
    UMP_GEOSERVER_CONNECTION_TIMEOUT: int = 60  # seconds
    UMP_JOB_DELETE_INTERVAL: int = 240  # minutes
    UMP_KEYCLOAK_URL: HttpUrl | None = HttpUrl("http://keycloak:8080/auth")
    UMP_KEYCLOAK_REALM: str = "UrbanModelPlatform"
    UMP_KEYCLOAK_CLIENT_ID: str = "ump-client"
    UMP_KEYCLOAK_USER: str = "admin"
    UMP_KEYCLOAK_PASSWORD: SecretStr = SecretStr("admin")
    UMP_API_SERVER_URL_PREFIX: str = "/"
    # Supported API versions (major.minor strings). Used to mount versioned routes like /v1.0/
    UMP_SUPPORTED_API_VERSIONS: list[str] = ["1.0"]
    # When enabled, replace external links in fetched processes with local API links
    UMP_REWRITE_REMOTE_LINKS: bool = True
    # When true, JobManager verifies remote results immediately for terminal success responses
    UMP_VERIFY_REMOTE_RESULTS: bool = True
    # If true, fetch each configured process individually via /processes/{id} instead
    # of fetching the bulk /processes list and filtering. This is slower for large
    # catalogs but ensures we get full descriptions even if the list endpoint omits
    # fields. Defaults to False for performance.
    UMP_PER_PROCESS_FETCH: bool = False
    # Landing page/site metadata
    UMP_SITE_TITLE: str = "Urban Model Platform"
    UMP_SITE_DESCRIPTION: str = "An OGC API Processes gateway for urban models."
    UMP_SITE_CONTACT: str = "maintainers@example.org"

    # Gunicorn default timeout is 30 seconds
    UMP_SERVER_TIMEOUT: int = 30

    @computed_field
    @property
    def UMP_GEOSERVER_URL_REST(self) -> HttpUrl:
        """Constructs the full URL for the GeoServer REST API"""
        return HttpUrl(str(self.UMP_GEOSERVER_URL) + "/rest")

    @computed_field
    @property
    def UMP_GEOSERVER_URL_WORKSPACE(self) -> HttpUrl:
        """Constructs the full URL for the GeoServer workspace"""
        return HttpUrl(str(self.UMP_GEOSERVER_URL) + "/rest/workspaces")

    def print_settings(self, logger: LoggingPort):
        """Prints the settings for debugging purposes"""
        logger.info("UMP Settings:")
        print(self)

    @field_validator("UMP_KEYCLOAK_URL", mode="before")
    def ensure_trailing_slash(cls, value: str) -> str:
        """Ensure UMP_KEYCLOAK_URL has a trailing slash."""
        if not value.endswith("/"):
            value += "/"
        return value


app_settings = UmpSettings()


class NoOpLogger(LoggingPort):
    def info(self, msg: str, *args):
        pass
    def warning(self, msg: str, *args):
        pass
    def error(self, msg: str, *args):
        pass
    def debug(self, msg: str, *args):
        pass


class DelegatingLogger(LoggingPort):
    """Indirection layer preventing early-import freeze.

    Modules may do `from ump.core.settings import logger` during import time.
    If we later replace the global with a concrete adapter those modules still
    hold the old object (NoOpLogger). This delegator keeps a stable reference
    while swapping underlying implementation when `set_logger` is called.
    """
    def __init__(self):
        self._delegate: LoggingPort = NoOpLogger()

    def set_delegate(self, delegate: LoggingPort):
        self._delegate = delegate

    def info(self, msg: str, *args):
        self._delegate.info(msg, *args)
    def warning(self, msg: str, *args):
        self._delegate.warning(msg, *args)
    def error(self, msg: str, *args):
        self._delegate.error(msg, *args)
    def debug(self, msg: str, *args):
        self._delegate.debug(msg, *args)


_delegating_logger = DelegatingLogger()
logger: LoggingPort = _delegating_logger


def set_logger(l: LoggingPort):
    """Inject a concrete logger adapter from the composition root.
    Safe for modules that imported `logger` early: the delegator pointer updates.
    """
    _delegating_logger.set_delegate(l)
    try:
        app_settings.print_settings(logger)
    except Exception:
        logging.getLogger("UMP").warning("Failed printing settings with injected logger")


def get_logger() -> LoggingPort:
    """Preferred access pattern for runtime modules to avoid early binding."""
    return logger