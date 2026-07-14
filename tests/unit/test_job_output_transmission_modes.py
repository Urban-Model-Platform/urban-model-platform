"""Tests for Job output transmission mode persistence."""

import asyncio
import base64
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from ump.api.models.job import Job


def test_job_stores_output_transmission_modes():
    """Job._to_dict() serializes output_transmission_modes to JSON."""
    job = Job.__new__(Job)
    job.output_transmission_modes = {
        "output_a": "value",
        "output_b": "reference",
    }
    job.process_id = "test-process"
    job.job_id = "job-123"
    job.remote_job_id = "remote-456"
    job.provider_prefix = "provider"
    job.provider_url = "http://example.com"
    job.status = "accepted"
    job.message = ""
    job.created = None
    job.started = None
    job.finished = None
    job.updated = None
    job.progress = 0
    job.parameters = {}
    job.results_metadata = {}
    job.user_id = None
    job.process_title = None
    job.name = None
    job.process_version = None
    job.transmission_mode = "value"

    result = job._to_dict()

    assert "output_transmission_modes" in result
    # Should be JSON string
    serialized = result["output_transmission_modes"]
    assert isinstance(serialized, str)
    # Should deserialize correctly
    deserialized = json.loads(serialized)
    assert deserialized == {
        "output_a": "value",
        "output_b": "reference",
    }


def test_job_init_from_dict_loads_output_transmission_modes():
    """Job._init_from_dict() deserializes output_transmission_modes."""
    job = Job.__new__(Job)
    job.output_transmission_modes = {}  # Initialize

    data = {
        "job_id": "job-123",
        "remote_job_id": "remote-456",
        "process_id": "test-process",
        "provider_prefix": "provider",
        "provider_url": "http://example.com",
        "process_id_with_prefix": "provider:test-process",
        "status": "accepted",
        "message": "",
        "created": None,
        "started": None,
        "finished": None,
        "updated": None,
        "progress": 0,
        "parameters": "{}",
        "results_metadata": "{}",
        "user_id": None,
        "process_title": None,
        "name": None,
        "process_version": None,
        "transmission_mode": "value",
        "output_transmission_modes": json.dumps(
            {
                "output_a": "value",
                "output_b": "reference",
            }
        ),
    }

    job._init_from_dict(data)

    assert job.output_transmission_modes == {
        "output_a": "value",
        "output_b": "reference",
    }


def test_job_init_from_dict_handles_missing_output_transmission_modes():
    """Job._init_from_dict() gracefully handles missing field (backward compat)."""
    job = Job.__new__(Job)
    job.output_transmission_modes = {}  # Initialize

    data = {
        "job_id": "job-123",
        "remote_job_id": "remote-456",
        "process_id": "test-process",
        "provider_prefix": "provider",
        "provider_url": "http://example.com",
        "process_id_with_prefix": "provider:test-process",
        "status": "accepted",
        "message": "",
        "created": None,
        "started": None,
        "finished": None,
        "updated": None,
        "progress": 0,
        "parameters": "{}",
        "results_metadata": "{}",
        "user_id": None,
        "process_title": None,
        "name": None,
        "process_version": None,
        "transmission_mode": "value",
        # output_transmission_modes not in data
    }

    job._init_from_dict(data)

    # Should default to empty dict
    assert job.output_transmission_modes == {}


def test_job_init_from_dict_handles_invalid_json():
    """Job._init_from_dict() handles corrupted JSON gracefully."""
    job = Job.__new__(Job)
    job.output_transmission_modes = {}  # Initialize

    data = {
        "job_id": "job-123",
        "remote_job_id": "remote-456",
        "process_id": "test-process",
        "provider_prefix": "provider",
        "provider_url": "http://example.com",
        "process_id_with_prefix": "provider:test-process",
        "status": "accepted",
        "message": "",
        "created": None,
        "started": None,
        "finished": None,
        "updated": None,
        "progress": 0,
        "parameters": "{}",
        "results_metadata": "{}",
        "user_id": None,
        "process_title": None,
        "name": None,
        "process_version": None,
        "transmission_mode": "value",
        "output_transmission_modes": "{ invalid json",
    }

    job._init_from_dict(data)

    # Should fall back to empty dict
    assert job.output_transmission_modes == {}


# ============================================================================
# Tests for per-output transmission mode delivery (results())
# ============================================================================


