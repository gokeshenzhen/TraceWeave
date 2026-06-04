from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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


TOKEN_BUDGET_SOFT_LIMIT = 80_000


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


class SimPathsResult(SchemaModel):
    verif_root: str
    case_name: str | None = None
    config_source: str
    config_root: str | None = None
    discovery_mode: str
    case_dir: str | None = None
    simulator: str | None = None
    fsdb_runtime: dict[str, Any] = Field(default_factory=dict)
    compile_logs: list[FileEntry] = Field(default_factory=list)
    sim_logs: list[FileEntry] = Field(default_factory=list)
    wave_files: list[FileEntry] = Field(default_factory=list)
    available_cases: list[CaseInfo] = Field(default_factory=list)
    hints: list[str] = Field(default_factory=list)
    next_required_step: NextRequiredStep | None = None


class BuildTbHierarchyResult(SchemaModel):
    """Slim LLM-facing payload for build_tb_hierarchy.

    Full hierarchy data (files list, complete component_tree, class
    hierarchy, raw compile_result) is held server-side and accessed via
    ``hierarchy_handle`` through the handle tools (get_tb_subtree,
    lookup_tb_files, find_tb_instance, get_tb_file_detail,
    get_tb_class_hierarchy, dump_tb_section).
    """

    hierarchy_handle: str = ""
    project: dict[str, Any] = Field(default_factory=dict)
    compile_command: str = ""
    stats: dict[str, int] = Field(default_factory=dict)
    tree_skeleton: dict[str, Any] = Field(default_factory=dict)
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
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


class ScanStructuralRisksResult(TruncatableResult):
    scan_scope: str = "scope1"
    files_scanned: int = 0
    total_risks: int = 0
    risks: list[StructuralRisk] = Field(default_factory=list)
    categories_scanned: list[str] = Field(default_factory=list)
    skipped_files: list[str] = Field(default_factory=list)


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


class ParseSimLogResult(TruncatableResult):
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
    diff_source: Literal["paths", "snapshots", "mixed", "auto_previous_snapshot"] | None = None
    base_summary: DiffEventSummary
    new_summary: DiffEventSummary
    problem_hints_comparison: DiffProblemHintsComparison | None = None
    resolved_events: list[dict[str, Any]] = Field(default_factory=list)
    persistent_events: list[PersistentEventDetail] = Field(default_factory=list)
    new_events: list[dict[str, Any]] = Field(default_factory=list)
    comparison_notes: list[str] = Field(default_factory=list)
    convergence_summary: str | None = None


class WaveformSummaryResult(SchemaModel):
    file: str
    format: str
    timescale_ps: int | None = None
    simulation_duration_ps: int
    simulation_duration_ns: float
    total_signals: int
    top_modules: list[str] | None = None
    sample_signals: list[str] | None = None


class SearchSignalsResult(SchemaModel):
    keyword: str
    total_matched: int
    results: list[dict[str, Any]] = Field(default_factory=list)
    hint: str | None = None


class SignalValue(SchemaModel):
    bin: str | None = None
    hex: str | None = None
    dec: int | None = None


class SignalAtTimeResult(SchemaModel):
    signal: str
    time_ps: int
    time_ns: float
    value: dict[str, Any] | None = None


class SignalTransitionsResult(SchemaModel):
    signal: str
    start_ps: int
    end_ps: int
    transition_count: int
    transitions: list[dict[str, Any]] = Field(default_factory=list)


class SignalsAroundTimeResult(SchemaModel):
    center_time_ps: int
    center_time_ns: float
    window_ps: int
    extra_transitions: int
    signals: dict[str, Any] = Field(default_factory=dict)
    truncated: bool = False


class CycleEntry(SchemaModel):
    cycle: int
    time_ps: int
    time_ns: float
    signals: dict[str, SignalValue] = Field(default_factory=dict)


class GetSignalsByCycleResult(SchemaModel):
    clock_path: str
    edge: Literal["posedge", "negedge"]
    sample_offset_ps: int = 1
    clock_period_ps: int | None = None
    total_edges_found: int
    start_cycle: int
    num_cycles_requested: int
    effective_num_cycles: int
    num_cycles_returned: int
    capped: bool = False
    truncated: bool
    cycles: list[CycleEntry] = Field(default_factory=list)
    signal_errors: dict[str, str] = Field(default_factory=dict)


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
    failure_event: dict[str, Any]
    time_anchor: TimeAnchor
    likely_instances: list[dict[str, Any]] = Field(default_factory=list)
    recommended_signals: list[dict[str, Any]] = Field(default_factory=list)
    related_source_files: list[dict[str, Any]] = Field(default_factory=list)
    reasoning_summary: list[str] = Field(default_factory=list)


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
    primary_failure_target: dict[str, Any] | None = None
    recommended_signals: list[dict[str, Any]] = Field(default_factory=list)
    recommended_instances: list[dict[str, Any]] = Field(default_factory=list)
    correlated_structural_risks: list[StructuralRiskCorrelation] = Field(default_factory=list)
    suspected_failure_class: str
    recommendation_strategy: str | None = None
    failure_window_center_ps: int | None = None
    why: list[str] = Field(default_factory=list)
    workflow_incomplete: bool = False
    degraded_reason: Literal["missing_structural_scan"] | None = None
    required_next_call: dict[str, Any] | None = None
    missing_inputs: list[str] = Field(default_factory=list)
    next_iteration_hint: dict[str, Any] | None = None


