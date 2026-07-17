"""Unit tests for status derivation strategies.

Tests the Strategy pattern implementation for deriving job status from
various provider response formats. Each strategy is tested independently
for both can_handle() and derive() methods.
"""

import pytest
from datetime import datetime, timezone

from ump.core.managers.status_derivation_strategies import (
    DirectStatusInfoStrategy,
    ImmediateResultsStrategy,
    LocationFollowupStrategy,
    FallbackFailedStrategy,
)
from ump.core.interfaces.status_derivation import StatusDerivationContext
from ump.core.models.job import Job, JobStatusInfo, StatusCode


# --- Test Fixtures ---

@pytest.fixture
def mock_job():
    """Create a minimal test job."""
    return Job(
        id="test-job-123",
        process_id="test:process",
        provider="test",
        status="accepted",
        inputs=None,
        inputs_storage="inline",
    )


@pytest.fixture
def accepted_status():
    """Create accepted status info."""
    return JobStatusInfo(
        jobID="test-job-123",
        status=StatusCode.accepted,
        type="process",
        processID="test:process",
        created=datetime.now(timezone.utc),
        updated=datetime.now(timezone.utc),
        progress=0,
    )


@pytest.fixture
def mock_provider():
    """Create mock provider."""
    class MockProvider:
        url = "http://provider.test/"
    return MockProvider()


@pytest.fixture
def mock_http_client():
    """Create mock HTTP client for testing."""
    class MockHttpClient:
        def __init__(self):
            self.get_response = None
        
        async def get(self, url, timeout=None, headers=None):
            if self.get_response is None:
                raise Exception("No response configured")
            return self.get_response
    
    return MockHttpClient()


# --- DirectStatusInfoStrategy Tests ---

class TestDirectStatusInfoStrategy:
    """Test DirectStatusInfoStrategy for responses with statusInfo in body."""
    
    def test_can_handle_with_valid_statusinfo(self, mock_job, mock_provider, accepted_status):
        """Should handle responses with valid statusInfo in body."""
        strategy = DirectStatusInfoStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {
                "jobID": "remote-123",
                "status": "running",
                "type": "process",
            },
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is True
    
    def test_can_handle_with_missing_fields(self, mock_job, mock_provider, accepted_status):
        """Should not handle responses missing required statusInfo fields."""
        strategy = DirectStatusInfoStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {"jobID": "remote-123"},  # Missing status and type
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is False
    
    def test_can_handle_with_non_dict_body(self, mock_job, mock_provider, accepted_status):
        """Should not handle responses with non-dict body."""
        strategy = DirectStatusInfoStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": "string response",
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is False
    
    @pytest.mark.asyncio
    async def test_derive_extracts_statusinfo(self, mock_job, mock_provider, accepted_status):
        """Should extract statusInfo from response body."""
        strategy = DirectStatusInfoStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {
                "jobID": "remote-123",
                "status": "running",
                "type": "process",
                "message": "Processing",
            },
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.status_info is not None
        assert result.status_info.status == StatusCode.running
        assert result.status_info.message == "Processing"
        assert result.remote_job_id == "remote-123"
    
    @pytest.mark.asyncio
    async def test_derive_captures_location_header(self, mock_job, mock_provider, accepted_status):
        """Should capture remote status URL from Location header."""
        strategy = DirectStatusInfoStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {
                "jobID": "remote-123",
                "status": "running",
                "type": "process",
            },
            "headers": {"Location": "/jobs/remote-123"},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.remote_status_url == "http://provider.test/jobs/remote-123"


# --- ImmediateResultsStrategy Tests ---

