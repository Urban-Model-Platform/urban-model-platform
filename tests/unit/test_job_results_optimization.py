"""Tests for optimized Job.results() avoiding unnecessary remote fetches.

This module tests the optimization where reference-mode outputs that were
already stored during job completion are returned from cache without
re-fetching from the remote model server.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from ump.api.models.job import Job


def _create_test_job(
    job_id="job-test-123",
    status="successful",
    transmission_mode="value",
    output_transmission_modes=None,
    provider_prefix="test-provider",
    process_id="test-process",
):
    """Helper to create a test Job instance without DB access."""
    job = Job.__new__(Job)
    job.job_id = job_id
    job.remote_job_id = "remote-456"
    job.process_id = process_id
    job.provider_prefix = provider_prefix
    job.provider_url = "http://modelserver:5000/"
    job.status = status
    job.message = ""
    object.__setattr__(job, "transmission_mode", transmission_mode)
    job.output_transmission_modes = output_transmission_modes or {}
    return job


class TestResultsOptimization:
    """Tests for results() method optimization with reference outputs."""

    @pytest.mark.asyncio
    async def test_reference_outputs_skip_remote_fetch(self, monkeypatch):
        """When all outputs are reference mode, skip remote fetch."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference"},
        )

        # Mock the cache check and builder
        monkeypatch.setattr(
            job,
            "_all_outputs_reference_and_stored",
            lambda: True,
        )

        mock_cached = {
            "dem": {
                "href": "http://geoserver/wfs?layer=job-test-123__dem",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": "application/geo+json",
                "title": "Result 'dem' for job job-test-123",
            }
        }

        monkeypatch.setattr(
            job,
            "_build_cached_reference_results",
            lambda: mock_cached,
        )

        # Mock remote fetch to prove it's not called
        fetch_mock = AsyncMock()
        monkeypatch.setattr(job, "_fetch_inline_results", fetch_mock)

        result = await job.results()

        # Should return cached results
        assert result == mock_cached
        # Remote fetch should NOT have been called
        fetch_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_mixed_modes_still_fetch_remote(self, monkeypatch):
        """When modes are mixed (value+reference), still fetch remote."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "value"},
        )

        # All outputs NOT reference, so cache optimization doesn't apply
        monkeypatch.setattr(
            job,
            "_all_outputs_reference_and_stored",
            lambda: False,
        )

        mock_inline = {
            "dem": {"type": "FeatureCollection", "features": []},
            "slope": {"type": "FeatureCollection", "features": []},
        }

        fetch_mock = AsyncMock(return_value=mock_inline)
        monkeypatch.setattr(job, "_fetch_inline_results", fetch_mock)

        apply_mock = MagicMock(
            return_value={
                "dem": {"href": "http://geoserver/..."},
                "slope": {"type": "FeatureCollection", "features": []},
            }
        )
        monkeypatch.setattr(
            job,
            "_apply_per_output_transmission_modes",
            apply_mock,
        )

        result = await job.results()

        # Should fetch remote and apply modes
        fetch_mock.assert_called_once()
        apply_mock.assert_called_once()

    @pytest.mark.asyncio
    async def test_value_mode_always_fetches_remote(self, monkeypatch):
        """Value mode outputs always fetch from remote."""
        job = _create_test_job(
            output_transmission_modes={"dem": "value"},
        )

        monkeypatch.setattr(
            job,
            "_all_outputs_reference_and_stored",
            lambda: False,
        )

        mock_inline = {"dem": {"type": "FeatureCollection", "features": []}}
        fetch_mock = AsyncMock(return_value=mock_inline)
        monkeypatch.setattr(job, "_fetch_inline_results", fetch_mock)

        apply_mock = MagicMock(return_value=mock_inline)
        monkeypatch.setattr(
            job,
            "_apply_per_output_transmission_modes",
            apply_mock,
        )

        await job.results()

        # Must fetch remote for value outputs
        fetch_mock.assert_called_once()
        apply_mock.assert_called_once()

    def test_all_outputs_reference_and_stored_checks_mode(self, monkeypatch):
        """_all_outputs_reference_and_stored returns False for mixed modes."""
        job = _create_test_job(
            output_transmission_modes={
                "dem": "reference",
                "slope": "value",
            },
        )

        # Mock provider check
        monkeypatch.setattr(
            job,
            "_require_provider_process_context",
            lambda: ("test-provider", "test-process"),
        )

        result = job._all_outputs_reference_and_stored()
        # Should be False because not all outputs are reference
        assert result is False

    def test_all_outputs_reference_and_stored_checks_storage(self, monkeypatch):
        """_all_outputs_reference_and_stored returns False if storage!=geoserver."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference"},
        )

        # Mock provider check
        monkeypatch.setattr(
            job,
            "_require_provider_process_context",
            lambda: ("test-provider", "test-process"),
        )

        # Mock that storage is NOT geoserver
        mock_providers = MagicMock()
        mock_providers.check_result_storage.return_value = "remote"
        monkeypatch.setattr(
            "ump.api.models.job.providers",
            mock_providers,
        )

        result = job._all_outputs_reference_and_stored()
        # Should be False because storage is "remote"
        assert result is False

    def test_all_outputs_reference_and_stored_true_case(self, monkeypatch):
        """_all_outputs_reference_and_stored returns True for valid case."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "reference"},
        )

        # Mock provider check
        monkeypatch.setattr(
            job,
            "_require_provider_process_context",
            lambda: ("test-provider", "test-process"),
        )

        # Mock that storage IS geoserver
        mock_providers = MagicMock()
        mock_providers.check_result_storage.return_value = "geoserver"
        monkeypatch.setattr(
            "ump.api.models.job.providers",
            mock_providers,
        )

        # Mock Geoserver so has_results_for_job returns True (no real DB)
        mock_geoserver = MagicMock()
        mock_geoserver.has_results_for_job.return_value = True
        monkeypatch.setattr(
            "ump.api.models.job.Geoserver",
            lambda: mock_geoserver,
        )

        result = job._all_outputs_reference_and_stored()
        # Should be True: all outputs are reference AND storage is geoserver
        assert result is True

    def test_build_cached_reference_results_format(self, monkeypatch):
        """_build_cached_reference_results returns proper OGC format."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "reference"},
        )

        # Mock job ID extraction
        monkeypatch.setattr(job, "_require_job_id", lambda: "job-test-123")

        # Mock storage job ID building
        def mock_build_storage(job_id, output_id):
            return f"{job_id}__{output_id}"

        monkeypatch.setattr(
            job,
            "_build_storage_job_id",
            mock_build_storage,
        )

        # Mock GeoServer URL builder
        mock_geoserver = MagicMock()
        mock_geoserver.get_layer_reference_url.side_effect = lambda storage_id: (
            f"http://geoserver/wfs?layer={storage_id}"
        )

        monkeypatch.setattr(
            "ump.api.models.job.Geoserver",
            lambda: mock_geoserver,
        )

        result = job._build_cached_reference_results()

        # Should have both outputs
        assert "dem" in result
        assert "slope" in result

        # Each should be OGC link.yaml format
        for output_id, link in result.items():
            assert "href" in link
            assert "rel" in link
            assert "type" in link
            assert "title" in link
            assert "http://geoserver" in link["href"]
            assert link["rel"] == "http://www.opengis.net/def/rel/ogc/1.0/results"
            assert link["type"] == "application/geo+json"

    def test_build_reference_link_for_output_returns_cached_for_successful(
        self,
        monkeypatch,
    ):
        """For successful jobs, return cached link without re-ingesting."""
        job = _create_test_job(
            status="successful",  # Already persisted
        )

        monkeypatch.setattr(
            job,
            "_require_provider_process_context",
            lambda: ("test-provider", "test-process"),
        )

        monkeypatch.setattr(job, "_require_job_id", lambda: "job-test-123")

        monkeypatch.setattr(
            job,
            "_build_storage_job_id",
            lambda job_id, output_id: f"{job_id}__{output_id}",
        )

        # Mock GeoServer
        mock_geoserver = MagicMock()
        mock_geoserver.get_layer_reference_url.return_value = (
            "http://geoserver/wfs?layer=job-test-123__dem"
        )
        monkeypatch.setattr(
            "ump.api.models.job.Geoserver",
            lambda: mock_geoserver,
        )

        # Mock providers to say geoserver is available
        mock_providers = MagicMock()
        mock_providers.check_result_storage.return_value = "geoserver"
        monkeypatch.setattr(
            "ump.api.models.job.providers",
            mock_providers,
        )

        # Mock save methods to prove they're NOT called
        save_results_mock = MagicMock()
        monkeypatch.setattr(
            job,
            "_store_flatgeobuf_reference_output",
            save_results_mock,
        )

        # Call with some dummy data
        result = job._build_reference_link_for_output(
            "dem",
            {"type": "FeatureCollection", "features": []},
        )

        # Should return a link
        assert result is not None
        assert "href" in result
        assert "http://geoserver" in result["href"]

        # Should NOT have called save (cached!)
        save_results_mock.assert_not_called()
