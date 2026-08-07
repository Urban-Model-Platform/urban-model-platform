from datetime import datetime, timezone
from enum import StrEnum
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

from ump.core.models.link import Link


class StatusCode(StrEnum):
    accepted = "accepted"  # Details je nach statusCode.yaml
    failed = "failed"
    running = "running"
    successful = "successful"
    dismissed = "dismissed"


class JobStatusInfo(BaseModel):
    jobID: str
    status: StatusCode
    type: Literal["process"] = "process"
    processID: Optional[str] = None
    message: Optional[str] = None
    created: Optional[datetime] = None
    started: Optional[datetime] = None
    finished: Optional[datetime] = None
    updated: Optional[datetime] = None
    progress: Optional[int] = Field(None, ge=0, le=100)
    links: Optional[List[Link]] = None

    # V-10: additive, non-standard property (OGC API Processes explicitly
    # permits additional properties on statusInfo).  Set to "value" when the
    # client explicitly requested transmissionMode: reference under the
    # emulate-ref policy but storage failed, so the result was delivered
    # inline instead.  None in every other case — including jobs that never
    # attempted storage — so its mere presence signals a downgrade happened.
    transmissionModeApplied: Optional[str] = None


class JobList(BaseModel):
    jobs: List[JobStatusInfo]
    links: List[Link]


class Job(BaseModel):
    """Domain Job model (internal) distinct from user-facing OGC statusInfo.

    Notes:
    - `status_info` mirrors the latest `JobStatusInfo` snapshot; it MUST NOT contain inputs.
    - Inputs are stored separately (either inline for small payloads or via `inputs_url`).
    - OGC `statusInfo` schema does not define inputs, so we keep them off the DTO.
    - Timestamps: only local `created` and `updated` are kept here. Remote lifecycle
      timestamps (started, finished) remain inside `status_info` to avoid redundancy.
    - `status` duplicates the status code (string) for quick querying/indexing.
    - Helper accessors (`started_at`, `finished_at`) expose remote timestamps if present.
    """

    id: str  # local UUID
    user_id: Optional[str] = (
        None  # authenticated user who started the job; None = anonymous / public
    )
    process_id: Optional[str] = None
    provider: Optional[str] = None  # provider name or identifier
    remote_job_id: Optional[str] = (
        None  # upstream job id if provider manages jobs (can differ from local UUID)
    )
    remote_status_url: Optional[str] = (
        None  # absolute URL to poll for remote statusInfo
    )
    # ID separation rationale:
    # - We generate a local UUID (`id`) for stability, uniqueness across multiple providers, and to allow
    #   retries or multi-step orchestration without exposing upstream internals.
    # - Providers may use numeric counters, short hashes, or opaque strings; these become `remote_job_id`.
    # - The public API route `/jobs/{id}` always uses the local UUID to avoid collisions and leaking provider semantics.
    # - We store the remote id and status URL for correlation, debugging, and polling, but never depend on them
    #   for persistence keys. This enables potential future features like re-binding a local job to a new
    #   remote attempt while keeping the external identifier stable.

    status: Optional[str] = (
        None  # normalized status string (e.g., accepted, running, successful, failed)
    )
    status_info: Optional[JobStatusInfo] = None

    # Input handling (never embedded in statusInfo)
    inputs: Optional[dict] = None  # inline only if small
    inputs_url: Optional[str] = None  # object storage pointer or external URL
    inputs_storage: Optional[Literal["inline", "object", "external-url"]] = "inline"
    inputs_size: Optional[int] = None  # bytes
    inputs_checksum: Optional[str] = None  # sha256 or similar for audit/idempotency

    # Local timestamps (UTC). Remote started/finished live in status_info.
    created: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="Local creation timestamp (UTC)",
    )
    updated: Optional[datetime] = Field(
        default=None, description="Local last update timestamp (UTC)"
    )

    # Links (results, status, etc.)
    links: List[Link] = Field(default_factory=list)

    # V-10: outputs that have been persisted to a result store, keyed by
    # output_id.  Each value mirrors one core.interfaces.result_storage.
    # StoredReference as a plain dict ("collection_id", "collection_url",
    # "items_url") so it survives JSON round-tripping without importing the
    # dataclass into the JSONB-backed ORM layer.  None/empty means this job
    # never stored anything — GET /jobs/{id}/results then proxies the remote
    # result unchanged, exactly as before V-10.
    stored_outputs: Optional[dict] = None

    # Internal/diagnostic fields
    diagnostic: Optional[str] = None  # capture provider error text or failure reason
    version: int = 0  # optimistic concurrency / event sequencing

    # ---- Fields captured from the original execute request ----
    # These are needed by Feature VIII (result storage / policy enforcement) to
    # decide whether to store a result and how to unwrap it.  They are persisted
    # so the information is available at job-completion time even if the job was
    # created by a different UMP instance or a previous process run.
    response_mode: Optional[str] = None  # "raw" | "document" — client's response field
    outputs_spec: Optional[dict] = None  # verbatim execute-body "outputs" map

    def touch(self) -> None:
        """Update the `updated` timestamp (manager should call after mutations)."""
        self.updated = datetime.now(timezone.utc)

    def apply_status_info(self, info: JobStatusInfo) -> None:
        """Merge latest statusInfo snapshot and keep denormalized status field in sync."""
        self.status_info = info
        if info and info.status:
            # StatusCode is a StrEnum subclass of str; assign directly
            self.status = str(info.status)
        self.touch()

    # Convenience accessors for remote lifecycle timestamps (from status_info)
    def started_at(self) -> Optional[datetime]:  # pragma: no cover - simple accessor
        return self.status_info.started if self.status_info else None

    def finished_at(self) -> Optional[datetime]:  # pragma: no cover - simple accessor
        return self.status_info.finished if self.status_info else None

    def is_in_terminal_state(self) -> bool:  # pragma: no cover - simple logic
        if not self.status_info or not self.status_info.status:
            return False
        return self.status_info.status in {
            StatusCode.successful,
            StatusCode.failed,
            StatusCode.dismissed,
        }
