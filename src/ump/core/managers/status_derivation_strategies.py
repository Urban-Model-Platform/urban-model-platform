"""Concrete implementations of status derivation strategies.

Provides four strategy classes that handle different provider response patterns:
1. DirectStatusInfoStrategy: statusInfo directly in response body
2. ImmediateResultsStrategy: outputs present without statusInfo (sync execution)
3. LocationFollowupStrategy: Location header only, requires follow-up GET
4. FallbackFailedStrategy: unparseable response, creates failed status
"""

from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urljoin

from ump.core.interfaces.status_derivation import (
    StatusDerivationContext,
    StatusDerivationResult,
)
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.models.job import JobStatusInfo, StatusCode
from ump.core.settings import logger

REQUIRED_STATUS_FIELDS = {"jobID", "status"}


class DirectStatusInfoStrategy:
    """Strategy for responses containing statusInfo directly in body.
    
    Handles the most common case where the provider returns a valid statusInfo
    object in the response body, with or without a Location header.
    """
    
    def __init__(self, http_client: HttpClientPort):
        self._http = http_client
    
    def can_handle(self, context: StatusDerivationContext) -> bool:
        """Check if response body contains valid statusInfo."""
        body = context.provider_body
        if not isinstance(body, dict):
            return False
        if not REQUIRED_STATUS_FIELDS.issubset(body.keys()):
            return False
        # Additional check: should not have 'outputs' without statusInfo
        # (that's ImmediateResultsStrategy's domain)
        if "outputs" in body and not REQUIRED_STATUS_FIELDS.issubset(body.keys()):
            return False
        return True
    
    async def derive(self, context: StatusDerivationContext) -> StatusDerivationResult:
        """Extract statusInfo from body and capture remote identifiers."""
        try:
            # Type guard: we know it's a dict from can_handle check
            body_dict = context.provider_body if isinstance(context.provider_body, dict) else {}
            status_info = JobStatusInfo(**body_dict)
        except Exception as e:
            logger.warning(
                f"[strategy:direct] Failed to parse statusInfo job_id={context.job.id} error={e}"
            )
            # Fallback to failed status
            return self._create_fallback(context)
        
        remote_status_url: Optional[str] = None
        remote_job_id: Optional[str] = None
        
        # Capture remote job ID (normalize to local UUID)
        if status_info.jobID and status_info.jobID != context.job.id:
            remote_job_id = status_info.jobID
            logger.debug(
                f"[strategy:direct] captured remote_job_id={remote_job_id} local_job_id={context.job.id}"
            )
        
        # Capture Location header as remote status URL
        if context.provider_headers.get("Location"):
            location = context.provider_headers["Location"]
            remote_status_url = self._resolve_location(
                str(context.provider.url), location
            )
            logger.debug(
                f"[strategy:direct] captured remote_status_url via Location job_id={context.job.id} url={remote_status_url}"
            )
        
        # Synthesize remote status URL if we have remote_job_id but no Location
        if remote_job_id and not remote_status_url:
            remote_status_url = (
                str(context.provider.url).rstrip("/")
                + f"/jobs/{remote_job_id}?f=json"
            )
            logger.debug(
                f"[strategy:direct] synthesized remote_status_url={remote_status_url} job_id={context.job.id}"
            )
        
        return StatusDerivationResult(
            status_info=status_info,
            remote_status_url=remote_status_url,
            remote_job_id=remote_job_id,
            diagnostic=None,
        )
    
    def _resolve_location(self, base: str, location: str) -> str:
        """Resolve relative Location header to absolute URL."""
        if location.startswith("http://") or location.startswith("https://"):
            return location
        return urljoin(base.rstrip("/") + "/", location.lstrip("/"))
    
    def _create_fallback(self, context: StatusDerivationContext) -> StatusDerivationResult:
        """Create failed status when statusInfo parsing fails."""
        status_info = JobStatusInfo(
            jobID=context.job.id,
            status=StatusCode.failed,
            type="process",
            processID=context.process_id,
            message="Failed to parse statusInfo from provider response",
            updated=datetime.now(timezone.utc),
            created=context.accepted_si.created,
            progress=None,
        )
        diagnostic = "statusInfo_parse_error"
        return StatusDerivationResult(
            status_info=status_info,
            remote_status_url=None,
            remote_job_id=None,
            diagnostic=diagnostic,
        )


class ImmediateResultsStrategy:
    """Strategy for responses containing outputs without statusInfo.
    
    Handles providers that return results directly (synchronous execution)
    without providing a statusInfo object. Synthesizes a successful status.
    """
    
    def can_handle(self, context: StatusDerivationContext) -> bool:
        """Check if response contains outputs but no statusInfo."""
        body = context.provider_body
        if not isinstance(body, dict):
            return False
        # Has outputs but missing required statusInfo fields
        return "outputs" in body and not REQUIRED_STATUS_FIELDS.issubset(body.keys())
    
    async def derive(self, context: StatusDerivationContext) -> StatusDerivationResult:
        """Synthesize successful statusInfo from immediate results."""
        logger.debug(
            f"[strategy:immediate] provider returned results body without statusInfo; synthesizing terminal success job_id={context.job.id}"
        )
        
        status_info = JobStatusInfo(
            jobID=context.job.id,
            status=StatusCode.successful,
            type="process",
            processID=context.process_id,
            message="Completed (immediate results)",
            created=context.accepted_si.created,
            started=context.accepted_si.created,
            finished=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            progress=100,
        )
        
        return StatusDerivationResult(
            status_info=status_info,
            remote_status_url=None,  # No polling needed for immediate results
            remote_job_id=None,
            diagnostic=None,
        )


