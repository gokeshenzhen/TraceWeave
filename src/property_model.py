"""Public bounded property explanation contract (no optional frontend imports)."""
from __future__ import annotations

from typing import Any, Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator
from .property_records import gap, blocked

VERSION = "traceweave.property.v1"


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PropertyUnit(Model):
    id: str = Field(min_length=1, max_length=128)
    declarations: str = Field(default="", max_length=65536)
    default_clock: str | None = Field(default=None, max_length=1024)
    default_disable: str | None = Field(default=None, max_length=1024)


class PropertyInput(Model):
    id: str = Field(default="property-1", min_length=1, max_length=128)
    text: str | None = Field(default=None, min_length=1, max_length=65536)
    source_file: str | None = Field(default=None, min_length=1, max_length=4096)
    target: str | None = Field(default=None, min_length=1, max_length=1024)
    line: int | None = Field(default=None, ge=1)
    instance_path: str | None = Field(default=None, min_length=1, max_length=1024)
    unit: str | None = Field(default=None, min_length=1, max_length=128)
    arguments: list[str] = Field(default_factory=list, max_length=64)


class PropertyRoleContext(Model):
    source: str = Field(min_length=1, max_length=2048)
    provenance: Literal["caller_statement", "script_declaration"] = "caller_statement"
    role: Literal["assert", "assume", "cover", "disabled"]
    enabled: bool | None = None


class ExplainPropertyOptions(PropertyInput):
    include_check_plan: bool = Field(default=False,
        description="Prepare a bounded, process-scoped static plan for verify_window; does not read waveforms.")
    items: list[PropertyInput] | None = Field(default=None, min_length=1, max_length=16)
    units: list[PropertyUnit] = Field(default_factory=list, max_length=16)
    mode: Literal["auto", "simulation", "formal", "unknown"] = "auto"
    run_anchor: str | None = Field(default=None, max_length=4096,
        description="Exact discovered log, waveform, selected case or formal project; never last-tool inference.")
    role_context: PropertyRoleContext | None = None
    compile_log: str | None = Field(default=None, max_length=4096)
    design_handle: str | None = Field(default=None, max_length=256)
    simulator: Literal["auto", "vcs", "xcelium", "verilator"] = "auto"
    top_hint: str | None = Field(default=None, max_length=1024)
    timeout_sec: float = Field(default=20, gt=0, le=120, allow_inf_nan=False)
    max_nodes: int = Field(default=1024, ge=1, le=4096)
    max_depth: int = Field(default=64, ge=1, le=128)
    max_output_bytes: int = Field(default=524288, ge=4096, le=2097152)
    detail: Literal["full", "summary"] = "full"

    @model_validator(mode="after")
    def request_shape(self):
        if self.items is not None and any((self.text, self.source_file, self.target, self.instance_path, self.line)):
            raise ValueError("Use one single target or items, not both")
        if len({u.id for u in self.units}) != len(self.units):
            raise ValueError("Shared unit IDs must be unique")
        items = self.entries()
        if len({i.id for i in items}) != len(items):
            raise ValueError("Property IDs must be unique")
        if sum(len((i.text or '').encode()) for i in items) + sum(len(u.declarations.encode()) for u in self.units) > 262144:
            raise ValueError("Total property text and shared declarations exceed 256 KiB")
        if any(sum(len(a.encode()) for a in i.arguments) > 16384 for i in items):
            raise ValueError("Property arguments exceed 16 KiB")
        return self

    def entries(self):
        return self.items if self.items is not None else [PropertyInput.model_validate(
            {name: getattr(self, name) for name in PropertyInput.model_fields})]


class PropertyGap(Model):
    code: str
    category: Literal["syntax", "context", "unsupported", "limit", "frontend", "identity"]
    message: str
    source: dict[str, Any] = Field(default_factory=dict)
    recovery: str


class PropertyNode(Model):
    id: str
    domain: Literal["value", "sequence", "property", "unresolved"]
    op: str
    source: dict[str, Any]
    children: list[str] = Field(default_factory=list)
    clock: str | None = None
    facts: dict[str, Any] = Field(default_factory=dict)
    explanation: str = ""


class PropertyExplanation(Model):
    check_plan: dict[str, Any] | None = None
    id: str
    identity: dict[str, Any] = Field(default_factory=dict)
    target: dict[str, Any] = Field(default_factory=dict)
    mode: dict[str, Any] = Field(default_factory=dict)
    directive: dict[str, Any] = Field(default_factory=dict)
    syntax_status: Literal["valid", "invalid", "not_checked"] = "not_checked"
    semantic_status: Literal["complete", "partial", "not_run", "unsupported"] = "not_run"
    context_status: Literal["bound", "incomplete", "unverified", "changed"] = "unverified"
    root: str | None = None
    nodes: list[PropertyNode] = Field(default_factory=list)
    clocks: list[dict[str, Any]] = Field(default_factory=list)
    variables: list[dict[str, Any]] = Field(default_factory=list)
    effects: list[dict[str, Any]] = Field(default_factory=list)
    references: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics: list[dict[str, Any]] = Field(default_factory=list)
    diagnostics_truncated: bool = False
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    gaps: list[PropertyGap] = Field(default_factory=list)
    gap_codes: list[str] = Field(default_factory=list)
    gaps_truncated: bool = False
    summary: list[str] = Field(default_factory=list)
    analysis_limited: bool = False
    output_truncated: bool = False
    node_count: int = 0


class ExplainPropertyResult(Model):
    schema_version: Literal["traceweave.property.v1"] = VERSION
    status: Literal["complete", "partial", "not_run"]
    results: list[PropertyExplanation]
    limits: dict[str, Any]
    frontend: dict[str, Any]
    runtime_analysis: Literal["not_performed"] = "not_performed"
    output_truncated: bool = False
