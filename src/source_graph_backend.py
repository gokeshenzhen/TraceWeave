"""Prepared Source Graph backend and conservative public result mapping.

The runtime owns preparation/cache lifecycle; this module consumes exactly one
prepared cache entry and emits driver/load/path facts from its Connectivity IR. It
does not invoke NPI or Legacy Static, so a returned result has one provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any

from .connectivity_ir import CoverageStatus, EdgeKind, ResolutionKind, SignalSelection
from .connectivity_query import (
    ConnectivityPathQueryResult,
    ConnectivityQueryResult,
    PathQueryStatus,
    QueryConfidence,
    QueryMatch,
    QueryStatus,
    SignalResolutionError,
)
from .source_graph_runtime import SourceGraphCacheEntry


_DISPLAY_SIGNAL_RE = re.compile(r"^(?P<base>.+?)(?:\[-?\d+(?::-?\d+)?\])?$")


@dataclass(frozen=True)
class SourceGraphQueryBlocked(Exception):
    """Fixed-label query blocker that is safe to expose in routing receipts."""

    code: str


def _public_confidence(confidence: QueryConfidence) -> str:
    return {
        QueryConfidence.EXACT_SOURCE: "exact",
        QueryConfidence.CONDITIONAL: "conditional",
        QueryConfidence.PARTIAL: "partial",
    }[confidence]


def _aggregate_confidence(matches: tuple[QueryMatch, ...]) -> str | None:
    values = {match.confidence for match in matches}
    if QueryConfidence.PARTIAL in values:
        return "partial"
    if QueryConfidence.CONDITIONAL in values:
        return "conditional"
    if QueryConfidence.EXACT_SOURCE in values:
        return "exact"
    return None


def _aggregate_positive_fact_confidence(
    matches: tuple[QueryMatch, ...],
) -> str | None:
    values = {match.positive_fact_confidence or match.confidence for match in matches}
    if QueryConfidence.PARTIAL in values:
        return "partial"
    if QueryConfidence.CONDITIONAL in values:
        return "conditional"
    if QueryConfidence.EXACT_SOURCE in values:
        return "exact"
    return None


def _target_bit_coverage(result: ConnectivityQueryResult) -> str:
    if not result.resolved_bits:
        return "none"
    if result.unresolved_bits:
        return "partial"
    return "complete"


def _query_claim_semantics(result: ConnectivityQueryResult) -> dict[str, Any]:
    # QueryStatus/CoverageStatus already account for unresolved boundaries and
    # depth limits. ``frontiers`` is also populated for a complete negative so
    # a later bounded artifact may choose to expand; it is not itself evidence
    # that the current query was non-exhaustive.
    search_exhaustive = result.coverage_status is CoverageStatus.COMPLETE
    positive_found = result.status is QueryStatus.FOUND and bool(result.matches)
    exclusive_driver_proved: bool | None = None
    if result.operation == "driver" and positive_found:
        # True means every requested bit has an exhaustive driver set and no
        # bit has overlapping drivers. Non-overlapping bit segments may still
        # have different drivers.
        exclusive_driver_proved = bool(
            search_exhaustive
            and not result.unresolved_bits
            and not result.multi_driver_bits
            and len(result.resolved_bits) == result.signal.width
        )
    negative_claim_allowed = bool(
        result.status is QueryStatus.NOT_CONNECTED and search_exhaustive
    )
    return {
        "positive_fact_confidence": (
            _aggregate_positive_fact_confidence(result.matches)
            if positive_found
            else None
        ),
        "target_bit_coverage": _target_bit_coverage(result),
        "global_coverage_status": result.coverage_status.value,
        "exhaustive_search": search_exhaustive,
        "exclusive_driver_proved": exclusive_driver_proved,
        "negative_claim_allowed": negative_claim_allowed,
    }


def _gap_codes(result: ConnectivityQueryResult) -> list[str]:
    return sorted({gap.code for gap in result.unresolved_boundaries})


def _query_receipt(
    result: ConnectivityQueryResult,
    *,
    claim_semantics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "coverage_status": result.coverage_status.value,
        "confidence": _aggregate_confidence(result.matches),
        "match_count": len(result.matches),
        "unresolved_boundary_codes": _gap_codes(result),
        "traversed_binding_edges": result.traversed_binding_edges,
        "max_depth": result.max_depth,
        "visited_state_count": result.visited_state_count,
        "inspected_edge_count": result.inspected_edge_count,
        "state_limit": result.state_limit,
        "edge_limit": result.edge_limit,
        "match_limit": result.match_limit,
        "frontier_limit": result.frontier_limit,
        "state_truncated": result.state_truncated,
        "edge_truncated": result.edge_truncated,
        "match_truncated": result.match_truncated,
        "frontier_truncated": result.frontier_truncated,
        "query_truncated": result.truncated,
        "queried_bit_count": result.signal.width,
        "resolved_bit_count": len(result.resolved_bits),
        "unresolved_bit_count": len(result.unresolved_bits),
        "constant_bit_count": len(result.constant_bits),
        "multi_driver_bit_count": len(result.multi_driver_bits),
        "claim_semantics": claim_semantics,
        # Internal orchestration input. The server removes this before merging
        # the privacy-safe public receipt or recording telemetry.
        "expansion_frontiers": [
            frontier.signal.path(include_bits=True) for frontier in result.frontiers
        ],
    }


def _constant_text(bits: tuple[str, ...]) -> str:
    return f"{len(bits)}'b{''.join(bits)}"


def _driver_bit_provenance(result: ConnectivityQueryResult) -> list[dict[str, Any]]:
    multiple = set(result.multi_driver_bits)
    segments: list[dict[str, Any]] = []
    for match in result.matches:
        is_constant = match.kind is EdgeKind.CONSTANT_DRIVER
        source_path = None
        if not is_constant and match.traversal:
            source_path = match.traversal[0].source.path(include_bits=True)
        segments.append(
            {
                "target_path": match.covered_signal.path(include_bits=True),
                "source_kind": "constant" if is_constant else "signal",
                "source_path": source_path,
                "terminal_path": match.target.path(include_bits=True),
                "constant_value": (
                    _constant_text(match.constant_bits) if is_constant else None
                ),
                "driver_kind": match.kind.value,
                "source_file": match.evidence.location.file,
                "source_line": match.evidence.location.line,
                "confidence": _public_confidence(match.confidence),
                "multiple_driver": bool(
                    multiple.intersection(match.covered_signal.bits)
                ),
            }
        )
    if result.unresolved_bits:
        unresolved = SignalSelection(
            instance_path=result.signal.instance_path,
            symbol=result.signal.symbol,
            bits=result.unresolved_bits,
        )
        segments.append(
            {
                "target_path": unresolved.path(include_bits=True),
                "source_kind": "unresolved",
                "source_path": None,
                "terminal_path": None,
                "constant_value": None,
                "driver_kind": None,
                "source_file": None,
                "source_line": None,
                "confidence": "partial",
                "multiple_driver": False,
            }
        )
    return segments


def _requested_symbol(signal_path: str, instance_path: str | None) -> str:
    match = _DISPLAY_SIGNAL_RE.fullmatch(signal_path.strip())
    base = match.group("base") if match is not None else signal_path.strip()
    prefix = f"{instance_path}." if instance_path else ""
    return base[len(prefix) :] if prefix and base.startswith(prefix) else base


def _path_confidence(result: ConnectivityPathQueryResult) -> str | None:
    if result.status is not PathQueryStatus.FOUND:
        return "exact" if result.status is PathQueryStatus.NOT_CONNECTED else None
    if result.coverage_status is not CoverageStatus.COMPLETE:
        return "partial"
    resolutions = {edge.evidence.resolution for edge in result.path}
    if ResolutionKind.UNRESOLVED in resolutions:
        return "partial"
    if ResolutionKind.CONDITIONAL in resolutions:
        return "conditional"
    return "exact"


def _path_positive_fact_confidence(
    result: ConnectivityPathQueryResult,
) -> str | None:
    if result.status is not PathQueryStatus.FOUND:
        return None
    if any(not edge.exact_bit_mapping for edge in result.path):
        return "partial"
    resolutions = {edge.evidence.resolution for edge in result.path}
    if ResolutionKind.UNRESOLVED in resolutions:
        return "partial"
    if ResolutionKind.CONDITIONAL in resolutions:
        return "conditional"
    return "exact"


def _path_claim_semantics(
    result: ConnectivityPathQueryResult,
) -> dict[str, Any]:
    negative_claim_allowed = bool(
        result.status is PathQueryStatus.NOT_CONNECTED
        and result.coverage_status is CoverageStatus.COMPLETE
        and not result.traversal_truncated
        and not result.output_truncated
    )
    return {
        "positive_fact_confidence": _path_positive_fact_confidence(result),
        "target_bit_coverage": "not_applicable",
        "global_coverage_status": result.coverage_status.value,
        # Positive path search returns the first proved path. It is exhaustive
        # only for a complete, non-truncated negative result.
        "exhaustive_search": negative_claim_allowed,
        "exclusive_driver_proved": None,
        "negative_claim_allowed": negative_claim_allowed,
    }


def _path_query_receipt(
    result: ConnectivityPathQueryResult,
    *,
    claim_semantics: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "coverage_status": result.coverage_status.value,
        "confidence": _path_confidence(result),
        "match_count": int(result.status is PathQueryStatus.FOUND),
        "unresolved_boundary_codes": sorted(
            {gap.code for gap in result.unresolved_boundaries}
        ),
        "path_edge_count": len(result.path),
        "traversed_edge_count": result.traversed_edge_count,
        "visited_state_count": result.visited_state_count,
        "traversal_limit": result.traversal_limit,
        "output_limit": result.output_limit,
        "traversal_truncated": result.traversal_truncated,
        "output_truncated": result.output_truncated,
        "endpoint_alias_equivalent": result.endpoint_alias_equivalent,
        "expand_assigns": result.expand_assigns,
        "claim_semantics": claim_semantics,
    }


class SourceGraphConnectivityBackend:
    """One-provenance driver/load/path queries over a prepared graph entry."""

    name = "source_graph"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, entry: SourceGraphCacheEntry) -> None:
        self._entry = entry
        self._unprojected_instance_candidates: tuple[str, ...] = ()

    def set_unprojected_instance_candidates(self, candidates: tuple[str, ...]) -> None:
        """Attach query-local scope hints without changing artifact identity."""

        self._unprojected_instance_candidates = tuple(
            dict.fromkeys(str(path) for path in candidates)
        )

    def find_driver(
        self,
        signal_path: str,
        wave_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        recursive: bool = False,
        max_depth: int = 10,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        del compile_log, top_hint, simulator
        try:
            query = self._entry.query_engine.query_driver(
                signal_path,
                max_depth=max_depth,
                unprojected_instance_candidates=(self._unprojected_instance_candidates),
            )
        except SignalResolutionError as exc:
            raise SourceGraphQueryBlocked(exc.code) from exc
        claim_semantics = _query_claim_semantics(query)
        result = self._map_driver(
            query,
            wave_path=wave_path,
            recursive=recursive,
            requested_signal_path=signal_path,
            claim_semantics=claim_semantics,
        )
        result["_source_graph_query_receipt"] = _query_receipt(
            query,
            claim_semantics=claim_semantics,
        )
        return result

    def find_loads(
        self,
        signal_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        max_depth: int = 1,
        include_expr: bool = True,
        kind_filter: list[str] | None = None,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        del compile_log, top_hint, simulator
        if kind_filter is not None and "rhs_expr" not in kind_filter:
            raise SourceGraphQueryBlocked("kind_filter_unsupported")
        try:
            query = self._entry.query_engine.query_loads(
                signal_path,
                max_depth=max_depth,
                unprojected_instance_candidates=(self._unprojected_instance_candidates),
            )
        except SignalResolutionError as exc:
            raise SourceGraphQueryBlocked(exc.code) from exc
        claim_semantics = _query_claim_semantics(query)
        result = self._map_loads(
            query,
            include_expr=include_expr,
            requested_signal_path=signal_path,
            claim_semantics=claim_semantics,
        )
        result["_source_graph_query_receipt"] = _query_receipt(
            query,
            claim_semantics=claim_semantics,
        )
        return result

    def find_path(
        self,
        from_signal: str,
        to_signal: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        expand_assigns: bool = False,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        del compile_log, top_hint, simulator
        query = self._entry.query_engine.query_path(
            from_signal,
            to_signal,
            expand_assigns=expand_assigns,
        )
        claim_semantics = _path_claim_semantics(query)
        result = self._map_path(query, claim_semantics=claim_semantics)
        result["_source_graph_query_receipt"] = _path_query_receipt(
            query,
            claim_semantics=claim_semantics,
        )
        return result

    def _definition_name(self, instance_path: str) -> str | None:
        instance = self._entry.ir.instance_index.get(instance_path)
        if instance is None:
            return None
        definition = self._entry.ir.definition_index.get(instance.definition_id)
        return definition.name if definition is not None else None

    def _selection_location(
        self, selection: SignalSelection
    ) -> tuple[str | None, int | None]:
        instance_path = selection.instance_path
        instance = self._entry.ir.instance_index.get(instance_path or "")
        if instance is None:
            return None, None
        definition = self._entry.ir.definition_index.get(instance.definition_id)
        if definition is None:
            return None, None
        for declaration in (*definition.ports, *definition.signals):
            if declaration.name == selection.symbol:
                return declaration.location.file, declaration.location.line
        if "." not in selection.symbol:
            return None, None
        port_name, member_name = selection.symbol.split(".", 1)
        port = definition.port(port_name)
        if port is None or not port.interface_definition:
            return None, None
        candidates = [
            item
            for item in self._entry.ir.definitions
            if item.definition_id == port.interface_definition
            or item.name == port.interface_definition
        ]
        if len(candidates) != 1:
            return None, None
        for declaration in candidates[0].signals:
            if declaration.name == member_name:
                return declaration.location.file, declaration.location.line
        return None, None

    @staticmethod
    def _path_unsupported_reason(status: PathQueryStatus) -> str | None:
        return {
            PathQueryStatus.FOUND: None,
            PathQueryStatus.NOT_CONNECTED: "not_connected",
            PathQueryStatus.FROM_UNRESOLVED: "from_not_found",
            PathQueryStatus.TO_UNRESOLVED: "to_not_found",
            PathQueryStatus.ENDPOINTS_UNRESOLVED: ("source_graph_endpoints_unresolved"),
            PathQueryStatus.INCONCLUSIVE: "source_graph_query_inconclusive",
            PathQueryStatus.TRUNCATED: "source_graph_path_truncated",
        }[status]

    def _map_path(
        self,
        query: ConnectivityPathQueryResult,
        *,
        claim_semantics: dict[str, Any],
    ) -> dict[str, Any]:
        found = query.status is PathQueryStatus.FOUND
        public_path: list[dict[str, Any]] = []
        if found and query.from_endpoint is not None:
            source_file, source_line = self._selection_location(query.from_endpoint)
            public_path.append(
                {
                    "index": 0,
                    "net_path": query.from_endpoint.path(include_bits=True),
                    "scope_inst": query.from_endpoint.instance_path,
                    "source_file": source_file,
                    "source_line": source_line,
                    "source_info_origin": "source_graph",
                    "backend": "source_graph",
                    "is_endpoint": True,
                }
            )
            last_index = len(query.path)
            for index, edge in enumerate(query.path, start=1):
                assignment_edge = edge.edge_kind in {
                    EdgeKind.CONTINUOUS_ASSIGN,
                    EdgeKind.PROCEDURAL_ASSIGN,
                }
                expose_edge = query.expand_assigns or not assignment_edge
                public_path.append(
                    {
                        "index": index,
                        "net_path": edge.target.path(include_bits=True),
                        "scope_inst": edge.target.instance_path,
                        "source_file": edge.evidence.location.file,
                        "source_line": edge.evidence.location.line,
                        "source_info_origin": "source_graph",
                        "backend": "source_graph",
                        "is_endpoint": index == last_index,
                        "edge_kind": edge.edge_kind.value if expose_edge else None,
                        "edge_id": edge.edge_id if expose_edge else None,
                        "edge_source_path": (
                            edge.source.path(include_bits=True) if expose_edge else None
                        ),
                        "exact_bit_mapping": (
                            edge.exact_bit_mapping if expose_edge else None
                        ),
                    }
                )
        return {
            "from_signal": query.from_signal,
            "to_signal": query.to_signal,
            "found": found,
            "hops": len(query.path) if found else 0,
            "path": public_path,
            "expand_assigns": query.expand_assigns,
            "unsupported_reason": self._path_unsupported_reason(query.status),
            "backend": "source_graph",
            "claim_semantics": claim_semantics,
        }

    def _map_driver(
        self,
        query: ConnectivityQueryResult,
        *,
        wave_path: str,
        recursive: bool,
        requested_signal_path: str,
        claim_semantics: dict[str, Any],
    ) -> dict[str, Any]:
        matches = query.matches
        head = matches[0] if matches else None
        instance_path = (
            head.instance_path if head is not None else query.signal.instance_path
        )
        confidence = _aggregate_confidence(matches)
        if query.status is QueryStatus.FOUND:
            if query.unresolved_bits:
                driver_status = "partial"
                stopped_at = "source_graph_bit_provenance_incomplete"
                unsupported_reason = "source_graph_bit_provenance_incomplete"
            elif query.truncated:
                driver_status = "partial"
                stopped_at = "source_graph_query_truncated"
                unsupported_reason = None
            else:
                driver_status = "resolved"
                stopped_at = None
                unsupported_reason = None
        elif query.status is QueryStatus.NOT_CONNECTED:
            driver_status = "not_connected"
            confidence = "exact"
            stopped_at = "not_connected"
            unsupported_reason = None
        else:
            driver_status = "partial"
            stopped_at = "source_graph_query_inconclusive"
            unsupported_reason = "source_graph_query_inconclusive"

        match_kinds = {match.kind for match in matches}
        if query.multi_driver_bits:
            driver_kind = "multiple"
        elif EdgeKind.CONSTANT_DRIVER in match_kinds and len(match_kinds) > 1:
            driver_kind = "composite_port_binding"
        elif match_kinds == {EdgeKind.CONSTANT_DRIVER}:
            driver_kind = "constant"
        else:
            driver_kind = head.kind.value if head is not None else None
        upstream_signals = sorted(
            {
                dependency.source.path()
                for match in matches
                for dependency in match.dependencies
            }
        )
        bit_provenance = _driver_bit_provenance(query) if matches else None
        if driver_kind == "constant" and head is not None:
            expression_summary = f"constant {_constant_text(head.constant_bits)}"
        elif driver_kind == "composite_port_binding":
            expression_summary = "segmented port binding"
        else:
            expression_summary = None

        result: dict[str, Any] = {
            "signal_path": requested_signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": _requested_symbol(
                requested_signal_path,
                query.signal.instance_path,
            ),
            "resolved_module": (
                self._definition_name(instance_path) if instance_path else None
            ),
            "resolved_instance_path": instance_path,
            "driver_status": driver_status,
            "driver_kind": driver_kind,
            "source_file": (head.evidence.location.file if head is not None else None),
            "source_line": (head.evidence.location.line if head is not None else None),
            "source_info_origin": "source_graph" if head is not None else None,
            # Constants are IR facts; arbitrary source expression text remains
            # intentionally absent.
            "expression_summary": expression_summary,
            "upstream_signals": upstream_signals,
            "instance_port_connections": None,
            "bit_provenance": bit_provenance,
            "resolved_bit_count": len(query.resolved_bits),
            "unresolved_bit_count": len(query.unresolved_bits),
            "multi_driver_bit_count": len(query.multi_driver_bits),
            "confidence": confidence,
            "unsupported_reason": unsupported_reason,
            "stopped_at": stopped_at,
            "recursive": recursive,
            "driver_chain": None,
            "chain_summary": None,
            "backend": "source_graph",
            "claim_semantics": claim_semantics,
        }
        if matches and (recursive or len(matches) > 1):
            result["driver_chain"] = [
                {
                    "depth": len(match.traversal),
                    "signal_path": match.target.path(),
                    "resolved_module": self._definition_name(match.instance_path),
                    "resolved_instance_path": match.instance_path,
                    "driver_kind": match.kind.value,
                    "source_file": match.evidence.location.file,
                    "source_line": match.evidence.location.line,
                    "source_info_origin": "source_graph",
                    "expression_summary": (
                        f"constant {_constant_text(match.constant_bits)}"
                        if match.kind is EdgeKind.CONSTANT_DRIVER
                        else None
                    ),
                    "upstream_signals": [
                        dependency.source.path() for dependency in match.dependencies
                    ],
                    "instance_port_connections": None,
                    "branch_candidates": None,
                    "stopped_at": None,
                    "backend": "source_graph",
                    "backend_confidence": _public_confidence(match.confidence),
                }
                for match in matches
            ]
            result["chain_summary"] = f"{len(matches)} Source Graph assignment fact(s)"
        return result

    def _map_loads(
        self,
        query: ConnectivityQueryResult,
        *,
        include_expr: bool,
        requested_signal_path: str,
        claim_semantics: dict[str, Any],
    ) -> dict[str, Any]:
        del include_expr
        loads = [
            {
                "load_path": match.target.path(),
                # Every terminal load match is a source assignment whose RHS or
                # guard consumes the requested signal.  Binding traversal is
                # represented in the query receipt, not guessed as a module pin.
                "kind": "rhs_expr",
                "expr": None,
                "source_file": match.evidence.location.file,
                "source_line": match.evidence.location.line,
                "source_info_origin": "source_graph",
                "backend": "source_graph",
                "confidence": _public_confidence(match.confidence),
            }
            for match in query.matches
        ]
        instance_path = query.signal.instance_path
        if query.status is QueryStatus.NOT_CONNECTED:
            stopped_at = "not_connected"
            unsupported_reason = None
        elif query.status is QueryStatus.INCONCLUSIVE:
            stopped_at = "source_graph_query_inconclusive"
            unsupported_reason = "source_graph_query_inconclusive"
        elif query.truncated:
            stopped_at = "source_graph_query_truncated"
            unsupported_reason = None
        else:
            stopped_at = None
            unsupported_reason = None
        return {
            "signal_path": requested_signal_path,
            "resolved_rtl_name": _requested_symbol(
                requested_signal_path,
                query.signal.instance_path,
            ),
            "resolved_module": (
                self._definition_name(instance_path) if instance_path else None
            ),
            "resolved_instance_path": instance_path,
            "loads": loads,
            "completeness": (
                "exact"
                if query.coverage_status is CoverageStatus.COMPLETE
                else "approximate"
            ),
            "stopped_at": stopped_at,
            "unsupported_reason": unsupported_reason,
            "claim_semantics": claim_semantics,
        }
