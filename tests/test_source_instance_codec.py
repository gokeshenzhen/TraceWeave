"""Handwritten v2 instance wire facts; no native/provider semantics asserted."""
from copy import deepcopy

import pytest
from pydantic import ValidationError

from src.hierarchy_source_codec import validate_source_result


def row():
    # Instance and definition locations are deliberately different.
    return dict(scope_id="scope-a", path="top.u", inst="u", kind="module",
                parent="top", definition_id="definition-leaf", module="leaf",
                source_file="/tb/inst.sv", source_line=23,
                definition_file="/rtl/leaf.sv", definition_line=4,
                parameterization={"WIDTH": "8"}, gaps=[])


def result():
    return dict(schema_version=2, facts_origin="verdi_npi", artifact_identity="artifact-a",
                coverage_status="complete", coverage_gaps=[], analysis_limits=[],
                query_metrics=dict(scope_count=1, native_call_count=3, symbol_count=0,
                                   file_count=0, estimated_fact_bytes=1024, query_ms=1.5),
                count_status="exact", count_lower_bound=1, count_mode="bounded",
                count_domain="exact_path", output_truncated=False,
                hits=[row()], total=1, truncated=False)


def validate(payload):
    return validate_source_result("find_tb_instance", payload)


def test_instance_codec_copies_primitives_without_changing_locations_or_optional_presence():
    original = result()
    original["hits"][0].update(source_file=None, source_line=None)
    original["handle"] = None
    copied = validate(original)
    assert copied == original and copied is not original
    assert copied["hits"][0]["source_file"] is None
    assert copied["hits"][0]["definition_file"] == "/rtl/leaf.sv"
    assert "handle" in copied and copied["handle"] is None
    assert "design_handle" not in copied
    copied["hits"][0]["parameterization"]["WIDTH"] = "99"
    copied["coverage_gaps"].append("control_gap")
    assert original["hits"][0]["parameterization"] == {"WIDTH": "8"}
    assert original["coverage_gaps"] == []


def test_unknown_total_is_explicit_null_while_positive_lower_bound_is_retained():
    payload = result()
    payload.update(count_status="lower_bound", total=None, coverage_status="partial",
                   coverage_gaps=["matching_instances_not_enumerated"])
    assert validate(payload) == payload
    assert "total" in validate(payload) and validate(payload)["total"] is None


def test_complete_exact_negative_is_distinct_from_unavailable():
    negative = result()
    negative.update(hits=[], total=0, count_lower_bound=0)
    assert validate(negative) == negative
    unavailable = {key: value for key, value in result().items() if key in {
        "schema_version", "facts_origin", "artifact_identity", "coverage_status",
        "coverage_gaps", "analysis_limits", "query_metrics"}}
    unavailable.update(error="semantic_provider_unavailable", semantic_status="unavailable",
                       coverage_status="partial", facts_origin="compile_metadata")
    assert validate(unavailable) == unavailable
    assert "hits" not in validate(unavailable) and "total" not in validate(unavailable)


@pytest.mark.parametrize("field,value", [
    ("schema_version", True), ("schema_version", 1), ("schema_version", "2"),
    ("facts_origin", "native_guess"), ("count_lower_bound", True),
    ("count_lower_bound", -1), ("total", True), ("total", -1),
    ("count_status", "estimated"), ("count_mode", "approximate"),
    ("output_truncated", 1), ("unexpected", "field"),
])
def test_invalid_instance_envelope_is_rejected(field, value):
    payload = result()
    payload[field] = value
    with pytest.raises((ValueError, ValidationError)):
        validate(payload)


@pytest.mark.parametrize("updates", [
    {"count_status": "exact", "total": None},
    {"count_status": "exact", "total": 2},
    {"count_status": "lower_bound", "total": 1},
    {"count_lower_bound": 0, "total": 0},
    {"analysis_limits": ["timeout_sec"]},
    {"error": "semantic_provider_unavailable"},
])
def test_counts_and_complete_negative_proofs_cannot_contradict_evidence(updates):
    payload = result()
    payload.update(updates)
    with pytest.raises((ValueError, ValidationError)):
        validate(payload)


@pytest.mark.parametrize("updates", [
    {"source_line": None}, {"source_line": 0}, {"source_line": True},
    {"definition_file": None}, {"definition_id": None},
    {"parent": "top.u"}, {"scope_id": ""}, {"kind": "guessed_array"},
    {"parameterization": {"WIDTH": 8}}, {"gaps": ["raw error text"]},
])
def test_invalid_scope_identity_and_location_pairs_are_rejected(updates):
    payload = result()
    payload["hits"][0].update(updates)
    with pytest.raises((ValueError, ValidationError)):
        validate(payload)


@pytest.mark.parametrize("duplicate", ["id", "path"])
def test_duplicate_scope_or_path_is_not_a_second_instance(duplicate):
    payload = result()
    other = deepcopy(payload["hits"][0])
    if duplicate == "id":
        other.update(path="top.v", inst="v")
    else:
        other["scope_id"] = "scope-b"
    payload.update(hits=[payload["hits"][0], other], total=2, count_lower_bound=2)
    with pytest.raises(ValueError):
        validate(payload)


def test_display_truncation_does_not_destroy_an_independently_exact_count():
    payload = result()
    payload.update(count_domain="matching_instances_under_top", total=3,
                   count_lower_bound=3, count_mode="exact", output_truncated=True,
                   truncated=True)
    assert validate(payload) == payload


def test_generate_scope_can_have_no_module_definition_or_location():
    payload = result()
    payload["hits"][0].update(path="top.g[0]", inst="g[0]", kind="generate",
                              definition_id=None, module=None, source_file=None,
                              source_line=None, definition_file=None, definition_line=None)
    assert validate(payload) == payload
