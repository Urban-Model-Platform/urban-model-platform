"""Adapter: process description proxy implementations.

Two concrete adapters live here:

PassThroughProcessDescriptionProxy
    The no-op.  Used when a process has no UMP-level configuration (e.g. a
    process that exists on the remote but is not listed in providers.yaml).
    Returns the process unchanged.

PolicyBasedProcessDescriptionProxy
    Rewrites the process description so that it reflects what UMP commits to
    deliver, not just what the remote server natively supports.

    Feature V scope: rewrites ``outputTransmission`` based on the configured
    ``transmission_mode_policy``.  This is the only field clients need to see
    differently depending on policy — it tells them which transmission modes
    they may request.

    Designed to grow: future work (Feature VIII) may add response_mode_policy
    rewriting, custom link injection, etc. as additional methods or a richer
    config object.  The public API (the ``apply`` method) is stable.
"""

import logging

from ump.core.interfaces.process_description_proxy import ProcessDescriptionProxyPort
from ump.core.models.process import Process, ProcessOutputTransmission
from ump.core.models.providers_config import ProcessConfig

logger = logging.getLogger(__name__)


class PassThroughProcessDescriptionProxy(ProcessDescriptionProxyPort):
    """No-op proxy: returns the process description unchanged.

    Used when no UMP ProcessConfig exists for a process (e.g. the process is
    served by a remote but not explicitly configured in providers.yaml).  In
    that case UMP advertises exactly what the remote server reports.
    """

    def apply(self, process: Process, config: ProcessConfig) -> Process:
        # Nothing to change — the remote description is the advertised description.
        return process


class PolicyBasedProcessDescriptionProxy(ProcessDescriptionProxyPort):
    """Rewrites the process description to reflect UMP's policy commitments.

    UMP is the authoritative source for the process descriptions it serves to
    clients.  When a ``transmission_mode_policy`` other than ``pass-through``
    is configured, clients must not see the remote's raw ``outputTransmission``
    list — they must see what UMP guarantees it can deliver.

    Rewriting rules:

    ``pass-through``
        No change.  The remote's native ``outputTransmission`` is advertised.

    ``emulate-ref``
        UMP adds ``reference`` to ``outputTransmission`` if it is not already
        present.  The remote may not support it natively, but UMP will fulfil
        it by fetching the value result and writing it to the result store.

    ``emulate-ref-only``
        UMP replaces ``outputTransmission`` with ``["reference"]`` only.
        Clients cannot request ``value``; UMP always stores and returns a link.

    ``value-only``
        UMP replaces ``outputTransmission`` with ``["value"]`` only.
        Clients cannot request ``reference`` even if the remote supports it.
    """

    def apply(self, process: Process, config: ProcessConfig) -> Process:
        """Return the policy-adjusted process description.

        A model_copy is used so the original object (and any cached references
        to it) are never mutated.
        """
        policy = config.transmission_mode_policy

        if policy == "pass-through":
            # No adjustment needed — trust the remote's advertised capabilities.
            return process

        new_transmission = _rewrite_output_transmission(
            current=process.outputTransmission,
            policy=policy,
        )

        if new_transmission == process.outputTransmission:
            # The rewrite produced the same list — skip the copy.
            return process

        logger.debug(
            "ProcessDescriptionProxy: process=%s policy=%r outputTransmission %r -> %r",
            process.pid,
            policy,
            [t.value for t in process.outputTransmission],
            [t.value for t in new_transmission],
        )

        return process.model_copy(update={"outputTransmission": new_transmission})


# ---------------------------------------------------------------------------
# Internal helpers (private to this module)
# ---------------------------------------------------------------------------


def _rewrite_output_transmission(
    current: list[ProcessOutputTransmission],
    policy: str,
) -> list[ProcessOutputTransmission]:
    """Return the ``outputTransmission`` list UMP should advertise for *policy*.

    This is a pure function with no side effects — easy to unit-test directly.
    """
    VALUE = ProcessOutputTransmission.VALUE
    REFERENCE = ProcessOutputTransmission.REFERENCE

    if policy == "emulate-ref":
        # Guarantee that reference is available.  Keep value if it is already
        # present so clients that prefer inline results are not broken.
        if REFERENCE in current:
            return current  # already advertised
        return list(current) + [REFERENCE]

    if policy == "emulate-ref-only":
        # Only reference is allowed — UMP always stores and returns a link.
        return [REFERENCE]

    if policy == "value-only":
        # Only value is allowed — block reference even if the remote supports it.
        return [VALUE]

    # Unreachable if ProcessConfig.transmission_mode_policy validation is correct,
    # but guard defensively to avoid silent no-ops on unexpected values.
    logger.warning(
        "ProcessDescriptionProxy: unrecognised policy %r — "
        "returning outputTransmission unchanged",
        policy,
    )
    return current
