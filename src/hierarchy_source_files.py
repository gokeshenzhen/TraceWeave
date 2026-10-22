"""Bounded file queries over captured ownership and one semantic provider.

The caller pins/validates the context and binds the provider to that context.
Metadata filtering never opens a semantic stream. Semantic filters use positive
declarations or explicit complete negatives, never compiler type hints, bare
file-name association, or an empty unsupported iterator.
"""
from __future__ import annotations

from .hierarchy_source_ir import (
    HIERARCHY_TOOL_SCHEMA_VERSION, HierarchyBudgetExceeded, HierarchyQueryBudget,
)
from .hierarchy_source_metadata import CompileSourceCatalog
from .hierarchy_source_symbols import SourceSymbol, _gaps


def validate_file_query(operation, arguments):
    """Pure argument validation before a caller acquires a semantic backend."""
    allowed = {
        "get_tb_file_detail": {"path"},
        "lookup_tb_files": {"basename", "name_contains", "path_contains", "has_module",
                            "contains_uvm", "file_type", "limit", "count_mode"},
    }
    if operation not in allowed or not isinstance(arguments, dict) or set(arguments) - allowed[operation]:
        raise ValueError("unsupported file query or arguments")
    if operation == "get_tb_file_detail":
        if not isinstance(arguments.get("path"), str) or not arguments["path"]:
            raise ValueError("a nonempty source path is required")
        return
    text_filters = tuple(arguments.get(name) for name in
                         ("basename", "name_contains", "path_contains", "has_module", "file_type"))
    contains_uvm = arguments.get("contains_uvm")
    if all(value is None for value in (*text_filters, contains_uvm)):
        raise ValueError("at least one file filter is required")
    if any(value is not None and (not isinstance(value, str) or not value) for value in text_filters):
        raise ValueError("file filters must be nonempty strings")
    if contains_uvm is not None and not isinstance(contains_uvm, bool):
        raise ValueError("contains_uvm must be an explicit bool")
    limit = arguments.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        raise ValueError("limit must be an integer in [1, 5000]")
    if arguments.get("count_mode", "bounded") not in {"bounded", "exact"}:
        raise ValueError("count_mode must be bounded or exact")


def _budget(provider, requested):
    budget = requested if requested is not None else getattr(provider, "budget", None)
    budget = budget if budget is not None else HierarchyQueryBudget()
    if not isinstance(budget, HierarchyQueryBudget):
        raise TypeError("a hierarchy query budget is required")
    return budget


def _metadata_row(entry, *, unavailable=False):
    return dict(entry.row(), file_type=None, modules=None, classes=None, interfaces=None,
                symbols=None, has_uvm_import=None, contains_uvm=None,
                semantic_status="unavailable" if unavailable else "not_run",
                semantic_work_scope="not_run",
                symbol_inventory_complete=False, symbol_count=None, symbol_count_lower_bound=0,
                semantic_gaps=["semantic_provider_unavailable"] if unavailable else [],
                analysis_limits=[])


