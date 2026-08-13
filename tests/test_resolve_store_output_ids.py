"""Unit tests for ``_resolve_store_output_ids``.

This pure function decides which outputs UMP writes to the result store when a
completed job is being processed. The critical contract it enforces:

Under ``emulate-ref``, storage is scoped to exactly the outputs the client
requested as ``transmissionMode: reference``. A ``value``-requested output
(e.g. a non-geospatial ``application/json`` table) must never enter the store
batch — otherwise it raises ``UnsupportedResultError`` and wrongly downgrades
the whole job, including the geospatial output the client wanted as a reference.
This is the regression guard for the mixed-output voronoi bug.
"""

from ump.core.services.result_storage_coordinator import (
    _resolve_store_output_ids,
)


def test_emulate_ref_stores_only_reference_requested_outputs():
    outputs_spec = {
        "voronoi_diagram": {"transmissionMode": "reference"},
        "classification_breaks_wb": {"transmissionMode": "value"},
        "classification_breaks_ma": {"transmissionMode": "value"},
    }

    ids = _resolve_store_output_ids(
        policy="emulate-ref",
        outputs_spec=outputs_spec,
        store_outputs_config=None,
    )

    assert ids == ["voronoi_diagram"]


def test_emulate_ref_multiple_reference_outputs():
    outputs_spec = {
        "geo_a": {"transmissionMode": "reference"},
        "geo_b": {"transmissionMode": "reference"},
        "table": {"transmissionMode": "value"},
    }

    ids = _resolve_store_output_ids("emulate-ref", outputs_spec, None)

    assert ids == ["geo_a", "geo_b"]


def test_explicit_store_outputs_config_wins():
    # Operator config always takes precedence, regardless of policy/spec.
    outputs_spec = {"voronoi_diagram": {"transmissionMode": "reference"}}
    config = ["results.voronoi"]

    ids = _resolve_store_output_ids("emulate-ref", outputs_spec, config)

    assert ids == ["results.voronoi"]


def test_emulate_ref_only_auto_detects_all():
    # Under emulate-ref-only every output is a reference by policy, so we let
    # the extractor auto-detect (None), rather than narrowing by client intent.
    outputs_spec = {
        "geo": {"transmissionMode": "reference"},
        "other": {"transmissionMode": "value"},
    }

    ids = _resolve_store_output_ids("emulate-ref-only", outputs_spec, None)

    assert ids is None


def test_emulate_ref_no_spec_falls_back_to_auto_detect():
    ids = _resolve_store_output_ids("emulate-ref", None, None)
    assert ids is None


def test_emulate_ref_only_value_outputs_falls_back_to_auto_detect():
    # Defensive: should_store guarantees at least one reference under
    # emulate-ref, but if none is present we must not pass an empty list
    # (which the extractor would treat as "store nothing / navigate []").
    outputs_spec = {"table": {"transmissionMode": "value"}}

    ids = _resolve_store_output_ids("emulate-ref", outputs_spec, None)

    assert ids is None
