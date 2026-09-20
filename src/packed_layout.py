"""Export named packed members only from a content-anchored semantic artifact."""
from .cancellation import check_cancelled
from .connectivity_ir import CoverageStatus
from .waveform_selection import MAX_BITS, MAX_VALUE_BITS, MAX_SELECTIONS


def packed_field_selections(entry, source_signal, dump_path, fields):
    if not fields or len(fields) > MAX_SELECTIONS or len(set(fields)) != len(fields):
        raise ValueError("request 1..128 unique field names")
    if not entry.build_key.cross_request_reusable or entry.artifact_identity is None:
        raise ValueError("compile_identity_incomplete")
    # A bounded hierarchy can still contain complete types for its explicitly
    # projected instances. Other semantic gaps (including type/diagnostic gaps)
    # are conservatively rejected; connectivity completeness is not type proof.
    if entry.ir.coverage.blocking_diagnostic_count:
        raise ValueError("semantic_type_diagnostics")
    if (entry.ir.coverage.status is not CoverageStatus.COMPLETE and
            (not entry.ir.coverage.gaps or any(g.code != "hierarchy_projection_scoped" for g in entry.ir.coverage.gaps))):
        raise ValueError("semantic_type_coverage_incomplete")
    engine = entry.query_engine
    root = engine.resolve_signal(source_signal)
    instance = engine.instance_index[root.instance_path]
    if instance.path not in entry.artifact_identity.scope.projection_instance_paths:
        raise ValueError("port_binding_only_scope_has_no_complete_type_layout")
    definition = engine.definition_index[instance.definition_id]
    declared = definition.direct_signal_range(root.symbol)
    if declared is None or root.bits != declared.indices or declared.width > MAX_BITS:
        raise ValueError("whole_packed_declaration_required")
    members = {m.name: m for m in definition.packed_members if m.aggregate == root.symbol}
    result = {}
    for field in fields:
        check_cancelled()
        name = root.symbol + "." + field
        if name not in members:
            raise ValueError("packed_member_type_evidence_missing")
        selected = engine.resolve_signal(source_signal + "." + field)
        if len(selected.bits) > MAX_VALUE_BITS:
            raise ValueError("packed_member_width_budget; map smaller fields explicitly")
        if selected.instance_path != root.instance_path or selected.symbol != root.symbol:
            raise ValueError("packed_member_root_mismatch")
        result[field] = {"path": dump_path, "bits": list(selected.bits)}
        if sum(len(value["bits"]) for value in result.values()) > MAX_BITS:
            raise ValueError("packed_field_bit_budget")
    return {"status": "resolved", "fields": result, "reasons": [],
            "evidence": {"compile_fingerprint_sha256": entry.artifact_identity.source.compile_inputs.fingerprint,
                         "artifact_fingerprint_sha256": entry.ir_fingerprint_sha256,
                         "instance": instance.path, "definition_id": instance.definition_id,
                         "parameterization": dict(instance.parameterization),
                         "source_signal": source_signal,
                         "declared_range": {"left": declared.left, "right": declared.right},
                         "layout_source": "source_graph_packed_members",
                         "validity": "current_compile_snapshot; re-resolve after input changes"}}
