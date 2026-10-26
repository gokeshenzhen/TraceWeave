from __future__ import annotations

from typing import Annotated, Any, Literal
import config as _config

from pydantic import BaseModel, ConfigDict, Field, StrictInt, WithJsonSchema, model_serializer, model_validator

from .property_model import ExplainPropertyOptions, ExplainPropertyResult, PropertyExplanation


class SchemaModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    def _as_dict(self) -> dict[str, Any]:
        return self.model_dump()

    def __getitem__(self, key: str) -> Any:
        return self._as_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self._as_dict().get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self._as_dict()

    def keys(self):
        return self._as_dict().keys()

    def items(self):
        return self._as_dict().items()

    def values(self):
        return self._as_dict().values()

    def __iter__(self):
        return iter(self._as_dict())


class NativeRouteMetrics(SchemaModel):
    metadata_ms: float = Field(ge=0)
    kdb_discovery_ms: float = Field(ge=0)
    kdb_validation_ms: float = Field(ge=0)
    npi_ms: float = Field(ge=0)
    source_prepare_ms: float = Field(ge=0)
    source_validation_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)
    source_prepare_count: int = Field(ge=0)
    source_capture_read_bytes: int = Field(ge=0)
    cached_kdb_source_read_bytes: int = Field(ge=0)


class NativeRouteReceipt(SchemaModel):
    entry: Literal["native_first"] = "native_first"
    input_validation: Literal["compile_metadata_and_kdb", "kdb_only", "compile_metadata_only"]
    compile_metadata_identity_sha256: str | None
    kdb_identity_sha256: str | None
    source_context_status: Literal["not_required", "prepared", "reused", "unavailable", "changed", "expired"]
    source_design_handle: str | None
    source_context_identity_sha256: str | None
    source_association: Literal["unverified"] = "unverified"
    metrics: NativeRouteMetrics

    @model_serializer(mode="plain")
    def serialize_native_receipt(self):
        # Null identities explicitly distinguish no source observation from
        # omitted legacy receipts, including nested exclude_none serializers.
        return {name: (self.metrics.model_dump() if name == "metrics" else getattr(self, name))
                for name in type(self).model_fields}


TOKEN_BUDGET_SOFT_LIMIT = 80_000


class EvidenceOutputOptions(SchemaModel):
    output_format: Literal["full", "compact"] = Field(
        default="full",
        description=(
            "full: original result. compact: lossless traceweave.compact.v1 "
            "{result,references}; refs are response-local JSON pointers, not cursors/handles. "
            "Same coverage/display limits."
        ),
    )


# Only inspected result families opt into the shared presentation contract.
LEGACY_COMPACT_OUTPUT_TOOLS = frozenset({
    "get_waveform_summary", "get_signal_at_time", "get_signal_transitions",
    "get_signals_around_time", "get_signals_by_cycle", "inspect_handshake",
    "inspect_tlul", "reconstruct_transactions", "resolve_packed_fields",
    "suggest_handshakes", "suggest_protocol_bundles", "sweep_handshakes",
    "diff_first_divergence", "trace_divergence", "trace_x_source",
    "scan_structural_risks", "get_diagnostic_snapshot",
})
COMPACT_OUTPUT_TOOLS = LEGACY_COMPACT_OUTPUT_TOOLS | {"explain_signal_driver"}


class DriverEvidenceOutputOptions(SchemaModel):
    output_format: Literal["full", "compact"] = Field(
        default="compact",
        description=(
            "compact (default) delivers all retained evidence in a self-contained "
            "traceweave.driver.compact.v1 response with typed evidence_tables. "
            "Refs are response-local table indices. full preserves the original "
            "driver JSON shape and indentation for legacy consumers."
        ),
    )


def output_options_for_tool(name: str) -> type[SchemaModel]:
    """One source for catalog defaults and actual call_tool argument parsing."""
    return DriverEvidenceOutputOptions if name == "explain_signal_driver" else EvidenceOutputOptions


class SelectionEvidenceReference(SchemaModel):
    path: Literal[
        "/result/channels/a/selections", "/result/channels/d/selections",
        "/result/transactions/selections",
    ]
    target: Literal["/result/selections"] = "/result/selections"


class CompactEvidenceResult(SchemaModel):
    """Lossless projection of the already bounded public result, not a cache."""

    format: Literal["traceweave.compact.v1"] = "traceweave.compact.v1"
    tool: str
    result: dict[str, Any]
    references: list[SelectionEvidenceReference] = Field(default_factory=list, max_length=3)

    @model_validator(mode="after")
    def validate_references(self):
        if self.tool not in LEGACY_COMPACT_OUTPUT_TOOLS:
            raise ValueError("compact_output_tool_unsupported")
        seen = set()
        for ref in self.references:
            if self.tool != "inspect_tlul" or ref.path in seen or not self.result.get("selections"):
                raise ValueError("compact_evidence_reference_invalid")
            seen.add(ref.path)
            parent = self.result
            for key in ref.path.split("/")[2:-1]:
                parent = parent.get(key) if isinstance(parent, dict) else None
            if not isinstance(parent, dict) or "selections" in parent:
                raise ValueError("compact_evidence_reference_invalid")
        return self


class TransactionDisplayOptions(SchemaModel):
    max_transactions: StrictInt = Field(
        default=256, ge=1, le=65536,
        description=("Display cap only; every admitted sample contributes to analysis. "
                     "Read analysis.stop_reason separately from display_status."),
    )


class TlulAnalysisOptions(TransactionDisplayOptions):
    reset_active_low: bool = True
    edge: Literal["posedge", "negedge"] = "posedge"
    start_time_ps: int | str = 0
    end_time_ps: int | str = -1
    max_cycles: StrictInt = Field(default=65536, ge=1, le=65536)
    max_wait_cycles: StrictInt = Field(default=16, ge=0)


class TruncatableResult(SchemaModel):
    detail_level: str = "summary"
    detail_hint: str | None = None
    auto_downgraded: bool = False
    payload_bytes: int | None = None


class ProblemHints(SchemaModel):
    has_x: bool = False
    has_z: bool = False
    first_error_time_ps: int | None = None
    error_pattern: str | None = None


class FileEntry(SchemaModel):
    path: str
    size: int
    mtime: str
    age_hours: float
    phase: str | None = None
    format: str | None = None
    is_mixed: bool | None = None


class CaseInfo(SchemaModel):
    name: str
    dir: str
    has_sim_log: bool
    has_wave: bool


class NextRequiredStep(SchemaModel):
    tool: str
    compile_log: str
    simulator: str
    reason: str


class FstRuntimeInfo(SchemaModel):
    enabled: bool
    dependency: Literal["pylibfst"]
    required_version: str
    installed_version: str | None = None
    status: Literal["platform_unsupported", "dependency_missing", "version_unsupported", "candidate_ready_unloaded"]
    message: str


class SimPathsResult(SchemaModel):
    verif_root: str
    case_name: str | None = None
    config_source: str
    config_root: str | None = None
    discovery_mode: str
    case_dir: str | None = None
    simulator: str | None = None
    fsdb_runtime: dict[str, Any] = Field(default_factory=dict)
    fst_runtime: FstRuntimeInfo | None = None
    compile_logs: list[FileEntry] = Field(default_factory=list)
    sim_logs: list[FileEntry] = Field(default_factory=list)
    wave_files: list[FileEntry] = Field(default_factory=list)
    available_cases: list[CaseInfo] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    next_required_step: NextRequiredStep | None = None
    suggested_calls: list[dict[str, Any]] = Field(default_factory=list)


class FormalProjectEntry(SchemaModel):
    path: str
    detected_formal_tool: str | None = None
    project_layout_state: Literal[
        "session_present", "markers_only", "explicit_unverified"
    ]
    detection_evidence: list[str] = Field(default_factory=list)


class FormalLogEntry(SchemaModel):
    path: str
    size: int
    mtime: str
    age_hours: float
    role: Literal[
        "console", "session", "command", "infrastructure", "unclassified"
    ]
    project_dir: str | None = None


class FormalWaveEntry(SchemaModel):
    path: str
    size: int
    mtime: str
    age_hours: float
    format: Literal["vcd", "fsdb"]


class FormalRunEntry(SchemaModel):
    name: str
    dir: str
    detected_formal_tool: str | None = None
    has_wave: bool


class FormalDiscoveryCoverage(SchemaModel):
    status: Literal["complete", "truncated", "degraded"]
    directories_examined: int
    files_examined: int
    entries_examined: int
    max_depth: int
    max_entries: int
    max_projects: int
    max_files_per_role: int
    truncation_reasons: list[str] = Field(default_factory=list)
    degradation_reasons: list[str] = Field(default_factory=list)
    skipped_count: int = 0


class FormalPathsResult(SchemaModel):
    """Local formal artifacts without property or trace semantics."""

    formal_root: str
    requested_formal_tool: Literal["auto", "jaspergold"]
    detected_formal_tools: list[str] = Field(default_factory=list)
    discovery_mode: Literal[
        "explicit", "formal_project_dir", "formal_root", "wave_only", "unknown"
    ]
    selected_project_dir: str | None = None
    projects: list[FormalProjectEntry] = Field(default_factory=list)
    formal_logs: list[FormalLogEntry] = Field(default_factory=list)
    wave_files: list[FormalWaveEntry] = Field(default_factory=list)
    available_runs: list[FormalRunEntry] = Field(default_factory=list)
    coverage: FormalDiscoveryCoverage
    hints: list[str] = Field(default_factory=list)


class PrepareDesignContextOptions(SchemaModel):
    """Independent preparation budgets; no discovery or semantic work."""

    model_config = ConfigDict(extra="forbid", strict=True)

    compile_log: str = Field(min_length=1, pattern=r"\S")
    supplementary_compile_logs: list[Annotated[str, Field(min_length=1, pattern=r"\S")]] = Field(
        default_factory=list, max_length=16,
        description="Complementary compile logs in compilation order.",
    )
    simulator: Literal["auto", "vcs", "xcelium", "verilator"] = "auto"
    top_hint: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = None
    backend_preference: Literal["auto", "source"] = "auto"
    kdb_path: Annotated[str, Field(min_length=1, pattern=r"\S")] | None = Field(
        default=None, description="Explicit candidate, captured on first query; does not prove source association.",
    )
    context_identity_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$",
        description=("Expected compile/source identity when replaying a recovery_context. "
                     "A mismatch rejects preparation before a new handle is published."),
    )
    timeout_sec: float = Field(default=30.0, gt=0, le=300, allow_inf_nan=False)
    max_files: int = Field(default=32768, ge=1, le=32768)
    max_bytes: int = Field(default=128 * 1024 * 1024, ge=1, le=128 * 1024 * 1024)
    max_references: int = Field(default=65536, ge=1, le=65536)
    max_include_depth: int = Field(default=64, ge=0, le=64)


class DesignContextRecovery(SchemaModel):
    compile_log: str
    supplementary_compile_logs: list[str]
    simulator: Literal["vcs", "xcelium", "verilator"]
    top_hint: str | None = None
    backend_preference: Literal["auto", "source"]
    kdb_path: str | None = None
    context_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class DesignContextPreparationMetrics(SchemaModel):
    parse_ms: float = Field(ge=0)
    prepare_ms: float = Field(ge=0)
    input_file_count: int = Field(ge=0)
    captured_bytes: int = Field(ge=0)
    read_bytes: int = Field(ge=0, description="Raw bytes read during capture and its validation.")
    dependency_file_visits: int = Field(ge=0)
    registration_ms: float = Field(ge=0, description="Additional content revalidation and metadata accounting.")
    total_ms: float = Field(ge=0, description="Preparation, admission wait and registration through receipt construction.")
    retained_metadata_bytes: int = Field(ge=0, description="Conservative registry capacity charge; no source bodies.")


class PrepareDesignContextResult(SchemaModel):
    design_handle: str
    handle_lifetime: Literal["process"] = "process"
    context_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_verification: Literal["current_inputs", "matched_recovery_identity"] = "current_inputs"
    simulator: Literal["vcs", "xcelium", "verilator"]
    selected_top: str | None = None
    backend_preference: Literal["auto", "source"]
    preparation_status: Literal["prepared", "partial"]
    source_snapshot_status: Literal["complete", "partial"]
    include_resolution_status: Literal["complete", "partial"]
    semantic_status: Literal["not_run"] = "not_run"
    kdb_status: Literal["not_checked"] = "not_checked"
    capabilities: list[Literal["compile_metadata", "source_observations"]]
    gaps: list[str]
    metrics: DesignContextPreparationMetrics
    recovery_context: DesignContextRecovery


class BuildTbHierarchyResult(SchemaModel):
    """Slim LLM-facing payload for build_tb_hierarchy.

    Full hierarchy data (files list, complete component_tree, class
    hierarchy, raw compile_result) is held server-side and accessed via
    ``hierarchy_handle`` through the handle tools (get_tb_subtree,
    lookup_tb_files, find_tb_instance, get_tb_file_detail,
    get_tb_class_hierarchy, dump_tb_section).
    """

    hierarchy_handle: str = ""
    handle_lifetime: Literal["process"] = "process"
    build_status: Literal["completed", "blocked"] = "completed"
    blocker: dict[str, Any] | None = None
    project: dict[str, Any] = Field(default_factory=dict)
    compile_command: str = ""
    stats: dict[str, int] = Field(default_factory=dict)
    tree_skeleton: dict[str, Any] = Field(default_factory=dict)
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    build_metrics: dict[str, Any] = Field(default_factory=dict)
    ambiguous_basenames: list[dict[str, Any]] = Field(default_factory=list)
    kdb_hint: dict[str, Any] | None = None
    handle_tools: dict[str, str] = Field(default_factory=dict)
    required_next_call: dict[str, Any] | None = None
    suggested_next: dict[str, Any] | None = None


class BuildTbHierarchyResultLegacy(SchemaModel):
    """Pre-slim hierarchy payload, kept behind the
    ``TRACEWEAVE_LEGACY_HIERARCHY_PAYLOAD=1`` env-var escape hatch as a
    one-release migration safety net. Slated for removal."""

    project: dict[str, Any] = Field(default_factory=dict)
    files: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    component_tree: dict[str, Any] = Field(default_factory=dict)
    class_hierarchy: list[str] = Field(default_factory=list)
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    compile_result: dict[str, Any] = Field(default_factory=dict)
    build_metrics: dict[str, Any] = Field(default_factory=dict)
    required_next_call: dict[str, Any] | None = None
    suggested_next: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Handle-based hierarchy access (phase 3 schemas)
#
# Each handle tool follows the same protocol: take ``handle`` as the first
# argument; resolve it against the server's HandleStore; return either a
# typed result or a ``HandleErrorResult`` describing why resolution failed
# (handle_expired / file_not_in_compile_set / filter_required).
# ---------------------------------------------------------------------------


class HandleErrorResult(SchemaModel):
    error: str
    hint: str | None = None
    current_handle: str | None = None
    did_you_mean: list[str] = Field(default_factory=list)


class TbNode(SchemaModel):
    inst: str
    module: str = ""
    source_file: str = ""
    source_line: int = 0
    child_count: int = 0
    truncated: bool = False
    children: list["TbNode"] = Field(default_factory=list)


class GetTbSubtreeResult(SchemaModel):
    handle: str
    root: str
    node: TbNode
    truncated: bool = False
    total_descendants: int = 0


class TbFileMatch(SchemaModel):
    path: str
    file_type: str = ""
    modules: list[str] = Field(default_factory=list)
    classes: list[str] = Field(default_factory=list)
    has_uvm_import: bool = False


