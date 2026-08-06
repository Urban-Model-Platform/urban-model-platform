"""In-memory implementation of JobRepositoryPort.

Thread-safe / async-safe using an asyncio.Lock. Suitable for TDD and local tests.
Not intended for production; replace with SQLModel/Postgres adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
from copy import deepcopy
from datetime import datetime, timezone
from typing import Dict, List, Optional, Sequence

from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.models.job import Job, JobStatusInfo, StatusCode


class InMemoryJobRepository(JobRepositoryPort):
    def __init__(self, dump_dir: str | None = None) -> None:
        self._jobs: Dict[str, Job] = {}
        self._status_history: Dict[str, List[JobStatusInfo]] = {}
        self._events: Dict[str, List[dict]] = {}
        self._lock = asyncio.Lock()
        self._dump_dir = dump_dir or os.environ.get("UMP_JOB_DUMP_DIR")
        if self._dump_dir:
            os.makedirs(self._dump_dir, exist_ok=True)

    def _dump(self, job: Job) -> None:
        if not self._dump_dir:
            return
        try:
            latest = job.status_info.model_dump() if job.status_info else None
            payload = {
                "meta": {
                    "dumped_at": datetime.now(timezone.utc).isoformat(),
                    "repository": "in-memory",
                    "version": 1,
                },
                "job": job.model_dump(exclude={"status_info"}),
                "latest_status": latest,
                "history": [
                    s.model_dump() for s in self._status_history.get(job.id, [])
                ],
                "events": self._events.get(job.id, []),
            }
            if job.inputs is not None:
                payload["inputs"] = job.inputs
            elif job.inputs_storage == "object":
                payload["inputs"] = {
                    "omitted": True,
                    "reason": "stored externally / too large",
                }
            path = os.path.join(self._dump_dir, f"{job.id}.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        except Exception:
            # Never break repository operations due to dump errors
            pass

    async def create(self, job: Job) -> Job:
        async with self._lock:
            if job.id in self._jobs:
                raise ValueError(f"Job already exists: {job.id}")
            # ensure timestamps
            if not job.created:
                job.created = datetime.now(timezone.utc)
            stored = deepcopy(job)
            self._jobs[job.id] = stored
            # Initialize empty history; accepted snapshot appended explicitly later
            self._status_history.setdefault(job.id, [])
            self._events.setdefault(job.id, [])
            self._dump(stored)
            return deepcopy(stored)

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._lock:
            j = self._jobs.get(job_id)
            return deepcopy(j) if j else None

    async def update(self, job: Job) -> Job:
        async with self._lock:
            if job.id not in self._jobs:
                raise KeyError(job.id)
            job.touch()
            stored = deepcopy(job)
            self._jobs[job.id] = stored
            # status snapshot updated separately via append_status to avoid duplication
            self._dump(stored)
            return deepcopy(stored)

    async def list(
        self,
        provider: Optional[str] = None,
        process_id: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        public_only: bool = False,
        include_public: bool = True,
    ) -> Sequence[Job]:
        async with self._lock:
            jobs = list(self._jobs.values())
            if provider is not None:
                jobs = [j for j in jobs if j.provider == provider]
            if process_id is not None:
                jobs = [j for j in jobs if j.process_id == process_id]
            if status is not None:
                jobs = [j for j in jobs if j.status == status]
            if public_only:
                jobs = [j for j in jobs if j.user_id is None]
            elif user_id is not None:
                if include_public:
                    jobs = [
                        j for j in jobs if j.user_id == user_id or j.user_id is None
                    ]
                else:
                    jobs = [j for j in jobs if j.user_id == user_id]
            return [deepcopy(j) for j in jobs]

    async def mark_failed(
        self, job_id: str, reason: str, diagnostic: Optional[str] = None
    ) -> Optional[Job]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            # construct a statusInfo snapshot indicating failure
            status_info = JobStatusInfo(
                jobID=job.id,
                status=StatusCode.failed,
                message=reason,
                updated=datetime.now(timezone.utc),
                created=job.created,
                progress=None,
            )
            job.apply_status_info(status_info)
            if diagnostic:
                job.diagnostic = diagnostic
            self._status_history[job.id].append(deepcopy(status_info))
            self._jobs[job.id] = deepcopy(job)
            self._dump(job)
            return deepcopy(job)

    async def append_status(
        self, job_id: str, status_info: JobStatusInfo
    ) -> Optional[Job]:
        async with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return None
            job.apply_status_info(status_info)
            self._status_history[job_id].append(deepcopy(status_info))
            self._jobs[job_id] = deepcopy(job)
            self._dump(job)
            return deepcopy(job)

    async def append_event(self, job_id: str, event: dict) -> None:
        async with self._lock:
            if job_id not in self._events:
                # Initialize missing job bucket to avoid exceptions (event before create is rare but safe)
                self._events[job_id] = []
            self._events[job_id].append(deepcopy(event))

    async def list_expired(
        self,
        anonymous_cutoff: Optional[datetime] = None,
        authenticated_cutoff: Optional[datetime] = None,
    ) -> Sequence[Job]:
        terminal = {
            str(StatusCode.successful),
            str(StatusCode.failed),
            str(StatusCode.dismissed),
        }
        async with self._lock:
            expired: List[Job] = []
            for job in self._jobs.values():
                if job.status not in terminal:
                    continue
                finished = job.finished_at()
                if finished is None:
                    continue
                cutoff = (
                    anonymous_cutoff if job.user_id is None else authenticated_cutoff
                )
                if cutoff is not None and finished < cutoff:
                    expired.append(deepcopy(job))
            return expired

    async def delete(self, job_id: str) -> None:
        async with self._lock:
            self._jobs.pop(job_id, None)
            self._status_history.pop(job_id, None)
            self._events.pop(job_id, None)
            if self._dump_dir:
                path = os.path.join(self._dump_dir, f"{job_id}.json")
                try:
                    os.remove(path)
                except FileNotFoundError:
                    pass

    # Convenience accessors (not part of port but useful for tests)
    async def history(
        self, job_id: str
    ) -> List[JobStatusInfo]:  # pragma: no cover simple access
        async with self._lock:
            return [deepcopy(s) for s in self._status_history.get(job_id, [])]

    async def events(self, job_id: str) -> List[dict]:  # pragma: no cover simple access
        async with self._lock:
            return [deepcopy(e) for e in self._events.get(job_id, [])]
