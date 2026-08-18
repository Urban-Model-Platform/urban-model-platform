# ump/adapters/web/fastapi.py
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, Protocol

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext, AuthPort
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.interfaces.process_id_validator import ProcessIdValidatorPort
from ump.core.interfaces.site_info import SiteInfoPort
from ump.core.logging_config import correlation_id_var
from ump.core.managers.job_manager import JobManager
from ump.core.managers.process_manager import ProcessManager
from ump.core.models.execute_request import ExecuteRequest
from ump.core.models.job import JobList, JobStatusInfo
from ump.core.models.ogcp_exception import OGCExceptionResponse
from ump.core.models.process import Process, ProcessList
from ump.core.services.authorization import AuthorizationService
from ump.core.settings import app_settings, logger

# OGC API - Processes Part 1: Core, /req/core/job-exception-no-such-job (and the
# equivalent job-results requirement) mandate this exact exception type URI
# whenever a jobID does not resolve to an existing job - including jobs that
# once existed but were permanently removed by the V-9 cleanup service. A
# removed job must be indistinguishable from a jobID that never existed.
NO_SUCH_JOB_TYPE = (
    "http://www.opengis.net/def/exceptions/ogcapi-processes-1/1.0/no-such-job"
)


# module global helpers
def render_problem(
    problem: OGCExceptionResponse,
    *,
    include_request_id: bool = False,
) -> JSONResponse:
    payload = jsonable_encoder(problem.model_dump(exclude_none=True))
    response = JSONResponse(status_code=problem.status, content=payload)
    if include_request_id and problem.additional and problem.additional.requestId:
        response.headers["X-Request-ID"] = problem.additional.requestId
    return response


def build_problem(
    status: int,
    title: str,
    detail: str,
    request: Request,
    type_uri: str = "about:blank",
) -> OGCExceptionResponse:
    return OGCExceptionResponse(
        type=type_uri,
        title=title,
        status=status,
        detail=detail,
        instance=str(request.url),
    )


def validate_process_id(
    process_id: str,
    request: Request,
    process_id_validator: ProcessIdValidatorPort | None = None,
) -> JSONResponse | None:
    """Returns a 400 problem response if process_id fails validation, else None."""
    if process_id_validator and not process_id_validator.validate(process_id):
        problem = build_problem(
            status=400,
            title="Invalid Process ID",
            detail=f"Process ID '{process_id}' does not match the expected format.",
            request=request,
        )
        return render_problem(problem)
    return None


async def _recover_orphaned_polls(
    job_repo: JobRepositoryPort, job_manager: "JobManager"
) -> None:
    """Re-schedule poll loops for jobs that survived a previous instance crash.

    Called once during lifespan startup, after all adapters are wired.
    Safe to run on every startup: ``_schedule_poll`` deduplicates within this
    instance, and the distributed advisory lock (``PgAdvisoryPollLock``)
    deduplicates across instances.
    """
    from ump.core.models.job import StatusCode

    terminal = {
        str(StatusCode.successful),
        str(StatusCode.failed),
        str(StatusCode.dismissed),
    }
    try:
        all_jobs = await job_repo.list()
        recovered = 0
        for job in all_jobs:
            if job.status not in terminal and job.remote_status_url:
                job_manager._schedule_poll(job.id)
                recovered += 1
        if recovered:
            logger.info(f"[startup] recovered {recovered} orphaned poll loop(s)")
    except Exception as exc:
        logger.warning(f"[startup] poll recovery failed: {exc}")


# Note: this a driver adapter, so it depends on the core interface (ProcessesPort)
# but the core does not depend on this adapter
# it does not need to implement a port/interface itself
# it just uses the interface of the core (ProcessesPort)
class BackgroundRunner(Protocol):
    """Structural protocol for any start/stop background loop (e.g. cleanup).

    Deliberately generic: the web adapter starts and stops these at the right
    point in the FastAPI lifespan without knowing what they do — job cleanup
    is the first user, but nothing here is specific to it.
    """

    def start(self) -> None: ...

    async def stop(self) -> Awaitable[None]: ...


