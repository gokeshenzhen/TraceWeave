"""Explicit section exports from captured metadata and one semantic provider.

An export may request global work, but never removes the query work budgets.
The versioned data is a semantic view, not a reconstructed legacy scan dict.
Legacy hierarchy handles keep their existing raw-section implementation.
"""
from __future__ import annotations

from .hierarchy_source_files import _budget, _collect_file, _metadata_row
from .hierarchy_source_ir import HIERARCHY_TOOL_SCHEMA_VERSION, HierarchyBudgetExceeded
from .hierarchy_source_metadata import build_compile_source_catalog, export_compile_metadata, observation_identity
from .hierarchy_source_service import HierarchySourceService


METADATA_SECTIONS = frozenset({"compile_result", "include_tree", "filelist_tree"})
SEMANTIC_SECTIONS = frozenset({"interfaces", "files_full", "component_tree_full", "class_hierarchy_full"})


def validate_section_query(*, section, max_items=5000):
    if not isinstance(section, str) or section not in METADATA_SECTIONS | SEMANTIC_SECTIONS:
        raise ValueError("unknown hierarchy section")
    if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 5000:
        raise ValueError("max_items must be an integer in [1, 5000]")


def _file_export(catalog, provider, section, max_items, budget):
    rows, seen, gaps, limits = [], {}, set(catalog.gaps), set()
    complete, truncated, examined, observed = True, False, 0, 0
    semantic_status = "not_run"
    try:
        for entry in catalog.files:
            budget.checkpoint()
            # An omitted compile member is already known from the catalog.
            # Do not query its symbols merely to establish output truncation.
            if section == "files_full" and len(rows) == max_items:
                complete, truncated = False, True
                break
            budget.admit_file(entry)
            examined += 1
            row = (_collect_file(provider, entry, budget) if provider is not None
                   else _metadata_row(entry, unavailable=True))
            semantic_status = row["semantic_status"]
            complete &= semantic_status == "complete"
            gaps.update(row["semantic_gaps"])
            limits.update(row["analysis_limits"])
            if section == "files_full":
                rows.append(row)
                observed += 1
            else:
                for symbol in row["symbols"] or []:
                    budget.checkpoint()
                    if symbol["kind"] != "interface":
                        continue
                    identity = symbol["symbol_id"]
                    if identity in seen:
                        if seen[identity] != symbol:
                            raise ValueError("conflicting interface declaration identity")
                        continue
                    seen[identity] = symbol
                    observed += 1
                    if len(rows) == max_items:
                        complete, truncated = False, True
                        break
                    rows.append(symbol)
            if truncated or limits:
                complete = False
                break
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        complete = False
        limits.add(exc.reason)
    complete &= not gaps and not limits
    if truncated:
        gaps.add("section_output_not_exported")
    return {
        "data": rows, "facts_origin": provider.origin if provider is not None and examined else "compile_metadata",
        "artifact_identity": provider.artifact_identity if provider is not None and examined else catalog.context_identity,
        "ownership_identity": catalog.context_identity, "ownership_basis": catalog.ownership_basis,
        "coverage_domain": "provider_file_declarations" if section == "files_full" else "provider_interface_declarations",
        "semantic_status": ("not_run" if not examined else "unavailable" if provider is None
                            else "complete" if complete else "partial"),
        "coverage_status": "complete" if complete else "partial", "coverage_gaps": sorted(gaps),
        "analysis_limits": sorted(limits), "output_truncated": truncated,
        "total": observed if complete else None, "count_lower_bound": observed,
        "count_status": "exact" if complete else "lower_bound", "examined_files": examined,
        "query_metrics": budget.metrics(),
    }


def export_hierarchy_section(context, provider=None, *, section, max_items=5000,
                             catalog=None, budget=None):
    """Export one section; the caller owns context validation/provider cleanup.

    Metadata sections do not inspect the provider or create a file catalog.
    Instance/class exports reuse their query engines, including real parent
    and base bindings. File exports enumerate captured compile members and
    retain the provider's declaration gaps. A partial or truncated export is
    explicitly incomplete even when an exact inventory count is available.
    """
    validate_section_query(section=section, max_items=max_items)
    budget = _budget(None if section in METADATA_SECTIONS else provider, budget)
    try:
        return _export_section(context, provider, section, max_items, catalog, budget)
    except HierarchyBudgetExceeded as exc:
        # Only preparation/metadata-copy stops reach here. Semantic query
        # engines retain their own admitted facts on a budget stop.
        return {
            "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "section": section,
            "data": None, "error": "section_export_incomplete", "export_complete": False,
            "facts_origin": "compile_metadata", "artifact_identity": observation_identity(context),
            "semantic_status": "not_run", "coverage_status": "partial",
            "coverage_gaps": list(context.gaps), "analysis_limits": [exc.reason], "output_truncated": False,
            "query_metrics": budget.metrics(),
            "warning": "section export is incomplete; inspect coverage and output limits",
        }


def _export_section(context, provider, section, max_items, catalog, budget):
    budget.checkpoint()
    if section in METADATA_SECTIONS:
        result = export_compile_metadata(context, section, checkpoint=budget.checkpoint)
        budget.admit_payload(result["data"])
        # record_export_complete reports an exact copy of the captured record.
        # The common export flag also requires complete reported coverage.
        complete = result["coverage_status"] == "complete" and not result["coverage_gaps"]
        return dict(result, schema_version=HIERARCHY_TOOL_SCHEMA_VERSION,
                    export_complete=complete, output_truncated=False, analysis_limits=[],
                    warning="" if complete else "section export is incomplete; inspect coverage and output limits",
                    query_metrics=budget.metrics())
    if section in {"files_full", "interfaces"}:
        if catalog is None:
            available = budget.max_bytes - budget.fact_bytes - 512
            if available < 1:
                raise HierarchyBudgetExceeded("fact_byte_limit")
            catalog = build_compile_source_catalog(context, max_bytes=min(16 * 1024 * 1024, available),
                                                   checkpoint=budget.checkpoint)
            budget.reserve_bytes(512 + catalog.metadata_bytes)
        if catalog.context_identity != observation_identity(context):
            raise ValueError("file catalog belongs to a different context")
        result = _file_export(catalog, provider, section, max_items, budget)
    else:
        if provider is None:
            result = {
                "data": None, "facts_origin": "compile_metadata", "artifact_identity": observation_identity(context),
                "semantic_status": "unavailable", "coverage_status": "partial",
                "coverage_gaps": ["semantic_provider_unavailable"], "analysis_limits": [],
                "output_truncated": False, "query_metrics": budget.metrics(),
            }
        else:
            service = HierarchySourceService(provider)
            if section == "component_tree_full":
                result = service.subtree(depth=-1, max_nodes=max_items, count_mode="exact", budget=budget)
                result["data"] = result.pop("node")
                result["view"] = "elaborated_instances"
            else:
                result = service.class_hierarchy(depth=-1, max_nodes=max_items, budget=budget)
                result["data"] = result.pop("roots")
            result["semantic_status"] = result["coverage_status"]
    complete = (result["coverage_status"] == "complete" and not result["output_truncated"]
                and not result["analysis_limits"] and "error" not in result)
    return dict(result, schema_version=HIERARCHY_TOOL_SCHEMA_VERSION, section=section,
                export_complete=complete,
                warning="" if complete else "section export is incomplete; inspect coverage and output limits")