class LocationFollowupStrategy:
    """Strategy for responses with Location header but no statusInfo in body.
    
    Handles asynchronous execution where provider returns only a Location header
    pointing to the job status endpoint. Requires follow-up GET to retrieve statusInfo.
    """
    
    def __init__(self, http_client: HttpClientPort):
        self._http = http_client
    
    def can_handle(self, context: StatusDerivationContext) -> bool:
        """Check if response has Location but no statusInfo."""
        body = context.provider_body

        # No statusInfo in body (or body is not a dict)
        if isinstance(body, dict) and REQUIRED_STATUS_FIELDS.issubset(body.keys()):
            pass

        has_location = bool(context.provider_headers.get("Location"))

        return has_location
    
    async def derive(self, context: StatusDerivationContext) -> StatusDerivationResult:
        """Follow Location header to fetch initial status snapshot."""
        location = context.provider_headers["Location"]
        resolved_url = self._resolve_location(str(context.provider.url), location)
        
        logger.debug(
            f"[strategy:location] following provider Location location={location} resolved={resolved_url} job_id={context.job.id}"
        )
        
        try:
            follow_resp = await self._http.get(resolved_url)
            status_info = self._extract_status_info(follow_resp)
            
            if not status_info:
                logger.warning(
                    f"[strategy:location] Location follow-up returned no statusInfo job_id={context.job.id}"
                )
                return self._create_fallback(context, resolved_url, "no_statusinfo_at_location")
            
            # Capture remote job ID if present
            remote_job_id: Optional[str] = None
            if status_info.jobID and status_info.jobID != context.job.id:
                remote_job_id = status_info.jobID
                logger.debug(
                    f"[strategy:location] captured remote_job_id={remote_job_id} local_job_id={context.job.id}"
                )
            
            return StatusDerivationResult(
                status_info=status_info,
                remote_status_url=resolved_url,
                remote_job_id=remote_job_id,
                diagnostic=None,
            )
            
        except Exception as follow_err:
            logger.warning(
                f"[strategy:location] Failed to follow Location {location}: {follow_err}"
            )
            return self._create_fallback(context, resolved_url, f"follow_error: {follow_err}")
    
    def _resolve_location(self, base: str, location: str) -> str:
        """Resolve relative Location header to absolute URL."""
        if location.startswith("http://") or location.startswith("https://"):
            return location
        return urljoin(base.rstrip("/") + "/", location.lstrip("/"))
    
    def _extract_status_info(self, body) -> Optional[JobStatusInfo]:
        """Extract statusInfo from response body."""
        if not isinstance(body, dict):
            return None
        if not REQUIRED_STATUS_FIELDS.issubset(body.keys()):
            return None
        try:
            return JobStatusInfo(**body)
        except Exception:
            return None
    
    def _create_fallback(
        self, context: StatusDerivationContext, url: str, reason: str
    ) -> StatusDerivationResult:
        """Create failed status when Location follow-up fails."""
        status_info = JobStatusInfo(
            jobID=context.job.id,
            status=StatusCode.failed,
            type="process",
            processID=context.process_id,
            message="Failed to fetch status from Location header",
            updated=datetime.now(timezone.utc),
            created=context.accepted_si.created,
            progress=None,
        )
        diagnostic = f"location_followup_failed: {url} reason={reason}"
        return StatusDerivationResult(
            status_info=status_info,
            remote_status_url=None,
            remote_job_id=None,
            diagnostic=diagnostic,
        )


class FallbackFailedStrategy:
    """Strategy for unparseable responses (fallback/catch-all).
    
    Handles responses that don't match any other pattern - missing both statusInfo
    and recognizable result structure. Creates a failed status with diagnostic info.
    """
    
    def can_handle(self, context: StatusDerivationContext) -> bool:
        """Always returns True as this is the fallback strategy."""
        return True
    
    async def derive(self, context: StatusDerivationContext) -> StatusDerivationResult:
        """Create failed status for unparseable response."""
        logger.debug(
            f"[strategy:fallback] missing statusInfo, marking failed job_id={context.job.id}"
        )
        
        status_info = JobStatusInfo(
            jobID=context.job.id,
            status=StatusCode.failed,
            type="process",
            processID=context.process_id,
            message="Provider response missing statusInfo",
            updated=datetime.now(timezone.utc),
            created=context.accepted_si.created,
            progress=None,
        )
        
        diagnostic = (
            f"provider_status={context.provider_status} "
            f"body_type={type(context.provider_body).__name__}"
        )
        
        return StatusDerivationResult(
            status_info=status_info,
            remote_status_url=None,
            remote_job_id=None,
            diagnostic=diagnostic,
        )