class LookupTbFilesResult(SchemaModel):
    handle: str
    matches: list[TbFileMatch] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class TbInstanceHit(SchemaModel):
    path: str
    module: str = ""
    parent: str = ""
    source_file: str = ""
    source_line: int = 0


class FindTbInstanceResult(SchemaModel):
    handle: str
    hits: list[TbInstanceHit] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class TbSymbol(SchemaModel):
    name: str
    kind: Literal["module", "class", "interface", "package", "program"]
    line: int = 0


class GetTbFileDetailResult(SchemaModel):
    handle: str
    path: str
    file_type: str = ""
    symbols: list[TbSymbol] = Field(default_factory=list)
    includes: list[str] = Field(default_factory=list)
    has_uvm_import: bool = False


class TbClassNode(SchemaModel):
    name: str
    source_file: str = ""
    source_line: int = 0
    children: list["TbClassNode"] = Field(default_factory=list)


class GetTbClassHierarchyResult(SchemaModel):
    handle: str
    roots: list[TbClassNode] = Field(default_factory=list)
    total: int = 0


class DumpTbSectionResult(SchemaModel):
    handle: str
    section: str
    data: Any = None
    warning: str = ""


class StructuralRisk(SchemaModel):
    type: str
    file: str
    line: int
    module: str | None = None
    risk_level: Literal["high", "medium", "low"]
    detail: str
    evidence: list[str] = Field(default_factory=list)


class StructuralPropagationReceipt(SchemaModel):
    status: Literal["not_run", "complete", "partial"] = "not_run"
    gaps: list[str] = Field(default_factory=list)
    inventory_complete: bool = False
    instances: int = 0
    signal_count: int = 0
    signal_bits: int = 0
    driver_count: int = 0
    driver_bits: int = 0
    ast_nodes: int = 0
    work_steps: int = 0
    boundary_count: int = 0
    unknown_bits: int = 0
    conflict_bits: int = 0
    note: str | None = None


class StructuralCategoryCoverage(SchemaModel):
    category: str
    status: Literal["not_run", "complete", "partial", "unavailable", "unsupported", "failed"]
    backend: Literal["none", "verdi_npi", "slang"]
    result_group: str | None = None
    scope: str | None = None
    checked_scope: str | None = None
    evidence_domain: str
    total_facts: int = 0
    gaps: list[str] = Field(default_factory=list)


class StructuralGapExample(SchemaModel):
    instance: str = Field(max_length=256)
    port: str = Field(max_length=128)
    expression_kind: str | None = Field(default=None, max_length=64)
    parent_kind: str | None = Field(default=None, max_length=64)
    operator: str | None = Field(default=None, max_length=64)
    actual_width: StrictInt | None = None
    port_width: StrictInt | None = None


class StructuralGapSummary(SchemaModel):
    reason: str = Field(pattern=r'^[a-z][a-z0-9_]*$', max_length=128)
    occurrences: StrictInt = Field(ge=1, le=1_000_000_000)
    affected_ports: StrictInt = Field(ge=0, le=1_000_000_000)
    examples: list[StructuralGapExample] = Field(default_factory=list, max_length=2)
    examples_truncated: bool = False


class StructuralScanProgress(SchemaModel):
    inventory: Literal['cumulative_counts_current_page_facts']
    completed_instances: StrictInt = Field(ge=0)
    pending_scopes: StrictInt = Field(ge=0, le=8192)
    cumulative_counts: dict[str, StrictInt]
    page_facts: StrictInt = Field(ge=0)
    status: Literal['available', 'blocked', 'complete']
    next_scopes: list[str] = Field(default_factory=list, max_length=3)


class StructuralResultGroup(SchemaModel):
    group_id: str
    backend: Literal["verdi_npi", "slang"]
    artifact_identity: str | None = None
    selected_top: str | None = None
    kdb_binding: dict[str, Any] | None = None
    scope: str | None = None
    checked_scope: str | None = None
    evidence_domain: str
    status: str
    categories_checked: list[str] = Field(default_factory=list)
    selected_categories: list[str] = Field(default_factory=list)
    total_facts: int = 0
    gaps: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)
    gap_summary: list[StructuralGapSummary] = Field(default_factory=list, max_length=64)
    gap_summary_truncated: bool = False
    scan_progress: StructuralScanProgress | None = None

    @model_serializer(mode='wrap')
    def omit_empty_gap_summary(self, handler):
        row = handler(self)
        if not self.gap_summary:
            row.pop('gap_summary', None)
        if not self.gap_summary_truncated:
            row.pop('gap_summary_truncated', None)
        if self.scan_progress is None:
            row.pop('scan_progress', None)
        return row


class SemanticScanResult(SchemaModel):
    continuation: str | None = None
    suggested_next_action: dict[str, Any] | None = None
    omitted_result_groups: list[str] | None = None
    termination_reason: str | None = None
    backend: Literal["none", "verdi_npi", "slang", "mixed"] = "none"
    category_coverage: list[StructuralCategoryCoverage] = Field(default_factory=list)
    result_groups: list[StructuralResultGroup] = Field(default_factory=list)
    backend_attempts: list[dict[str, str]] = Field(default_factory=list)
    native_execution: dict[str, str] = Field(default_factory=dict)
    status: Literal["not_run", "complete", "partial", "unavailable"] = "not_run"
    scope: str | None = None
    categories_checked: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    total_facts: int = 0
    counts: dict[str, int] = Field(default_factory=dict)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    instance_bindings: list[dict[str, str]] = Field(default_factory=list)
    instances_visited: int = 0
    template_count: int = 0
    ast_nodes_visited: int = 0
    blocking_diagnostics: int = 0
    cache_disposition: str | None = None
    query_artifact_status: str = "not_requested"
    propagation: StructuralPropagationReceipt = Field(default_factory=StructuralPropagationReceipt)
    metrics: dict[str, int | float] = Field(default_factory=dict)
    note: str | None = None
    error_type: str | None = None
    output_truncated: bool = False


class ScanStructuralRisksResult(TruncatableResult):
    analysis_mode: Literal["fast", "auto", "deep"] = "auto"
    lexical_coverage_status: Literal["complete", "zero_coverage", "degraded"] = "complete"
    semantic: SemanticScanResult = Field(default_factory=SemanticScanResult)
    scan_scope: str = "scope1"
    eligible_file_count: int = 0
    files_scanned: int = 0
    coverage_status: Literal["complete", "zero_coverage", "degraded"] = "complete"
    coverage_warnings: list[str] = Field(default_factory=list)
    total_risks: int = 0
    risks: list[StructuralRisk] = Field(default_factory=list)
    categories_scanned: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)
    scan_metrics: dict[str, int | str] = Field(default_factory=dict)


class StructuralScanSummary(SchemaModel):
    """Diagnostic projection; lexical, semantic and display coverage stay separate."""

    eligible_file_count: int
    files_scanned: int
    coverage_status: str
    coverage_warnings: list[str]
    analysis_mode: Literal["fast", "auto", "deep"]
    lexical_coverage_status: str
    categories_scanned: list[str]
    semantic_status: str
    semantic_backend: str = "none"
    semantic_category_coverage: list[StructuralCategoryCoverage] = Field(default_factory=list)
    semantic_result_groups: list[StructuralResultGroup] = Field(default_factory=list)
    semantic_backend_attempts: list[dict[str, str]] = Field(default_factory=list)
    semantic_native_execution: dict[str, str] = Field(default_factory=dict)
    semantic_termination_reason: str | None = None
    semantic_continuation: str | None = None
    semantic_fact_count: int
    semantic_scope: str | None = None
    semantic_categories_checked: list[str]
    semantic_gaps: list[str]
    semantic_propagation: StructuralPropagationReceipt
    semantic_query_artifact_status: str
    semantic_output_truncated: bool
    total_risks: int
    risks_returned: int
    display_truncated: bool
    high_risk_count: int
    high_risk_count_basis: Literal["displayed_risks"] = "displayed_risks"


class ErrorGroup(SchemaModel):
    signature: str
    severity: str
    count: int
    first_line: int
    first_time_ps: int | None = None
    last_time_ps: int | None = None
    sample_event_id: str | None = None
    sample_message: str
    source_file: str | None = None
    source_line: int | None = None
    instance_path: str | None = None
    group_index: int | None = None
    xprop_priority: Literal["high", "normal"] | None = None


class SuggestedToolCall(SchemaModel):
    # A concrete, ready-to-run tool call (args already filled) surfaced when the
    # args first co-exist. Unlike NextAction (a signal-keyed forward link), this
    # carries the actual arguments so a weak model can run it verbatim instead of
    # re-deriving them. Suggestion only — the caller still decides.
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str | None = None
    reason_code: str | None = None
    addresses: list[str] | None = None
    remaining_gaps: list[str] | None = None


class ParseSimLogResult(TruncatableResult):
    native_time_unit: Literal["fs", "ps", "ns", "us", "ms", "s"] | None = None
    log_file: str
    # Generic, boundary-safe pointer set when a scoreboard/compare-style failure
    # is detected: such failures are often a SYMPTOM of a lower-level bus-protocol
    # problem. The hint names the protocol-health tools but does NOT assert a
    # protocol type or a specific signal — root-cause judgement stays with the LLM.
    # Placed near the top of the schema ON PURPOSE: parse_sim_log returns large
    # first_group_context/failure_events blobs, and a hint buried after them gets
    # diluted past a weak model's attention. Field order = serialized JSON order,
    # so this surfaces the pointer before the big payloads.
    protocol_symptom_hint: str | None = None
    # Optional sweep or coverage-repair discovery action, bound to a waveform
    # from the same discovery observation as this log. Missing time/scope
    # evidence stays explicit; this is never a required workflow step.
    protocol_symptom_next_step: SuggestedToolCall | None = None
    simulator: str
    schema_version: str
    contract_version: str
    failure_events_schema_version: str
    parser_capabilities: list[str] = Field(default_factory=list)
    runtime_total_errors: int
    runtime_fatal_count: int
    runtime_error_count: int
    unique_types: int
    total_groups: int
    truncated: bool
    max_groups: int
    first_error_line: int
    groups: list[ErrorGroup] = Field(default_factory=list)
    sampling_strategy: str | None = None
    failure_events: list[dict[str, Any]] = Field(default_factory=list)
    failure_events_total: int = 0
    failure_events_returned: int = 0
    failure_events_truncated: bool = False
    previous_log_detected: bool = False
    candidate_previous_logs: list[str] = Field(default_factory=list)
    suggested_followup_tool: str | None = None
    first_group_context: ErrorContextResult | None = None
    problem_hints: ProblemHints | None = None
    auto_diff: DiffResult | None = None
    log_snapshot_id: str | None = None
    previous_log_snapshot_id: str | None = None


class ErrorContextResult(SchemaModel):
    log_file: str
    center_line: int
    start_line: int
    end_line: int
    context: str


class DiffEventSummary(SchemaModel):
    total_events: int
    unique_groups: int
    groups: dict[str, int] = Field(default_factory=dict)


class DiffProblemHintsComparison(SchemaModel):
    base: ProblemHints
    new: ProblemHints
    x_resolved: bool = False
    z_resolved: bool = False
    x_introduced: bool = False
    z_introduced: bool = False
    error_pattern_changed: bool = False
    error_pattern_transition: str | None = None
    first_error_time_shift_ps: int | None = None
    first_error_time_direction: Literal["later", "earlier", "unchanged"] | None = None


class PersistentEventDetail(SchemaModel):
    base_event: dict[str, Any]
    new_event: dict[str, Any]
    time_shift_ps: int | None = None
    time_direction: Literal["later", "earlier"] | None = None
    group_changed: bool = False
    mechanism_changed: bool = False
    mechanism_transition: str | None = None
    x_to_deterministic: bool = False
    value_changed: bool = False


class DiffResult(SchemaModel):
    base_log_file: str | None = None
    new_log_file: str | None = None
    base_snapshot_id: str | None = None
    new_snapshot_id: str | None = None
    diff_source: (
        Literal["paths", "snapshots", "mixed", "auto_previous_snapshot"] | None
    ) = None
    base_summary: DiffEventSummary
    new_summary: DiffEventSummary
    problem_hints_comparison: DiffProblemHintsComparison | None = None
    resolved_events: list[dict[str, Any]] = Field(default_factory=list)
    persistent_events: list[PersistentEventDetail] = Field(default_factory=list)
    new_events: list[dict[str, Any]] = Field(default_factory=list)
    comparison_notes: list[str] = Field(default_factory=list)
    convergence_summary: str | None = None


class FstRecordingGap(SchemaModel):
    start_fs: int
    end_fs: int
    start_inclusive: bool
    end_inclusive: bool
    reason: Literal["outside_recorded_range", "unrecorded", "dump_inactive",
                    "unrecorded_after_dump_resume", "read_limit"]


class FstReading(SchemaModel):
    mode: Literal["isolated_libfst_v1"]
    coverage_status: Literal["complete", "partial"]
    recorded_start_fs: int
    recorded_end_fs: int
    gaps: list[FstRecordingGap]
    time_labels: Literal["ceil_ps"]
    native_memory_limit_bytes: int


class FstBackend(SchemaModel):
    dependency: Literal["pylibfst"]
    version: str
    native_path: str
    native_sha256: str
    storage_count: int
    supported_tools: list[str]
    analysis_status: Literal["not_run"]
    observation_scope: Literal["recorded_digital_values_only"]


class WaveformSummaryResult(SchemaModel):
    file: str
    format: str
    timescale_ps: float | None = None
    # Time-scale self-check: the scale the parser read from the waveform
    # header ("100fs"/"1ps"/"1ns"; "unknown" when unreadable) and the derived
    # fs-per-tick conversion factor (0 = unknown). Timestamps in all tool
    # output are real picoseconds converted with this factor.
    scale_unit: str | None = None
    scale_fs_per_tick: int | None = None
    scale_warning: str | None = None
    simulation_duration_ps: int
    simulation_duration_ns: float
    total_signals: int
    top_modules: list[str] | None = None
    sample_signals: list[str] | None = None
    metadata_query_mode: Literal["native_v1", "legacy_search", "fst_isolated_v1"] | None = None
    sample_signals_order: Literal["lexical", "legacy_ranked"] | None = None
    top_modules_complete: bool | None = None
    producer_hint: str | None = None
    producer_evidence: str | None = None
    fst_backend: FstBackend | None = None
    fst_reading: FstReading | None = None


class SearchSignalsResult(SchemaModel):
    keyword: str
    total_matched: int
    truncated: bool = False
    results: list[dict[str, Any]] = Field(default_factory=list)
    hint: str | None = None


class SearchSignalsBatchEntry(SearchSignalsResult):
    """One batch row has the same strict shape as a single search result."""


class SearchSignalsBatchResult(SchemaModel):
    """search_signals with a list keyword: one entry per keyword, input order,
    each the same shape as a single search. Exists to collapse the
    consecutive-search keyword-groping chains telemetry surfaced (334/524
    calls arrived in runs of >=4) into one round trip."""

    batch: list[SearchSignalsBatchEntry] = Field(default_factory=list)
    hint: str | None = None


class SignalValue(SchemaModel):
    bin: str | None = None
    hex: str | None = None
    dec: int | None = None


