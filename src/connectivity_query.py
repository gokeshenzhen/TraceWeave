"""Internal Phase 1A driver/load queries over :mod:`connectivity_ir`.

The prototype intentionally has no MCP registration and no relationship to the
production NPI -> Legacy Static route.  Port and interface bindings are
transparent hierarchy edges.  Source assignments are terminal driver/consumer
facts, and sequential boundaries are never crossed.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Iterable

from .connectivity_ir import (
    AssignmentFact,
    BindingStyle,
    BitMapping,
    BoundaryKind,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionTemplate,
    DependencyFact,
    EdgeKind,
    PortBinding,
    PortDirection,
    ResolutionKind,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
)


class QueryStatus(str, Enum):
    FOUND = "found"
    NOT_CONNECTED = "not_connected"
    INCONCLUSIVE = "inconclusive"


class QueryConfidence(str, Enum):
    EXACT_SOURCE = "exact_source"
    CONDITIONAL = "conditional"
    PARTIAL = "partial"


@dataclass(frozen=True)
class TraversalHop:
    edge_kind: EdgeKind
    source: SignalSelection
    target: SignalSelection
    binding_id: str
    binding_style: BindingStyle
    evidence: SourceEvidence


@dataclass(frozen=True)
class QueryDependency:
    source: SignalSelection
    target: SignalSelection
    role: str
    exact_bit_mapping: bool
    guard: str | None


@dataclass(frozen=True)
class QueryMatch:
    instance_path: str
    fact_id: str
    kind: EdgeKind
    target: SignalSelection
    source: SignalSelection | None
    dependencies: tuple[QueryDependency, ...]
    dependency_role: str | None
    boundary: BoundaryKind
    procedure_kind: str | None
    guard: str | None
    generate_scope: str | None
    evidence: SourceEvidence
    traversal: tuple[TraversalHop, ...]
    confidence: QueryConfidence


@dataclass(frozen=True)
class ConnectivityQueryResult:
    operation: str
    signal: SignalSelection
    status: QueryStatus
    coverage_status: CoverageStatus
    matches: tuple[QueryMatch, ...]
    unresolved_boundaries: tuple[CoverageGap, ...]
    traversed_binding_edges: int
    max_depth: int

    def to_dict(self) -> dict[str, Any]:
        return _enum_values(asdict(self))


@dataclass(frozen=True)
class _FlowSegment:
    source: SignalSelection
    target: SignalSelection
    binding: PortBinding


_TRAILING_SELECT_RE = re.compile(
    r"^(?P<base>.+?)(?:\[(?P<left>-?\d+)(?::(?P<right>-?\d+))?\])?$"
)


class ConnectivityQueryEngine:
    """Build lightweight indexes and answer internal driver/load queries."""

    def __init__(self, ir: ConnectivityIR):
        self.ir = ir
        self._definitions = ir.definition_index
        self._instances = ir.instance_index
        self._instance_paths = sorted(self._instances, key=len, reverse=True)
        self._incoming: dict[tuple[str, str], list[_FlowSegment]] = defaultdict(list)
        self._outgoing: dict[tuple[str, str], list[_FlowSegment]] = defaultdict(list)
        self._writes: dict[tuple[str, str], list[AssignmentFact]] = defaultdict(list)
        self._reads: dict[
            tuple[str, str], list[tuple[AssignmentFact, DependencyFact]]
        ] = defaultdict(list)
        self._build_indexes()

    def query_driver(
        self,
        signal_path: str,
        *,
        max_depth: int = 64,
    ) -> ConnectivityQueryResult:
        if max_depth < 0:
            raise ValueError("max_depth must not be negative")
        signal = self.resolve_signal(signal_path)
        matches: list[QueryMatch] = []
        visited: set[tuple[str, str, tuple[int, ...]]] = set()
        traversed = 0
        depth_limited = False
        touched_paths: set[str] = {signal.path()}

        def visit(
            current: SignalSelection,
            depth: int,
            traversal: tuple[TraversalHop, ...],
        ) -> None:
            nonlocal traversed, depth_limited
            state = _state_key(current)
            if state in visited:
                return
            visited.add(state)
            touched_paths.add(current.path())

            assignments = [
                assignment
                for assignment in self._writes.get(_endpoint_key(current), ())
                if _overlaps(assignment.target.bits, current.bits)
            ]
            for assignment in assignments:
                matches.append(
                    self._driver_match(
                        assignment,
                        current.instance_path or "",
                        current,
                        traversal,
                    )
                )

            segments = [
                segment
                for segment in self._incoming.get(_endpoint_key(current), ())
                if _overlaps(segment.target.bits, current.bits)
            ]
            if not segments:
                return
            if depth >= max_depth:
                depth_limited = True
                return
            for segment in segments:
                upstream = _map_selection(
                    selected=current,
                    mapped_from=segment.target,
                    mapped_to=segment.source,
                )
                hop = _hop(segment)
                traversed += 1
                visit(upstream, depth + 1, traversal + (hop,))

        visit(signal, 0, ())
        return self._finish_result(
            operation="driver",
            signal=signal,
            matches=matches,
            touched_paths=touched_paths,
            traversed=traversed,
            depth_limited=depth_limited,
            max_depth=max_depth,
        )

    def query_loads(
        self,
        signal_path: str,
        *,
        max_depth: int = 64,
    ) -> ConnectivityQueryResult:
        if max_depth < 0:
            raise ValueError("max_depth must not be negative")
        signal = self.resolve_signal(signal_path)
        matches: list[QueryMatch] = []
        visited: set[tuple[str, str, tuple[int, ...]]] = set()
        traversed = 0
        depth_limited = False
        touched_paths: set[str] = {signal.path()}

        def visit(
            current: SignalSelection,
            depth: int,
            traversal: tuple[TraversalHop, ...],
        ) -> None:
            nonlocal traversed, depth_limited
            state = _state_key(current)
            if state in visited:
                return
            visited.add(state)
            touched_paths.add(current.path())

            for assignment, dependency in self._reads.get(_endpoint_key(current), ()):
                if not _overlaps(dependency.source.bits, current.bits):
                    continue
                matches.append(
                    self._load_match(
                        assignment,
                        dependency,
                        current.instance_path or "",
                        current,
                        traversal,
                    )
                )

            segments = [
                segment
                for segment in self._outgoing.get(_endpoint_key(current), ())
                if _overlaps(segment.source.bits, current.bits)
            ]
            if not segments:
                return
            if depth >= max_depth:
                depth_limited = True
                return
            for segment in segments:
                downstream = _map_selection(
                    selected=current,
                    mapped_from=segment.source,
                    mapped_to=segment.target,
                )
                hop = _hop(segment)
                traversed += 1
                visit(downstream, depth + 1, traversal + (hop,))

        visit(signal, 0, ())
        return self._finish_result(
            operation="load",
            signal=signal,
            matches=matches,
            touched_paths=touched_paths,
            traversed=traversed,
            depth_limited=depth_limited,
            max_depth=max_depth,
        )

    def resolve_signal(self, signal_path: str) -> SignalSelection:
        match = _TRAILING_SELECT_RE.fullmatch(signal_path.strip())
        if not match:
            raise ValueError(f"invalid signal path {signal_path!r}")
        base = match.group("base")
        instance_path = next(
            (
                path
                for path in self._instance_paths
                if base.startswith(f"{path}.") and len(base) > len(path) + 1
            ),
            None,
        )
        if instance_path is None:
            raise KeyError(
                f"signal path does not resolve to an IR instance: {signal_path}"
            )
        symbol = base[len(instance_path) + 1 :]
        declared = self._resolve_symbol_range(instance_path, symbol)
        if declared is None:
            raise KeyError(f"signal is not declared in the IR: {signal_path}")
        if match.group("left") is None:
            bits = declared.indices
        else:
            left = int(match.group("left"))
            right = int(match.group("right") or left)
            step = -1 if left > right else 1
            bits = tuple(range(left, right + step, step))
            undeclared = set(bits) - set(declared.indices)
            if undeclared:
                raise ValueError(
                    f"selection {signal_path} contains undeclared bits {sorted(undeclared)}"
                )
        return SignalSelection(instance_path=instance_path, symbol=symbol, bits=bits)

    def _build_indexes(self) -> None:
        for binding in self.ir.bindings:
            for mapping in binding.mappings:
                for segment in self._oriented_segments(binding, mapping):
                    self._incoming[_endpoint_key(segment.target)].append(segment)
                    self._outgoing[_endpoint_key(segment.source)].append(segment)
        for instance in self.ir.instances:
            definition = self._definitions[instance.definition_id]
            for assignment in definition.assignments:
                self._writes[(instance.path, assignment.target.symbol)].append(
                    assignment
                )
                for dependency in assignment.dependencies:
                    self._reads[(instance.path, dependency.source.symbol)].append(
                        (assignment, dependency)
                    )

    @staticmethod
    def _oriented_segments(
        binding: PortBinding,
        mapping: BitMapping,
    ) -> Iterable[_FlowSegment]:
        actual = mapping.source
        formal = mapping.target
        if binding.direction is PortDirection.INPUT:
            yield _FlowSegment(source=actual, target=formal, binding=binding)
        elif binding.direction is PortDirection.OUTPUT:
            yield _FlowSegment(source=formal, target=actual, binding=binding)
        elif binding.direction is PortDirection.INOUT:
            yield _FlowSegment(source=actual, target=formal, binding=binding)
            yield _FlowSegment(source=formal, target=actual, binding=binding)
        elif binding.style in {BindingStyle.INTERFACE, BindingStyle.MODPORT}:
            raise ValueError(
                f"interface binding {binding.binding_id} must use member input/output/inout direction"
            )
        else:
            raise ValueError(f"unsupported binding direction {binding.direction}")

    def _resolve_symbol_range(self, instance_path: str, symbol: str):
        instance = self._instances[instance_path]
        definition = self._definitions[instance.definition_id]
        direct = definition.direct_signal_range(symbol)
        if direct is not None:
            return direct
        if "." not in symbol:
            return None
        port_name, member_name = symbol.split(".", 1)
        port = definition.port(port_name)
        if port is None or not port.interface_definition:
            return None
        interface_definition = self._definition_by_name(port.interface_definition)
        return interface_definition.direct_signal_range(member_name)

    def _definition_by_name(self, name: str) -> DefinitionTemplate:
        matches = [item for item in self.ir.definitions if item.name == name]
        if len(matches) != 1:
            raise KeyError(f"interface definition {name!r} is not uniquely available")
        return matches[0]

    def _driver_match(
        self,
        assignment: AssignmentFact,
        instance_path: str,
        selected_target: SignalSelection,
        traversal: tuple[TraversalHop, ...],
    ) -> QueryMatch:
        dependencies = tuple(
            _bound_dependency(dependency, instance_path)
            for dependency in assignment.dependencies
            if _overlaps(dependency.target.bits, selected_target.bits)
        )
        return QueryMatch(
            instance_path=instance_path,
            fact_id=assignment.assignment_id,
            kind=assignment.kind,
            target=assignment.target.bind(instance_path),
            source=None,
            dependencies=dependencies,
            dependency_role=None,
            boundary=assignment.boundary,
            procedure_kind=assignment.procedure_kind,
            guard=assignment.guard,
            generate_scope=assignment.generate_scope,
            evidence=assignment.evidence,
            traversal=traversal,
            confidence=_base_confidence(assignment.evidence, traversal),
        )

    def _load_match(
        self,
        assignment: AssignmentFact,
        dependency: DependencyFact,
        instance_path: str,
        selected_source: SignalSelection,
        traversal: tuple[TraversalHop, ...],
    ) -> QueryMatch:
        bound = _bound_dependency(dependency, instance_path)
        source_bits = tuple(
            bit for bit in bound.source.bits if bit in set(selected_source.bits)
        )
        source = SignalSelection(
            instance_path=instance_path,
            symbol=bound.source.symbol,
            bits=source_bits,
        )
        return QueryMatch(
            instance_path=instance_path,
            fact_id=assignment.assignment_id,
            kind=assignment.kind,
            target=assignment.target.bind(instance_path),
            source=source,
            dependencies=(bound,),
            dependency_role=dependency.role.value,
            boundary=assignment.boundary,
            procedure_kind=assignment.procedure_kind,
            guard=assignment.guard,
            generate_scope=assignment.generate_scope,
            evidence=assignment.evidence,
            traversal=traversal,
            confidence=_base_confidence(assignment.evidence, traversal),
        )

    def _finish_result(
        self,
        *,
        operation: str,
        signal: SignalSelection,
        matches: list[QueryMatch],
        touched_paths: set[str],
        traversed: int,
        depth_limited: bool,
        max_depth: int,
    ) -> ConnectivityQueryResult:
        gaps = _relevant_gaps(self.ir.coverage.gaps, touched_paths)
        if depth_limited:
            gaps = gaps + (
                CoverageGap(
                    code="query_depth_limit",
                    message=f"query traversal reached max_depth={max_depth}",
                    impact=CoverageStatus.INCONCLUSIVE,
                    scopes=(signal.path(),),
                ),
            )
        coverage = _query_coverage(self.ir.coverage, gaps)
        deduped = _dedupe_matches(matches)
        if deduped:
            status = QueryStatus.FOUND
            if coverage is not CoverageStatus.COMPLETE:
                deduped = tuple(
                    _with_confidence(match, QueryConfidence.PARTIAL)
                    for match in deduped
                )
        elif coverage is CoverageStatus.COMPLETE:
            status = QueryStatus.NOT_CONNECTED
        else:
            status = QueryStatus.INCONCLUSIVE
        return ConnectivityQueryResult(
            operation=operation,
            signal=signal,
            status=status,
            coverage_status=coverage,
            matches=deduped,
            unresolved_boundaries=gaps,
            traversed_binding_edges=traversed,
            max_depth=max_depth,
        )


def _endpoint_key(selection: SignalSelection) -> tuple[str, str]:
    if selection.instance_path is None:
        raise ValueError("query endpoint must be hierarchy-bound")
    return selection.instance_path, selection.symbol


def _state_key(selection: SignalSelection) -> tuple[str, str, tuple[int, ...]]:
    instance, symbol = _endpoint_key(selection)
    return instance, symbol, selection.bits


def _overlaps(left: tuple[int, ...], right: tuple[int, ...]) -> bool:
    return not set(left).isdisjoint(right)


def _map_selection(
    *,
    selected: SignalSelection,
    mapped_from: SignalSelection,
    mapped_to: SignalSelection,
) -> SignalSelection:
    bit_map = dict(zip(mapped_from.bits, mapped_to.bits))
    selected_bits = tuple(bit for bit in selected.bits if bit in bit_map)
    if not selected_bits:
        raise ValueError("selected bits do not overlap mapping")
    return SignalSelection(
        instance_path=mapped_to.instance_path,
        symbol=mapped_to.symbol,
        bits=tuple(bit_map[bit] for bit in selected_bits),
    )


def _hop(segment: _FlowSegment) -> TraversalHop:
    return TraversalHop(
        edge_kind=segment.binding.edge_kind,
        source=segment.source,
        target=segment.target,
        binding_id=segment.binding.binding_id,
        binding_style=segment.binding.style,
        evidence=segment.binding.evidence,
    )


def _bound_dependency(
    dependency: DependencyFact,
    instance_path: str,
) -> QueryDependency:
    return QueryDependency(
        source=dependency.source.bind(instance_path),
        target=dependency.target.bind(instance_path),
        role=dependency.role.value,
        exact_bit_mapping=dependency.exact_bit_mapping,
        guard=dependency.guard,
    )


def _base_confidence(
    evidence: SourceEvidence,
    traversal: tuple[TraversalHop, ...],
) -> QueryConfidence:
    resolutions = [evidence.resolution, *(hop.evidence.resolution for hop in traversal)]
    if ResolutionKind.UNRESOLVED in resolutions:
        return QueryConfidence.PARTIAL
    if ResolutionKind.CONDITIONAL in resolutions:
        return QueryConfidence.CONDITIONAL
    return QueryConfidence.EXACT_SOURCE


def _relevant_gaps(
    gaps: tuple[CoverageGap, ...],
    touched_paths: set[str],
) -> tuple[CoverageGap, ...]:
    return tuple(
        gap for gap in gaps if any(gap.affects(path) for path in touched_paths)
    )


def _query_coverage(
    report: CoverageReport,
    gaps: tuple[CoverageGap, ...],
) -> CoverageStatus:
    if any(gap.impact is CoverageStatus.INCONCLUSIVE for gap in gaps):
        return CoverageStatus.INCONCLUSIVE
    if gaps:
        return CoverageStatus.PARTIAL
    if report.status is CoverageStatus.COMPLETE:
        return CoverageStatus.COMPLETE
    # A globally partial projection can still establish a complete result for
    # a cone that is disjoint from every explicitly scoped gap.  Missing files
    # or a gap without a scope remain global and therefore never take this path.
    if (
        report.status is CoverageStatus.PARTIAL
        and report.files_projected == report.files_total
        and report.gaps
        and all(gap.scopes and "*" not in gap.scopes for gap in report.gaps)
    ):
        return CoverageStatus.COMPLETE
    if report.status is CoverageStatus.INCONCLUSIVE:
        return CoverageStatus.INCONCLUSIVE
    if report.status is CoverageStatus.PARTIAL:
        return CoverageStatus.PARTIAL
    return CoverageStatus.COMPLETE


def _dedupe_matches(matches: list[QueryMatch]) -> tuple[QueryMatch, ...]:
    by_key: dict[tuple[str, str, str, tuple[int, ...], str | None], QueryMatch] = {}
    for match in matches:
        key = (
            match.instance_path,
            match.fact_id,
            match.target.symbol,
            match.target.bits,
            match.dependency_role,
        )
        current = by_key.get(key)
        if current is None or len(match.traversal) < len(current.traversal):
            by_key[key] = match
    return tuple(
        sorted(
            by_key.values(),
            key=lambda item: (
                item.instance_path,
                item.evidence.location.file,
                item.evidence.location.line,
                item.fact_id,
                item.dependency_role or "",
            ),
        )
    )


def _with_confidence(match: QueryMatch, confidence: QueryConfidence) -> QueryMatch:
    return QueryMatch(
        instance_path=match.instance_path,
        fact_id=match.fact_id,
        kind=match.kind,
        target=match.target,
        source=match.source,
        dependencies=match.dependencies,
        dependency_role=match.dependency_role,
        boundary=match.boundary,
        procedure_kind=match.procedure_kind,
        guard=match.guard,
        generate_scope=match.generate_scope,
        evidence=match.evidence,
        traversal=match.traversal,
        confidence=confidence,
    )


def _enum_values(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, SourceLocation):
        return _enum_values(asdict(value))
    if isinstance(value, dict):
        return {key: _enum_values(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_enum_values(item) for item in value]
    return value
