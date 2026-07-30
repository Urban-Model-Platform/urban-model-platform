"""Core settings module.

Hexagonal note: the core must not depend on concrete adapter implementations.
Previously this module imported `LoggingAdapter` (an infrastructure adapter)
directly, creating an inward dependency. We now provide only a `LoggingPort`
placeholder and a setter used by the composition root (web adapter) to inject
an implementation at startup. A lightweight NoOpLogger is used until then so
imports do not fail when modules call `logger.info` during initialization.
"""

import logging
from pathlib import Path

from pydantic import FilePath, HttpUrl, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings
from rich import print

from ump.core.interfaces.logging import LoggingPort


# using pydantic_settings to manage environment variables
# and do automatic type casting in a central place
class UmpSettings(BaseSettings):
    model_config = {
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "case_sensitive": True,
        "extra": "ignore",  # Ignoriere unbekannte Umgebungsvariablen
    }
    UMP_LOG_LEVEL: str = "INFO"
    UMP_PROVIDERS_FILE: FilePath = Path("providers.yaml")
    UMP_API_SERVER_URL: str = "http://localhost:8000/"
    UMP_API_SERVER_HOST: str = "0.0.0.0"
    UMP_API_SERVER_PORT: int = 8000
    UMP_API_SERVER_WORKERS: int = 1
    UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL: int = 5
    UMP_REMOTE_JOB_TTW: int | None = (
        None  # Time-to-wait timeout for remote jobs (seconds)
    )
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
    # ---- to be overhauled -----
    # UMP_GEOSERVER_URL: HttpUrl | None = HttpUrl("http://geoserver:8080/geoserver")
    # UMP_GEOSERVER_DB_HOST: str = "postgis"
    # UMP_GEOSERVER_DB_PORT: int = 5432
    # UMP_GEOSERVER_DB_NAME: str = "ump"
    # UMP_GEOSERVER_DB_USER: str = "ump"
    # UMP_GEOSERVER_DB_PASSWORD: SecretStr = SecretStr("ump")
    # Internal Geoserver datastore configuration (used by Geoserver container for internal datastores)
    # UMP_GEOSERVER_INTERNAL_DB_HOST: str = "geoserver-db"
    # UMP_GEOSERVER_INTERNAL_DB_PORT: int = 5432
    # UMP_GEOSERVER_WORKSPACE_NAME: str = "UMP"
    # UMP_GEOSERVER_USER: str = "geoserver"
    # UMP_GEOSERVER_PASSWORD: SecretStr = SecretStr("geoserver")
    # UMP_GEOSERVER_CONNECTION_TIMEOUT: int = 60  # seconds
    # ---------------------------
    UMP_JOB_DELETE_INTERVAL: int = 240  # minutes
    # jwt-based user-facing auth
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

    # ── JWT / OIDC authentication (Feature IV) ────────────────────────────────
    # Master switch.  Set to false in development to bypass all auth checks.
    UMP_AUTH_ENABLED: bool = False
    # JWKS endpoint from which UMP fetches public keys for offline token validation.
    # Example (Keycloak): https://keycloak:8080/auth/realms/UMP/protocol/openid-connect/certs
    # Example (generic):  https://idp.example.com/.well-known/jwks.json
    UMP_JWKS_URL: str | None = None
    # Expected 'iss' claim — must exactly match the issuer in every token.
    # Example (Keycloak): https://keycloak:8080/auth/realms/UrbanModelPlatform
    UMP_JWT_ISSUER: str | None = None
    # Expected 'aud' claim — must be present in every token.
    # Typically the Keycloak client-id or the API's own identifier.
    UMP_JWT_AUDIENCE: str | None = None
    # Comma-separated dot-notation paths to role arrays inside the JWT payload.
    # UMP walks each path and merges the results into a flat role list.
    #   Keycloak: realm_access.roles,resource_access.ump-client.roles
    #   Azure AD: roles
    #   Auth0:    https://myapp.example.com/roles
    UMP_JWT_ROLES_CLAIMS: str = "realm_access.roles"
    # How long (seconds) to cache the fetched JWKS public keys.
    # On cache miss or unknown 'kid', UMP re-fetches immediately (key-rotation defence).
    UMP_JWKS_CACHE_TTL_SECONDS: int = 3600
    # When true, GET /processes and GET /processes/{id} require no token.
    # Per-process anonymous access is still controlled by providers.yaml anonymous-access.
    UMP_PUBLIC_PROCESSES: bool = False

    # ── Process ID format ─────────────────────────────────────────────────────
    # Character used to separate provider prefix from process id in all external
    # and internal representations, e.g. ``fair2adapt:pluvial-flood-risk``.
    # Default ":" is allowed in URL path segments (RFC 3986 §3.3 pchar).
    # Operators may choose a different character (e.g. "-") for cleaner URLs.
    # Changing this on an existing deployment requires updating stored process_id
    # values in the jobs table (a data migration).
    UMP_PROCESS_ID_SEPARATOR: str = ":"

    # ── CORS ─────────────────────────────────────────────────────────────────
    # Comma-separated list of origins that browsers are allowed to call UMP
    # from.  An empty list (the default) means no CORS headers are emitted —
    # only same-origin browser calls work.  Use ["*"] to allow any origin
    # (appropriate only when every endpoint is protected by token auth and you
    # accept that any website can issue requests on behalf of a logged-in user).
    #
    # Production recommendation: list only the known frontend origin(s).
    #   UMP_CORS_ORIGINS=["https://app.example.com"]
    #
    # Note: credentials (cookies / Authorization headers) are intentionally
    # excluded from CORS grants here — UMP uses Bearer tokens passed explicitly
    # by the client, so allow_credentials stays False regardless of this list.
    UMP_CORS_ORIGINS: list[str] = []

    # ── Feature V: Result storage (ldproxy) ───────────────────────────────────────
    # Public base URL of the ldproxy ump-results service, e.g.
    # https://geodata.example.com/ump-results
    # Required when any process uses result-storage: ldproxy.
    UMP_RESULTSTORE_LDPROXY_BASE_URL: str | None = None

    # Path to the ldproxy store root on the shared filesystem (Azure File Share
    # in production, a local directory in development).  GeoPackage files and,
    # when the filesystem backend is used, entity YAML files are written here.
    # Required when any process uses result-storage: ldproxy.
    UMP_RESULTSTORE_LDPROXY_ROOTPATH: str | None = None

    # EPSG code for the default native CRS used in generated ldproxy provider
    # entities.  OGC GeoJSON is WGS84 by RFC 7946, so 4326 is the right default.
    UMP_RESULTSTORE_LDPROXY_NATIVE_CRS: int = 4326

    # Where entity YAML files (ldproxy provider + service configs) are written.
    # 'filesystem': write directly to UMP_RESULTSTORE_LDPROXY_ROOTPATH (dev/Docker).
    # 'k8s': create/patch Kubernetes ConfigMaps via the k8s API (production).
    UMP_RESULTSTORE_CONFIG_BACKEND: str = "filesystem"

    # Kubernetes settings — only required when UMP_RESULTSTORE_CONFIG_BACKEND=k8s.
    # Namespace where ldproxy ConfigMaps are managed.
    UMP_RESULTSTORE_K8S_NAMESPACE: str | None = None
    # Name of the ConfigMap holding the shared ump-results service entity.
    UMP_RESULTSTORE_K8S_SERVICE_CONFIGMAP: str = "ump-ldproxy-service"
    # Prefix for per-job provider ConfigMap names (suffix is the job UUID).
    UMP_RESULTSTORE_K8S_PROVIDER_CM_PREFIX: str = "ump-ldproxy-provider-"

    def print_settings(self, logger: LoggingPort):
        """Prints the settings for debugging purposes"""
        logger.info("UMP Settings:")
        print(self)


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


def set_logger(logger: LoggingPort):
    """Inject a concrete logger adapter from the composition root.
    Safe for modules that imported `logger` early: the delegator pointer updates.
    """
    _delegating_logger.set_delegate(logger)
    try:
        app_settings.print_settings(logger)
    except Exception:
        logging.getLogger("UMP").warning(
            "Failed printing settings with injected logger"
        )


def get_logger() -> LoggingPort:
    """Preferred access pattern for runtime modules to avoid early binding."""
    return logger
