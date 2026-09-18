"""Read positive constant-port facts from a compatible prepared query artifact."""
from collections import Counter
from dataclasses import asdict

from .cancellation import check_cancelled
from .connectivity_ir import BindingSourceKind, PortDirection


def scan_ir_connections(entry, options):
    scope = options["scope"]
    facts = []
    gaps = {"query_artifact_port_bindings_only"}
    gaps.update(entry.artifact_scope_receipt.gap_codes)
    for binding in entry.ir.bindings:
        check_cancelled()
        if binding.direction is not PortDirection.INPUT:
            continue
        if scope and not (binding.instance_path == scope or binding.instance_path.startswith(scope + ".")):
            continue
        if "constant_connection" not in options["categories"]:
            continue
        for mapping in binding.mappings:
            if mapping.source_kind is not BindingSourceKind.CONSTANT:
                continue
            if len(facts) >= options["limits"]["max_facts"]:
                gaps.add("fact_limit")
                break
            facts.append({"kind": "constant_connection", "instance": binding.instance_path,
                          "port": binding.port_name, "target_bits": list(mapping.target.bits),
                          "bits": "".join(mapping.constant_bits), "origin": "query_ir_input_binding",
                          "source": asdict(binding.evidence.location)})
        if "fact_limit" in gaps:
            break
    return {"status": "partial", "scope": scope, "facts": facts, "total_facts": len(facts),
            "counts": dict(Counter(f["kind"] for f in facts)), "categories_checked": ["constant_connection"] if "constant_connection" in options["categories"] else [],
            "gaps": sorted(gaps), "cache_disposition": "hit_query_artifact",
            "query_artifact_status": "reused", "metrics": {"frontend_build_count": 0},
            "note": "Positive constant-port facts from a bounded query artifact. Other ports, assignments and comparisons remain unchecked; request deep for those checks."}
