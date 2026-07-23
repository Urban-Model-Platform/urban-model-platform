"""AuthorizationService — decides whether a caller may execute a process.

This is pure business logic and lives in the core: it depends only on
``ProvidersPort`` (to read anonymous-access configuration) and on the
``AuthContext`` produced by the auth adapter.  The web adapter merely calls
``check_process_access`` and lets the raised ``OGCProcessException`` propagate
to the global error handler.

Authorization rules (role-based, two levels):
- A process configured with ``anonymous-access: true`` is executable without
  authentication.
- A ``{provider_name}`` role grants access to every process of that provider.
- A ``{provider_name}:{process_bare_id}`` role grants access to one process.
"""

from __future__ import annotations

import logging
from typing import Optional

from ump.core.exceptions import OGCProcessException
from ump.core.interfaces.auth import AuthContext
from ump.core.interfaces.providers import ProvidersPort
from ump.core.models.ogcp_exception import OGCExceptionResponse

_log = logging.getLogger(__name__)


class AuthorizationService:
    """Access-control decisions for process execution.

    Stateless apart from the injected ``ProvidersPort``. Safe to share a single
    instance across requests.
    """

    def __init__(self, providers: ProvidersPort) -> None:
        self._providers = providers

    def check_process_access(self, auth: AuthContext, process_id: str) -> None:
        """Allow or deny execution of *process_id* for *auth*.

        Returns ``None`` when access is granted; raises ``OGCProcessException``
        with status 401 (unauthenticated) or 403 (authenticated but lacking the
        required role) otherwise.
        """
        if self._is_anonymous_process(process_id):
            return

        if not auth.is_authenticated:
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Unauthorized",
                    status=401,
                    detail="Authentication required to execute this process.",
                    instance=None,
                )
            )

        provider_name = self._provider_of(process_id)
        if provider_name in auth.roles or process_id in auth.roles:
            return

        raise OGCProcessException(
            OGCExceptionResponse(
                type="about:blank",
                title="Forbidden",
                status=403,
                detail=f"Missing role '{provider_name}' or '{process_id}'.",
                instance=None,
            )
        )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_anonymous_process(self, process_id: str) -> bool:
        provider_name = self._provider_of(process_id)
        if not provider_name:
            return False
        try:
            provider = self._providers.get_provider(provider_name)
        except Exception as exc:
            _log.warning(
                "[authz] provider lookup failed for %r; denying anonymous access: %s",
                provider_name,
                exc,
            )
            return False
        if not provider:
            return False
        for proc_cfg in provider.processes:
            canonical = self._to_canonical_id(provider_name, proc_cfg.id)
            if canonical == process_id or proc_cfg.id == process_id:
                return proc_cfg.anonymous_access
        return False

    @staticmethod
    def _provider_of(process_id: str) -> Optional[str]:
        return process_id.split(":", 1)[0] if ":" in process_id else None

    @staticmethod
    def _to_canonical_id(provider_name: str, configured_id: str) -> str:
        if configured_id.startswith(f"{provider_name}:"):
            return configured_id
        return f"{provider_name}:{configured_id}"
