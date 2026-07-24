"""NoOpPollLock — always grants the lock.

Used for single-instance deployments, the in-memory job store, and tests.
"""

from ump.core.interfaces.poll_lock import PollLockPort


class NoOpPollLock(PollLockPort):
    async def try_acquire(self, job_id: str) -> bool:
        return True

    async def release(self, job_id: str) -> None:
        pass