class WaveformSelection(SchemaModel):
    """Exact dump declaration: ordered bits or lsb+width."""
    path: str = Field(min_length=1, max_length=16384, description="Exact declaration path returned by search_signals.")
    bits: list[StrictInt] | None = Field(default=None, min_length=1, max_length=4096,
        description="Unique declared indices, MSB first. Supply bits OR lsb+width.")
    lsb: StrictInt | None = Field(default=None, description="Declared start index; width extends toward the declaration's left bound.")
    width: StrictInt | None = Field(default=None, ge=1, le=4096, description="Number of selected bits; requires lsb.")

    @model_validator(mode="after")
    def validate_selection(self):
        if self.bits is not None:
            if self.lsb is not None or self.width is not None:
                raise ValueError("provide bits OR lsb + width")
            if len(set(self.bits)) != len(self.bits):
                raise ValueError("selection bits must be unique")
        elif self.lsb is None or self.width is None:
            raise ValueError("provide ordered bits or both lsb and width")
        return self


class WaveformSelectionReceipt(SchemaModel):
    key: str
    path: str
    declared_range: dict[str, int]
    bits: list[int]
    width: int


class ExpressionMember(SchemaModel):
    name: str = Field(min_length=1, max_length=256)
    lsb: StrictInt = Field(ge=0, le=4095, description="Member offset from the packed aggregate's least significant bit.")
    type: "ExpressionType"


# Both bounds have the same type. Publish homogeneous items so clients that
# support items but discard prefixItems still expose integer bounds to models.
# Runtime validation and the immutable pair representation remain unchanged.
ExpressionRange = Annotated[
    tuple[StrictInt, StrictInt],
    WithJsonSchema({"type": "array", "items": {"type": "integer"}, "minItems": 2, "maxItems": 2}),
]


class ExpressionType(SchemaModel):
    """Explicit integral type; width is packed bits per element."""
    width: StrictInt = Field(ge=1, le=4096, description="Total packed bits per element; {width:8} describes unsigned [7:0].")
    signed: bool = Field(default=False, description="Declared signedness; default unsigned.")
    two_state: bool = Field(default=False, description="True for two-state; default four-state.")
    packed: list[ExpressionRange] = Field(default_factory=list, max_length=8,
        description="Declared [left,right] ranges, outermost first; e.g. [[3,0],[7,0]] for four packed bytes.")
    unpacked: list[ExpressionRange] = Field(default_factory=list, max_length=8,
        description="Unpacked [left,right] ranges, outermost first; requires elements or path_template.")
    members: list[ExpressionMember] = Field(default_factory=list, max_length=128,
        description="Packed struct/union: name, lsb, type. Packed arrays use the innermost aggregate; its final packed range is flattened bits.")


class ExpressionElement(SchemaModel):
    indices: list[StrictInt] = Field(min_length=1, max_length=8, description="One declared index per unpacked dimension.")
    signal: str | WaveformSelection


class ExpressionArray(SchemaModel):
    """Sparse elements or bounded 1D path template."""
    elements: list[ExpressionElement] = Field(default_factory=list, max_length=128,
        description="Exact sparse mappings. Missing elements remain missing evidence.")
    path_template: str | None = Field(default=None, min_length=1, max_length=16384,
        description="Instead of elements: one {index}, e.g. tb.m[{index}][7:0]; expand types.<name>.unpacked (1D, <=128).")

    @model_validator(mode="after")
    def check_mapping(self):
        if self.path_template is not None:
            if self.elements:
                raise ValueError("provide elements OR path_template")
            if self.path_template.count("{index}") != 1 or any(c in self.path_template.replace("{index}", "") for c in "{}"):
                raise ValueError("path_template requires exactly one {index} and no other placeholders")
        elif "elements" not in self.model_fields_set:
            raise ValueError("provide elements OR path_template")
        return self


class WaveformExpression(SchemaModel):
    """Read-only SV expression evaluated per sample."""
    expr: str = Field(min_length=1, max_length=16384,
        description="SV values, e.g. a[i], data[base+:W], $countones(mask); no assignments, user functions or temporal syntax.")
    typing: Literal["semantic", "wave_bits"] = Field(default="semantic",
        description="semantic: types required. wave_bits: unsigned dump vectors. No automatic RTL type loading.")
    scope: str | None = Field(default=None, max_length=16384,
        description="Unbound-name prefix, e.g. tb.dut; bindings override. Not a type source.")
    bindings: dict[str, str | WaveformSelection | ExpressionArray] = Field(default_factory=dict, max_length=128,
        description="Names to exact search_signals paths, fixed selections or arrays.")
    types: dict[str, ExpressionType] = Field(default_factory=dict, max_length=128,
        description="Declared operand/cast types: signedness and array/member layout; dump width cannot infer these.")
    constants: dict[str, str] = Field(default_factory=dict, max_length=128,
        description="Integral SV text, e.g. {W:'8'} for slice width. No parameter/macro lookup.")


ExpressionMember.model_rebuild()


class ExpressionDependency(SchemaModel):
    signal: str
    bits: list[int]
    width: int
    role: str
    value: str | None = None


class ExpressionObservation(SchemaModel):
    time_fs: int
    time_ps: int
    phase: Literal['after','before','dependency_anchor']
    value: str | None = None
    gaps: list[str] = Field(default_factory=list)
    dependencies: list[ExpressionDependency] = Field(default_factory=list)


class WaveformExpressionReceipt(SchemaModel):
    key: str
    expr: str
    typing: Literal['semantic','wave_bits']
    kind: Literal['derived'] = 'derived'
    width: int
    signed: bool
    two_state: bool
    type_sources: dict[str,str] = Field(default_factory=dict)
    dependencies: list[str] = Field(default_factory=list)
    observations: list[ExpressionObservation] = Field(default_factory=list)
    observation_count: int = 0
    observations_truncated: bool = False
    coverage_status: Literal['not_observed','complete','partial'] = 'not_observed'
    gaps: list[str] = Field(default_factory=list)
    side: Literal['a','b'] | None = None
    wave_path: str | None = None


class TlulFields(SchemaModel):
    a_valid: str | WaveformSelection | WaveformExpression | None = None
    a_ready: str | WaveformSelection | WaveformExpression | None = None
    a_source: str | WaveformSelection | WaveformExpression | None = None
    a_opcode: str | WaveformSelection | WaveformExpression | None = None
    a_param: str | WaveformSelection | WaveformExpression | None = None
    a_size: str | WaveformSelection | WaveformExpression | None = None
    a_address: str | WaveformSelection | WaveformExpression | None = None
    a_mask: str | WaveformSelection | WaveformExpression | None = None
    a_data: str | WaveformSelection | WaveformExpression | None = None
    a_user: str | WaveformSelection | WaveformExpression | None = None
    d_valid: str | WaveformSelection | WaveformExpression | None = None
    d_ready: str | WaveformSelection | WaveformExpression | None = None
    d_source: str | WaveformSelection | WaveformExpression | None = None
    d_opcode: str | WaveformSelection | WaveformExpression | None = None
    d_param: str | WaveformSelection | WaveformExpression | None = None
    d_size: str | WaveformSelection | WaveformExpression | None = None
    d_sink: str | WaveformSelection | WaveformExpression | None = None
    d_data: str | WaveformSelection | WaveformExpression | None = None
    d_user: str | WaveformSelection | WaveformExpression | None = None
    d_error: str | WaveformSelection | WaveformExpression | None = None


class SignalAtTimeResult(SchemaModel):
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    signal: str
    time_ps: int
    time_ns: float
    value: dict[str, Any] | None = None
    value_status: Literal["recorded", "outside_recorded_range", "unrecorded", "dump_inactive",
                          "unrecorded_after_dump_resume", "read_incomplete"] | None = None
    fst_reading: FstReading | None = None
    # Set when a bare bus name was auto-completed to name[msb:lsb]; carries the
    # original input so the caller sees the path was resolved.
    resolved_from: str | None = None


class SignalTransitionsResult(SchemaModel):
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    signal: str
    start_ps: int
    end_ps: int
    # Total transitions found in [start_ps, end_ps] — NOT len(transitions):
    # the returned list is capped at max_transitions (earliest kept).
    transition_count: int
    transitions: list[dict[str, Any]] = Field(default_factory=list)
    # Last value-change strictly before start_ps. Kept separate so transitions
    # remains a strict closed-window list while clock samplers can classify the
    # first in-window edge without rereading the waveform.
    predecessor: dict[str, Any] | None = None
    initial_state: dict[str, Any] | None = None
    fst_reading: FstReading | None = None
    predecessor_kind: Literal["declaration_anchor", "dependency_anchor"] | None = None
    truncated: bool = False
    transition_count_is_lower_bound: bool = False
    hint: str | None = None


class SignalsAroundTimeResult(SchemaModel):
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    center_time_ps: int
    center_time_ns: float
    window_ps: int
    extra_transitions: int
    signals: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False
    # "values_only" when the per-signal transition lists were stripped server-side
    # (each signal instead carries window_transition_count); "full" otherwise.
    return_mode: Literal["full", "values_only"] = "full"
    # Set when a value_at_center is a sub-cycle transient (combinational glitch at
    # the clock edge that settles back within the cycle); the affected signals also
    # carry center_transient / center_settles_to / center_settle_ps. None otherwise.
    transient_note: str | None = None
    # Bare bus names auto-completed to name[msb:lsb] (original -> resolved). The
    # signals dict is keyed by the resolved path.
    resolved_aliases: dict[str, str] = Field(default_factory=dict)
    # did_you_mean for signals that still could not be resolved (original -> candidates).
    signal_suggestions: dict[str, list[str]] = Field(default_factory=dict)


class CycleEntry(SchemaModel):
    cycle: int
    time_ps: int
    time_ns: float
    time_fs: int | None = None
    sample_time_fs: int | None = None
    signals: dict[str, SignalValue] = Field(default_factory=dict)


class GetSignalsByCycleResult(SchemaModel):
    cycle_index_origin: Literal["global", "window"] = "global"
    local_coverage: dict[str, Any] | None = None
    reading: dict[str, Any] | None = None
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    transition_data_truncated: bool = False
    transition_signals_truncated: list[str] = Field(default_factory=list)
    clock_path: str
    edge: Literal["posedge", "negedge"]
    sample_phase: Literal["after", "before"] = "after"
    sample_offset_ps: int = 1
    clock_period_ps: int | None = None
    clock_period_fs: int | None = None
    total_edges_found: int
    clock_edges_complete: bool = True
    start_cycle: int
    num_cycles_requested: int
    effective_num_cycles: int
    num_cycles_returned: int
    capped: bool = False
    truncated: bool
    resolved_from_time: bool = False
    requested_start_time_ps: int | None = None
    requested_end_time_ps: int | None = None
    cycles: list[CycleEntry] = Field(default_factory=list)
    signal_errors: dict[str, str] = Field(default_factory=dict)
    # Bare bus names auto-completed to name[msb:lsb] (original -> resolved). The
    # cycle signal maps are keyed by the resolved path.
    resolved_aliases: dict[str, str] = Field(default_factory=dict)
    # did_you_mean for signals that still could not be resolved (path -> candidates).
    signal_suggestions: dict[str, list[str]] = Field(default_factory=dict)


class AnalyzeFailuresResult(TruncatableResult):
    summary: dict[str, Any] = Field(default_factory=dict)
    focused_group: dict[str, Any] | None = None
    focused_event: dict[str, Any] | None = None
    log_context: dict[str, Any] | None = None
    wave_context: dict[str, Any] | None = None
    remaining_groups: int = 0
    signals_queried: list[str] | None = None
    extra_transitions: int | None = None
    analysis_guide: dict[str, str] = Field(default_factory=dict)
    problem_hints: ProblemHints | None = None


class TimeAnchor(SchemaModel):
    time_ps: int | None = None
    kind: str
    log_line: int | None = None
    wave_path: str


class AnalyzeFailureEventResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    failure_event: dict[str, Any]
    time_anchor: TimeAnchor
    likely_instances: list[dict[str, Any]] = Field(default_factory=list)
    recommended_signals: list[dict[str, Any]] = Field(default_factory=list)
    related_source_files: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_summary: list[str] = Field(default_factory=list)
    source_correlation: dict[str, Any] | None = None
    backend_status: BackendStatus | None = None


class StructuralRiskCorrelation(SchemaModel):
    risk_type: str
    file: str
    line: int
    module: str | None = None
    risk_level: Literal["high", "medium", "low"]
    detail: str
    relevance_score: int
    relevance_reasons: list[str] = Field(default_factory=list)


class RecommendNextStepsResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    primary_failure_target: dict[str, Any] | None = None
    recommended_signals: list[dict[str, Any]] = Field(default_factory=list)
    recommended_instances: list[dict[str, Any]] = Field(default_factory=list)
    correlated_structural_risks: list[StructuralRiskCorrelation] = Field(
        default_factory=list
    )
    suspected_failure_class: str
    recommendation_strategy: str | None = None
    failure_window_center_ps: int | None = None
    why: list[str] = Field(default_factory=list)
    # Runtime-layer counterpart of correlated_structural_risks: flagged rows
    # carried over from a compatible sweep_handshakes cache. Facts (already sorted
    # by sweep's mechanical key), never a verdict — the LLM judges them.
    runtime_protocol_findings: list[dict[str, Any]] = Field(default_factory=list)
    # Coverage receipt is kept even when there are no flagged rows. In
    # particular, zero_coverage + [] findings is not a protocol pass.
    runtime_protocol_coverage: dict[str, Any] | None = None
    suggested_next_actions: list[dict[str, Any]] = Field(default_factory=list)
    workflow_incomplete: bool = False
    degraded_reason: (
        Literal[
            "missing_structural_scan",
            "missing_handshake_sweep",
            "incomplete_handshake_sweep",
        ]
        | None
    ) = None
    required_next_call: dict[str, Any] | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    next_iteration_hint: dict[str, Any] | None = None
    source_correlation: dict[str, Any] | None = None
    backend_status: BackendStatus | None = None


RecommendFailureDebugNextStepsResult = RecommendNextStepsResult


class DiagnosticSnapshotSection(SchemaModel):
    available: bool
    stale: bool = False
    summary: dict[str, Any] | None = None
    suggested_call: dict[str, Any] | None = None


class DiagnosticSnapshot(SchemaModel):
    sim_paths: DiagnosticSnapshotSection
    hierarchy: DiagnosticSnapshotSection
    design_context: DiagnosticSnapshotSection | None = None
    log_analysis: DiagnosticSnapshotSection
    structural_scan: DiagnosticSnapshotSection | None = None
    # Optional sweep evidence, including not_run and partial coverage. Its
    # suggested_call is advisory and never makes sweep a missing workflow step.
    protocol_health: DiagnosticSnapshotSection | None = None
    recommended_next: DiagnosticSnapshotSection
    simulator: str | None = None
    case_dir: str | None = None
    top_module: str | None = None
    total_errors: int | None = None
    problem_hints: ProblemHints | None = None
    primary_failure_target: dict[str, Any] | None = None
    suspected_failure_class: str | None = None
    recommended_signals: list[dict[str, Any]] | None = None
    # Mirrors ParseSimLogResult.protocol_symptom_hint when a scoreboard/compare
    # failure is present, so the snapshot's recommended_next surfaces the same
    # boundary-safe protocol-health pointer at session start.
    protocol_symptom_hint: str | None = None
    missing_steps: list[dict[str, Any]] | None = None


class DriverStatementEvidence(SchemaModel):
    source_line: int = Field(ge=1)
    source_column: int | None = Field(default=None, ge=0)
    evidence_index: int = Field(ge=0)