class TestImmediateResultsStrategy:
    """Test ImmediateResultsStrategy for sync execution responses."""
    
    def test_can_handle_with_outputs(self, mock_job, mock_provider, accepted_status):
        """Should handle responses with outputs but no statusInfo."""
        strategy = ImmediateResultsStrategy()
        provider_resp = {
            "status": 200,
            "body": {"outputs": {"result": {"value": 42}}},
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is True
    
    def test_can_handle_without_outputs(self, mock_job, mock_provider, accepted_status):
        """Should not handle responses without outputs."""
        strategy = ImmediateResultsStrategy()
        provider_resp = {
            "status": 200,
            "body": {"something": "else"},
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is False
    
    @pytest.mark.asyncio
    async def test_derive_synthesizes_success(self, mock_job, mock_provider, accepted_status):
        """Should synthesize successful status with outputs."""
        strategy = ImmediateResultsStrategy()
        provider_resp = {
            "status": 200,
            "body": {"outputs": {"result": {"value": 42}}},
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.status_info is not None
        assert result.status_info.status == StatusCode.successful


# --- LocationFollowupStrategy Tests ---

class TestLocationFollowupStrategy:
    """Test LocationFollowupStrategy for async execution with Location header."""
    
    def test_can_handle_with_location_no_body(self, mock_job, mock_provider, accepted_status):
        """Should handle responses with Location but no body."""
        strategy = LocationFollowupStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {},
            "headers": {"Location": "/jobs/remote-123"},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is True
    
    def test_can_handle_without_location(self, mock_job, mock_provider, accepted_status):
        """Should not handle responses without Location header."""
        strategy = LocationFollowupStrategy(http_client=None)
        provider_resp = {
            "status": 201,
            "body": {},
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is False
    
    @pytest.mark.asyncio
    async def test_derive_follows_location(self, mock_job, mock_provider, accepted_status, mock_http_client):
        """Should follow Location header and extract statusInfo."""
        mock_http_client.get_response = {
            "jobID": "remote-123",
            "status": "running",
            "type": "process",
        }
        
        strategy = LocationFollowupStrategy(http_client=mock_http_client)
        provider_resp = {
            "status": 201,
            "body": {},
            "headers": {"Location": "/jobs/remote-123"},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.status_info is not None
        assert result.status_info.status == StatusCode.running
        assert result.remote_status_url == "http://provider.test/jobs/remote-123"
    
    @pytest.mark.asyncio
    async def test_derive_handles_followup_error(self, mock_job, mock_provider, accepted_status, mock_http_client):
        """Should handle errors when following Location."""
        mock_http_client.get_response = None  # Will raise exception
        
        strategy = LocationFollowupStrategy(http_client=mock_http_client)
        provider_resp = {
            "status": 201,
            "body": {},
            "headers": {"Location": "/jobs/remote-123"},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.status_info is not None
        assert result.status_info.status == StatusCode.failed
        assert result.diagnostic is not None
        assert "location_followup_failed" in result.diagnostic


# --- FallbackFailedStrategy Tests ---

class TestFallbackFailedStrategy:
    """Test FallbackFailedStrategy for unparseable responses."""
    
    def test_can_handle_always_returns_true(self, mock_job, mock_provider, accepted_status):
        """Fallback strategy should always handle (catch-all)."""
        strategy = FallbackFailedStrategy()
        provider_resp = {
            "status": 500,
            "body": "unexpected response",
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        assert strategy.can_handle(context) is True
    
    @pytest.mark.asyncio
    async def test_derive_creates_failed_status(self, mock_job, mock_provider, accepted_status):
        """Should create failed status with diagnostic info."""
        strategy = FallbackFailedStrategy()
        provider_resp = {
            "status": 500,
            "body": "Server error",
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await strategy.derive(context)
        
        assert result.status_info is not None
        assert result.status_info.status == StatusCode.failed
        assert result.status_info.message is not None
        assert "Provider response missing statusInfo" in result.status_info.message
        assert result.diagnostic is not None


# --- StatusDerivationOrchestrator Tests ---

class TestStatusDerivationOrchestrator:
    """Test strategy orchestration and selection logic."""
    
    @pytest.mark.asyncio
    async def test_selects_direct_strategy_for_statusinfo(
        self, mock_job, mock_provider, accepted_status, mock_http_client
    ):
        """Should select DirectStatusInfoStrategy for responses with statusInfo."""
        from ump.core.managers.status_derivation_orchestrator import StatusDerivationOrchestrator
        
        orchestrator = StatusDerivationOrchestrator(mock_http_client)
        provider_resp = {
            "status": 201,
            "body": {
                "jobID": "remote-123",
                "status": "running",
                "type": "process",
            },
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await orchestrator.derive_status(context)
        
        assert result.status_info.status == StatusCode.running
    
    @pytest.mark.asyncio
    async def test_selects_immediate_strategy_for_outputs(
        self, mock_job, mock_provider, accepted_status, mock_http_client
    ):
        """Should select ImmediateResultsStrategy for sync responses."""
        from ump.core.managers.status_derivation_orchestrator import StatusDerivationOrchestrator
        
        orchestrator = StatusDerivationOrchestrator(mock_http_client)
        provider_resp = {
            "status": 200,
            "body": {"outputs": {"result": {"value": 42}}},
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await orchestrator.derive_status(context)
        
        assert result.status_info.status == StatusCode.successful
    
    @pytest.mark.asyncio
    async def test_fallback_for_unparseable(
        self, mock_job, mock_provider, accepted_status, mock_http_client
    ):
        """Should fallback to FallbackFailedStrategy for unparseable responses."""
        from ump.core.managers.status_derivation_orchestrator import StatusDerivationOrchestrator
        
        orchestrator = StatusDerivationOrchestrator(mock_http_client)
        provider_resp = {
            "status": 500,
            "body": "error text",
            "headers": {},
        }
        context = StatusDerivationContext(
            job=mock_job,
            process_id="test:process",
            provider=mock_provider,
            provider_resp=provider_resp,
            accepted_si=accepted_status,
        )
        
        result = await orchestrator.derive_status(context)
        
        assert result.status_info.status == StatusCode.failed