RecommendFailureDebugNextStepsResult = RecommendNextStepsResult


class DiagnosticSnapshotSection(SchemaModel):
    available: bool
    stale: bool = False
    summary: dict[str, Any] | None = None
    suggested_call: dict[str, Any] | None = None


class DiagnosticSnapshot(SchemaModel):
    sim_paths: DiagnosticSnapshotSection
    hierarchy: DiagnosticSnapshotSection
    log_analysis: DiagnosticSnapshotSection
    structural_scan: DiagnosticSnapshotSection | None = None
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


class DriverChainHop(SchemaModel):
    depth: int
    signal_path: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    driver_kind: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_info_origin: Literal["compile_log", "npi"] | None = None
    expression_summary: str | None = None
    upstream_signals: list[str] = Field(default_factory=list)
    instance_port_connections: list[dict[str, Any]] | None = None
    branch_candidates: list[str] | None = None
    stopped_at: str | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl"] = "static"
    backend_confidence: Literal["exact", "approximate", "unverified"] = "approximate"


class ExplainDriverResult(SchemaModel):
    signal_path: str
    wave_path: str
    resolved_rtl_name: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    driver_status: str
    driver_kind: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    expression_summary: str | None = None
    upstream_signals: list[str] = Field(default_factory=list)
    instance_port_connections: list[dict[str, Any]] | None = None
    confidence: str | None = None
    unsupported_reason: str | None = None
    stopped_at: str | None = None
    recursive: bool = False
    driver_chain: list[DriverChainHop] | None = None
    chain_summary: str | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl"] = "static"
    backend_status: BackendStatus | None = None


ExplainSignalDriverResult = ExplainDriverResult


class BackendStatus(SchemaModel):
    simulator: Literal["vcs", "xcelium", "unknown"] = "unknown"
    backend: Literal["static", "verdi_npi", "verdi_tcl"] = "static"
    actual_backend: Literal["static", "verdi_npi", "verdi_tcl"] | None = None
    fallback_reason: str | None = None
    parser_match: Literal["exact", "approximate"] = "approximate"
    kdb_path: str | None = None
    kdb_flow: Literal[
        "vcs_two_step", "vcs_three_step", "vericom_standalone",
        "vericom_import_from_file", "traceweave_cached", "none",
    ] = "none"
    kdb_hint: str | None = None


class LoadHop(SchemaModel):
    load_path: str
    kind: Literal["module_input", "rhs_expr", "always_sensitivity"]
    expr: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    source_info_origin: Literal["compile_log", "npi"] | None = None
    backend: Literal["static", "verdi_npi", "verdi_tcl"] = "static"
    confidence: Literal["exact", "approximate", "unverified"] = "approximate"


class FindSignalLoadsResult(SchemaModel):
    signal_path: str
    resolved_rtl_name: str
    resolved_module: str | None = None
    resolved_instance_path: str | None = None
    loads: list[LoadHop] = Field(default_factory=list)
    completeness: Literal["exact", "approximate", "shallow_only"] = "shallow_only"
    stopped_at: str | None = None
    unsupported_reason: str | None = None
    backend_status: BackendStatus = Field(default_factory=BackendStatus)


class SignalPathHop(SchemaModel):
    index: int
    net_path: str
    scope_inst: str | None = None
    source_file: str | None = None
    source_line: int | None = None
    is_endpoint: bool = False


_TRACE_SIGNAL_PATH_DIRECTION_NOTE = (
    "Connectivity only — not a temporal driver relation. "
    "Use explain_signal_driver for driver direction."
)


class TraceSignalPathResult(SchemaModel):
    from_signal: str
    to_signal: str
    found: bool
    hops: int = 0
    path: list[SignalPathHop] = Field(default_factory=list)
    expand_assigns: bool = False
    direction_note: str = _TRACE_SIGNAL_PATH_DIRECTION_NOTE
    unsupported_reason: Literal[
        "from_not_found",
        "to_not_found",
        "not_connected",
        "static_backend_no_path_api",
        "npi_call_failed",
    ] | None = None
    backend_status: BackendStatus = Field(default_factory=BackendStatus)


class TraceChainNode(SchemaModel):
    depth: int
    signal_path: str
    value_at_time: str | None = None
    has_x: bool | None = None
    module: str | None = None
    source_file: str | None = None
    driver_kind: str | None = None
    driver_expression: str | None = None
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


class TraceXSourceResult(SchemaModel):
    start_signal: str
    start_time_ps: int
    trace_status: str
    trace_depth: int
    max_depth: int
    propagation_chain: list[TraceChainNode] = Field(default_factory=list)
    root_cause: TraceRootCause | None = None
    analysis_guide: dict[str, str] = Field(default_factory=dict)


