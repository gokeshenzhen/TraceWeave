"""Bounded hierarchy views shared by lexical and semantic backends.

The public hierarchy handle remains a compatibility nested dictionary.  This
module defines the internal contract used by connectivity planning: resolve one
signal to an exact ancestor chain, look up one instance binding, or enumerate a
bounded set of direct children.  Providers must never require a full subtree
materialization for those operations.

The compile-log provider exposes only hierarchy edges already admitted by
``build_tb_hierarchy``.  The Connectivity-IR provider consumes elaborated
``InstanceDecl`` records, including generate scopes and parameter
specializations, without making the basic hierarchy build depend on Slang.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
import hashlib
import re
from typing import Any, Protocol

from .cancellation import check_cancelled
from .connectivity_ir import ConnectivityIR, DefinitionTemplate, InstanceDecl


_FIXED_LABEL_RE = re.compile(r"^[a-z][a-z0-9_]*$")
HIERARCHY_INFORMATIONAL_GAP_CODES = frozenset(
    {
        # The lexical hierarchy does not materialize specializations, but the
        # Slang provider does.  A direct parameterized edge remains valid scope
        # evidence and therefore is not a Source Graph coverage exclusion.
        "hierarchy_parameter_specialization_unmodeled",
    }
)


class HierarchyProviderKind(str, Enum):
    COMPILE_LEXICAL = "compile_lexical"
    SLANG_IR = "slang_ir"
    VERDI_NPI = "verdi_npi"


def _fixed_label(value: str, label: str) -> str:
    if not isinstance(value, str) or not _FIXED_LABEL_RE.fullmatch(value):
        raise ValueError(f"{label} must be a fixed snake_case label")
    return value


def _semantic_gap_label(code: str) -> str:
    """Collapse frontend-specific diagnostic names to privacy-safe labels."""

    if _FIXED_LABEL_RE.fullmatch(code):
        return code
    namespace, separator, detail = code.partition(":")
    if namespace == "frontend_diagnostic" and separator and detail:
        return "frontend_diagnostic"
    return "semantic_hierarchy_gap"


def _instance_id(
    *,
    provider_kind: HierarchyProviderKind,
    design_identity: str,
    path: str,
    definition_id: str,
) -> str:
    payload = "\0".join(
        (provider_kind.value, design_identity, path, definition_id)
    ).encode("utf-8")
    return f"hinst_{hashlib.sha256(payload).hexdigest()[:32]}"


@dataclass(frozen=True)
class HierarchyInstanceBinding:
    """One provider-local instance -> definition binding.

    ``instance_id`` is stable for the provider's immutable design identity; it
    is not a cross-snapshot public identifier.  Definition facts are shared by
    ID while path/source/generate facts remain instance-specific.
    """

    provider_kind: HierarchyProviderKind
    instance_id: str
    path: str
    name: str
    definition_id: str
    definition_name: str
    parent_path: str | None
    source_file: str | None = None
    source_line: int | None = None
    source_origin: str | None = None
    generate_scope: str | None = None
    parameterization: tuple[tuple[str, str], ...] = ()
    gap_codes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "provider_kind", HierarchyProviderKind(self.provider_kind)
        )
        if not self.instance_id.startswith("hinst_"):
            raise ValueError("hierarchy instance id must use the hinst_ namespace")
        if not self.path or not self.name or not self.definition_id:
            raise ValueError("hierarchy binding path/name/definition must not be empty")
        if not self.definition_name:
            raise ValueError("hierarchy binding definition name must not be empty")
        if self.parent_path is not None and not self.path.startswith(
            f"{self.parent_path}."
        ):
            raise ValueError("hierarchy binding parent must be an exact path ancestor")
        if self.source_line is not None and self.source_line < 1:
            raise ValueError("hierarchy binding source line must be positive")
        if self.source_origin is not None:
            _fixed_label(self.source_origin, "hierarchy source origin")
        parameter_names = [name for name, _ in self.parameterization]
        if len(parameter_names) != len(set(parameter_names)):
            raise ValueError("hierarchy binding parameters must be unique")
        gaps = tuple(sorted(set(str(code) for code in self.gap_codes)))
        for code in gaps:
            _fixed_label(code, "hierarchy gap code")
        object.__setattr__(self, "gap_codes", gaps)


@dataclass(frozen=True)
class HierarchyAncestorResolution:
    """Bounded path-resolution evidence used by Source Graph planning.

    A dotted suffix after the last proved instance can be an interface or
    packed member, so it is deferred unless independent evidence proves it was
    intended as a child instance.  ``bindings`` is an internal instance table;
    the existing public receipt continues to expose only aggregate counts.
    """

    ancestors: tuple[str, ...]
    remaining_path_segment_count: int
    stop_depth: int | None = None
    candidate_instance_path: str | None = None
    missing_instance_proved: bool = False
    gap_codes: tuple[str, ...] = ()
    provider_kind: HierarchyProviderKind = HierarchyProviderKind.COMPILE_LEXICAL
    bindings: tuple[HierarchyInstanceBinding, ...] = ()

    def __post_init__(self) -> None:
        ancestors = tuple(str(path) for path in self.ancestors)
        if not ancestors or any(not path for path in ancestors):
            raise ValueError("hierarchy resolution requires proved ancestors")
        if self.remaining_path_segment_count < 1:
            raise ValueError("hierarchy resolution must retain a signal suffix")
        if self.candidate_instance_path is None:
            if self.stop_depth is not None or self.missing_instance_proved:
                raise ValueError(
                    "resolved hierarchy path cannot carry missing-instance evidence"
                )
        elif not self.candidate_instance_path or self.stop_depth is None:
            raise ValueError(
                "deferred hierarchy path requires a candidate and stop depth"
            )
        elif self.stop_depth < 1:
            raise ValueError("hierarchy stop depth must be positive")
        gaps = tuple(sorted(set(str(code) for code in self.gap_codes)))
        for code in gaps:
            _fixed_label(code, "hierarchy gap code")
        provider_kind = HierarchyProviderKind(self.provider_kind)
        bindings = tuple(self.bindings)
        if bindings:
            if tuple(item.path for item in bindings) != ancestors:
                raise ValueError("hierarchy bindings must exactly match ancestor order")
            if any(item.provider_kind is not provider_kind for item in bindings):
                raise ValueError("hierarchy resolution cannot mix provider bindings")
        object.__setattr__(self, "ancestors", ancestors)
        object.__setattr__(self, "gap_codes", gaps)
        object.__setattr__(self, "provider_kind", provider_kind)
        object.__setattr__(self, "bindings", bindings)

    @property
    def status(self) -> str:
        if self.candidate_instance_path is None:
            return "resolved"
        if self.missing_instance_proved:
            return "truncated"
        return "deferred"

    @property
    def coverage_gap_codes(self) -> tuple[str, ...]:
        return tuple(
            code
            for code in self.gap_codes
            if code not in HIERARCHY_INFORMATIONAL_GAP_CODES
        )


@dataclass(frozen=True)
class HierarchyChildrenResult:
    parent_path: str
    children: tuple[HierarchyInstanceBinding, ...]
    truncated: bool
    available_count: int | None = None

    def __post_init__(self) -> None:
        if not self.parent_path:
            raise ValueError("hierarchy children require a parent path")
        children = tuple(self.children)
        if any(item.parent_path != self.parent_path for item in children):
            raise ValueError("hierarchy child result contains a different parent")
        if self.available_count is not None:
            if self.available_count < len(children):
                raise ValueError("available child count is smaller than the payload")
            if self.truncated != (self.available_count > len(children)):
                raise ValueError("hierarchy child truncation/count mismatch")
        object.__setattr__(self, "children", children)

    @property
    def paths(self) -> tuple[str, ...]:
        return tuple(item.path for item in self.children)


class HierarchyProvider(Protocol):
    """Read-only, bounded hierarchy access over one immutable design view."""

    kind: HierarchyProviderKind
    design_identity: str

    def resolve_scope(
        self, *, top: str, signal_path: str
    ) -> HierarchyAncestorResolution | None: ...

    def lookup_instance(
        self, *, top: str, instance_path: str
    ) -> HierarchyInstanceBinding | None: ...

    def direct_children(
        self,
        *,
        top: str,
        instance_path: str,
        max_children: int | None,
    ) -> HierarchyChildrenResult | None: ...


def _module_gap_codes(
    hierarchy_result: Mapping[str, Any], module_name: str
) -> tuple[str, ...]:
    scans = hierarchy_result.get("_scan_results")
    if not isinstance(scans, Sequence) or isinstance(scans, (str, bytes)):
        return ()
    gap_codes: set[str] = set()
    definition_count = 0
    for scan in scans:
        check_cancelled()
        if not isinstance(scan, Mapping):
            continue
        raw_modules = scan.get("structural_modules")
        if raw_modules is None:
            raw_modules = scan.get("modules", ())
        raw_interfaces = scan.get("structural_interfaces")
        if raw_interfaces is None:
            raw_interfaces = scan.get("interfaces", ())
        for raw_definitions in (raw_modules, raw_interfaces):
            if isinstance(raw_definitions, Sequence) and not isinstance(
                raw_definitions, (str, bytes)
            ):
                definition_count += sum(
                    str(name) == module_name for name in raw_definitions
                )
        module_gap_map = scan.get("hierarchy_module_gap_map")
        if not isinstance(module_gap_map, Mapping):
            continue
        raw_codes = module_gap_map.get(module_name)
        if isinstance(raw_codes, Sequence) and not isinstance(
            raw_codes, (str, bytes)
        ):
            gap_codes.update(str(code) for code in raw_codes)
    if definition_count > 1:
        gap_codes.add("hierarchy_definition_ambiguous")
    return tuple(sorted(gap_codes))


def _child_instance_evidence(
    hierarchy_result: Mapping[str, Any],
    *,
    parent_module: str | None,
    instance_name: str,
) -> tuple[bool, tuple[str, ...]]:
    if not parent_module:
        return False, ()
    scans = hierarchy_result.get("_scan_results")
    if not isinstance(scans, Sequence) or isinstance(scans, (str, bytes)):
        return False, ()
    parent_definition_count = 0
    child_proved = False
    gap_codes = set(_module_gap_codes(hierarchy_result, parent_module))
    for scan in scans:
        check_cancelled()
        if not isinstance(scan, Mapping):
            continue
        by_module = scan.get("module_instance_map")
        if not isinstance(by_module, Mapping) or parent_module not in by_module:
            continue
        items = by_module.get(parent_module)
        if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
            continue
        parent_definition_count += 1
        for item in items:
            if (
                not isinstance(item, Mapping)
                or str(item.get("instance_name") or "") != instance_name
            ):
                continue
            raw_codes = item.get("hierarchy_gap_codes")
            if isinstance(raw_codes, Sequence) and not isinstance(
                raw_codes, (str, bytes)
            ):
                gap_codes.update(str(code) for code in raw_codes)
            child_proved = child_proved or str(
                item.get("hierarchy_edge_status") or "complete"
            ) in {"complete", "positive_local"}
    if parent_definition_count > 1:
        gap_codes.add("hierarchy_definition_ambiguous")
    return (
        parent_definition_count == 1
        and child_proved
        and "hierarchy_definition_ambiguous" not in gap_codes,
        tuple(sorted(gap_codes)),
    )


class LexicalHierarchyProvider:
    """O(depth) view over the compatibility ``component_tree`` hierarchy."""

    kind = HierarchyProviderKind.COMPILE_LEXICAL

    def __init__(
        self,
        hierarchy_result: Mapping[str, Any],
        *,
        design_identity: str | None = None,
    ) -> None:
        self._hierarchy_result = hierarchy_result
        raw_tree = hierarchy_result.get("component_tree")
        self._component_tree = raw_tree if isinstance(raw_tree, Mapping) else None
        self.design_identity = str(
            design_identity
            or hierarchy_result.get("_hierarchy_snapshot_sha256")
            or "unversioned_compile_hierarchy"
        )

    def _binding(
        self,
        *,
        path: str,
        name: str,
        definition_name: str,
        parent_path: str | None,
        node: Mapping[str, Any] | None,
        gap_codes: Sequence[str] = (),
    ) -> HierarchyInstanceBinding:
        definition_id = definition_name
        source_line = node.get("source_line") if node is not None else None
        if not isinstance(source_line, int) or isinstance(source_line, bool):
            source_line = None
        source_file = node.get("source_file") if node is not None else None
        source_origin = node.get("source_info_origin") if node is not None else None
        return HierarchyInstanceBinding(
            provider_kind=self.kind,
            instance_id=_instance_id(
                provider_kind=self.kind,
                design_identity=self.design_identity,
                path=path,
                definition_id=definition_id,
            ),
            path=path,
            name=name,
            definition_id=definition_id,
            definition_name=definition_name,
            parent_path=parent_path,
            source_file=str(source_file) if source_file else None,
            source_line=source_line,
            source_origin=str(source_origin) if source_origin else None,
            gap_codes=tuple(gap_codes),
        )

    def _root_binding(self, top: str) -> HierarchyInstanceBinding:
        gaps = _module_gap_codes(self._hierarchy_result, top)
        return self._binding(
            path=top,
            name=top,
            definition_name=top,
            parent_path=None,
            node=None,
            gap_codes=gaps,
        )

    def _node_at(
        self, *, top: str, instance_path: str
    ) -> tuple[Mapping[str, Any] | None, Mapping[str, Any] | None]:
        """Return ``(node, children)`` without enumerating siblings."""

        if self._component_tree is None:
            return None, None
        children = self._component_tree.get(top)
        if not isinstance(children, Mapping):
            return None, None
        if instance_path == top:
            return None, children
        parts = instance_path.split(".")
        if not parts or parts[0] != top:
            return None, None
        node: Mapping[str, Any] | None = None
        for name in parts[1:]:
            check_cancelled()
            candidate = children.get(name)
            if not isinstance(candidate, Mapping):
                return None, None
            node = candidate
            nested = node.get("children")
            children = nested if isinstance(nested, Mapping) else {}
        return node, children

    def lookup_instance(
        self, *, top: str, instance_path: str
    ) -> HierarchyInstanceBinding | None:
        if self._component_tree is None:
            return None
        if instance_path == top:
            return self._root_binding(top)
        node, _ = self._node_at(top=top, instance_path=instance_path)
        if node is None:
            return None
        definition_name = str(node.get("class") or node.get("module") or "")
        if not definition_name:
            return None
        raw_gaps = node.get("hierarchy_gap_codes")
        gaps = (
            tuple(str(code) for code in raw_gaps)
            if isinstance(raw_gaps, Sequence)
            and not isinstance(raw_gaps, (str, bytes))
            else ()
        )
        parent_path, _, name = instance_path.rpartition(".")
        return self._binding(
            path=instance_path,
            name=name,
            definition_name=definition_name,
            parent_path=parent_path,
            node=node,
            gap_codes=gaps,
        )

    def resolve_scope(
        self, *, top: str, signal_path: str
    ) -> HierarchyAncestorResolution | None:
        if self._component_tree is None:
            return None
        children = self._component_tree.get(top)
        if not isinstance(children, Mapping):
            children = None
        parts = signal_path.split(".")
        if len(parts) < 2 or parts[0] != top:
            return None
        ancestors = [top]
        bindings = [self._root_binding(top)]
        hierarchy_gap_codes = set(bindings[0].gap_codes)
        parent_module: str | None = top
        index = 1
        while index < len(parts) - 1 and isinstance(children, Mapping):
            check_cancelled()
            node = children.get(parts[index])
            if not isinstance(node, Mapping):
                break
            raw_gap_codes = node.get("hierarchy_gap_codes")
            node_gaps = (
                tuple(str(code) for code in raw_gap_codes)
                if isinstance(raw_gap_codes, Sequence)
                and not isinstance(raw_gap_codes, (str, bytes))
                else ()
            )
            hierarchy_gap_codes.update(node_gaps)
            path = ".".join(parts[: index + 1])
            raw_module = node.get("class") or node.get("module")
            parent_module = str(raw_module) if raw_module else None
            if parent_module is None:
                break
            ancestors.append(path)
            bindings.append(
                self._binding(
                    path=path,
                    name=parts[index],
                    definition_name=parent_module,
                    parent_path=ancestors[-2],
                    node=node,
                    gap_codes=node_gaps,
                )
            )
            nested = node.get("children")
            children = nested if isinstance(nested, Mapping) else None
            index += 1

        candidate_instance_path = None
        missing_instance_proved = False
        stop_depth = None
        if index < len(parts) - 1:
            candidate_instance_path = ".".join(parts[: index + 1])
            stop_depth = index
            missing_instance_proved, candidate_gaps = _child_instance_evidence(
                self._hierarchy_result,
                parent_module=parent_module,
                instance_name=parts[index],
            )
            hierarchy_gap_codes.update(candidate_gaps)
        return HierarchyAncestorResolution(
            ancestors=tuple(ancestors),
            remaining_path_segment_count=len(parts) - index,
            stop_depth=stop_depth,
            candidate_instance_path=candidate_instance_path,
            missing_instance_proved=missing_instance_proved,
            gap_codes=tuple(sorted(hierarchy_gap_codes)),
            provider_kind=self.kind,
            bindings=tuple(bindings),
        )

    def direct_children(
        self,
        *,
        top: str,
        instance_path: str,
        max_children: int | None,
    ) -> HierarchyChildrenResult | None:
        if max_children is not None and (
            not isinstance(max_children, int)
            or isinstance(max_children, bool)
            or max_children < 1
        ):
            raise ValueError("max_children must be a positive integer or None")
        _, children = self._node_at(top=top, instance_path=instance_path)
        if children is None:
            return None
        names = sorted(
            name
            for name, node in children.items()
            if isinstance(name, str) and name and isinstance(node, Mapping)
        )
        available_count = len(names)
        selected = names if max_children is None else names[:max_children]
        bindings = tuple(
            binding
            for name in selected
            if (
                binding := self.lookup_instance(
                    top=top, instance_path=f"{instance_path}.{name}"
                )
            )
            is not None
        )
        return HierarchyChildrenResult(
            parent_path=instance_path,
            children=bindings,
            truncated=available_count > len(bindings),
            available_count=available_count,
        )


class ConnectivityIRHierarchyProvider:
    """Semantic hierarchy view over an existing compact Connectivity IR."""

    kind = HierarchyProviderKind.SLANG_IR

    def __init__(
        self,
        ir: ConnectivityIR,
        *,
        design_identity: str | None = None,
        instance_index: Mapping[str, InstanceDecl] | None = None,
        definition_index: Mapping[str, DefinitionTemplate] | None = None,
    ) -> None:
        self._ir = ir
        self._instances = (
            instance_index if instance_index is not None else ir.instance_index
        )
        self._definitions = (
            definition_index
            if definition_index is not None
            else ir.definition_index
        )
        self.design_identity = str(
            design_identity
            or f"{ir.frontend_name}:{ir.frontend_version}:"
            + ",".join(ir.top_instances)
        )
        self._children_by_parent: dict[str, tuple[str, ...]] | None = None

    def _gap_codes(self, path: str) -> tuple[str, ...]:
        return tuple(
            sorted(
                {
                    _semantic_gap_label(gap.code)
                    for gap in self._ir.coverage.gaps
                    if gap.affects(path)
                }
            )
        )

    def _binding(self, instance: InstanceDecl) -> HierarchyInstanceBinding | None:
        definition = self._definitions.get(instance.definition_id)
        if definition is None:
            return None
        return HierarchyInstanceBinding(
            provider_kind=self.kind,
            instance_id=_instance_id(
                provider_kind=self.kind,
                design_identity=self.design_identity,
                path=instance.path,
                definition_id=instance.definition_id,
            ),
            path=instance.path,
            name=instance.name,
            definition_id=instance.definition_id,
            definition_name=definition.name,
            parent_path=instance.parent_path,
            source_file=instance.location.file,
            source_line=instance.location.line,
            source_origin="slang",
            generate_scope=instance.generate_scope,
            parameterization=instance.parameterization,
            gap_codes=self._gap_codes(instance.path),
        )

    def lookup_instance(
        self, *, top: str, instance_path: str
    ) -> HierarchyInstanceBinding | None:
        if top not in self._ir.top_instances:
            return None
        instance = self._instances.get(instance_path)
        if instance is None:
            return None
        binding = self._binding(instance)
        if binding is None:
            return None
        root = binding.path.split(".", 1)[0]
        return binding if root == top else None

    def _longest_instance_prefix(self, signal_path: str) -> str | None:
        boundary = signal_path.rfind(".")
        while boundary > 0:
            check_cancelled()
            candidate = signal_path[:boundary]
            if boundary < len(signal_path) - 1 and candidate in self._instances:
                return candidate
            boundary = signal_path.rfind(".", 0, boundary)
        return None

    def _ancestor_bindings(
        self, *, top: str, leaf_path: str
    ) -> tuple[HierarchyInstanceBinding, ...] | None:
        reverse: list[HierarchyInstanceBinding] = []
        seen: set[str] = set()
        current: str | None = leaf_path
        while current is not None:
            check_cancelled()
            if current in seen:
                return None
            seen.add(current)
            instance = self._instances.get(current)
            if instance is None:
                return None
            binding = self._binding(instance)
            if binding is None:
                return None
            reverse.append(binding)
            current = instance.parent_path
        bindings = tuple(reversed(reverse))
        if not bindings or bindings[0].path != top:
            return None
        return bindings

    def resolve_scope(
        self, *, top: str, signal_path: str
    ) -> HierarchyAncestorResolution | None:
        if top not in self._ir.top_instances or not signal_path.startswith(f"{top}."):
            return None
        leaf_path = self._longest_instance_prefix(signal_path)
        if leaf_path is None:
            return None
        bindings = self._ancestor_bindings(top=top, leaf_path=leaf_path)
        if bindings is None:
            return None
        suffix = signal_path[len(leaf_path) + 1 :]
        if not suffix:
            return None
        suffix_parts = suffix.split(".")
        candidate_instance_path = None
        stop_depth = None
        if len(suffix_parts) > 1:
            candidate_instance_path = f"{leaf_path}.{suffix_parts[0]}"
            stop_depth = len(leaf_path.split("."))
        gaps = {
            code for binding in bindings for code in binding.gap_codes
        }
        return HierarchyAncestorResolution(
            ancestors=tuple(binding.path for binding in bindings),
            remaining_path_segment_count=len(suffix_parts),
            stop_depth=stop_depth,
            candidate_instance_path=candidate_instance_path,
            missing_instance_proved=False,
            gap_codes=tuple(sorted(gaps)),
            provider_kind=self.kind,
            bindings=bindings,
        )

    def _ensure_children_index(self) -> Mapping[str, tuple[str, ...]]:
        if self._children_by_parent is None:
            children: dict[str, list[str]] = defaultdict(list)
            for instance in self._instances.values():
                check_cancelled()
                if instance.parent_path is not None:
                    children[instance.parent_path].append(instance.path)
            self._children_by_parent = {
                parent: tuple(sorted(paths)) for parent, paths in children.items()
            }
        return self._children_by_parent

    def direct_children(
        self,
        *,
        top: str,
        instance_path: str,
        max_children: int | None,
    ) -> HierarchyChildrenResult | None:
        if max_children is not None and (
            not isinstance(max_children, int)
            or isinstance(max_children, bool)
            or max_children < 1
        ):
            raise ValueError("max_children must be a positive integer or None")
        if self.lookup_instance(top=top, instance_path=instance_path) is None:
            return None
        child_paths = self._ensure_children_index().get(instance_path, ())
        selected = child_paths if max_children is None else child_paths[:max_children]
        bindings = tuple(
            binding
            for path in selected
            if (binding := self.lookup_instance(top=top, instance_path=path))
            is not None
        )
        return HierarchyChildrenResult(
            parent_path=instance_path,
            children=bindings,
            truncated=len(child_paths) > len(bindings),
            available_count=len(child_paths),
        )


def lexical_hierarchy_provider(
    hierarchy_result: Mapping[str, Any],
) -> LexicalHierarchyProvider:
    """Construct the default provider without importing an optional frontend."""

    return LexicalHierarchyProvider(hierarchy_result)


__all__ = [
    "ConnectivityIRHierarchyProvider",
    "HIERARCHY_INFORMATIONAL_GAP_CODES",
    "HierarchyAncestorResolution",
    "HierarchyChildrenResult",
    "HierarchyInstanceBinding",
    "HierarchyProvider",
    "HierarchyProviderKind",
    "LexicalHierarchyProvider",
    "lexical_hierarchy_provider",
]