def _collect_file(provider, entry, budget):
    """Copy a file-bound declaration stream before releasing provider resources.

    open_file_symbols(entry, budget) must bind by the captured canonical path
    or exact alias, and expose complete/gaps/has_uvm_import/uses_uvm. The two UVM
    flags are bool or None: absence of an import is not absence of UVM usage.
    Declaration and import coverage may differ. Missing locations remain null;
    a supplied location must belong to this exact compile member.
    """
    declarations, seen, gaps, limits = [], set(), set(), set()
    complete, imported, used, stream = False, None, None, None
    work_scope = "provider_defined"
    try:
        budget.checkpoint()
        with provider.open_file_symbols(entry, budget) as stream:
            work_scope = getattr(stream, "work_scope", "provider_defined")
            if work_scope not in {"provider_defined", "file_declarations", "global_namespaces_bounded"}:
                raise ValueError("file semantic work scope must be a fixed label")
            for symbol in stream:
                budget.checkpoint()
                if not isinstance(symbol, SourceSymbol):
                    raise ValueError("provider returned an invalid file symbol")
                if symbol.symbol_id in seen:
                    raise ValueError("provider returned a duplicate file symbol identity")
                if (symbol.location is not None and symbol.location.file != entry.path
                        and symbol.location.file not in entry.aliases):
                    raise ValueError("provider returned a symbol from another compile member")
                budget.admit_symbol(symbol)
                declarations.append(symbol)
                seen.add(symbol.symbol_id)
                if symbol.location is None:
                    gaps.add("symbol_source_location_unavailable")
            budget.checkpoint()
            if not isinstance(stream.complete, bool):
                raise ValueError("file declaration completeness must be explicit")
            complete = stream.complete
            gaps.update(_gaps(stream.gaps))
            imported, used = stream.has_uvm_import, stream.uses_uvm
            if any(value is not None and not isinstance(value, bool) for value in (imported, used)):
                raise ValueError("UVM evidence must be explicit bool or unknown")
            if imported is True and used is not True:
                raise ValueError("a proved UVM import must imply UVM usage")
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        complete = False
        limits.add(exc.reason)
        if stream is not None:
            gaps.update(_gaps(stream.gaps))
            # A provider may already have copied positive import/usage facts
            # before its declaration inventory stopped. Unknown/false cannot
            # survive this stop as a proved negative.
            imported = True if getattr(stream, "has_uvm_import", None) is True else imported
            used = True if getattr(stream, "uses_uvm", None) is True else used
        # Budget stops cannot manufacture a negative semantic filter result.
        imported = True if imported is True else None
        used = True if used is True else None

    if imported is True and used is not True:
        raise ValueError("a proved UVM import must imply UVM usage")
    kinds = {symbol.kind for symbol in declarations}
    # Preserve the legacy classification priority, while not declaring class
    # or interface in the presence of an unexamined higher-priority kind.
    file_type = "module" if "module" in kinds else None
    if file_type is None and complete:
        file_type = "class" if "class" in kinds else "interface" if "interface" in kinds else "unknown"
    return dict(entry.row(), file_type=file_type,
                modules=list(dict.fromkeys(symbol.name for symbol in declarations if symbol.kind == "module")),
                classes=list(dict.fromkeys(symbol.qualified_name for symbol in declarations if symbol.kind == "class")),
                interfaces=list(dict.fromkeys(symbol.name for symbol in declarations if symbol.kind == "interface")),
                symbols=[symbol.row() for symbol in declarations],
                has_uvm_import=imported, contains_uvm=used,
                semantic_status="complete" if complete and not gaps and not limits else "partial",
                semantic_work_scope=work_scope,
                symbol_inventory_complete=complete,
                symbol_count=len(declarations) if complete else None,
                symbol_count_lower_bound=len(declarations),
                semantic_gaps=sorted(gaps), analysis_limits=sorted(limits))


def query_file_detail(catalog: CompileSourceCatalog, provider=None, *, path,
                      budget: HierarchyQueryBudget | None = None):
    """Resolve compile membership first; a miss never starts semantic work."""
    if not isinstance(catalog, CompileSourceCatalog):
        raise TypeError("a captured compile source catalog is required")
    validate_file_query("get_tb_file_detail", {"path": path})
    budget = _budget(provider, budget)
    try:
        entry = catalog.find_file(path, checkpoint=budget.checkpoint)
        if entry is None:
            suggestions = catalog.suggest_files(path, checkpoint=budget.checkpoint)
            budget.checkpoint()
            return {
                "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "error": "file_not_in_compile_set",
                "path": path, "did_you_mean": suggestions,
                "facts_origin": "compile_metadata", "artifact_identity": catalog.context_identity,
                "coverage_domain": "reported_compile_members", "semantic_status": "not_run",
                "coverage_status": "partial" if catalog.gaps else "complete",
                "coverage_gaps": list(catalog.gaps), "analysis_limits": [], "query_metrics": budget.metrics(),
            }
        budget.admit_file(entry)
    except HierarchyBudgetExceeded as exc:
        # Failure to finish ownership lookup/admission is neither a complete
        # compile-set miss nor a reason to start another semantic backend.
        return {
            "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "error": "file_query_incomplete",
            "path": path,
            "facts_origin": "compile_metadata", "artifact_identity": catalog.context_identity,
            "coverage_domain": "reported_compile_members", "semantic_status": "not_run",
            "coverage_status": "partial", "coverage_gaps": list(catalog.gaps),
            "analysis_limits": [exc.reason], "query_metrics": budget.metrics(),
        }
    row = (_collect_file(provider, entry, budget) if provider is not None
           else _metadata_row(entry, unavailable=True))
    gaps = sorted(set(catalog.gaps) | set(row["semantic_gaps"]))
    return dict(row, schema_version=HIERARCHY_TOOL_SCHEMA_VERSION,
                facts_origin=provider.origin if provider is not None else "compile_metadata",
                artifact_identity=provider.artifact_identity if provider is not None else catalog.context_identity,
                ownership_identity=catalog.context_identity, ownership_basis=catalog.ownership_basis, coverage_domain="provider_file_declarations",
                coverage_status="complete" if row["semantic_status"] == "complete" and not catalog.gaps else "partial",
                coverage_gaps=gaps, query_metrics=budget.metrics())


