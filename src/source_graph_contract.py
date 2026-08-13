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


SOURCE_GRAPH_BUILD_CONTRACT_VERSION = "3.0"
SOURCE_GRAPH_WORKER_PROTOCOL_VERSION = "3.0"
SOURCE_GRAPH_PROJECTOR_NAME = "slang_connectivity_projector"
SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION = "1.2"
SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION = "1.0"
SOURCE_GRAPH_QUERY_IDENTITY_VERSION = "1.0"
SOURCE_GRAPH_QUERY_MAPPING_VERSION = "1.0"
DEFAULT_PATH_TRAVERSAL_LIMIT = 4096
DEFAULT_PATH_OUTPUT_LIMIT = 256

_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


class QueryOperation(str, Enum):
    DRIVER = "driver"
    LOADS = "loads"
    PATH = "path"


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
        if self.operation is QueryOperation.PATH:
            raise ValueError("path queries require a dual-endpoint path target")
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


def _positive_limit(value: int, label: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{label} must be a positive integer")
    return value


@dataclass(frozen=True)
class ConnectivityPathTarget:
    """Target-specific identity for one bounded structural path query."""

    operation: QueryOperation
    from_signal: str
    to_signal: str
    from_instance_path: str
    to_instance_path: str
    expand_assigns: bool = False
    traversal_limit: int = DEFAULT_PATH_TRAVERSAL_LIMIT
    output_limit: int = DEFAULT_PATH_OUTPUT_LIMIT

    def __post_init__(self) -> None:
        operation = QueryOperation(self.operation)
        if operation is not QueryOperation.PATH:
            raise ValueError("dual-endpoint target operation must be path")
        object.__setattr__(self, "operation", operation)
        for field_name in ("from_signal", "to_signal"):
            signal_path = _hier_path(
                getattr(self, field_name), f"path target {field_name}"
            )
            if "." not in signal_path:
                raise ValueError(
                    f"path target {field_name} must include an instance and signal"
                )
            object.__setattr__(self, field_name, signal_path)
        for endpoint in ("from", "to"):
            instance_field = f"{endpoint}_instance_path"
            signal_field = f"{endpoint}_signal"
            instance_path = _hier_path(
                getattr(self, instance_field), f"path target {instance_field}"
            )
            signal_path = getattr(self, signal_field)
            if not signal_path.startswith(f"{instance_path}."):
                raise ValueError(
                    f"path target {signal_field} must be within {instance_field}"
                )
            object.__setattr__(self, instance_field, instance_path)
        if not isinstance(self.expand_assigns, bool):
            raise ValueError("path target expand_assigns must be boolean")
        object.__setattr__(
            self,
            "traversal_limit",
            _positive_limit(self.traversal_limit, "path traversal_limit"),
        )
        object.__setattr__(
            self,
            "output_limit",
            _positive_limit(self.output_limit, "path output_limit"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation.value,
            "from_signal": self.from_signal,
            "to_signal": self.to_signal,
            "from_instance_path": self.from_instance_path,
            "to_instance_path": self.to_instance_path,
            "expand_assigns": self.expand_assigns,
            "traversal_limit": self.traversal_limit,
            "output_limit": self.output_limit,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> ConnectivityPathTarget:
        return cls(
            operation=QueryOperation(value["operation"]),
            from_signal=value["from_signal"],
            to_signal=value["to_signal"],
            from_instance_path=value["from_instance_path"],
            to_instance_path=value["to_instance_path"],
            expand_assigns=value.get("expand_assigns", False),
            traversal_limit=value.get("traversal_limit", DEFAULT_PATH_TRAVERSAL_LIMIT),
            output_limit=value.get("output_limit", DEFAULT_PATH_OUTPUT_LIMIT),
        )


def _target_from_dict(
    value: Mapping[str, Any],
) -> ConnectivityTarget | ConnectivityPathTarget:
    operation = QueryOperation(value["operation"])
    if operation is QueryOperation.PATH:
        return ConnectivityPathTarget.from_dict(value)
    return ConnectivityTarget.from_dict(value)


def _ancestor_chain(values: Sequence[str], label: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{label} collection must be a sequence")
    chain = tuple(_hier_path(value, label) for value in values)
    if not chain:
        raise ValueError(f"{label} must not be empty")
    for parent, child in zip(chain, chain[1:]):
        if not child.startswith(parent + "."):
            raise ValueError(f"{label} must be ordered root-to-leaf")
    return chain


def _path_union(*chains: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted(set().union(*chains), key=lambda path: (path.count("."), path)))


@dataclass(frozen=True)
class PathHierarchyScope:
    """Proof receipt for two endpoint chains and their bounded common scope."""

    from_ancestors: tuple[str, ...]
    to_ancestors: tuple[str, ...]
    ancestor_union: tuple[str, ...]
    lca: str

    def __post_init__(self) -> None:
        from_ancestors = _ancestor_chain(
            self.from_ancestors, "from endpoint hierarchy ancestor"
        )
        to_ancestors = _ancestor_chain(
            self.to_ancestors, "to endpoint hierarchy ancestor"
        )
        if from_ancestors[0] != to_ancestors[0]:
            raise ValueError("path endpoint ancestor chains must share one top")
        common = from_ancestors[0]
        for left, right in zip(from_ancestors, to_ancestors):
            if left != right:
                break
            common = left
        lca = _hier_path(self.lca, "path hierarchy lca")
        if lca != common:
            raise ValueError(
                "path hierarchy lca must be the proved lowest common ancestor"
            )
        ancestor_union = tuple(
            _hier_path(path, "path hierarchy ancestor union")
            for path in self.ancestor_union
        )
        expected_union = _path_union(from_ancestors, to_ancestors)
        if ancestor_union != expected_union:
            raise ValueError(
                "path hierarchy ancestor_union must exactly cover both endpoint chains"
            )
        object.__setattr__(self, "from_ancestors", from_ancestors)
        object.__setattr__(self, "to_ancestors", to_ancestors)
        object.__setattr__(self, "ancestor_union", ancestor_union)
        object.__setattr__(self, "lca", lca)

    @property
    def top(self) -> str:
        return self.from_ancestors[0]

    def to_dict(self) -> dict[str, Any]:
        return {
            "from_ancestors": list(self.from_ancestors),
            "to_ancestors": list(self.to_ancestors),
            "ancestor_union": list(self.ancestor_union),
            "lca": self.lca,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> PathHierarchyScope:
        return cls(
            from_ancestors=tuple(value.get("from_ancestors", ())),
            to_ancestors=tuple(value.get("to_ancestors", ())),
            ancestor_union=tuple(value.get("ancestor_union", ())),
            lca=value["lca"],
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
    target: ConnectivityTarget | ConnectivityPathTarget
    hierarchy_ancestors: tuple[str, ...]
    requested_cone: RequestedCone
    coverage_boundary: CoverageBoundary
    path_hierarchy: PathHierarchyScope | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "design", _required_text(self.design, "design"))
        top = _hier_path(self.top, "top")
        object.__setattr__(self, "top", top)
        ancestors = tuple(
            _hier_path(path, "hierarchy ancestor") for path in self.hierarchy_ancestors
        )
        if isinstance(self.target, ConnectivityPathTarget):
            if self.path_hierarchy is None:
                raise ValueError("path target requires a dual-endpoint hierarchy scope")
            if self.path_hierarchy.top != top:
                raise ValueError("path endpoint hierarchy must belong to the scope top")
            if (
                self.path_hierarchy.from_ancestors[-1] != self.target.from_instance_path
                or self.path_hierarchy.to_ancestors[-1] != self.target.to_instance_path
            ):
                raise ValueError(
                    "path endpoint hierarchy must end at both target instances"
                )
            if ancestors != self.path_hierarchy.ancestor_union:
                raise ValueError(
                    "path hierarchy ancestors must equal the proved endpoint ancestor union"
                )
            target_instances = {
                self.target.from_instance_path,
                self.target.to_instance_path,
            }
        else:
            if self.path_hierarchy is not None:
                raise ValueError("single-endpoint target cannot carry path hierarchy")
            if not ancestors or ancestors[0] != top:
                raise ValueError("hierarchy ancestors must start at top")
            if ancestors[-1] != self.target.instance_path:
                raise ValueError("hierarchy ancestors must end at the target instance")
            for parent, child in zip(ancestors, ancestors[1:]):
                if not child.startswith(parent + "."):
                    raise ValueError("hierarchy ancestors must be ordered root-to-leaf")
            target_instances = {self.target.instance_path}
        if self.requested_cone.operation is not self.target.operation:
            raise ValueError("requested cone operation must match target operation")
        if not target_instances.issubset(self.requested_cone.instance_paths):
            raise ValueError("requested cone must include every target instance")
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
            "path_hierarchy": (
                self.path_hierarchy.to_dict()
                if self.path_hierarchy is not None
                else None
            ),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphBuildScope:
        target = value.get("target")
        cone = value.get("requested_cone")
        boundary = value.get("coverage_boundary")
        path_hierarchy = value.get("path_hierarchy")
        if not all(isinstance(item, Mapping) for item in (target, cone, boundary)):
            raise ValueError(
                "target, requested_cone, and coverage_boundary are required"
            )
        return cls(
            design=value["design"],
            top=value["top"],
            target=_target_from_dict(target),
            hierarchy_ancestors=tuple(value.get("hierarchy_ancestors", ())),
            requested_cone=RequestedCone.from_dict(cone),
            coverage_boundary=CoverageBoundary.from_dict(boundary),
            path_hierarchy=(
                PathHierarchyScope.from_dict(path_hierarchy)
                if isinstance(path_hierarchy, Mapping)
                else None
            ),
        )


@dataclass(frozen=True)
class SourceGraphBuildRequest:
    identity: SourceGraphIdentity
    scope: SourceGraphBuildScope
    artifact: SourceGraphArtifactIdentity | None = None
    query: SourceGraphQueryIdentity | None = None
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
        if self.artifact is not None:
            if self.artifact.source != self.identity:
                raise ValueError("artifact source identity must match build identity")
            if (
                self.artifact.scope.design != self.scope.design
                or self.artifact.scope.top != self.scope.top
            ):
                raise ValueError("artifact scope must match build design and top")
            if self.scope.target.operation not in self.artifact.scope.capabilities:
                raise ValueError("artifact lacks the requested query capability")
            requested_artifact_scope = SourceGraphArtifactScope.from_build_scope(
                self.scope,
                hierarchy_snapshot_sha256=(
                    self.artifact.scope.hierarchy_snapshot_sha256
                ),
                capabilities=(self.scope.target.operation,),
            )
            if not _artifact_scope_covers(
                self.artifact.scope, requested_artifact_scope
            ):
                raise ValueError("artifact scope does not cover the query scope")
        if self.query is not None and self.query.target != self.scope.target:
            raise ValueError("query identity target must match build scope target")

    @property
    def artifact_identity(self) -> SourceGraphArtifactIdentity:
        if self.artifact is not None:
            return self.artifact
        return _legacy_exact_artifact_identity(self.identity, self.scope)

    @property
    def query_identity(self) -> SourceGraphQueryIdentity:
        return self.query or SourceGraphQueryIdentity.from_build_scope(self.scope)

    @property
    def artifact_build_request(self) -> SourceGraphArtifactBuildRequest:
        return SourceGraphArtifactBuildRequest(identity=self.artifact_identity)

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "identity": self.identity.to_dict(),
            "scope": self.scope.to_dict(),
            "artifact_identity": self.artifact_identity.to_dict(),
            "query_identity": self.query_identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphBuildRequest:
        identity = value.get("identity")
        scope = value.get("scope")
        if not isinstance(identity, Mapping) or not isinstance(scope, Mapping):
            raise ValueError("identity and scope are required")
        source_identity = SourceGraphIdentity.from_dict(identity)
        build_scope = SourceGraphBuildScope.from_dict(scope)
        contract_version = value.get(
            "contract_version", SOURCE_GRAPH_BUILD_CONTRACT_VERSION
        )
        legacy = cls(
            identity=source_identity,
            scope=build_scope,
            contract_version=contract_version,
        )
        artifact_value = value.get("artifact_identity")
        query_value = value.get("query_identity")
        artifact = (
            SourceGraphArtifactIdentity.from_dict(artifact_value)
            if isinstance(artifact_value, Mapping)
            else None
        )
        query = (
            SourceGraphQueryIdentity.from_dict(query_value)
            if isinstance(query_value, Mapping)
            else None
        )
        if artifact == legacy.artifact_identity and query == legacy.query_identity:
            return legacy
        return cls(
            identity=source_identity,
            scope=build_scope,
            artifact=artifact,
            query=query,
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


@dataclass(frozen=True)
class SourceGraphArtifactScope:
    """Canonical, hierarchy-proved scope of one reusable IR artifact.

    The scope deliberately contains no query operation, signal target,
    presentation flag, or traversal/output cap.  The worker projects only the
    explicit assignment paths and binding skeleton in ``coverage_boundary``;
    those are therefore the artifact-affecting scope inputs.

    ``proved_ancestor_chains`` and ``proved_lcas`` are receipts from the
    hierarchy handle.  Reuse compares their exact path atoms and never derives
    scope from source/module names or unproved textual prefixes.
    """

    design: str
    top: str
    hierarchy_snapshot_sha256: str
    proved_ancestor_chains: tuple[tuple[str, ...], ...]
    proved_lcas: tuple[str, ...]
    projection_instance_paths: tuple[str, ...]
    coverage_boundary: CoverageBoundary
    capabilities: tuple[QueryOperation, ...] = (
        QueryOperation.DRIVER,
        QueryOperation.LOADS,
        QueryOperation.PATH,
    )

    def __post_init__(self) -> None:
        object.__setattr__(self, "design", _required_text(self.design, "design"))
        top = _hier_path(self.top, "top")
        object.__setattr__(self, "top", top)
        snapshot = _required_text(
            self.hierarchy_snapshot_sha256, "hierarchy snapshot fingerprint"
        ).lower()
        if not _SHA256_RE.fullmatch(snapshot):
            raise ValueError(
                "hierarchy snapshot fingerprint must be a SHA-256 hex digest"
            )
        object.__setattr__(self, "hierarchy_snapshot_sha256", snapshot)

        chains = tuple(
            sorted(
                {
                    _ancestor_chain(chain, "proved hierarchy ancestor")
                    for chain in self.proved_ancestor_chains
                },
                key=lambda chain: (len(chain), chain),
            )
        )
        if not chains:
            raise ValueError("artifact scope requires a proved hierarchy chain")
        if any(chain[0] != top for chain in chains):
            raise ValueError("every proved hierarchy chain must start at top")
        object.__setattr__(self, "proved_ancestor_chains", chains)

        lcas = _path_set(self.proved_lcas, "proved hierarchy lca")
        chain_atoms = set().union(*(set(chain) for chain in chains))
        if len(lcas) != 1:
            raise ValueError("artifact scope requires exactly one proved hierarchy LCA")
        common_prefix = chains[0]
        for chain in chains[1:]:
            prefix_length = 0
            for left, right in zip(common_prefix, chain):
                if left != right:
                    break
                prefix_length += 1
            common_prefix = common_prefix[:prefix_length]
        if not common_prefix or lcas[0] != common_prefix[-1]:
            raise ValueError(
                "proved hierarchy LCA must equal the canonical chain intersection"
            )
        object.__setattr__(self, "proved_lcas", lcas)

        projection_paths = _path_set(
            self.projection_instance_paths, "artifact projection instance path"
        )
        if not projection_paths:
            raise ValueError("artifact scope requires a bounded projection path")
        if not set(projection_paths).issubset(chain_atoms):
            raise ValueError(
                "artifact projection paths must occur in proved ancestor chains"
            )
        object.__setattr__(self, "projection_instance_paths", projection_paths)

        if self.coverage_boundary.explicit:
            required = chain_atoms | set(projection_paths)
            if not required.issubset(self.coverage_boundary.instance_paths):
                raise ValueError(
                    "artifact coverage boundary must include every proved and "
                    "projected path"
                )

        capabilities = tuple(
            sorted(
                {QueryOperation(item) for item in self.capabilities},
                key=lambda item: item.value,
            )
        )
        if not capabilities:
            raise ValueError("artifact scope requires at least one query capability")
        object.__setattr__(self, "capabilities", capabilities)

    @classmethod
    def from_build_scope(
        cls,
        scope: SourceGraphBuildScope,
        *,
        hierarchy_snapshot_sha256: str,
        capabilities: Sequence[QueryOperation | str] = (
            QueryOperation.DRIVER,
            QueryOperation.LOADS,
            QueryOperation.PATH,
        ),
    ) -> SourceGraphArtifactScope:
        if scope.path_hierarchy is None:
            chains = (scope.hierarchy_ancestors,)
            # A single proved chain has one trivial common anchor: its leaf.
            # Recording it makes a same-instance dual-endpoint request canonical
            # with driver/load requests over that exact bounded projection.
            lcas: tuple[str, ...] = (scope.hierarchy_ancestors[-1],)
        else:
            chains = (
                scope.path_hierarchy.from_ancestors,
                scope.path_hierarchy.to_ancestors,
            )
            lcas = (scope.path_hierarchy.lca,)
        return cls(
            design=scope.design,
            top=scope.top,
            hierarchy_snapshot_sha256=hierarchy_snapshot_sha256,
            proved_ancestor_chains=chains,
            proved_lcas=lcas,
            projection_instance_paths=scope.requested_cone.instance_paths,
            coverage_boundary=scope.coverage_boundary,
            capabilities=tuple(QueryOperation(item) for item in capabilities),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "design": self.design,
            "top": self.top,
            "hierarchy_snapshot_sha256": self.hierarchy_snapshot_sha256,
            "proved_ancestor_chains": [
                list(chain) for chain in self.proved_ancestor_chains
            ],
            "proved_lcas": list(self.proved_lcas),
            "projection_instance_paths": list(self.projection_instance_paths),
            "coverage_boundary": self.coverage_boundary.to_dict(),
            "capabilities": [item.value for item in self.capabilities],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphArtifactScope:
        boundary = value.get("coverage_boundary")
        if not isinstance(boundary, Mapping):
            raise ValueError("artifact coverage_boundary must be an object")
        return cls(
            design=value["design"],
            top=value["top"],
            hierarchy_snapshot_sha256=value["hierarchy_snapshot_sha256"],
            proved_ancestor_chains=tuple(
                tuple(chain) for chain in value.get("proved_ancestor_chains", ())
            ),
            proved_lcas=tuple(value.get("proved_lcas", ())),
            projection_instance_paths=tuple(value.get("projection_instance_paths", ())),
            coverage_boundary=CoverageBoundary.from_dict(boundary),
            capabilities=tuple(
                QueryOperation(item) for item in value.get("capabilities", ())
            ),
        )


@dataclass(frozen=True)
class SourceGraphArtifactIdentity:
    """All inputs that can change a prepared Source Graph artifact."""

    source: SourceGraphIdentity
    scope: SourceGraphArtifactScope
    compile_snapshot_sha256: str
    adapter_version: str
    worker_protocol_version: str
    snapshots_complete: bool = True
    identity_version: str = SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION
    build_contract_version: str = SOURCE_GRAPH_BUILD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        for field_name in (
            "compile_snapshot_sha256",
            "adapter_version",
            "worker_protocol_version",
            "identity_version",
            "build_contract_version",
        ):
            value = _required_text(getattr(self, field_name), field_name)
            if field_name == "compile_snapshot_sha256":
                value = value.lower()
                if not _SHA256_RE.fullmatch(value):
                    raise ValueError(
                        "compile snapshot fingerprint must be a SHA-256 hex digest"
                    )
            object.__setattr__(self, field_name, value)
        if self.identity_version != SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION:
            raise ValueError("unsupported Source Graph artifact identity version")
        if not isinstance(self.snapshots_complete, bool):
            raise ValueError("artifact snapshots_complete must be boolean")
        if (
            self.source.compile_inputs.tops_complete
            and self.source.compile_inputs.ordered_tops
            and self.scope.top not in self.source.compile_inputs.ordered_tops
        ):
            raise ValueError(
                "artifact top is absent from the complete compile top list"
            )

    def to_dict(self) -> dict[str, Any]:
        return {
            "identity_version": self.identity_version,
            "build_contract_version": self.build_contract_version,
            "source": self.source.to_dict(),
            "scope": self.scope.to_dict(),
            "compile_snapshot_sha256": self.compile_snapshot_sha256,
            "adapter_version": self.adapter_version,
            "worker_protocol_version": self.worker_protocol_version,
            "snapshots_complete": self.snapshots_complete,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphArtifactIdentity:
        source = value.get("source")
        scope = value.get("scope")
        if not isinstance(source, Mapping) or not isinstance(scope, Mapping):
            raise ValueError("artifact source and scope must be objects")
        return cls(
            source=SourceGraphIdentity.from_dict(source),
            scope=SourceGraphArtifactScope.from_dict(scope),
            compile_snapshot_sha256=value["compile_snapshot_sha256"],
            adapter_version=value["adapter_version"],
            worker_protocol_version=value["worker_protocol_version"],
            snapshots_complete=bool(value.get("snapshots_complete", False)),
            identity_version=value.get(
                "identity_version", SOURCE_GRAPH_ARTIFACT_IDENTITY_VERSION
            ),
            build_contract_version=value.get(
                "build_contract_version", SOURCE_GRAPH_BUILD_CONTRACT_VERSION
            ),
        )


def _legacy_exact_artifact_identity(
    source: SourceGraphIdentity,
    scope: SourceGraphBuildScope,
) -> SourceGraphArtifactIdentity:
    """Adapt pre-3B requests without granting cross-target scope reuse.

    Old internal benchmark callers supplied a finite coverage boundary and an
    assignment projection, but not a hierarchy-handle snapshot.  Some of those
    projections deliberately contain several explicitly listed branches.  Keep
    them runnable and exact-repeat cacheable by keying the *entire* legacy scope
    (including target, operation, caps, and cone policy) into build semantics.
    Consequently this compatibility route can never provide a target-changing
    or dominating hit.  Production adapters always pass the explicit Phase 3B
    identity and do not use this path.

    Every compatibility chain atom below already occurs in the request's
    explicit boundary; no sibling is enumerated and no source/module name is
    used to infer coverage.
    """

    if scope.path_hierarchy is None:
        chains = [scope.hierarchy_ancestors]
    else:
        chains = [
            scope.path_hierarchy.from_ancestors,
            scope.path_hierarchy.to_ancestors,
        ]
    chain_atoms = set().union(*(set(chain) for chain in chains))
    for path in scope.requested_cone.instance_paths:
        if path in chain_atoms:
            continue
        chains.append((scope.top,) if path == scope.top else (scope.top, path))
        chain_atoms.add(path)

    common_prefix = chains[0]
    for chain in chains[1:]:
        prefix_length = 0
        for left, right in zip(common_prefix, chain):
            if left != right:
                break
            prefix_length += 1
        common_prefix = common_prefix[:prefix_length]
    if not common_prefix:
        raise ValueError("legacy scope paths do not share the declared top")

    hierarchy_snapshot = _sha256(
        {
            "legacy_exact_boundary": True,
            "design": scope.design,
            "top": scope.top,
            "chains": [list(chain) for chain in chains],
            "projection_instance_paths": list(scope.requested_cone.instance_paths),
            "coverage_boundary": scope.coverage_boundary.to_dict(),
        }
    )
    compile_snapshot = _sha256(
        {
            "compile_inputs": source.compile_inputs.to_dict(),
            "legacy_exact_request_scope": scope.to_dict(),
        }
    )
    artifact_scope = SourceGraphArtifactScope(
        design=scope.design,
        top=scope.top,
        hierarchy_snapshot_sha256=hierarchy_snapshot,
        proved_ancestor_chains=tuple(chains),
        proved_lcas=(common_prefix[-1],),
        projection_instance_paths=scope.requested_cone.instance_paths,
        coverage_boundary=scope.coverage_boundary,
        capabilities=(scope.target.operation,),
    )
    return SourceGraphArtifactIdentity(
        source=source,
        scope=artifact_scope,
        compile_snapshot_sha256=compile_snapshot,
        adapter_version="legacy_exact_request_2_0",
        worker_protocol_version=SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    )


@dataclass(frozen=True)
class SourceGraphArtifactBuildRequest:
    """Worker-facing request containing artifact semantics and no query state."""

    identity: SourceGraphArtifactIdentity
    contract_version: str = SOURCE_GRAPH_BUILD_CONTRACT_VERSION

    def __post_init__(self) -> None:
        contract_version = _required_text(self.contract_version, "contract_version")
        if contract_version != SOURCE_GRAPH_BUILD_CONTRACT_VERSION:
            raise ValueError(
                f"unsupported Source Graph build contract version: {contract_version}"
            )
        object.__setattr__(self, "contract_version", contract_version)

    @property
    def source(self) -> SourceGraphIdentity:
        return self.identity.source

    @property
    def scope(self) -> SourceGraphArtifactScope:
        return self.identity.scope

    def to_dict(self) -> dict[str, Any]:
        return {
            "contract_version": self.contract_version,
            "artifact_identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphArtifactBuildRequest:
        identity = value.get("artifact_identity")
        if not isinstance(identity, Mapping):
            raise ValueError("worker request requires artifact_identity")
        return cls(
            identity=SourceGraphArtifactIdentity.from_dict(identity),
            contract_version=value.get(
                "contract_version", SOURCE_GRAPH_BUILD_CONTRACT_VERSION
            ),
        )


@dataclass(frozen=True)
class SourceGraphQueryIdentity:
    """Query and public-mapping semantics over an already prepared artifact."""

    target: ConnectivityTarget | ConnectivityPathTarget
    max_depth: int | None = None
    recursive: bool = False
    include_expr: bool = True
    kind_filter: tuple[str, ...] = ()
    mapping_version: str = SOURCE_GRAPH_QUERY_MAPPING_VERSION
    identity_version: str = SOURCE_GRAPH_QUERY_IDENTITY_VERSION

    def __post_init__(self) -> None:
        operation = self.target.operation
        if operation is QueryOperation.PATH:
            if self.max_depth is not None:
                raise ValueError("path query identity must use traversal_limit")
            if self.recursive or not self.include_expr or self.kind_filter:
                raise ValueError(
                    "path query identity carries unsupported query options"
                )
        else:
            if self.max_depth is None:
                raise ValueError("driver/load query identity requires max_depth")
            if (
                not isinstance(self.max_depth, int)
                or isinstance(self.max_depth, bool)
                or self.max_depth < 0
            ):
                raise ValueError("query max_depth must be a non-negative integer")
            if operation is QueryOperation.DRIVER and (
                not self.include_expr or self.kind_filter
            ):
                raise ValueError("driver query identity carries load-only options")
            if operation is QueryOperation.LOADS and self.recursive:
                raise ValueError("load query identity carries driver-only recursion")
        if not isinstance(self.recursive, bool) or not isinstance(
            self.include_expr, bool
        ):
            raise ValueError("query mapping flags must be boolean")
        object.__setattr__(
            self,
            "kind_filter",
            _fixed_labels(self.kind_filter, "query kind filter"),
        )
        for field_name, expected in (
            ("mapping_version", SOURCE_GRAPH_QUERY_MAPPING_VERSION),
            ("identity_version", SOURCE_GRAPH_QUERY_IDENTITY_VERSION),
        ):
            value = _required_text(getattr(self, field_name), field_name)
            if value != expected:
                raise ValueError(f"unsupported Source Graph {field_name}")
            object.__setattr__(self, field_name, value)

    @classmethod
    def from_build_scope(
        cls,
        scope: SourceGraphBuildScope,
        *,
        recursive: bool = False,
        include_expr: bool = True,
        kind_filter: Sequence[str] = (),
    ) -> SourceGraphQueryIdentity:
        max_depth = (
            None
            if scope.target.operation is QueryOperation.PATH
            else scope.requested_cone.max_hops
        )
        return cls(
            target=scope.target,
            max_depth=max_depth,
            recursive=recursive,
            include_expr=include_expr,
            kind_filter=tuple(kind_filter),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "identity_version": self.identity_version,
            "mapping_version": self.mapping_version,
            "target": self.target.to_dict(),
        }
        if self.target.operation is QueryOperation.DRIVER:
            result.update(max_depth=self.max_depth, recursive=self.recursive)
        elif self.target.operation is QueryOperation.LOADS:
            result.update(
                max_depth=self.max_depth,
                include_expr=self.include_expr,
                kind_filter=list(self.kind_filter),
            )
        return result

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphQueryIdentity:
        target_value = value.get("target")
        if not isinstance(target_value, Mapping):
            raise ValueError("query target must be an object")
        target = _target_from_dict(target_value)
        return cls(
            target=target,
            max_depth=(
                None
                if target.operation is QueryOperation.PATH
                else int(value["max_depth"])
            ),
            recursive=bool(value.get("recursive", False)),
            include_expr=bool(value.get("include_expr", True)),
            kind_filter=tuple(value.get("kind_filter", ())),
            mapping_version=value.get(
                "mapping_version", SOURCE_GRAPH_QUERY_MAPPING_VERSION
            ),
            identity_version=value.get(
                "identity_version", SOURCE_GRAPH_QUERY_IDENTITY_VERSION
            ),
        )


@dataclass(frozen=True)
class SourceGraphArtifactKey:
    digest: str
    build_semantics_digest: str
    scope_digest: str
    cross_request_reusable: bool
    incomplete_reasons: tuple[str, ...]

    @property
    def cache_key(self) -> str | None:
        return self.digest if self.cross_request_reusable else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "digest": self.digest,
            "build_semantics_digest": self.build_semantics_digest,
            "scope_digest": self.scope_digest,
            "cross_request_reusable": self.cross_request_reusable,
            "incomplete_reasons": list(self.incomplete_reasons),
        }


@dataclass(frozen=True)
class SourceGraphQueryKey:
    digest: str

    def to_dict(self) -> dict[str, str]:
        return {"digest": self.digest}


def compute_source_graph_artifact_key(
    identity: SourceGraphArtifactIdentity,
) -> SourceGraphArtifactKey:
    source_payload = identity.source.to_dict()
    build_semantics_digest = _sha256(
        {
            "source": source_payload,
            "compile_snapshot_sha256": identity.compile_snapshot_sha256,
            "adapter_version": identity.adapter_version,
            "worker_protocol_version": identity.worker_protocol_version,
            "build_contract_version": identity.build_contract_version,
            "identity_version": identity.identity_version,
        }
    )
    scope_digest = _sha256(identity.scope.to_dict())
    digest = _sha256(
        {
            "build_semantics_digest": build_semantics_digest,
            "scope_digest": scope_digest,
        }
    )
    reasons = list(identity.source.compile_inputs.incomplete_reasons)
    if not identity.snapshots_complete:
        reasons.append("snapshot_identity_unproved")
    if not identity.scope.coverage_boundary.explicit:
        reasons.append("scope_boundary_unscoped")
    incomplete_reasons = tuple(sorted(set(reasons)))
    return SourceGraphArtifactKey(
        digest=digest,
        build_semantics_digest=build_semantics_digest,
        scope_digest=scope_digest,
        cross_request_reusable=not incomplete_reasons,
        incomplete_reasons=incomplete_reasons,
    )


def compute_source_graph_query_key(
    identity: SourceGraphQueryIdentity,
) -> SourceGraphQueryKey:
    return SourceGraphQueryKey(digest=_sha256(identity.to_dict()))


def _chain_covered_by(
    available_chains: tuple[tuple[str, ...], ...],
    requested_chain: tuple[str, ...],
) -> bool:
    # Compare exact hierarchy-handle atoms.  A requested ancestor prefix is
    # proved by a longer chain; arbitrary string-prefix matching is forbidden.
    return any(
        len(available) >= len(requested_chain)
        and available[: len(requested_chain)] == requested_chain
        for available in available_chains
    )


def _artifact_scope_covers(
    available: SourceGraphArtifactScope,
    requested: SourceGraphArtifactScope,
) -> bool:
    if (
        available.design != requested.design
        or available.top != requested.top
        or available.hierarchy_snapshot_sha256 != requested.hierarchy_snapshot_sha256
        or not available.coverage_boundary.explicit
        or not requested.coverage_boundary.explicit
    ):
        return False
    if available.coverage_boundary.objective_exclusions != (
        requested.coverage_boundary.objective_exclusions
    ):
        return False
    if not set(requested.capabilities).issubset(available.capabilities):
        return False
    if not set(requested.projection_instance_paths).issubset(
        available.projection_instance_paths
    ):
        return False
    if not set(requested.coverage_boundary.instance_paths).issubset(
        available.coverage_boundary.instance_paths
    ):
        return False
    available_chain_atoms = set().union(
        *(set(chain) for chain in available.proved_ancestor_chains)
    )
    if not set(requested.proved_lcas).issubset(available_chain_atoms):
        return False
    return all(
        _chain_covered_by(available.proved_ancestor_chains, requested_chain)
        for requested_chain in requested.proved_ancestor_chains
    )


def compare_source_graph_artifact_scopes(
    available: SourceGraphArtifactScope,
    requested: SourceGraphArtifactScope,
) -> ScopeRelation:
    if available.design != requested.design or available.top != requested.top:
        return ScopeRelation.DISJOINT
    if (
        available.hierarchy_snapshot_sha256 != requested.hierarchy_snapshot_sha256
        or not available.coverage_boundary.explicit
        or not requested.coverage_boundary.explicit
    ):
        return ScopeRelation.UNPROVEN
    if available.to_dict() == requested.to_dict():
        return ScopeRelation.EXACT
    if _artifact_scope_covers(available, requested):
        return ScopeRelation.SUPERSET
    if _artifact_scope_covers(requested, available):
        return ScopeRelation.SUBSET
    return ScopeRelation.UNPROVEN


@dataclass(frozen=True)
class SourceGraphArtifactScopeReceipt:
    scope: SourceGraphArtifactScope
    coverage_status: CoverageStatus
    gap_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        status = CoverageStatus(self.coverage_status)
        gap_codes = _fixed_labels(self.gap_codes, "artifact scope gap code")
        if status is CoverageStatus.COMPLETE:
            if not self.scope.coverage_boundary.explicit:
                raise ValueError("unscoped artifact coverage cannot be complete")
            if gap_codes or self.scope.coverage_boundary.objective_exclusions:
                raise ValueError(
                    "artifact coverage with gaps or exclusions cannot be complete"
                )
        elif (
            self.scope.coverage_boundary.explicit
            and not gap_codes
            and not self.scope.coverage_boundary.objective_exclusions
        ):
            raise ValueError(
                "partial or inconclusive artifact coverage requires a gap receipt"
            )
        object.__setattr__(self, "coverage_status", status)
        object.__setattr__(self, "gap_codes", gap_codes)

    def reuse_for(self, requested: SourceGraphArtifactScope) -> ScopeReuseDecision:
        relation = compare_source_graph_artifact_scopes(self.scope, requested)
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
    def from_dict(cls, value: Mapping[str, Any]) -> SourceGraphArtifactScopeReceipt:
        scope = value.get("scope")
        if not isinstance(scope, Mapping):
            raise ValueError("artifact scope receipt requires a scope object")
        return cls(
            scope=SourceGraphArtifactScope.from_dict(scope),
            coverage_status=CoverageStatus(value["coverage_status"]),
            gap_codes=tuple(value.get("gap_codes", ())),
        )


def compute_source_graph_build_key(
    request: SourceGraphBuildRequest,
) -> SourceGraphBuildKey:
    artifact = compute_source_graph_artifact_key(request.artifact_identity)
    return SourceGraphBuildKey(
        digest=artifact.digest,
        design_digest=artifact.build_semantics_digest,
        scope_digest=artifact.scope_digest,
        cross_request_reusable=artifact.cross_request_reusable,
        incomplete_reasons=artifact.incomplete_reasons,
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
