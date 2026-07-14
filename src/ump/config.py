import logging
from pathlib import Path
from typing import Literal

from pydantic import FilePath, HttpUrl, SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings
from rich import print

logger = logging.getLogger(__name__)


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
    UMP_API_SERVER_URL: str = "http://localhost:3000"
    UMP_API_SERVER_WORKERS: int = 4
    UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL: int = 5
    UMP_DATABASE_NAME: str = "ump"
    UMP_DATABASE_HOST: str = "postgres"
    UMP_DATABASE_PORT: int = 5432
    UMP_DATABASE_USER: str = "postgres"
    UMP_DATABASE_PASSWORD: SecretStr = SecretStr("postgres")
    UMP_GEOSERVER_URL: HttpUrl | None = HttpUrl("http://geoserver:8080/geoserver")
    UMP_GEOSERVER_PUBLIC_URL: HttpUrl | None = None
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
    UMP_GEOSERVER_SERVICE_TYPE: Literal["wfs", "oaf"] = "wfs"
    UMP_GEOSERVER_CONNECTION_TIMEOUT: int = 60  # seconds
    # How often the cleanup task runs (seconds). Default: every 10 minutes.
    UMP_JOB_CLEANUP_INTERVAL_SECONDS: int = 600
    # How long jobs of anonymous users are kept before deletion (minutes). Default: 4 hours.
    UMP_JOB_RETENTION_MINUTES: int = 240
    # Set to False to disable automatic job cleanup entirely.
    UMP_JOB_CLEANUP_ENABLED: bool = True
    UMP_KEYCLOAK_URL: HttpUrl | None = HttpUrl("http://keycloak:8080/auth")
    UMP_KEYCLOAK_REALM: str = "UrbanModelPlatform"
    UMP_KEYCLOAK_CLIENT_ID: str = "ump-client"
    UMP_KEYCLOAK_USER: str = "admin"
    UMP_KEYCLOAK_PASSWORD: SecretStr = SecretStr("admin")
    UMP_API_SERVER_URL_PREFIX: str = "/"

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

    def print_settings(self):
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
app_settings.print_settings()