class DriverEvidenceGroup(SchemaModel):
    structural_driver_id: str | None = None
    statement_semantics: dict[str, Any] | None = None
    source_column: int | None = None
    statement_count: int | None = Field(default=None, ge=0)
    statement_evidence: list[DriverStatementEvidence] | None = None
    evidence_index: int | None = None


class DriverChainHop(DriverEvidenceGroup):
    depth: int
    signal_path: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    driver_kind: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_info_origin: Literal["compile_log", "npi", "source_graph"] | None = None
    expression_summary: str | None = None
    upstream_signals: list[str] = Field(default_factory=list)
    instance_port_connections: list[dict[str, Any]] | None = None
    branch_candidates: list[str] | None = None
    stopped_at: str | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"] = "static"
    backend_confidence: Literal[
        "exact",
        "conditional",
        "partial",
        "approximate",
        "unverified",
    ] = "approximate"


class DriverLoadCrossCheck(SchemaModel):
    """Receipt of the NPI driver-vs-loads contradiction check.

    Emitted only by the NPI backend when the driver it reports for a net is
    ALSO a load of that same net — an interface-slice alias in the net's
    ``driver_list``, or a register reached by fan-in — and no genuine RTL
    driver remains. A net cannot be both driven by and read into the same
    pin, so that "driver" is a LOAD: the real driver is testbench/behavioral
    (procedural drive via virtual interface + clocking block), invisible to
    NPI's RTL fan-in. ``conflict=True`` pairs with
    ``driver_status="testbench_driven"`` on the parent result.
    """

    performed: bool = False
    conflict: bool = False
    matched_scope: str | None = None
    matched_line: int | None = None
    note: str | None = None


class DriverBitProvenanceSegment(DriverEvidenceGroup):
    """Per-bit-range source provenance for a segmented driver result."""

    target_path: str
    target_bits: list[int] = Field(default_factory=list)
    resolution: Literal["found", "proved_no_driver", "unknown"] | None = None
    reason_codes: list[str] = Field(default_factory=list)
    driver_set_complete: bool | None = None
    source_kind: Literal["signal", "constant", "unresolved"]
    source_path: str | None = None
    terminal_path: str | None = None
    constant_value: str | None = None
    driver_kind: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    confidence: Literal["exact", "conditional", "partial"]
    multiple_driver: bool = False


class DriverTraversalReceipt(SchemaModel):
    """Resource and completeness boundary for driver fact traversal."""

    returned_fact_count: int = Field(ge=0)
    output_limit: int = Field(ge=1)
    output_truncated: bool
    visited_state_count: int = Field(ge=0)
    state_limit: int = Field(ge=1)
    state_truncated: bool
    callback_observed_count: int | None = Field(default=None, ge=0)
    callback_pruned_count: int | None = Field(default=None, ge=0)
    search_exhaustive: bool
    incomplete_reasons: list[
        Literal[
            "output_limit",
            "work_limit",
            "depth_limit",
            "coverage_incomplete",
            "backend_degraded",
        ]
    ] = Field(default_factory=list)
    continuation_supported: Literal[False] = False
    structural_driver_count: int | None = Field(default=None, ge=0)
    structural_driver_limit: int | None = Field(default=None, ge=1)
    driver_limit_truncated: bool = False
    inspected_assignment_count: int | None = Field(default=None, ge=0)
    assignment_limit: int | None = Field(default=None, ge=1)
    assignment_truncated: bool = False
    retained_evidence_count: int | None = Field(default=None, ge=0)
    evidence_limit: int | None = Field(default=None, ge=1)
    evidence_truncated: bool = False


class SourceGraphClaimSemanticsReceipt(SchemaModel):
    """Orthogonal meaning of a Source Graph query result.

    The historical ``confidence`` remains the conservative combination of
    positive source evidence and whole-artifact coverage.  These fields let a
    caller accept a proved positive fact without mistaking it for an exhaustive
    search, an exclusive-driver proof, or permission to make a negative claim.
    """

    positive_fact_confidence: Literal["exact", "conditional", "partial"] | None = None
    target_bit_coverage: Literal["complete", "partial", "none", "not_applicable"]
    global_coverage_status: Literal["complete", "partial", "inconclusive"]
    exhaustive_search: bool
    exclusive_driver_proved: bool | None = None
    negative_claim_allowed: bool


class HierarchyRecoveryReceipt(SchemaModel):
    status: Literal["hit_existing", "rebuilt", "prepared_design", "hit_design_context", "blocked"]
    attempted: bool = False
    coalesced: bool = False
    hierarchy_handle: str | None = None
    design_handle: str | None = None
    hierarchy_available: bool | None = None
    preparation_metrics: dict[str, int | float] | None = None
    context_gaps: list[str] | None = None
    handle_lifetime: Literal["process"] = "process"
    identity_basis: Literal["validated_historical_source", "current_sources"] | None = None
    compile_context: dict[str, Any] | None = None
    blocker: str | None = None
    timeout_sec: float | None = None
    max_source_files: int | None = None
    max_source_bytes: int | None = None
    source_files_read: int | None = None
    source_bytes_read: int | None = None
    wall_time_ms: float = 0
    structural_scan_performed: Literal[False] = False
    note: str | None = None


class ExplainDriverResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    hierarchy_recovery: HierarchyRecoveryReceipt | None = None
    dependency_context: dict[str, Any] | None = None
    wave_design_binding: dict[str, Any] | None = None
    signal_path: str
    wave_path: str
    resolved_rtl_name: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    query_instance_path: str | None = None
    driver_status: str
    driver_kind: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_info_origin: Literal["compile_log", "npi", "source_graph"] | None = None
    expression_summary: str | None = None
    upstream_signals: list[str] = Field(default_factory=list)
    instance_port_connections: list[dict[str, Any]] | None = None
    bit_provenance: list[DriverBitProvenanceSegment] | None = None
    resolved_bit_count: int | None = None
    unresolved_bit_count: int | None = None
    multi_driver_bit_count: int | None = None
    confidence: str | None = None
    claim_semantics: SourceGraphClaimSemanticsReceipt | None = None
    unsupported_reason: str | None = None
    stopped_at: str | None = None
    recursive: bool = False
    driver_chain: list[DriverChainHop] | None = None
    chain_summary: str | None = None
    traversal: DriverTraversalReceipt | None = None
    cross_check: DriverLoadCrossCheck | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"] = "static"
    backend_status: BackendStatus | None = None


ExplainSignalDriverResult = ExplainDriverResult


class DriverEvidenceReferences(SchemaModel):
    """Only these two fields can reference response-local driver tables."""

    statement_semantics_ref: StrictInt | None = Field(default=None, ge=0)
    statement_evidence_ref: StrictInt | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def exclusive_references(self):
        for field in ("statement_semantics", "statement_evidence"):
            ref = field + "_ref"
            if ref in self.model_fields_set:
                if getattr(self, ref) is None or field in self.model_fields_set:
                    raise ValueError("driver_compact_reference_invalid")
        return self


class CompactDriverChainHop(DriverEvidenceReferences, DriverChainHop):
    pass


class CompactDriverBitProvenanceSegment(DriverEvidenceReferences, DriverBitProvenanceSegment):
    pass


class CompactDriverPayload(ExplainDriverResult):
    driver_chain: list[CompactDriverChainHop] | None = None
    bit_provenance: list[CompactDriverBitProvenanceSegment] | None = None


class DriverLocationTable(SchemaModel):
    columns: list[Literal["source_line", "source_column"]]
    rows: list[list[StrictInt | None]]

    @model_validator(mode="after")
    def typed_columns(self):
        from .cancellation import check_cancelled
        if self.columns != ["source_line", "source_column"]:
            raise ValueError("driver_compact_columns_invalid")
        for index, row in enumerate(self.rows):
            if index % 256 == 0:
                check_cancelled()
            if (len(row) != 2 or type(row[0]) is not int or row[0] < 1
                    or (row[1] is not None and (type(row[1]) is not int or row[1] < 0))):
                raise ValueError("driver_compact_location_invalid")
        return self


class DriverStatementReference(SchemaModel):
    locations_ref: StrictInt = Field(ge=0)
    evidence_indices_ref: StrictInt = Field(ge=0)


class DriverEvidenceTables(SchemaModel):
    statement_semantics: list[dict[str, Any]]
    statement_evidence: list[DriverStatementReference]
    locations: list[DriverLocationTable]
    evidence_indices: list[list[Annotated[StrictInt, Field(ge=0)]]]


class DriverCompactEvidenceResult(SchemaModel):
    """Wire-only; canonical analysis and direct dispatch keep ExplainDriverResult."""

    format: Literal["traceweave.driver.compact.v1"]
    tool: Literal["explain_signal_driver"]
    result: CompactDriverPayload
    evidence_tables: DriverEvidenceTables

    @model_validator(mode="after")
    def reference_closure(self):
        from .cancellation import check_cancelled
        tables = self.evidence_tables
        for pair in tables.statement_evidence:
            check_cancelled()
            if (pair.locations_ref >= len(tables.locations)
                    or pair.evidence_indices_ref >= len(tables.evidence_indices)):
                raise ValueError("driver_compact_reference_out_of_range")
            if len(tables.locations[pair.locations_ref].rows) != len(tables.evidence_indices[pair.evidence_indices_ref]):
                raise ValueError("driver_compact_evidence_length_mismatch")
        for section in (self.result.driver_chain, self.result.bit_provenance):
            for group in section or ():
                check_cancelled()
                sem = group.statement_semantics_ref
                evidence = group.statement_evidence_ref
                if sem is not None and sem >= len(tables.statement_semantics):
                    raise ValueError("driver_compact_reference_out_of_range")
                if evidence is not None:
                    if evidence >= len(tables.statement_evidence):
                        raise ValueError("driver_compact_reference_out_of_range")
                    pair = tables.statement_evidence[evidence]
                    if group.statement_count != len(tables.locations[pair.locations_ref].rows):
                        raise ValueError("driver_compact_statement_count_mismatch")
        return self


class BackendAttemptReceipt(SchemaModel):
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"]
    status: Literal[
        "success",
        "unavailable",
        "failed",
        "blocked",
        "timed_out",
        "inconclusive",
        "skipped",
    ]
    reason: str | None = None
    coverage_status: Literal["complete", "partial", "inconclusive"] | None = None


class SourceGraphBlockerReceipt(SchemaModel):
    code: str
    stage: str


class SourceGraphMetricsReceipt(SchemaModel):
    adapter_wall_ms: float | None = None
    prepare_total_wall_ms: float | None = None
    admission_wait_ms: float | None = None
    build_wall_ms: float | None = None
    load_wall_ms: float | None = None
    query_wall_ms: float | None = None
    actual_build_count: int = 0
    coalesced_waiter_count: int = 0
    cancel_to_exit_ms: float | None = None
    worker_cpu_ms: float | None = None
    rss_start_kib: int | None = None
    rss_peak_kib: int | None = None
    rss_end_kib: int | None = None
    ir_bytes: int = 0
    cache_bytes: int = 0
    cache_entry_count: int = 0
    cache_peak_entry_count: int = 0
    cache_peak_bytes: int = 0
    cache_eviction_count: int = 0
    cache_oversize_bypass_count: int = 0
    frontend_launch_count: int = 0
    semantic_session_hit_count: int = 0
    semantic_session_miss_count: int = 0
    semantic_session_restart_count: int = 0
    semantic_session_eviction_count: int = 0
    disk_lookup_wall_ms: float = 0.0
    disk_read_wall_ms: float = 0.0
    disk_validate_wall_ms: float = 0.0
    disk_publish_wall_ms: float = 0.0
    disk_write_wall_ms: float = 0.0
    disk_eviction_wall_ms: float = 0.0
    disk_hit_count: int = 0
    disk_miss_count: int = 0
    disk_corrupt_count: int = 0
    disk_build_skip_count: int = 0
    disk_bytes_read: int = 0
    disk_bytes_written: int = 0
    disk_entry_count: int = 0
    disk_bytes: int = 0
    disk_eviction_count: int = 0

    @model_serializer(mode="wrap")
    def _omit_inactive_phase3d_fields(self, handler):
        data = handler(self)
        for field in (
            "frontend_launch_count",
            "semantic_session_hit_count",
            "semantic_session_miss_count",
            "semantic_session_restart_count",
            "semantic_session_eviction_count",
            "disk_lookup_wall_ms",
            "disk_read_wall_ms",
            "disk_validate_wall_ms",
            "disk_publish_wall_ms",
            "disk_write_wall_ms",
            "disk_eviction_wall_ms",
            "disk_hit_count",
            "disk_miss_count",
            "disk_corrupt_count",
            "disk_build_skip_count",
            "disk_bytes_read",
            "disk_bytes_written",
            "disk_entry_count",
            "disk_bytes",
            "disk_eviction_count",
        ):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


class SourceGraphScopeMatchReceipt(SchemaModel):
    relation: Literal["exact", "superset", "subset", "disjoint", "unproven"]
    reusable: bool
    complete_for_request: bool
    reason: Literal[
        "coverage_complete",
        "coverage_preserved_partial",
        "coverage_preserved_inconclusive",
        "scope_subset",
        "scope_disjoint",
        "scope_unproven",
    ]


