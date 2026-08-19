"""Configuration models for core domain components.

This module provides Pydantic-based configuration classes that consolidate
settings for domain managers, enabling dependency injection and testability.
"""

from typing import Optional

from pydantic import BaseModel, Field


class JobManagerConfig(BaseModel):
    """Configuration for JobManager behavior.

    Consolidates global job execution settings in one place, enabling:
    - Clear dependency injection in composition roots
    - Easy testing with custom configurations
    - Type-safe access to settings
    - Self-documenting configuration

    Note: poll_timeout is now resolved per-process from ProviderConfig.ttw_job_done
    or ProcessConfig.ttw_job_done, not from this config. See JobManager._resolve_ttw().

    Attributes:
        poll_interval: Seconds between remote status poll requests (global default, can be overridden per-process)
        rewrite_remote_links: Whether to replace remote links with local equivalents
        inline_inputs_size_limit: Maximum size (bytes) for storing inputs inline vs object storage
    """

    poll_interval: float = Field(
        default=5.0,
        gt=0,
        description="Global default interval in seconds between remote job status polling requests (can be overridden per-process)",
    )

    rewrite_remote_links: bool = Field(
        default=True,
        description="Replace external provider links with local API links in responses",
    )

    inline_inputs_size_limit: int = Field(
        default=64 * 1024,  # 64 KB
        ge=0,
        description="Maximum size in bytes for storing job inputs inline (larger inputs use object storage)",
    )

    verify_remote_results: bool = Field(
        default=True,
        description="Attempt to fetch remote results immediately when provider reports success",
    )

    forward_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retry attempts for transient errors when forwarding to remote provider",
    )

    forward_retry_base_wait: float = Field(
        default=1.0,
        gt=0,
        description="Base wait time in seconds for exponential backoff between retries",
    )

    forward_retry_max_wait: float = Field(
        default=5.0,
        gt=0,
        description="Maximum wait time in seconds between retry attempts",
    )

    results_fetch_timeout: float = Field(
        default=120.0,
        gt=0,
        description="Total timeout in seconds for a single remote /results fetch attempt",
    )

    results_fetch_max_retries: int = Field(
        default=3,
        ge=1,
        le=10,
        description="Maximum retry attempts for transient errors when fetching remote results",
    )

    results_fetch_retry_base_wait: float = Field(
        default=1.0,
        gt=0,
        description="Base wait time in seconds for exponential backoff between results-fetch retries",
    )

    results_fetch_retry_max_wait: float = Field(
        default=10.0,
        gt=0,
        description="Maximum wait time in seconds between results-fetch retry attempts",
    )

    model_config = {
        "frozen": True,  # Immutable after creation for safety
        "extra": "forbid",  # Reject unknown fields
    }

    @classmethod
    def from_app_settings(cls, settings) -> "JobManagerConfig":
        """Factory method to construct config from UmpSettings instance.

        Args:
            settings: UmpSettings instance from core.settings

        Returns:
            JobManagerConfig with values from app settings
        """
        return cls(
            poll_interval=settings.UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL,
            rewrite_remote_links=settings.UMP_REWRITE_REMOTE_LINKS,
            verify_remote_results=settings.UMP_VERIFY_REMOTE_RESULTS,
            results_fetch_timeout=settings.UMP_RESULTS_FETCH_TIMEOUT,
            results_fetch_max_retries=settings.UMP_RESULTS_FETCH_MAX_RETRIES,
            results_fetch_retry_base_wait=settings.UMP_RESULTS_FETCH_RETRY_BASE_WAIT,
            results_fetch_retry_max_wait=settings.UMP_RESULTS_FETCH_RETRY_MAX_WAIT,
            # results_finalizing_retry_after intentionally omitted: it is an
            # internal tuning value that uses the field default (see above) and
            # is not exposed as an operator setting.
            # poll_timeout removed: now resolved per-process from provider/process config
            # inline_inputs_size_limit, forward_max_retries, forward_retry_base_wait,
            # forward_retry_max_wait all use defaults (no settings exist yet)
        )
