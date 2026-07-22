"""AuthPort — port for verifying inbound JWT tokens and producing an AuthContext.

The core depends on this port to answer two questions on every request:
  1. Is this caller authenticated?
  2. What roles do they hold?

All JWT mechanics (JWKS fetching, signature verification, claim extraction)
are the adapter's concern.  The core only sees AuthContext.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class AuthContext:
    """Resolved identity and roles for a single inbound request.

    Passed into route handlers and authorization helpers.
    """

    user_id: Optional[str]       # None for anonymous / unauthenticated requests
    roles: List[str]             # flat list merged from all configured claim paths
    is_authenticated: bool       # False when token absent or auth disabled


class AuthPort(ABC):
    """Verify an inbound bearer token and return the caller's AuthContext.

    Implementations must be async-safe — they may be called concurrently from
    multiple request handlers.
    """

    @abstractmethod
    async def verify(self, token: Optional[str]) -> AuthContext:
        """Validate *token* and return the caller's identity and roles.

        Behaviour:
        - ``token`` is ``None``  →  return ``AuthContext(is_authenticated=False)``
                                    (caller is anonymous; route handler decides
                                    whether that is acceptable)
        - valid token            →  return ``AuthContext(is_authenticated=True, ...)``
        - expired / invalid      →  raise ``OGCProcessException`` with status 401

        When ``UMP_AUTH_ENABLED=false`` the adapter must return
        ``AuthContext(is_authenticated=False)`` for every token value, including
        malformed ones.
        """