class PrerequisiteBlockResult(SchemaModel):
    ok: bool = False
    error_code: str = "missing_prerequisite"
    missing_step: str
    required_before: str
    reason: str
    suggested_call: dict[str, Any] = Field(default_factory=dict)


class ToolErrorResult(SchemaModel):
    error: str
    error_code: str | None = None
    fsdb_runtime: dict[str, Any] | None = None
    fallback: dict[str, Any] | None = None


# ---------------------------------------------------------------------------
# Auto-debug v2: cursors + verify primitives
# See docs/auto-debug-decisions-v2.md
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


class DiffFirstDivergenceResult(SchemaModel):
    diverged: bool
    wave_path_a: str
    wave_path_b: str
    signal_a: str
    signal_b: str
    start_ps: int
    end_ps: int
    first_divergence_time_ps: int | None = None
    value_a: str | None = None
    value_b: str | None = None
    cursor: CursorRefSchema | None = None
    transitions_compared: int = 0
    missing_a: bool = False
    missing_b: bool = False
    note: str | None = None


class PeriodResult(SchemaModel):
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
    # A forward-link emitted by a bus-fact tool ONLY when it has a concrete
    # finding. Bridges a waveform fact to the next investigation (e.g. attribute a
    # violation to a driving instance). Bus-fact tools never self-attribute
    # master vs slave; attribution = bus-fact + drive-direction, composed by the
    # caller. signal_path is the signal to feed the suggested tool.
    tool: str
    reason: str
    signal_path: str | None = None


class HandshakeFinding(SchemaModel):
    type: str
    severity: str
    # long_stall fields
    begin_ps: int | None = None
    end_ps: int | None = None
    cycles: int | None = None
    # payload_hold_violation fields
    time_ps: int | None = None
    signal: str | None = None
    from_value: str | None = None
    to_value: str | None = None
    stall_begin_ps: int | None = None


class HandshakeCoverage(SchemaModel):
    # Facts about what inspect_handshake actually evaluated. These are not side
    # or protocol verdicts; discovery/caller context owns those labels.
    clock_sampled: bool = False
    valid_ready_resolved: bool = False
    stall_checked: bool = False
    backpressure_checked: bool = False
    payload_hold_requested: bool = False
    payload_hold_checked: bool = False
    payload_hold_partially_checked: bool = False
    payload_signals_requested: int = 0
    payload_signals_checked: int = 0
    payload_signals_unresolved: int = 0


class HandshakeInspectResult(SchemaModel):
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
    payload_unresolved: list[str] = Field(default_factory=list)
    coverage: HandshakeCoverage = Field(default_factory=HandshakeCoverage)
    unknown_sample_cycles: int = 0
    findings: list[HandshakeFinding] = Field(default_factory=list)
    # violating_signal: the signal carrying the primary (cursor-anchored) finding
    # — raw material for master/slave attribution, NOT a verdict. None when there
    # is no signal-specific finding. next_actions fires only when a finding
    # exists; it bridges the bus fact to RTL tracing (explain_signal_driver).
    violating_signal: str | None = None
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


class SuggestHandshakesResult(SchemaModel):
    wave_path: str
    scope: str | None = None
    candidate_count: int = 0
    candidates: list[HandshakeBundle] = Field(default_factory=list)
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
    valid: str
    ready: str
    payload: list[str] = Field(default_factory=list)
    confidence: str | None = None
    coverage: HandshakeCoverage = Field(default_factory=HandshakeCoverage)
    # flags are factual observations, never verdicts
    flags: list[str] = Field(default_factory=list)
    sample_count: int = 0
    transfer_count: int = 0
    stall_count: int = 0
    max_stall_cycles: int = 0
    max_stall_begin_ps: int | None = None
    ended_in_stall: bool = False
    final_stall_cycles: int = 0
    payload_hold_violations: int = 0
    ready_without_valid_cycles: int = 0
    unknown_sample_cycles: int = 0


class SweptSkip(SchemaModel):
    scope: str
    valid: str
    ready: str
    reason: str


class HandshakeSweepResult(SchemaModel):
    wave_path: str
    scope: str | None = None
    edge: str = "posedge"
    start_ps: int = 0
    end_ps: int = -1
    discovered_count: int = 0
    interface_count: int = 0
    flagged_count: int = 0
    truncated: bool = False
    interfaces: list[SweptInterface] = Field(default_factory=list)
    skipped: list[SweptSkip] = Field(default_factory=list)
    cursor: CursorRefSchema | None = None
    note: str | None = None
    reason: str | None = None


class VerifyEvidence(SchemaModel):
    time_ps: int
    cycle_index: int
    signal_values: dict[str, str | None] = Field(default_factory=dict)


class WindowVerifyResult(SchemaModel):
    wave_path: str
    clock: str
    edge: str = "posedge"
    mode: str
    start_ps: int = 0
    end_ps: int = -1
    within_cycles: int | None = None
    signals: list[str] = Field(default_factory=list)
    holds: bool = False
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


class TxnReconstructResult(SchemaModel):
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
    reset_clears: int = 0
    orphan_data_beats: int = 0
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