class SourceGraphBackendReceipt(SchemaModel):
    adapter_status: Literal["ready", "blocked", "disabled", "invalid"]
    bootstrap_context: dict[str, Any] | None = None
    request_timeout_sec: float | None = None
    request_remaining_sec: float | None = None
    semantic_instances_charged: int | None = None
    semantic_members_charged: int | None = None
    adapter: dict[str, Any] | None = None
    prepare_status: (
        Literal[
            "ready",
            "dependency_blocked",
            "build_failed",
            "worker_crash",
            "timed_out",
            "cancelled",
            "invalid_response",
        ]
        | None
    ) = None
    effective_timeout_sec: float | None = Field(
        default=None,
        ge=0.001,
        le=86_400.0,
    )
    cache_disposition: (
        Literal[
            "hit_exact",
            "hit_superset",
            "miss",
            "bypass_incomplete_key",
            "bypass_capacity",
        ]
        | None
    ) = None
    cache_tier: Literal["memory", "disk", "build", "handoff"] | None = None
    disk_validation_outcome: (
        Literal[
            "disabled",
            "not_checked",
            "hit",
            "not_found",
            "identity_not_reusable",
            "unsafe_namespace",
            "unsafe_entry",
            "manifest_missing",
            "manifest_too_large",
            "manifest_invalid",
            "unknown_format",
            "incomplete_entry",
            "artifact_key_mismatch",
            "artifact_identity_mismatch",
            "build_semantics_mismatch",
            "scope_mismatch",
            "snapshot_mismatch",
            "version_mismatch",
            "coverage_receipt_mismatch",
            "ir_missing",
            "ir_too_large",
            "ir_size_mismatch",
            "ir_digest_mismatch",
            "ir_schema_mismatch",
            "ir_identity_mismatch",
            "io_error",
        ]
        | None
    ) = None
    flight_disposition: Literal["none", "builder", "coalesced"] | None = None
    coverage_status: Literal["complete", "partial", "inconclusive"] | None = None
    coverage_files_total: int = 0
    coverage_files_projected: int = 0
    coverage_diagnostic_count: int = 0
    coverage_blocking_diagnostic_count: int = 0
    coverage_gap_count: int = 0
    coverage_gap_codes: list[str] = Field(default_factory=list)
    objective_exclusions: list[str] = Field(default_factory=list)
    query_status: (
        Literal[
            "found",
            "not_connected",
            "from_unresolved",
            "to_unresolved",
            "endpoints_unresolved",
            "inconclusive",
            "truncated",
        ]
        | None
    ) = None
    query_confidence: Literal["exact", "conditional", "partial"] | None = None
    query_match_count: int = 0
    query_count: int = 0
    attempted_query_count: int = 0
    query_fingerprints_sha256: list[str] = Field(default_factory=list)
    query_statuses: list[Literal["found", "not_connected", "inconclusive"]] = Field(
        default_factory=list
    )
    coverage_statuses: list[Literal["complete", "partial", "inconclusive"]] = Field(
        default_factory=list
    )
    query_gap_codes: list[str] = Field(default_factory=list)
    positive_query_count: int = 0
    complete_negative_query_count: int = 0
    inconclusive_negative_count: int = 0
    traversed_binding_edges: int = 0
    max_depth: int | None = None
    inspected_edge_count: int = 0
    structural_driver_count: int | None = None
    inspected_assignment_count: int | None = None
    retained_evidence_count: int | None = None
    assignment_limit: int | None = None
    evidence_limit: int | None = None
    assignment_truncated: bool = False
    evidence_truncated: bool = False
    state_limit: int | None = None
    edge_limit: int | None = None
    match_limit: int | None = None
    frontier_limit: int | None = None
    state_truncated: bool = False
    edge_truncated: bool = False
    match_truncated: bool = False
    frontier_truncated: bool = False
    query_truncated: bool = False
    queried_bit_count: int = 0
    resolved_bit_count: int = 0
    unresolved_bit_count: int = 0
    constant_bit_count: int = 0
    multi_driver_bit_count: int = 0
    claim_semantics: SourceGraphClaimSemanticsReceipt | None = None
    path_edge_count: int = 0
    traversed_edge_count: int = 0
    visited_state_count: int = 0
    traversal_limit: int | None = None
    output_limit: int | None = None
    traversal_truncated: bool = False
    output_truncated: bool = False
    endpoint_alias_equivalent: bool = False
    expand_assigns: bool | None = None
    build_key_sha256: str | None = None
    artifact_fingerprint_sha256: str | None = None
    selected_artifact_fingerprint_sha256: str | None = None
    final_artifact_fingerprint_sha256: str | None = None
    attempted_artifact_fingerprints_sha256: list[str] = Field(default_factory=list)
    artifact_attempt_count: int = 0
    scope_expansion_count: int = 0
    single_artifact_provenance: bool | None = None
    final_artifact_scope_match: bool | None = None
    query_fingerprint_sha256: str | None = None
    artifact_reuse: (
        Literal[
            "cold",
            "exact_hit",
            "dominating_hit",
            "coalesced_build",
            "session_handoff",
            "bypass_incomplete",
            "bypass_capacity",
            "disk_exact_hit",
        ]
        | None
    ) = None
    cache_lookup_reason: (
        Literal[
            "exact_artifact",
            "dominating_artifact",
            "no_cached_artifact",
            "artifact_semantics_mismatch",
            "cached_scope_not_dominating",
            "identity_not_reusable",
            "same_artifact_inflight",
            "same_artifact_session_handoff",
            "artifact_exceeds_cache_capacity",
            "cancelled_before_lookup",
        ]
        | None
    ) = None
    scope_match: SourceGraphScopeMatchReceipt | None = None
    compile_fingerprint_sha256: str | None = None
    ir_fingerprint_sha256: str | None = None
    blocker: SourceGraphBlockerReceipt | None = None
    metrics: SourceGraphMetricsReceipt = Field(
        default_factory=SourceGraphMetricsReceipt
    )
    fallback_used: bool = False

    @model_serializer(mode="wrap")
    def _omit_inactive_phase3d_fields(self, handler):
        data = handler(self)
        for field in ("cache_tier", "disk_validation_outcome"):
            if field not in self.model_fields_set:
                data.pop(field, None)
        return data


class KdbArtifactEvidence(SchemaModel):
    artifact_status: Literal["missing", "empty", "source_only_candidate", "elaborated_candidate", "elaboration_error", "unrecognized"]
    identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    identity_basis: Literal["bounded_elaboration_maps"]
    identity_status: Literal["complete", "partial"]
    load_status: Literal["not_checked", "loaded", "failed"]
    source_association: Literal["unverified"]
    gaps: list[str]


class DesignQueryMetrics(SchemaModel):
    source_validation_ms: float = Field(ge=0)
    binding_wait_ms: float = Field(ge=0)
    discovery_ms: float = Field(ge=0)
    discovery_read_bytes: int = Field(ge=0)
    discovery_file_count: int = Field(ge=0)
    discovery_entry_count: int = Field(ge=0)
    anchor_ms: float = Field(ge=0)
    binding_ms: float = Field(ge=0)
    binding_validation_ms: float = Field(ge=0)
    query_ms: float = Field(ge=0)
    final_validation_ms: float = Field(ge=0)
    total_ms: float = Field(ge=0)


class DesignQueryReceipt(SchemaModel):
    design_handle: str
    context_identity_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    backend_preference: Literal["auto", "source"]
    binding_reused: bool
    selection: Literal["candidate", "unavailable", "source_policy", "probe_partial", "unusable_candidate"]
    artifact: KdbArtifactEvidence | None = None
    source_snapshot_status: Literal["complete", "partial"]
    include_resolution_status: Literal["complete", "partial"]
    previous_npi_failure: Literal["npi_load_failed", "npi_lsf_npi_unavailable"] | None = None
    gaps: list[str]
    metrics: DesignQueryMetrics


class BackendStatus(SchemaModel):
    design_context: DesignQueryReceipt | None = None
    simulator: Literal["vcs", "xcelium", "verilator", "unknown"] = "unknown"
    # ``backend`` retains its legacy meaning (policy-selected backend).  The
    # additive fields make a multi-attempt NPI -> Source Graph -> Static route
    # explicit without changing existing callers.
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"] = "static"
    selected_backend: (
        Literal[
            "static",
            "verdi_npi",
            "verdi_tcl",
            "source_graph",
        ]
        | None
    ) = None
    attempted_backend: (
        Literal[
            "static",
            "verdi_npi",
            "verdi_tcl",
            "source_graph",
        ]
        | None
    ) = None
    actual_backend: (
        Literal[
            "static",
            "verdi_npi",
            "verdi_tcl",
            "source_graph",
        ]
        | None
    ) = None
    attempted_backends: list[BackendAttemptReceipt] = Field(default_factory=list)
    whole_trace_restart_count: int = 0
    whole_trace_restart_reasons: list[str] = Field(default_factory=list)
    single_backend_provenance: bool | None = None
    fallback_reason: str | None = None
    connectivity_route: Literal["auto", "source_graph"] = "auto"
    connectivity_route_error: Literal["connectivity_route_config_invalid"] | None = None
    source_graph: SourceGraphBackendReceipt | None = None
    execution_mode: Literal["local", "lsf", "invalid"] | None = None
    scheduler_status: (
        Literal[
            "not_started",
            "completed",
            "failed",
            "timed_out",
        ]
        | None
    ) = None
    worker_status: (
        Literal[
            "not_started",
            "completed",
            "npi_unavailable",
            "failed",
        ]
        | None
    ) = None
    parser_match: Literal["exact", "approximate"] = "approximate"
    kdb_path: str | None = None
    kdb_flow: Literal[
        "vcs_two_step",
        "vcs_three_step",
        "vericom_standalone",
        "vericom_import_from_file",
        "traceweave_cached",
        "explicit",
        "none",
    ] = "none"
    kdb_validation_status: Literal[
        "usable",
        "elaboration_error",
        "unavailable",
    ] = "unavailable"
    # ``kdb_validation_status`` describes the artifact on disk.  This flag is
    # stronger: it becomes true only after NPI actually loaded that partial
    # netlist and passed the top-instance self-check.
    kdb_degraded: bool = False
    kdb_load_status: Literal["not_checked", "loaded", "failed"] | None = None
    kdb_error_count: int | None = Field(default=None, ge=0)
    kdb_error_log: str | None = None
    kdb_hint: str | None = None


class LoadHop(SchemaModel):
    load_path: str
    kind: Literal["module_input", "rhs_expr", "always_sensitivity"]
    expr: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_info_origin: Literal["compile_log", "npi", "source_graph"] | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"] = "static"
    confidence: Literal[
        "exact",
        "conditional",
        "partial",
        "approximate",
        "unverified",
    ] = "approximate"


class LoadEnumerationReceipt(SchemaModel):
    """Backend-neutral bounds and claim strength for a load enumeration."""

    returned_count: int = Field(ge=0)
    output_limit: int = Field(ge=1)
    output_truncated: bool = False
    search_exhaustive: bool = False
    incomplete_reasons: list[
        Literal[
            "output_limit",
            "work_limit",
            "depth_limit",
            "coverage_incomplete",
            "backend_degraded",
        ]
    ] = Field(default_factory=list)
    # No backend currently has a continuation token that can preserve its
    # work/coverage identity safely.  Keep the fact explicit instead of
    # implying that a truncated prefix can be resumed.
    continuation_supported: Literal[False] = False


class FindSignalLoadsResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    wave_design_binding: dict[str, Any] | None = None
    signal_path: str
    resolved_rtl_name: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    loads: list[LoadHop] = Field(default_factory=list)
    completeness: Literal["exact", "approximate", "shallow_only"] = "shallow_only"
    stopped_at: str | None = None
    unsupported_reason: str | None = None
    claim_semantics: SourceGraphClaimSemanticsReceipt | None = None
    enumeration: LoadEnumerationReceipt | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl", "source_graph"] = "static"
    backend_status: BackendStatus = Field(default_factory=BackendStatus)


class SignalPathHop(SchemaModel):
    index: int
    net_path: str
    scope_inst: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    is_endpoint: bool = False
    source_info_origin: Literal["npi", "source_graph"] | None = None
    backend: Literal["verdi_npi", "source_graph"] | None = None
    edge_kind: (
        Literal[
            "port_bind_input",
            "port_bind_output",
            "port_bind_inout",
            "interface_bind",
            "continuous_assign",
            "procedural_assign",
        ]
        | None
    ) = None
    edge_id: str | None = None
    edge_source_path: str | None = None
    exact_bit_mapping: bool | None = None


_TRACE_SIGNAL_PATH_DIRECTION_NOTE = (
    "Connectivity only — not a temporal driver relation. "
    "Use explain_signal_driver for driver direction."
)


class TraceSignalPathResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    wave_design_binding: dict[str, Any] | None = None
    from_signal: str
    to_signal: str
    found: bool
    hops: int = 0
    path: list[SignalPathHop] = Field(default_factory=list)
    expand_assigns: bool = False
    direction_note: str = _TRACE_SIGNAL_PATH_DIRECTION_NOTE
    unsupported_reason: (
        Literal[
            "from_not_found",
            "to_not_found",
            "not_connected",
            "static_backend_no_path_api",
            "npi_call_failed",
            "source_graph_endpoints_unresolved",
            "source_graph_query_inconclusive",
            "source_graph_path_truncated",
        ]
        | None
    ) = None
    claim_semantics: SourceGraphClaimSemanticsReceipt | None = None
    backend: Literal["static", "verdi_npi", "source_graph"] = "static"
    backend_status: BackendStatus = Field(default_factory=BackendStatus)


class TraceChainNode(SchemaModel):
    value_status: str | None = None
    fst_reading: FstReading | None = None
    depth: int
    signal_path: str
    value_at_time: str | None = None
    has_x: bool | None = None
    module: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    driver_status: str | None = None
    driver_kind: str | None = None
    driver_expression: str | None = None
    driver_confidence: str | None = None
    claim_semantics: SourceGraphClaimSemanticsReceipt | None = None
    traversal: DriverTraversalReceipt | None = None
    unsupported_reason: str | None = None
    cross_check: DriverLoadCrossCheck | None = None
    instance_port_connections: list[dict[str, Any]] | None = None
    x_upstream_signals: list[str] | None = None
    clean_upstream_signals: list[str] | None = None
    unresolved_signals: list[str] | None = None
    skipped_signals: list[str] | None = None
    trace_stop_reason: str | None = None


class TraceRootCause(SchemaModel):
    signal_path: str | None = None
    driver_kind: str | None = None
    stop_reason: str | None = None
    source_file: str | None = None
    source_line: int | None = None


class XHistoryEvidence(SchemaModel):
    """Observed facts and labelled candidates; frontiers never prove exclusion.

    A predecessor_sampling_candidate retains sampling_order_unresolved and
    uses candidate_* edges. It cannot establish simulator scheduling order.
    async_observation records typed control/clock events; its assignment value
    remains unmodeled and async_control_onset_candidate is not causal proof.
    """
    status: Literal["partial", "blocked", "inconclusive", "signal_is_clean"]
    window: dict[str, Any]
    context: dict[str, Any] = Field(default_factory=dict)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    frontier: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    operation_metrics: dict[str, int | float] = Field(default_factory=dict)


class TraceXSourceResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    wave_design_binding: dict[str, Any] | None = None
    start_signal: str
    start_time_ps: int
    trace_status: str
    trace_depth: int
    max_depth: int
    propagation_chain: list[TraceChainNode] = Field(default_factory=list)
    root_cause: TraceRootCause | None = None
    analysis_guide: dict[str, str] = Field(default_factory=dict)
    backend_status: BackendStatus = Field(default_factory=BackendStatus)
    trace_restarted: bool = False
    mode: Literal["snapshot", "history"] = "snapshot"
    history: XHistoryEvidence | None = None


class PrerequisiteBlockResult(SchemaModel):
    ok: bool = False
    error_code: str = "missing_prerequisite"
    missing_step: str
    required_before: str
    reason: str
    suggested_call: dict[str, Any] = Field(default_factory=dict)
    hierarchy_recovery: HierarchyRecoveryReceipt | None = None


class ToolErrorResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    error: str
    error_code: str | None = None
    hierarchy_recovery: HierarchyRecoveryReceipt | None = None
    fsdb_runtime: dict[str, Any] | None = None
    fst_runtime: FstRuntimeInfo | None = None
    fallback: dict[str, Any] | None = None


class FstCapabilityErrorResult(ToolErrorResult):
    error_code: Literal["fst_analysis_not_validated"] = "fst_analysis_not_validated"
    analysis_status: Literal["not_run"] = "not_run"
    supported_tools: list[str]
    suggested_call: dict[str, Any]


class KeywordLimitErrorResult(ToolErrorResult):
    error_code: Literal["too_many_keywords"] = "too_many_keywords"
    parameter: Literal["keyword"] = "keyword"
    provided_count: StrictInt = Field(gt=0)
    max_count: StrictInt = Field(gt=0)
    search_executed: Literal[False] = False
    recovery: str


class CycleInputErrorResult(ToolErrorResult):
    error_code: Literal["invalid_cycle_arguments"] = "invalid_cycle_arguments"
    issues: list[str] = Field(min_length=1, max_length=3)
    sampling_executed: Literal[False] = False
    recovery: str


class ExpressionRecovery(SchemaModel):
    action: str
    message: str


class ExpressionInputIssue(SchemaModel):
    parameter: str
    message: str


class ExpressionToolErrorResult(ToolErrorResult):
    reason: str
    operand: str | None = None
    position: int | None = Field(default=None, ge=0)
    parameter: str | None = None
    recovery: ExpressionRecovery
    issues: list[ExpressionInputIssue] = Field(default_factory=list, max_length=16)


# ---------------------------------------------------------------------------
# Auto-debug v2: cursors + verify primitives
# ---------------------------------------------------------------------------


class CursorRefSchema(SchemaModel):
    name: str
    time_ps: int
    note: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class CursorSetResult(SchemaModel):
    cursor: CursorRefSchema


