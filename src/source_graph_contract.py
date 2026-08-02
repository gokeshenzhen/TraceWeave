"""Internal contracts for scoped, on-demand Source Graph preparation.

This module is intentionally separate from :mod:`src.schemas` and the public
MCP surface.  It describes only the inputs needed to prepare a bounded
Connectivity IR projection, the stable identity of that work, and the
evidence required before one prepared scope may satisfy another request.

The contract is fail-closed in two places:

* incomplete compile-input manifests still receive a diagnostic digest, but
  never a cross-request cache key; and
* scope reuse requires explicit, finite instance-path coverage.  There is no
  wildcard or implicit full-hierarchy form.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import json
import re
from typing import Any, Mapping, Sequence

from .connectivity_ir import CONNECTIVITY_IR_VERSION, CoverageStatus


SOURCE_GRAPH_BUILD_CONTRACT_VERSION = "1.0"
SOURCE_GRAPH_PROJECTOR_NAME = "slang_connectivity_projector"
SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION = "1.0"

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class QueryOperation(str, Enum):
    DRIVER = "driver"
    LOADS = "loads"


class BoundaryMode(str, Enum):
    EXPLICIT = "explicit"
    UNSCOPED_GAP = "unscoped_gap"


class ScopeRelation(str, Enum):
    EXACT = "exact"
    SUPERSET = "superset"
    SUBSET = "subset"
    DISJOINT = "disjoint"
    UNPROVEN = "unproven"


def _required_text(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} must not be empty")
    if "\x00" in value:
        raise ValueError(f"{label} must not contain NUL")
    return value.strip()


def _hier_path(value: str, label: str) -> str:
    value = _required_text(value, label)
    parts = tuple(part.strip() for part in value.split("."))
    if any(not part for part in parts):
        raise ValueError(f"{label} must be a dotted hierarchy path")
    if any("*" in part for part in parts):
        raise ValueError(f"{label} must not contain wildcards")
    return ".".join(parts)


def _ordered_text(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} collection must be a sequence")
    return tuple(_required_text(value, label) for value in values)


def _path_set(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} collection must be a sequence")
    return tuple(sorted({_hier_path(value, label) for value in values}))


def _fixed_labels(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} collection must be a sequence")
    normalized = {_required_text(value, label) for value in values}
    for value in normalized:
        if not re.fullmatch(r"[a-z][a-z0-9_]*", value):
            raise ValueError(f"{label} entries must be fixed snake_case labels")
    return tuple(sorted(normalized))


def _canonical_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _sha256(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


@dataclass(frozen=True)
class CompileInputManifest:
    """Ordered compile inputs plus an independently computed content digest.

    Completeness flags are explicit and default to ``False``.  Empty option
    lists are valid, so list emptiness cannot stand in for whether option
    discovery was complete.
    """

    fingerprint: str | None
    ordered_inputs: tuple[str, ...] = ()
    ordered_options: tuple[str, ...] = ()
    ordered_tops: tuple[str, ...] = ()
    inputs_complete: bool = False
    options_complete: bool = False
    tops_complete: bool = False

    def __post_init__(self) -> None:
        fingerprint = self.fingerprint
        if fingerprint is not None:
            fingerprint = _required_text(fingerprint, "compile-input fingerprint")
            if not _SHA256_RE.fullmatch(fingerprint):
                raise ValueError(
                    "compile-input fingerprint must be a SHA-256 hex digest"
                )
            fingerprint = fingerprint.lower()
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(
            self,
            "ordered_inputs",
            _ordered_text(self.ordered_inputs, "compile input"),
        )
        object.__setattr__(
            self,
            "ordered_options",
            _ordered_text(self.ordered_options, "compile option"),
        )
        object.__setattr__(
            self,
            "ordered_tops",
            tuple(_hier_path(top, "compile top") for top in self.ordered_tops),
        )

    @property
    def incomplete_reasons(self) -> tuple[str, ...]:
        reasons: list[str] = []
        if self.fingerprint is None:
            reasons.append("compile_fingerprint_missing")
        if not self.inputs_complete:
            reasons.append("compile_input_order_incomplete")
        if not self.options_complete:
            reasons.append("compile_option_order_incomplete")
        if not self.tops_complete:
            reasons.append("compile_top_order_incomplete")
        if not self.ordered_inputs:
            reasons.append("compile_inputs_empty")
        if not self.ordered_tops:
            reasons.append("compile_tops_empty")
        return tuple(reasons)

    @property
    def complete(self) -> bool:
        return not self.incomplete_reasons

    def to_dict(self) -> dict[str, Any]:
        return {
            "fingerprint": self.fingerprint,
            "ordered_inputs": list(self.ordered_inputs),
            "ordered_options": list(self.ordered_options),
            "ordered_tops": list(self.ordered_tops),
            "inputs_complete": self.inputs_complete,
            "options_complete": self.options_complete,
            "tops_complete": self.tops_complete,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CompileInputManifest:
        return cls(
            fingerprint=value.get("fingerprint"),
            ordered_inputs=tuple(value.get("ordered_inputs", ())),
            ordered_options=tuple(value.get("ordered_options", ())),
            ordered_tops=tuple(value.get("ordered_tops", ())),
            inputs_complete=bool(value.get("inputs_complete", False)),
            options_complete=bool(value.get("options_complete", False)),
            tops_complete=bool(value.get("tops_complete", False)),
        )


@dataclass(frozen=True)
class SourceGraphIdentity:
    compile_inputs: CompileInputManifest
    frontend_name: str
    frontend_version: str
    ir_schema_version: str = CONNECTIVITY_IR_VERSION
    projector_name: str = SOURCE_GRAPH_PROJECTOR_NAME
    projector_version: str = SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
    projector_schema_version: str = SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "frontend_name",
            "frontend_version",
            "ir_schema_version",
            "projector_name",
            "projector_version",
            "projector_schema_version",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_text(getattr(self, field_name), field_name),
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "compile_inputs": self.compile_inputs.to_dict(),
            "frontend_name": self.frontend_name,
            "frontend_version": self.frontend_version,
            "ir_schema_version": self.ir_schema_version,
            "projector_name": self.projector_name,
            "projector_version": self.projector_version,
            "projector_schema_version": self.projector_schema_version,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphIdentity:
        compile_inputs = value.get("compile_inputs")
        if not isinstance(compile_inputs, Mapping):
            raise ValueError("compile_inputs must be an object")
        return cls(
            compile_inputs=CompileInputManifest.from_dict(compile_inputs),
            frontend_name=value["frontend_name"],
            frontend_version=value["frontend_version"],
            ir_schema_version=value.get("ir_schema_version", CONNECTIVITY_IR_VERSION),
            projector_name=value.get("projector_name", SOURCE_GRAPH_PROJECTOR_NAME),
            projector_version=value.get(
                "projector_version", SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
            ),
            projector_schema_version=value.get(
                "projector_schema_version", SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
            ),
        )


@dataclass(frozen=True)
class ConnectivityTarget:
    operation: QueryOperation
    signal_path: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", QueryOperation(self.operation))
        signal_path = _hier_path(self.signal_path, "target signal_path")
        if "." not in signal_path:
            raise ValueError("target signal_path must include an instance and signal")
        object.__setattr__(
            self,
            "signal_path",
            signal_path,
        )

    @property
    def instance_path(self) -> str:
        return self.signal_path.rsplit(".", 1)[0]

    def to_dict(self) -> dict[str, str]:
        return {
            "operation": self.operation.value,
            "signal_path": self.signal_path,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConnectivityTarget:
        return cls(
            operation=QueryOperation(value["operation"]),
            signal_path=value["signal_path"],
        )


@dataclass(frozen=True)
class RequestedCone:
    operation: QueryOperation
    max_hops: int
    instance_paths: tuple[str, ...]
    cross_instance_boundaries: bool = True
    stop_at_sequential: bool = True
    include_control_dependencies: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", QueryOperation(self.operation))
        if not isinstance(self.max_hops, int) or isinstance(self.max_hops, bool):
            raise ValueError("requested cone max_hops must be an integer")
        if self.max_hops < 0:
            raise ValueError("requested cone max_hops must not be negative")
        paths = _path_set(self.instance_paths, "requested cone instance path")
        if not paths:
            raise ValueError("requested cone must name at least one instance path")
        object.__setattr__(self, "instance_paths", paths)

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "max_hops": self.max_hops,
            "instance_paths": list(self.instance_paths),
            "cross_instance_boundaries": self.cross_instance_boundaries,
            "stop_at_sequential": self.stop_at_sequential,
            "include_control_dependencies": self.include_control_dependencies,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> RequestedCone:
        return cls(
            operation=QueryOperation(value["operation"]),
            max_hops=int(value["max_hops"]),
            instance_paths=tuple(value.get("instance_paths", ())),
            cross_instance_boundaries=bool(
                value.get("cross_instance_boundaries", True)
            ),
            stop_at_sequential=bool(value.get("stop_at_sequential", True)),
            include_control_dependencies=bool(
                value.get("include_control_dependencies", False)
            ),
        )


@dataclass(frozen=True)
class CoverageBoundary:
    mode: BoundaryMode
    instance_paths: tuple[str, ...] = ()
    objective_exclusions: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        mode = BoundaryMode(self.mode)
        object.__setattr__(self, "mode", mode)
        paths = _path_set(self.instance_paths, "coverage boundary instance path")
        exclusions = _fixed_labels(
            self.objective_exclusions, "coverage objective exclusion"
        )
        if mode is BoundaryMode.EXPLICIT and not paths:
            raise ValueError("explicit coverage boundary must name instance paths")
        if mode is BoundaryMode.UNSCOPED_GAP and paths:
            raise ValueError("unscoped coverage gap must not claim instance paths")
        object.__setattr__(self, "instance_paths", paths)
        object.__setattr__(self, "objective_exclusions", exclusions)

    @property
    def explicit(self) -> bool:
        return self.mode is BoundaryMode.EXPLICIT

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "instance_paths": list(self.instance_paths),
            "objective_exclusions": list(self.objective_exclusions),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> CoverageBoundary:
        return cls(
            mode=BoundaryMode(value["mode"]),
            instance_paths=tuple(value.get("instance_paths", ())),
            objective_exclusions=tuple(value.get("objective_exclusions", ())),
        )


@dataclass(frozen=True)
class SourceGraphBuildScope:
    design: str
    top: str
    target: ConnectivityTarget
    hierarchy_ancestors: tuple[str, ...]
    requested_cone: RequestedCone
    coverage_boundary: CoverageBoundary

    def __post_init__(self) -> None:
        object.__setattr__(self, "design", _required_text(self.design, "design"))
        top = _hier_path(self.top, "top")
        object.__setattr__(self, "top", top)
        ancestors = tuple(
            _hier_path(path, "hierarchy ancestor") for path in self.hierarchy_ancestors
        )
        if not ancestors or ancestors[0] != top:
            raise ValueError("hierarchy ancestors must start at top")
        if ancestors[-1] != self.target.instance_path:
            raise ValueError("hierarchy ancestors must end at the target instance")
        for parent, child in zip(ancestors, ancestors[1:]):
            if not child.startswith(parent + "."):
                raise ValueError("hierarchy ancestors must be ordered root-to-leaf")
        if self.requested_cone.operation is not self.target.operation:
            raise ValueError("requested cone operation must match target operation")
        if self.target.instance_path not in self.requested_cone.instance_paths:
            raise ValueError("requested cone must include the target instance")
        if self.coverage_boundary.explicit:
            boundary = set(self.coverage_boundary.instance_paths)
            required_paths = set(ancestors) | set(self.requested_cone.instance_paths)
            if not required_paths.issubset(boundary):
                raise ValueError(
                    "coverage boundary must include ancestors and requested cone paths"
                )
        object.__setattr__(self, "hierarchy_ancestors", ancestors)

    def to_dict(self) -> dict[str, Any]:
        return {
            "design": self.design,
            "top": self.top,
            "target": self.target.to_dict(),
            "hierarchy_ancestors": list(self.hierarchy_ancestors),
            "requested_cone": self.requested_cone.to_dict(),
            "coverage_boundary": self.coverage_boundary.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphBuildScope:
        target = value.get("target")
        cone = value.get("requested_cone")
        boundary = value.get("coverage_boundary")
        if not all(isinstance(item, Mapping) for item in (target, cone, boundary)):
            raise ValueError(
                "target, requested_cone, and coverage_boundary are required"
            )
        return cls(
            design=value["design"],
            top=value["top"],
            target=ConnectivityTarget.from_dict(target),
            hierarchy_ancestors=tuple(value.get("hierarchy_ancestors", ())),
            requested_cone=RequestedCone.from_dict(cone),
            coverage_boundary=CoverageBoundary.from_dict(boundary),
        )


@dataclass(frozen=True)
class SourceGraphBuildRequest:
    identity: SourceGraphIdentity
    scope: SourceGraphBuildScope
    contract_version: str = SOURCE_GRAPH_BUILD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        contract_version = _required_text(self.contract_version, "contract_version")
        if contract_version != SOURCE_GRAPH_BUILD_CONTRACT_VERSION:
            raise ValueError(
                f"unsupported Source Graph build contract version: {contract_version}"
            )
        object.__setattr__(self, "contract_version", contract_version)
        if (
            self.identity.compile_inputs.tops_complete
            and self.identity.compile_inputs.ordered_tops
            and self.scope.top not in self.identity.compile_inputs.ordered_tops
        ):
            raise ValueError("scope top is absent from the complete compile top list")

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "identity": self.identity.to_dict(),
            "scope": self.scope.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphBuildRequest:
        identity = value.get("identity")
        scope = value.get("scope")
        if not isinstance(identity, Mapping) or not isinstance(scope, Mapping):
            raise ValueError("identity and scope are required")
        return cls(
            identity=SourceGraphIdentity.from_dict(identity),
            scope=SourceGraphBuildScope.from_dict(scope),
            contract_version=value.get(
                "contract_version", SOURCE_GRAPH_BUILD_CONTRACT_VERSION
            ),
        )


@dataclass(frozen=True)
class SourceGraphBuildKey:
    digest: str
    design_digest: str
    scope_digest: str
    cross_request_reusable: bool
    incomplete_reasons: tuple[str, ...]

    @property
    def cache_key(self) -> str | None:
        return self.digest if self.cross_request_reusable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "design_digest": self.design_digest,
            "scope_digest": self.scope_digest,
            "cross_request_reusable": self.cross_request_reusable,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


def compute_source_graph_build_key(
    request: SourceGraphBuildRequest,
) -> SourceGraphBuildKey:
    identity_payload = request.identity.to_dict()
    scope_payload = request.scope.to_dict()
    design_digest = _sha256(identity_payload)
    scope_digest = _sha256(scope_payload)
    digest = _sha256(
        {
            "contract_version": request.contract_version,
            "design_digest": design_digest,
            "scope_digest": scope_digest,
        }
    )
    reasons = list(request.identity.compile_inputs.incomplete_reasons)
    if not request.scope.coverage_boundary.explicit:
        reasons.append("scope_boundary_unscoped")
    incomplete_reasons = tuple(sorted(set(reasons)))
    return SourceGraphBuildKey(
        digest=digest,
        design_digest=design_digest,
        scope_digest=scope_digest,
        cross_request_reusable=not incomplete_reasons,
        incomplete_reasons=incomplete_reasons,
    )


def _scope_covers(
    available: SourceGraphBuildScope,
    requested: SourceGraphBuildScope,
) -> bool:
    if (
        available.design != requested.design
        or available.top != requested.top
        or available.target != requested.target
        or available.hierarchy_ancestors != requested.hierarchy_ancestors
    ):
        return False
    if not available.coverage_boundary.explicit:
        return False
    if not requested.coverage_boundary.explicit:
        return False

    available_cone = available.requested_cone
    requested_cone = requested.requested_cone
    if available_cone.max_hops < requested_cone.max_hops:
        return False
    if not set(requested_cone.instance_paths).issubset(available_cone.instance_paths):
        return False
    if not set(requested.coverage_boundary.instance_paths).issubset(
        available.coverage_boundary.instance_paths
    ):
        return False
    if (
        requested_cone.cross_instance_boundaries
        and not available_cone.cross_instance_boundaries
    ):
        return False
    if not requested_cone.stop_at_sequential and available_cone.stop_at_sequential:
        return False
    if (
        requested_cone.include_control_dependencies
        and not available_cone.include_control_dependencies
    ):
        return False
    if not set(available.coverage_boundary.objective_exclusions).issubset(
        requested.coverage_boundary.objective_exclusions
    ):
        return False
    return True


def compare_source_graph_scopes(
    available: SourceGraphBuildScope,
    requested: SourceGraphBuildScope,
) -> ScopeRelation:
    if (
        available.design != requested.design
        or available.top != requested.top
        or available.target != requested.target
    ):
        return ScopeRelation.DISJOINT
    if (
        not available.coverage_boundary.explicit
        or not requested.coverage_boundary.explicit
    ):
        return ScopeRelation.UNPROVEN
    if available.to_dict() == requested.to_dict():
        return ScopeRelation.EXACT
    if _scope_covers(available, requested):
        return ScopeRelation.SUPERSET
    if _scope_covers(requested, available):
        return ScopeRelation.SUBSET
    return ScopeRelation.UNPROVEN


@dataclass(frozen=True)
class ScopeReuseDecision:
    relation: ScopeRelation
    reusable: bool
    coverage_status: CoverageStatus
    complete_for_request: bool
    reason: str


@dataclass(frozen=True)
class SourceGraphScopeReceipt:
    scope: SourceGraphBuildScope
    coverage_status: CoverageStatus
    gap_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = CoverageStatus(self.coverage_status)
        gap_codes = _fixed_labels(self.gap_codes, "scope gap code")
        if status is CoverageStatus.COMPLETE:
            if not self.scope.coverage_boundary.explicit:
                raise ValueError("unscoped coverage cannot be complete")
            if gap_codes or self.scope.coverage_boundary.objective_exclusions:
                raise ValueError("coverage with gaps or exclusions cannot be complete")
        elif (
            self.scope.coverage_boundary.explicit
            and not gap_codes
            and not self.scope.coverage_boundary.objective_exclusions
        ):
            raise ValueError("partial or inconclusive coverage requires a gap receipt")
        object.__setattr__(self, "coverage_status", status)
        object.__setattr__(self, "gap_codes", gap_codes)

    def reuse_for(self, requested: SourceGraphBuildScope) -> ScopeReuseDecision:
        relation = compare_source_graph_scopes(self.scope, requested)
        reusable = relation in {ScopeRelation.EXACT, ScopeRelation.SUPERSET}
        complete = reusable and self.coverage_status is CoverageStatus.COMPLETE
        if not reusable:
            reason = f"scope_{relation.value}"
        elif complete:
            reason = "coverage_complete"
        else:
            reason = f"coverage_preserved_{self.coverage_status.value}"
        return ScopeReuseDecision(
            relation=relation,
            reusable=reusable,
            coverage_status=self.coverage_status,
            complete_for_request=complete,
            reason=reason,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope.to_dict(),
            "coverage_status": self.coverage_status.value,
            "gap_codes": list(self.gap_codes),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphScopeReceipt:
        scope = value.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("scope receipt requires a scope object")
        return cls(
            scope=SourceGraphBuildScope.from_dict(scope),
            coverage_status=CoverageStatus(value["coverage_status"]),
            gap_codes=tuple(value.get("gap_codes", ())),
        )
