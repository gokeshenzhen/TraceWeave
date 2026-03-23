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
    project: dict[str, Any] = Field(default_factory=dict)
    files: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    component_tree: dict[str, Any] = Field(default_factory=dict)
    class_hierarchy: list[str] = Field(default_factory=list)
    interfaces: list[dict[str, Any]] = Field(default_factory=list)
    compile_result: dict[str, Any] = Field(default_factory=dict)


class StructuralRisk(SchemaModel):
    type: str
    file: str
    line: int
    module: str | None = None
    risk_level: Literal["high", "medium", "low"]
    detail: str
    evidence: list[str] = Field(default_factory=list)


class ScanStructuralRisksResult(SchemaModel):
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


class ParseSimLogResult(SchemaModel):
    log_file: str
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
    detail_level: str | None = None
    detail_hint: str | None = None
    auto_downgraded: bool | None = None
    failure_events: list[dict[str, Any]] = Field(default_factory=list)
    failure_events_total: int = 0
    failure_events_returned: int = 0
    failure_events_truncated: bool = False
    previous_log_detected: bool = False
    candidate_previous_logs: list[str] = Field(default_factory=list)
    suggested_followup_tool: str | None = None
    first_group_context: ErrorContextResult | None = None
    problem_hints: ProblemHints | None = None


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


class DiffResult(SchemaModel):
    base_summary: DiffEventSummary
    new_summary: DiffEventSummary
    resolved_events: list[dict[str, Any]] = Field(default_factory=list)
    persistent_events: list[dict[str, Any]] = Field(default_factory=list)
    new_events: list[dict[str, Any]] = Field(default_factory=list)
    comparison_notes: list[str] = Field(default_factory=list)


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


class AnalyzeFailuresResult(SchemaModel):
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


class RecommendNextStepsResult(SchemaModel):
    primary_failure_target: dict[str, Any] | None = None
    recommended_signals: list[dict[str, Any]] = Field(default_factory=list)
    recommended_instances: list[dict[str, Any]] = Field(default_factory=list)
    suspected_failure_class: str
    recommendation_strategy: str | None = None
    failure_window_center_ps: int | None = None
    why: list[str] = Field(default_factory=list)


RecommendFailureDebugNextStepsResult = RecommendNextStepsResult


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


ExplainSignalDriverResult = ExplainDriverResult


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
