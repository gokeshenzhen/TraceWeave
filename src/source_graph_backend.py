"""Prepared Source Graph backend and conservative public result mapping.

The runtime owns preparation/cache lifecycle; this module consumes exactly one
prepared cache entry and emits driver/load facts from its Connectivity IR.  It
does not invoke NPI or Legacy Static, so a returned result has one provenance.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .connectivity_ir import CoverageStatus
from .connectivity_query import (
    ConnectivityQueryResult,
    QueryConfidence,
    QueryMatch,
    QueryStatus,
)
from .source_graph_runtime import SourceGraphCacheEntry


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


def _gap_codes(result: ConnectivityQueryResult) -> list[str]:
    return sorted({gap.code for gap in result.unresolved_boundaries})


def _query_receipt(result: ConnectivityQueryResult) -> dict[str, Any]:
    return {
        "status": result.status.value,
        "coverage_status": result.coverage_status.value,
        "confidence": _aggregate_confidence(result.matches),
        "match_count": len(result.matches),
        "unresolved_boundary_codes": _gap_codes(result),
        "traversed_binding_edges": result.traversed_binding_edges,
        "max_depth": result.max_depth,
    }


class SourceGraphConnectivityBackend:
    """One-provenance driver/load queries over a prepared Source Graph entry."""

    name = "source_graph"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(self, entry: SourceGraphCacheEntry) -> None:
        self._entry = entry

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
        query = self._entry.query_engine.query_driver(
            signal_path,
            max_depth=max_depth,
        )
        result = self._map_driver(
            query,
            wave_path=wave_path,
            recursive=recursive,
        )
        result["_source_graph_query_receipt"] = _query_receipt(query)
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
        query = self._entry.query_engine.query_loads(
            signal_path,
            max_depth=max_depth,
        )
        result = self._map_loads(query, include_expr=include_expr)
        result["_source_graph_query_receipt"] = _query_receipt(query)
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
        """Arbitrary path integration is outside the approved Phase 2 scope."""

        del compile_log, top_hint, simulator
        return {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": expand_assigns,
            "unsupported_reason": "source_graph_path_not_integrated",
        }

    def _definition_name(self, instance_path: str) -> str | None:
        instance = self._entry.ir.instance_index.get(instance_path)
        if instance is None:
            return None
        definition = self._entry.ir.definition_index.get(instance.definition_id)
        return definition.name if definition is not None else None

    def _map_driver(
        self,
        query: ConnectivityQueryResult,
        *,
        wave_path: str,
        recursive: bool,
    ) -> dict[str, Any]:
        matches = query.matches
        head = matches[0] if matches else None
        instance_path = (
            head.instance_path if head is not None else query.signal.instance_path
        )
        confidence = _aggregate_confidence(matches)
        if query.status is QueryStatus.FOUND:
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

        result: dict[str, Any] = {
            "signal_path": query.signal.path(),
            "wave_path": wave_path,
            "resolved_rtl_name": query.signal.symbol,
            "resolved_module": (
                self._definition_name(instance_path) if instance_path else None
            ),
            "resolved_instance_path": instance_path,
            "driver_status": driver_status,
            "driver_kind": head.kind.value if head is not None else None,
            "source_file": (head.evidence.location.file if head is not None else None),
            "source_line": (head.evidence.location.line if head is not None else None),
            "source_info_origin": "source_graph" if head is not None else None,
            # Connectivity IR intentionally retains dependencies and guards,
            # not source expression text.  Do not synthesize an expression.
            "expression_summary": None,
            "upstream_signals": (
                [dependency.source.path() for dependency in head.dependencies]
                if head is not None
                else []
            ),
            "instance_port_connections": None,
            "confidence": confidence,
            "unsupported_reason": unsupported_reason,
            "stopped_at": stopped_at,
            "recursive": recursive,
            "driver_chain": None,
            "chain_summary": None,
            "backend": "source_graph",
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
                    "expression_summary": None,
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
        else:
            stopped_at = None
            unsupported_reason = None
        return {
            "signal_path": query.signal.path(),
            "resolved_rtl_name": query.signal.symbol,
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
        }