class CursorListResult(SchemaModel):
    cursors: list[CursorRefSchema] = Field(default_factory=list)


class CursorDeleteResult(SchemaModel):
    name: str
    deleted: bool


class DivergenceContext(SchemaModel):
    compile_log: str = Field(min_length=1)
    simulator: Literal["auto", "vcs", "xcelium", "verilator"] = "auto"
    supplementary_compile_logs: list[str] = Field(default_factory=list, max_length=16)
    top_hint: str | None = None
    hierarchy_handle: str | None = None
    snapshot_sha256: str | None = None
    source_snapshot_sha256: str | None = None
    compile_log_identity_sha256: str | None = None


class DivergenceQueryContext(DivergenceContext):
    """One explicit prepared handle or an exact legacy compile observation."""
    compile_log: str | None = Field(default=None, min_length=1)
    design_handle: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def explicit_design_identity(self):
        if self.design_handle is None:
            if self.compile_log is None and not (getattr(self, "kdb_path", None) and self.top_hint):
                raise ValueError("compile_log_or_design_handle_required")
        elif any(getattr(self, key) is not None for key in (
            "hierarchy_handle", "snapshot_sha256", "source_snapshot_sha256",
            "compile_log_identity_sha256",
        )):
            raise ValueError("design_handle_conflicts_with_legacy_identity")
        return self


class DivergenceNextAction(SchemaModel):
    side: Literal["a", "b", "both"]
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str
    status: Literal["ready", "needs_context", "needs_prerequisite"]
    missing_fields: list[str] = Field(default_factory=list)
    prerequisite_calls: list[dict[str, Any]] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)


class ComparisonReading(SchemaModel):
    mode_a: Literal['native_event_pages_v1', 'vcd_index_pages', 'fst_spool_pages_v1', 'legacy_materialized', 'event_pages_failed', 'expression_time_groups']
    mode_b: Literal['native_event_pages_v1', 'vcd_index_pages', 'fst_spool_pages_v1', 'legacy_materialized', 'event_pages_failed', 'expression_time_groups']
    events_read: int = Field(default=0, ge=0)
    pages_read: int = Field(default=0, ge=0)
    native_read_calls: int | None = Field(default=None, ge=0)
    record_bytes_read: int | None = Field(default=None, ge=0)
    byte_basis: Literal['native_abi', 'record_estimate', 'unavailable'] = 'unavailable'


class DiffFirstDivergenceResult(SchemaModel):
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    diverged: bool
    wave_path_a: str
    wave_path_b: str
    signal_a: str
    signal_b: str
    start_ps: int
    end_ps: int
    first_divergence_time_ps: int | None = None
    first_divergence_time_fs: int | None = None
    value_a: str | None = None
    value_b: str | None = None
    cursor: CursorRefSchema | None = None
    transitions_compared: int = 0
    missing_a: bool = False
    missing_b: bool = False
    note: str | None = None
    comparison_status: Literal["different", "equal", "inconclusive"] = "inconclusive"
    coverage_status: Literal["complete", "partial", "none"] = "none"
    earliest_difference_proven: bool = False
    coverage_gaps: list[dict[str, Any]] = Field(default_factory=list)
    next_actions: list[DivergenceNextAction] = Field(default_factory=list)
    requested_start_ps: int | None = None
    requested_end_ps: int | None = None
    width_a: int | None = None
    width_b: int | None = None
    excluded_intervals: list[dict[str, Any]] = Field(default_factory=list)
    reading: ComparisonReading | None = None


class PeriodResult(SchemaModel):
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    period_fs: int | None = None
    jitter_fs: int | None = None
    first_off_beat_time_fs: int | None = None
    wave_path: str
    signal: str
    edge: str
    start_ps: int
    end_ps: int
    period_ps: int | None = None
    edges_used: int = 0
    jitter_ps: int = 0
    off_beat_count: int = 0
    first_off_beat_time_ps: int | None = None
    cursor: CursorRefSchema | None = None
    reason: str | None = None


class NextAction(SchemaModel):
    expression_key: str | None = None
    # A forward-link emitted by a bus-fact tool ONLY when it has a concrete
    # finding. Bridges a waveform fact to the next investigation (e.g. attribute a
    # violation to a driving instance). Bus-fact tools never self-attribute
    # master vs slave; attribution = bus-fact + drive-direction, composed by the
    # caller. signal_path is the signal to feed the suggested tool.
    tool: str
    reason: str
    signal_path: str | None = None
    signal_selection: WaveformSelection | None = None


class HandshakeFinding(SchemaModel):
    expression_key: str | None = None
    type: str
    severity: str
    # long_stall fields
    begin_ps: int | None = None
    end_ps: int | None = None
    cycles: int | None = None
    # payload_hold_violation / premature_valid_deassertion fields
    time_ps: int | None = None
    signal: str | None = None
    from_value: str | None = None
    to_value: str | None = None
    stall_begin_ps: int | None = None
    # premature_valid_deassertion: cycles the beat was stalled before valid dropped
    stall_cycles: int | None = None
    # premature_valid_deassertion witness: False = the dropped beat was never
    # accepted (ready/HREADY low the whole time it was asserted). Forecloses the
    # "just AHB pipeline overlap" misreading of a true positive.
    accepted_before_deassert: bool | None = None


class HandshakeAttribution(SchemaModel):
    # Structured side attribution for one-sided protocol violations. NOT read
    # from the trace (which holds values, not ownership) — derived from protocol:
    # payload-hold and premature-valid-deassertion are both breaches of the
    # valid-driver's obligation, so violating_side='valid_driver' /
    # exonerated_side='ready_driver'. A plain (two-sided) stall leaves both None.
    # The valid-driver is the channel producer — master on AXI AW/AR/W, slave on
    # R/B — so resolve the actual instance via explain_signal_driver rather than
    # assuming master (AHB htrans is the exception: always master-driven).
    violating_side: str | None = None
    exonerated_side: str | None = None
    basis: str | None = None
    note: str | None = None


class HandshakeCoverage(SchemaModel):
    # Facts about what inspect_handshake actually evaluated. These are not side
    # or protocol verdicts; discovery/caller context owns those labels.
    clock_sampled: bool = False
    sampling_gaps: list[str] = Field(default_factory=list)
    valid_ready_resolved: bool = False
    stall_checked: bool = False
    backpressure_checked: bool = False
    payload_hold_requested: bool = False
    payload_hold_checked: bool = False
    payload_hold_partially_checked: bool = False
    payload_signals_requested: int = 0
    payload_signals_checked: int = 0
    payload_signals_unresolved: int = 0
    # wait-state hold (premature valid deassertion) — needs no payload signal
    valid_hold_requested: bool = False
    valid_hold_checked: bool = False
    # x-while-valid: only run when the payload is control (address-phase) signals,
    # i.e. AHB (valid_htrans) bundles — off for a literal-valid interface whose
    # payload may be data lanes that are legally x on disabled byte strobes.
    x_while_valid_checked: bool = False
    # AHB write data-phase hold: needs hwrite + write_data (the data-phase window,
    # offset one cycle from the address-phase valid).
    write_data_hold_requested: bool = False
    write_data_hold_checked: bool = False
    # A bounded native transition read returned only a prefix. Existing check
    # booleans still describe what ran, but the facts are partial.
    transition_data_truncated: bool = False
    transition_signals_truncated: int = 0


class HandshakeInspectResult(SchemaModel):
    sampling_phase: Literal["before"] = "before"
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    wave_path: str
    clock: str
    valid: str
    valid_source: str = "signal"
    ready: str
    payload: list[str] = Field(default_factory=list)
    edge: str
    start_ps: int
    end_ps: int
    active_high: bool = True
    sample_count: int = 0
    transfer_count: int = 0
    stall_count: int = 0
    max_stall_cycles: int = 0
    max_stall_begin_ps: int | None = None
    # ended_in_stall: the window ended with a stall still open (valid asserted,
    # ready never came) — the deadlock signature. A fact, not a "deadlock"
    # verdict; the window may simply have been cut short.
    ended_in_stall: bool = False
    final_stall_cycles: int = 0
    ready_without_valid_cycles: int = 0
    payload_hold_violations: int = 0
    payload_hold_checked: bool = False
    # premature_valid_deassertion count: the master dropped a stalled transfer
    # (valid/htrans went inactive) before ready/HREADY arrived. The AHB
    # master-not-waiting-for-HREADY signature payload_hold cannot see.
    valid_deassert_violations: int = 0
    # x_while_valid count: a control/address payload signal was x/z at an edge where
    # valid was known-asserted — a definite violation. Only checked on AHB
    # (control-only payload); see coverage.x_while_valid_checked.
    x_while_valid_violations: int = 0
    # write_data_hold count: HWDATA changed during a write data-phase wait state
    # (HREADY low) — the master must hold write data until accepted. AHB-only.
    write_data_hold_violations: int = 0
    # protocol_semantics: AHB-only receipt naming which metrics are faithful vs
    # suppressed on this interface (so the surface reads as all-true-positive and a
    # reader cannot wave a real finding away as a valid/ready-vs-AHB mismatch).
    # None for a literal-valid interface, where every metric is faithful as-is.
    protocol_semantics: dict[str, str] | None = None
    payload_unresolved: list[str] = Field(default_factory=list)
    transition_data_truncated: bool = False
    transition_signals_truncated: list[str] = Field(default_factory=list)
    coverage: HandshakeCoverage = Field(default_factory=HandshakeCoverage)
    unknown_sample_cycles: int = 0
    findings: list[HandshakeFinding] = Field(default_factory=list)
    # violating_signal: the signal carrying the primary (cursor-anchored) finding
    # — raw material for master/slave attribution, NOT a verdict. None when there
    # is no signal-specific finding. next_actions fires only when a finding
    # exists; it bridges the bus fact to RTL tracing (explain_signal_driver).
    violating_signal: str | None = None
    violating_expression: str | None = None
    # Structured side attribution for one-sided violations (payload-hold,
    # premature-valid-deassertion). Empty (all None) for a two-sided stall.
    attribution: HandshakeAttribution = Field(default_factory=HandshakeAttribution)
    next_actions: list[NextAction] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    signal_errors: dict[str, str] = Field(default_factory=dict)


class HandshakeBundle(SchemaModel):
    scope: str
    clock: str | None = None
    valid: str
    ready: str
    payload: list[str] = Field(default_factory=list)
    confidence: str
    rationale: str
    needs: list[str] = Field(default_factory=list)
    handshake_semantics: Literal["valid_ready", "requires_confirmation"] = "valid_ready"
    signal_directions: dict[str, str] = Field(default_factory=dict)


class SignalDiscoveryCoverage(SchemaModel):
    status: Literal["complete", "partial"] = "complete"
    signal_limit: int = 65_536
    signals_returned: int = 0
    reasons: list[str] = Field(default_factory=list)
    mode: Literal["paged", "native_scope_v1", "vcd_scope_v1", "fst_scope_v1", "legacy_search"] | None = None
    pages_read: int | None = None
    signals_scanned: int | None = None
    scan_limit: int | None = None
    bytes_returned: int | None = None
    byte_limit: int | None = None
    resident_bytes_estimate: int | None = None
    memory_limit: int | None = None
    scope_signals_returned: int | None = None
    scope_total: int | None = None
    scope_total_lower_bound: int | None = None
    ancestor_signals_returned: int | None = None


class SuggestHandshakesResult(SchemaModel):
    wave_path: str
    scope: str | None = None
    candidate_count: int = 0
    candidates: list[HandshakeBundle] = Field(default_factory=list)
    discovery: SignalDiscoveryCoverage = Field(default_factory=SignalDiscoveryCoverage)
    reason: str | None = None


class ProtocolBundle(SchemaModel):
    protocol: Literal["ahb", "apb"]
    scope: str
    direction_tag: Literal["initiator_side", "responder_side", "unknown"] = "unknown"
    direction_basis: str = "unknown"
    direction_confidence: Literal["high", "medium", "unknown"] = "unknown"
    clock: str | None = None
    reset: str | None = None
    valid_htrans: str | None = None
    htrans_rule: str | None = None
    psel: str | None = None
    penable: str | None = None
    ready: str
    payload: list[str] = Field(default_factory=list)
    # AHB only: HWRITE (write qualifier) + HWDATA (data bus) for the write
    # data-phase HWDATA-hold check. None when not located; HWDATA is deliberately
    # NOT in `payload` (address-phase hold) because it is a data-phase signal.
    hwrite: str | None = None
    write_data: str | None = None
    inspect_handshake_args: dict[str, Any] | None = None
    confidence: str
    rationale: str
    needs: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SuggestProtocolBundlesResult(SchemaModel):
    wave_path: str
    protocol: Literal["ahb", "apb"]
    scope: str | None = None
    candidate_count: int = 0
    candidates: list[ProtocolBundle] = Field(default_factory=list)
    discovery: SignalDiscoveryCoverage = Field(default_factory=SignalDiscoveryCoverage)
    reason: str | None = None
    # Copy-paste-ready inspect_handshake relay for the discovered candidates.
    # Discovery only LOCATES interfaces; the analysis step is inspect_handshake.
    # Weak models stop after discovery unless the next call is spelled out for
    # them with concrete args at the point the args first exist (here, not at
    # parse time — parse has no signal paths). Boundary-safe: it advances the
    # analysis, it does not assert a protocol side or a root cause.
    next_step: str | None = None


class SweptInterface(SchemaModel):
    scope: str
    clock: str | None = None
    # kind: "valid_ready" (AXI / generic valid-ready / req-ack) or "ahb" (no
    # literal valid — derived from htrans). For an ahb row, `valid` carries the
    # HTRANS signal path used as the derived valid.
    kind: str = "valid_ready"
    valid: str
    ready: str
    payload: list[str] = Field(default_factory=list)
    confidence: str | None = None
    discovery_needs: list[str] = Field(default_factory=list)
    coverage: HandshakeCoverage = Field(default_factory=HandshakeCoverage)
    # flags are factual observations, never verdicts
    flags: list[str] = Field(default_factory=list)
    # Side attribution for a one-sided violation row (payload-hold / premature
    # deassertion → valid_driver side); None for clean or two-sided-stall rows.
    attribution: HandshakeAttribution | None = None
    sample_count: int = 0
    transfer_count: int = 0
    stall_count: int = 0
    max_stall_cycles: int = 0
    max_stall_begin_ps: int | None = None
    ended_in_stall: bool = False
    final_stall_cycles: int = 0
    payload_hold_violations: int = 0
    valid_deassert_violations: int = 0
    x_while_valid_violations: int = 0
    write_data_hold_violations: int = 0
    # ready_without_valid_cycles is a raw count; on an `ahb` row it is idle-bus
    # (HREADY high while HTRANS idle), NOT backpressure — it is suppressed from
    # `flags` and the sort for ahb rows so it never reads as an anomaly.
    ready_without_valid_cycles: int = 0
    unknown_sample_cycles: int = 0
    transition_data_truncated: bool = False
    transition_signals_truncated: list[str] = Field(default_factory=list)


class SweptSkip(SchemaModel):
    scope: str
    valid: str
    ready: str
    reason: str