def _create_test_job(
    status="successful",
    transmission_mode="value",
    output_transmission_modes=None,
):
    """Helper to create a test Job instance without DB access."""
    job = Job.__new__(Job)
    job.job_id = "job-test-123"
    job.remote_job_id = "remote-456"
    job.process_id = "test-process"
    job.provider_prefix = "test-provider"
    job.provider_url = "http://modelserver:5000/"
    job.status = status
    job.message = ""
    # Type: Literal["value", "reference"]
    object.__setattr__(job, "transmission_mode", transmission_mode)
    job.output_transmission_modes = output_transmission_modes or {}
    return job


class TestApplyPerOutputTransmissionModes:
    """Tests for _apply_per_output_transmission_modes()."""

    def test_all_value_mode_returns_inline_data(self):
        """All outputs with value mode return inline data unchanged."""
        job = _create_test_job(
            output_transmission_modes={"dem": "value", "slope": "value"}
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
        }

        result = job._apply_per_output_transmission_modes(inline_results)

        assert result == inline_results

    def test_mixed_modes_returns_mixed_document(self):
        """Mixed value/reference modes return appropriate format for each."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "value"}
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
        }

        # Mock the reference link builder
        with patch.object(
            job,
            "_build_reference_link_for_output",
            return_value={
                "href": "http://geoserver/wfs?layer=job-test-123",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": "application/geo+json",
                "title": "Result 'dem' for job job-test-123",
            },
        ) as mock_build:
            result = job._apply_per_output_transmission_modes(inline_results)

        # dem should be reference link
        assert "href" in result["dem"]
        assert result["dem"]["href"] == "http://geoserver/wfs?layer=job-test-123"

        # slope should be inline
        assert result["slope"] == inline_results["slope"]

        # Only dem should have triggered reference link building
        mock_build.assert_called_once_with("dem", inline_results["dem"])

    def test_reference_fallback_on_build_failure(self):
        """Reference mode falls back to inline if link build fails."""
        job = _create_test_job(output_transmission_modes={"dem": "reference"})

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
        }

        # Mock reference link builder to return None (failure)
        with patch.object(
            job,
            "_build_reference_link_for_output",
            return_value=None,
        ):
            result = job._apply_per_output_transmission_modes(inline_results)

        # Should fall back to inline data
        assert result["dem"] == inline_results["dem"]

    def test_unspecified_output_defaults_to_value(self):
        """Outputs not in output_transmission_modes default to value mode."""
        job = _create_test_job(
            output_transmission_modes={"dem": "reference"}  # slope not specified
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
        }

        with patch.object(
            job,
            "_build_reference_link_for_output",
            return_value={"href": "http://geoserver/wfs"},
        ):
            result = job._apply_per_output_transmission_modes(inline_results)

        # slope should be inline (default to value)
        assert result["slope"] == inline_results["slope"]

    def test_non_dict_inline_results_returned_unchanged(self):
        """Non-dict inline results are returned as-is with warning."""
        job = _create_test_job(output_transmission_modes={"dem": "reference"})

        # Edge case: inline_results is not a dict (type: ignore for test)
        inline_results = "not a dict"  # type: ignore[assignment]

        result = job._apply_per_output_transmission_modes(inline_results)  # type: ignore[arg-type]

        assert result == "not a dict"

    def test_single_output_geojson_payload_is_normalized_and_referenced(self):
        """Unwrapped single-output GeoJSON is wrapped using output id."""
        job = _create_test_job(output_transmission_modes={"result": "reference"})

        # Remote provider returns a direct FeatureCollection, not
        # {'result': <FeatureCollection>}.
        inline_results = {
            "type": "FeatureCollection",
            "features": [{"id": 1}],
        }

        with patch.object(
            job,
            "_build_reference_link_for_output",
            return_value={"href": "http://geoserver/layer"},
        ) as mock_build:
            result = job._apply_per_output_transmission_modes(inline_results)

        assert result == {"result": {"href": "http://geoserver/layer"}}
        mock_build.assert_called_once_with("result", inline_results)


class TestBuildReferenceLinkForOutput:
    """Tests for _build_reference_link_for_output().

    Note: Jobs are created with status="accepted" (not "successful")
    because the optimization skips re-ingesting for successful jobs.
    These tests verify the ingest logic, so they use accepted status.
    """

    def test_returns_ogc_compliant_link(self):
        """Returns OGC link.yaml compliant object."""
        job = _create_test_job(status="accepted")  # Must ingest

        with (
            patch(
                "ump.api.models.job.providers.check_result_storage",
                return_value="geoserver",
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
        ):
            mock_geoserver = MagicMock()
            mock_geoserver.get_layer_reference_url.return_value = (
                "http://geoserver:8080/wfs?service=WFS&request=GetFeature"
            )
            mock_geoserver_class.return_value = mock_geoserver

            result = job._build_reference_link_for_output(
                "dem",
                {"type": "FeatureCollection", "features": []},
            )

        # OGC link.yaml required fields
        assert result is not None
        assert "href" in result
        assert (
            result["href"] == "http://geoserver:8080/wfs?service=WFS&request=GetFeature"
        )

        # OGC recommended fields
        assert result["rel"] == "http://www.opengis.net/def/rel/ogc/1.0/results"
        assert result["type"] == "application/geo+json"
        assert "title" in result
        assert "dem" in result["title"]
        mock_geoserver.save_results.assert_called_once()

    def test_returns_none_for_non_geoserver_storage(self):
        """Returns None if result-storage is not geoserver."""
        job = _create_test_job()

        with patch(
            "ump.api.models.job.providers.check_result_storage",
            return_value="remote",
        ):
            result = job._build_reference_link_for_output(
                "dem",
                {"type": "FeatureCollection", "features": []},
            )

        assert result is None

    def test_returns_none_on_exception(self):
        """Returns None and logs error on exception."""
        job = _create_test_job()
        # Will cause _require_provider_process_context to fail
        job.provider_prefix = None

        result = job._build_reference_link_for_output(
            "dem",
            {"type": "FeatureCollection", "features": []},
        )

        assert result is None

    def test_flatgeobuf_media_type_is_preserved_in_reference_link(self):
        """Reference link keeps flatgeobuf media type when detected."""
        job = _create_test_job(status="accepted")  # Must ingest

        with (
            patch(
                "ump.api.models.job.providers.check_result_storage",
                return_value="geoserver",
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
            patch.object(
                job,
                "_store_flatgeobuf_reference_output",
                return_value=True,
            ) as mock_store_fgb,
        ):
            mock_geoserver = MagicMock()
            mock_geoserver.get_layer_reference_url.return_value = (
                "http://geoserver:8080/wfs?service=WFS&request=GetFeature"
            )
            mock_geoserver_class.return_value = mock_geoserver

            result = job._build_reference_link_for_output(
                "dem",
                {
                    "type": "application/vnd.flatgeobuf",
                    "href": "http://model/results.fgb",
                },
            )

        assert result is not None
        assert result["type"] == "application/vnd.flatgeobuf"
        mock_store_fgb.assert_called_once()


class TestFlatGeobufExtraction:
    """Tests for FlatGeobuf extraction and storage helpers."""

    def test_store_flatgeobuf_from_link_uses_url_ingestion(self):
        job = _create_test_job()
        geoserver = MagicMock()

        result = job._store_flatgeobuf_reference_output(
            geoserver,
            "job-test-123__dem",
            {
                "href": "http://remote/results.fgb",
                "type": "application/vnd.flatgeobuf",
            },
        )

        assert result is True
        geoserver.save_flatgeobuf_results.assert_called_once_with(
            "job-test-123__dem",
            "http://remote/results.fgb",
        )

    def test_extract_flatgeobuf_from_base64_dict(self):
        job = _create_test_job()
        encoded = base64.b64encode(b"abc123").decode("ascii")

        result = job._extract_flatgeobuf_bytes(
            {
                "type": "application/vnd.flatgeobuf",
                "data": encoded,
            }
        )

        assert result == b"abc123"

    def test_extract_flatgeobuf_from_direct_bytes(self):
        job = _create_test_job()
        payload = b"binary-fgb"

        result = job._extract_flatgeobuf_bytes(payload)

        assert result == payload

    def test_store_flatgeobuf_from_inline_base64_uses_byte_ingestion(self):
        job = _create_test_job()
        geoserver = MagicMock()
        encoded = base64.b64encode(b"abc123").decode("ascii")

        result = job._store_flatgeobuf_reference_output(
            geoserver,
            "job-test-123__dem",
            {
                "type": "application/vnd.flatgeobuf",
                "data": encoded,
            },
        )

        assert result is True
        geoserver.save_flatgeobuf_bytes.assert_called_once_with(
            "job-test-123__dem",
            b"abc123",
        )

    def test_detect_media_type_from_href_extension(self):
        job = _create_test_job()

        media_type = job._detect_output_media_type({"href": "http://x/out.fgb"})

        assert media_type == "application/vnd.flatgeobuf"


class TestResultsWithPerOutputModes:
    """Integration tests for results() with per-output transmission modes."""

    def test_results_with_mixed_modes(self):
        """results() returns mixed document when output modes differ."""

        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "value"}
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": []},
            "slope": {"type": "FeatureCollection", "features": []},
        }

        with (
            patch.object(
                job, "_fetch_inline_results", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                job,
                "_build_reference_link_for_output",
                return_value={"href": "http://geoserver/layer"},
            ),
        ):
            mock_fetch.return_value = inline_results

            result = asyncio.run(job.results())

        # dem should be reference
        assert "href" in result["dem"]

        # slope should be inline
        assert result["slope"] == inline_results["slope"]

    def test_results_legacy_fallback_without_output_modes(self):
        """results() falls back to global mode when output_transmission_modes empty."""

        job = _create_test_job(
            transmission_mode="reference",
            output_transmission_modes={},  # Empty = use legacy behavior
        )

        with (
            patch.object(
                job, "_fetch_inline_results", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                job,
                "_build_reference_result",
                return_value={"result": {"href": "http://geoserver/layer"}},
            ) as mock_legacy,
        ):
            mock_fetch.return_value = {"result": {"features": []}}

            result = asyncio.run(job.results())

        # Should use legacy _build_reference_result
        mock_legacy.assert_called_once()
        assert result == {"result": {"href": "http://geoserver/layer"}}

    def test_results_all_value_returns_inline(self):
        """results() with all value modes returns inline data."""

        job = _create_test_job(
            output_transmission_modes={"dem": "value", "slope": "value"}
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
        }

        with patch.object(
            job, "_fetch_inline_results", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = inline_results

            result = asyncio.run(job.results())

        assert result == inline_results

    def test_results_defaults_to_value_for_unspecified_outputs(self):
        """Outputs not in output_transmission_modes default to value (inline)."""

        # Only dem has explicit mode, slope is not specified
        job = _create_test_job(output_transmission_modes={"dem": "reference"})

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
            "metadata": {"version": "1.0"},
        }

        with (
            patch.object(
                job, "_fetch_inline_results", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                job,
                "_build_reference_link_for_output",
                return_value={"href": "http://geoserver/layer"},
            ),
        ):
            mock_fetch.return_value = inline_results

            result = asyncio.run(job.results())

        # dem should be reference (explicitly set)
        assert "href" in result["dem"]

        # slope and metadata should be inline (default to value)
        assert result["slope"] == inline_results["slope"]
        assert result["metadata"] == inline_results["metadata"]

    def test_results_graceful_fallback_when_reference_fails(self):
        """Reference mode falls back to inline when GeoServer unavailable."""

        job = _create_test_job(
            output_transmission_modes={"dem": "reference", "slope": "reference"}
        )

        inline_results = {
            "dem": {"type": "FeatureCollection", "features": [{"id": 1}]},
            "slope": {"type": "FeatureCollection", "features": [{"id": 2}]},
        }

        # Simulate dem succeeds, slope fails
        def _build_ref_side_effect(output_id, _output_data):
            if output_id == "dem":
                return {"href": "http://geoserver/dem"}
            return None  # slope fails

        with (
            patch.object(
                job, "_fetch_inline_results", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                job,
                "_build_reference_link_for_output",
                side_effect=_build_ref_side_effect,
            ),
        ):
            mock_fetch.return_value = inline_results

            result = asyncio.run(job.results())

        # dem should be reference (success)
        assert "href" in result["dem"]
        assert result["dem"]["href"] == "http://geoserver/dem"

        # slope should fall back to inline (reference failed)
        assert result["slope"] == inline_results["slope"]

    def test_results_normalizes_unwrapped_single_output_payload(self):
        """results() wraps unwrapped single-output payload before mode handling."""

        job = _create_test_job(output_transmission_modes={"result": "reference"})

        inline_results = {
            "type": "FeatureCollection",
            "features": [{"id": 1}],
        }

        with (
            patch.object(
                job, "_fetch_inline_results", new_callable=AsyncMock
            ) as mock_fetch,
            patch.object(
                job,
                "_build_reference_link_for_output",
                return_value={"href": "http://geoserver/layer"},
            ) as mock_build,
        ):
            mock_fetch.return_value = inline_results
            result = asyncio.run(job.results())

        assert result == {"result": {"href": "http://geoserver/layer"}}
        mock_build.assert_called_once_with("result", inline_results)


class TestResultsToGeoserverHardening:
    """Hardening tests for results_to_geoserver()."""

    def test_normalizes_unwrapped_single_output_before_persistence(self):
        """Unwrapped output payload is normalized and stored under output id."""
        job = _create_test_job(
            status="successful",
            transmission_mode="reference",
            output_transmission_modes={"result": "reference"},
        )
        job.process_id_with_prefix = "test-provider:test-process"

        inline_results = {
            "data": base64.b64encode(b"fgb-bytes").decode("ascii"),
            "encoding": "base64",
            "title": "flatgeobuf",
            "type": "application/x-flatgeobuf",
        }

        provider = SimpleNamespace(
            processes={"test-process": SimpleNamespace(result_path=None)}
        )

        with (
            patch(
                "ump.api.models.job.providers.get_providers",
                return_value={"test-provider": provider},
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
            patch.object(
                job,
                "_fetch_inline_results",
                new_callable=AsyncMock,
                return_value=inline_results,
            ),
            patch.object(
                job,
                "_store_flatgeobuf_reference_output",
                return_value=True,
            ) as mock_store,
        ):
            mock_geoserver_class.return_value = MagicMock()
            stored = asyncio.run(job.results_to_geoserver())

        assert stored is True
        mock_store.assert_called_once_with(
            mock_geoserver_class.return_value,
            "job-test-123-result",
            inline_results,
        )

    def test_returns_false_when_reference_outputs_cannot_be_persisted(self):
        """Method reports failure instead of succeeding silently."""
        job = _create_test_job(
            status="successful",
            transmission_mode="reference",
            output_transmission_modes={"result": "reference"},
        )
        job.process_id_with_prefix = "test-provider:test-process"

        provider = SimpleNamespace(
            processes={"test-process": SimpleNamespace(result_path=None)}
        )
        wrapped_results = {
            "result": {
                "data": base64.b64encode(b"fgb-bytes").decode("ascii"),
                "encoding": "base64",
                "type": "application/x-flatgeobuf",
            }
        }

        with (
            patch(
                "ump.api.models.job.providers.get_providers",
                return_value={"test-provider": provider},
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
            patch.object(
                job,
                "_fetch_inline_results",
                new_callable=AsyncMock,
                return_value=wrapped_results,
            ),
            patch.object(
                job,
                "_store_flatgeobuf_reference_output",
                return_value=False,
            ),
        ):
            mock_geoserver_class.return_value = MagicMock()
            stored = asyncio.run(job.results_to_geoserver())

        assert stored is False


class TestReferenceCacheValidation:
    """Tests ensuring reference links are only returned for persisted outputs."""

    def test_all_outputs_reference_and_stored_requires_persisted_data(self):
        job = _create_test_job(
            status="successful",
            transmission_mode="reference",
            output_transmission_modes={"result": "reference"},
        )

        with (
            patch(
                "ump.api.models.job.providers.check_result_storage",
                return_value="geoserver",
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
        ):
            mock_geoserver = MagicMock()
            mock_geoserver.has_results_for_job.return_value = False
            mock_geoserver_class.return_value = mock_geoserver

            result = job._all_outputs_reference_and_stored()

        assert result is False

    def test_build_reference_link_falls_back_to_ingest_when_cache_missing(self):
        job = _create_test_job(
            status="successful",
            transmission_mode="reference",
            output_transmission_modes={"result": "reference"},
        )

        output_data = {"type": "FeatureCollection", "features": []}

        with (
            patch(
                "ump.api.models.job.providers.check_result_storage",
                return_value="geoserver",
            ),
            patch("ump.api.models.job.Geoserver") as mock_geoserver_class,
        ):
            mock_geoserver = MagicMock()
            mock_geoserver.has_results_for_job.return_value = False
            mock_geoserver.get_layer_reference_url.return_value = (
                "http://geoserver:8080/wfs?service=WFS&request=GetFeature"
            )
            mock_geoserver_class.return_value = mock_geoserver

            result = job._build_reference_link_for_output("result", output_data)

        assert result is not None
        assert result["href"].startswith("http://geoserver:8080/wfs")
        mock_geoserver.save_results.assert_called_once()
