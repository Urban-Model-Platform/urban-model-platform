"""PgAdvisoryPollLock — PostgreSQL session-level advisory lock.

Maps a job UUID to a stable int64 key and holds a session-level advisory lock
for the duration of the poll loop.  Key properties:

- Non-blocking: ``pg_try_advisory_lock`` returns immediately.
- Session-scoped: the lock is released automatically if the DB connection is
  closed, so a crashed instance never permanently blocks a job.
- Each instance opens its own dedicated connection per lock (via
  ``session_factory``), which is kept open until ``release()`` is called.

UUID → int64 mapping
--------------------
We take the first 8 bytes of the UUID's big-endian byte representation and
interpret them as a signed 64-bit integer.  Collision probability is
negligible for random UUIDs.
"""

from __future__ import annotations

import logging
import uuid as _uuid_mod
from typing import Any, Dict

from sqlalchemy import text

from ump.core.interfaces.poll_lock import PollLockPort

_log = logging.getLogger(__name__)


def _uuid_to_lock_key(job_id: str) -> int:
    """Convert a UUID string to a signed int64 advisory lock key."""
    raw = _uuid_mod.UUID(job_id).bytes[:8]
    return int.from_bytes(raw, "big", signed=True)


class PgAdvisoryPollLock(PollLockPort):
    """PostgreSQL advisory lock for exclusive poll-loop ownership.

    One persistent session is kept open per held lock.  The session is
    closed on ``release()``.  This is intentional: advisory locks are
    session-scoped in PostgreSQL, and closing the session releases the lock.
    """

    def __init__(self, session_factory: Any) -> None:
        self._session_factory = session_factory
        # job_id → open async session holding the lock
        self._held: Dict[str, Any] = {}

    async def try_acquire(self, job_id: str) -> bool:
        if job_id in self._held:
            return True  # already held by this instance

        key = _uuid_to_lock_key(job_id)
        session = self._session_factory()
        try:
            await session.__aenter__()
            row = await session.execute(
                text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
            )
            acquired: bool = row.scalar()
            if acquired:
                self._held[job_id] = session
                _log.debug("[poll-lock] acquired key=%d job_id=%s", key, job_id)
                return True
            # Lock not acquired — close session immediately
            await session.__aexit__(None, None, None)
            _log.debug(
                "[poll-lock] not acquired (held elsewhere) key=%d job_id=%s",
                key,
                job_id,
            )
            return False
        except Exception as exc:
            _log.warning("[poll-lock] try_acquire failed job_id=%s err=%s", job_id, exc)
            try:
                await session.__aexit__(type(exc), exc, None)
            except Exception:
                pass
            return False

    async def release(self, job_id: str) -> None:
        session = self._held.pop(job_id, None)
        if session is None:
            return
        key = _uuid_to_lock_key(job_id)
        try:
            await session.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
            await session.commit()
        except Exception as exc:
            _log.warning("[poll-lock] unlock failed job_id=%s err=%s", job_id, exc)
        finally:
            try:
                await session.__aexit__(None, None, None)
            except Exception:
                pass
        _log.debug("[poll-lock] released key=%d job_id=%s", key, job_id)
