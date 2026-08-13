"""Internal structural connectivity queries over :mod:`connectivity_ir`.

Port and interface bindings are transparent hierarchy edges. Source assignments
are terminal driver/consumer facts for driver/load queries and directed
combinational edges for path queries. Sequential boundaries are never crossed.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from enum import Enum
import re
from typing import Any, Iterable

from .cancellation import check_cancelled
from .connectivity_ir import (
    AssignmentFact,
    BindingSourceKind,
    BindingStyle,
    BitMapping,
    BoundaryKind,
    ConnectivityIR,
    CoverageGap,
    CoverageReport,
    CoverageStatus,
    DefinitionTemplate,
    DependencyFact,
    DependencyRole,
    EdgeKind,
    PortBinding,
    PortDirection,
    ResolutionKind,
    SignalSelection,
    SourceEvidence,
    SourceLocation,
)
from .source_graph_contract import (
    DEFAULT_PATH_OUTPUT_LIMIT,
    DEFAULT_PATH_TRAVERSAL_LIMIT,
)


class QueryStatus(str, Enum):
    FOUND = "found"
    NOT_CONNECTED = "not_connected"
    INCONCLUSIVE = "inconclusive"


class QueryConfidence(str, Enum):
    EXACT_SOURCE = "exact_source"
    CONDITIONAL = "conditional"
    PARTIAL = "partial"


class PathQueryStatus(str, Enum):
    FOUND = "found"
    NOT_CONNECTED = "not_connected"
    FROM_UNRESOLVED = "from_unresolved"
    TO_UNRESOLVED = "to_unresolved"
    ENDPOINTS_UNRESOLVED = "endpoints_unresolved"
    INCONCLUSIVE = "inconclusive"
    TRUNCATED = "truncated"


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
class PathTraversalEdge:
    edge_id: str
    edge_kind: EdgeKind
    source: SignalSelection
    target: SignalSelection
    evidence: SourceEvidence
    exact_bit_mapping: bool
    binding_style: BindingStyle | None = None
    dependency_role: str | None = None
    boundary: BoundaryKind | None = None
    procedure_kind: str | None = None


@dataclass(frozen=True)
class ConnectivityPathQueryResult:
    operation: str
    from_signal: str
    to_signal: str
    from_endpoint: SignalSelection | None
    to_endpoint: SignalSelection | None
    status: PathQueryStatus
    coverage_status: CoverageStatus
    path: tuple[PathTraversalEdge, ...]
    unresolved_boundaries: tuple[CoverageGap, ...]
    endpoint_alias_equivalent: bool
    expand_assigns: bool
    traversed_edge_count: int
    visited_state_count: int
    traversal_limit: int
    output_limit: int
    traversal_truncated: bool
    output_truncated: bool

    def to_dict(self) -> dict[str, Any]:
        return _enum_values(asdict(self))


@dataclass(frozen=True)
class _FlowSegment:
    source: SignalSelection
    target: SignalSelection
    binding: PortBinding


@dataclass(frozen=True)
class _PathEdge:
    edge_id: str
    edge_kind: EdgeKind
    source: SignalSelection
    target: SignalSelection
    evidence: SourceEvidence
    exact_bit_mapping: bool
    binding_style: BindingStyle | None = None
    dependency_role: str | None = None
    boundary: BoundaryKind | None = None
    procedure_kind: str | None = None


_TRAILING_SELECT_RE = re.compile(
    r"^(?P<base>.+?)(?:\[(?P<left>-?\d+)(?::(?P<right>-?\d+))?\])?$"
)


class ConnectivityQueryEngine:
    """Build lightweight indexes and answer driver/load/path queries."""

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
        self._path_outgoing: dict[tuple[str, str], list[_PathEdge]] = defaultdict(list)
        self._path_exclusions: dict[
            tuple[str, str], list[tuple[SignalSelection, CoverageGap]]
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

    def query_path(
        self,
        from_signal: str,
        to_signal: str,
        *,
        expand_assigns: bool = False,
        traversal_limit: int = DEFAULT_PATH_TRAVERSAL_LIMIT,
        output_limit: int = DEFAULT_PATH_OUTPUT_LIMIT,
    ) -> ConnectivityPathQueryResult:
        """Return the deterministic shortest structural data-flow path.

        Port bindings and combinational assignment dependencies are directed
        structural edges.  Sequential and control-only dependencies are
        deliberately not crossed; if one is encountered while searching for a
        negative result it remains an explicit inconclusive boundary.
        """

        if not isinstance(expand_assigns, bool):
            raise ValueError("expand_assigns must be boolean")
        if (
            not isinstance(traversal_limit, int)
            or isinstance(traversal_limit, bool)
            or traversal_limit < 1
        ):
            raise ValueError("traversal_limit must be a positive integer")
        if (
            not isinstance(output_limit, int)
            or isinstance(output_limit, bool)
            or output_limit < 1
        ):
            raise ValueError("output_limit must be a positive integer")
        check_cancelled()

        from_endpoint, from_error = self._try_resolve_path_endpoint(
            from_signal, label="from"
        )
        to_endpoint, to_error = self._try_resolve_path_endpoint(to_signal, label="to")
        endpoint_gaps = tuple(gap for gap in (from_error, to_error) if gap is not None)
        if from_endpoint is None or to_endpoint is None:
            if from_endpoint is None and to_endpoint is None:
                status = PathQueryStatus.ENDPOINTS_UNRESOLVED
            elif from_endpoint is None:
                status = PathQueryStatus.FROM_UNRESOLVED
            else:
                status = PathQueryStatus.TO_UNRESOLVED
            return ConnectivityPathQueryResult(
                operation="path",
                from_signal=from_signal,
                to_signal=to_signal,
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
                status=status,
                coverage_status=CoverageStatus.INCONCLUSIVE,
                path=(),
                unresolved_boundaries=endpoint_gaps,
                endpoint_alias_equivalent=False,
                expand_assigns=expand_assigns,
                traversed_edge_count=0,
                visited_state_count=0,
                traversal_limit=traversal_limit,
                output_limit=output_limit,
                traversal_truncated=False,
                output_truncated=False,
            )

        if _same_path_endpoint(from_endpoint, to_endpoint):
            coverage_gaps = _relevant_gaps(
                self.ir.coverage.gaps,
                {from_endpoint.path(), to_endpoint.path()},
            )
            return ConnectivityPathQueryResult(
                operation="path",
                from_signal=from_signal,
                to_signal=to_signal,
                from_endpoint=from_endpoint,
                to_endpoint=to_endpoint,
                status=PathQueryStatus.FOUND,
                coverage_status=_query_coverage(self.ir.coverage, coverage_gaps),
                path=(),
                unresolved_boundaries=coverage_gaps,
                endpoint_alias_equivalent=True,
                expand_assigns=expand_assigns,
                traversed_edge_count=0,
                visited_state_count=1,
                traversal_limit=traversal_limit,
                output_limit=output_limit,
                traversal_truncated=False,
                output_truncated=False,
            )

        queue: deque[tuple[SignalSelection, tuple[PathTraversalEdge, ...]]] = deque(
            [(from_endpoint, ())]
        )
        visited = {_state_key(from_endpoint)}
        touched_paths = {from_endpoint.path(), to_endpoint.path()}
        query_gaps: dict[tuple[str, str, int, str], CoverageGap] = {}
        traversed_edges = 0
        traversal_truncated = False

        while queue and not traversal_truncated:
            check_cancelled()
            current, current_path = queue.popleft()
            touched_paths.add(current.path())
            for excluded_source, gap in self._path_exclusions.get(
                _endpoint_key(current), ()
            ):
                if _overlaps(excluded_source.bits, current.bits):
                    query_gaps[_gap_key(gap)] = gap

            edges = self._path_outgoing.get(_endpoint_key(current), ())
            for edge in edges:
                check_cancelled()
                if not _overlaps(edge.source.bits, current.bits):
                    continue
                if traversed_edges >= traversal_limit:
                    traversal_truncated = True
                    break
                traversed_edges += 1
                downstream = _follow_path_edge(current, edge)
                touched_paths.add(downstream.path())
                path_gap = None
                if edge.evidence.resolution is ResolutionKind.UNRESOLVED:
                    path_gap = CoverageGap(
                        code="path_edge_evidence_unresolved",
                        message="path edge source evidence is unresolved",
                        impact=CoverageStatus.INCONCLUSIVE,
                        scopes=(edge.source.path(), edge.target.path()),
                        location=edge.evidence.location,
                    )
                    query_gaps[_gap_key(path_gap)] = path_gap
                hop = _public_path_edge(edge, current, downstream)
                next_path = current_path + (hop,)
                if _same_path_endpoint(downstream, to_endpoint):
                    path_touched = {
                        from_endpoint.path(),
                        to_endpoint.path(),
                        *(
                            endpoint.path()
                            for path_hop in next_path
                            for endpoint in (path_hop.source, path_hop.target)
                        ),
                    }
                    relevant_gaps = _merge_path_gaps(
                        self.ir.coverage,
                        path_touched,
                        _path_evidence_gaps(next_path),
                    )
                    output_truncated = len(next_path) > output_limit
                    target_state = _state_key(downstream)
                    return ConnectivityPathQueryResult(
                        operation="path",
                        from_signal=from_signal,
                        to_signal=to_signal,
                        from_endpoint=from_endpoint,
                        to_endpoint=to_endpoint,
                        status=(
                            PathQueryStatus.TRUNCATED
                            if output_truncated
                            else PathQueryStatus.FOUND
                        ),
                        coverage_status=(
                            CoverageStatus.INCONCLUSIVE
                            if output_truncated
                            else _query_coverage(self.ir.coverage, relevant_gaps)
                        ),
                        path=() if output_truncated else next_path,
                        unresolved_boundaries=(
                            relevant_gaps
                            + (
                                CoverageGap(
                                    code="path_output_limit",
                                    message=(
                                        "shortest path exceeds the internal output limit"
                                    ),
                                    impact=CoverageStatus.INCONCLUSIVE,
                                    scopes=(from_endpoint.path(), to_endpoint.path()),
                                ),
                            )
                            if output_truncated
                            else relevant_gaps
                        ),
                        endpoint_alias_equivalent=False,
                        expand_assigns=expand_assigns,
                        traversed_edge_count=traversed_edges,
                        visited_state_count=len(visited | {target_state}),
                        traversal_limit=traversal_limit,
                        output_limit=output_limit,
                        traversal_truncated=False,
                        output_truncated=output_truncated,
                    )
                state = _state_key(downstream)
                if state in visited:
                    continue
                visited.add(state)
                queue.append((downstream, next_path))

        relevant_gaps = _merge_path_gaps(
            self.ir.coverage,
            touched_paths,
            query_gaps.values(),
        )
        if traversal_truncated:
            truncation_gap = CoverageGap(
                code="path_traversal_limit",
                message="path search reached the internal traversal edge limit",
                impact=CoverageStatus.INCONCLUSIVE,
                scopes=(from_endpoint.path(), to_endpoint.path()),
            )
            relevant_gaps = relevant_gaps + (truncation_gap,)
            coverage = CoverageStatus.INCONCLUSIVE
            status = PathQueryStatus.TRUNCATED
        else:
            coverage = _query_coverage(self.ir.coverage, relevant_gaps)
            status = (
                PathQueryStatus.NOT_CONNECTED
                if coverage is CoverageStatus.COMPLETE
                else PathQueryStatus.INCONCLUSIVE
            )
        return ConnectivityPathQueryResult(
            operation="path",
            from_signal=from_signal,
            to_signal=to_signal,
            from_endpoint=from_endpoint,
            to_endpoint=to_endpoint,
            status=status,
            coverage_status=coverage,
            path=(),
            unresolved_boundaries=relevant_gaps,
            endpoint_alias_equivalent=False,
            expand_assigns=expand_assigns,
            traversed_edge_count=traversed_edges,
            visited_state_count=len(visited),
            traversal_limit=traversal_limit,
            output_limit=output_limit,
            traversal_truncated=traversal_truncated,
            output_truncated=False,
        )

    def _try_resolve_path_endpoint(
        self, signal_path: str, *, label: str
    ) -> tuple[SignalSelection | None, CoverageGap | None]:
        if label not in {"from", "to"}:
            raise ValueError("path endpoint label must be from or to")
        try:
            return self.resolve_signal(signal_path), None
        except (KeyError, ValueError):
            return None, CoverageGap(
                code=f"path_{label}_endpoint_unresolved",
                message=f"path {label} endpoint is absent from the bounded IR",
                impact=CoverageStatus.INCONCLUSIVE,
                scopes=(signal_path,),
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
                if mapping.source_kind is not BindingSourceKind.SIGNAL:
                    continue
                for segment in self._oriented_segments(binding, mapping):
                    self._incoming[_endpoint_key(segment.target)].append(segment)
                    self._outgoing[_endpoint_key(segment.source)].append(segment)
                    self._path_outgoing[_endpoint_key(segment.source)].append(
                        _PathEdge(
                            edge_id=segment.binding.binding_id,
                            edge_kind=segment.binding.edge_kind,
                            source=segment.source,
                            target=segment.target,
                            evidence=segment.binding.evidence,
                            exact_bit_mapping=True,
                            binding_style=segment.binding.style,
                        )
                    )
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
                    source = dependency.source.bind(instance.path)
                    target = dependency.target.bind(instance.path)
                    if assignment.boundary is BoundaryKind.SEQUENTIAL:
                        self._path_exclusions[_endpoint_key(source)].append(
                            (
                                source,
                                CoverageGap(
                                    code="sequential_boundary",
                                    message=(
                                        "path traversal does not cross sequential state"
                                    ),
                                    impact=CoverageStatus.INCONCLUSIVE,
                                    scopes=(source.path(), target.path()),
                                    location=assignment.evidence.location,
                                ),
                            )
                        )
                        continue
                    if dependency.role is DependencyRole.CONTROL:
                        self._path_exclusions[_endpoint_key(source)].append(
                            (
                                source,
                                CoverageGap(
                                    code="control_dependency_excluded",
                                    message=(
                                        "path traversal excludes control-only dependencies"
                                    ),
                                    impact=CoverageStatus.INCONCLUSIVE,
                                    scopes=(source.path(), target.path()),
                                    location=assignment.evidence.location,
                                ),
                            )
                        )
                        continue
                    self._path_outgoing[_endpoint_key(source)].append(
                        _PathEdge(
                            edge_id=assignment.assignment_id,
                            edge_kind=assignment.kind,
                            source=source,
                            target=target,
                            evidence=assignment.evidence,
                            exact_bit_mapping=dependency.exact_bit_mapping,
                            dependency_role=dependency.role.value,
                            boundary=assignment.boundary,
                            procedure_kind=assignment.procedure_kind,
                        )
                    )
        for edges in self._path_outgoing.values():
            edges.sort(key=_path_edge_sort_key)

    @staticmethod
    def _oriented_segments(
        binding: PortBinding,
        mapping: BitMapping,
    ) -> Iterable[_FlowSegment]:
        actual = mapping.source
        formal = mapping.target
        if actual is None:
            raise ValueError("signal binding mapping is missing its source")
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
        exact = self._definitions.get(name)
        if exact is not None:
            return exact
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


def _same_path_endpoint(left: SignalSelection, right: SignalSelection) -> bool:
    return _endpoint_key(left) == _endpoint_key(right) and _overlaps(
        left.bits, right.bits
    )


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


def _follow_path_edge(
    selected: SignalSelection,
    edge: _PathEdge,
) -> SignalSelection:
    if edge.exact_bit_mapping:
        return _map_selection(
            selected=selected,
            mapped_from=edge.source,
            mapped_to=edge.target,
        )
    return edge.target


def _public_path_edge(
    edge: _PathEdge,
    source: SignalSelection,
    target: SignalSelection,
) -> PathTraversalEdge:
    return PathTraversalEdge(
        edge_id=edge.edge_id,
        edge_kind=edge.edge_kind,
        source=source,
        target=target,
        evidence=edge.evidence,
        exact_bit_mapping=edge.exact_bit_mapping,
        binding_style=edge.binding_style,
        dependency_role=edge.dependency_role,
        boundary=edge.boundary,
        procedure_kind=edge.procedure_kind,
    )


def _path_edge_sort_key(edge: _PathEdge) -> tuple[Any, ...]:
    return (
        edge.target.instance_path or "",
        edge.target.symbol,
        edge.target.bits,
        edge.edge_kind.value,
        edge.edge_id,
        edge.source.bits,
        edge.evidence.location.file,
        edge.evidence.location.line,
    )


def _gap_key(gap: CoverageGap) -> tuple[str, str, int, str]:
    location = gap.location
    return (
        gap.code,
        location.file if location is not None else "",
        location.line if location is not None else 0,
        "\x1f".join(gap.scopes),
    )


def _merge_path_gaps(
    coverage: CoverageReport,
    touched_paths: set[str],
    query_gaps: Iterable[CoverageGap],
) -> tuple[CoverageGap, ...]:
    by_key = {
        _gap_key(gap): gap for gap in _relevant_gaps(coverage.gaps, touched_paths)
    }
    for gap in query_gaps:
        by_key[_gap_key(gap)] = gap
    return tuple(by_key[key] for key in sorted(by_key))


def _path_evidence_gaps(
    path: tuple[PathTraversalEdge, ...],
) -> tuple[CoverageGap, ...]:
    gaps: list[CoverageGap] = []
    for edge in path:
        if edge.evidence.resolution is ResolutionKind.UNRESOLVED:
            gaps.append(
                CoverageGap(
                    code="path_edge_evidence_unresolved",
                    message="path edge source evidence is unresolved",
                    impact=CoverageStatus.INCONCLUSIVE,
                    scopes=(edge.source.path(), edge.target.path()),
                    location=edge.evidence.location,
                )
            )
        if not edge.exact_bit_mapping:
            gaps.append(
                CoverageGap(
                    code="path_bit_mapping_inexact",
                    message=(
                        "structural dependency is known but its exact bit mapping "
                        "is not available"
                    ),
                    impact=CoverageStatus.PARTIAL,
                    scopes=(edge.source.path(), edge.target.path()),
                    location=edge.evidence.location,
                )
            )
    return tuple(gaps)


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
