"""Minimal mock OGC API Processes server for local development and manual testing.

Implements enough of the OGC API Processes v1.0.0 spec to exercise all UMP
execution proxy mechanics without requiring any real model infrastructure.

Available processes
-------------------
echo          Returns every input value unchanged after a short delay.
              Good for testing basic async round-trips and link normalization.

hello-world   Accepts a 'name' string, returns a greeting message.
              Supports both sync-execute and async-execute.

slow          Sleeps for MOCK_SLOW_DELAY_SECONDS (default 10) before succeeding.
              Use this to exercise UMP polling, timeout handling, and job status
              updates over time.

failing-job   Always fails after a short delay.
              Use this to test UMP failure propagation and error responses.

Configuration (environment variables)
--------------------------------------
MOCK_HOST                  bind host              (default: 0.0.0.0)
MOCK_PORT                  bind port              (default: 5001)
MOCK_JOB_DELAY_SECONDS     echo/hello delay, s    (default: 2)
MOCK_SLOW_DELAY_SECONDS    slow process delay, s  (default: 10)
MOCK_BASE_URL              self-link base URL     (default: http://localhost:5001)

Usage (local)
-------------
  PYTHONPATH=src .venv/bin/uvicorn scripts.mock_ogc_server:app --port 5001 --reload

  # or directly:
  PYTHONPATH=src .venv/bin/python scripts/mock_ogc_server.py

Usage (docker-compose-dev.yaml)
--------------------------------
  See the mock-ogc-server service in docker-compose-dev.yaml.

  Then add to providers.yaml:
    - name: mock
      url: http://mock-ogc-server:5001
      processes:
        - id: echo
        - id: hello-world
        - id: slow
        - id: failing-job
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import uvicorn
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

HOST = os.environ.get("MOCK_HOST", "0.0.0.0")
PORT = int(os.environ.get("MOCK_PORT", "5001"))
JOB_DELAY = float(os.environ.get("MOCK_JOB_DELAY_SECONDS", "2"))
SLOW_DELAY = float(os.environ.get("MOCK_SLOW_DELAY_SECONDS", "10"))
BASE_URL = os.environ.get("MOCK_BASE_URL", f"http://localhost:{PORT}").rstrip("/")

# ---------------------------------------------------------------------------
# In-memory job store
# ---------------------------------------------------------------------------

_jobs: Dict[str, Dict[str, Any]] = {}
_results: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# Process catalog
# ---------------------------------------------------------------------------


def _self_link(path: str) -> Dict[str, str]:
    return {"href": f"{BASE_URL}{path}", "rel": "self", "type": "application/json"}


PROCESSES: List[Dict[str, Any]] = [
    {
        "id": "echo",
        "version": "1.0.0",
        "title": "Echo",
        "description": (
            "Returns every input value unchanged. "
            "Simulates async execution with a short configurable delay."
        ),
        "jobControlOptions": ["async-execute", "sync-execute"],
        "outputTransmission": ["value"],
        "inputs": {
            "message": {
                "title": "Message",
                "description": "Any string or JSON-serializable value to echo back.",
                "schema": {"type": "string"},
                "minOccurs": 0,
                "maxOccurs": 1,
            }
        },
        "outputs": {
            "echo": {
                "title": "Echo",
                "description": "The received message, unchanged.",
                "schema": {"type": "string"},
            }
        },
        "links": [_self_link("/processes/echo")],
    },
    {
        "id": "hello-world",
        "version": "1.0.0",
        "title": "Hello World",
        "description": "Accepts a name and returns a greeting.",
        "jobControlOptions": ["async-execute", "sync-execute"],
        "outputTransmission": ["value"],
        "inputs": {
            "name": {
                "title": "Name",
                "description": "The name to greet.",
                "schema": {"type": "string"},
                "minOccurs": 0,
                "maxOccurs": 1,
            }
        },
        "outputs": {
            "greeting": {
                "title": "Greeting",
                "description": "A greeting message.",
                "schema": {"type": "string"},
            }
        },
        "links": [_self_link("/processes/hello-world")],
    },
    {
        "id": "slow",
        "version": "1.0.0",
        "title": "Slow Process",
        "description": (
            f"Sleeps for MOCK_SLOW_DELAY_SECONDS (currently {SLOW_DELAY}s) "
            "before succeeding. Exercises UMP polling and timeout handling."
        ),
        "jobControlOptions": ["async-execute"],
        "outputTransmission": ["value"],
        "inputs": {
            "payload": {
                "title": "Payload",
                "description": "Optional input — returned as-is in the output.",
                "schema": {"type": "string"},
                "minOccurs": 0,
                "maxOccurs": 1,
            }
        },
        "outputs": {
            "result": {
                "title": "Result",
                "description": "Echo of the input payload.",
                "schema": {"type": "string"},
            }
        },
        "links": [_self_link("/processes/slow")],
    },
    {
        "id": "failing-job",
        "version": "1.0.0",
        "title": "Failing Job",
        "description": "Always fails after a short delay. Use this to test UMP failure handling.",
        "jobControlOptions": ["async-execute"],
        "outputTransmission": ["value"],
        "inputs": {},
        "outputs": {},
        "links": [_self_link("/processes/failing-job")],
    },
]

_PROCESS_INDEX: Dict[str, Dict[str, Any]] = {p["id"]: p for p in PROCESSES}

# ---------------------------------------------------------------------------
# Job helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _make_status(
    job_id: str,
    process_id: str,
    status: str,
    message: Optional[str] = None,
    started: Optional[str] = None,
    finished: Optional[str] = None,
    progress: Optional[int] = None,
) -> Dict[str, Any]:
    si: Dict[str, Any] = {
        "jobID": job_id,
        "type": "process",
        "processID": process_id,
        "status": status,
        "created": _jobs[job_id]["created"],
        "updated": _now(),
        "links": [
            _self_link(f"/jobs/{job_id}"),
        ],
    }
    if message:
        si["message"] = message
    if started:
        si["started"] = started
    if finished:
        si["finished"] = finished
    if progress is not None:
        si["progress"] = progress
    if status == "successful":
        si["links"].append(
            {
                "href": f"{BASE_URL}/jobs/{job_id}/results",
                "rel": "results",
                "type": "application/json",
            }
        )
    return si


def _create_job(process_id: str) -> str:
    job_id = str(uuid.uuid4())
    _jobs[job_id] = {
        "process_id": process_id,
        "created": _now(),
        "status": "accepted",
    }
    return job_id


# ---------------------------------------------------------------------------
# Background execution simulators
# ---------------------------------------------------------------------------


async def _run_echo(job_id: str, inputs: Dict[str, Any]) -> None:
    process_id = _jobs[job_id]["process_id"]
    started = _now()
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["status_info"] = _make_status(
        job_id, process_id, "running", message="Running", started=started, progress=10
    )
    await asyncio.sleep(JOB_DELAY)
    finished = _now()
    message_val = inputs.get("message", "")
    name_val = inputs.get("name", "World")
    if process_id == "hello-world":
        _results[job_id] = {"greeting": {"value": f"Hello, {name_val}!"}}
    else:
        _results[job_id] = {"echo": {"value": message_val}}
    _jobs[job_id]["status"] = "successful"
    _jobs[job_id]["status_info"] = _make_status(
        job_id,
        process_id,
        "successful",
        message="Completed",
        started=started,
        finished=finished,
        progress=100,
    )


async def _run_slow(job_id: str, inputs: Dict[str, Any]) -> None:
    process_id = _jobs[job_id]["process_id"]
    started = _now()
    # Report running with progress updates
    for pct in [10, 30, 60, 90]:
        _jobs[job_id]["status"] = "running"
        _jobs[job_id]["status_info"] = _make_status(
            job_id,
            process_id,
            "running",
            started=started,
            progress=pct,
            message=f"Running ({pct}%)",
        )
        await asyncio.sleep(SLOW_DELAY / 4)
    finished = _now()
    payload = inputs.get("payload", "done")
    _results[job_id] = {"result": {"value": payload}}
    _jobs[job_id]["status"] = "successful"
    _jobs[job_id]["status_info"] = _make_status(
        job_id,
        process_id,
        "successful",
        message="Completed",
        started=started,
        finished=finished,
        progress=100,
    )


async def _run_failing(job_id: str, _inputs: Dict[str, Any]) -> None:
    process_id = _jobs[job_id]["process_id"]
    started = _now()
    _jobs[job_id]["status"] = "running"
    _jobs[job_id]["status_info"] = _make_status(
        job_id, process_id, "running", started=started, progress=5
    )
    await asyncio.sleep(JOB_DELAY)
    _jobs[job_id]["status"] = "failed"
    _jobs[job_id]["status_info"] = _make_status(
        job_id,
        process_id,
        "failed",
        message="Intentional failure for testing purposes.",
        started=started,
        finished=_now(),
    )


_RUNNERS = {
    "echo": _run_echo,
    "hello-world": _run_echo,  # same logic
    "slow": _run_slow,
    "failing-job": _run_failing,
}

# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Mock OGC API Processes Server",
    description="Lightweight mock for local UMP development. No external dependencies.",
    version="1.0.0",
    openapi_url="/openapi.json",
)


@app.get("/", summary="Landing page")
async def landing():
    return {
        "title": "Mock OGC API Processes Server",
        "description": "Minimal OGC API Processes mock for UMP development.",
        "links": [
            _self_link("/"),
            {
                "href": f"{BASE_URL}/processes",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/processes",
                "type": "application/json",
            },
            {
                "href": f"{BASE_URL}/jobs",
                "rel": "http://www.opengis.net/def/rel/ogc/1.0/job-list",
                "type": "application/json",
            },
            {
                "href": f"{BASE_URL}/openapi.json",
                "rel": "service-desc",
                "type": "application/vnd.oai.openapi+json;version=3.0",
            },
        ],
    }


@app.get("/processes", summary="List processes")
async def list_processes():
    summaries = [
        {k: v for k, v in p.items() if k != "inputs" and k != "outputs"}
        for p in PROCESSES
    ]
    return {
        "processes": summaries,
        "links": [_self_link("/processes")],
    }


@app.get("/processes/{process_id}", summary="Describe a process")
async def describe_process(process_id: str):
    proc = _PROCESS_INDEX.get(process_id)
    if not proc:
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")
    return proc


@app.post("/processes/{process_id}/execution", summary="Execute a process")
async def execute_process(process_id: str, request: Request):
    proc = _PROCESS_INDEX.get(process_id)
    if not proc:
        raise HTTPException(status_code=404, detail=f"Process '{process_id}' not found")

    body: Dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass

    # Normalize inputs: OGC clients may send {"message": {"value": "..."}} or {"message": "..."}
    raw_inputs: Dict[str, Any] = body.get("inputs", {})
    inputs: Dict[str, Any] = {}
    for k, v in raw_inputs.items():
        if isinstance(v, dict) and "value" in v:
            inputs[k] = v["value"]
        elif isinstance(v, dict) and "href" in v:
            inputs[k] = v["href"]
        else:
            inputs[k] = v

    prefer = request.headers.get("Prefer", "").lower()
    respond_sync = "respond-sync" in prefer

    job_id = _create_job(process_id)
    accepted_si = _make_status(job_id, process_id, "accepted", progress=0)
    _jobs[job_id]["status_info"] = accepted_si

    runner = _RUNNERS.get(process_id)

    if respond_sync and runner:
        # Synchronous execution: await runner, return final result directly
        await runner(job_id, inputs)
        final_status = _jobs[job_id]["status"]
        final_si = _jobs[job_id].get("status_info", {})
        if final_status == "successful":
            results = _results.get(job_id, {})
            return JSONResponse(status_code=200, content=results)
        else:
            # Failed sync execution: return 500 with statusInfo
            return JSONResponse(status_code=500, content=final_si)

    # Asynchronous execution (Prefer: respond-async or no preference)
    if runner:
        asyncio.create_task(runner(job_id, inputs))

    location = f"{BASE_URL}/jobs/{job_id}"
    return JSONResponse(
        status_code=201,
        content=accepted_si,
        headers={"Location": location},
    )


@app.get("/jobs", summary="List jobs")
async def list_jobs():
    job_list = []
    for job_id, job in _jobs.items():
        si = job.get("status_info") or _make_status(
            job_id, job["process_id"], job["status"]
        )
        job_list.append(si)
    return {
        "jobs": job_list,
        "links": [_self_link("/jobs")],
    }


@app.get("/jobs/{job_id}", summary="Get job status")
async def get_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    return job.get("status_info") or _make_status(
        job_id, job["process_id"], job["status"]
    )


@app.get("/jobs/{job_id}/results", summary="Get job results")
async def get_job_results(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    if job["status"] != "successful":
        raise HTTPException(status_code=404, detail="Results not yet available")
    results = _results.get(job_id)
    if not results:
        raise HTTPException(status_code=404, detail="Results not found")
    return results


@app.delete("/jobs/{job_id}", summary="Dismiss a job")
async def dismiss_job(job_id: str):
    job = _jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job '{job_id}' not found")
    job["status"] = "dismissed"
    si = _make_status(job_id, job["process_id"], "dismissed")
    job["status_info"] = si
    return si


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run("scripts.mock_ogc_server:app", host=HOST, port=PORT, reload=False)
