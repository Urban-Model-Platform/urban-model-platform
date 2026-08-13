"""SQLModel-backed implementation of JobRepositoryPort.

Two-model pattern (hexagonal architecture):
  - Core domain model:  ump.core.models.job.Job  (pure Pydantic, no ORM)
  - Adapter ORM model:  JobRecord / JobStatusHistoryRecord (SQLModel, table=True)

The only place these two models touch is the from_domain / to_domain bridge
defined on JobRecord.  The core never imports sqlmodel or sqlalchemy.

Session management:  an async_sessionmaker is injected at construction time
(Option B from the REF design plan) so that the composition root (main.py)
controls session lifecycle and the repository is fully testable with a
provided session factory.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional, Sequence

from sqlalchemy import Column, DateTime
from sqlalchemy import delete as sa_delete
from sqlalchemy import update as sa_update
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import Field, SQLModel, col, select

from ump.core.exceptions import OptimisticLockError
from ump.core.interfaces.job_repository import JobRepositoryPort
from ump.core.models.job import Job, JobStatusInfo, StatusCode
from ump.core.models.link import Link

# ---------------------------------------------------------------------------
# ORM table models (adapter layer only)
# ---------------------------------------------------------------------------


class JobRecord(SQLModel, table=True):
    """Persistent representation of a Job.

    Scalar fields are mapped directly to columns for SQL-level filtering.
    Complex nested objects (status_info, inputs, links) are stored as JSONB
    so they can be round-tripped without a schema migration when their
    Pydantic definitions evolve.
    """

    __tablename__: str = "jobs"

    id: str = Field(primary_key=True)
    process_id: Optional[str] = Field(default=None, index=True)
    provider: Optional[str] = Field(default=None, index=True)
    remote_job_id: Optional[str] = Field(default=None)
    remote_status_url: Optional[str] = Field(default=None)
    user_id: Optional[str] = Field(default=None, index=True)
    # Denormalized status string for fast WHERE filters
    status: Optional[str] = Field(default=None, index=True)
    created: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )
    updated: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True),
    )
    # Denormalized from status_info.finished so cleanup can filter and
    # index on it directly instead of extracting from JSONB on every run.
    finished: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), nullable=True, index=True),
    )
    inputs_url: Optional[str] = Field(default=None)
    inputs_storage: Optional[str] = Field(default=None)
    inputs_size: Optional[int] = Field(default=None)
    inputs_checksum: Optional[str] = Field(default=None)
    diagnostic: Optional[str] = Field(default=None)
    version: int = Field(default=0, nullable=False)

    # ---- Execute-request context ----
    # Stored as plain TEXT and JSONB so they survive without a schema migration
    # when the underlying Pydantic models evolve.
    response_mode: Optional[str] = Field(default=None)
    outputs_spec: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # JSONB columns — declared with sa_column to get native PostgreSQL JSONB
    # (SQLModel defaults dict → TEXT without explicit sa_column)
    status_info: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )
    inputs: Optional[dict] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    links: Optional[list] = Field(default=None, sa_column=Column(JSONB, nullable=True))
    # output_id -> {collection_id, collection_url, items_url}. JSONB so
    # it round-trips without a migration when the shape evolves.
    stored_outputs: Optional[dict] = Field(
        default=None, sa_column=Column(JSONB, nullable=True)
    )

    # ------------------------------------------------------------------
    # Domain ↔ ORM mapping bridge
    # ------------------------------------------------------------------

    @classmethod
    def from_domain(cls, job: Job) -> "JobRecord":
        return cls(
            id=job.id,
            process_id=job.process_id,
            provider=job.provider,
            remote_job_id=job.remote_job_id,
            remote_status_url=job.remote_status_url,
            user_id=job.user_id,
            status=job.status,
            created=job.created,
            updated=job.updated,
            finished=job.finished_at(),
            status_info=job.status_info.model_dump(mode="json")
            if job.status_info
            else None,
            inputs=job.inputs,
            inputs_url=job.inputs_url,
            inputs_storage=job.inputs_storage,
            inputs_size=job.inputs_size,
            inputs_checksum=job.inputs_checksum,
            links=[lnk.model_dump(mode="json") for lnk in job.links]
            if job.links
            else None,
            diagnostic=job.diagnostic,
            version=job.version,
            response_mode=job.response_mode,
            outputs_spec=job.outputs_spec,
            stored_outputs=job.stored_outputs,
        )

    def to_domain(self) -> Job:
        status_info = JobStatusInfo(**self.status_info) if self.status_info else None
        links = [Link(**lnk) for lnk in (self.links or [])]
        return Job(
            id=self.id,
            process_id=self.process_id,
            provider=self.provider,
            remote_job_id=self.remote_job_id,
            remote_status_url=self.remote_status_url,
            user_id=self.user_id,
            status=self.status,
            created=self.created,
            updated=self.updated,
            status_info=status_info,
            inputs=self.inputs,
            inputs_url=self.inputs_url,
            inputs_storage=self.inputs_storage,  # type: ignore[arg-type]
            inputs_size=self.inputs_size,
            inputs_checksum=self.inputs_checksum,
            links=links,
            diagnostic=self.diagnostic,
            version=self.version,
            response_mode=self.response_mode,
            outputs_spec=self.outputs_spec,
            stored_outputs=self.stored_outputs,
        )


class JobStatusHistoryRecord(SQLModel, table=True):
    """Append-only audit log of every status transition for a job."""

    __tablename__: str = "job_status_history"

    id: Optional[int] = Field(default=None, primary_key=True)
    job_id: str = Field(foreign_key="jobs.id", nullable=False, index=True)
    seq: int = Field(nullable=False)
    snapshot: dict = Field(sa_column=Column(JSONB, nullable=False))
    recorded_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        sa_column=Column(DateTime(timezone=True), nullable=False),
    )


# ---------------------------------------------------------------------------
# Repository adapter
# ---------------------------------------------------------------------------


class SQLModelJobRepository(JobRepositoryPort):
    """Async PostgreSQL-backed job repository using SQLModel + asyncpg.

    The caller (main.py) is responsible for calling ``create_tables()`` at
    startup (or running Alembic migrations instead).  The repository itself
    never issues DDL.
    """

    def __init__(self, database_url: str) -> None:
        self._engine = create_async_engine(database_url, echo=False)
        self._session_factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
            self._engine, expire_on_commit=False
        )

    async def create_tables(self) -> None:
        """Create tables via SQLModel metadata (development / testing only).

        In production use Alembic migrations instead.
        """
        async with self._engine.begin() as conn:
            await conn.run_sync(SQLModel.metadata.create_all)

    # ------------------------------------------------------------------
    # Port implementation
    # ------------------------------------------------------------------

    async def create(self, job: Job) -> Job:
        record = JobRecord.from_domain(job)
        async with self._session_factory() as session:
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_domain()

    async def get(self, job_id: str) -> Optional[Job]:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id)
            return record.to_domain() if record else None

    async def update(self, job: Job) -> Job:
        """Persist job changes using optimistic locking on the version column.

        Raises ``OptimisticLockError`` if another instance already incremented
        the version — callers should re-read the job and retry.
        """
        job.touch()
        updated = JobRecord.from_domain(job)
        values = {
            field: getattr(updated, field)
            for field in JobRecord.model_fields
            if field != "version"
        }
        values["version"] = job.version + 1

        async with self._session_factory() as session:
            result = await session.execute(
                sa_update(JobRecord)
                .where(
                    col(JobRecord.id) == job.id,
                    col(JobRecord.version) == job.version,
                )
                .values(**values)
                .returning(JobRecord)
            )
            row = result.fetchone()
            if row is None:
                raise OptimisticLockError(
                    f"Job {job.id} was concurrently modified (version mismatch)"
                )
            await session.commit()
            # Re-fetch the updated record cleanly
            refreshed = await session.get(JobRecord, job.id)
            return refreshed.to_domain() if refreshed else job

    async def list(
        self,
        provider: Optional[str] = None,
        process_id: Optional[str] = None,
        status: Optional[str] = None,
        user_id: Optional[str] = None,
        public_only: bool = False,
        include_public: bool = True,
    ) -> Sequence[Job]:
        async with self._session_factory() as session:
            stmt = select(JobRecord)
            if provider is not None:
                stmt = stmt.where(JobRecord.provider == provider)
            if process_id is not None:
                stmt = stmt.where(JobRecord.process_id == process_id)
            if status is not None:
                stmt = stmt.where(JobRecord.status == status)
            if public_only:
                stmt = stmt.where(col(JobRecord.user_id).is_(None))
            elif user_id is not None:
                from sqlalchemy import or_

                if include_public:
                    stmt = stmt.where(
                        or_(
                            col(JobRecord.user_id) == user_id,
                            col(JobRecord.user_id).is_(None),
                        )
                    )
                else:
                    stmt = stmt.where(col(JobRecord.user_id) == user_id)
            stmt = stmt.order_by(col(JobRecord.created).desc())
            result = await session.execute(stmt)
            return [row.to_domain() for row in result.scalars().all()]

    async def mark_failed(
        self,
        job_id: str,
        reason: str,
        diagnostic: Optional[str] = None,
    ) -> Optional[Job]:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id)
            if record is None:
                return None
            record.status = str(StatusCode.failed)
            record.diagnostic = diagnostic or reason
            record.updated = datetime.now(timezone.utc)
            if record.status_info:
                record.status_info = {
                    **record.status_info,
                    "status": str(StatusCode.failed),
                    "message": reason,
                }
            session.add(record)
            await session.commit()
            await session.refresh(record)
            return record.to_domain()

    async def append_status(
        self, job_id: str, status_info: JobStatusInfo
    ) -> Optional[Job]:
        async with self._session_factory() as session:
            record = await session.get(JobRecord, job_id)
            if record is None:
                return None

            # Determine next sequence number
            result = await session.execute(
                select(JobStatusHistoryRecord)
                .where(JobStatusHistoryRecord.job_id == job_id)
                .order_by(col(JobStatusHistoryRecord.seq).desc())
            )
            last = result.scalars().first()
            next_seq = (last.seq + 1) if last else 0

            history_entry = JobStatusHistoryRecord(
                job_id=job_id,
                seq=next_seq,
                snapshot=status_info.model_dump(mode="json"),
            )
            session.add(history_entry)

            # Update current snapshot
            record.status_info = status_info.model_dump(mode="json")
            record.status = str(status_info.status)
            record.updated = datetime.now(timezone.utc)
            if status_info.finished is not None:
                record.finished = status_info.finished
            session.add(record)

            await session.commit()
            await session.refresh(record)
            return record.to_domain()

    async def append_event(self, job_id: str, event: dict) -> None:
        """Record a generic domain event. Stored as a status-history entry
        with seq=-1 convention to distinguish from status snapshots, or
        simply ignored if a dedicated events table is not yet available.

        For now this is a no-op — the history table stores status snapshots
        only.  A separate events table can be added in a future migration.
        """

    async def list_expired(
        self,
        anonymous_cutoff: Optional[datetime] = None,
        authenticated_cutoff: Optional[datetime] = None,
    ) -> Sequence[Job]:
        from sqlalchemy import and_, or_

        terminal = {
            str(StatusCode.successful),
            str(StatusCode.failed),
            str(StatusCode.dismissed),
        }
        conditions = []
        if anonymous_cutoff is not None:
            conditions.append(
                and_(
                    col(JobRecord.user_id).is_(None),
                    col(JobRecord.finished) < anonymous_cutoff,
                )
            )
        if authenticated_cutoff is not None:
            conditions.append(
                and_(
                    col(JobRecord.user_id).is_not(None),
                    col(JobRecord.finished) < authenticated_cutoff,
                )
            )
        if not conditions:
            # Both cutoffs disabled — deliberately return nothing rather than
            # "everything", so a caller cannot accidentally sweep the table.
            return []

        async with self._session_factory() as session:
            stmt = (
                select(JobRecord)
                .where(col(JobRecord.status).in_(terminal))
                .where(col(JobRecord.finished).is_not(None))
                .where(or_(*conditions))
            )
            result = await session.execute(stmt)
            return [row.to_domain() for row in result.scalars().all()]

    async def delete(self, job_id: str) -> None:
        async with self._session_factory() as session:
            await session.execute(
                sa_delete(JobStatusHistoryRecord).where(
                    col(JobStatusHistoryRecord.job_id) == job_id
                )
            )
            await session.execute(
                sa_delete(JobRecord).where(col(JobRecord.id) == job_id)
            )
            await session.commit()
