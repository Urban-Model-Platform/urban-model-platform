import asyncio
import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from multiprocessing import dummy

import aiohttp
from flask import g

import ump.api.providers as providers
from ump.api import remote_auth
from ump.api.db_handler import engine
from ump.api.models.job import Job, JobStatus
from ump.api.models.ogc_exception import OGCExceptionResponse
from ump.api.models.providers_config import ProcessConfig, ProviderConfig
from ump.config import app_settings as config
from ump.errors import OGCProcessException
from ump.utils import fetch_json, fetch_response_content

logger = logging.getLogger(__name__)

metadata_request_timeout = aiohttp.ClientTimeout(
    total=5,
    connect=2,
    sock_connect=2,
    sock_read=5,
)

# Submission requests can carry very large payloads, so do not cap total duration.
submission_request_timeout = aiohttp.ClientTimeout(
    total=None,
    connect=10,
    sock_connect=10,
    sock_read=120,
)

submission_post_timeout_seconds = 300


# TODO: this is not an OGC API Process in a strict sense,
# instead it incorporates remote process execution logic
# these two things should probably be separated with regard to
# **Single Responsibility Principle (SRP)**
class Process:
    def __init__(self, process_id_with_prefix):
        self.inputs: dict
        self.outputs: dict

        self.process_id_with_prefix = process_id_with_prefix
        self.id = None
        self.title = None
        self.version = None
        self.job_control_options = None
        self.description = None
        self.keywords = None
        self.metadata = None
        self.links = None

        match = re.search(r"([^:]+):(.*)", self.process_id_with_prefix)

        if not match:
            logger.warning(
                "Process ID '%s' does not match pattern 'provider:process_id'. "
                "See /api/processes for a list of available processes.",
                self.process_id_with_prefix,
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                    title="Invalid Process ID",
                    status=400,
                    detail=(
                        f"Process ID '{self.process_id_with_prefix}' "
                        "does not match pattern: 'provider:process_id'}. "
                        "See /api/processes for a list of available processes."
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}",
                )
            )

        self.provider_prefix = match.group(1)
        self.process_id = match.group(2)

        # this checks if the process is known from providers and configured as available
        # for what purpose? -> security! -if a user selects a remote process which is
        # not (!) explicitly configured to be available, but exists on the remote server
        try:
            process_config = providers.get_process_config(
                self.provider_prefix, self.process_id
            )
        except ValueError as e:
            logger.error(e)

            raise OGCProcessException(
                OGCExceptionResponse(
                    type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                    title="Invalid Process ID",
                    status=400,
                    detail=(
                        f"Process ID '{self.process_id_with_prefix}' "
                        "does not match pattern: 'provider:process_id'}. "
                        "See /api/processes for a list of available processes."
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}",
                )
            )
        if process_config.exclude:
            logger.warning(
                "Process ID '%s' is explicitly excluded. ",
                self.process_id_with_prefix,
            )
            # raise OGCProcessException to inform users, but with less detail
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                    title="Not available.",
                    status=400,
                    detail=(
                        f"Process ID '{self.process_id_with_prefix}' is not available."
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}",
                )
            )

        logger.debug(
            "Process %s is available. Loading process details.",
            self.process_id_with_prefix,
        )

        # if anonymous access isn’t enabled, require a token
        if not process_config.anonymous_access:
            logger.debug(
                "Process %s requires authentication. Checking user roles.",
                self.process_id_with_prefix,
            )

            # check if token was provided by the user
            auth = getattr(g, "auth_token", None)

            if not auth:
                logger.warning(
                    "User is not allowed to access process %s. "
                    "The request lacks necessary authentication details!",
                    self.process_id_with_prefix,
                )
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Unauthorized.",
                        status=401,
                        detail=("The request lacks necessary authentication details."),
                        instance=f"/processes/{self.process_id_with_prefix}",
                    )
                )

            role_name = f"{self.provider_prefix}_{self.process_id}"

            realm_roles = auth.get("realm_access", {}).get("roles", []) if auth else []

            client_roles = (
                (
                    auth.get("resource_access", {})
                    .get(config.UMP_KEYCLOAK_CLIENT_ID, {})
                    .get("roles", [])
                )
                if auth
                else []
            )

            allowed = any(
                r in realm_roles + client_roles
                for r in [self.provider_prefix, role_name]
            )

            if not allowed:
                logger.warning(
                    "User is not allowed to access process %s. "
                    "User roles: %s, required roles: %s",
                    self.process_id_with_prefix,
                    realm_roles + client_roles,
                    [self.provider_prefix, role_name],
                )
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Forbidden.",
                        status=403,
                        detail=(
                            f"User is not allowed to access process "
                            f"'{self.process_id_with_prefix}'"
                        ),
                        instance=f"/processes/{self.process_id_with_prefix}",
                    )
                )

        asyncio.run(self.load_process_details())

    async def load_process_details(self):
        provider_config = providers.get_providers()[self.provider_prefix]

        auth_strategy = remote_auth.get_auth_strategy(provider_config.authentication)
        provider_auth = auth_strategy.get_auth()

        async with aiohttp.ClientSession(timeout=metadata_request_timeout) as session:
            process_details = await fetch_json(
                session=session,
                url=f"{provider_config.server_url}processes/{self.process_id}",
                auth=provider_auth.auth,
                headers=provider_auth.headers,
            )
            for key in process_details:
                setattr(self, key, process_details[key])

    # def validate_exec_body(self, parameters):
    #     if not self.inputs:
    #         return

    #     for input in self.inputs:
    #         try:
    #             if not "schema" in self.inputs[input]:
    #                 continue

    #             parameter_metadata = self.inputs[input]
    #             schema = parameter_metadata["schema"]

    #             if not input in parameters["inputs"]:
    #                 if self.is_required(parameter_metadata):
    #                     raise InvalidUsage(
    #                         f"Parameter {input} is required",
    #                         payload={"parameter_description": parameter_metadata},
    #                     )
    #                 else:
    #                     logger.warning(
    #                         "Model execution %s started without parameter %s.",
    #                         self.process_id_with_prefix,
    #                         input,
    #                     )
    #                     continue

    #             param = parameters["inputs"][input]

    #             if "minimum" in schema:
    #                 assert param >= schema["minimum"]

    #             if "maximum" in schema:
    #                 assert param <= schema["maximum"]

    #             if "type" in schema:
    #                 if schema["type"] == "number":
    #                     assert (
    #                         type(param) == int
    #                         or type(param) == float
    #                         or type(param) == complex
    #                     )

    #                 if schema["type"] == "string":
    #                     assert type(param) == str

    #                     if "maxLength" in schema:
    #                         assert len(param) <= schema["maxLength"]

    #                     if "minLength" in schema:
    #                         assert len(param) >= schema["minLength"]

    #                 if schema["type"] == "array":
    #                     assert type(param) == list
    #                     if (
    #                         "items" in schema
    #                         and "type" in schema["items"]
    #                         and schema["items"]["type"] == "string"
    #                     ):
    #                         for item in param:
    #                             assert type(item) == str
    #                     if schema["items"]["type"] == "number":
    #                         for item in param:
    #                             assert (
    #                                 type(item) == int
    #                                 or type(item) == float
    #                                 or type(item) == complex
    #                             )
    #                     if "uniqueItems" in schema and schema["uniqueItems"]:
    #                         assert len(param) == len(set(param))
    #                     if "minItems" in schema:
    #                         assert len(param) >= schema["minItems"]

    #             if "pattern" in schema:
    #                 assert re.search(schema["pattern"], param)

    #         except AssertionError as exc:
    #             raise InvalidUsage(
    #                 f"Invalid parameter {input} = {param}: does not match mandatory schema {schema}"
    #             ) from exc

    def is_required(self, parameter_metadata):
        if "required" in parameter_metadata:
            return parameter_metadata["required"]

        if "required" in parameter_metadata["schema"]:
            return parameter_metadata["schema"]["required"]

        if "minOccurs" in parameter_metadata:
            return parameter_metadata["minOccurs"] > 0

        return False

    @staticmethod
    def compute_job_hash(parameters_json, process_version, user_id):
        """
        Compute the cache hash for a job in Python.
        Must match the PostgreSQL formula:
        encode(sha512(convert_to(params::json::text || version || user_id, 'UTF8')), 'base64')
        """
        raw = parameters_json + process_version + (user_id or "")
        digest = hashlib.sha512(raw.encode("utf-8")).digest()
        return base64.b64encode(digest).decode("ascii")

    def check_for_cache(self, parameters, user_id):
        """
        Checks if the job has already been executed. Returns the job id if it has, None otherwise.
        """
        # p = providers.PROVIDERS[self.provider_prefix]["processes"][self.process_id]
        provider: ProviderConfig = providers.get_providers()[self.provider_prefix]
        process_config: ProcessConfig = provider.processes[self.process_id]

        if process_config.deterministic:
            return None

        hash_value = self.compute_job_hash(
            json.dumps(parameters), self.version, user_id
        )

        sql = "select job_id from jobs where hash = %(hash)s"
        with engine.begin() as conn:
            result = conn.exec_driver_sql(sql, {"hash": hash_value})
            for row in result:
                return row.job_id
        return None

    def execute(self, exec_body, user):
        provider: ProviderConfig = providers.get_providers()[self.provider_prefix]

        # TODO: make this optional (remote servers should do this themselves)
        # because of semantic errors deactivated
        # self.validate_exec_body(exec_body)

        logger.info(
            "Executing %s on model server %s with params %s as process %s for user %s",
            self.process_id,
            str(provider.server_url),
            str(exec_body)[:50],
            self.process_id_with_prefix,
            user,
        )

        job = asyncio.run(self.start_process_execution(exec_body, user))

        results_thread = dummy.Process(target=self._wait_for_results_async, args=[job])
        results_thread.start()

        return {"jobID": job.job_id, "status": job.status}

    async def start_process_execution(self, request_body, user):
        request_body["mode"] = "async"
        provider: ProviderConfig = providers.get_providers()[self.provider_prefix]
        name = request_body.pop("job_name", None)

        job_id = self.check_for_cache(request_body, user)
        if job_id:
            logger.info("Job found, returning cached job.")
            return Job(job_id, user)

        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
            "Prefer": "respond-async",
        }

        auth_strategy = remote_auth.get_auth_strategy(provider.authentication)
        provider_auth = auth_strategy.get_auth()
        headers.update(provider_auth.headers)

        # TODO: if a server decides to ignore the Prefer header, response contains results, not job status info
        # need to adress this case!
        async with aiohttp.ClientSession(timeout=submission_request_timeout) as session:
            try:
                response = await self._submit_remote_job(
                    session,
                    str(provider.server_url),
                    request_body,
                    provider_auth,
                    headers,
                    max_submit_seconds=max(
                        submission_post_timeout_seconds, provider.timeout
                    ),
                )

                response_content = await fetch_response_content(response)

                response.raise_for_status()  # Raise an error for bad responses

                remote_job_id = await self._extract_remote_job_id(response)

                job = await self._create_local_job_instance(
                    remote_job_id, name, request_body, user
                )

                headers.pop("Prefer", None)
                # this can probably be omitted here and deferred for _wait_for_status
                remote_job_status_info = await self._fetch_remote_job_status(
                    session,
                    str(provider.server_url),
                    remote_job_id,
                    provider_auth,
                    headers,  # remove prefer header
                )

                self._update_job_from_status(job, remote_job_status_info)

                job.update()

                logger.info(
                    "Job %s for model %s started.",
                    job.job_id,
                    self.process_id_with_prefix,
                )

                return job

            except aiohttp.ClientResponseError as e:
                logger.error("HTTP error during job submission: %s", e)
                # response_body = await response.text()
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Remote job submission failed",
                        status=e.status,
                        detail=(
                            "Job could not be started remotely "
                            f"due to {response_content[0]}"
                        ),
                        instance=f"/processes/{self.process_id_with_prefix}/jobs",
                    )
                ) from e

            except OGCProcessException:
                raise

            except Exception as e:
                logger.exception("Unexpected error during job submission: \n%s", e)

                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/server-error",
                        title="Unexpected error during job submission",
                        status=500,
                        detail=f"Job could not be started remotely due to unexpected error. Please see logs for more details.",
                        instance=f"/processes/{self.process_id_with_prefix}/execution",
                    )
                ) from e

    async def _submit_remote_job(
        self,
        session: aiohttp.ClientSession,
        url: str,
        request_body: dict,
        auth: remote_auth.ProviderAuth,
        headers: dict | None = None,
        max_submit_seconds: int = submission_post_timeout_seconds,
    ) -> aiohttp.ClientResponse:
        try:
            response = await asyncio.wait_for(
                session.post(
                    f"{url}processes/{self.process_id}/execution",
                    json=request_body,
                    auth=auth.auth,
                    headers=headers,
                ),
                timeout=max_submit_seconds,
            )
        except asyncio.TimeoutError as e:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Remote job submission timed out",
                    status=504,
                    detail=(
                        "Submitting the job to the remote server exceeded "
                        f"the timeout of {max_submit_seconds} seconds."
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}/execution",
                )
            ) from e
        except aiohttp.ServerDisconnectedError as e:
            logger.error("Client error during job submission: %s", e)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Remote server disconnected during submission",
                    status=502,
                    detail=(
                        "The remote server closed the connection while "
                        "the job submission request was in progress."
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}/execution",
                )
            ) from e
        except aiohttp.ClientError as e:
            logger.error("Client error during job submission: %s", e)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Remote server communication failed",
                    status=502,
                    detail=(
                        "Failed to submit the job to the remote server due "
                        f"to a transport error: {e}"
                    ),
                    instance=f"/processes/{self.process_id_with_prefix}/execution",
                )
            ) from e

        if response.headers or response.content_type == "application/json":
            return response

        raise OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="No valid response from remote server.",
                status=502,
                detail=(
                    "No application/json response and no headers from "
                    "remote server received. Response is not valid."
                ),
                instance=f"/processes/{self.process_id_with_prefix}/jobs",
            )
        )

    async def _create_local_job_instance(
        self, remote_job_id: str, name, request_body, user
    ):
        job = Job()

        job.insert(
            remote_job_id=remote_job_id,
            process_id_with_prefix=self.process_id_with_prefix,
            process_title=self.title,
            name=name,
            exec_body=request_body,
            user=user,
            process_version=self.version,
        )

        return job

    async def _fetch_remote_job_status(
        self,
        session: aiohttp.ClientSession,
        url: str,
        remote_job_id: str,
        auth: remote_auth.ProviderAuth,
        headers: dict | None = None,
    ) -> dict:
        job_status = await fetch_json(
            session=session,
            url=f"{url}jobs/{remote_job_id}?f=json",
            auth=auth.auth,
            headers=headers,
        )

        logger.debug(job_status)

        return job_status

    async def _extract_remote_job_id(self, response: aiohttp.ClientResponse) -> str:
        """
        Extracts the remote job ID from the aiohttp response object.
        Tries the Location header first, then the response body for jobID/jobId.
        """
        location_header = response.headers.get("location")
        if location_header:
            match = re.search(r"/jobs/([^/]+)", location_header)
            if match:
                return match.group(1)

        # Try to extract jobID from response body
        try:
            if response.content_type == "application/json":
                body_json = await response.json()
                job_id = body_json.get("jobID") or body_json.get("jobId")
                if job_id:
                    return job_id
        except Exception as e:
            logger.warning("Failed to parse jobID from response body: %s", e)

        raise OGCProcessException(
            OGCExceptionResponse(
                type="http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-process",
                title="Invalid Location Header",
                status=500,
                detail="Could not extract remote job ID from Location header or response body.",
                instance="/jobs",
            )
        )

    def _wait_for_results_async(self, job: Job):
        asyncio.run(self._wait_for_results(job))

    async def _wait_for_results(self, job: Job):
        logger.info("Thread started to wait for results.")
        provider_config: ProviderConfig = providers.get_providers()[
            self.provider_prefix
        ]
        timeout_seconds = provider_config.timeout

        try:
            await asyncio.wait_for(
                self._poll_job_until_finished(job, provider_config),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            self._set_job_failed(
                job,
                (
                    "While waiting for remote job to finish the"
                    f" timeout ({provider_config.timeout} sec.) was reached."
                ),
            )
        # TODO: consider better error handling: this except block catches all exceptions
        # even programming errors, which should be fixed, not caught
        # setting job to "failed" even if remote job was successfull!
        except Exception as e:
            logger.error("Error while waiting for job results: %s", e)
            self._set_job_failed(
                job,
                (
                    "An unexpected error occurred while waiting for job results."
                    "See the logs for details"
                ),
            )
        else:
            await self._store_results_if_needed(job)

    async def _poll_job_until_finished(
        self, job: Job, provider_config: ProviderConfig
    ) -> dict:

        headers = {
            "Content-type": "application/json",
            "Accept": "application/json",
        }

        auth_strategy = remote_auth.get_auth_strategy(provider_config.authentication)
        provider_auth = auth_strategy.get_auth()

        headers.update(provider_auth.headers)

        async with aiohttp.ClientSession(timeout=metadata_request_timeout) as session:
            while True:
                status_info = await self._fetch_remote_job_status(
                    session,
                    str(provider_config.server_url),
                    job.remote_job_id,
                    provider_auth,
                    headers,
                )
                self._update_job_from_status(job, status_info)
                if self.is_finished(status_info):
                    break
                await asyncio.sleep(config.UMP_REMOTE_JOB_STATUS_REQUEST_INTERVAL)

        return status_info

    def _update_job_from_status(self, job: Job, status_info):
        job.started = status_info.get("started")
        job.created = status_info.get("created")
        job.updated = status_info.get("updated")
        job.finished = status_info.get("finished")
        job.message = status_info.get("message", "")
        job.progress = status_info.get("progress")
        job.status = status_info.get("status", "")

        # save to database
        job.update()

    def _set_job_failed(self, job: Job, message: str):
        job.status = JobStatus.failed.value
        job.message = message

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        job.finished = now
        job.updated = now
        job.progress = 0
        job.update()

    # left here if we want to set a job as successful, manually
    # this is not used in the current implementation, but could be useful
    def _set_job_successful(self, job: Job):
        job.status = JobStatus.successful.value
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        job.finished = now
        job.updated = now
        job.progress = 100
        job.update()

    async def _store_results_if_needed(self, job: Job):
        try:
            if (
                providers.check_result_storage(self.provider_prefix, self.process_id)
                == "geoserver"
            ):
                await job.results_to_geoserver()
        except Exception as e:
            logger.error("Could not store results for job %s: %s", job.job_id, e)

    def is_finished(self, job_details):
        finished = False

        if "finished" in job_details and job_details["finished"]:
            finished = True

        if job_details["status"] in [
            JobStatus.dismissed.value,
            JobStatus.failed.value,
            JobStatus.successful.value,
        ]:
            finished = True

        return finished

    def to_dict(self):
        process_dict = self.__dict__
        process_dict.pop("process_id")
        process_dict.pop("provider_prefix")
        process_dict["id"] = process_dict.pop("process_id_with_prefix")

        # delete all keys containing None
        for key, value in list(process_dict.items()):
            if value is None:
                process_dict.pop(key)

        return process_dict

    def to_json(self):
        return json.dumps(
            self.to_dict(), default=lambda o: o.__dict__, sort_keys=True, indent=2
        )

    def __str__(self):
        return (
            f"src.process.Process object: process_id={self.process_id}, "
            + f"process_id_with_prefix={self.process_id_with_prefix}, "
            + f"provider_prefix={self.provider_prefix}"
        )

    def __repr__(self):
        return f"src.process.Process(process_id={self.process_id})"
