"""Unit tests for JobManager refactored polling and retry logic.

Tests the extracted polling helper methods and retry error classification.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, Mock

import pytest
import pytest_asyncio

from ump.adapters.job_repository_inmemory import InMemoryJobRepository
from ump.core.config import JobManagerConfig
from ump.core.exceptions import OGCProcessException
from ump.core.managers.job_manager import JobManager, TransientOGCError
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.ogcp_exception import OGCExceptionResponse

# --- Test Fixtures ---


@pytest.fixture
def test_config():
    """Create test configuration."""
    return JobManagerConfig(
        poll_interval=0.01,
        rewrite_remote_links=True,
        inline_inputs_size_limit=64 * 1024,
        forward_max_retries=3,
        forward_retry_base_wait=0.1,
        forward_retry_max_wait=0.5,
    )


@pytest.fixture
def test_repo():
    """Create test repository."""
    return InMemoryJobRepository()


@pytest_asyncio.fixture
async def test_job(test_repo):
    """Create and persist test job."""
    job = Job(
        id="test-job-123",
        process_id="test:process",
        provider="test",
        status="accepted",
        inputs=None,
        inputs_storage="inline",
        created=datetime.now(timezone.utc),
    )
    job.remote_status_url = "http://remote.test/jobs/123"

    status_info = JobStatusInfo(
        jobID=job.id,
        status=StatusCode.running,
        type="process",
        processID=job.process_id,
        created=job.created,
        updated=datetime.now(timezone.utc),
        progress=0,
    )
    job.apply_status_info(status_info)

    await test_repo.create(job)
    return job


@pytest.fixture
def mock_providers():
    """Create mock providers port."""

    class MockProvider:
        url = "http://provider.test/"

    class MockProviders:
        def get_provider(self, prefix):
            return MockProvider()

        def list_providers(self):
            return ["test"]

    return MockProviders()


@pytest.fixture
def mock_validator():
    """Create mock process ID validator."""

    class MockValidator:
        def extract(self, process_id):
            parts = process_id.split(":", 1)
            if len(parts) == 2:
                return parts[0], parts[1]
            raise ValueError("Invalid process ID")

    return MockValidator()


@pytest.fixture
def mock_http_client():
    """Create mock HTTP client."""
    return AsyncMock()


@pytest.fixture
def job_manager(
    test_config, mock_providers, mock_http_client, mock_validator, test_repo
):
    """Create JobManager instance for testing."""
    return JobManager(
        providers=mock_providers,
        http_client=mock_http_client,
        process_id_validator=mock_validator,
        job_repo=test_repo,
        config=test_config,
        observers=[],  # No observers for unit tests
    )


# --- Polling Logic Tests ---


class TestShouldStopPolling:
    """Test _should_stop_polling termination conditions."""

    @pytest.mark.asyncio
    async def test_stops_when_job_not_found(self, job_manager, test_repo):
        """Should stop polling if job no longer exists."""
        should_stop, reason = await job_manager._should_stop_polling("nonexistent-job")

        assert should_stop is True
        assert "not found" in reason

    @pytest.mark.asyncio
    async def test_stops_when_job_is_terminal(self, job_manager, test_job, test_repo):
        """Should stop polling if job reached terminal state."""
        # Update job to successful
        success_status = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.successful,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=datetime.now(timezone.utc),
            progress=100,
        )
        test_job.apply_status_info(success_status)
        await test_repo.update(test_job)

        should_stop, reason = await job_manager._should_stop_polling(test_job.id)

        assert should_stop is True
        assert "terminal" in reason.lower()

    @pytest.mark.asyncio
    async def test_stops_when_no_remote_url(self, job_manager, test_job, test_repo):
        """Should stop polling if job has no remote status URL."""
        test_job.remote_status_url = None
        await test_repo.update(test_job)

        should_stop, reason = await job_manager._should_stop_polling(test_job.id)

        assert should_stop is True
        assert "remote_status_url" in reason

    @pytest.mark.asyncio
    async def test_continues_for_running_job(self, job_manager, test_job):
        """Should continue polling for running jobs with remote URL."""
        should_stop, reason = await job_manager._should_stop_polling(test_job.id)

        assert should_stop is False
        assert reason == ""


class TestNeedsEnrichment:
    """Test _needs_enrichment logic."""

    def test_needs_enrichment_when_status_changed(self, job_manager: JobManager):
        """Should need enrichment when status changes."""
        status_info = JobStatusInfo(
            jobID="test",
            status=StatusCode.running,
            type="process",
            processID="test:process",
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            started=datetime.now(timezone.utc),
            progress=50,
            message="Processing",
        )

        needs = job_manager._needs_enrichment(status_info, StatusCode.accepted)

        assert needs is True

    def test_needs_enrichment_when_fields_missing(self, job_manager):
        """Should need enrichment when optional fields are None."""
        status_info = JobStatusInfo(
            jobID="test",
            status=StatusCode.running,
            type="process",
            processID="test:process",
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            started=None,  # Missing
            progress=None,  # Missing
            message=None,  # Missing
        )

        needs = job_manager._needs_enrichment(status_info, StatusCode.running)

        assert needs is True

    def test_no_enrichment_when_complete(self, job_manager):
        """Should not need enrichment when all fields present and status unchanged."""
        status_info = JobStatusInfo(
            jobID="test",
            status=StatusCode.running,
            type="process",
            processID="test:process",
            created=datetime.now(timezone.utc),
            updated=datetime.now(timezone.utc),
            started=datetime.now(timezone.utc),
            progress=50,
            message="Processing",
        )

        needs = job_manager._needs_enrichment(status_info, StatusCode.running)

        assert needs is False


class TestPollAndUpdateStatus:
    """Test _poll_and_update_status error handling."""

    @pytest.mark.asyncio
    async def test_returns_false_when_no_remote_url(self, job_manager, test_job):
        """Should return False if job has no remote status URL."""
        test_job.remote_status_url = None

        terminal = await job_manager._poll_and_update_status(test_job)

        assert terminal is False

    @pytest.mark.asyncio
    async def test_handles_http_error_gracefully(
        self, job_manager, test_job, mock_http_client
    ):
        """Should handle HTTP errors without raising."""
        mock_http_client.get.side_effect = Exception("Connection error")

        terminal = await job_manager._poll_and_update_status(test_job)

        assert terminal is False  # Error logged, polling continues

    @pytest.mark.asyncio
    async def test_returns_false_for_invalid_statusinfo(
        self, job_manager, test_job, mock_http_client
    ):
        """Should return False if response doesn't contain valid statusInfo."""
        mock_http_client.get.return_value = {"invalid": "response"}

        terminal = await job_manager._poll_and_update_status(test_job)

        assert terminal is False

    @pytest.mark.asyncio
    async def test_returns_true_for_terminal_status(
        self, job_manager, test_job, mock_http_client, test_repo
    ):
        """Should return True when job reaches terminal state."""
        mock_http_client.get.return_value = {
            "jobID": "remote-123",
            "status": "successful",
            "type": "process",
        }

        terminal = await job_manager._poll_and_update_status(test_job)

        assert terminal is True


