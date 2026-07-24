from typing import Optional
from ump.core.models.ogcp_exception import OGCExceptionResponse


class OGCProcessException(Exception):
    """Base exception for OGC Process API errors."""
    def __init__(self, response: OGCExceptionResponse):
        self.response = response


class OptimisticLockError(Exception):
    """Raised when a DB update fails because another instance modified the row.

    Callers should re-read the row from the DB and retry the operation.
    """
    pass


# Domain-specific job execution exceptions

class JobExecutionError(Exception):
    """Base exception for job execution failures.
    
    Attributes:
        message: Human-readable error description
        diagnostic: Technical diagnostic information for debugging
        job_id: Optional job identifier
    """
    def __init__(
        self,
        message: str,
        diagnostic: Optional[str] = None,
        job_id: Optional[str] = None
    ):
        self.message = message
        self.diagnostic = diagnostic
        self.job_id = job_id
        super().__init__(message)


class JobTimeoutError(JobExecutionError):
    """Raised when job exceeds configured timeout waiting for remote completion.
    
    Attributes:
        elapsed_seconds: Time elapsed before timeout
        timeout_seconds: Configured timeout value
    """
    def __init__(
        self,
        job_id: str,
        elapsed_seconds: float,
        timeout_seconds: float,
        diagnostic: Optional[str] = None
    ):
        self.elapsed_seconds = elapsed_seconds
        self.timeout_seconds = timeout_seconds
        message = f"Job {job_id} timed out after {elapsed_seconds:.1f}s (limit: {timeout_seconds}s)"
        super().__init__(message=message, diagnostic=diagnostic, job_id=job_id)


class RemoteProviderError(JobExecutionError):
    """Raised when remote provider returns error response or fails to respond.
    
    Attributes:
        provider_name: Name/prefix of the provider
        upstream_status: HTTP status code from provider (if applicable)
        upstream_body: Response body from provider (if available)
    """
    def __init__(
        self,
        message: str,
        provider_name: Optional[str] = None,
        upstream_status: Optional[int] = None,
        upstream_body: Optional[str] = None,
        diagnostic: Optional[str] = None,
        job_id: Optional[str] = None
    ):
        self.provider_name = provider_name
        self.upstream_status = upstream_status
        self.upstream_body = upstream_body
        super().__init__(message=message, diagnostic=diagnostic, job_id=job_id)


class ResultsFetchError(JobExecutionError):
    """Raised when results cannot be fetched for a successful job.
    
    This indicates a mismatch between reported success status and actual
    results availability, typically downgraded to failed status.
    
    Attributes:
        remote_job_id: Remote provider's job identifier
        results_url: URL that failed to return results
    """
    def __init__(
        self,
        job_id: str,
        remote_job_id: str,
        results_url: str,
        diagnostic: Optional[str] = None
    ):
        self.remote_job_id = remote_job_id
        self.results_url = results_url
        message = f"Result fetch failed for job {job_id} (remote: {remote_job_id})"
        super().__init__(message=message, diagnostic=diagnostic, job_id=job_id)