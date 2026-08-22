"""Minimal Slang-to-Connectivity-IR projector for the Phase 1A experiment.

The module uses duck-typed pyslang objects and therefore imports no optional
native package at module import time.  It deliberately avoids the Phase 0B
broad ``root.visit`` evidence traversal.  Hierarchy is enumerated through
direct ``InstanceBody`` / generate-scope iteration, while each elaborated
definition specialization is projected exactly once.

This is an internal prototype.  It is not a connectivity backend, is not
registered with MCP, and does not alter production routing.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .connectivity_ir import (
    AssignmentFact,
    BindingSourceKind,
    BindingStyle,
    BitMapping,
    BitRange,
    BoundaryKind,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionKind,
    DefinitionTemplate,
    DependencyFact,
    DependencyRole,
    EdgeKind,
    InstanceDecl,
    ModportDecl,
    ModportMember,
    PackedMemberDecl,
    PortBinding,
    PortDecl,
    PortDirection,
    ResolutionKind,
    SignalDecl,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
    SymbolKind,
    selections_for_concat,
)


SLANG_FRONTEND_NAME = "Slang/pyslang"
SLANG_FRONTEND_VERSION = "11.0.0"
MAX_RECORDED_PROJECTION_GAPS = 256


class SlangProjectionError(RuntimeError):
    """The supplied frontend objects cannot produce a sound minimal IR."""


@dataclass(frozen=True)
class ProjectionDiagnostic:
    code: str
    severity: str
    message: str
    location: SourceLocation | None = None
    scopes: tuple[str, ...] = ()
    constructs: tuple[str, ...] = ()

    @property
    def blocking(self) -> bool:
        return self.severity.lower() in {"error", "fatal"}


@dataclass(frozen=True)
class ProjectionExclusion:
    code: str
    message: str
    impact: CoverageStatus = CoverageStatus.INCONCLUSIVE
    location: SourceLocation | None = None
    scopes: tuple[str, ...] = ()
    constructs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.impact is CoverageStatus.COMPLETE:
            raise ValueError("projection exclusion impact cannot be complete")


@dataclass(frozen=True)
class ProjectionOptions:
    source_root: Path | None = None
    files_total: int = 0
    files_projected: int = 0
    diagnostics: tuple[ProjectionDiagnostic, ...] = ()
    diagnostic_total: int | None = None
    blocking_diagnostic_total: int | None = None
    exclusions: tuple[ProjectionExclusion, ...] = ()
    focus_instance_paths: tuple[str, ...] = ()
    assignment_instance_paths: tuple[str, ...] = ()
    metadata: tuple[tuple[str, str], ...] = ()
    max_recorded_gaps: int = MAX_RECORDED_PROJECTION_GAPS

    def __post_init__(self) -> None:
        if self.files_total < 0 or self.files_projected < 0:
            raise ValueError("projection file counts must not be negative")
        if self.files_projected > self.files_total:
            raise ValueError("files_projected cannot exceed files_total")
        if self.max_recorded_gaps < 1:
            raise ValueError("max_recorded_gaps must be positive")
        diagnostic_total = (
            len(self.diagnostics)
            if self.diagnostic_total is None
            else self.diagnostic_total
        )
        blocking_total = (
            sum(item.blocking for item in self.diagnostics)
            if self.blocking_diagnostic_total is None
            else self.blocking_diagnostic_total
        )
        if diagnostic_total < len(self.diagnostics):
            raise ValueError("diagnostic_total cannot be smaller than supplied items")
        if blocking_total < sum(item.blocking for item in self.diagnostics):
            raise ValueError(
                "blocking_diagnostic_total cannot be smaller than supplied items"
            )
        if blocking_total > diagnostic_total:
            raise ValueError("blocking diagnostics cannot exceed total diagnostics")
        if self.assignment_instance_paths and not self.focus_instance_paths:
            raise ValueError(
                "assignment_instance_paths require an explicit focused projection"
            )
        if any(not path or path.strip() != path for path in self.focus_instance_paths):
            raise ValueError("focus instance paths must be non-empty and trimmed")
        if any(
            not path or path.strip() != path for path in self.assignment_instance_paths
        ):
            raise ValueError("assignment instance paths must be non-empty and trimmed")


@dataclass(frozen=True)
class ProjectionReceipt:
    hierarchy_instances_seen: int
    definition_specializations_seen: int
    assignment_facts_seen: int
    port_bindings_seen: int
    skipped_bindings: int
    skipped_assignments: int
    projection_gap_count: int
    projection_gaps_truncated: bool
    projection_scope: str
    requested_instance_count: int
    skeleton_definition_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "hierarchy_instances_seen": self.hierarchy_instances_seen,
            "definition_specializations_seen": self.definition_specializations_seen,
            "assignment_facts_seen": self.assignment_facts_seen,
            "port_bindings_seen": self.port_bindings_seen,
            "skipped_bindings": self.skipped_bindings,
            "skipped_assignments": self.skipped_assignments,
            "projection_gap_count": self.projection_gap_count,
            "projection_gaps_truncated": self.projection_gaps_truncated,
            "projection_scope": self.projection_scope,
            "requested_instance_count": self.requested_instance_count,
            "skeleton_definition_count": self.skeleton_definition_count,
        }


@dataclass(frozen=True)
class SlangProjection:
    ir: ConnectivityIR
    receipt: ProjectionReceipt


@dataclass(frozen=True)
class _InstanceRecord:
    symbol: Any
    path: str
    parent_path: str | None
    generate_scope: str | None
    definition_id: str
    parameterization: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _BindingOperand:
    """One ordered actual-expression segment before formal-bit placement."""

    width: int
    source: SignalSelection | None = None
    constant_bits: tuple[str, ...] = ()
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        if self.width < 1:
            raise ValueError("binding operand width must be positive")
        present = sum(
            (
                self.source is not None,
                bool(self.constant_bits),
                self.unresolved_reason is not None,
            )
        )
        if present != 1:
            raise ValueError("binding operand requires exactly one provenance kind")
        if self.source is not None and self.source.width != self.width:
            raise ValueError("binding operand source width must match operand width")
        if self.constant_bits:
            normalized = tuple(str(bit).lower() for bit in self.constant_bits)
            if len(normalized) != self.width:
                raise ValueError("binding operand constant width must match")
            if any(bit not in {"0", "1", "x", "z"} for bit in normalized):
                raise ValueError("binding operand constants must be four-state bits")
            object.__setattr__(self, "constant_bits", normalized)
        if self.unresolved_reason is not None and not self.unresolved_reason:
            raise ValueError("binding operand unresolved reason must not be empty")

    @classmethod
    def signal(cls, source: SignalSelection) -> _BindingOperand:
        return cls(width=source.width, source=source)

    @classmethod
    def constant(cls, bits: Sequence[str]) -> _BindingOperand:
        normalized = tuple(bits)
        return cls(width=len(normalized), constant_bits=normalized)

    @classmethod
    def unresolved(cls, width: int, reason: str) -> _BindingOperand:
        return cls(width=width, unresolved_reason=reason)


class _GapAccumulator:
    def __init__(self, limit: int):
        self._limit = limit
        self._seen: set[tuple[Any, ...]] = set()
        self._items: list[CoverageGap] = []
        self.total = 0

    def add(
        self,
        code: str,
        message: str,
        *,
        impact: CoverageStatus = CoverageStatus.INCONCLUSIVE,
        constructs: Sequence[str] = (),
        scopes: Sequence[str] = (),
        location: SourceLocation | None = None,
    ) -> None:
        key = (
            code,
            message,
            impact.value,
            tuple(constructs),
            tuple(scopes),
            location.file if location else None,
            location.line if location else None,
            location.column if location else None,
        )
        if key in self._seen:
            return
        self._seen.add(key)
        self.total += 1
        if len(self._items) >= self._limit:
            return
        self._items.append(
            CoverageGap(
                code=code,
                message=message,
                impact=impact,
                constructs=tuple(constructs),
                scopes=tuple(scopes),
                location=location,
            )
        )

    @property
    def truncated(self) -> bool:
        return self.total > len(self._items)

    def finish(self) -> tuple[CoverageGap, ...]:
        items = list(self._items)
        if self.truncated:
            items.append(
                CoverageGap(
                    code="projection_gaps_truncated",
                    message=(
                        f"recorded the first {self._limit} of {self.total} distinct "
                        "projection gaps"
                    ),
                    impact=CoverageStatus.INCONCLUSIVE,
                    constructs=("projection",),
                    scopes=("*",),
                )
            )
        return tuple(items)


class SlangConnectivityProjector:
    """Project an elaborated Slang root into the internal minimal IR."""

    def __init__(
        self,
        *,
        source_manager: Any,
        frontend_version: str = SLANG_FRONTEND_VERSION,
        options: ProjectionOptions | None = None,
    ):
        self.source_manager = source_manager
        self.frontend_version = frontend_version
        self.options = options or ProjectionOptions()
        self._gaps = _GapAccumulator(self.options.max_recorded_gaps)
        self._records: list[_InstanceRecord] = []
        self._record_by_path: dict[str, _InstanceRecord] = {}
        self._definition_representatives: dict[str, _InstanceRecord] = {}
        self._definition_name_to_ids: dict[str, set[str]] = defaultdict(set)
        self._skipped_bindings = 0
        self._skipped_assignments = 0
        self._assignment_definition_ids: set[str] = set()

    def project(self, root: Any) -> SlangProjection:
        self._seed_declared_gaps()
        self._collect_hierarchy(root)
        definitions = tuple(
            self._project_definition(record)
            for _, record in sorted(self._definition_representatives.items())
        )
        instances = tuple(self._project_instance(record) for record in self._records)
        bindings: list[PortBinding] = []
        for record in self._records:
            if record.parent_path is None:
                continue
            bindings.extend(self._project_bindings(record))

        gaps = self._gaps.finish()
        blocking_count = (
            sum(item.blocking for item in self.options.diagnostics)
            if self.options.blocking_diagnostic_total is None
            else self.options.blocking_diagnostic_total
        )
        diagnostic_count = (
            len(self.options.diagnostics)
            if self.options.diagnostic_total is None
            else self.options.diagnostic_total
        )
        if any(gap.impact is CoverageStatus.INCONCLUSIVE for gap in gaps):
            coverage_status = CoverageStatus.INCONCLUSIVE
        elif gaps or self.options.files_projected < self.options.files_total:
            coverage_status = CoverageStatus.PARTIAL
        else:
            coverage_status = CoverageStatus.COMPLETE
        if self.options.files_total == 0 and self.options.files_projected == 0:
            files_total = files_projected = _source_file_count(definitions)
        else:
            files_total = self.options.files_total
            files_projected = self.options.files_projected
        coverage = CoverageReport(
            status=coverage_status,
            files_total=files_total,
            files_projected=files_projected,
            gaps=gaps,
            diagnostic_count=diagnostic_count,
            blocking_diagnostic_count=blocking_count,
        )
        top_instances = tuple(
            record.path for record in self._records if record.parent_path is None
        )
        metadata = tuple(
            sorted(
                {
                    "projector": "minimal_slang_connectivity",
                    "projection_model": "definition_template_plus_instance_binding",
                    **dict(self.options.metadata),
                }.items()
            )
        )
        ir = ConnectivityIR(
            frontend_name=SLANG_FRONTEND_NAME,
            frontend_version=self.frontend_version,
            definitions=definitions,
            instances=instances,
            bindings=tuple(bindings),
            coverage=coverage,
            top_instances=top_instances,
            metadata=metadata,
        )
        receipt = ProjectionReceipt(
            hierarchy_instances_seen=len(instances),
            definition_specializations_seen=len(definitions),
            assignment_facts_seen=sum(
                len(definition.assignments) for definition in definitions
            ),
            port_bindings_seen=len(bindings),
            skipped_bindings=self._skipped_bindings,
            skipped_assignments=self._skipped_assignments,
            projection_gap_count=self._gaps.total,
            projection_gaps_truncated=self._gaps.truncated,
            projection_scope=(
                "focused_instances"
                if self.options.focus_instance_paths
                else "full_elaborated_hierarchy"
            ),
            requested_instance_count=len(self.options.focus_instance_paths),
            skeleton_definition_count=sum(
                definition.definition_id not in self._assignment_definition_ids
                for definition in definitions
            )
            if self.options.focus_instance_paths
            else 0,
        )
        return SlangProjection(ir=ir, receipt=receipt)

    def _seed_declared_gaps(self) -> None:
        if self.options.focus_instance_paths:
            self._gaps.add(
                code="hierarchy_projection_scoped",
                message=(
                    "projection contains the requested instance paths and their "
                    "hierarchy ancestors, not the complete elaborated design"
                ),
                impact=CoverageStatus.INCONCLUSIVE,
                constructs=("hierarchy_scope",),
                scopes=("*",),
            )
        for diagnostic in self.options.diagnostics:
            if not diagnostic.blocking:
                continue
            self._gaps.add(
                code=f"frontend_diagnostic:{diagnostic.code}",
                message=diagnostic.message,
                impact=CoverageStatus.INCONCLUSIVE,
                constructs=diagnostic.constructs or ("frontend_diagnostic",),
                scopes=diagnostic.scopes or ("*",),
                location=diagnostic.location,
            )
        for exclusion in self.options.exclusions:
            self._gaps.add(
                code=exclusion.code,
                message=exclusion.message,
                impact=exclusion.impact,
                constructs=exclusion.constructs,
                scopes=exclusion.scopes or ("*",),
                location=exclusion.location,
            )
        if self.options.files_projected < self.options.files_total:
            self._gaps.add(
                code="source_files_not_projected",
                message=(
                    f"projected {self.options.files_projected} of "
                    f"{self.options.files_total} source files"
                ),
                impact=CoverageStatus.INCONCLUSIVE,
                constructs=("compile_inputs",),
                scopes=("*",),
            )

    def _collect_hierarchy(self, root: Any) -> None:
        if self.options.focus_instance_paths:
            self._collect_focused_hierarchy(root)
            return
        stack = [
            (instance, None, None)
            for instance in reversed(tuple(getattr(root, "topInstances", ())))
        ]
        while stack:
            symbol, parent_path, generate_scope = stack.pop()
            path = str(symbol.hierarchicalPath)
            if path in self._record_by_path:
                continue
            parameters = _parameterization(symbol)
            definition_id = _definition_id(
                symbol,
                parameters,
                self.source_manager,
                self.options.source_root,
            )
            record = _InstanceRecord(
                symbol=symbol,
                path=path,
                parent_path=parent_path,
                generate_scope=generate_scope,
                definition_id=definition_id,
                parameterization=parameters,
            )
            self._records.append(record)
            self._record_by_path[path] = record
            self._definition_representatives.setdefault(definition_id, record)
            self._definition_name_to_ids[str(symbol.definition.name)].add(definition_id)
            children = list(_direct_child_instances(symbol.body, path))
            for child, child_generate_scope in reversed(children):
                stack.append((child, path, child_generate_scope))
        self._records.sort(key=lambda item: item.path)
        if not self._records:
            raise SlangProjectionError(
                "Slang root contains no elaborated top instances"
            )

    def _collect_focused_hierarchy(self, root: Any) -> None:
        selected: dict[str, Any] = {}
        unresolved_requested: set[str] = set()
        for requested in sorted(set(self.options.focus_instance_paths)):
            parts = requested.split(".")
            resolved_requested = False
            while parts:
                candidate = ".".join(parts)
                try:
                    symbol = root.lookupName(candidate)
                except Exception:
                    parts.pop()
                    continue
                if _kind_name(symbol) == "Instance":
                    selected[candidate] = symbol
                    if candidate == requested:
                        resolved_requested = True
                parts.pop()
            if not resolved_requested:
                unresolved_requested.add(requested)
                self._gaps.add(
                    code="focused_instance_not_elaborated",
                    message=(
                        "a compile-hierarchy focus candidate is absent from the "
                        "selected elaborated specialization"
                    ),
                    impact=CoverageStatus.INCONCLUSIVE,
                    constructs=("hierarchy_scope", "conditional_instance"),
                    scopes=(requested,),
                )

        if not selected:
            raise SlangProjectionError(
                "focused projection contains no elaborated instances"
            )

        selected_paths = set(selected)
        for path, symbol in sorted(selected.items()):
            parent_path = max(
                (
                    candidate
                    for candidate in selected_paths
                    if path.startswith(f"{candidate}.")
                ),
                key=len,
                default=None,
            )
            between = (
                path[len(parent_path) + 1 :].rsplit(".", 1)
                if parent_path is not None
                else [path]
            )
            generate_scope = between[0] if len(between) == 2 else None
            parameters = _parameterization(symbol)
            definition_id = _definition_id(
                symbol,
                parameters,
                self.source_manager,
                self.options.source_root,
            )
            record = _InstanceRecord(
                symbol=symbol,
                path=path,
                parent_path=parent_path,
                generate_scope=generate_scope,
                definition_id=definition_id,
                parameterization=parameters,
            )
            self._records.append(record)
            self._record_by_path[path] = record
            self._definition_representatives.setdefault(definition_id, record)
            self._definition_name_to_ids[str(symbol.definition.name)].add(definition_id)

        assignment_paths = set(
            self.options.assignment_instance_paths or self.options.focus_instance_paths
        )
        missing_assignment_paths = assignment_paths - selected_paths
        for path in sorted(missing_assignment_paths - unresolved_requested):
            self._gaps.add(
                code="focused_instance_not_elaborated",
                message=(
                    "an assignment focus candidate is absent from the selected "
                    "elaborated specialization"
                ),
                impact=CoverageStatus.INCONCLUSIVE,
                constructs=("hierarchy_scope", "conditional_instance"),
                scopes=(path,),
            )
        assignment_paths &= selected_paths
        self._assignment_definition_ids = {
            self._record_by_path[path].definition_id for path in assignment_paths
        }
        skeleton_paths = [
            record.path
            for record in self._records
            if record.definition_id not in self._assignment_definition_ids
        ]
        if skeleton_paths:
            self._gaps.add(
                code="ancestor_definition_skeleton_only",
                message=(
                    "hierarchy ancestors retain definitions, ports, and bindings but "
                    "omit local signals and assignment facts"
                ),
                impact=CoverageStatus.INCONCLUSIVE,
                constructs=("definition_skeleton",),
                scopes=tuple(skeleton_paths),
            )

    def _project_instance(self, record: _InstanceRecord) -> InstanceDecl:
        location = self._location(record.symbol.location)
        if location is None:
            location = self._location(record.symbol.definition.location)
        if location is None:
            raise SlangProjectionError(
                f"instance {record.path} and its definition have no source location"
            )
        return InstanceDecl(
            path=record.path,
            name=str(record.symbol.name),
            definition_id=record.definition_id,
            parent_path=record.parent_path,
            location=location,
            generate_scope=record.generate_scope,
            parameterization=record.parameterization,
        )

    def _project_definition(self, record: _InstanceRecord) -> DefinitionTemplate:
        instance = record.symbol
        definition = instance.definition
        definition_location = self._required_location(
            definition.location,
            f"definition {definition.name}",
        )
        definition_kind = (
            DefinitionKind.INTERFACE
            if str(definition.definitionKind.name).lower() == "interface"
            else DefinitionKind.MODULE
        )
        aliases = self._interface_aliases(instance)
        ports, port_internal_paths, packed_members = self._project_ports(record)
        if (
            self.options.focus_instance_paths
            and record.definition_id not in self._assignment_definition_ids
        ):
            return DefinitionTemplate(
                definition_id=record.definition_id,
                name=str(definition.name),
                kind=definition_kind,
                location=definition_location,
                ports=tuple(ports),
                packed_members=tuple(packed_members),
            )
        signals: list[SignalDecl] = []
        modports: list[ModportDecl] = []
        assignments: list[AssignmentFact] = []
        assignment_keys: set[tuple[Any, ...]] = set()
        for member, generate_scope in _template_members(instance.body, record.path):
            kind = _kind_name(member)
            if kind in {"Variable", "Net"}:
                absolute_path = str(member.hierarchicalPath)
                if absolute_path in port_internal_paths:
                    continue
                packed_range = _packed_range(member)
                location = self._location(member.location)
                if packed_range is None or location is None:
                    self._gaps.add(
                        code="signal_shape_unresolved",
                        message=f"cannot project packed shape for {absolute_path}",
                        constructs=(kind.lower(),),
                        scopes=(record.path,),
                        location=location,
                    )
                    continue
                relative = _relative_path(absolute_path, record.path)
                signals.append(
                    SignalDecl(
                        name=relative,
                        kind=(
                            SymbolKind.INTERFACE_FIELD
                            if definition_kind is DefinitionKind.INTERFACE
                            else (
                                SymbolKind.NET if kind == "Net" else SymbolKind.VARIABLE
                            )
                        ),
                        packed_range=packed_range,
                        location=location,
                    )
                )
                packed_members.extend(
                    self._project_packed_members(
                        member,
                        aggregate=relative,
                        aggregate_bits=packed_range.indices,
                        fallback_location=location,
                        scope=record.path,
                    )
                )
            elif kind == "Modport":
                projected = self._project_modport(member)
                if projected is not None:
                    modports.append(projected)
            elif kind == "ContinuousAssign":
                facts = self._project_continuous_assignment(
                    member,
                    record,
                    aliases,
                    generate_scope,
                )
                _extend_unique_assignments(assignments, facts, assignment_keys)
            elif kind == "ProceduralBlock":
                facts = self._project_procedural_block(
                    member,
                    record,
                    aliases,
                    generate_scope,
                )
                _extend_unique_assignments(assignments, facts, assignment_keys)
        return DefinitionTemplate(
            definition_id=record.definition_id,
            name=str(definition.name),
            kind=definition_kind,
            location=definition_location,
            ports=tuple(ports),
            signals=tuple(_dedupe_signals(signals)),
            packed_members=tuple(_dedupe_packed_members(packed_members)),
            modports=tuple(sorted(modports, key=lambda item: item.name)),
            assignments=tuple(assignments),
        )

    def _project_ports(
        self,
        record: _InstanceRecord,
    ) -> tuple[list[PortDecl], set[str], list[PackedMemberDecl]]:
        ports: list[PortDecl] = []
        internal_paths: set[str] = set()
        packed_members: list[PackedMemberDecl] = []
        for ordinal, port in enumerate(tuple(record.symbol.body.portList)):
            kind = _kind_name(port)
            location = self._location(port.location)
            if location is None:
                location = self._required_location(
                    record.symbol.definition.location,
                    f"port {record.path}.{port.name}",
                )
            if kind == "InterfacePort":
                interface_definition = self._connected_interface_definition_id(port)
                ports.append(
                    PortDecl(
                        name=str(port.name),
                        direction=PortDirection.INTERFACE,
                        packed_range=BitRange.scalar(),
                        ordinal=ordinal,
                        location=location,
                        interface_definition=interface_definition,
                        modport=str(port.modport) if port.modport else None,
                    )
                )
                continue
            if kind != "Port":
                self._gaps.add(
                    code="port_kind_unresolved",
                    message=f"unsupported port symbol kind {kind}",
                    constructs=("port",),
                    scopes=(record.path,),
                    location=location,
                )
                continue
            packed_range = _packed_range(port)
            if packed_range is None:
                self._gaps.add(
                    code="port_shape_unresolved",
                    message=f"cannot project packed shape for {record.path}.{port.name}",
                    constructs=("port",),
                    scopes=(record.path,),
                    location=location,
                )
                continue
            direction = _port_direction(port.direction)
            ports.append(
                PortDecl(
                    name=str(port.name),
                    direction=direction,
                    packed_range=packed_range,
                    ordinal=ordinal,
                    location=location,
                )
            )
            packed_members.extend(
                self._project_packed_members(
                    port,
                    aggregate=str(port.name),
                    aggregate_bits=packed_range.indices,
                    fallback_location=location,
                    scope=record.path,
                )
            )
            internal = getattr(port, "internalSymbol", None)
            if internal is not None:
                internal_paths.add(str(internal.hierarchicalPath))
        return ports, internal_paths, packed_members

    def _project_packed_members(
        self,
        symbol: Any,
        *,
        aggregate: str,
        aggregate_bits: tuple[int, ...],
        fallback_location: SourceLocation,
        scope: str,
    ) -> list[PackedMemberDecl]:
        """Flatten packed struct/union fields onto one root aggregate."""

        projected: list[PackedMemberDecl] = []

        def walk(value_type: Any, prefix: str, container_bits: tuple[int, ...]) -> None:
            canonical = getattr(value_type, "canonicalType", value_type)
            if not (
                bool(getattr(canonical, "isStruct", False))
                or bool(getattr(canonical, "isPackedUnion", False))
            ) or bool(getattr(canonical, "isUnpackedStruct", False)):
                return
            try:
                fields = tuple(canonical)
            except Exception:
                self._gaps.add(
                    code="packed_members_unresolved",
                    message=f"cannot enumerate packed members for {scope}.{prefix}",
                    impact=CoverageStatus.PARTIAL,
                    constructs=("packed_aggregate",),
                    scopes=(f"{scope}.{prefix}",),
                    location=fallback_location,
                )
                return
            for field in fields:
                if _kind_name(field) != "Field":
                    continue
                field_range = _packed_range(field)
                try:
                    offset = int(field.bitOffset)
                except Exception:
                    offset = -1
                field_bits = _packed_member_bits(
                    container_bits,
                    offset=offset,
                    width=field_range.width if field_range is not None else 0,
                )
                location = self._location(getattr(field, "location", None))
                location = location or fallback_location
                name = f"{prefix}.{field.name}"
                if field_range is None or not field_bits:
                    self._gaps.add(
                        code="packed_member_shape_unresolved",
                        message=f"cannot map packed member {scope}.{name}",
                        impact=CoverageStatus.PARTIAL,
                        constructs=("packed_aggregate",),
                        scopes=(f"{scope}.{name}",),
                        location=location,
                    )
                    continue
                projected.append(
                    PackedMemberDecl(
                        name=name,
                        aggregate=aggregate,
                        packed_range=field_range,
                        aggregate_bits=field_bits,
                        location=location,
                    )
                )
                walk(field.type, name, field_bits)

        walk(getattr(symbol, "type", None), aggregate, aggregate_bits)
        return projected

    def _connected_interface_definition_id(self, port: Any) -> str:
        connection = getattr(port, "connection", None)
        if connection and len(connection) >= 1:
            path = str(connection[0].hierarchicalPath)
            record = self._record_by_path.get(path)
            if record is not None:
                return record.definition_id
        candidates = self._definition_name_to_ids.get(
            str(port.interfaceDef.name), set()
        )
        if len(candidates) == 1:
            return next(iter(candidates))
        self._gaps.add(
            code="interface_specialization_unresolved",
            message=f"cannot identify interface specialization for {port.name}",
            constructs=("interface",),
            scopes=(str(port.hierarchicalPath),),
        )
        return str(port.interfaceDef.name)

    def _project_modport(self, symbol: Any) -> ModportDecl | None:
        location = self._location(symbol.location)
        if location is None:
            return None
        members: list[ModportMember] = []
        for member in symbol:
            if _kind_name(member) != "ModportPort":
                self._gaps.add(
                    code="modport_member_unresolved",
                    message=f"unsupported modport member kind {_kind_name(member)}",
                    constructs=("modport",),
                    scopes=(str(symbol.hierarchicalPath),),
                    location=location,
                )
                continue
            members.append(
                ModportMember(
                    name=str(member.name),
                    direction=_port_direction(member.direction),
                )
            )
        return ModportDecl(
            name=str(symbol.name), members=tuple(members), location=location
        )

    def _project_bindings(self, record: _InstanceRecord) -> list[PortBinding]:
        instance = record.symbol
        style = _binding_style(instance)
        bindings: list[PortBinding] = []
        for position, connection in enumerate(tuple(instance.portConnections)):
            port = connection.port
            if _kind_name(port) == "InterfacePort":
                projected = self._project_interface_binding(
                    record,
                    connection,
                    position,
                )
                bindings.extend(projected)
                continue
            try:
                direction = _port_direction(port.direction)
            except (AttributeError, ValueError):
                self._skip_binding(
                    record,
                    port,
                    "port_direction_unresolved",
                    "plain port direction is unavailable",
                )
                continue
            expression = connection.expression
            if _kind_name(expression) == "Assignment":
                expression = expression.left
            actuals = self._bound_expression_operands(expression, record.symbol)
            port_range = _packed_range(port)
            if not actuals or port_range is None:
                self._skip_binding(
                    record,
                    port,
                    "port_expression_unresolved",
                    "port expression is not a supported signal/slice/concat",
                )
                continue
            formal = SignalSelection(
                instance_path=record.path,
                symbol=str(port.name),
                bits=port_range.indices,
            )
            mappings = _map_concat_to_target(actuals, formal)
            if mappings is None:
                self._skip_binding(
                    record,
                    port,
                    "port_width_mapping_unresolved",
                    "actual and formal packed widths differ or require a conversion",
                )
                continue
            unresolved = tuple(
                mapping
                for mapping in mappings
                if mapping.source_kind is BindingSourceKind.UNRESOLVED
            )
            if unresolved:
                self._gaps.add(
                    code="port_segment_unresolved",
                    message=(
                        "one or more port-expression bit segments could not be "
                        "mapped to signal or constant provenance"
                    ),
                    impact=CoverageStatus.INCONCLUSIVE,
                    constructs=("port_binding", "bit_provenance"),
                    scopes=(formal.path(include_bits=True),),
                    location=self._expression_location(expression),
                )
            location = self._expression_location(expression)
            if location is None:
                location = self._required_location(
                    instance.location,
                    f"instance port binding {record.path}.{port.name}",
                )
            bindings.append(
                PortBinding(
                    binding_id=f"{record.path}:{position}:{port.name}",
                    instance_path=record.path,
                    port_name=str(port.name),
                    direction=direction,
                    style=style,
                    mappings=mappings,
                    evidence=self._evidence(
                        location,
                        f"{style.value}_port_binding",
                    ),
                    port_position=position
                    if style is BindingStyle.POSITIONAL
                    else None,
                )
            )
        return bindings

    def _project_interface_binding(
        self,
        record: _InstanceRecord,
        connection: Any,
        position: int,
    ) -> list[PortBinding]:
        port = connection.port
        interface_connection = getattr(port, "connection", None)
        if not interface_connection or len(interface_connection) < 1:
            self._skip_binding(
                record,
                port,
                "interface_connection_unresolved",
                "interface port has no elaborated connection",
            )
            return []
        modport_symbol = (
            interface_connection[1] if len(interface_connection) > 1 else None
        )
        if modport_symbol is None:
            self._skip_binding(
                record,
                port,
                "interface_modport_unresolved",
                "generic interface binding has no member direction contract",
            )
            return []
        interface_definition = self._connected_interface_definition_id(port)
        location = self._required_location(
            record.symbol.location,
            f"interface binding {record.path}.{port.name}",
        )
        bindings: list[PortBinding] = []
        for member in modport_symbol:
            if _kind_name(member) != "ModportPort":
                continue
            internal = member.internalSymbol
            packed_range = _packed_range(internal)
            if packed_range is None:
                self._skip_binding(
                    record,
                    port,
                    "interface_member_shape_unresolved",
                    f"cannot project interface member {member.name}",
                )
                continue
            actual = self._bound_symbol_selection(internal)
            if actual is None:
                self._skip_binding(
                    record,
                    port,
                    "interface_member_path_unresolved",
                    f"cannot bind interface member {member.name}",
                )
                continue
            formal = SignalSelection(
                instance_path=record.path,
                symbol=f"{port.name}.{member.name}",
                bits=packed_range.indices,
            )
            bindings.append(
                PortBinding(
                    binding_id=f"{record.path}:{position}:{port.name}.{member.name}",
                    instance_path=record.path,
                    port_name=f"{port.name}.{member.name}",
                    direction=_port_direction(member.direction),
                    style=BindingStyle.MODPORT,
                    mappings=(BitMapping(source=actual, target=formal),),
                    evidence=self._evidence(location, "modport_binding"),
                    port_position=None,
                    interface_definition=interface_definition,
                    modport=str(port.modport)
                    if port.modport
                    else str(modport_symbol.name),
                )
            )
        if not bindings:
            self._skip_binding(
                record,
                port,
                "interface_members_unresolved",
                "interface binding produced no member mappings",
            )
        return bindings

    def _project_continuous_assignment(
        self,
        symbol: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
        generate_scope: str | None,
    ) -> list[AssignmentFact]:
        assignment = symbol.assignment
        if _kind_name(assignment) != "Assignment":
            self._skip_assignment(
                record,
                "continuous_assignment_unresolved",
                "continuous assignment has no assignment expression",
                self._location(symbol.location),
            )
            return []
        return self._assignment_facts(
            assignment,
            record,
            aliases,
            kind=EdgeKind.CONTINUOUS_ASSIGN,
            boundary=BoundaryKind.COMBINATIONAL,
            procedure_kind=None,
            controls=(),
            guard=None,
            generate_scope=generate_scope,
            fallback_location=self._location(symbol.location),
        )

    def _project_procedural_block(
        self,
        block: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
        generate_scope: str | None,
    ) -> list[AssignmentFact]:
        procedure_kind = str(block.procedureKind.name)
        boundary = (
            BoundaryKind.SEQUENTIAL
            if procedure_kind == "AlwaysFF"
            else BoundaryKind.COMBINATIONAL
        )
        facts: list[AssignmentFact] = []

        def walk(
            statement: Any,
            controls: tuple[SignalSelection, ...],
            guards: tuple[str, ...],
        ) -> None:
            if statement is None:
                return
            kind = _kind_name(statement)
            if kind == "Timed":
                timing_controls = tuple(
                    self._template_expression_selections(
                        statement.timing,
                        record,
                        aliases,
                    )
                )
                walk(
                    statement.stmt, _merge_selections(controls, timing_controls), guards
                )
                return
            if kind == "Block":
                walk(statement.body, controls, guards)
                return
            if kind == "List":
                for child in statement.list:
                    walk(child, controls, guards)
                return
            if kind == "Conditional":
                condition_exprs = tuple(
                    condition.expr for condition in statement.conditions
                )
                condition_controls = tuple(
                    selection
                    for expression in condition_exprs
                    for selection in self._template_expression_selections(
                        expression,
                        record,
                        aliases,
                    )
                )
                guard_text = " && ".join(_syntax_text(item) for item in condition_exprs)
                next_controls = _merge_selections(controls, condition_controls)
                next_guards = guards + ((guard_text or "conditional"),)
                walk(statement.ifTrue, next_controls, next_guards)
                walk(statement.ifFalse, next_controls, next_guards)
                return
            if kind == "ProceduralAssign":
                runtime_facts = self._assignment_facts(
                    statement.assignment,
                    record,
                    aliases,
                    kind=EdgeKind.PROCEDURAL_ASSIGN,
                    boundary=boundary,
                    procedure_kind=procedure_kind,
                    controls=controls,
                    guard=" && ".join(guards) or None,
                    generate_scope=generate_scope,
                    fallback_location=self._location(block.location),
                )
                facts.extend(runtime_facts)
                force = bool(getattr(statement, "isForce", False))
                self._gaps.add(
                    code=(
                        "runtime_force_not_modeled"
                        if force
                        else "procedural_assign_lifetime_not_modeled"
                    ),
                    message=(
                        "runtime force semantics are not represented by static edges"
                        if force
                        else "procedural assign/deassign lifetime is not represented"
                    ),
                    impact=CoverageStatus.INCONCLUSIVE,
                    constructs=("force" if force else "procedural_assign",),
                    scopes=tuple(
                        f"{record.path}.{fact.target.symbol}" for fact in runtime_facts
                    )
                    or (record.path,),
                    location=self._location(block.location),
                )
                return
            if kind == "ProceduralDeassign":
                self._gaps.add(
                    code="runtime_release_not_modeled",
                    message="runtime release/deassign semantics are not represented",
                    impact=CoverageStatus.INCONCLUSIVE,
                    constructs=("release", "procedural_deassign"),
                    scopes=(record.path,),
                    location=self._location(block.location),
                )
                return
            if (
                kind == "ExpressionStatement"
                and _kind_name(statement.expr) == "Assignment"
            ):
                facts.extend(
                    self._assignment_facts(
                        statement.expr,
                        record,
                        aliases,
                        kind=EdgeKind.PROCEDURAL_ASSIGN,
                        boundary=boundary,
                        procedure_kind=procedure_kind,
                        controls=controls,
                        guard=" && ".join(guards) or None,
                        generate_scope=generate_scope,
                        fallback_location=self._location(block.location),
                    )
                )
                return
            assignments = _find_assignment_expressions(statement)
            if assignments:
                self._gaps.add(
                    code="procedural_control_shape_partial",
                    message=(
                        f"projected assignments inside unsupported statement kind {kind} "
                        "without complete branch-control context"
                    ),
                    impact=CoverageStatus.PARTIAL,
                    constructs=("procedural_control", kind),
                    scopes=(record.path,),
                    location=self._location(block.location),
                )
                for assignment in assignments:
                    facts.extend(
                        self._assignment_facts(
                            assignment,
                            record,
                            aliases,
                            kind=EdgeKind.PROCEDURAL_ASSIGN,
                            boundary=boundary,
                            procedure_kind=procedure_kind,
                            controls=controls,
                            guard=" && ".join(guards) or None,
                            generate_scope=generate_scope,
                            fallback_location=self._location(block.location),
                        )
                    )

        walk(block.body, (), ())
        return facts

    def _assignment_facts(
        self,
        assignment: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
        *,
        kind: EdgeKind,
        boundary: BoundaryKind,
        procedure_kind: str | None,
        controls: tuple[SignalSelection, ...],
        guard: str | None,
        generate_scope: str | None,
        fallback_location: SourceLocation | None,
    ) -> list[AssignmentFact]:
        targets = self._template_exact_operands(assignment.left, record, aliases)
        if not targets:
            self._skip_assignment(
                record,
                "assignment_target_unresolved",
                "assignment lvalue is not a supported signal/slice/concat",
                fallback_location,
            )
            return []
        rhs_exact = self._template_exact_operands(assignment.right, record, aliases)
        rhs_reads = tuple(
            self._template_expression_selections(assignment.right, record, aliases)
        )
        location = self._expression_location(assignment) or fallback_location
        if location is None:
            self._skip_assignment(
                record,
                "assignment_location_unresolved",
                "assignment has no source location",
                None,
            )
            return []
        self._mark_expression_limitations(
            assignment.right,
            record,
            targets,
            location,
        )
        facts: list[AssignmentFact] = []
        if len(targets) > 1:
            self._gaps.add(
                code="concat_lvalue_partial",
                message="concat lvalue projected as independent write facts",
                impact=CoverageStatus.PARTIAL,
                constructs=("concat_lvalue",),
                scopes=(record.path,),
                location=location,
            )
        for target_index, target in enumerate(targets):
            dependencies: tuple[DependencyFact, ...]
            if len(targets) == 1 and rhs_exact:
                exact = _dependencies_for_exact_rhs(target, rhs_exact)
                dependencies = exact if exact is not None else ()
            else:
                dependencies = ()
            if not dependencies:
                dependencies = tuple(
                    DependencyFact(
                        source=source,
                        target=target,
                        role=DependencyRole.DATA,
                        exact_bit_mapping=False,
                        guard=guard,
                    )
                    for source in rhs_reads
                )
            control_dependencies = tuple(
                DependencyFact(
                    source=control,
                    target=target,
                    role=DependencyRole.CONTROL,
                    exact_bit_mapping=False,
                    guard=guard,
                )
                for control in controls
                if control.symbol != target.symbol or control.bits != target.bits
            )
            all_dependencies = _dedupe_dependencies(
                (*dependencies, *control_dependencies)
            )
            assignment_id = _assignment_id(
                record.definition_id,
                kind,
                location,
                target,
                target_index,
            )
            facts.append(
                AssignmentFact(
                    assignment_id=assignment_id,
                    kind=kind,
                    target=target,
                    dependencies=all_dependencies,
                    boundary=boundary,
                    evidence=self._evidence(
                        location,
                        procedure_kind or "continuous_assign",
                        conditional=bool(guard),
                    ),
                    procedure_kind=procedure_kind,
                    guard=guard,
                    generate_scope=generate_scope,
                )
            )
        return facts

    def _mark_expression_limitations(
        self,
        expression: Any,
        record: _InstanceRecord,
        targets: Sequence[SignalSelection],
        location: SourceLocation,
    ) -> None:
        limitations: set[tuple[str, str, CoverageStatus, tuple[str, ...]]] = set()

        def inspect(node: Any) -> None:
            if _kind_name(node) != "Call":
                return
            name = str(getattr(node, "subroutineName", "<unknown>"))
            if bool(getattr(node, "isSystemCall", False)):
                if _is_opaque_runtime_system_call(node):
                    limitations.add(
                        (
                            "runtime_system_call_not_modeled",
                            f"runtime system call {name} is not statically evaluated",
                            CoverageStatus.INCONCLUSIVE,
                            ("runtime_system_call",),
                        )
                    )
                return
            subroutine = getattr(node, "subroutine", None)
            flags = str(getattr(subroutine, "flags", ""))
            is_uvm_call = _is_uvm_dynamic_call(node)
            if is_uvm_call:
                limitations.add(
                    (
                        "uvm_dynamic_call_not_modeled",
                        f"UVM call {name} has no projected dynamic implementation",
                        CoverageStatus.INCONCLUSIVE,
                        ("uvm_dynamic_call",),
                    )
                )
            if "DPIImport" in flags:
                limitations.add(
                    (
                        "dpi_runtime_not_modeled",
                        f"DPI call {name} has no projected native implementation",
                        CoverageStatus.INCONCLUSIVE,
                        ("dpi",),
                    )
                )
            elif not is_uvm_call:
                limitations.add(
                    (
                        "subroutine_body_not_projected",
                        f"subroutine body dependencies for {name} are not projected",
                        CoverageStatus.PARTIAL,
                        ("subroutine_call",),
                    )
                )

        try:
            expression.visit(inspect)
        except Exception:
            return
        scopes = tuple(f"{record.path}.{target.symbol}" for target in targets)
        for code, message, impact, constructs in sorted(
            limitations,
            key=lambda item: (item[0], item[1]),
        ):
            self._gaps.add(
                code=code,
                message=message,
                impact=impact,
                constructs=constructs,
                scopes=scopes,
                location=location,
            )

    def _template_exact_operands(
        self,
        expression: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
    ) -> tuple[SignalSelection, ...]:
        expression = _unwrap_expression(expression)
        kind = _kind_name(expression)
        if kind == "Concatenation":
            result: list[SignalSelection] = []
            for operand in expression.operands:
                nested = self._template_exact_operands(operand, record, aliases)
                if not nested:
                    return ()
                result.extend(nested)
            return tuple(result)
        selection = self._template_selection(expression, record, aliases)
        return (selection,) if selection is not None else ()

    def _template_expression_selections(
        self,
        expression: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
    ) -> tuple[SignalSelection, ...]:
        direct = self._template_exact_operands(expression, record, aliases)
        if direct:
            return _dedupe_selections(direct)
        candidates: list[SignalSelection] = []

        def collect(node: Any) -> Any:
            if _kind_name(node) == "Call" and _is_opaque_dependency_call(node):
                # Keep the assignment as terminal driver evidence, but do not
                # turn arguments to an opaque runtime/DPI/UVM call into
                # structural data edges to the call's return value.
                from pyslang import ast as slang_ast

                return slang_ast.VisitAction.Skip
            if _kind_name(node) not in {
                "NamedValue",
                "HierarchicalValue",
                "ArbitrarySymbol",
                "MemberAccess",
                "RangeSelect",
                "ElementSelect",
            }:
                return
            selection = self._template_selection(node, record, aliases)
            if selection is not None:
                candidates.append(selection)
            return None

        try:
            expression.visit(collect)
        except Exception:
            return ()
        return _prefer_specific_selections(candidates)

    def _template_selection(
        self,
        expression: Any,
        record: _InstanceRecord,
        aliases: Mapping[str, str],
    ) -> SignalSelection | None:
        expression = _unwrap_expression(expression)
        kind = _kind_name(expression)
        if kind == "MemberAccess":
            base = self._template_selection(expression.value, record, aliases)
            if base is None:
                return None
            selected_bits = _packed_member_bits(
                base.bits,
                offset=int(getattr(expression.member, "bitOffset", -1)),
                width=_expression_width(expression) or 0,
            )
            if not selected_bits:
                return None
            return SignalSelection(symbol=base.symbol, bits=selected_bits)
        if kind in {"RangeSelect", "ElementSelect"}:
            selected_value = _unwrap_expression(expression.value)
            if _kind_name(selected_value) == "MemberAccess":
                base = self._template_selection(
                    selected_value.value,
                    record,
                    aliases,
                )
                if base is None:
                    return None
                selected_bits = _selected_packed_member_bits(
                    expression,
                    selected_value.member,
                    base.bits,
                )
                if not selected_bits:
                    return None
                return SignalSelection(symbol=base.symbol, bits=selected_bits)
            base = self._template_selection(selected_value, record, aliases)
            if base is None:
                return None
            selected_bits = _selected_bits(expression, base.bits)
            if not selected_bits:
                return None
            return SignalSelection(symbol=base.symbol, bits=selected_bits)
        symbol = _expression_symbol(expression)
        if symbol is None:
            return None
        symbol = _underlying_symbol(symbol)
        if _is_elaboration_constant_symbol(symbol):
            return None
        absolute_path = str(getattr(symbol, "hierarchicalPath", ""))
        if not absolute_path:
            return None
        relative: str | None = None
        if absolute_path.startswith(f"{record.path}."):
            relative = absolute_path[len(record.path) + 1 :]
        else:
            for actual_path, formal_name in aliases.items():
                if absolute_path.startswith(f"{actual_path}."):
                    relative = f"{formal_name}.{absolute_path[len(actual_path) + 1 :]}"
                    break
        if relative is None:
            return None
        packed_range = _packed_range(symbol)
        if packed_range is None:
            return None
        return SignalSelection(symbol=relative, bits=packed_range.indices)

    def _bound_expression_operands(
        self,
        expression: Any,
        context_symbol: Any | None = None,
    ) -> tuple[_BindingOperand, ...]:
        """Return width-preserving, ordered actual-expression provenance.

        Unsupported dynamic expressions reserve only their own target bits.
        This is intentionally different from assignment dependency extraction:
        constants are terminal port-driver facts here, but never waveform
        signals or runtime assignment dependencies.
        """

        kind = _kind_name(expression)
        width = _expression_width(expression)
        constant_bits = _constant_expression_bits(expression, context_symbol)
        if constant_bits is not None:
            return (_BindingOperand.constant(constant_bits),)

        if kind == "Concatenation":
            result: list[_BindingOperand] = []
            for operand in expression.operands:
                nested = self._bound_expression_operands(operand, context_symbol)
                if not nested:
                    operand_width = _expression_width(operand)
                    if operand_width is None:
                        return ()
                    nested = (
                        _BindingOperand.unresolved(
                            operand_width,
                            "port_operand_width_known_source_unresolved",
                        ),
                    )
                result.extend(nested)
            return tuple(result)

        if kind == "Replication":
            count = _constant_int(expression.count, context_symbol)
            repeated = self._bound_expression_operands(
                expression.concat,
                context_symbol,
            )
            if count is None or count < 1 or not repeated:
                return (
                    (_BindingOperand.unresolved(width, "replication_unresolved"),)
                    if width is not None
                    else ()
                )
            return tuple(item for _ in range(count) for item in repeated)

        if kind == "Conversion":
            nested = self._bound_expression_operands(
                expression.operand,
                context_symbol,
            )
            if width is None or not nested:
                return (
                    (_BindingOperand.unresolved(width, "conversion_unresolved"),)
                    if width is not None
                    else ()
                )
            signed = bool(getattr(expression.operand.type, "isSigned", False))
            return _resize_binding_operands(nested, width, signed=signed)

        if kind in {"RangeSelect", "ElementSelect"}:
            base_expression = expression.value
            selected_value = _unwrap_expression(base_expression)
            if _kind_name(selected_value) == "MemberAccess":
                base = self._bound_expression_operands(
                    selected_value.value,
                    context_symbol,
                )
                if len(base) != 1 or base[0].source is None:
                    return (
                        (
                            _BindingOperand.unresolved(
                                width,
                                "selected_member_source_unresolved",
                            ),
                        )
                        if width is not None
                        else ()
                    )
                bits = _selected_packed_member_bits(
                    expression,
                    selected_value.member,
                    base[0].source.bits,
                    context_symbol,
                )
                if not bits:
                    return (
                        (
                            _BindingOperand.unresolved(
                                width,
                                "selected_member_mapping_unresolved",
                            ),
                        )
                        if width is not None
                        else ()
                    )
                return (
                    _BindingOperand.signal(
                        SignalSelection(
                            instance_path=base[0].source.instance_path,
                            symbol=base[0].source.symbol,
                            bits=bits,
                        )
                    ),
                )
            base = self._bound_expression_operands(base_expression, context_symbol)
            if len(base) != 1 or base[0].source is None:
                return (
                    (_BindingOperand.unresolved(width, "selected_source_unresolved"),)
                    if width is not None
                    else ()
                )
            bits = _selected_bits(expression, base[0].source.bits, context_symbol)
            if not bits:
                return (
                    (_BindingOperand.unresolved(width, "selection_unresolved"),)
                    if width is not None
                    else ()
                )
            return (
                _BindingOperand.signal(
                    SignalSelection(
                        instance_path=base[0].source.instance_path,
                        symbol=base[0].source.symbol,
                        bits=bits,
                    )
                ),
            )

        if kind == "MemberAccess":
            base = self._bound_expression_operands(
                expression.value,
                context_symbol,
            )
            if len(base) != 1 or base[0].source is None:
                return (
                    (_BindingOperand.unresolved(width, "member_source_unresolved"),)
                    if width is not None
                    else ()
                )
            bits = _packed_member_bits(
                base[0].source.bits,
                offset=int(getattr(expression.member, "bitOffset", -1)),
                width=width or 0,
            )
            if not bits:
                return (
                    (_BindingOperand.unresolved(width, "member_selection_unresolved"),)
                    if width is not None
                    else ()
                )
            return (
                _BindingOperand.signal(
                    SignalSelection(
                        instance_path=base[0].source.instance_path,
                        symbol=base[0].source.symbol,
                        bits=bits,
                    )
                ),
            )

        symbol = _expression_symbol(expression)
        if symbol is not None:
            selection = self._bound_symbol_selection(_underlying_symbol(symbol))
            if selection is not None:
                return (_BindingOperand.signal(selection),)
        if width is None:
            return ()
        return (_BindingOperand.unresolved(width, "port_expression_dynamic"),)

    def _bound_symbol_selection(self, symbol: Any) -> SignalSelection | None:
        symbol = _underlying_symbol(symbol)
        if _is_elaboration_constant_symbol(symbol):
            return None
        absolute_path = str(getattr(symbol, "hierarchicalPath", ""))
        packed_range = _packed_range(symbol)
        if not absolute_path or packed_range is None:
            return None
        instance_path = next(
            (
                path
                for path in sorted(self._record_by_path, key=len, reverse=True)
                if absolute_path.startswith(f"{path}.")
            ),
            None,
        )
        if instance_path is None:
            return None
        return SignalSelection(
            instance_path=instance_path,
            symbol=absolute_path[len(instance_path) + 1 :],
            bits=packed_range.indices,
        )

    def _interface_aliases(self, instance: Any) -> dict[str, str]:
        aliases: dict[str, str] = {}
        for port in tuple(instance.body.portList):
            if _kind_name(port) != "InterfacePort":
                continue
            connection = getattr(port, "connection", None)
            if connection and len(connection) >= 1:
                aliases[str(connection[0].hierarchicalPath)] = str(port.name)
        return aliases

    def _skip_binding(
        self,
        record: _InstanceRecord,
        port: Any,
        code: str,
        message: str,
    ) -> None:
        self._skipped_bindings += 1
        self._gaps.add(
            code=code,
            message=f"{record.path}.{getattr(port, 'name', '?')}: {message}",
            constructs=("port_binding",),
            scopes=(record.path,),
            location=self._location(getattr(port, "location", None)),
        )

    def _skip_assignment(
        self,
        record: _InstanceRecord,
        code: str,
        message: str,
        location: SourceLocation | None,
    ) -> None:
        self._skipped_assignments += 1
        self._gaps.add(
            code=code,
            message=f"{record.path}: {message}",
            constructs=("assignment",),
            scopes=(record.path,),
            location=location,
        )

    def _evidence(
        self,
        location: SourceLocation,
        construct: str,
        *,
        conditional: bool = False,
    ) -> SourceEvidence:
        return SourceEvidence(
            construct=construct,
            location=location,
            resolution=(
                ResolutionKind.CONDITIONAL
                if conditional
                else ResolutionKind.EXACT_SOURCE
            ),
            frontend=SLANG_FRONTEND_NAME,
            frontend_version=self.frontend_version,
        )

    def _location(self, location: Any) -> SourceLocation | None:
        if location is None:
            return None
        try:
            file_name = str(self.source_manager.getFileName(location))
            line = int(self.source_manager.getLineNumber(location))
            column = int(self.source_manager.getColumnNumber(location))
        except Exception:
            return None
        if not file_name or line < 1:
            return None
        normalized = normalize_source_path(file_name, self.options.source_root)
        return SourceLocation(file=normalized, line=line, column=max(column, 0))

    def _expression_location(self, expression: Any) -> SourceLocation | None:
        try:
            return self._location(expression.sourceRange.start)
        except Exception:
            return None

    def _required_location(self, location: Any, label: str) -> SourceLocation:
        projected = self._location(location)
        if projected is None:
            raise SlangProjectionError(f"{label} has no readable source location")
        return projected


def project_slang_design(
    *,
    root: Any,
    source_manager: Any,
    frontend_version: str = SLANG_FRONTEND_VERSION,
    options: ProjectionOptions | None = None,
) -> SlangProjection:
    """Convenience entry point that never imports pyslang itself."""

    return SlangConnectivityProjector(
        source_manager=source_manager,
        frontend_version=frontend_version,
        options=options,
    ).project(root)


def normalize_source_path(file_name: str, source_root: Path | None) -> str:
    path = Path(file_name)
    if source_root is None:
        return path.as_posix()
    try:
        resolved = path.resolve()
        root = source_root.resolve()
    except OSError:
        return path.as_posix()
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        # Frontends commonly report sources outside the worker cwd as a
        # relative ``../../...`` path even when the compile manifest used an
        # absolute input.  Preserve in-root paths as portable IR-relative
        # names, but make an out-of-root location unambiguous for public
        # driver/load/path results.
        return resolved.as_posix()


def _source_file_count(definitions: Sequence[DefinitionTemplate]) -> int:
    return len({definition.location.file for definition in definitions})


def _kind_name(value: Any) -> str:
    kind = getattr(value, "kind", None)
    return str(getattr(kind, "name", kind or ""))


def _is_opaque_runtime_system_call(call: Any) -> bool:
    if not bool(getattr(call, "isSystemCall", False)):
        return False
    name = str(getattr(call, "subroutineName", ""))
    return name in {
        "$random",
        "$urandom",
        "$time",
        "$realtime",
        "$test$plusargs",
        "$value$plusargs",
    } or name.lower().startswith("$fsdb")


def _is_uvm_dynamic_call(call: Any) -> bool:
    """Recognize calls whose implementation belongs to the UVM runtime."""

    name = str(getattr(call, "subroutineName", ""))
    subroutine = getattr(call, "subroutine", None)
    path = str(getattr(subroutine, "hierarchicalPath", ""))
    return (
        path.startswith("uvm_pkg::")
        or path.startswith("uvm_pkg.")
        or name.lower().startswith("uvm_hdl_")
    )


def _is_opaque_dependency_call(call: Any) -> bool:
    if _is_opaque_runtime_system_call(call) or _is_uvm_dynamic_call(call):
        return True
    subroutine = getattr(call, "subroutine", None)
    return "DPIImport" in str(getattr(subroutine, "flags", ""))


def _parameterization(instance: Any) -> tuple[tuple[str, str], ...]:
    result = []
    for parameter in tuple(getattr(instance.body, "parameters", ())):
        if bool(getattr(parameter, "isValue", False)):
            value = getattr(parameter, "value", "<unresolved-value>")
        else:
            declared = getattr(parameter, "targetType", None)
            value = getattr(declared, "type", "<unresolved-type>")
        result.append((str(parameter.name), str(value)))
    return tuple(result)


def _definition_id(
    instance: Any,
    parameters: tuple[tuple[str, str], ...],
    source_manager: Any,
    source_root: Path | None,
) -> str:
    definition = instance.definition
    try:
        file_name = normalize_source_path(
            str(source_manager.getFileName(definition.location)),
            source_root,
        )
        line = int(source_manager.getLineNumber(definition.location))
    except Exception:
        file_name = ""
        line = 0
    payload = json.dumps(
        {
            "name": str(definition.name),
            "kind": str(definition.definitionKind.name),
            "file": file_name,
            "line": line,
            "parameters": parameters,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    suffix = hashlib.sha256(payload).hexdigest()[:12]
    return f"{definition.name}@{suffix}"


def _direct_child_instances(
    scope: Any,
    parent_instance_path: str,
) -> Iterable[tuple[Any, str | None]]:
    for member in scope:
        kind = _kind_name(member)
        if kind == "Instance":
            path = str(member.hierarchicalPath)
            between = path[len(parent_instance_path) + 1 :].rsplit(".", 1)
            generate_scope = between[0] if len(between) == 2 else None
            yield member, generate_scope
        elif kind in {"GenerateBlock", "GenerateBlockArray", "InstanceArray"}:
            yield from _direct_child_instances(member, parent_instance_path)


def _template_members(
    scope: Any,
    instance_path: str,
) -> Iterable[tuple[Any, str | None]]:
    for member in scope:
        kind = _kind_name(member)
        if kind in {"Instance", "InstanceArray"}:
            continue
        if kind in {"GenerateBlock", "GenerateBlockArray"}:
            scope_path = str(member.hierarchicalPath)
            relative = _relative_path(scope_path, instance_path)
            yield from (
                (child, child_scope or relative)
                for child, child_scope in _template_members(member, instance_path)
            )
            continue
        absolute = str(getattr(member, "hierarchicalPath", ""))
        generate_scope = None
        if absolute.startswith(f"{instance_path}.") and "." in _relative_path(
            absolute, instance_path
        ):
            generate_scope = _relative_path(absolute, instance_path).rsplit(".", 1)[0]
        yield member, generate_scope


def _relative_path(path: str, instance_path: str) -> str:
    prefix = f"{instance_path}."
    return path[len(prefix) :] if path.startswith(prefix) else path


def _packed_range(symbol: Any) -> BitRange | None:
    value_type = getattr(symbol, "type", None)
    if value_type is None:
        internal = getattr(symbol, "internalSymbol", None)
        value_type = getattr(internal, "type", None)
    if value_type is None:
        return None
    try:
        fixed = value_type.getBitVectorRange()
        width = int(fixed.width)
        if width < 1:
            return None
        return BitRange(int(fixed.left), int(fixed.right))
    except Exception:
        try:
            width = int(value_type.bitWidth)
        except Exception:
            return None
        return BitRange.from_width(width) if width > 0 else None


def _packed_member_bits(
    container_bits: tuple[int, ...],
    *,
    offset: int,
    width: int,
) -> tuple[int, ...]:
    """Map a Slang packed-field LSB offset into source-order root bits."""

    if offset < 0 or width < 1 or offset + width > len(container_bits):
        return ()
    start = len(container_bits) - offset - width
    stop = len(container_bits) - offset
    return container_bits[start:stop]


def _port_direction(direction: Any) -> PortDirection:
    name = str(getattr(direction, "name", direction)).lower()
    return {
        "in": PortDirection.INPUT,
        "input": PortDirection.INPUT,
        "out": PortDirection.OUTPUT,
        "output": PortDirection.OUTPUT,
        "inout": PortDirection.INOUT,
        "ref": PortDirection.INOUT,
    }[name]


def _binding_style(instance: Any) -> BindingStyle:
    syntax = getattr(instance, "syntax", None)
    connections = tuple(getattr(syntax, "connections", ())) if syntax else ()
    if any(
        _kind_name(item) in {"NamedPortConnection", "WildcardPortConnection"}
        for item in connections
    ):
        return BindingStyle.NAMED
    return BindingStyle.POSITIONAL


def _map_concat_to_target(
    sources: Sequence[SignalSelection | _BindingOperand],
    target: SignalSelection,
) -> tuple[BitMapping, ...] | None:
    operands = tuple(
        item if isinstance(item, _BindingOperand) else _BindingOperand.signal(item)
        for item in sources
    )
    if sum(item.width for item in operands) != target.width:
        return None
    mappings: list[BitMapping] = []
    offset = 0
    for operand in operands:
        target_part = SignalSelection(
            instance_path=target.instance_path,
            symbol=target.symbol,
            bits=target.bits[offset : offset + operand.width],
        )
        if operand.source is not None:
            mapping = BitMapping(source=operand.source, target=target_part)
        elif operand.constant_bits:
            mapping = BitMapping(
                source=None,
                target=target_part,
                source_kind=BindingSourceKind.CONSTANT,
                constant_bits=operand.constant_bits,
            )
        else:
            mapping = BitMapping(
                source=None,
                target=target_part,
                source_kind=BindingSourceKind.UNRESOLVED,
                unresolved_reason=operand.unresolved_reason,
            )
        mappings.append(mapping)
        offset += operand.width
    return tuple(mappings)


def _slice_binding_operand(
    operand: _BindingOperand,
    start: int,
    stop: int,
) -> _BindingOperand:
    width = stop - start
    if width < 1 or start < 0 or stop > operand.width:
        raise ValueError("invalid binding operand slice")
    if operand.source is not None:
        return _BindingOperand.signal(
            SignalSelection(
                instance_path=operand.source.instance_path,
                symbol=operand.source.symbol,
                bits=operand.source.bits[start:stop],
            )
        )
    if operand.constant_bits:
        return _BindingOperand.constant(operand.constant_bits[start:stop])
    return _BindingOperand.unresolved(width, operand.unresolved_reason or "unresolved")


def _trim_binding_operands_msb(
    operands: Sequence[_BindingOperand],
    trim: int,
) -> tuple[_BindingOperand, ...]:
    remaining = trim
    result: list[_BindingOperand] = []
    for operand in operands:
        if remaining >= operand.width:
            remaining -= operand.width
            continue
        if remaining:
            result.append(_slice_binding_operand(operand, remaining, operand.width))
            remaining = 0
        else:
            result.append(operand)
    if remaining:
        raise ValueError("binding operand trim exceeds total width")
    return tuple(result)


def _sign_extension_operands(
    operand: _BindingOperand,
    width: int,
) -> tuple[_BindingOperand, ...]:
    if operand.source is not None:
        sign = _slice_binding_operand(operand, 0, 1)
        # Keep each alias separate because SignalSelection intentionally rejects
        # repeated bit indices inside one exact mapping.
        return tuple(sign for _ in range(width))
    if operand.constant_bits:
        return (_BindingOperand.constant((operand.constant_bits[0],) * width),)
    return (
        _BindingOperand.unresolved(
            width,
            operand.unresolved_reason or "sign_extension_source_unresolved",
        ),
    )


def _resize_binding_operands(
    operands: Sequence[_BindingOperand],
    width: int,
    *,
    signed: bool,
) -> tuple[_BindingOperand, ...]:
    current = sum(item.width for item in operands)
    if current == width:
        return tuple(operands)
    if current > width:
        return _trim_binding_operands_msb(operands, current - width)
    extension = width - current
    if signed:
        prefix = _sign_extension_operands(operands[0], extension)
    else:
        prefix = (_BindingOperand.constant(("0",) * extension),)
    return (*prefix, *operands)


def _underlying_symbol(symbol: Any) -> Any:
    kind = _kind_name(symbol)
    if kind in {"ModportPort", "Port"}:
        internal = getattr(symbol, "internalSymbol", None)
        if internal is not None:
            return internal
    return symbol


_ELABORATION_CONSTANT_SYMBOL_KINDS = frozenset(
    {"EnumValue", "Genvar", "Parameter", "Specparam", "TypeParameter"}
)


def _is_elaboration_constant_symbol(symbol: Any) -> bool:
    """Whether a Slang symbol is fixed during elaboration, not wave-sampleable."""

    return _kind_name(symbol) in _ELABORATION_CONSTANT_SYMBOL_KINDS


def _expression_symbol(expression: Any) -> Any | None:
    if _kind_name(expression) in {
        "NamedValue",
        "HierarchicalValue",
        "ArbitrarySymbol",
    }:
        return getattr(expression, "symbol", None)
    return None


def _unwrap_expression(expression: Any) -> Any:
    current = expression
    while _kind_name(current) == "Conversion":
        current = current.operand
    return current


def _expression_width(expression: Any) -> int | None:
    value_type = getattr(expression, "type", None)
    if value_type is None:
        return None
    try:
        width = int(value_type.bitWidth)
    except Exception:
        try:
            width = int(value_type.getBitVectorRange().width)
        except Exception:
            return None
    return width if width > 0 else None


def _constant_value(expression: Any, context_symbol: Any | None) -> Any | None:
    for candidate in (
        getattr(expression, "constant", None),
        getattr(expression, "value", None),
        getattr(getattr(expression, "symbol", None), "value", None),
    ):
        if candidate is not None and (
            getattr(candidate, "value", None) is not None
            or getattr(candidate, "bitWidth", None) is not None
        ):
            return candidate
    if context_symbol is None or not hasattr(expression, "eval"):
        return None
    try:
        # Keep pyslang optional at module import time.  This path runs only in
        # the isolated frontend worker (or an explicit frontend regression).
        from pyslang import ast as slang_ast

        evaluated = expression.eval(slang_ast.EvalContext(context_symbol))
    except Exception:
        return None
    return evaluated if getattr(evaluated, "value", None) is not None else None


def _constant_svint(expression: Any, context_symbol: Any | None) -> Any | None:
    value = _constant_value(expression, context_symbol)
    if value is None:
        return None
    nested = getattr(value, "value", None)
    candidate = nested if nested is not None else value
    if getattr(candidate, "bitWidth", None) is None or not hasattr(
        candidate, "__getitem__"
    ):
        return None
    return candidate


def _constant_expression_bits(
    expression: Any,
    context_symbol: Any | None,
) -> tuple[str, ...] | None:
    value = _constant_svint(expression, context_symbol)
    if value is None:
        return None
    try:
        source_width = int(value.bitWidth)
        bits = tuple(
            str(value[index]).lower() for index in range(source_width - 1, -1, -1)
        )
    except Exception:
        return None
    if not bits or any(bit not in {"0", "1", "x", "z"} for bit in bits):
        return None
    width = _expression_width(expression) or source_width
    if source_width > width:
        return bits[-width:]
    if source_width < width:
        signed = bool(getattr(value, "isSigned", False))
        extension = bits[0] if signed else "0"
        return (extension,) * (width - source_width) + bits
    return bits


def _constant_int(
    expression: Any,
    context_symbol: Any | None = None,
) -> int | None:
    value = _constant_svint(expression, context_symbol)
    if value is None:
        return None
    try:
        width = int(value.bitWidth)
        bits = tuple(str(value[index]).lower() for index in range(width - 1, -1, -1))
    except Exception:
        return None
    if not bits or any(bit in {"x", "z"} for bit in bits):
        return None
    result = int("".join(bits), 2)
    if bool(getattr(value, "isSigned", False)) and bits[0] == "1":
        result -= 1 << width
    return result


def _selected_bits(
    expression: Any,
    base_bits: tuple[int, ...],
    context_symbol: Any | None = None,
) -> tuple[int, ...]:
    kind = _kind_name(expression)
    base_set = set(base_bits)
    if kind == "ElementSelect":
        selector = getattr(expression, "selector", None)
        index = _constant_int(selector, context_symbol)
        return (index,) if index in base_set else ()
    if kind != "RangeSelect":
        return ()
    left = _constant_int(expression.left, context_symbol)
    right = _constant_int(expression.right, context_symbol)
    if left is None or right is None:
        return ()
    selection_kind = str(expression.selectionKind.name)
    if selection_kind == "Simple":
        # Slang can retain syntax from an inactive parameterized generate
        # branch.  An unsigned expression such as ``Offset-2`` may therefore
        # elaborate to 32'hffff_ffff even when the selected specialization has
        # a one-bit declaration.  Validate against that declaration before
        # materializing a Python range; otherwise one malformed / inactive
        # select can allocate billions of indices and stall a large-SoC
        # projection.
        span = abs(left - right) + 1
        if left not in base_set or right not in base_set or span > len(base_bits):
            return ()
        step = -1 if left > right else 1
        selected = tuple(range(left, right + step, step))
        return selected if all(bit in base_set for bit in selected) else ()
    if selection_kind == "IndexedUp":
        width = right
        last = left + width - 1
    elif selection_kind == "IndexedDown":
        width = right
        last = left - width + 1
    else:
        return ()
    if (
        width < 1
        or width > len(base_bits)
        or left not in base_set
        or last not in base_set
    ):
        return ()
    low, high = sorted((left, last))
    selected = tuple(bit for bit in base_bits if low <= bit <= high)
    return selected if len(selected) == width else ()


def _selected_packed_member_bits(
    selection: Any,
    member: Any,
    container_bits: tuple[int, ...],
    context_symbol: Any | None = None,
) -> tuple[int, ...]:
    member_range = _packed_range(member)
    if member_range is None:
        return ()
    physical_bits = _packed_member_bits(
        container_bits,
        offset=int(getattr(member, "bitOffset", -1)),
        width=member_range.width,
    )
    if not physical_bits:
        return ()
    local_bits = _selected_bits(
        selection,
        member_range.indices,
        context_symbol,
    )
    physical_by_local = dict(zip(member_range.indices, physical_bits))
    try:
        return tuple(physical_by_local[bit] for bit in local_bits)
    except KeyError:
        return ()


def _syntax_text(expression: Any) -> str:
    syntax = getattr(expression, "syntax", None)
    if syntax is None:
        return _kind_name(expression)
    rendered = str(syntax).strip()
    return " ".join(rendered.split()) if rendered else _kind_name(expression)


def _find_assignment_expressions(statement: Any) -> tuple[Any, ...]:
    result: list[Any] = []

    def collect(node: Any) -> None:
        if _kind_name(node) == "Assignment":
            result.append(node)

    try:
        statement.visit(collect)
    except Exception:
        return ()
    return tuple(result)


def _dependencies_for_exact_rhs(
    target: SignalSelection,
    sources: Sequence[SignalSelection],
) -> tuple[DependencyFact, ...] | None:
    try:
        return selections_for_concat(target, sources)
    except ValueError:
        return None


def _merge_selections(
    first: Sequence[SignalSelection],
    second: Sequence[SignalSelection],
) -> tuple[SignalSelection, ...]:
    return _dedupe_selections((*first, *second))


def _dedupe_selections(
    selections: Sequence[SignalSelection],
) -> tuple[SignalSelection, ...]:
    result: list[SignalSelection] = []
    seen: set[tuple[str, tuple[int, ...]]] = set()
    for selection in selections:
        key = selection.symbol, selection.bits
        if key in seen:
            continue
        seen.add(key)
        result.append(selection)
    return tuple(result)


def _dedupe_packed_members(
    members: Sequence[PackedMemberDecl],
) -> tuple[PackedMemberDecl, ...]:
    by_name: dict[str, PackedMemberDecl] = {}
    for member in members:
        by_name.setdefault(member.name, member)
    return tuple(by_name[name] for name in sorted(by_name))


def _prefer_specific_selections(
    selections: Sequence[SignalSelection],
) -> tuple[SignalSelection, ...]:
    deduped = _dedupe_selections(selections)
    return tuple(
        selection
        for selection in deduped
        if not any(
            other.symbol == selection.symbol and set(other.bits) < set(selection.bits)
            for other in deduped
        )
    )


def _dedupe_dependencies(
    dependencies: Sequence[DependencyFact],
) -> tuple[DependencyFact, ...]:
    result: list[DependencyFact] = []
    seen: set[tuple[Any, ...]] = set()
    for dependency in dependencies:
        key = (
            dependency.source.symbol,
            dependency.source.bits,
            dependency.target.symbol,
            dependency.target.bits,
            dependency.role.value,
            dependency.exact_bit_mapping,
            dependency.guard,
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(dependency)
    return tuple(result)


def _assignment_id(
    definition_id: str,
    kind: EdgeKind,
    location: SourceLocation,
    target: SignalSelection,
    target_index: int,
) -> str:
    material = (
        f"{definition_id}|{kind.value}|{location.file}|{location.line}|"
        f"{location.column}|{target.symbol}|{target.bits}|{target_index}"
    ).encode("utf-8")
    return f"assign_{hashlib.sha256(material).hexdigest()[:20]}"


def _extend_unique_assignments(
    target: list[AssignmentFact],
    additions: Sequence[AssignmentFact],
    seen: set[tuple[Any, ...]],
) -> None:
    for assignment in additions:
        key = (
            assignment.assignment_id,
            assignment.target.symbol,
            assignment.target.bits,
        )
        if key in seen:
            continue
        seen.add(key)
        target.append(assignment)


def _dedupe_signals(signals: Sequence[SignalDecl]) -> tuple[SignalDecl, ...]:
    by_name: dict[str, SignalDecl] = {}
    for signal in signals:
        by_name.setdefault(signal.name, signal)
    return tuple(sorted(by_name.values(), key=lambda item: item.name))
