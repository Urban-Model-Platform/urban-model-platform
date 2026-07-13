import base64
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, NoReturn

import aiohttp
import geopandas as gpd

import ump.api.providers as providers
from ump.api import remote_auth
from ump.api.db_handler import DBHandler
from ump.api.models.flatgeobuf_ingest import (
    build_storage_job_id,
    extract_flatgeobuf_bytes,
    store_flatgeobuf_ingest,
    try_decode_base64,
)
from ump.api.models.job_status import JobStatus
from ump.api.models.ogc_exception import OGCExceptionResponse
from ump.api.models.output_media_type import (
    FLATGEOBUF_MEDIA_TYPES,
    GEOJSON_MEDIA_TYPES,
    detect_output_media_type,
)
from ump.api.models.providers_config import ProcessConfig, ProviderConfig
from ump.config import app_settings as config
from ump.errors import InvalidUsage, OGCProcessException
from ump.geoserver.geoserver import Geoserver
from ump.utils import join_url_parts

logger = logging.getLogger(__name__)

# OGC API Processes - Part 1: Core (Clause 7.11.2.5)
# https://docs.ogc.org/is/18-062r2/18-062r2.html#toc32
TransmissionMode = Literal["value", "reference"]

results_client_timeout = aiohttp.ClientTimeout(
    total=5,  # Set a reasonable timeout for the requests
    connect=2,  # Connection timeout
    sock_connect=2,  # Socket connection timeout
    sock_read=5,  # Socket read timeout
)


