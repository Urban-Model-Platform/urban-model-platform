from dataclasses import dataclass
from typing import Any, Literal

TransmissionMode = Literal["value", "reference"]
TransmissionModePolicy = Literal[
    "pass-through",
    "emulate-ref",
    "emulate-ref-only",
    "value-only",
]
ResultStorage = Literal["remote", "geoserver", "ldproxy"]


class TransmissionPolicyError(ValueError):
    """Invalid combination of request, policy and storage configuration."""


@dataclass(frozen=True)
class TransmissionDecision:
    requested_mode: TransmissionMode
    forwarded_mode: TransmissionMode
    delivered_mode: TransmissionMode
    store_results: bool


def extract_requested_mode_from_outputs(
    outputs: dict[str, Any] | None,
    default_mode: TransmissionMode = "value",
) -> TransmissionMode:
    """Extract and validate a global requested transmission mode from outputs.

    All outputs must use the same transmissionMode. If no transmissionMode is
    provided for any output, ``default_mode`` is returned.

    Args:
        outputs: Output definitions from execute request (keyed by output ID)
        default_mode: Mode to use if no transmissionMode specified in request.
                      Should be set based on transmission-mode-policy.
    """
    if not outputs:
        return default_mode

    found_modes: set[TransmissionMode] = set()

    for output_id, output_def in outputs.items():
        if not isinstance(output_def, dict):
            continue

        mode = output_def.get("transmissionMode")
        if mode is None:
            continue

        if mode not in {"value", "reference"}:
            raise TransmissionPolicyError(
                f"Invalid transmissionMode '{mode}' for output '{output_id}'. "
                "Allowed values are 'value' and 'reference'."
            )

        found_modes.add(mode)

    if not found_modes:
        return default_mode

    if len(found_modes) > 1:
        raise TransmissionPolicyError("All outputs must use the same transmissionMode.")

    return next(iter(found_modes))


def get_default_transmission_mode(policy: TransmissionModePolicy) -> TransmissionMode:
    """Determine default transmission mode based on policy.

    When a client doesn't specify transmissionMode in the execute request,
    use a sensible default that aligns with the provider's policy:

    - ``value-only``: Must default to "value" (only mode allowed)
    - ``emulate-ref-only``: Must default to "reference" (only mode allowed)
    - ``emulate-ref``: Default to "reference" (the use case of emulate-ref)
    - ``pass-through``: Default to "value" (OGC API default)

    Args:
        policy: The transmission-mode-policy configured for the process

    Returns:
        TransmissionMode: The appropriate default for the policy
    """
    if policy == "emulate-ref-only":
        return "reference"
    if policy == "emulate-ref":
        return "reference"
    # pass-through and value-only default to value
    return "value"


def decide_transmission(
    requested_mode: TransmissionMode,
    policy: TransmissionModePolicy,
    result_storage: ResultStorage,
) -> TransmissionDecision:
    """Compute effective forward/deliver/store behavior from policy."""
    if result_storage == "ldproxy":
        raise TransmissionPolicyError(
            "result-storage 'ldproxy' is configured but not implemented yet."
        )

    if policy in {"emulate-ref", "emulate-ref-only"} and result_storage == "remote":
        raise TransmissionPolicyError(
            f"Policy '{policy}' requires result-storage 'geoserver'."
        )

    if policy == "pass-through":
        # Keep remote behavior untouched. UMP does not synthesize references,
        # therefore delivery is handled as regular remote results retrieval.
        return TransmissionDecision(
            requested_mode=requested_mode,
            forwarded_mode=requested_mode,
            delivered_mode="value",
            store_results=False,
        )

    if policy == "emulate-ref":
        if requested_mode == "reference":
            return TransmissionDecision(
                requested_mode="reference",
                forwarded_mode="value",
                delivered_mode="reference",
                store_results=True,
            )

        return TransmissionDecision(
            requested_mode="value",
            forwarded_mode="value",
            delivered_mode="value",
            store_results=False,
        )

    if policy == "emulate-ref-only":
        if requested_mode != "reference":
            raise TransmissionPolicyError(
                "Policy 'emulate-ref-only' only allows transmissionMode='reference'."
            )

        return TransmissionDecision(
            requested_mode="reference",
            forwarded_mode="value",
            delivered_mode="reference",
            store_results=True,
        )

    if policy == "value-only":
        if requested_mode != "value":
            raise TransmissionPolicyError(
                "Policy 'value-only' only allows transmissionMode='value'."
            )

        return TransmissionDecision(
            requested_mode="value",
            forwarded_mode="value",
            delivered_mode="value",
            store_results=False,
        )

    raise TransmissionPolicyError(f"Unknown transmission-mode-policy '{policy}'.")


def extract_output_transmission_modes(
    outputs: dict[str, Any] | None,
) -> dict[str, TransmissionMode]:
    """Extract individual transmissionMode per output ID.

    Returns a mapping of output_id -> transmissionMode for all outputs
    that explicitly specify a transmissionMode, or have it set to "value" by default.

    This is used to preserve the original per-output transmission modes
    before they are rewritten by the forwarded_mode policy.
    """
    if not outputs or not isinstance(outputs, dict):
        return {}

    modes: dict[str, TransmissionMode] = {}
    for output_id, output_def in outputs.items():
        if not isinstance(output_def, dict):
            continue
        # Default to "value" if not explicitly specified
        mode = output_def.get("transmissionMode", "value")
        if mode in {"value", "reference"}:
            modes[output_id] = mode
    return modes


def apply_forwarded_mode_to_execute_outputs(
    execute_body: dict[str, Any],
    forwarded_mode: TransmissionMode,
    process_output_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Rewrite execute request outputs to one global forwarded mode.

    - If outputs are present, all listed outputs get ``forwarded_mode``.
    - If outputs are absent and process output ids are known, outputs are created
      explicitly so downstream gets deterministic behavior.
    """
    body = dict(execute_body)
    outputs = body.get("outputs")

    if isinstance(outputs, dict) and outputs:
        rewritten: dict[str, Any] = {}
        for output_id, output_def in outputs.items():
            entry = dict(output_def) if isinstance(output_def, dict) else {}
            entry["transmissionMode"] = forwarded_mode
            rewritten[output_id] = entry

        body["outputs"] = rewritten
        return body

    if process_output_ids:
        body["outputs"] = {
            output_id: {"transmissionMode": forwarded_mode}
            for output_id in process_output_ids
        }

    return body
