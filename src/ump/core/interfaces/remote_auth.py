"""RemoteAuthPort — port for resolving per-provider outbound authentication.

The core depends on this port to obtain HTTP headers that should be added to
every outbound request to a remote OGC API Processes server.  The concrete
mechanism (Basic, Bearer, API-key, OAuth2 …) is entirely the adapter's concern.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict

from ump.core.models.providers_config import AuthConfig


@dataclass
class ProviderCredentials:
    """Ready-to-use credentials expressed as HTTP headers.

    All auth types are normalised to headers so ``HttpClientPort`` needs no
    knowledge of authentication at all.
    """

    headers: Dict[str, str] = field(default_factory=dict)


class RemoteAuthPort(ABC):
    """Resolve a provider's ``AuthConfig`` to concrete HTTP credentials."""

    @abstractmethod
    def resolve(self, auth_config: AuthConfig) -> ProviderCredentials:
        """Return headers to add to every outbound request for this provider."""
