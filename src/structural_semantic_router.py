"""Category coverage for independent native and source structural observations.

One selected result per category avoids adding overlapping partial inventories.
Groups retain separate evidence domains and identities, including replaced groups.
This module performs no discovery, licensed work, source capture or cache access.
"""
from __future__ import annotations

from collections import Counter
import copy

NPI_PORT_CATEGORIES = frozenset({"constant_connection", "open_input"})


def category_result(result, category):
    if result is None:
        return {"status": "not_run", "gaps": ["category_not_checked"]}
    if category in result.get("category_results", {}):
        return result["category_results"][category]
    if result["status"] in {"not_run", "unavailable", "failed", "unsupported"}:
        return {"status": result["status"], "gaps": result.get("gaps", [])}
    if category not in result.get("categories_checked", []):
        return {"status": "not_run", "gaps": result.get("gaps", []) or ["category_not_checked"]}
    return {"status": result["status"], "gaps": result.get("gaps", [])}


def missing_categories(categories, native):
    return [c for c in categories if category_result(native, c)["status"] != "complete"]


def combine_semantics(options, native=None, source=None, *, attempts=(), elapsed_ms=0):
    categories, scope = options["categories"], options["scope"]
    groups, selected, coverage, facts = [], {}, [], []
    for backend, raw in (("verdi_npi", native), ("slang", source)):
        if raw is None:
            continue
        group_id = backend
        groups.append({
            "group_id": group_id, "backend": backend,
            "artifact_identity": raw.get("artifact_identity"), "scope": scope,
            "evidence_domain": "elaborated_input_ports" if backend == "verdi_npi" else "source_semantics",
            "status": raw["status"], "categories_checked": raw.get("categories_checked", []),
            "selected_categories": [], "total_facts": raw.get("total_facts", 0),
            "gaps": raw.get("gaps", []), "limitations": raw.get("limitations", []),
        })
    for category in categories:
        nr, sr = category_result(native, category), category_result(source, category)
        # A complete source pass may replace a partial native category. Never
        # sum their overlapping facts or use a cache receipt as coverage proof.
        backend, raw, receipt = "none", None, nr
        if nr["status"] == "complete":
            backend, raw, receipt = "verdi_npi", native, nr
        elif sr["status"] == "complete" or (nr["status"] != "partial" and sr["status"] == "partial"):
            backend, raw, receipt = "slang", source, sr
        elif nr["status"] == "partial":
            backend, raw, receipt = "verdi_npi", native, nr
        elif source is not None:
            backend, raw, receipt = "slang", source, sr
        elif native is not None:
            backend, raw, receipt = "verdi_npi", native, nr
        rows = [dict(copy.deepcopy(f), backend=backend, result_group=backend)
                for f in (raw or {}).get("facts", []) if f["kind"] == category]
        facts.extend(rows)
        if raw is not None:
            selected.setdefault(backend, []).append(category)
        coverage.append({
            "category": category, "status": receipt["status"], "backend": backend,
            "result_group": backend if raw is not None else None,
            "scope": scope, "total_facts": len(rows), "gaps": list(receipt.get("gaps", [])),
            "evidence_domain": "elaborated_input_ports" if backend == "verdi_npi" else "source_semantics",
        })
    for group in groups:
        group["selected_categories"] = selected.get(group["backend"], [])
    complete = all(c["status"] == "complete" for c in coverage)
    checked = [c["category"] for c in coverage if c["status"] in {"complete", "partial"}]
    statuses = {c["status"] for c in coverage}
    status = ("complete" if complete else "partial" if checked else
              "unavailable" if statuses & {"unavailable", "unsupported", "failed"} else "not_run")
    backends = {c["backend"] for c in coverage if c["backend"] != "none"}
    # Preserve existing source fields (propagation, templates, display receipt).
    result = copy.deepcopy(source or {})
    for key in ("category_results", "artifact_identity", "limitations"):
        result.pop(key, None)
    result.update(status=status, scope=scope, backend=(next(iter(backends)) if len(backends) == 1 else "mixed" if backends else "none"),
                  facts=facts, total_facts=len(facts), counts=dict(Counter(f["kind"] for f in facts)),
                  categories_checked=checked, category_coverage=coverage, result_groups=groups,
                  instances_visited=sum(r.get("instances_visited", 0) for r in (native, source) if r),
                  backend_attempts=list(attempts),
                  gaps=sorted({gap for c in coverage if c["status"] != "complete" for gap in c["gaps"]}),
                  native_execution=copy.deepcopy((native or {}).get("native_execution", {})))
    result["metrics"] = {**(source or {}).get("metrics", {}), **(native or {}).get("metrics", {}),
                         "semantic_total_ms": elapsed_ms}
    # Two individually bounded inventories can exceed the display allowance.
    # Preserve computed counts/coverage; this is display trimming, not a proof gap.
    display_limit = options.get("limits", {}).get("max_facts", len(facts))
    if len(facts) > display_limit:
        result["facts"] = facts[:display_limit]
        result["output_truncated"] = True
    result["note"] = "Structural evidence, not confirmed defects. Read each category's domain, identity and gaps; KDB/source association is unverified."
    return result
