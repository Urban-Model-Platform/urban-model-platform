import base64
import hashlib
import json
import logging
import re
import uuid
from datetime import datetime, timezone

import aiohttp
import geopandas as gpd

import ump.api.providers as providers
from ump.api import remote_auth
from ump.api.db_handler import DBHandler
from ump.api.models.job_status import JobStatus
from ump.api.models.ogc_exception import OGCExceptionResponse
from ump.api.models.providers_config import ProcessConfig, ProviderConfig
from ump.config import app_settings as config
from ump.errors import InvalidUsage, OGCProcessException
from ump.geoserver.geoserver import Geoserver
from ump.utils import fetch_json, join_url_parts

results_client_timeout = aiohttp.ClientTimeout(
    total=None,  # No hard cap — model outputs can be large
    connect=10,  # Connection timeout
    sock_connect=10,  # Socket connection timeout
    sock_read=120,  # Allow up to 2 minutes without new data before giving up
)


# TODO class violates Single Responsibility Principle (SRP), it mixes
# business logic with data access logic and metadata handling
# TODO methods like insert use redundant fields instead of job instance fields
# TODO queries use raw SQL, which is not recommended for different reasons: no migrations, reduced maintainability
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
        self.provider_prefix = None
        self.process_id = None
        self.provider_url = None

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
        )

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
                process_title, name, process_version, hash
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
                %(hash)s
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
    ):
        self.job_id = job_id
        self.remote_job_id = remote_job_id
        self.user_id = user_id
        self.process_title = process_title
        self.name = name
        self.process_version = process_version

        if remote_job_id and not job_id:
            self.job_id = f"job-{remote_job_id}"

        if job_id and not remote_job_id:
            match = re.search("job-(.*)$", job_id)
            self.remote_job_id = match.group(1)

        self.process_id_with_prefix = process_id_with_prefix
        self.parameters = parameters

        if process_id_with_prefix:
            match = re.search(r"(.*):(.*)", self.process_id_with_prefix)
            if not match:
                raise InvalidUsage(
                    f"Process ID {self.process_id_with_prefix} is not known! "
                    + "Please check endpoint api/processes for a list of available processes."
                )

            self.provider_prefix = match.group(1)
            self.process_id = match.group(2)
            self.provider_url = providers.get_providers()[
                self.provider_prefix
            ].server_url

        if not self.job_id:
            self.job_id = str(uuid.uuid4())

    def _init_from_db(self, job_id, user):
        query = """
      SELECT j.* FROM jobs j left join jobs_users u on j.job_id = u.job_id WHERE j.job_id = %(job_id)s
    """
        if user is None:
            query += " and j.user_id is null"
        else:
            query += f" and (j.user_id = '{user}' or u.user_id = '{user}')"

        with DBHandler() as db:
            job_details = db.run_query(query, query_params={"job_id": job_id})

        if len(job_details) > 0:
            self._init_from_dict(dict(job_details[0]))
            return True
        return False

    def _init_from_dict(self, data):
        self.job_id = data["job_id"]
        self.remote_job_id = data["remote_job_id"]
        self.process_id: str = data["process_id"]
        self.provider_prefix: str = data["provider_prefix"]
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
        }

    def update(self):
        self.updated = datetime.now(timezone.utc)

        query = """
            UPDATE jobs SET
            (process_id, provider_prefix, provider_url, status, progress, parameters, message, created, started, finished, updated, results_metadata, process_version)
            =
            (%(process_id)s, %(provider_prefix)s, %(provider_url)s, %(status)s, %(progress)s, %(parameters)s, %(message)s, %(created)s, %(started)s, %(finished)s, %(updated)s, %(results_metadata)s, %(process_version)s)
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
        if self.status != JobStatus.successful.value:
            self.results_not_available()

        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }

        provider: ProviderConfig = providers.get_providers()[self.provider_prefix]
        self.provider_url = provider.server_url

        auth_strategy = remote_auth.get_auth_strategy(provider.authentication)
        provider_auth = auth_strategy.get_auth()
        headers.update(provider_auth.headers)

        async with aiohttp.ClientSession(timeout=results_client_timeout) as session:
            results = await fetch_json(
                session,
                url=f"{self.provider_url}jobs/{self.remote_job_id}/results?f=json",
                headers=headers,
                auth=provider_auth.auth,
            )

            return results

    async def results_to_geoserver(self):
        try:
            provider: ProviderConfig = providers.get_providers()[self.provider_prefix]

            process_config: ProcessConfig = provider.processes[self.process_id]

            results = await self.results()
            if result_path := process_config.result_path:
                parts = result_path.split(".")
                for part in parts:
                    results = results[part]

            geoserver = Geoserver()

            self.set_results_metadata(results)

            geoserver.save_results(job_id=self.job_id, data=results)

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
                "detail": "The job has been accepted but has not started yet. Results are not available.",
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