# --- Retry Logic Tests ---


class TestTransientErrorClassification:
    """Test _is_transient_error error classification."""

    def test_502_is_transient(self, job_manager):
        """502 Bad Gateway should be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Bad Gateway",
                status=502,
                detail="Upstream server error",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is True

    def test_503_is_transient(self, job_manager):
        """503 Service Unavailable should be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Service Unavailable",
                status=503,
                detail="Server temporarily unavailable",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is True

    def test_504_is_transient(self, job_manager):
        """504 Gateway Timeout should be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Gateway Timeout",
                status=504,
                detail="Upstream timeout",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is True

    def test_400_is_not_transient(self, job_manager):
        """400 Bad Request should not be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Bad Request",
                status=400,
                detail="Invalid request",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is False

    def test_404_is_not_transient(self, job_manager):
        """404 Not Found should not be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Not Found",
                status=404,
                detail="Resource not found",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is False

    def test_401_is_not_transient(self, job_manager):
        """401 Unauthorized should not be transient."""
        exc = OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Unauthorized",
                status=401,
                detail="Authentication required",
                instance=None,
            )
        )

        assert job_manager._is_transient_error(exc) is False

    def test_non_ogc_exception_is_transient(self, job_manager):
        """Non-OGC exceptions (connection errors, etc.) should be transient."""
        exc = Exception("Connection refused")

        assert job_manager._is_transient_error(exc) is True


class TestTransientOGCError:
    """Test TransientOGCError wrapper exception."""

    def test_wraps_ogc_exception(self):
        """Should wrap OGCProcessException for retry signaling."""
        response = OGCExceptionResponse(
            type="about:blank",
            title="Bad Gateway",
            status=502,
            detail="Upstream error",
            instance=None,
        )

        transient = TransientOGCError(response)

        assert isinstance(transient, OGCProcessException)
        assert transient.response.status == 502

    def test_preserves_response_info(self):
        """Should preserve all response information."""
        response = OGCExceptionResponse(
            type="custom:error",
            title="Temporary Failure",
            status=503,
            detail="Service temporarily unavailable",
            instance="/jobs/123",
        )

        transient = TransientOGCError(response)

        assert transient.response.type == "custom:error"
        assert transient.response.title == "Temporary Failure"
        assert transient.response.detail == "Service temporarily unavailable"
        assert transient.response.instance == "/jobs/123"


# --- ProcessStatusUpdate Tests ---


class TestProcessStatusUpdate:
    """Test _process_status_update normalization and enrichment."""

    @pytest.mark.asyncio
    async def test_normalizes_remote_job_id(self, job_manager, test_job, test_repo):
        """Should normalize remote job ID to local ID."""
        status_info = JobStatusInfo(
            jobID="remote-999",  # Different from local
            status=StatusCode.running,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=datetime.now(timezone.utc),
            progress=0,
        )

        terminal = await job_manager._process_status_update(test_job, status_info)

        assert status_info.jobID == test_job.id  # Normalized
        assert test_job.remote_job_id == "remote-999"  # Captured

    @pytest.mark.asyncio
    async def test_enriches_missing_fields(self, job_manager, test_job, test_repo):
        """Should enrich missing optional fields."""
        status_info = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.running,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=datetime.now(timezone.utc),
            started=None,  # Will be enriched
            progress=None,  # Will be enriched
            message=None,  # Will be enriched
        )

        await job_manager._process_status_update(test_job, status_info)

        assert status_info.started is not None
        assert status_info.progress == 0  # Default for running
        assert status_info.message == "Running"  # Default message

    @pytest.mark.asyncio
    async def test_returns_true_for_terminal(self, job_manager, test_job, test_repo):
        """Should return True when processing terminal status."""
        status_info = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.successful,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=datetime.now(timezone.utc),
            progress=100,
        )

        terminal = await job_manager._process_status_update(test_job, status_info)

        assert terminal is True

    @pytest.mark.asyncio
    async def test_returns_false_for_non_terminal(
        self, job_manager, test_job, test_repo
    ):
        """Should return False when processing non-terminal status."""
        status_info = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.running,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=datetime.now(timezone.utc),
            progress=50,
        )

        terminal = await job_manager._process_status_update(test_job, status_info)

        assert terminal is False


# --- Polling fan-out regression tests ---
# These tests guard against the bug where every poll cycle spawned a new poll
# loop task, exhausting the DB connection pool within seconds.


class TestSchedulePollDeduplication:
    """_schedule_poll must not create duplicate loops for the same job."""

    @pytest.mark.asyncio
    async def test_first_call_creates_task(self, job_manager):
        """A fresh job gets exactly one poll task scheduled."""
        job_manager._schedule_poll("job-abc")
        assert "job-abc" in job_manager._active_poll_jobs

    @pytest.mark.asyncio
    async def test_second_call_is_ignored(self, job_manager):
        """A second call while the loop is active is silently dropped."""
        job_manager._schedule_poll("job-abc")
        task_count_before = len(job_manager._poll_tasks)
        job_manager._schedule_poll("job-abc")  # duplicate — must be no-op
        assert len(job_manager._poll_tasks) == task_count_before

    @pytest.mark.asyncio
    async def test_different_jobs_each_get_a_task(self, job_manager):
        """Each distinct job ID gets its own poll loop."""
        job_manager._schedule_poll("job-1")
        job_manager._schedule_poll("job-2")
        assert "job-1" in job_manager._active_poll_jobs
        assert "job-2" in job_manager._active_poll_jobs

    def test_shutdown_prevents_scheduling(self, job_manager):
        """No task is created after shutdown is set."""
        job_manager._shutdown = True
        job_manager._schedule_poll("job-abc")
        assert "job-abc" not in job_manager._active_poll_jobs
        assert len(job_manager._poll_tasks) == 0


class TestProcessStatusUpdateObserverGuard:
    """_process_status_update must only fire on_status_changed on real transitions."""

    @pytest.mark.asyncio
    async def test_no_observer_notification_on_unchanged_status(
        self, job_manager, test_job, test_repo
    ):
        """Polling the same status (e.g. accepted→accepted) must not notify observers."""
        notifications: list[tuple] = []

        class TrackingObserver:
            async def on_job_created(self, job, status_info):
                pass

            async def on_status_changed(self, job, old_si, new_si):
                notifications.append((old_si.status if old_si else None, new_si.status))

            async def on_job_completed(self, job, final_si):
                pass

        job_manager._observers = [TrackingObserver()]

        # Job is currently accepted; remote also returns accepted (no change)
        accepted_si = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.accepted,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=test_job.created,
        )
        test_job.apply_status_info(accepted_si)
        await test_repo.update(test_job)

        remote_si = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.accepted,  # still accepted — no real change
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=test_job.created,
        )
        await job_manager._process_status_update(test_job, remote_si)

        assert notifications == [], (
            "on_status_changed must not fire when old_status == new_status; "
            "that would spawn duplicate poll loops via PollingSchedulerObserver"
        )

    @pytest.mark.asyncio
    async def test_observer_notified_on_real_transition(
        self, job_manager, test_job, test_repo
    ):
        """Polling that returns a different status (accepted→running) must notify."""
        notifications: list[tuple] = []

        class TrackingObserver:
            async def on_job_created(self, job, status_info):
                pass

            async def on_status_changed(self, job, old_si, new_si):
                notifications.append((old_si.status if old_si else None, new_si.status))

            async def on_job_completed(self, job, final_si):
                pass

        job_manager._observers = [TrackingObserver()]

        accepted_si = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.accepted,
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=test_job.created,
        )
        test_job.apply_status_info(accepted_si)
        await test_repo.update(test_job)

        running_si = JobStatusInfo(
            jobID=test_job.id,
            status=StatusCode.running,  # genuine transition
            type="process",
            processID=test_job.process_id,
            created=test_job.created,
            updated=test_job.created,
        )
        await job_manager._process_status_update(test_job, running_si)

        assert len(notifications) == 1
        assert notifications[0] == (StatusCode.accepted, StatusCode.running)
