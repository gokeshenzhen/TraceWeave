"""Internal, frontend-neutral source connectivity intermediate representation.

This module is deliberately independent from :mod:`src.schemas` and the MCP
surface.  It is the Phase 1A prototype contract shared by the optional source
frontend projector and the in-process driver/load query prototype.  It stores
definition templates once and keeps hierarchy in instance and port-binding
records; query-time traversal supplies the instance context.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping


CONNECTIVITY_IR_VERSION = "1.1"


class DefinitionKind(str, Enum):
    MODULE = "module"
    INTERFACE = "interface"


class SymbolKind(str, Enum):
    NET = "net"
    VARIABLE = "variable"
    PORT = "port"
    INTERFACE_FIELD = "interface_field"


class PortDirection(str, Enum):
    INPUT = "input"
    OUTPUT = "output"
    INOUT = "inout"
    INTERFACE = "interface"


class BindingStyle(str, Enum):
    NAMED = "named"
    POSITIONAL = "positional"
    INTERFACE = "interface"
    MODPORT = "modport"


class BindingSourceKind(str, Enum):
    """Provenance kind for one ordered port-binding bit segment."""

    SIGNAL = "signal"
    CONSTANT = "constant"
    UNRESOLVED = "unresolved"


class EdgeKind(str, Enum):
    PORT_BIND_INPUT = "port_bind_input"
    PORT_BIND_OUTPUT = "port_bind_output"
    PORT_BIND_INOUT = "port_bind_inout"
    INTERFACE_BIND = "interface_bind"
    CONTINUOUS_ASSIGN = "continuous_assign"
    PROCEDURAL_ASSIGN = "procedural_assign"
    DATA_DEPENDENCY = "data_dependency"
    CONTROL_DEPENDENCY = "control_dependency"
    CONSTANT_DRIVER = "constant_driver"


class DependencyRole(str, Enum):
    DATA = "data"
    CONTROL = "control"


class BoundaryKind(str, Enum):
    COMBINATIONAL = "combinational"
    SEQUENTIAL = "sequential"


class ResolutionKind(str, Enum):
    EXACT_SOURCE = "exact_source"
    CONDITIONAL = "conditional"
    UNRESOLVED = "unresolved"


class CoverageStatus(str, Enum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class SourceLocation:
    file: str
    line: int
    column: int = 0

    def __post_init__(self) -> None:
        if not self.file:
            raise ValueError("source location file must not be empty")
        if self.line < 1:
            raise ValueError("source location line must be positive")
        if self.column < 0:
            raise ValueError("source location column must not be negative")


@dataclass(frozen=True)
class SourceEvidence:
    construct: str
    location: SourceLocation
    resolution: ResolutionKind = ResolutionKind.EXACT_SOURCE
    frontend: str = "unknown"
    frontend_version: str | None = None
    detail: str | None = None

    def __post_init__(self) -> None:
        if not self.construct:
            raise ValueError("evidence construct must not be empty")
        if not self.frontend:
            raise ValueError("evidence frontend must not be empty")


@dataclass(frozen=True)
class BitRange:
    """A declared packed range, preserving its source left-to-right order."""

    left: int
    right: int

    @classmethod
    def scalar(cls) -> BitRange:
        return cls(0, 0)

    @classmethod
    def from_width(cls, width: int) -> BitRange:
        if width < 1:
            raise ValueError("bit width must be positive")
        return cls(width - 1, 0)

    @property
    def width(self) -> int:
        return abs(self.left - self.right) + 1

    @property
    def indices(self) -> tuple[int, ...]:
        step = -1 if self.left > self.right else 1
        return tuple(range(self.left, self.right + step, step))


@dataclass(frozen=True)
class SignalDecl:
    name: str
    kind: SymbolKind
    packed_range: BitRange
    location: SourceLocation

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("signal name must not be empty")


@dataclass(frozen=True)
class PortDecl:
    name: str
    direction: PortDirection
    packed_range: BitRange
    ordinal: int
    location: SourceLocation
    interface_definition: str | None = None
    modport: str | None = None

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("port name must not be empty")
        if self.ordinal < 0:
            raise ValueError("port ordinal must not be negative")
        if self.direction is PortDirection.INTERFACE and not self.interface_definition:
            raise ValueError("interface ports require interface_definition")
        if self.direction is not PortDirection.INTERFACE and (
            self.interface_definition or self.modport
        ):
            raise ValueError("plain ports cannot name an interface or modport")


@dataclass(frozen=True)
class ModportMember:
    name: str
    direction: PortDirection

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("modport member name must not be empty")
        if self.direction is PortDirection.INTERFACE:
            raise ValueError("modport member direction must be input/output/inout")


@dataclass(frozen=True)
class ModportDecl:
    name: str
    members: tuple[ModportMember, ...]
    location: SourceLocation

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("modport name must not be empty")
        names = [member.name for member in self.members]
        if len(names) != len(set(names)):
            raise ValueError(f"duplicate member in modport {self.name}")


@dataclass(frozen=True)
class SignalSelection:
    """A signal plus ordered packed-bit selection.

    ``instance_path=None`` denotes a definition-template-local reference.
    Port bindings use explicit instance paths on both actual and formal sides.
    The ordered bit tuples make concat and ascending-range mappings unambiguous.
    """

    symbol: str
    bits: tuple[int, ...]
    instance_path: str | None = None

    def __post_init__(self) -> None:
        if not self.symbol:
            raise ValueError("selection symbol must not be empty")
        if not self.bits:
            raise ValueError("selection must contain at least one bit")
        if len(self.bits) != len(set(self.bits)):
            raise ValueError("selection bits must be unique")
        if self.instance_path == "":
            raise ValueError("instance path must be None or non-empty")

    @classmethod
    def template(cls, symbol: str, packed_range: BitRange) -> SignalSelection:
        return cls(symbol=symbol, bits=packed_range.indices)

    @classmethod
    def bound(
        cls,
        instance_path: str,
        symbol: str,
        packed_range: BitRange,
    ) -> SignalSelection:
        return cls(
            instance_path=instance_path,
            symbol=symbol,
            bits=packed_range.indices,
        )

    @property
    def width(self) -> int:
        return len(self.bits)

    def bind(self, instance_path: str) -> SignalSelection:
        if self.instance_path is not None:
            if self.instance_path != instance_path:
                raise ValueError(
                    f"selection is already bound to {self.instance_path}, not {instance_path}"
                )
            return self
        return SignalSelection(
            instance_path=instance_path,
            symbol=self.symbol,
            bits=self.bits,
        )

    def path(self, include_bits: bool = False) -> str:
        prefix = f"{self.instance_path}." if self.instance_path else ""
        base = f"{prefix}{self.symbol}"
        if not include_bits:
            return base
        if len(self.bits) == 1:
            return f"{base}[{self.bits[0]}]"
        step = self.bits[1] - self.bits[0]
        contiguous = abs(step) == 1 and all(
            right - left == step for left, right in zip(self.bits, self.bits[1:])
        )
        if contiguous:
            return f"{base}[{self.bits[0]}:{self.bits[-1]}]"
        return f"{base}[{','.join(str(bit) for bit in self.bits)}]"


@dataclass(frozen=True)
class BitMapping:
    """Ordered provenance for one port-binding target bit segment.

    Signal segments retain the historical equal-width source/target mapping.
    Constant segments preserve their four-state bits in target order, while an
    unresolved segment reserves the affected target bits without inventing a
    signal source.  Keeping all three forms in the same ordered collection lets
    a binding such as ``.data_i({8'h0, payload[23:0]})`` remain useful without
    pretending that the complete 32-bit formal is driven by ``payload``.
    """

    source: SignalSelection | None
    target: SignalSelection
    source_kind: BindingSourceKind = BindingSourceKind.SIGNAL
    constant_bits: tuple[str, ...] = ()
    unresolved_reason: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "source_kind", BindingSourceKind(self.source_kind))
        if self.source_kind is BindingSourceKind.SIGNAL:
            if self.source is None:
                raise ValueError("signal bit mapping requires a source selection")
            if self.source.width != self.target.width:
                raise ValueError("bit mapping source and target widths must match")
            if self.constant_bits or self.unresolved_reason is not None:
                raise ValueError(
                    "signal bit mapping cannot carry non-signal provenance"
                )
            return
        if self.source is not None:
            raise ValueError("non-signal bit mapping cannot carry a source selection")
        if self.source_kind is BindingSourceKind.CONSTANT:
            normalized = tuple(str(bit).lower() for bit in self.constant_bits)
            if len(normalized) != self.target.width:
                raise ValueError("constant mapping width must match target width")
            if any(bit not in {"0", "1", "x", "z"} for bit in normalized):
                raise ValueError("constant mapping bits must be 0, 1, x, or z")
            if self.unresolved_reason is not None:
                raise ValueError("constant mapping cannot carry an unresolved reason")
            object.__setattr__(self, "constant_bits", normalized)
            return
        if self.constant_bits:
            raise ValueError("unresolved mapping cannot carry constant bits")
        if (
            not self.unresolved_reason
            or self.unresolved_reason.strip() != self.unresolved_reason
        ):
            raise ValueError("unresolved mapping requires a non-empty trimmed reason")

    @property
    def width(self) -> int:
        return self.target.width


@dataclass(frozen=True)
class DependencyFact:
    source: SignalSelection
    target: SignalSelection
    role: DependencyRole = DependencyRole.DATA
    exact_bit_mapping: bool = True
    guard: str | None = None

    def __post_init__(self) -> None:
        if (
            self.source.instance_path is not None
            or self.target.instance_path is not None
        ):
            raise ValueError("assignment dependencies must be template-local")
        if (
            self.role is DependencyRole.DATA
            and self.exact_bit_mapping
            and self.source.width != self.target.width
        ):
            raise ValueError("exact data dependency widths must match")


@dataclass(frozen=True)
class AssignmentFact:
    assignment_id: str
    kind: EdgeKind
    target: SignalSelection
    dependencies: tuple[DependencyFact, ...]
    boundary: BoundaryKind
    evidence: SourceEvidence
    procedure_kind: str | None = None
    guard: str | None = None
    generate_scope: str | None = None

    def __post_init__(self) -> None:
        if not self.assignment_id:
            raise ValueError("assignment_id must not be empty")
        if self.kind not in {
            EdgeKind.CONTINUOUS_ASSIGN,
            EdgeKind.PROCEDURAL_ASSIGN,
        }:
            raise ValueError(
                "assignment kind must be continuous_assign or procedural_assign"
            )
        if self.target.instance_path is not None:
            raise ValueError("assignment target must be template-local")
        if self.kind is EdgeKind.CONTINUOUS_ASSIGN and self.procedure_kind:
            raise ValueError("continuous assignments cannot have a procedure kind")
        if self.kind is EdgeKind.PROCEDURAL_ASSIGN and not self.procedure_kind:
            raise ValueError("procedural assignments require a procedure kind")
        target_bits = set(self.target.bits)
        for dependency in self.dependencies:
            if dependency.target.symbol != self.target.symbol:
                raise ValueError(
                    "dependency target must match assignment target symbol"
                )
            if not set(dependency.target.bits).issubset(target_bits):
                raise ValueError(
                    "dependency target bits must be within assignment target"
                )


@dataclass(frozen=True)
class DefinitionTemplate:
    definition_id: str
    name: str
    kind: DefinitionKind
    location: SourceLocation
    ports: tuple[PortDecl, ...] = ()
    signals: tuple[SignalDecl, ...] = ()
    modports: tuple[ModportDecl, ...] = ()
    assignments: tuple[AssignmentFact, ...] = ()

    def __post_init__(self) -> None:
        if not self.definition_id or not self.name:
            raise ValueError("definition id and name must not be empty")
        port_names = [port.name for port in self.ports]
        signal_names = [signal.name for signal in self.signals]
        if len(port_names) != len(set(port_names)):
            raise ValueError(f"duplicate port in definition {self.definition_id}")
        if len(signal_names) != len(set(signal_names)):
            raise ValueError(f"duplicate signal in definition {self.definition_id}")
        if set(port_names) & set(signal_names):
            raise ValueError(
                f"port and internal signal names overlap in definition {self.definition_id}"
            )
        ordinals = [port.ordinal for port in self.ports]
        if len(ordinals) != len(set(ordinals)):
            raise ValueError(
                f"duplicate port ordinal in definition {self.definition_id}"
            )
        modport_names = [modport.name for modport in self.modports]
        if len(modport_names) != len(set(modport_names)):
            raise ValueError(f"duplicate modport in definition {self.definition_id}")
        assignment_ids = [assignment.assignment_id for assignment in self.assignments]
        if len(assignment_ids) != len(set(assignment_ids)):
            raise ValueError(
                f"duplicate assignment id in definition {self.definition_id}"
            )

    def direct_signal_range(self, symbol: str) -> BitRange | None:
        for port in self.ports:
            if port.name == symbol:
                return port.packed_range
        for signal in self.signals:
            if signal.name == symbol:
                return signal.packed_range
        return None

    def port(self, name: str) -> PortDecl | None:
        return next((port for port in self.ports if port.name == name), None)


@dataclass(frozen=True)
class InstanceDecl:
    path: str
    name: str
    definition_id: str
    parent_path: str | None
    location: SourceLocation
    generate_scope: str | None = None
    parameterization: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if not self.path or not self.name or not self.definition_id:
            raise ValueError("instance path, name, and definition id must not be empty")
        if self.parent_path == self.path:
            raise ValueError("instance cannot be its own parent")
        parameter_names = [name for name, _ in self.parameterization]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError(f"duplicate instance parameter on {self.path}")


@dataclass(frozen=True)
class PortBinding:
    binding_id: str
    instance_path: str
    port_name: str
    direction: PortDirection
    style: BindingStyle
    mappings: tuple[BitMapping, ...]
    evidence: SourceEvidence
    port_position: int | None = None
    interface_definition: str | None = None
    modport: str | None = None

    def __post_init__(self) -> None:
        if not self.binding_id or not self.instance_path or not self.port_name:
            raise ValueError(
                "binding id, instance path, and port name must not be empty"
            )
        if not self.mappings:
            raise ValueError("port binding must contain at least one bit mapping")
        if self.style is BindingStyle.POSITIONAL and self.port_position is None:
            raise ValueError("positional binding requires port_position")
        if self.port_position is not None and self.port_position < 0:
            raise ValueError("port_position must not be negative")
        if self.style in {BindingStyle.INTERFACE, BindingStyle.MODPORT}:
            if not self.interface_definition:
                raise ValueError("interface binding requires interface_definition")
        elif self.interface_definition or self.modport:
            raise ValueError("plain binding cannot name an interface or modport")
        for mapping in self.mappings:
            if mapping.target.instance_path is None:
                raise ValueError("port binding mappings must be hierarchy-bound")
            if (
                mapping.source_kind is BindingSourceKind.SIGNAL
                and mapping.source is not None
                and mapping.source.instance_path is None
            ):
                raise ValueError("port binding mappings must be hierarchy-bound")

    @property
    def edge_kind(self) -> EdgeKind:
        if self.style in {BindingStyle.INTERFACE, BindingStyle.MODPORT}:
            return EdgeKind.INTERFACE_BIND
        return {
            PortDirection.INPUT: EdgeKind.PORT_BIND_INPUT,
            PortDirection.OUTPUT: EdgeKind.PORT_BIND_OUTPUT,
            PortDirection.INOUT: EdgeKind.PORT_BIND_INOUT,
        }[self.direction]


@dataclass(frozen=True)
class CoverageGap:
    code: str
    message: str
    impact: CoverageStatus
    constructs: tuple[str, ...] = ()
    scopes: tuple[str, ...] = ()
    location: SourceLocation | None = None

    def __post_init__(self) -> None:
        if not self.code or not self.message:
            raise ValueError("coverage gap code and message must not be empty")
        if self.impact is CoverageStatus.COMPLETE:
            raise ValueError("coverage gap impact cannot be complete")

    def affects(self, signal_path: str) -> bool:
        if not self.scopes or "*" in self.scopes:
            return True
        return any(
            signal_path == scope
            or signal_path.startswith(f"{scope}.")
            or scope.startswith(f"{signal_path}.")
            for scope in self.scopes
        )


@dataclass(frozen=True)
class CoverageReport:
    status: CoverageStatus
    files_total: int
    files_projected: int
    gaps: tuple[CoverageGap, ...] = ()
    diagnostic_count: int = 0
    blocking_diagnostic_count: int = 0

    def __post_init__(self) -> None:
        if self.files_total < 0 or self.files_projected < 0:
            raise ValueError("coverage file counts must not be negative")
        if self.files_projected > self.files_total:
            raise ValueError("files_projected cannot exceed files_total")
        if self.diagnostic_count < 0 or self.blocking_diagnostic_count < 0:
            raise ValueError("diagnostic counts must not be negative")
        if self.blocking_diagnostic_count > self.diagnostic_count:
            raise ValueError("blocking diagnostics cannot exceed total diagnostics")
        if self.status is CoverageStatus.COMPLETE and self.gaps:
            raise ValueError("complete coverage cannot contain coverage gaps")
        if self.status is CoverageStatus.COMPLETE and (
            self.files_projected != self.files_total or self.blocking_diagnostic_count
        ):
            raise ValueError(
                "complete coverage requires all files and no blocking diagnostics"
            )


@dataclass(frozen=True)
class ConnectivityIR:
    """Versioned, deterministic, internal-only minimal connectivity model."""

    frontend_name: str
    frontend_version: str
    definitions: tuple[DefinitionTemplate, ...]
    instances: tuple[InstanceDecl, ...]
    bindings: tuple[PortBinding, ...]
    coverage: CoverageReport
    top_instances: tuple[str, ...]
    metadata: tuple[tuple[str, str], ...] = ()
    ir_version: str = field(default=CONNECTIVITY_IR_VERSION, init=False)

    def __post_init__(self) -> None:
        if not self.frontend_name or not self.frontend_version:
            raise ValueError("frontend name and version must not be empty")
        definition_ids = [definition.definition_id for definition in self.definitions]
        if len(definition_ids) != len(set(definition_ids)):
            raise ValueError("definition ids must be unique")
        instance_paths = [instance.path for instance in self.instances]
        if len(instance_paths) != len(set(instance_paths)):
            raise ValueError("instance paths must be unique")
        binding_ids = [binding.binding_id for binding in self.bindings]
        if len(binding_ids) != len(set(binding_ids)):
            raise ValueError("binding ids must be unique")
        definition_set = set(definition_ids)
        instance_set = set(instance_paths)
        for instance in self.instances:
            if instance.definition_id not in definition_set:
                raise ValueError(
                    f"instance {instance.path} references unknown definition "
                    f"{instance.definition_id}"
                )
            if (
                instance.parent_path is not None
                and instance.parent_path not in instance_set
            ):
                raise ValueError(
                    f"instance {instance.path} references unknown parent {instance.parent_path}"
                )
        for top in self.top_instances:
            if top not in instance_set:
                raise ValueError(f"top instance {top} is not present in instances")
            instance = next(item for item in self.instances if item.path == top)
            if instance.parent_path is not None:
                raise ValueError(f"top instance {top} must not have a parent")
        for binding in self.bindings:
            if binding.instance_path not in instance_set:
                raise ValueError(
                    f"binding {binding.binding_id} references unknown instance "
                    f"{binding.instance_path}"
                )
            for mapping in binding.mappings:
                selections = (
                    (mapping.source, mapping.target)
                    if mapping.source is not None
                    else (mapping.target,)
                )
                for selection in selections:
                    if selection.instance_path not in instance_set:
                        raise ValueError(
                            f"binding {binding.binding_id} references unknown endpoint "
                            f"{selection.instance_path}"
                        )
        metadata_names = [name for name, _ in self.metadata]
        if len(metadata_names) != len(set(metadata_names)):
            raise ValueError("metadata keys must be unique")

    @property
    def definition_index(self) -> dict[str, DefinitionTemplate]:
        return {definition.definition_id: definition for definition in self.definitions}

    @property
    def instance_index(self) -> dict[str, InstanceDecl]:
        return {instance.path: instance for instance in self.instances}

    def stats(self) -> dict[str, int]:
        signal_count = sum(
            len(definition.ports) + len(definition.signals)
            for definition in self.definitions
        )
        assignment_count = sum(
            len(definition.assignments) for definition in self.definitions
        )
        dependency_count = sum(
            len(assignment.dependencies)
            for definition in self.definitions
            for assignment in definition.assignments
        )
        binding_segment_count = sum(len(binding.mappings) for binding in self.bindings)
        return {
            "definition_count": len(self.definitions),
            "instance_count": len(self.instances),
            "signal_decl_count": signal_count,
            "modport_count": sum(len(item.modports) for item in self.definitions),
            "binding_count": len(self.bindings),
            "binding_segment_count": binding_segment_count,
            "assignment_count": assignment_count,
            "dependency_count": dependency_count,
            "node_count": len(self.definitions) + len(self.instances) + signal_count,
            "edge_count": binding_segment_count + assignment_count + dependency_count,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["definitions"] = sorted(
            payload["definitions"], key=lambda item: item["definition_id"]
        )
        for definition in payload["definitions"]:
            definition["ports"] = sorted(
                definition["ports"], key=lambda item: (item["ordinal"], item["name"])
            )
            definition["signals"] = sorted(
                definition["signals"], key=lambda item: item["name"]
            )
            definition["modports"] = sorted(
                definition["modports"], key=lambda item: item["name"]
            )
            definition["assignments"] = sorted(
                definition["assignments"], key=lambda item: item["assignment_id"]
            )
        payload["instances"] = sorted(
            payload["instances"], key=lambda item: item["path"]
        )
        payload["bindings"] = sorted(
            payload["bindings"], key=lambda item: item["binding_id"]
        )
        payload["top_instances"] = sorted(payload["top_instances"])
        payload["metadata"] = sorted(payload["metadata"])
        payload["coverage"]["gaps"] = sorted(
            payload["coverage"]["gaps"],
            key=lambda item: (
                item["code"],
                item["location"]["file"] if item["location"] else "",
                item["location"]["line"] if item["location"] else 0,
            ),
        )
        return _enum_values(payload)

    def to_json_bytes(self) -> bytes:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")

    def fingerprint_sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ConnectivityIR:
        version = payload.get("ir_version")
        if version != CONNECTIVITY_IR_VERSION:
            raise ValueError(
                f"unsupported connectivity IR version {version!r}; "
                f"expected {CONNECTIVITY_IR_VERSION!r}"
            )
        definitions = tuple(
            _definition_from_dict(item) for item in payload["definitions"]
        )
        instances = tuple(_instance_from_dict(item) for item in payload["instances"])
        bindings = tuple(_binding_from_dict(item) for item in payload["bindings"])
        coverage = _coverage_from_dict(payload["coverage"])
        return cls(
            frontend_name=str(payload["frontend_name"]),
            frontend_version=str(payload["frontend_version"]),
            definitions=definitions,
            instances=instances,
            bindings=bindings,
            coverage=coverage,
            top_instances=tuple(str(item) for item in payload["top_instances"]),
            metadata=tuple(
                (str(key), str(value)) for key, value in payload.get("metadata", ())
            ),
        )

    @classmethod
    def from_json_bytes(cls, payload: bytes) -> ConnectivityIR:
        decoded = json.loads(payload)
        if not isinstance(decoded, dict):
            raise ValueError("connectivity IR JSON root must be an object")
        return cls.from_dict(decoded)


def selections_for_concat(
    target: SignalSelection,
    sources: Iterable[SignalSelection],
) -> tuple[DependencyFact, ...]:
    """Map left-to-right concat operands onto a target selection exactly."""

    source_list = tuple(sources)
    if sum(source.width for source in source_list) != target.width:
        raise ValueError("concat source widths must equal target width")
    dependencies: list[DependencyFact] = []
    offset = 0
    for source in source_list:
        target_part = SignalSelection(
            symbol=target.symbol,
            bits=target.bits[offset : offset + source.width],
        )
        dependencies.append(
            DependencyFact(
                source=source,
                target=target_part,
                role=DependencyRole.DATA,
                exact_bit_mapping=True,
            )
        )
        offset += source.width
    return tuple(dependencies)


def _enum_values(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enum_values(item) for item in value]
    return value


def _location_from_dict(payload: Mapping[str, Any]) -> SourceLocation:
    return SourceLocation(
        file=str(payload["file"]),
        line=int(payload["line"]),
        column=int(payload.get("column", 0)),
    )


def _evidence_from_dict(payload: Mapping[str, Any]) -> SourceEvidence:
    return SourceEvidence(
        construct=str(payload["construct"]),
        location=_location_from_dict(payload["location"]),
        resolution=ResolutionKind(
            payload.get("resolution", ResolutionKind.EXACT_SOURCE)
        ),
        frontend=str(payload.get("frontend", "unknown")),
        frontend_version=(
            str(payload["frontend_version"])
            if payload.get("frontend_version") is not None
            else None
        ),
        detail=str(payload["detail"]) if payload.get("detail") is not None else None,
    )


def _range_from_dict(payload: Mapping[str, Any]) -> BitRange:
    return BitRange(left=int(payload["left"]), right=int(payload["right"]))


def _selection_from_dict(payload: Mapping[str, Any]) -> SignalSelection:
    return SignalSelection(
        symbol=str(payload["symbol"]),
        bits=tuple(int(item) for item in payload["bits"]),
        instance_path=(
            str(payload["instance_path"])
            if payload.get("instance_path") is not None
            else None
        ),
    )


def _dependency_from_dict(payload: Mapping[str, Any]) -> DependencyFact:
    return DependencyFact(
        source=_selection_from_dict(payload["source"]),
        target=_selection_from_dict(payload["target"]),
        role=DependencyRole(payload.get("role", DependencyRole.DATA)),
        exact_bit_mapping=bool(payload.get("exact_bit_mapping", True)),
        guard=str(payload["guard"]) if payload.get("guard") is not None else None,
    )


def _assignment_from_dict(payload: Mapping[str, Any]) -> AssignmentFact:
    return AssignmentFact(
        assignment_id=str(payload["assignment_id"]),
        kind=EdgeKind(payload["kind"]),
        target=_selection_from_dict(payload["target"]),
        dependencies=tuple(
            _dependency_from_dict(item) for item in payload.get("dependencies", ())
        ),
        boundary=BoundaryKind(payload["boundary"]),
        evidence=_evidence_from_dict(payload["evidence"]),
        procedure_kind=(
            str(payload["procedure_kind"])
            if payload.get("procedure_kind") is not None
            else None
        ),
        guard=str(payload["guard"]) if payload.get("guard") is not None else None,
        generate_scope=(
            str(payload["generate_scope"])
            if payload.get("generate_scope") is not None
            else None
        ),
    )


def _definition_from_dict(payload: Mapping[str, Any]) -> DefinitionTemplate:
    ports = tuple(
        PortDecl(
            name=str(item["name"]),
            direction=PortDirection(item["direction"]),
            packed_range=_range_from_dict(item["packed_range"]),
            ordinal=int(item["ordinal"]),
            location=_location_from_dict(item["location"]),
            interface_definition=(
                str(item["interface_definition"])
                if item.get("interface_definition") is not None
                else None
            ),
            modport=str(item["modport"]) if item.get("modport") is not None else None,
        )
        for item in payload.get("ports", ())
    )
    signals = tuple(
        SignalDecl(
            name=str(item["name"]),
            kind=SymbolKind(item["kind"]),
            packed_range=_range_from_dict(item["packed_range"]),
            location=_location_from_dict(item["location"]),
        )
        for item in payload.get("signals", ())
    )
    modports = tuple(
        ModportDecl(
            name=str(item["name"]),
            members=tuple(
                ModportMember(
                    name=str(member["name"]),
                    direction=PortDirection(member["direction"]),
                )
                for member in item.get("members", ())
            ),
            location=_location_from_dict(item["location"]),
        )
        for item in payload.get("modports", ())
    )
    return DefinitionTemplate(
        definition_id=str(payload["definition_id"]),
        name=str(payload["name"]),
        kind=DefinitionKind(payload["kind"]),
        location=_location_from_dict(payload["location"]),
        ports=ports,
        signals=signals,
        modports=modports,
        assignments=tuple(
            _assignment_from_dict(item) for item in payload.get("assignments", ())
        ),
    )


def _instance_from_dict(payload: Mapping[str, Any]) -> InstanceDecl:
    return InstanceDecl(
        path=str(payload["path"]),
        name=str(payload["name"]),
        definition_id=str(payload["definition_id"]),
        parent_path=(
            str(payload["parent_path"])
            if payload.get("parent_path") is not None
            else None
        ),
        location=_location_from_dict(payload["location"]),
        generate_scope=(
            str(payload["generate_scope"])
            if payload.get("generate_scope") is not None
            else None
        ),
        parameterization=tuple(
            (str(name), str(value))
            for name, value in payload.get("parameterization", ())
        ),
    )


def _binding_from_dict(payload: Mapping[str, Any]) -> PortBinding:
    return PortBinding(
        binding_id=str(payload["binding_id"]),
        instance_path=str(payload["instance_path"]),
        port_name=str(payload["port_name"]),
        direction=PortDirection(payload["direction"]),
        style=BindingStyle(payload["style"]),
        mappings=tuple(
            BitMapping(
                source=(
                    _selection_from_dict(item["source"])
                    if item.get("source") is not None
                    else None
                ),
                target=_selection_from_dict(item["target"]),
                source_kind=BindingSourceKind(
                    item.get("source_kind", BindingSourceKind.SIGNAL)
                ),
                constant_bits=tuple(str(bit) for bit in item.get("constant_bits", ())),
                unresolved_reason=(
                    str(item["unresolved_reason"])
                    if item.get("unresolved_reason") is not None
                    else None
                ),
            )
            for item in payload.get("mappings", ())
        ),
        evidence=_evidence_from_dict(payload["evidence"]),
        port_position=(
            int(payload["port_position"])
            if payload.get("port_position") is not None
            else None
        ),
        interface_definition=(
            str(payload["interface_definition"])
            if payload.get("interface_definition") is not None
            else None
        ),
        modport=str(payload["modport"]) if payload.get("modport") is not None else None,
    )


def _coverage_from_dict(payload: Mapping[str, Any]) -> CoverageReport:
    gaps = tuple(
        CoverageGap(
            code=str(item["code"]),
            message=str(item["message"]),
            impact=CoverageStatus(item["impact"]),
            constructs=tuple(str(value) for value in item.get("constructs", ())),
            scopes=tuple(str(value) for value in item.get("scopes", ())),
            location=(
                _location_from_dict(item["location"])
                if item.get("location") is not None
                else None
            ),
        )
        for item in payload.get("gaps", ())
    )
    return CoverageReport(
        status=CoverageStatus(payload["status"]),
        files_total=int(payload["files_total"]),
        files_projected=int(payload["files_projected"]),
        gaps=gaps,
        diagnostic_count=int(payload.get("diagnostic_count", 0)),
        blocking_diagnostic_count=int(payload.get("blocking_diagnostic_count", 0)),
    )
