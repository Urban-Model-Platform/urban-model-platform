"""Unit tests for the outbound transmission-mode rewrite.

Covers ``_rewrite_outbound_transmission_mode`` — the pure function that decides
what ``transmissionMode`` UMP sends to the remote model server based on the
process's configured ``transmission-mode-policy``.

Contract under test:
  - ``emulate-ref`` / ``emulate-ref-only``: a client-requested
    ``transmissionMode: reference`` is downgraded to ``value`` for the outbound
    request (UMP fulfils the reference itself by storing the result).
  - ``value`` requests, and all other policies, are passed through unchanged.
  - The input payload is never mutated (client intent is preserved elsewhere).
"""

import pytest

from ump.core.managers.steps.execution_steps import (
    _rewrite_outbound_transmission_mode,
)


@pytest.mark.parametrize("policy", ["emulate-ref", "emulate-ref-only"])
def test_reference_downgraded_to_value_for_store_owning_policies(policy):
    payload = {
        "inputs": {"x": 1},
        "outputs": {"result": {"transmissionMode": "reference", "format": {}}},
    }

    out = _rewrite_outbound_transmission_mode(payload, policy)

    assert out["outputs"]["result"]["transmissionMode"] == "value"
    # Other keys are preserved verbatim.
    assert out["outputs"]["result"]["format"] == {}
    assert out["inputs"] == {"x": 1}


@pytest.mark.parametrize("policy", ["emulate-ref", "emulate-ref-only"])
def test_value_request_passed_through(policy):
    payload = {"outputs": {"result": {"transmissionMode": "value"}}}

    out = _rewrite_outbound_transmission_mode(payload, policy)

    assert out["outputs"]["result"]["transmissionMode"] == "value"


@pytest.mark.parametrize("policy", ["pass-through", "value-only", None])
def test_non_store_owning_policies_untouched(policy):
    payload = {"outputs": {"result": {"transmissionMode": "reference"}}}

    out = _rewrite_outbound_transmission_mode(payload, policy)

    # Reference is left as-is: UMP does not take ownership under these policies.
    assert out["outputs"]["result"]["transmissionMode"] == "reference"
    assert out is payload  # no copy when nothing changes


def test_input_payload_is_not_mutated():
    payload = {
        "outputs": {"result": {"transmissionMode": "reference"}},
    }

    out = _rewrite_outbound_transmission_mode(payload, "emulate-ref")

    # Original intent preserved on the input object …
    assert payload["outputs"]["result"]["transmissionMode"] == "reference"
    # … while the returned copy carries the downgraded value.
    assert out["outputs"]["result"]["transmissionMode"] == "value"
    assert out is not payload


def test_mixed_outputs_only_reference_entries_rewritten():
    payload = {
        "outputs": {
            "ref_out": {"transmissionMode": "reference"},
            "val_out": {"transmissionMode": "value"},
            "default_out": {"format": {"mediaType": "application/geo+json"}},
        },
    }

    out = _rewrite_outbound_transmission_mode(payload, "emulate-ref")

    assert out["outputs"]["ref_out"]["transmissionMode"] == "value"
    assert out["outputs"]["val_out"]["transmissionMode"] == "value"
    # An output without an explicit transmissionMode is left untouched
    # (OGC default is already "value").
    assert "transmissionMode" not in out["outputs"]["default_out"]


def test_missing_or_empty_outputs_is_noop():
    assert _rewrite_outbound_transmission_mode({}, "emulate-ref") == {}
    assert _rewrite_outbound_transmission_mode({"inputs": {"x": 1}}, "emulate-ref") == {
        "inputs": {"x": 1}
    }
