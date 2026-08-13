"""Unit tests for transmission-mode policy enforcement.

Covers:
  - ``_validate_transmission_mode_against_policy`` — the pure function that
    decides whether an execute request violates the configured
    ``transmission-mode-policy``.
  - ``EnforceTransmissionPolicyStep`` — the pipeline step that turns a violation
    into an OGC-conformant 400 Bad Request and halts the pipeline before any
    local job is created.

OGC API - Processes conformance: ``outputTransmission`` in the process
description advertises the transmission modes a process supports. UMP rewrites
that list per policy (``emulate-ref-only`` -> only ``reference``, ``value-only``
-> only ``value``). Requesting an unadvertised mode is an invalid request and
must be answered with 400.
"""

import pytest

from ump.core.managers.job_manager import JobExecutionContext
from ump.core.managers.steps.execution_steps import (
    EnforceTransmissionPolicyStep,
    _validate_transmission_mode_against_policy,
)

# ---------------------------------------------------------------------------
# Pure function: _validate_transmission_mode_against_policy
# ---------------------------------------------------------------------------


def test_emulate_ref_only_rejects_value_request():
    detail = _validate_transmission_mode_against_policy(
        {"result": {"transmissionMode": "value"}}, "emulate-ref-only"
    )
    assert detail is not None
    assert "value" in detail
    assert "reference" in detail


def test_value_only_rejects_reference_request():
    detail = _validate_transmission_mode_against_policy(
        {"result": {"transmissionMode": "reference"}}, "value-only"
    )
    assert detail is not None
    assert "reference" in detail
    assert "value" in detail


def test_emulate_ref_only_accepts_reference_request():
    detail = _validate_transmission_mode_against_policy(
        {"result": {"transmissionMode": "reference"}}, "emulate-ref-only"
    )
    assert detail is None


def test_value_only_accepts_value_request():
    detail = _validate_transmission_mode_against_policy(
        {"result": {"transmissionMode": "value"}}, "value-only"
    )
    assert detail is None


@pytest.mark.parametrize("policy", ["emulate-ref", "pass-through", None])
def test_non_exclusive_policies_never_reject(policy):
    # These policies advertise both modes (or impose no restriction), so any
    # client request is permitted.
    assert (
        _validate_transmission_mode_against_policy(
            {"result": {"transmissionMode": "value"}}, policy
        )
        is None
    )
    assert (
        _validate_transmission_mode_against_policy(
            {"result": {"transmissionMode": "reference"}}, policy
        )
        is None
    )


def test_no_explicit_transmission_mode_is_allowed():
    # When the client does not specify a transmissionMode, UMP applies the
    # policy default without rejecting (OGC default is "value").
    assert (
        _validate_transmission_mode_against_policy(
            {"result": {"format": {"mediaType": "application/geo+json"}}},
            "emulate-ref-only",
        )
        is None
    )


def test_empty_or_missing_outputs_is_allowed():
    assert _validate_transmission_mode_against_policy(None, "emulate-ref-only") is None
    assert _validate_transmission_mode_against_policy({}, "value-only") is None


def test_mixed_outputs_one_violation_is_rejected():
    detail = _validate_transmission_mode_against_policy(
        {
            "ok_out": {"transmissionMode": "reference"},
            "bad_out": {"transmissionMode": "value"},
        },
        "emulate-ref-only",
    )
    assert detail is not None
    assert "bad_out" in detail


# ---------------------------------------------------------------------------
# Pipeline step: EnforceTransmissionPolicyStep
# ---------------------------------------------------------------------------


class _StubProcessConfig:
    def __init__(self, policy):
        self.transmission_mode_policy = policy


@pytest.mark.asyncio
async def test_step_halts_with_400_on_violation():
    ctx = JobExecutionContext(
        process_id="prov:proc",
        output_specs={"result": {"transmissionMode": "value"}},
    )
    ctx.process_config = _StubProcessConfig("emulate-ref-only")

    await EnforceTransmissionPolicyStep().process(ctx)

    assert ctx.should_halt is True
    assert ctx.response is not None
    assert ctx.response["status"] == 400
    assert ctx.response["body"]["title"] == "Bad Request"
    assert "value" in ctx.response["body"]["detail"]
    # No job must have been created by this early-rejection step.
    assert ctx.job is None


@pytest.mark.asyncio
async def test_step_passes_valid_request_through():
    ctx = JobExecutionContext(
        process_id="prov:proc",
        output_specs={"result": {"transmissionMode": "reference"}},
    )
    ctx.process_config = _StubProcessConfig("emulate-ref-only")

    await EnforceTransmissionPolicyStep().process(ctx)

    assert ctx.should_halt is False
    assert ctx.response is None


@pytest.mark.asyncio
async def test_step_noop_when_process_config_missing():
    ctx = JobExecutionContext(
        process_id="prov:proc",
        output_specs={"result": {"transmissionMode": "value"}},
    )
    # process_config left as None (e.g. bare id fallback) — no policy to enforce.
    await EnforceTransmissionPolicyStep().process(ctx)

    assert ctx.should_halt is False
    assert ctx.response is None