# TODO class violates Single Responsibility Principle (SRP), it mixes
# business logic with data access logic and metadata handling
# TODO methods like insert use redundant fields instead of job instance fields
# TODO queries use raw SQL, which is not recommended for different reasons:
# no migrations, reduced maintainability
# TODO: table schema is missing normalization
class Job:
    DISPLAYED_ATTRIBUTES = [
        "processID",
        "type",
        "jobID",
        "status",
        "message",
        "created",
        "started",
        "finished",
        "updated",
        "progress",
        "links",
    ]

    SORTABLE_COLUMNS = [
        "created",
        "finished",
        "updated",
        "started",
        "process_id",
        "status",
        "message",
    ]

    def __init__(self, job_id=None, user=None):
        self.job_id = job_id
        self.status = None
        self.message = ""
        self.progress = 0
        self.created = None
        self.started = None
        self.finished = None
        self.updated = None
        self.results_metadata = {}
        self.user_id = None
        self.name = None
        self.process_title = None
        self.process_version = None
        self.remote_job_id = None
        self.process_id_with_prefix = None
        self.parameters = None
        self.provider_prefix: str | None = None
        self.process_id: str | None = None
        self.provider_url = None
        self.transmission_mode: TransmissionMode = "value"
        self.output_transmission_modes: dict[str, TransmissionMode] = {}

        # TODO: this produces 404 if a job is beeing queried for which was
        # stored with a user id, consider to distinguish between
        # 404, 401 and 403 here
        if job_id and not self._init_from_db(job_id, user):
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job",
                    title="Job not found",
                    detail="The job with the given ID does not exist.",
                    status=404,
                    instance="/".join(
                        [
                            config.UMP_API_SERVER_URL,
                            f"{config.UMP_API_SERVER_URL_PREFIX}",
                            "jobs",
                            job_id,
                        ]
                    ),
                )
            )

    def insert(
        self,
        job_id=None,
        remote_job_id=None,
        process_id_with_prefix=None,
        process_title=None,
        name=None,
        exec_body=None,
        user=None,
        process_version=None,
        transmission_mode=None,
        output_transmission_modes=None,
    ):
        self._set_attributes(
            job_id,
            remote_job_id,
            process_id_with_prefix,
            process_title,
            name,
            exec_body,
            user_id=user,
            process_version=process_version,
            transmission_mode=transmission_mode,
        )

        # Store per-output transmission modes (preserved from execute request)
        if output_transmission_modes is not None:
            self.output_transmission_modes = dict(output_transmission_modes)

        # TODO: these metadata should come from remote job
        # instead of being set here
        # because the remote server ultimately decides if a job was accepted!
        self.status = JobStatus.accepted.value
        self.created = datetime.now(timezone.utc)
        self.updated = datetime.now(timezone.utc)

        # TODO: we need proper normalization here
        # TODO: we need a SQL model for this, raw queries are error prone
        query = """
            INSERT INTO jobs
            (
                job_id, remote_job_id, process_id,
                provider_prefix, provider_url, status,
                progress, parameters, message, created,
                started, finished, updated, user_id,
                process_title, name, process_version, hash,
                transmission_mode, output_transmission_modes
            )
            VALUES
            (
                %(job_id)s,
                %(remote_job_id)s,
                %(process_id)s,
                %(provider_prefix)s,
                %(provider_url)s,
                %(status)s,
                %(progress)s,
                %(parameters)s,
                %(message)s,
                %(created)s,
                %(started)s,
                %(finished)s,
                %(updated)s,
                %(user_id)s,
                %(process_title)s,
                %(name)s,
                %(process_version)s,
                %(hash)s,
                %(transmission_mode)s,
                %(output_transmission_modes)s
            )
        """
        params = self._to_dict()
        raw = (
            (params.get("parameters") or "")
            + (params.get("process_version") or "")
            + (params.get("user_id") or "")
        )
        params["hash"] = base64.b64encode(
            hashlib.sha512(raw.encode("utf-8")).digest()
        ).decode("ascii")

        with DBHandler() as db:
            db.run_query(query, query_params=params)

        logging.info(" --> Job %s for %s created.", self.job_id, self.process_id)

    def _set_attributes(
        self,
        job_id=None,
        remote_job_id=None,
        process_id_with_prefix=None,
        process_title=None,
        name=None,
        parameters=None,
        user_id=None,
        process_version=None,
        transmission_mode=None,
    ):
        self.job_id = job_id
        self.remote_job_id = remote_job_id
        self.user_id = user_id
        self.process_title = process_title
        self.name = name
        self.process_version = process_version
        self.transmission_mode = transmission_mode or "value"

        if remote_job_id and not job_id:
            self.job_id = f"job-{remote_job_id}"

        if job_id and not remote_job_id:
            match = re.search("job-(.*)$", job_id)
            if match is None:
                raise InvalidUsage(f"Job ID {job_id} is not valid.")
            self.remote_job_id = match.group(1)

        self.process_id_with_prefix = process_id_with_prefix
        self.parameters = parameters

        if process_id_with_prefix:
            match = re.search(r"(.*):(.*)", process_id_with_prefix)
            if not match:
                raise InvalidUsage(
                    f"Process ID {self.process_id_with_prefix} is not known! "
                    + (
                        "Please check endpoint api/processes for a list of "
                        "available processes."
                    )
                )

            self.provider_prefix = match.group(1)
            self.process_id = match.group(2)
            if self.provider_prefix is None:
                raise InvalidUsage("Provider prefix could not be resolved.")
            self.provider_url = providers.get_providers()[
                self.provider_prefix
            ].server_url

        if not self.job_id:
            self.job_id = str(uuid.uuid4())

    def _init_from_db(self, job_id, user):
        query = """
            SELECT j.*
            FROM jobs j
            LEFT JOIN jobs_users u ON j.job_id = u.job_id
            WHERE j.job_id = %(job_id)s
        """
        if user is None:
            query += " and j.user_id is null"
        else:
            query += f" and (j.user_id = '{user}' or u.user_id = '{user}')"

        with DBHandler() as db:
            job_details = db.run_query(query, query_params={"job_id": job_id}) or []

        if len(job_details) > 0:
            self._init_from_dict(dict(job_details[0]))
            return True
        return False

    def _init_from_dict(self, data):
        self.job_id = data["job_id"]
        self.remote_job_id = data["remote_job_id"]
        self.process_id = data["process_id"]
        self.provider_prefix = data["provider_prefix"]
        self.provider_url = data["provider_url"]
        self.process_id_with_prefix = f"{data['provider_prefix']}:{data['process_id']}"
        self.status = data["status"]
        self.message = data["message"]
        self.created = data["created"]
        self.started = data["started"]
        self.finished = data["finished"]
        self.updated = data["updated"]
        self.progress = data["progress"]
        self.parameters = data["parameters"]
        self.results_metadata = data["results_metadata"]
        self.user_id = data["user_id"]
        self.process_title = data["process_title"]
        self.name = data["name"]
        self.process_version = data["process_version"]

        # The DB CHECK constraint guarantees a valid value; fall back to the
        # OGC default for legacy rows that may pre-date the migration.
        self.transmission_mode = data.get("transmission_mode") or "value"

        # Load per-output transmission modes from serialized JSON.
        # For backward compatibility with jobs created before this field existed,
        # an empty dict is used as default.
        output_modes_raw = data.get("output_transmission_modes")
        if output_modes_raw:
            try:
                self.output_transmission_modes = json.loads(output_modes_raw)
            except (json.JSONDecodeError, TypeError):
                # Fall back to empty dict if deserialization fails
                self.output_transmission_modes = {}
        else:
            self.output_transmission_modes = {}

    def _raise_invalid_job_state(self, detail: str) -> NoReturn:
        raise OGCProcessException(
            OGCExceptionResponse(
                type=(
                    "http://www.opengis.net/def/exceptions/"
                    "ogcapi-processes-1/1.0/server-error"
                ),
                title="Invalid job state",
                detail=detail,
                status=500,
                instance=(
                    f"{config.UMP_API_SERVER_URL}/"
                    f"{config.UMP_API_SERVER_URL_PREFIX}/jobs/"
                    f"{self.job_id or 'unknown'}"
                ),
            )
        )

    def _require_provider_process_context(self) -> tuple[str, str]:
        if self.provider_prefix is None or self.process_id is None:
            self._raise_invalid_job_state(
                "The job is missing provider or process metadata required "
                "to resolve result delivery."
            )
        return self.provider_prefix, self.process_id

    def _require_job_id(self) -> str:
        if self.job_id is None:
            self._raise_invalid_job_state("The job is missing its local identifier.")
        return self.job_id

    def _require_remote_execution_context(self) -> tuple[str, str]:
        if self.provider_prefix is None or self.remote_job_id is None:
            self._raise_invalid_job_state(
                "The job is missing provider or remote job metadata required "
                "to fetch inline results."
            )
        return self.provider_prefix, self.remote_job_id

    def _to_dict(self):
        return {
            "process_id": self.process_id,
            "job_id": self.job_id,
            "remote_job_id": self.remote_job_id,
            "provider_prefix": self.provider_prefix,
            "provider_url": str(self.provider_url),
            "status": self.status,
            "message": self.message,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "updated": self.updated,
            "progress": self.progress,
            "process_title": self.process_title,
            "name": self.name,
            "parameters": json.dumps(self.parameters),
            "results_metadata": json.dumps(self.results_metadata),
            "user_id": self.user_id,
            "process_version": self.process_version,
            "transmission_mode": self.transmission_mode,
            "output_transmission_modes": json.dumps(self.output_transmission_modes),
        }

    def update(self):
        self.updated = datetime.now(timezone.utc)

        query = """
            UPDATE jobs SET
            (
                process_id,
                provider_prefix,
                provider_url,
                status,
                progress,
                parameters,
                message,
                created,
                started,
                finished,
                updated,
                results_metadata,
                process_version,
                output_transmission_modes
            )
            =
            (
                %(process_id)s,
                %(provider_prefix)s,
                %(provider_url)s,
                %(status)s,
                %(progress)s,
                %(parameters)s,
                %(message)s,
                %(created)s,
                %(started)s,
                %(finished)s,
                %(updated)s,
                %(results_metadata)s,
                %(process_version)s,
                %(output_transmission_modes)s
            )
            WHERE job_id = %(job_id)s
        """
        with DBHandler() as db:
            db.run_query(query, query_params=self._to_dict())

    def set_results_metadata(self, results_as_json):
        results_df = gpd.GeoDataFrame.from_features(results_as_json)

        minimal_values_df = results_df.min(numeric_only=True)
        maximal_values_df = results_df.max(numeric_only=True)

        minimal_values_dict = minimal_values_df.to_dict()
        maximal_values_dict = maximal_values_df.to_dict()

        types = results_df.dtypes.to_dict()

        values = []
        for column in maximal_values_dict:
            data_type = str(types[column])
            if (
                data_type == "float64"
                and results_df[column].apply(float.is_integer).all()
            ):
                data_type = "int"

            values.append(
                {
                    column: {
                        "type": data_type,
                        "min": minimal_values_dict[column],
                        "max": maximal_values_dict[column],
                    }
                }
            )

        for column in results_df.select_dtypes(include=[object]).to_dict():
            try:
                values.append(
                    {
                        column: {
                            "type": "string",
                            "values": list(set(results_df[column])),
                        }
                    }
                )
            except Exception as e:
                logging.error("Unable to store column %s, skipping: %s", column, e)

        self.results_metadata = {"values": values}

        return self.results_metadata

    def display(self, additional_metadata=False):
        job_dict = self._to_dict()
        job_dict["type"] = "process"
        job_dict["jobID"] = job_dict.pop("job_id")
        job_dict["parameters"] = self.parameters
        job_dict["results_metadata"] = self.results_metadata
        job_dict["processID"] = self.process_id_with_prefix
        job_dict["links"] = []

        for attr in job_dict:
            if isinstance(job_dict[attr], datetime):
                job_dict[attr] = job_dict[attr].strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        if self.status in (
            JobStatus.successful.value,
            JobStatus.running.value,
            JobStatus.accepted.value,
        ):
            job_result_url = join_url_parts(
                config.UMP_API_SERVER_URL,
                config.UMP_API_SERVER_URL_PREFIX,
                "jobs",
                f"{self.job_id}/results",
            )

            job_dict["links"] = [
                {
                    "href": job_result_url,
                    "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                    "type": "application/json",
                    "hreflang": "en",
                    "title": "Job result",
                }
            ]
        if isinstance(additional_metadata, str):
            additional_metadata = additional_metadata.lower() == "true"

        if additional_metadata:
            metadata = {}
            if self.name is not None:
                metadata["name"] = self.name
            if self.parameters is not None:
                metadata["parameters"] = self.parameters
            if self.results_metadata is not None:
                metadata["results_metadata"] = self.results_metadata
            if self.process_title is not None:
                metadata["process_title"] = self.process_title
            if self.process_version is not None:
                metadata["process_version"] = self.process_version
            if self.process_version is not None:
                metadata["user_id"] = self.user_id

            job_dict["metadata"] = metadata

            for key in [
                "name",
                "parameters",
                "results_metadata",
                "process_title",
                "process_version",
                "user_id",
            ]:
                job_dict.pop(key, None)

            return job_dict

        else:
            return {k: job_dict[k] for k in self.DISPLAYED_ATTRIBUTES}

    async def results(self):
        """Return job results following OGC API Processes transmission modes.

        Per OGC API Processes - Part 1: Core (Clause 7.13), each output can
        have its own transmission mode:

        - ``value``: Return the output data inline.
        - ``reference``: Return an OGC link.yaml object pointing to the
          stored representation (e.g. a GeoServer WFS layer).

        The per-output modes are determined from ``output_transmission_modes``
        (preserved from the original execute request). If not available,
        falls back to the global ``transmission_mode``.

        Optimization: For reference outputs that were already stored during job
        completion, we skip re-fetching from remote and return cached links directly.

        Returns:
            dict: An OGC results document where each output is either inline
            data (value mode) or a link.yaml object (reference mode).
        """
        if self.status != JobStatus.successful.value:
            self.results_not_available()

        # If no per-output modes stored, use legacy global behavior
        if not self.output_transmission_modes:
            if self.transmission_mode == "reference":
                reference = self._build_reference_result()
                if reference is not None:
                    return reference
            # For value mode, fetch from remote
            inline_results = await self._fetch_inline_results()
            return inline_results

        # For per-output modes: check if all outputs are reference mode
        # and already stored. If so, return cached links without re-fetching
        if self._all_outputs_reference_and_stored():
            return self._build_cached_reference_results()

        # Otherwise, fetch inline results and apply per-output modes
        inline_results = await self._fetch_inline_results()
        return self._apply_per_output_transmission_modes(inline_results)

    def _apply_per_output_transmission_modes(self, inline_results: dict) -> dict:
        """Transform inline results based on per-output transmission modes.

        For each output in inline_results:
        - If mode="reference": Return OGC link.yaml pointing to GeoServer
        - If mode="value" or unspecified: Return inline data

        Args:
            inline_results: Raw results fetched from remote server

        Returns:
            dict: OGC results document with mixed value/reference outputs
        """
        if not isinstance(inline_results, dict):
            logger.warning(
                "Job %s: inline_results is not a dict, returning as-is",
                self.job_id,
            )
            return inline_results

        # Some providers return a single output payload directly (e.g.
        # GeoJSON FeatureCollection) instead of an OGC results document
        # keyed by output id. If exactly one output mode exists, normalize
        # to a keyed results document so per-output mode handling can work.
        if len(self.output_transmission_modes) == 1:
            only_output_id = next(iter(self.output_transmission_modes.keys()))
            if (
                only_output_id not in inline_results
                and self._looks_like_single_output_payload(inline_results)
            ):
                logger.debug(
                    "Job %s: normalizing unwrapped single-output payload "
                    "to output id '%s'",
                    self.job_id,
                    only_output_id,
                )
                inline_results = {only_output_id: inline_results}

        result_document: dict = {}

        for output_id, output_data in inline_results.items():
            # Default to "value" per OGC API Processes spec
            output_mode = self.output_transmission_modes.get(output_id, "value")

            if output_mode == "reference":
                ref_link = self._build_reference_link_for_output(
                    output_id,
                    output_data,
                )
                if ref_link is not None:
                    result_document[output_id] = ref_link
                else:
                    # Fallback to inline if reference cannot be created
                    logger.warning(
                        "Job %s, output %s: reference mode requested but "
                        "unavailable, returning inline data",
                        self.job_id,
                        output_id,
                    )
                    result_document[output_id] = output_data
            else:
                # Value mode: return inline data
                result_document[output_id] = output_data

        return result_document

    @staticmethod
    def _looks_like_single_output_payload(payload: dict) -> bool:
        """Heuristically detect an unwrapped single-output payload.

        We only normalize when the payload shape resembles an actual output
        value (GeoJSON object or link/value wrapper), not a multi-output
        OGC results document.
        """
        if not isinstance(payload, dict):
            return False

        if "href" in payload:
            return True

        if "type" in payload and any(
            key in payload for key in ("features", "geometry", "coordinates")
        ):
            return True

        if any(key in payload for key in ("data", "value")):
            return True

        return False

    def _build_reference_link_for_output(
        self,
        output_id: str,
        output_data: Any,
    ) -> dict | None:
        """Build an OGC link.yaml object for a specific output.

        Per OGC API Processes link.yaml schema:
        - href (required): URL to the result resource
        - rel: Link relation type
        - type: Media type of the result
        - title: Human-readable title

        Optimization: If data was already stored during job completion
        (successful status), return cached GeoServer link without re-ingesting.

        Args:
            output_id: The output identifier
            output_data: Output data from remote fetch

        Returns:
            dict | None: OGC link.yaml object, or None if reference unavailable
        """
        try:
            provider_prefix, process_id = self._require_provider_process_context()
            result_storage = providers.check_result_storage(provider_prefix, process_id)

            if result_storage != "geoserver":
                logger.debug(
                    "Job %s, output %s: result-storage='%s' does not support "
                    "reference mode",
                    self.job_id,
                    output_id,
                    result_storage,
                )
                return None

            job_id = self._require_job_id()
            storage_job_id = self._build_storage_job_id(job_id, output_id)

            # Optimization: If job is successful (already persisted to
            # GeoServer during completion), just return cached link without
            # re-ingesting. This avoids expensive ogr2ogr+PostGIS operations
            # on every result retrieval for mixed reference/value outputs.
            if self.status == JobStatus.successful.value:
                logger.debug(
                    "Job %s, output %s: returning cached GeoServer link "
                    "(job already completed)",
                    self.job_id,
                    output_id,
                )
                geoserver = Geoserver()
                geoserver_url = geoserver.get_layer_reference_url(storage_job_id)
                return {
                    "href": geoserver_url,
                    "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                    "type": "application/geo+json",
                    "title": f"Result '{output_id}' for job {self.job_id}",
                }

            # If job is not yet complete, must ingest data now
            media_type = self._detect_output_media_type(output_data)
            geoserver = Geoserver()

            if media_type in FLATGEOBUF_MEDIA_TYPES:
                saved = self._store_flatgeobuf_reference_output(
                    geoserver,
                    storage_job_id,
                    output_data,
                )
                if not saved:
                    return None
            elif media_type in GEOJSON_MEDIA_TYPES:
                if isinstance(output_data, dict) and "features" in output_data:
                    geoserver.save_results(
                        job_id=storage_job_id,
                        data=output_data,
                    )
            else:
                logger.warning(
                    "Job %s, output %s: media type '%s' not supported for "
                    "GeoServer reference conversion",
                    self.job_id,
                    output_id,
                    media_type,
                )
                return None

            geoserver_url = geoserver.get_layer_reference_url(storage_job_id)

            # OGC link.yaml compliant object
            return {
                "href": geoserver_url,
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": media_type,
                "title": f"Result '{output_id}' for job {self.job_id}",
            }

        except Exception as e:
            logger.error(
                "Job %s, output %s: failed to build reference link: %s",
                self.job_id,
                output_id,
                e,
            )
            return None

    def _build_storage_job_id(self, job_id: str, output_id: str) -> str:
        """Create output-specific storage key while preserving old table schema."""
        return build_storage_job_id(job_id, output_id)

    def _detect_output_media_type(self, output_data: Any) -> str:
        """Detect output media type from OGC output payload.

        See :func:`~ump.api.models.output_media_type.detect_output_media_type`
        for the full detection priority.
        """
        return detect_output_media_type(output_data)

    def _store_flatgeobuf_reference_output(
        self,
        geoserver: Geoserver,
        storage_job_id: str,
        output_data: Any,
    ) -> bool:
        """Persist FlatGeobuf output via efficient GDAL/ogr2ogr ingestion.

        See :func:`~ump.api.models.flatgeobuf_ingest.store_flatgeobuf_ingest`
        for the full routing logic.
        """
        return store_flatgeobuf_ingest(
            geoserver, storage_job_id, output_data, self.job_id or ""
        )

    def _extract_flatgeobuf_bytes(self, output_data: Any) -> bytes | None:
        """Extract FlatGeobuf bytes from supported result representations.

        See :func:`~ump.api.models.flatgeobuf_ingest.extract_flatgeobuf_bytes`
        for supported payload forms.
        """
        return extract_flatgeobuf_bytes(output_data)

    def _try_decode_base64(self, candidate: str) -> bytes | None:
        """Attempt to base64-decode a string, returning ``None`` on failure."""
        return try_decode_base64(candidate)

    def _build_reference_result(self) -> dict | None:
        """Build an OGC-compliant results document with a link to the result.

        Returns:
            dict | None: A results document of the form
            ``{output_id: link.yaml}`` if a reference can be produced,
            otherwise ``None`` to signal that the caller should fall back
            to inline transmission.
        """
        provider_prefix, process_id = self._require_provider_process_context()
        result_storage = providers.check_result_storage(provider_prefix, process_id)
        if result_storage != "geoserver":
            logger.warning(
                "Job %s: transmissionMode='reference' is configured but "
                "result-storage='%s' has no addressable result resource; "
                "falling back to inline results.",
                self.job_id,
                result_storage,
            )
            return None

        job_id = self._require_job_id()
        geoserver_url = Geoserver().get_layer_reference_url(job_id)
        logger.debug(
            "Job %s: returning OGC reference result for GeoServer layer %s",
            self.job_id,
            geoserver_url,
        )

        # Per OGC results.yaml: additionalProperties is inlineOrRefData; for
        # reference outputs the value is a link.yaml object.
        return {
            "result": {
                "href": geoserver_url,
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": "application/json",
                "title": f"Process results for job {self.job_id}",
            }
        }

    def _all_outputs_reference_and_stored(self) -> bool:
        """Check if all outputs are in reference mode and stored in GeoServer.

        If true, we can skip remote fetching and return cached links directly.

        Returns:
            bool: True if all outputs are reference mode AND result-storage
            is configured to 'geoserver', False otherwise.
        """
        if not self.output_transmission_modes:
            return False

        # Check if all outputs requested reference mode
        all_reference = all(
            mode == "reference" for mode in self.output_transmission_modes.values()
        )
        if not all_reference:
            return False

        # Check if result-storage is geoserver for this process
        try:
            provider_prefix, process_id = self._require_provider_process_context()
            result_storage = providers.check_result_storage(provider_prefix, process_id)
            return result_storage == "geoserver"
        except Exception:
            return False

    def _build_cached_reference_results(self) -> dict:
        """Build OGC results document with GeoServer reference links.

        Returns stored result links without fetching from remote.
        This is safe because job completion already persisted data.

        Returns:
            dict: OGC results document with link.yaml objects for each output.
        """
        result_document: dict = {}
        job_id = self._require_job_id()
        geoserver = Geoserver()

        for output_id in self.output_transmission_modes.keys():
            storage_job_id = self._build_storage_job_id(job_id, output_id)
            geoserver_url = geoserver.get_layer_reference_url(storage_job_id)

            result_document[output_id] = {
                "href": geoserver_url,
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/results",
                "type": "application/geo+json",
                "title": f"Result '{output_id}' for job {self.job_id}",
            }

        logger.debug(
            "Job %s: returning %d cached GeoServer reference links "
            "(skipped remote fetch)",
            self.job_id,
            len(result_document),
        )
        return result_document

    async def _fetch_inline_results(self) -> dict:
        """Fetch results from remote model server.

        Supports JSON (standard OGC results document) and direct binary
        responses. Binary responses are wrapped into a synthetic single-output
        OGC-like structure so downstream per-output logic can still run.
        """
        provider_prefix, remote_job_id = self._require_remote_execution_context()
        provider: ProviderConfig = providers.get_providers()[provider_prefix]
        self.provider_url = provider.server_url

        auth_strategy = remote_auth.get_auth_strategy(provider.authentication)
        provider_auth = auth_strategy.get_auth()

        headers = {
            "Content-type": "application/json",
            "Accept": (
                "application/json,application/geo+json,application/vnd.flatgeobuf,*/*"
            ),
        }
        headers.update(provider_auth.headers)

        async with aiohttp.ClientSession(timeout=results_client_timeout) as session:
            url = f"{self.provider_url}jobs/{remote_job_id}/results?f=json"
            async with session.get(
                url,
                headers=headers,
                auth=provider_auth.auth,
            ) as resp:
                if resp.status >= 400:
                    resp.raise_for_status()

                content_type = resp.headers.get("Content-Type", "")
                media_type = content_type.split(";")[0].strip().lower()

                if media_type in FLATGEOBUF_MEDIA_TYPES:
                    raw = await resp.read()
                    return {
                        "result": {
                            "type": media_type,
                            "data": base64.b64encode(raw).decode("ascii"),
                            "encoding": "base64",
                        }
                    }

                if (
                    "application/json" in media_type
                    or "application/geo+json" in media_type
                ):
                    return await resp.json()

                # Fallback: preserve response as text payload
                text_payload = await resp.text()
                return {
                    "result": {
                        "type": media_type or "text/plain",
                        "value": text_payload,
                    }
                }

    async def results_to_geoserver(self):
        try:
            provider_prefix, process_id = self._require_provider_process_context()
            job_id = self._require_job_id()
            provider: ProviderConfig = providers.get_providers()[provider_prefix]

            process_config: ProcessConfig = provider.processes[process_id]

            # Always fetch inline results from the remote model server for
            # persistence. Using self.results() here can return an OGC
            # reference link in transmissionMode='reference', which is not
            # a GeoJSON feature collection and cannot be stored in PostGIS.
            results = await self._fetch_inline_results()
            results_is_dict = isinstance(results, dict)
            if result_path := process_config.result_path:
                parts = result_path.split(".")
                for part in parts:
                    results = results[part]
                # After extraction, results might no longer be a dict
                # (e.g., if extracting a single value). Update flag.
                results_is_dict = isinstance(results, dict)

            geoserver = Geoserver()
            metadata_set = False

            # Per-output persistence path (preferred): preserve output ids and
            # store each reference output under its dedicated storage key.
            # Only use this if results remains a dict and we have per-output modes.
            if self.output_transmission_modes and results_is_dict:
                stored_any = False
                for output_id, mode in self.output_transmission_modes.items():
                    if mode != "reference":
                        continue

                    output_data = results.get(output_id)
                    if output_data is None:
                        logger.warning(
                            "Job %s, output %s: missing in result payload; "
                            "skipping persistence",
                            self.job_id,
                            output_id,
                        )
                        continue

                    storage_job_id = self._build_storage_job_id(job_id, output_id)
                    media_type = self._detect_output_media_type(output_data)

                    if media_type in FLATGEOBUF_MEDIA_TYPES:
                        try:
                            saved = self._store_flatgeobuf_reference_output(
                                geoserver,
                                storage_job_id,
                                output_data,
                            )
                            stored_any = stored_any or saved
                        except Exception as err:
                            logger.error(
                                "Job %s, output %s: FlatGeobuf persistence failed: %s",
                                self.job_id,
                                output_id,
                                err,
                            )
                        continue

                    if media_type in GEOJSON_MEDIA_TYPES:
                        if isinstance(output_data, dict) and "features" in output_data:
                            try:
                                geoserver.save_results(
                                    job_id=storage_job_id,
                                    data=output_data,
                                )
                                if not metadata_set:
                                    self.set_results_metadata(output_data)
                                    metadata_set = True
                                stored_any = True
                            except Exception as err:
                                logger.error(
                                    "Job %s, output %s: GeoJSON persistence failed: %s",
                                    self.job_id,
                                    output_id,
                                    err,
                                )
                        continue

                    logger.warning(
                        "Job %s, output %s: media type '%s' not supported for "
                        "GeoServer persistence",
                        self.job_id,
                        output_id,
                        media_type,
                    )

                if stored_any:
                    logger.info(
                        " --> Successfully stored reference results for job %s "
                        "(=%s)/%s to geoserver.",
                        self.process_id_with_prefix,
                        self.process_id,
                        self.job_id,
                    )
                return

            # Legacy single-output fallback
            media_type = self._detect_output_media_type(results)
            if media_type in FLATGEOBUF_MEDIA_TYPES:
                self._store_flatgeobuf_reference_output(
                    geoserver,
                    job_id,
                    results,
                )
            elif media_type in GEOJSON_MEDIA_TYPES:
                if isinstance(results, dict) and "features" in results:
                    self.set_results_metadata(results)
                    geoserver.save_results(job_id=job_id, data=results)
            else:
                logger.warning(
                    "Job %s: media type '%s' not supported for GeoServer persistence",
                    self.job_id,
                    media_type,
                )
                return

            logging.info(
                " --> Successfully stored results for job %s (=%s)/%s to geoserver.",
                self.process_id_with_prefix,
                self.process_id,
                self.job_id,
            )

        except Exception as e:
            logging.error(
                " --> Could not store results for job %s (=%s)/%s to geoserver: %s",
                self.process_id_with_prefix,
                self.process_id,
                self.job_id,
                e,
            )

    def results_not_available(self):
        """
        Raises an OGCProcessException with a meaningful type and detail
        according to the current job status.
        """
        status_map = {
            JobStatus.failed.value: {
                "type": "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/failed",
                "title": "Job failed",
                "detail": self.message
                or "The job failed and no results are available.",
            },
            JobStatus.dismissed.value: {
                "type": "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/dismissed",
                "title": "Job dismissed",
                "detail": self.message
                or "The job was dismissed and no results are available.",
            },
            JobStatus.running.value: {
                "type": "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/result-not-ready",
                "title": "Job still running",
                "detail": "The job is still running. Results are not yet available.",
            },
            JobStatus.accepted.value: {
                "type": "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/result-not-ready",
                "title": "Job accepted",
                "detail": (
                    "The job has been accepted but has not started yet. "
                    "Results are not available."
                ),
            },
        }

        info = status_map.get(
            self.status if self.status is not None else "",
            {
                "type": "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job",
                "title": "No results available",
                "detail": "No results are available for this job.",
            },
        )

        raise OGCProcessException(
            OGCExceptionResponse(
                type=info["type"],
                title=info["title"],
                detail=info["detail"],
                status=404,
                instance=f"{config.UMP_API_SERVER_URL}/{config.UMP_API_SERVER_URL_PREFIX}/jobs/{self.job_id}/results",
            )
        )

    def __str__(self):
        return f"""
      ----- src.job.Job -----
      job_id={self.job_id}, process_id={self.process_id},
      status={self.status}, message={self.message},
      progress={self.progress}, parameters={self.parameters},
      started={self.started}, created={self.created},
      finished={self.finished}, updated={self.updated}
    """

    def __repr__(self):
        return f"src.job.Job(job_id={self.job_id})"
