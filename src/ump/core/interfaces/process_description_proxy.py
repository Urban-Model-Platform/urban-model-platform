"""Port: ProcessDescriptionProxyPort.

UMP is the authoritative source of the process descriptions it serves to
clients.  This port is the single place where that authority is exercised:
an implementation receives the raw process description fetched from a remote
server together with the UMP-level configuration for that process, and returns
the version UMP will advertise.

Two invariants that every implementation must respect:
  1. Return a *new* Process object — never mutate the one passed in.
  2. A None config is never passed here.  The composition root is responsible
     for choosing the right adapter (or the pass-through no-op) when a
     ProcessConfig is unavailable.
"""

from abc import ABC, abstractmethod

from ump.core.models.process import Process
from ump.core.models.providers_config import ProcessConfig


class ProcessDescriptionProxyPort(ABC):
    """Transforms the process description UMP will advertise to clients.

    Called once per process fetch, after the raw dict has been validated into a
    Process model.  The result is what gets cached and served.
    """

    @abstractmethod
    def apply(self, process: Process, config: ProcessConfig) -> Process:
        """Return the UMP-advertised version of *process*.

        Args:
            process: The Process model as returned by the remote server (after
                     the handler pipeline has run).
            config:  The UMP ProcessConfig for this process.  Never None —
                     the caller must supply a real config or choose the
                     PassThroughProcessDescriptionProxy instead.

        Returns:
            A (possibly new) Process object that clients will receive.
            Implementations that make no changes may return the same object.
        """
