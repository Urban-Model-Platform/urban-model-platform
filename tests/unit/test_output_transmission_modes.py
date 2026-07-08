"""Tests for per-output transmission mode preservation."""

import json

import pytest

from ump.api.transmission_policy import extract_output_transmission_modes


def test_extract_no_outputs():
    """Empty outputs should return empty dict."""
    result = extract_output_transmission_modes(None)
    assert result == {}

    result = extract_output_transmission_modes({})
    assert result == {}


def test_extract_single_output_with_mode():
    """Single output with explicit mode."""
    outputs = {"result": {"transmissionMode": "reference"}}
    result = extract_output_transmission_modes(outputs)
    assert result == {"result": "reference"}


def test_extract_multiple_outputs_mixed_modes():
    """Multiple outputs with different modes."""
    outputs = {
        "output_a": {"transmissionMode": "value"},
        "output_b": {"transmissionMode": "reference"},
        "output_c": {},  # no explicit mode
    }
    result = extract_output_transmission_modes(outputs)
    assert result == {
        "output_a": "value",
        "output_b": "reference",
        "output_c": "value",  # defaults to value
    }


def test_extract_defaults_to_value_when_missing():
    """Output without transmissionMode defaults to 'value'."""
    outputs = {
        "result": {},
    }
    result = extract_output_transmission_modes(outputs)
    assert result == {"result": "value"}


def test_extract_ignores_non_dict_outputs():
    """Non-dict output definitions are skipped."""
    outputs = {
        "output_a": {"transmissionMode": "reference"},
        "output_b": "not_a_dict",  # Invalid, should be skipped
        "output_c": None,  # Invalid, should be skipped
    }
    result = extract_output_transmission_modes(outputs)
    assert result == {"output_a": "reference"}


def test_serialization_roundtrip():
    """Serialized modes can be deserialized back."""
    original = {"output_a": "value", "output_b": "reference"}
    serialized = json.dumps(original)
    deserialized = json.loads(serialized)
    assert deserialized == original


def test_extract_empty_string_for_non_dict_input():
    """Non-dict input returns empty dict."""
    result = extract_output_transmission_modes("not_a_dict")
    assert result == {}

    result = extract_output_transmission_modes(123)
    assert result == {}