def create_app(
    process_manager_factory: Callable[[HttpClientPort], ProcessManager],
    http_client: HttpClientPort,
    job_manager_factory: Callable[[HttpClientPort, ProcessManager], JobManager],
    job_repo: JobRepositoryPort,
    process_id_validator: ProcessIdValidatorPort | None = None,
    auth_port: AuthPort | None = None,
    authorization_service: AuthorizationService | None = None,
    site_info: SiteInfoPort | None = None,
    background_runners: list[BackgroundRunner] | None = None,
):
    """Create the FastAPI app.

    Adapters and concrete infrastructure (logging, repositories, providers) are
    assembled outside and passed as factories. This keeps the web adapter focused
    purely on HTTP concerns and lifecycle orchestration.
    """

    # We intentionally do NOT configure logging here. Composition root (main)
    # must call configure_logging. If invoked directly without main, logging
    # will remain minimal (NoOpLogger) which is acceptable for that edge case.

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        async with http_client as client:
            process_port = process_manager_factory(client)
            if process_port is None:
                raise RuntimeError(
                    "process_manager_factory returned None — composition root error"
                )
            # JobManager already attached inside job_manager_factory (composition root)
            job_manager = job_manager_factory(client, process_port)
            if job_manager is None:
                raise RuntimeError(
                    "job_manager_factory returned None — composition root error"
                )
            if job_repo is None:
                raise RuntimeError("job_repo is None — composition root error")
            app.state.process_port = process_port
            app.state.job_manager = job_manager
            app.state.job_repo = job_repo
            app.state.auth_port = auth_port
            app.state.authz = authorization_service

            # Re-schedule poll loops for any non-terminal jobs that were left
            # running by a previously crashed or restarted instance.
            await _recover_orphaned_polls(job_repo, job_manager)

            for runner in background_runners or []:
                runner.start()

            try:
                yield
            finally:
                if hasattr(job_manager, "shutdown"):
                    await job_manager.shutdown()
                for runner in background_runners or []:
                    await runner.stop()

    app = FastAPI(lifespan=lifespan, redirect_slashes=False)

    # ── CORS middleware ───────────────────────────────────────────────────────
    # Only added when origins are explicitly configured.  CORSMiddleware must be
    # registered via add_middleware (not as a decorator) so it wraps the entire
    # application and handles OPTIONS preflight requests before any route or
    # auth logic runs.
    #
    # allow_credentials is intentionally False: UMP uses Bearer tokens that the
    # client supplies explicitly — cookies are not in play, so a wildcard origin
    # cannot be used to silently re-send session credentials.
    if app_settings.UMP_CORS_ORIGINS:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=app_settings.UMP_CORS_ORIGINS,
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["Authorization", "Content-Type", "Accept", "X-Request-ID"],
        )

    app.add_middleware(
        ProxyHeadersMiddleware,  # type: ignore[arg-type]
        trusted_hosts="*",
    )

    # Correlation ID middleware: assigns per-request id (header override) and exposes it to logging
    @app.middleware("http")
    async def correlation_id_middleware(request: Request, call_next):
        incoming = request.headers.get("x-request-id") or request.headers.get(
            "X-Request-ID"
        )
        cid = incoming or uuid.uuid4().hex[:12]
        # set context var so logs include this id
        correlation_id_var.set(cid)
        try:
            response = await call_next(request)
        finally:
            # ensure context is reset to avoid leak across reused worker tasks
            correlation_id_var.set("-")
        # always return id header for traceability
        response.headers["X-Request-ID"] = cid
        return response

    # Serve static files and templates from the adapter package itself
    adapter_root = Path(__file__).parent
    adapter_static = adapter_root / "static"
    adapter_templates = adapter_root / "templates"

    if adapter_static.exists():
        app.mount("/static", StaticFiles(directory=str(adapter_static)), name="static")

    templates = Jinja2Templates(directory=str(adapter_templates))

    # ------------------------------------------------------------------
    # Auth helpers (used as FastAPI dependencies in routes below)
    # ------------------------------------------------------------------

    def _extract_bearer(request: Request) -> str | None:
        """Extract the Bearer token string from the Authorization header, or None."""
        auth_header = request.headers.get("Authorization") or request.headers.get(
            "authorization"
        )
        if auth_header and auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip() or None
        return None

    async def _get_auth(request: Request) -> AuthContext:
        """FastAPI dependency: resolve the caller's AuthContext from the request."""
        port: AuthPort | None = app.state.auth_port
        if port is None:
            return AuthContext(user_id=None, roles=[], is_authenticated=False)
        token = _extract_bearer(request)
        return await port.verify(token)

    def _check_process_access(
        auth: AuthContext, process_id: str, request: Request
    ) -> None:
        """Enforce execution access, delegating the decision to the core.

        The web adapter only owns the "is auth wired at all?" gate; the actual
        role/anonymous-access policy lives in ``AuthorizationService``.
        """
        if app.state.auth_port is None or app.state.authz is None:
            return  # auth disabled globally
        app.state.authz.check_process_access(auth, process_id)

    def _check_job_access(job, auth: AuthContext, request: Request) -> bool:
        """Return True if the caller may see this job, False if it should appear as 404."""
        if job.user_id is None:
            return True  # public job
        if not auth.is_authenticated:
            return False  # private job, caller unauthenticated
        return job.user_id == auth.user_id

    # API routes — defined once, mounted under each supported version prefix
    api_router = APIRouter()

    @api_router.get(
        "/processes",
        response_model=ProcessList,
        response_model_exclude_none=True,
        response_model_by_alias=True,
    )
    async def get_all_processes(request: Request):
        if app.state.auth_port is not None and not app_settings.UMP_PUBLIC_PROCESSES:
            auth = await _get_auth(request)
            if not auth.is_authenticated:
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Unauthorized",
                        status=401,
                        detail="Authentication required to view processes.",
                        instance=str(request.url),
                    )
                )
        return await app.state.process_port.get_all_processes()

    @api_router.get(
        "/processes/{process_id}",
        response_model=Process,
        response_model_exclude_none=True,
        response_model_by_alias=True,
    )
    async def get_process(process_id: str, request: Request):
        if err := validate_process_id(process_id, request, process_id_validator):
            return err
        if app.state.auth_port is not None and not app_settings.UMP_PUBLIC_PROCESSES:
            auth = await _get_auth(request)
            if not auth.is_authenticated:
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Unauthorized",
                        status=401,
                        detail=(
                            "Authentication required to view process details. "
                            "If you think this is an error reach out to the platform "
                            "administrator and give them the following requestId for debugging."
                        ),
                        instance=str(request.url),
                    )
                )
        return await app.state.process_port.get_process(process_id)

    @api_router.get("/jobs", response_model=JobList, response_model_exclude_none=True)
    async def list_jobs(request: Request):
        auth = await _get_auth(request)
        if not auth.is_authenticated:
            jobs = await app.state.job_repo.list(public_only=True)
        else:
            jobs = await app.state.job_repo.list(
                user_id=auth.user_id, include_public=True
            )
        status_infos = [j.status_info for j in jobs if j.status_info]
        return JobList(jobs=status_infos, links=[])

    @api_router.get(
        "/jobs/{job_id}",
        response_model=JobStatusInfo,
        response_model_exclude_none=True,
    )
    async def get_job(job_id: str, request: Request):
        auth = await _get_auth(request)
        job = await app.state.job_repo.get(job_id)
        if not job or not job.status_info or not _check_job_access(job, auth, request):
            return render_problem(
                build_problem(
                    status=404,
                    title="Job Not Found",
                    detail=f"Job '{job_id}' not found",
                    request=request,
                    type_uri=NO_SUCH_JOB_TYPE,
                )
            )
        return job.status_info

    @api_router.get("/jobs/{job_id}/results")
    async def get_job_results(job_id: str, request: Request):
        auth = await _get_auth(request)
        job = await app.state.job_repo.get(job_id)
        if not job or not _check_job_access(job, auth, request):
            return render_problem(
                build_problem(
                    status=404,
                    title="Job Not Found",
                    detail=f"Job '{job_id}' not found",
                    request=request,
                    type_uri=NO_SUCH_JOB_TYPE,
                )
            )
        try:
            resp = await app.state.job_manager.get_results(job_id)
        except OGCProcessException:
            raise  # let the app-level OGC handler format it
        except Exception as exc:
            problem = build_problem(
                status=500,
                title="Results Unavailable",
                detail=f"Unexpected error fetching job results: {exc}",
                request=request,
            )
            return render_problem(problem)

        status = resp.get("status", 200)

        # Error responses from get_results use the "body" key (plain dict).
        # Optional "headers" (e.g. Retry-After for a still-finalizing store)
        # are forwarded verbatim when present.
        if status != 200:
            return JSONResponse(
                status_code=status,
                content=resp.get("body", {}),
                headers=resp.get("headers"),
            )

        # Successful results: forward the remote's Content-Type verbatim.
        # body_bytes is always present for status 200; the content type tells
        # the client whether they received JSON, FlatGeobuf, multipart, etc.
        from fastapi.responses import Response as RawResponse

        return RawResponse(
            content=resp.get("body_bytes", b""),
            media_type=resp.get("content_type", "application/octet-stream"),
            status_code=200,
        )

    @api_router.post("/processes/{process_id}/execution")
    async def execute_process(request: Request, process_id: str):
        if err := validate_process_id(process_id, request, process_id_validator):
            return err
        # Parse and validate execute request body against ExecuteRequest model.
        try:
            raw = await request.json()
        except Exception as exc:
            problem = build_problem(
                status=400,
                title="Invalid JSON",
                detail=f"Request body is not valid JSON: {exc}",
                request=request,
            )
            return render_problem(problem)

        try:
            ExecuteRequest.from_raw(
                raw
            )  # structural validation — raises 400 if malformed
        except ValidationError as ve:
            detail_messages = []
            for err in ve.errors():
                loc = ".".join(str(part) for part in err.get("loc", []))
                msg = err.get("msg", "invalid value")
                detail_messages.append(f"{loc or 'body'}: {msg}")
            detail_text = (
                "; ".join(detail_messages) or "Invalid execute request payload"
            )
            problem = build_problem(
                status=400,
                title="Invalid Execute Request",
                detail=detail_text,
                request=request,
            )
            return render_problem(problem)

        # Collect headers of interest (Prefer) and forward the rest if needed
        headers = {}
        prefer = request.headers.get("prefer") or request.headers.get("Prefer")
        if prefer:
            headers["Prefer"] = prefer

        # Structural validation passed; forward the original body unchanged.
        # UMP does not transform execute request payloads — the process
        # description is the authority, not UMP.
        # Resolve auth and check process access before forwarding
        auth = await _get_auth(request)
        _check_process_access(auth, process_id, request)
        # Forward the original raw body; pipeline extracts response/outputs from it.
        resp = await app.state.process_port.execute_process(
            process_id, raw, headers, user_id=auth.user_id
        )

        # If the backend returned structured dict with status/headers/body, map to response
        if isinstance(resp, dict) and "status" in resp:
            status = resp.get("status") or 200
            content = resp.get("body") or {}
            location = None

            if isinstance(resp.get("headers"), dict):
                location = resp["headers"].get("Location")

            safe_content = jsonable_encoder(content)
            response = JSONResponse(status_code=status, content=safe_content)

            if location:
                response.headers["Location"] = location
            return response

        # Otherwise return generic JSON
        return JSONResponse(status_code=200, content=resp or {})

    # Mount the router under each supported version prefix
    for ver in getattr(app_settings, "UMP_SUPPORTED_API_VERSIONS", ["1.0"]):
        app.include_router(api_router, prefix=f"/v{ver}")

    # Dedicated route for the landing CSS. This is a robust fallback for environments
    # where StaticFiles mounting might not be available (packaged apps, different cwd).
    @app.get("/landing.css", name="landing_css")
    async def landing_css():
        css_path = Path(__file__).parent / "static" / "landing.css"
        if css_path.exists():
            return FileResponse(str(css_path), media_type="text/css")
        return JSONResponse(status_code=404, content={})

    # Landing page route (optional) - uses site_info adapter if provided
    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        # Content negotiation: query param 'f' or Accept header
        f = request.query_params.get("f")
        accept = request.headers.get("accept", "")

        api = (
            site_info.get_site_info()
            if site_info is not None
            else {
                "title": "Urban Model Platform",
                "description": "API available at /processes",
                "routes": [
                    {"path": "/processes", "description": "List available processes"}
                ],
                "contact": "",
            }
        )

        # JSON response when requested
        if f == "json" or ("application/json" in accept and f != "html"):
            return JSONResponse(api)

        links = api.get("routes", [])
        contact = api.get("contact") or {}

        # Adapter-local style
        css_href = "/static/style.css"

        supported_versions = getattr(
            app_settings, "UMP_SUPPORTED_API_VERSIONS", ["1.0"]
        )

        context = {
            "request": request,
            "title": api.get("title"),
            "version": ", ".join(supported_versions),
            "description": api.get("description"),
            "links": links,
            "contact": contact,
            "powered_by": "<a href='https://github.com/citysciencelab/urban-model-platform'>urban-model-platform</a>",
            "css": css_href,
            "supported_versions": supported_versions,
        }

        return templates.TemplateResponse("template.html", context)

    # Exception handler for OGC Process exceptions
    @app.exception_handler(OGCProcessException)
    async def ogc_exception_handler(request: Request, exc: OGCProcessException):
        cid = correlation_id_var.get()
        problem = exc.response.model_copy().with_request_id(cid)
        status = problem.status
        # Log every error response with the request ID so it can be found
        # when a user reports the ID from their error response body.
        log_msg = (
            f"[http:error] status={status} title={problem.title!r} "
            f"path={request.url.path!r} request_id={cid}"
        )
        if status >= 500:
            logger.error(log_msg)
        elif status == 401 or status == 403:
            logger.warning(log_msg)
        else:
            logger.debug(log_msg)
        return render_problem(problem, include_request_id=True)

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        cid = correlation_id_var.get()
        problem = OGCExceptionResponse(
            type="about:blank",
            title="Internal Server Error",
            status=500,
            detail="An unexpected error occurred.",
            instance=str(request.url),
        ).with_request_id(cid)
        return render_problem(problem, include_request_id=True)

    return app