def _semantic_match(row, *, has_module, contains_uvm, file_type):
    predicates = []
    if has_module is not None:
        found = any(item["kind"] == "module" and has_module in (item["name"], item["qualified_name"])
                    for item in row["symbols"] or [])
        predicates.append(True if found else False if row["symbol_inventory_complete"] else None)
    if contains_uvm is not None:
        value = row["contains_uvm"]
        predicates.append(None if value is None else value == contains_uvm)
    if file_type is not None:
        value = row["file_type"]
        predicates.append(None if value is None else value == file_type)
    if False in predicates:
        return False
    return None if None in predicates else True


def lookup_source_files(catalog: CompileSourceCatalog, provider=None, *, basename=None,
                        name_contains=None, path_contains=None, has_module=None,
                        contains_uvm=None, file_type=None, limit=50, count_mode="bounded",
                        budget: HierarchyQueryBudget | None = None):
    """AND filters with separate proved matches, unknown files, and counts.

    A metadata-only query returns null semantic fields and never calls the
    provider. Exact mode may scan the whole captured file set; bounded mode
    stops after one omitted positive match. Unknown filters cannot be reported
    as a complete empty result, even if there are no displayed matches.
    """
    if not isinstance(catalog, CompileSourceCatalog):
        raise TypeError("a captured compile source catalog is required")
    validate_file_query("lookup_tb_files", dict(basename=basename, name_contains=name_contains,
                        path_contains=path_contains, has_module=has_module, contains_uvm=contains_uvm,
                        file_type=file_type, limit=limit, count_mode=count_mode))
    budget = _budget(provider, budget)
    semantic = any(value is not None for value in (has_module, contains_uvm, file_type))
    matches, gaps, limits = [], set(catalog.gaps), set()
    count, unknown, examined, semantic_queries = 0, 0, 0, 0
    complete, truncated, semantic_partial = False, False, False
    try:
        selected = (catalog.select_files(basename=basename, name_contains=name_contains,
                                        path_contains=path_contains, checkpoint=budget.checkpoint)
                    if any(value is not None for value in (basename, name_contains, path_contains))
                    else iter(catalog.files))
        for entry in selected:
            budget.checkpoint()
            budget.admit_file(entry)
            examined += 1
            if semantic and provider is not None:
                semantic_queries += 1
            row = (_collect_file(provider, entry, budget) if semantic and provider is not None
                   else _metadata_row(entry, unavailable=semantic))
            gaps.update(row["semantic_gaps"])
            limits.update(row["analysis_limits"])
            semantic_partial |= semantic and row["semantic_status"] != "complete"
            match = _semantic_match(row, has_module=has_module, contains_uvm=contains_uvm,
                                    file_type=file_type)
            if match is None:
                unknown += 1
            elif match:
                count += 1
                if len(matches) < limit:
                    matches.append(row)
                else:
                    truncated = True
                    if count_mode == "bounded":
                        gaps.add("matching_files_not_enumerated")
                        break
            if row["analysis_limits"]:
                break
        else:
            complete = unknown == 0 and not catalog.gaps
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        limits.add(exc.reason)
        complete = False
    origin = provider.origin if semantic_queries else "compile_metadata"
    identity = provider.artifact_identity if semantic_queries else catalog.context_identity
    return {
        "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "facts_origin": origin,
        "artifact_identity": identity, "ownership_identity": catalog.context_identity, "ownership_basis": catalog.ownership_basis,
        "semantic_status": ("not_run" if not semantic or examined == 0 else "unavailable" if provider is None
                            else "partial" if semantic_partial or limits or not complete else "complete"),
        "matches": matches, "total": count if complete else None, "count_lower_bound": count,
        "count_status": "exact" if complete else "lower_bound", "count_mode": count_mode,
        "count_domain": "matching_compile_members", "unknown_filter_files": unknown,
        "examined_files": examined, "semantic_file_queries": semantic_queries,
        "truncated": truncated, "output_truncated": truncated,
        "coverage_status": "complete" if complete and not semantic_partial else "partial",
        "coverage_gaps": sorted(gaps), "analysis_limits": sorted(limits), "query_metrics": budget.metrics(),
    }
