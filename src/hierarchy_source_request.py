"""Plan all six source queries before selecting or loading a semantic backend.

Plans are request-local and own no frontend, native handle or source body. The
caller pins the DesignContext, supplies its cumulative budget, and validates
the context/artifact again after provider cleanup before publishing the result.
"""
from __future__ import annotations

from dataclasses import dataclass
import json

from .cancellation import check_cancelled
from .design_context import DesignContext
from .hierarchy_source_export import METADATA_SECTIONS, export_hierarchy_section, validate_section_query
from .hierarchy_source_files import lookup_source_files, query_file_detail, validate_file_query
from .hierarchy_source_ir import HIERARCHY_TOOL_SCHEMA_VERSION, HierarchyBudgetExceeded, HierarchyQueryBudget
from .hierarchy_source_metadata import CompileSourceCatalog, build_compile_source_catalog
from .hierarchy_source_service import HierarchySourceService, validate_hierarchy_query


HIERARCHY_SOURCE_TOOLS = frozenset({
    "get_tb_subtree", "find_tb_instance", "lookup_tb_files", "get_tb_file_detail",
    "get_tb_class_hierarchy", "dump_tb_section",
})


def validate_source_query(operation, arguments):
    if not isinstance(operation, str) or operation not in HIERARCHY_SOURCE_TOOLS or not isinstance(arguments, dict):
        raise ValueError("unsupported hierarchy/source query")
    if operation in {"get_tb_file_detail", "lookup_tb_files"}:
        validate_file_query(operation, arguments)
    elif operation == "dump_tb_section":
        if set(arguments) - {"section", "max_items"} or "section" not in arguments:
            raise ValueError("unsupported section query arguments")
        validate_section_query(**arguments)
    else:
        validate_hierarchy_query(operation, arguments)


@dataclass(frozen=True)
class HierarchySourceRequest:
    context: DesignContext
    operation: str
    arguments_json: str
    budget: HierarchyQueryBudget
    catalog: CompileSourceCatalog | None
    needs_provider: bool
    preparation_limit: str | None = None

    def _unstarted(self, reason):
        return {
            "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "error": "hierarchy_query_incomplete",
            "facts_origin": "compile_metadata", "artifact_identity": self.context.identity_sha256,
            "semantic_status": "not_run", "coverage_status": "partial",
            "coverage_gaps": list(self.context.gaps), "analysis_limits": [reason],
            "query_metrics": self.budget.metrics(),
        }

    def execute(self, provider=None):
        """Query only the selected provider; no implicit fallback or loading."""
        check_cancelled()
        if self.catalog is not None:
            if not isinstance(self.catalog, CompileSourceCatalog):
                raise TypeError("a captured compile source catalog is required")
            if self.catalog.context_identity != self.context.identity_sha256:
                raise ValueError("file catalog belongs to a different context")
        if self.preparation_limit is not None:
            return self._unstarted(self.preparation_limit)
        try:
            self.budget.checkpoint()
        except HierarchyBudgetExceeded as exc:
            return self._unstarted(exc.reason)
        arguments = json.loads(self.arguments_json)
        # Metadata requests must not even inspect a provider's properties or
        # lazy budget accessor. A prepared selection is an optimization only;
        # each engine still applies its own validation and work checkpoints.
        provider = provider if self.needs_provider else None
        if self.operation == "get_tb_file_detail":
            return query_file_detail(self.catalog, provider, budget=self.budget, **arguments)
        if self.operation == "lookup_tb_files":
            return lookup_source_files(self.catalog, provider, budget=self.budget, **arguments)
        if self.operation == "dump_tb_section":
            return export_hierarchy_section(self.context, provider, catalog=self.catalog,
                                            budget=self.budget, **arguments)
        if provider is None:
            return {
                "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "error": "semantic_provider_unavailable",
                "facts_origin": "compile_metadata", "artifact_identity": self.context.identity_sha256,
                "semantic_status": "unavailable", "coverage_status": "partial",
                "coverage_gaps": sorted(set(self.context.gaps) | {"semantic_provider_unavailable"}),
                "analysis_limits": [], "query_metrics": self.budget.metrics(),
            }
        method = {"get_tb_subtree": "subtree", "find_tb_instance": "find_instances",
                  "get_tb_class_hierarchy": "class_hierarchy"}[self.operation]
        return getattr(HierarchySourceService(provider), method)(budget=self.budget, **arguments)


def prepare_source_query(context, operation, arguments, *, budget=None):
    """Validate and determine semantic demand using captured metadata only.

    A nonmember file, metadata-only filter, or empty metadata selection needs
    no licensed/frontend work. Querying class relationships or semantic file
    filters on candidate files does need the shared provider. Catalog creation
    and selection cost remain inside the original request's deadline.
    """
    if not isinstance(context, DesignContext):
        raise TypeError("a prepared DesignContext is required")
    validate_source_query(operation, arguments)
    budget = budget if budget is not None else HierarchyQueryBudget()
    if not isinstance(budget, HierarchyQueryBudget):
        raise TypeError("a hierarchy query budget is required")
    arguments_json = json.dumps(arguments, ensure_ascii=True, sort_keys=True)
    arguments = json.loads(arguments_json)
    catalog, needs_provider, preparation_limit = None, True, None
    try:
        budget.checkpoint()
        if operation == "dump_tb_section" and arguments["section"] in METADATA_SECTIONS:
            needs_provider = False
        elif operation in {"get_tb_file_detail", "lookup_tb_files"} or (
            operation == "dump_tb_section" and arguments["section"] in {"files_full", "interfaces"}
        ):
            available = budget.max_bytes - budget.fact_bytes - 512
            if available < 1:
                raise HierarchyBudgetExceeded("fact_byte_limit")
            catalog = build_compile_source_catalog(context, max_bytes=min(16 * 1024 * 1024, available),
                                                   checkpoint=budget.checkpoint)
            budget.reserve_bytes(512 + catalog.metadata_bytes)
            if operation == "get_tb_file_detail":
                needs_provider = catalog.find_file(arguments["path"], checkpoint=budget.checkpoint) is not None
            elif operation == "lookup_tb_files":
                needs_provider = any(arguments.get(key) is not None
                                     for key in ("has_module", "contains_uvm", "file_type"))
                filters = {key: arguments.get(key) for key in ("basename", "name_contains", "path_contains")}
                if needs_provider:
                    candidates = (catalog.select_files(**filters, checkpoint=budget.checkpoint)
                                  if any(value is not None for value in filters.values()) else iter(catalog.files))
                    needs_provider = next(candidates, None) is not None
            else:
                needs_provider = bool(catalog.files)
        budget.checkpoint()
    except HierarchyBudgetExceeded as exc:
        preparation_limit, needs_provider = exc.reason, False
        catalog = None
    return HierarchySourceRequest(context, operation, arguments_json, budget, catalog,
                                  needs_provider, preparation_limit)
