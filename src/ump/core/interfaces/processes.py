# ump/core/interfaces/processes.py
from abc import ABC, abstractmethod

from ump.core.models.process import Process, ProcessList


class ProcessesPort(ABC):
    @abstractmethod
    async def get_all_processes(self) -> ProcessList:
        pass

    @abstractmethod
    async def get_process(self, process_id: str) -> Process:
        """Return a full Process description for the given process_id.

        The process_id may be a provider-prefixed identifier (for example
        'provider:proc') or a bare identifier. If a provider prefix is included
        the implementation SHOULD query that provider first; otherwise it may
        search across configured providers.
        """
        pass

    @abstractmethod
    async def cleanup(self) -> None:
        """Cleanup resources"""
        pass

    @abstractmethod
    async def execute_process(
        self, process_id: str, body: dict, headers: dict, user_id: str | None = None
    ) -> dict:
        """Execute a process identified by process_id. Implementations should
        respect common OGC semantics, including honoring the Prefer header
        (e.g. Prefer: respond-async) when forwarding requests to providers.

        Returns a dict representing the provider response (or a local job
        representation for async executions).
        """
        pass
