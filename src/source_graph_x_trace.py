"""Single-artifact Source Graph guard for multi-node X/Z traces.

The public trace must never combine facts from multiple prepared artifacts.
This module validates every prospective driver target against one exact
hierarchy-proved artifact scope, records identity-free query provenance, and
raises fixed-label restart/fallback markers before an unsafe fact can enter the
propagation chain. It performs no waveform I/O and owns no worker lifecycle.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from .cancellation import check_cancelled
from .source_graph_adapter import resolve_source_graph_hierarchy_ancestors
from .source_graph_contract import (
    BoundaryMode,
    ConnectivityTarget,
    CoverageBoundary,
    QueryOperation,
    ScopeRelation,
    SourceGraphArtifactScope,
    SourceGraphQueryIdentity,
    compare_source_graph_artifact_scopes,
    compute_source_graph_query_key,
)


_FIXED_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _fixed_label(value: str, label: str) -> str:
    if not isinstance(value, str) or not _FIXED_LABEL_RE.fullmatch(value):
        raise ValueError(f"{label} must be a fixed snake_case label")
    return value


class SourceGraphTraceScopeExpansion(RuntimeError):
    """A proved target lies outside the current artifact projection."""

    code = "source_graph_trace_scope_expansion_required"

    def __init__(self, signal_paths: Sequence[str]) -> None:
        self.signal_paths = tuple(dict.fromkeys(signal_paths))
        if not self.signal_paths:
            raise ValueError("scope expansion requires at least one signal path")
        super().__init__(self.code)


class SourceGraphTraceFallbackRequired(RuntimeError):
    """The current artifact cannot safely support a final public trace."""

    def __init__(self, code: str) -> None:
        self.code = _fixed_label(code, "Source Graph trace fallback code")
        super().__init__(self.code)


def _requested_driver_scope(
    available: SourceGraphArtifactScope,
    ancestors: tuple[str, ...],
) -> SourceGraphArtifactScope:
    return SourceGraphArtifactScope(
        design=available.design,
        top=available.top,
        hierarchy_snapshot_sha256=available.hierarchy_snapshot_sha256,
        proved_ancestor_chains=(ancestors,),
        proved_lcas=(ancestors[-1],),
        projection_instance_paths=ancestors,
        coverage_boundary=CoverageBoundary(
            mode=BoundaryMode.EXPLICIT,
            instance_paths=ancestors,
            objective_exclusions=(available.coverage_boundary.objective_exclusions),
        ),
        capabilities=(QueryOperation.DRIVER,),
    )


class SourceGraphTraceArtifactGuard:
    """Admit only targets covered by one hierarchy-proved artifact."""

    def __init__(
        self,
        *,
        artifact_scope: SourceGraphArtifactScope,
        hierarchy_result: Mapping[str, Any],
    ) -> None:
        self._scope = artifact_scope
        self._hierarchy_result = hierarchy_result

    def require(self, signal_paths: Sequence[str]) -> None:
        check_cancelled()
        uncovered: list[str] = []
        for raw_path in signal_paths:
            check_cancelled()
            signal_path = str(raw_path).strip()
            if not signal_path or "." not in signal_path:
                raise SourceGraphTraceFallbackRequired(
                    "source_graph_trace_signal_unscoped"
                )
            if signal_path.split(".", 1)[0] != self._scope.top:
                raise SourceGraphTraceFallbackRequired(
                    "source_graph_trace_target_top_mismatch"
                )
            ancestors = resolve_source_graph_hierarchy_ancestors(
                hierarchy_result=self._hierarchy_result,
                top=self._scope.top,
                signal_path=signal_path,
            )
            if ancestors is None:
                raise SourceGraphTraceFallbackRequired(
                    "source_graph_trace_hierarchy_scope_unresolved"
                )
            requested = _requested_driver_scope(self._scope, ancestors)
            relation = compare_source_graph_artifact_scopes(
                self._scope,
                requested,
            )
            if relation not in {ScopeRelation.EXACT, ScopeRelation.SUPERSET}:
                uncovered.append(signal_path)
        if uncovered:
            raise SourceGraphTraceScopeExpansion(uncovered)


class SourceGraphTraceQueryLedger:
    """Identity-free receipt for queries made against exactly one artifact."""

    def __init__(self, artifact_fingerprint_sha256: str) -> None:
        fingerprint = str(artifact_fingerprint_sha256).lower()
        if not _SHA256_RE.fullmatch(fingerprint):
            raise ValueError("artifact fingerprint must be a SHA-256 digest")
        self.artifact_fingerprint_sha256 = fingerprint
        self.query_fingerprints_sha256: list[str] = []
        self.query_statuses: list[str] = []
        self.coverage_statuses: list[str] = []
        self.positive_query_count = 0
        self.complete_negative_query_count = 0
        self.inconclusive_negative_count = 0

    def record(
        self,
        *,
        signal_path: str,
        max_depth: int,
        recursive: bool,
        query_receipt: Mapping[str, Any],
    ) -> None:
        identity = SourceGraphQueryIdentity(
            target=ConnectivityTarget(
                operation=QueryOperation.DRIVER,
                signal_path=signal_path,
            ),
            max_depth=max_depth,
            recursive=recursive,
        )
        self.query_fingerprints_sha256.append(
            compute_source_graph_query_key(identity).digest
        )
        status = _fixed_label(
            str(query_receipt.get("status") or "unknown"), "query status"
        )
        coverage = _fixed_label(
            str(query_receipt.get("coverage_status") or "inconclusive"),
            "coverage status",
        )
        self.query_statuses.append(status)
        self.coverage_statuses.append(coverage)
        if status == "found":
            self.positive_query_count += 1
        elif status == "not_connected" and coverage == "complete":
            self.complete_negative_query_count += 1
        else:
            self.inconclusive_negative_count += 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "single_artifact_provenance": True,
            "final_artifact_scope_match": True,
            "artifact_fingerprint_sha256": self.artifact_fingerprint_sha256,
            "query_count": len(self.query_fingerprints_sha256),
            "query_fingerprints_sha256": list(self.query_fingerprints_sha256),
            "query_statuses": list(self.query_statuses),
            "coverage_statuses": list(self.coverage_statuses),
            "positive_query_count": self.positive_query_count,
            "complete_negative_query_count": self.complete_negative_query_count,
            "inconclusive_negative_count": self.inconclusive_negative_count,
        }


class SourceGraphTraceConnectivityBackend:
    """Driver backend that rejects unsafe negatives and mixed provenance."""

    name = "source_graph"
    execution_mode = "local"
    uses_external_worker = False

    def __init__(
        self,
        *,
        backend,
        artifact_scope: SourceGraphArtifactScope,
        hierarchy_result: Mapping[str, Any],
        artifact_fingerprint_sha256: str,
    ) -> None:
        self._backend = backend
        self._guard = SourceGraphTraceArtifactGuard(
            artifact_scope=artifact_scope,
            hierarchy_result=hierarchy_result,
        )
        self.ledger = SourceGraphTraceQueryLedger(artifact_fingerprint_sha256)

    def require_scope(self, signal_paths: Sequence[str]) -> None:
        self._guard.require(signal_paths)

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
        check_cancelled()
        self._guard.require((signal_path,))
        result = self._backend.find_driver(
            signal_path=signal_path,
            wave_path=wave_path,
            compile_log=compile_log,
            top_hint=top_hint,
            recursive=recursive,
            max_depth=max_depth,
            simulator=simulator,
        )
        check_cancelled()
        if not isinstance(result, dict):
            raise SourceGraphTraceFallbackRequired("source_graph_trace_query_invalid")
        query_receipt = result.get("_source_graph_query_receipt")
        if not isinstance(query_receipt, Mapping):
            raise SourceGraphTraceFallbackRequired(
                "source_graph_trace_query_receipt_missing"
            )
        explicit_backends = [result.get("backend")]
        chain = result.get("driver_chain")
        if isinstance(chain, list):
            explicit_backends.extend(
                hop.get("backend")
                for hop in chain
                if isinstance(hop, Mapping) and hop.get("backend") is not None
            )
        if any(item != "source_graph" for item in explicit_backends):
            raise SourceGraphTraceFallbackRequired(
                "source_graph_trace_mixed_provenance"
            )

        self.ledger.record(
            signal_path=signal_path,
            max_depth=max_depth,
            recursive=recursive,
            query_receipt=query_receipt,
        )
        status = query_receipt.get("status")
        coverage = query_receipt.get("coverage_status")
        if status == "found":
            if result.get("driver_status") != "resolved":
                raise SourceGraphTraceFallbackRequired(
                    "source_graph_trace_positive_mapping_invalid"
                )
        elif status == "not_connected" and coverage == "complete":
            if result.get("driver_status") != "not_connected":
                raise SourceGraphTraceFallbackRequired(
                    "source_graph_trace_negative_mapping_invalid"
                )
        elif status == "not_connected":
            raise SourceGraphTraceFallbackRequired(
                "source_graph_trace_incomplete_negative"
            )
        else:
            raise SourceGraphTraceFallbackRequired(
                "source_graph_trace_query_inconclusive"
            )

        clean = dict(result)
        clean.pop("_source_graph_query_receipt", None)
        return clean
