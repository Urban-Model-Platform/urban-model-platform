# ump/adapters/web/fastapi.py
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable

from fastapi import APIRouter, FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError

from ump.core.exceptions import OGCProcessException
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
from ump.core.settings import app_settings


# Note: this a driver adapter, so it depends on the core interface (ProcessesPort)
# but the core does not depend on this adapter
# it does not need to implement a port/interface itself
# it just uses the interface of the core (ProcessesPort)
def create_app(
    process_manager_factory: Callable[[HttpClientPort], ProcessManager],
    http_client: HttpClientPort,
    job_manager_factory: Callable[[HttpClientPort, ProcessManager], JobManager],
    job_repo: JobRepositoryPort,
    process_id_validator: ProcessIdValidatorPort | None = None,
    site_info: SiteInfoPort | None = None,
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
            # JobManager already attached inside job_manager_factory (composition root)
            job_manager = job_manager_factory(client, process_port)
            app.state.process_port = process_port
            app.state.job_manager = job_manager
            app.state.job_repo = job_repo

            try:
                yield
            finally:
                if hasattr(job_manager, "shutdown"):
                    await job_manager.shutdown()

    app = FastAPI(lifespan=lifespan)

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

    def validate_process_id(process_id: str, request: Request) -> JSONResponse | None:
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

    # API routes — defined once, mounted under each supported version prefix
    api_router = APIRouter()

    @api_router.get(
        "/processes",
        response_model=ProcessList,
        response_model_exclude_none=True,
        response_model_by_alias=True,
    )
    async def get_all_processes():
        return await app.state.process_port.get_all_processes()

    @api_router.get(
        "/processes/{process_id}",
        response_model=Process,
        response_model_exclude_none=True,
        response_model_by_alias=True,
    )
    async def get_process(process_id: str, request: Request):
        if err := validate_process_id(process_id, request):
            return err
        return await app.state.process_port.get_process(process_id)

    @api_router.get("/jobs", response_model=JobList, response_model_exclude_none=True)
    async def list_jobs():
        jobs = await app.state.job_repo.list()
        status_infos = [j.status_info for j in jobs if j.status_info]
        return JobList(jobs=status_infos, links=[])

    @api_router.get(
        "/jobs/{job_id}",
        response_model=JobStatusInfo,
        response_model_exclude_none=True,
    )
    async def get_job(job_id: str, request: Request):
        job = await app.state.job_repo.get(job_id)
        if not job or not job.status_info:
            problem = build_problem(
                status=404,
                title="Job Not Found",
                detail=f"Job '{job_id}' not found",
                request=request,
            )
            return render_problem(problem)
        return job.status_info

    @api_router.get("/jobs/{job_id}/results")
    async def get_job_results(job_id: str, request: Request):
        jm = app.state.job_manager
        if jm is None:
            problem = build_problem(
                status=404,
                title="Results Not Supported",
                detail="Results endpoint not available",
                request=request,
            )
            return render_problem(problem)
        resp = await jm.get_results(job_id)
        return JSONResponse(
            status_code=resp.get("status", 200), content=resp.get("body", {})
        )

    @api_router.post("/processes/{process_id}/execution")
    async def execute_process(request: Request, process_id: str):
        if err := validate_process_id(process_id, request):
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
            exec_req = ExecuteRequest.from_raw(raw)
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

        provider_payload = exec_req.as_provider_payload()
        # Forward full normalized payload (includes inputs, outputs, response, subscriber)
        resp = await app.state.process_port.execute_process(
            process_id, provider_payload, headers
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

        # Build table rows
        links = api.get("routes", [])
        table_rows = "".join(
            f"<tr><td><a href='{r['path']}'>{r['path']}</a></td><td>{r.get('description', '')}</td></tr>"
            for r in links
        )

        contact = api.get("contact") or {}
        contact_line = " | ".join(
            filter(
                None,
                [
                    f"<a href='{contact.get('url')}'>{contact.get('name')}</a>"
                    if isinstance(contact, dict) and contact.get("url")
                    else None,
                    f"<a href='mailto:{contact.get('email')}'>{contact.get('email')}</a>"
                    if isinstance(contact, dict) and contact.get("email")
                    else None,
                ],
            )
        )

        # Adapter-local style
        css_href = "/static/style.css"

        # Also include discovered supported versions for the template
        supported_versions = getattr(
            app_settings, "UMP_SUPPORTED_API_VERSIONS", ["1.0"]
        )

        context = {
            "request": request,
            "title": api.get("title"),
            "version": ", ".join(supported_versions),
            "description": api.get("description"),
            "contact_line": contact_line,
            "license_line": "",
            "terms_of_service": "",
            "powered_by": "<a href='https://github.com/citysciencelab/urban-model-platform'>urban-model-platform</a>",
            "css": css_href,
            "table_rows": table_rows,
            "supported_versions": supported_versions,
        }

        return templates.TemplateResponse("template.html", context)

    # Exception handler for OGC Process exceptions
    @app.exception_handler(OGCProcessException)
    async def ogc_exception_handler(request: Request, exc: OGCProcessException):
        # Decide whether to surface requestId (only for unexpected / upstream server side errors)
        status_code = exc.response.status
        cid = correlation_id_var.get()
        include_request_id = status_code >= 500
        problem = exc.response.model_copy()
        if include_request_id:
            problem = problem.with_request_id(cid)
        return render_problem(problem, include_request_id=include_request_id)

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
