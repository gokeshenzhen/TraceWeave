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
from .hierarchy_source_metadata import CompileSourceCatalog, build_compile_source_catalog, build_native_source_catalog, observation_identity
from .hierarchy_source_service import HierarchySourceService, validate_hierarchy_query


HIERARCHY_SOURCE_TOOLS = frozenset({
    "get_tb_subtree", "find_tb_instance", "lookup_tb_files", "get_tb_file_detail",
    "get_tb_class_hierarchy", "dump_tb_section",
})
SOURCE_QUERY_OPERATIONS = HIERARCHY_SOURCE_TOOLS | {"resolve_packed_fields"}


def validate_source_query(operation, arguments):
    if not isinstance(operation, str) or operation not in SOURCE_QUERY_OPERATIONS or not isinstance(arguments, dict):
        raise ValueError("unsupported hierarchy/source query")
    if operation == "resolve_packed_fields":
        from .npi_packed_layout import validate_packed_query
        validate_packed_query(arguments)
    elif operation in {"get_tb_file_detail", "lookup_tb_files"}:
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

    def validate(self):
        """Recheck plan invariants before a caller acquires a backend."""
        if not isinstance(self.context, DesignContext) or not isinstance(self.budget, HierarchyQueryBudget):
            raise TypeError("a prepared context and hierarchy budget are required")
        validate_source_query(self.operation, json.loads(self.arguments_json))
        if self.catalog is not None:
            if not isinstance(self.catalog, CompileSourceCatalog):
                raise TypeError("a captured compile source catalog is required")
            if self.catalog.context_identity != observation_identity(self.context):
                raise ValueError("file catalog belongs to a different context")

    def _unstarted(self, reason):
        return {
            "schema_version": HIERARCHY_TOOL_SCHEMA_VERSION, "error": "hierarchy_query_incomplete",
            "facts_origin": "compile_metadata", "artifact_identity": observation_identity(self.context),
            "semantic_status": "not_run", "coverage_status": "partial",
            "coverage_gaps": list(self.context.gaps), "analysis_limits": [reason],
            "query_metrics": self.budget.metrics(),
        }

    def execute(self, provider=None):
        """Query only the selected provider; no implicit fallback or loading."""
        check_cancelled()
        self.validate()
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
                "facts_origin": "compile_metadata", "artifact_identity": observation_identity(self.context),
                "semantic_status": "unavailable", "coverage_status": "partial",
                "coverage_gaps": sorted(set(self.context.gaps) | {"semantic_provider_unavailable"}),
                "analysis_limits": [], "query_metrics": self.budget.metrics(),
            }
        if self.operation == "resolve_packed_fields":
            from .npi_packed_layout import query_packed_layout
            return query_packed_layout(provider, budget=self.budget, **arguments)
        method = {"get_tb_subtree": "subtree", "find_tb_instance": "find_instances",
                  "get_tb_class_hierarchy": "class_hierarchy"}[self.operation]
        return getattr(HierarchySourceService(provider), method)(budget=self.budget, **arguments)


@dataclass(frozen=True)
class NativeSourceRequest(HierarchySourceRequest):
    """Independent metadata request; never a partial DesignContext."""
    def validate(self):
        from .native_query import NativeQueryInputs
        if not isinstance(self.context, NativeQueryInputs) or not isinstance(self.budget, HierarchyQueryBudget):
            raise TypeError("native metadata inputs and a hierarchy budget are required")
        validate_source_query(self.operation, json.loads(self.arguments_json))
        if self.catalog is not None and self.catalog.context_identity != self.context.compile_metadata_identity_sha256:
            raise ValueError("file catalog belongs to different compile metadata")


def prepare_native_source_query(inputs, operation, arguments, *, budget=None):
    return _prepare_query(inputs, operation, arguments, budget=budget, native=True)


def prepare_source_query(context, operation, arguments, *, budget=None):
    """Validate and determine semantic demand using captured metadata only.

    A nonmember file, metadata-only filter, or empty metadata selection needs
    no licensed/frontend work. Querying class relationships or semantic file
    filters on candidate files does need the shared provider. Catalog creation
    and selection cost remain inside the original request's deadline.
    """
    if not isinstance(context, DesignContext):
        raise TypeError("a prepared DesignContext is required")
    return _prepare_query(context, operation, arguments, budget=budget)


def _prepare_query(context, operation, arguments, *, budget=None, native=False):
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
        elif operation in ({"get_tb_file_detail", "lookup_tb_files"} if native else {"get_tb_file_detail", "lookup_tb_files", "get_tb_class_hierarchy"}) or (
            operation == "dump_tb_section" and arguments["section"] in ({"files_full", "interfaces"} if native else {"files_full", "interfaces", "class_hierarchy_full"})
        ):
            available = budget.max_bytes - budget.fact_bytes - 512
            if available < 1:
                raise HierarchyBudgetExceeded("fact_byte_limit")
            catalog = (build_native_source_catalog if native else build_compile_source_catalog)(context, max_bytes=min(16 * 1024 * 1024, available),
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
    return (NativeSourceRequest if native else HierarchySourceRequest)(context, operation, arguments_json, budget, catalog,
                                  needs_provider, preparation_limit)