class FindingSummary(SchemaModel):
    """Compact top-level factual summary of flagged findings, survives truncation.

    by_flag: count of flagged interfaces carrying each flag type.
    by_channel_hint: per-AXI-channel flagged count (R/W/AR/AW/B, "other" for
                     non-AXI/unidentifiable), inferred from the valid/htrans
                     signal name. SEEDED with 0 for every channel present among
                     inspected interfaces, so a clean channel shows explicitly as
                     ``R: 0`` rather than vanishing — an absent key would be
                     ambiguous (not checked vs. no such channel vs. clean). This
                     is a naming heuristic for grouping only, never a verdict.
    top_scopes: up to 3 DISTINCT scope paths in sort order (most likely
                interesting first); a top-level interface renders as "(top)".
    """

    by_flag: dict[str, int] = Field(default_factory=dict)
    by_channel_hint: dict[str, int] = Field(default_factory=dict)
    top_scopes: list[str] = Field(default_factory=list)


class HandshakeSweepResult(SchemaModel):
    sampling_phase: Literal["before"] = "before"
    wave_path: str
    scope: str | None = None
    edge: str = "posedge"
    start_ps: int = 0
    end_ps: int = -1
    discovered_count: int = 0
    interface_count: int = 0
    flagged_count: int = 0
    transition_truncated_count: int = 0
    truncated: bool = False
    discovery: dict[str, SignalDiscoveryCoverage] = Field(default_factory=dict)
    # Coverage facts for interpreting flagged_count. In particular,
    # zero_coverage means no protocol interfaces were checked, so flagged_count=0
    # is not evidence of a clean protocol run.
    coverage_status: Literal["complete", "truncated", "zero_coverage", "degraded"] = (
        "complete"
    )
    coverage_warnings: list[str] = Field(default_factory=list)
    suggested_next_actions: list[dict[str, Any]] = Field(default_factory=list)
    finding_summary: FindingSummary | None = None
    interfaces: list[SweptInterface] = Field(default_factory=list)
    skipped: list[SweptSkip] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    note: str | None = None
    reason: str | None = None


class ProtocolHealthSummary(SchemaModel):
    sampling_phase: Literal["before"] = "before"
    interfaces_inspected: int
    flagged_count: int
    discovered_count: int
    truncated: bool
    coverage_status: Literal["complete", "truncated", "zero_coverage", "degraded"]
    coverage_warnings: list[str]
    suggested_next_actions: list[dict[str, Any]]
    scope: str | None = None
    start_ps: int
    end_ps: int
    edge: str
    discovery: dict[str, SignalDiscoveryCoverage]
    finding_summary: FindingSummary | None = None
    transition_truncated_count: int
    skipped_count: int


class VerifyEvidence(SchemaModel):
    time_ps: int
    cycle_index: int
    signal_values: dict[str, str | None] = Field(default_factory=dict)


class PropertyCheckResult(SchemaModel):
    mode: Literal['property'] = 'property'
    wave_path: str
    status: Literal['failure_reproduced','not_reproduced','insufficient_evidence','unsupported']
    execution_status: Literal['not_run','partial','completed'] = 'not_run'
    coverage_status: Literal['zero_coverage','partial','complete'] = 'zero_coverage'
    clock: str | None = None
    edge: str | None = None
    plan: dict[str,Any] = Field(default_factory=dict)
    target: dict[str,Any] = Field(default_factory=dict)
    failure_event: dict[str,Any] = Field(default_factory=dict)
    attempt_selection: dict[str,Any] = Field(default_factory=dict)
    sampling: dict[str,Any] = Field(default_factory=dict)
    evidence_context: dict[str,Any] = Field(default_factory=dict)
    bindings: list[dict[str,Any]] = Field(default_factory=list)
    bindings_truncated: bool = False
    attempts: list[dict[str,Any]] = Field(default_factory=list)
    attempts_truncated: bool = False
    reproduced_count: int = 0
    gap_codes: list[str] = Field(default_factory=list)
    work_steps: int = 0
    match_count: int = 0
    output_truncated: bool = False
    suggested_next_action: dict[str,Any] | None = None
    error_code: str | None = None


class WindowVerifyResult(SchemaModel):
    sample_phase: Literal["before", "after"] = "after"
    sample_offset_ps: int | None = None
    response_min_cycles: int | None = None
    response_max_cycles: int | None = None
    attempt_start_indices: list[int] | None = None
    attempts_evaluated: int = 0
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    transition_data_truncated: bool = False
    coverage_status: Literal["complete","partial","zero_coverage"] = "zero_coverage"
    wave_path: str
    clock: str
    edge: str = "posedge"
    mode: str
    start_ps: int = 0
    end_ps: int = -1
    within_cycles: int | None = None
    # implication only: True = overlapping (|->, response window [i, i+N]
    # includes the antecedent cycle); False = non-overlapping (|=>, window
    # [i+1, i+N]) for stability/hold properties. None for non-implication modes.
    overlap: bool | None = None
    signals: list[str] = Field(default_factory=list)
    holds: bool = False
    # implication only: True when a PASS was vacuous — every antecedent satisfied
    # the consequent on its OWN cycle, so within_cycles never mattered. A vacuous
    # holds proves nothing about a LATER cycle; re-run with overlap=false.
    vacuous: bool = False
    cycles_evaluated: int = 0
    unknown_cycles: int = 0
    antecedent_count: int = 0
    # beats_evaluated: sequence mode only — accepted beats where a delta was
    # actually compared (excludes first/restart/gate-false/unknown beats).
    beats_evaluated: int = 0
    violation_count: int = 0
    inconclusive_count: int = 0
    counterexample: VerifyEvidence | None = None
    witness: VerifyEvidence | None = None
    # violating_signal + next_actions: see HandshakeInspectResult. sequence mode
    # populates these on an address/stride violation (master-driven signal).
    violating_signal: str | None = None
    violating_expression: str | None = None
    next_actions: list[NextAction] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    signal_errors: dict[str, str] = Field(default_factory=dict)


class LatencyStats(SchemaModel):
    min_cycles: int
    median_cycles: int
    max_cycles: int
    mean_cycles: float


class TxnBeat(SchemaModel):
    time_ps: int
    last: bool = False
    fields: dict[str, str | None] = Field(default_factory=dict)


class TxnRecord(SchemaModel):
    id: int | None = None  # null in no-id (in-order FIFO) mode
    request_time_ps: int
    completion_time_ps: int
    latency_cycles: int
    latency_ps: int
    beat_count: int = 1
    # AxLEN+1 (None when req_len was not supplied or its value was x/z). A
    # beat_count != expected_beats is a real burst-length violation.
    expected_beats: int | None = None
    beat_count_mismatch: bool = False
    outstanding_at_start: int = 0
    data_complete: bool = True
    req_fields: dict[str, str | None] = Field(default_factory=dict)
    cmp_fields: dict[str, str | None] = Field(default_factory=dict)
    # present only when capture_beats=True (per-beat write/read data)
    data_beats: list[TxnBeat] = Field(default_factory=list)


class TxnEndpoint(SchemaModel):
    id: int | None = None  # null in no-id (in-order FIFO) mode
    request_time_ps: int | None = None
    completion_time_ps: int | None = None


class TxnAnalysis(SchemaModel):
    available_samples: int = 0
    analyzed_samples: int = 0
    last_time_ps: int | None = None
    stop_reason: str | None = None
    events_read: int = 0
    events_admitted: int = 0
    value_bytes_read: int = 0
    value_reuse_hits: int = 0
    value_reuse_misses: int = 0
    value_table_peak_entries: int = 0
    value_table_peak_bytes: int = 0
    decoded_bytes: int = 0
    read_output_bytes: int = 0
    read_pages: int = 0
    read_calls: int = 0
    legacy_reads: int = 0
    peak_pending: int = 0
    peak_early_data: int = 0
    state_peak_bytes: int = 0
    result_bytes: int = 0
    display_status: Literal["complete", "partial"] = "complete"
    display_stop_reason: str | None = None
    sample_ms: float = 0.0
    walk_ms: float = 0.0
    projection_ms: float = 0.0


class TxnReconstructResult(SchemaModel):
    sampling_phase: Literal["before"] = "before"
    coverage_status: Literal["complete", "partial", "zero_coverage"] = "zero_coverage"
    gaps: list[str] = Field(default_factory=list)
    analysis: TxnAnalysis = Field(default_factory=TxnAnalysis)
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)
    wave_path: str
    clock: str
    edge: str = "posedge"
    start_ps: int = 0
    end_ps: int = -1
    request_count: int = 0
    completion_count: int = 0
    matched_count: int = 0
    outstanding_at_end: int = 0
    max_outstanding: int = 0
    max_outstanding_time_ps: int | None = None
    max_outstanding_per_id: int = 0
    max_outstanding_id: int | None = None
    reorder_count: int = 0
    unknown_id_beats: int = 0
    unknown_control_cycles: int = 0
    reset_clears: int = 0
    unknown_history_clears: int = 0
    orphan_data_beats: int = 0
    # transactions whose observed beat_count != AxLEN+1 (needs req_len). 0 when
    # req_len was not supplied — a fact, not a clean-burst verdict in that case.
    beat_count_mismatch_count: int = 0
    timeout_cycles: int | None = None
    slow_count: int = 0
    latency: LatencyStats | None = None
    transactions: list[TxnRecord] = Field(default_factory=list)
    transactions_truncated: bool = False
    unmatched_request_count: int = 0
    unmatched_completion_count: int = 0
    unmatched_requests: list[TxnEndpoint] = Field(default_factory=list)
    unmatched_completions: list[TxnEndpoint] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    reason: str | None = None
    warnings: list[str] = Field(default_factory=list)
    signal_errors: dict[str, str] = Field(default_factory=dict)


class TlulInspectResult(SchemaModel):
    sampling_phase: Literal["before"] = "before"
    wave_path: str
    clock: str
    start_ps: int
    end_ps: int
    coverage_status: Literal["zero_coverage", "partial", "complete"]
    mapping_status: Literal["explicit", "mapping_required"]
    required_fields: list[str] = Field(default_factory=list)
    unmapped_fields: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    channels: dict[str, HandshakeInspectResult] = Field(default_factory=dict)
    transactions: TxnReconstructResult | None = None
    field_facts: dict[str, dict[str, int]] = Field(default_factory=dict)
    sample_count: int = 0
    sample_limit_reached: bool = False
    transition_data_truncated: bool = False
    unknown_fields: dict[str, int] = Field(default_factory=dict)
    unknown_control_cycles: int = 0
    unknown_reset_cycles: int = 0
    unknown_id_beats: int = 0
    reset_cycles: int = 0
    correlation_breaks: int = 0
    accepted_a_count: int = 0
    accepted_d_count: int = 0
    carry_in: str
    tail: str
    warnings: list[str] = Field(default_factory=list)
    selections: list[WaveformSelectionReceipt] = Field(default_factory=list)
    expressions: list[WaveformExpressionReceipt] = Field(default_factory=list)


class PackedFieldsResult(SchemaModel):
    native_route: NativeRouteReceipt | None = None
    status: Literal["resolved", "mapping_required"]
    fields: dict[str, WaveformSelection] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    evidence: dict[str, Any] = Field(default_factory=dict)
    backend_status: BackendStatus | None = None


class DistValueCount(SchemaModel):
    value: str
    count: int


class DistGroupSummary(SchemaModel):
    n_samples: int = 0
    distinct: int = 0
    unreadable: int = 0
    top_values: list[DistValueCount] = Field(default_factory=list)


class DistValueEnrichment(SchemaModel):
    value: str
    count_a: int
    count_b: int
    freq_a: float
    freq_b: float
    delta: float


class DistBitDiff(SchemaModel):
    bit: int
    p1_a: float | None = None
    p1_b: float | None = None
    delta: float | None = None
    x_frac_a: float = 0.0
    x_frac_b: float = 0.0


class DiffValueDistributionResult(SchemaModel):
    wave_path: str
    signal: str
    width: int = 0
    group_a: DistGroupSummary = Field(default_factory=DistGroupSummary)
    group_b: DistGroupSummary | None = None
    value_enrichment: list[DistValueEnrichment] = Field(default_factory=list)
    bit_diff: list[DistBitDiff] = Field(default_factory=list)
    discriminative_bits: list[int] = Field(default_factory=list)
    note: str | None = None


# Explicit dual-context divergence tracing; no mutable current-session defaults.
class XHistoryInput(SchemaModel):
    mode: Literal["history"] = "history"
    wave_path: str = Field(min_length=1)
    signal_path: str = Field(min_length=1)
    signal_bits: list[StrictInt] | None = Field(default=None, min_length=1, max_length=4096)
    time_ps: int | str
    compile_log: str = Field(min_length=1)
    simulator: str = "auto"
    top_hint: str | None = None
    compile_context: DivergenceContext | None = None
    history_start_ps: int | str
    phase: Literal["before", "after"] = "after"
    max_depth: int = Field(default=_config.DEFAULT_X_TRACE_MAX_DEPTH, ge=0, le=64)
    max_nodes: int = Field(default=128, ge=1, le=1024)
    max_events: int = Field(default=65536, ge=1, le=262144)
    max_read_bytes: int = Field(default=32 * 1024 * 1024, ge=1024, le=128 * 1024 * 1024)
    timeout_sec: float = Field(default=30, gt=0, le=120)


class NativeXHistoryInput(XHistoryInput):
    compile_log: str | None = Field(default=None, min_length=1)
    timeout_sec: float = Field(default=30, gt=0, le=300)


class DivergenceSide(DivergenceQueryContext):
    kdb_path: str | None = Field(default=None, min_length=1)
    backend_preference: Literal["auto", "source"] = "auto"
    retry_npi: bool = False

    @model_validator(mode="after")
    def native_identity_mode(self):
        if self.design_handle and (self.kdb_path is not None or "retry_npi" in self.model_fields_set):
            raise ValueError("design_handle_conflicts_with_native_identity")
        return self

    wave_path: str = Field(min_length=1)
    signal_path: str = Field(min_length=1)


class DivergencePair(SchemaModel):
    a: str = Field(min_length=1)
    b: str = Field(min_length=1)


class DivergenceLimits(SchemaModel):
    max_depth: int = Field(default=_config.DIVERGENCE_DEFAULT_DEPTH, ge=0, le=_config.DIVERGENCE_MAX_DEPTH)
    max_nodes: int = Field(default=_config.DIVERGENCE_DEFAULT_NODES, ge=1, le=_config.DIVERGENCE_MAX_NODES)
    max_branches: int = Field(default=_config.DIVERGENCE_DEFAULT_BRANCHES, ge=1, le=_config.DIVERGENCE_MAX_BRANCHES)
    timeout_sec: float = Field(default=_config.DIVERGENCE_DEFAULT_TIMEOUT, gt=0, le=_config.DIVERGENCE_MAX_TIMEOUT)


class DivergenceComparison(SchemaModel):
    mode: Literal["event", "clock"] = "event"
    clock_a: str | None = None
    clock_b: str | None = None
    edge: Literal["posedge", "negedge"] = "posedge"
    sample_offset_ps: int = Field(default=0, ge=0)


class TraceDivergenceInput(SchemaModel):
    side_a: DivergenceSide
    side_b: DivergenceSide
    start_time_ps: int | str = 0
    end_time_ps: int | str = -1
    reference_side: Literal["unspecified", "a", "b"] = "unspecified"
    comparison: DivergenceComparison = Field(default_factory=DivergenceComparison)
    signal_pairs: list[DivergencePair] = Field(default_factory=list, max_length=1024)
    scope_pairs: list[DivergencePair] = Field(default_factory=list, max_length=128)
    limits: DivergenceLimits = Field(default_factory=DivergenceLimits)
    cursor_name: str | None = None
    cursor_note: str | None = None


