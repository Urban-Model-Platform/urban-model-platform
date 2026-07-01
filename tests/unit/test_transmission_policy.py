import pytest

from ump.api.transmission_policy import (
    TransmissionPolicyError,
    apply_forwarded_mode_to_execute_outputs,
    decide_transmission,
    extract_requested_mode_from_outputs,
)


def test_extract_requested_mode_default_when_outputs_missing():
    assert extract_requested_mode_from_outputs(None, default_mode="value") == "value"


def test_extract_requested_mode_rejects_mixed_outputs():
    outputs = {
        "a": {"transmissionMode": "value"},
        "b": {"transmissionMode": "reference"},
    }

    with pytest.raises(TransmissionPolicyError):
        extract_requested_mode_from_outputs(outputs)


def test_extract_requested_mode_rejects_invalid_value():
    outputs = {"a": {"transmissionMode": "foo"}}

    with pytest.raises(TransmissionPolicyError):
        extract_requested_mode_from_outputs(outputs)


def test_decide_transmission_emulate_ref_reference_requests_store():
    decision = decide_transmission(
        requested_mode="reference",
        policy="emulate-ref",
        result_storage="geoserver",
    )

    assert decision.forwarded_mode == "value"
    assert decision.delivered_mode == "reference"
    assert decision.store_results is True


def test_decide_transmission_emulate_ref_value_does_not_store():
    decision = decide_transmission(
        requested_mode="value",
        policy="emulate-ref",
        result_storage="geoserver",
    )

    assert decision.forwarded_mode == "value"
    assert decision.delivered_mode == "value"
    assert decision.store_results is False


def test_decide_transmission_rejects_missing_store_for_emulate_ref_only():
    with pytest.raises(TransmissionPolicyError):
        decide_transmission(
            requested_mode="reference",
            policy="emulate-ref-only",
            result_storage="remote",
        )


def test_decide_transmission_ldproxy_not_implemented():
    with pytest.raises(TransmissionPolicyError, match="not implemented yet"):
        decide_transmission(
            requested_mode="reference",
            policy="emulate-ref",
            result_storage="ldproxy",
        )


def test_apply_forwarded_mode_rewrites_all_existing_outputs():
    body = {
        "inputs": {"x": 1},
        "outputs": {
            "out1": {"transmissionMode": "reference"},
            "out2": {},
        },
    }

    rewritten = apply_forwarded_mode_to_execute_outputs(body, "value")

    assert rewritten["outputs"]["out1"]["transmissionMode"] == "value"
    assert rewritten["outputs"]["out2"]["transmissionMode"] == "value"


def test_apply_forwarded_mode_creates_outputs_when_missing():
    body = {"inputs": {"x": 1}}

    rewritten = apply_forwarded_mode_to_execute_outputs(
        body,
        "reference",
        process_output_ids=["result"],
    )

    assert rewritten["outputs"]["result"]["transmissionMode"] == "reference"