class TraceDivergenceResult(SchemaModel):
    status: Literal["complete", "partial", "blocked", "no_difference", "inconclusive"]
    reference_side: Literal["unspecified", "a", "b"] = "unspecified"
    comparison: dict[str, Any] = Field(default_factory=dict)
    contexts: dict[str, Any] = Field(default_factory=dict)
    nodes: list[dict[str, Any]] = Field(default_factory=list)
    edges: list[dict[str, Any]] = Field(default_factory=list)
    findings: list[dict[str, Any]] = Field(default_factory=list)
    frontier: list[dict[str, Any]] = Field(default_factory=list)
    coverage: dict[str, Any] = Field(default_factory=dict)
    next_actions: list[DivergenceNextAction] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    operation_metrics: dict[str, int | float] = Field(default_factory=dict)


# Version 2 hierarchy/source facts. Legacy handle schemas above remain intact.
# Trees use shallow rows plus iterative validation at the transport boundary;
# a supported depth of 256 must not depend on Pydantic's recursive-model cap.
SourceFactLabel = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_]*$")]
SourceFactCount = Annotated[int, Field(ge=0, strict=True)]
SourceFactName = Annotated[str, Field(min_length=1)]


class SourceFactModel(SchemaModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class SourceScopeRow(SourceFactModel):
    scope_id: SourceFactName
    path: SourceFactName
    inst: SourceFactName
    kind: Literal["module", "interface", "program", "generate", "instance_array"]
    parent: SourceFactName | None
    definition_id: SourceFactName | None
    module: SourceFactName | None
    source_file: SourceFactName | None
    source_line: Annotated[int, Field(ge=1)] | None
    definition_file: SourceFactName | None
    definition_line: Annotated[int, Field(ge=1)] | None
    parameterization: dict[str, str]
    gaps: list[SourceFactLabel]

    @model_validator(mode="after")
    def consistent_locations(self):
        for left, right in ((self.definition_id, self.module), (self.source_file, self.source_line),
                            (self.definition_file, self.definition_line)):
            if (left is None) != (right is None):
                raise ValueError("scope identity/location pairs must agree")
        if self.parent == self.path:
            raise ValueError("a scope cannot parent itself")
        return self


class SourceScopeNode(SourceScopeRow):
    children: list[dict[str, Any]]
    child_count: SourceFactCount | None
    child_count_lower_bound: SourceFactCount
    truncated: bool


class SourceSymbolRow(SourceFactModel):
    symbol_id: SourceFactName
    name: SourceFactName
    qualified_name: SourceFactName
    kind: Literal["module", "interface", "program", "package", "class", "typedef",
                  "function", "task", "variable", "parameter", "enum"]
    scope_id: SourceFactName | None = None
    declaration_id: SourceFactName | None = None
    type_id: SourceFactName | None = None
    source_file: SourceFactName | None
    source_line: Annotated[int, Field(ge=1)] | None
    source_column: SourceFactCount | None
    parameterization: dict[str, str] = Field(default_factory=dict)
    external: bool
    gaps: list[SourceFactLabel]
    declaration_status: Literal["declaration", "specialization", "namespace_observation"] = "declaration"

    @model_validator(mode="after")
    def consistent_location(self):
        if (self.source_file is None) != (self.source_line is None):
            raise ValueError("symbol file and line must agree")
        if self.source_file is None and self.source_column is not None:
            raise ValueError("an unknown location cannot supply a column")
        return self


class SourceClassNode(SourceSymbolRow):
    kind: Literal["class"]
    children: list[dict[str, Any]]
    truncated: bool
    reference_only: bool = False
    base_status: Literal["none", "resolved", "unresolved"] | None = None
    base_symbol_id: SourceFactName | None = None
    base_qualified_name: SourceFactName | None = None
    binding_gaps: list[SourceFactLabel] = Field(default_factory=list)
    base_relation: Literal["declaration", "specialization"] | None = None
    base_value_parameters: dict[str, str] = Field(default_factory=dict, max_length=128)
    base_parameter_status: Literal["not_observed", "observed", "partial"] = "not_observed"
    type_parameter_bindings: dict[str, SourceFactName] = Field(default_factory=dict, max_length=128)
    base_type_parameter_bindings: dict[str, SourceFactName] = Field(default_factory=dict, max_length=128)
    type_identity_domain: Literal["declaration", "artifact_canonical"] = "declaration"

    @model_validator(mode="after")
    def consistent_binding(self):
        if self.reference_only:
            if not self.external or self.base_status is not None:
                raise ValueError("a reference-only base is not a recovered declaration")
        elif self.base_status is None:
            raise ValueError("class base status must be explicit")
        if self.base_status == "resolved":
            if self.base_symbol_id is None or self.base_qualified_name is None:
                raise ValueError("a resolved base requires its semantic binding")
        elif self.base_symbol_id is not None:
            raise ValueError("an unresolved base cannot have a bound identity")
        if self.base_status == "none" and self.base_qualified_name is not None:
            raise ValueError("a class with no base cannot name one")
        if self.base_relation is not None and self.base_status != "resolved":
            raise ValueError("a declaration relation requires a resolved base")
        if (self.base_value_parameters or self.base_type_parameter_bindings) and (
            self.base_status != "resolved" or self.base_parameter_status == "not_observed"
        ):
            raise ValueError("base parameter observations require a resolved declaration")
        if self.base_parameter_status == "observed" and not (
                self.base_value_parameters or self.base_type_parameter_bindings):
            raise ValueError("observed base parameters require positive facts")
        if any(not k or not v for k, v in self.base_value_parameters.items()):
            raise ValueError("base parameter observations must be nonempty")
        if any(not key for key in (*self.type_parameter_bindings, *self.base_type_parameter_bindings)):
            raise ValueError("type parameter names must be nonempty")
        return self


class SourceFileRow(SourceFactModel):
    file_id: SourceFactName
    path: SourceFactName
    aliases: list[str]
    ownership_origin: Literal["compile_metadata"]
    compile_categories: list[str]
    compiler_file_types: list[str]
    compilation_roles: list[str]
    includes: list[str]
    include_origin: Literal["compile_log"]
    is_compilation_unit: bool
    is_reported_include: bool
    is_dependency_include: bool
    source_status: Literal["captured", "unavailable", "not_captured"]
    file_type: str | None
    modules: list[str] | None
    classes: list[str] | None
    interfaces: list[str] | None
    symbols: list[SourceSymbolRow] | None
    has_uvm_import: bool | None
    contains_uvm: bool | None
    semantic_status: Literal["not_run", "unavailable", "complete", "partial"]
    semantic_work_scope: Literal["not_run", "provider_defined", "file_declarations", "global_namespaces_bounded"]
    symbol_inventory_complete: bool
    symbol_count: SourceFactCount | None
    symbol_count_lower_bound: SourceFactCount
    semantic_gaps: list[SourceFactLabel]
    analysis_limits: list[SourceFactLabel]

    @model_validator(mode="after")
    def consistent_inventory(self):
        if self.has_uvm_import is True and self.contains_uvm is not True:
            raise ValueError("UVM import requires positive usage")
        if self.symbol_inventory_complete != (self.symbol_count is not None):
            raise ValueError("symbol count requires complete inventory")
        if self.symbol_count is not None and self.symbol_count != self.symbol_count_lower_bound:
            raise ValueError("exact symbol count must equal its lower bound")
        if self.symbols is not None and len(self.symbols) != self.symbol_count_lower_bound:
            raise ValueError("file detail must retain every admitted symbol")
        if self.analysis_limits and self.symbol_inventory_complete:
            raise ValueError("limited file inventory cannot be complete")
        if self.semantic_status == "complete" and (not self.symbol_inventory_complete or self.semantic_gaps):
            raise ValueError("complete file semantics requires complete gap-free inventory")
        if self.semantic_status in {"not_run", "unavailable"} and any(value is not None for value in (
            self.symbols, self.file_type, self.modules, self.classes, self.interfaces,
            self.has_uvm_import, self.contains_uvm,
        )):
            raise ValueError("unqueried file semantics must remain unknown")
        seen, owned_paths = set(), {self.path, *self.aliases}
        from .cancellation import check_cancelled
        for symbol in self.symbols or []:
            check_cancelled()
            if symbol.symbol_id in seen:
                raise ValueError("duplicate file symbol identity")
            seen.add(symbol.symbol_id)
            if symbol.source_file is not None and symbol.source_file not in owned_paths:
                raise ValueError("symbol belongs to another compile member")
        return self


class SourceQueryMetrics(SourceFactModel):
    scope_count: SourceFactCount
    native_call_count: SourceFactCount
    symbol_count: SourceFactCount
    file_count: SourceFactCount
    estimated_fact_bytes: SourceFactCount
    query_ms: Annotated[float, Field(ge=0)]
    native_handle_count: SourceFactCount | None = None
    native_iterator_release_count: SourceFactCount | None = None
    language_cleanup_ms: Annotated[float, Field(ge=0)] | None = None
    design_load_ms: Annotated[float, Field(ge=0)] | None = None
    query_with_cleanup_validation_ms: Annotated[float, Field(ge=0)] | None = None
    worker_context_decode_ms: Annotated[float, Field(ge=0)] | None = None
    worker_source_validation_ms: Annotated[float, Field(ge=0)] | None = None
    worker_metadata_validation_ms: Annotated[float, Field(ge=0)] | None = None
    worker_kdb_validation_ms: Annotated[float, Field(ge=0)] | None = None
    worker_result_validation_ms: Annotated[float, Field(ge=0)] | None = None
    worker_query_ms: Annotated[float, Field(ge=0)] | None = None
    context_transport_encode_ms: Annotated[float, Field(ge=0)] | None = None
    context_transport_bytes: SourceFactCount | None = None
    lsf_parent_preparation_ms: Annotated[float, Field(ge=0)] | None = None
    lsf_round_trip_ms: Annotated[float, Field(ge=0)] | None = None
    lsf_result_validation_ms: Annotated[float, Field(ge=0)] | None = None


class SourceKdbBindingEvidence(KdbArtifactEvidence):
    load_quality: Literal["clean", "degraded"]


class SourceQueryEnvelope(SourceFactModel):
    native_route: NativeRouteReceipt | None = None
    schema_version: Literal[2]
    facts_origin: Literal["compile_metadata", "verdi_npi", "slang", "static_inference"]
    artifact_identity: SourceFactName
    coverage_status: Literal["complete", "partial"]
    coverage_gaps: list[SourceFactLabel]
    analysis_limits: list[SourceFactLabel]
    query_metrics: SourceQueryMetrics
    error: SourceFactLabel | None = None
    semantic_status: Literal["not_run", "unavailable", "complete", "partial"] | None = None
    coverage_domain: SourceFactLabel | None = None
    context_identity_sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")] | None = None
    ownership_identity: SourceFactName | None = None
    ownership_basis: Literal["compile_metadata", "captured_source"] | None = None
    kdb_binding: SourceKdbBindingEvidence | None = None
    handle: str | None = None
    design_handle: str | None = None
    design_context: DesignQueryReceipt | None = None
    backend_status: BackendStatus | None = None

    @model_validator(mode="after")
    def consistent_coverage(self):
        if self.analysis_limits and self.coverage_status == "complete":
            raise ValueError("limited analysis cannot prove complete coverage")
        if self.error in {"hierarchy_query_incomplete", "semantic_provider_unavailable", "file_query_incomplete"}:
            if self.coverage_status != "partial":
                raise ValueError("unavailable/incomplete queries must remain partial")
        return self


class SourcePackedLayoutResult(SourceQueryEnvelope):
    source_signal: SourceFactName
    status: Literal["resolved", "mapping_required"]
    field_bits: dict[SourceFactName, list[SourceFactCount]]
    declared_range: dict[str, SourceFactCount] | None

    @model_validator(mode="after")
    def consistent_layout(self):
        if self.status == "mapping_required":
            if self.field_bits or self.declared_range is not None:
                raise ValueError("an unresolved layout cannot publish selections")
        else:
            bounds = self.declared_range
            if (self.coverage_status != "complete" or self.analysis_limits or self.coverage_gaps
                    or bounds is None or set(bounds) != {"left", "right"}
                    or bounds["right"] != 0 or bounds["left"] >= 256
                    or not 1 <= len(self.field_bits) <= 128):
                raise ValueError("resolved layout requires complete bounded type evidence")
            for bits in self.field_bits.values():
                if not bits or len(bits) > 256 or len(set(bits)) != len(bits) or max(bits) > bounds["left"]:
                    raise ValueError("invalid packed field selection")
        return self


class SourceCountResult(SourceQueryEnvelope):
    count_status: Literal["exact", "lower_bound"]
    count_lower_bound: SourceFactCount
    count_mode: Literal["bounded", "exact"] | None = None
    count_domain: SourceFactLabel | None = None
    output_truncated: bool


class SourceSubtreeResult(SourceCountResult):
    root: str
    node: dict[str, Any] | None
    total_descendants: SourceFactCount | None
    truncated: bool


class SourceInstanceResult(SourceCountResult):
    hits: list[SourceScopeRow]
    total: SourceFactCount | None
    truncated: bool


class SourceFileLookupResult(SourceCountResult):
    matches: list[SourceFileRow]
    total: SourceFactCount | None
    unknown_filter_files: SourceFactCount
    examined_files: SourceFactCount
    semantic_file_queries: SourceFactCount
    truncated: bool


class SourceFileDetailResult(SourceQueryEnvelope, SourceFileRow):
    semantic_status: Literal["not_run", "unavailable", "complete", "partial"]


class SourceFileMissResult(SourceQueryEnvelope):
    path: str
    did_you_mean: list[str] = Field(default_factory=list)
    error: Literal["file_not_in_compile_set", "file_query_incomplete"]


class SourceClassResult(SourceQueryEnvelope):
    view: Literal["static_class_inheritance"]
    roots: list[dict[str, Any]]
    total: SourceFactCount
    output_truncated: bool
    class_inventory_domain: Literal["provider_declarations", "declarations_and_referenced_specializations"]
    class_inventory_count: SourceFactCount | None
    class_inventory_lower_bound: SourceFactCount
    class_inventory_complete: bool
    candidates: list[dict[str, str]] = Field(default_factory=list)


class SourceSectionResult(SourceQueryEnvelope):
    section: Literal["compile_result", "include_tree", "filelist_tree", "files_full",
                     "interfaces", "component_tree_full", "class_hierarchy_full"]
    data: Any
    export_complete: bool
    output_truncated: bool
    warning: str = ""
    record_export_complete: bool | None = None
    count_status: Literal["exact", "lower_bound"] | None = None
    count_lower_bound: SourceFactCount | None = None
    count_mode: Literal["bounded", "exact"] | None = None
    count_domain: SourceFactLabel | None = None
    total: SourceFactCount | None = None
    examined_files: SourceFactCount | None = None
    root: str | None = None
    total_descendants: SourceFactCount | None = None
    truncated: bool | None = None
    view: Literal["elaborated_instances", "static_class_inheritance"] | None = None
    class_inventory_domain: Literal["provider_declarations", "declarations_and_referenced_specializations"] | None = None
    class_inventory_count: SourceFactCount | None = None
    class_inventory_lower_bound: SourceFactCount | None = None
    class_inventory_complete: bool | None = None
    candidates: list[dict[str, str]] = Field(default_factory=list)

    @model_validator(mode="after")
    def consistent_export(self):
        if self.export_complete and (self.coverage_status != "complete" or self.coverage_gaps
                                     or self.analysis_limits or self.output_truncated or self.error):
            raise ValueError("partial/truncated exports cannot be complete")
        return self
