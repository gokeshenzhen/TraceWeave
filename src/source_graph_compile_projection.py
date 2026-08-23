"""Conservative compile-input closure for bounded Source Graph artifacts.

This module is a planner, not a SystemVerilog frontend.  It consumes only the
definition/dependency facts already captured while ``build_tb_hierarchy`` read
the compile session, and leaves parsing and elaboration to Slang.  A projected
build is always coverage-limited: omitted siblings can become unknown modules,
so only IR-proved positive facts are useful and negative conclusions remain
inconclusive.
"""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .cancellation import check_cancelled
from .source_graph_contract import (
    CompileInputManifest,
    CompileProjectionMode,
    SOURCE_GRAPH_COMPILE_PROJECTION_GAP,
    SourceGraphCompileProjection,
)


COMPILE_PROJECTION_GAP = SOURCE_GRAPH_COMPILE_PROJECTION_GAP
_MIN_FULL_INPUT_COUNT = 64
_MIN_EXCLUDED_INPUT_COUNT = 32
_MAX_PROJECTED_INPUT_COUNT = 512
_MAX_PROJECTED_RATIO = 0.5
_FRONTEND_HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})
_VHDL_SUFFIXES = frozenset({".vhd", ".vhdl"})


@dataclass(frozen=True)
class CompileProjectionDecision:
    projection: SourceGraphCompileProjection | None
    mode: str
    input_count: int
    excluded_input_count: int
    seed_symbol_count: int
    dependency_symbol_count: int
    fallback_reason: str | None
    gap_codes: tuple[str, ...] = ()

    @property
    def applied(self) -> bool:
        return self.projection is not None


def _canonical(value: str) -> str:
    return str(Path(value).resolve(strict=False))


def _full_manifest_decision(
    manifest: CompileInputManifest,
    reason: str,
    *,
    seed_symbol_count: int = 0,
    dependency_symbol_count: int = 0,
) -> CompileProjectionDecision:
    return CompileProjectionDecision(
        projection=None,
        mode="full_manifest",
        input_count=len(manifest.ordered_inputs),
        excluded_input_count=0,
        seed_symbol_count=seed_symbol_count,
        dependency_symbol_count=dependency_symbol_count,
        fallback_reason=reason,
    )


def _scan_records(
    hierarchy_result: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...] | None:
    raw = hierarchy_result.get("_scan_results")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return None
    records = tuple(item for item in raw if isinstance(item, Mapping))
    if len(records) != len(raw) or any(
        "package_qualifiers" not in item for item in records
    ):
        return None
    return records


def _seed_symbols_for_scope(
    *,
    hierarchy_result: Mapping[str, Any],
    top: str,
    instance_paths: Sequence[str],
    compile_tops: Sequence[str],
) -> set[str] | None:
    component_tree = hierarchy_result.get("component_tree")
    if not isinstance(component_tree, Mapping):
        return None
    root = component_tree.get(top)
    if not isinstance(root, Mapping):
        return None

    seeds = {top, *compile_tops}
    for path in instance_paths:
        check_cancelled()
        parts = str(path).split(".")
        if not parts or parts[0] != top:
            return None
        children: Mapping[str, Any] = root
        for part in parts[1:]:
            node = children.get(part)
            if not isinstance(node, Mapping) or not node.get("class"):
                return None
            seeds.add(str(node["class"]))
            raw_children = node.get("children", {})
            if not isinstance(raw_children, Mapping):
                return None
            children = raw_children
    return seeds


def plan_source_graph_compile_projection(
    *,
    manifest: CompileInputManifest,
    hierarchy_result: Mapping[str, Any],
    top: str,
    instance_paths: Sequence[str],
) -> CompileProjectionDecision:
    """Select an ordered, hierarchy-proved dependency closure when worthwhile.

    Any missing proof returns the unchanged full manifest.  Applying a closure
    always adds :data:`COMPILE_PROJECTION_GAP`; the worker may elaborate known
    ancestor instances beside unknown omitted siblings, but coverage can never
    become a complete negative from that artifact.
    """

    full_inputs = manifest.ordered_inputs
    if len(full_inputs) < _MIN_FULL_INPUT_COUNT:
        return _full_manifest_decision(manifest, "below_input_threshold")
    if not manifest.complete:
        return _full_manifest_decision(manifest, "manifest_incomplete")
    if len(set(full_inputs)) != len(full_inputs):
        return _full_manifest_decision(manifest, "duplicate_compile_inputs")
    if any(Path(path).suffix.lower() in _VHDL_SUFFIXES for path in full_inputs):
        return _full_manifest_decision(manifest, "mixed_language_manifest")

    records = _scan_records(hierarchy_result)
    if records is None:
        return _full_manifest_decision(manifest, "hierarchy_scan_index_unavailable")
    seeds = _seed_symbols_for_scope(
        hierarchy_result=hierarchy_result,
        top=top,
        instance_paths=instance_paths,
        compile_tops=manifest.ordered_tops,
    )
    if seeds is None:
        return _full_manifest_decision(manifest, "hierarchy_projection_unresolved")

    canonical_inputs_list: list[str] = []
    for path in full_inputs:
        check_cancelled()
        canonical_inputs_list.append(_canonical(path))
    canonical_inputs = tuple(canonical_inputs_list)
    if len(set(canonical_inputs)) != len(canonical_inputs):
        return _full_manifest_decision(
            manifest, "duplicate_canonical_compile_inputs"
        )
    input_set = set(canonical_inputs)
    order_index = {path: index for index, path in enumerate(canonical_inputs)}
    scan_by_path: dict[str, Mapping[str, Any]] = {}
    design_providers: dict[str, list[str]] = defaultdict(list)
    package_providers: dict[str, list[str]] = defaultdict(list)
    macro_mutation_providers: dict[str, list[str]] = defaultdict(list)
    macro_reset_providers: list[str] = []
    for record in records:
        check_cancelled()
        raw_path = record.get("path")
        if not raw_path:
            continue
        path = _canonical(str(raw_path))
        scan_by_path[path] = record
        for symbol in record.get("structural_modules", record.get("modules", ())):
            if path not in design_providers[str(symbol)]:
                design_providers[str(symbol)].append(path)
        for symbol in record.get(
            "structural_interfaces", record.get("interfaces", ())
        ):
            if path not in design_providers[str(symbol)]:
                design_providers[str(symbol)].append(path)
        for package in record.get("packages", ()):
            if path not in package_providers[str(package)]:
                package_providers[str(package)].append(path)
        for macro in {
            *(str(item) for item in record.get("macro_definitions", ())),
            *(str(item) for item in record.get("macro_undefinitions", ())),
        }:
            if path not in macro_mutation_providers[macro]:
                macro_mutation_providers[macro].append(path)
        if record.get("has_macro_undefineall"):
            macro_reset_providers.append(path)

    basename_providers: dict[str, list[str]] = defaultdict(list)
    for path in canonical_inputs:
        basename_providers[Path(path).stem].append(path)

    selected: set[str] = set()
    for symbol in seeds:
        providers = [
            path for path in design_providers.get(symbol, ()) if path in input_set
        ]
        if not providers:
            return _full_manifest_decision(
                manifest,
                "seed_definition_unresolved",
                seed_symbol_count=len(seeds),
            )
        if len(providers) != 1:
            return _full_manifest_decision(
                manifest,
                "seed_definition_ambiguous",
                seed_symbol_count=len(seeds),
            )
        selected.add(providers[0])

    queue = deque(sorted(selected, key=order_index.__getitem__))
    visited: set[str] = set()
    dependency_symbols: set[str] = set()
    while queue:
        check_cancelled()
        path = queue.popleft()
        if path in visited:
            continue
        visited.add(path)
        record = scan_by_path.get(path)
        if record is None:
            # Tool-library inputs such as uvm_pkg.sv are intentionally absent
            # from the project hierarchy scan. Their own include directories
            # and command-line defines remain in the full ordered options.
            continue

        imported_packages = {
            str(name) for name in record.get("package_imports", ())
        }
        qualified_packages = {
            str(name) for name in record.get("package_qualifiers", ())
        }
        for package in sorted(imported_packages | qualified_packages):
            providers = [
                provider
                for provider in package_providers.get(package, ())
                if provider in input_set
            ]
            if not providers and package in imported_packages:
                # A simulator-added tool library such as ``uvm_pkg.sv`` is
                # absent from the project hierarchy scan.  Basename recovery
                # is safe only for such unscanned replay inputs; a scanned
                # project file that failed to prove the package definition is
                # not silently promoted to a provider.
                providers = [
                    provider
                    for provider in basename_providers.get(package, ())
                    if provider not in scan_by_path
                ]
            if not providers:
                if package in imported_packages:
                    return _full_manifest_decision(
                        manifest,
                        "package_dependency_unresolved",
                        seed_symbol_count=len(seeds),
                        dependency_symbol_count=len(dependency_symbols),
                    )
                # A qualified name can be a class or nested type rather than a
                # package. It becomes a dependency only on an exact package
                # provider match.
                continue
            if len(providers) != 1:
                return _full_manifest_decision(
                    manifest,
                    "package_dependency_ambiguous",
                    seed_symbol_count=len(seeds),
                    dependency_symbol_count=len(dependency_symbols),
                )
            dependency_symbols.add(f"package:{package}")
            for provider in providers:
                if provider not in selected:
                    selected.add(provider)
                    queue.append(provider)

        current_index = order_index[path]
        macro_dependencies = {
            *(str(name) for name in record.get("macro_uses", ())),
            *(str(name) for name in record.get("conditional_macros", ())),
        }
        for macro in sorted(macro_dependencies):
            providers = [
                provider
                for provider in macro_mutation_providers.get(macro, ())
                if provider in input_set and order_index[provider] < current_index
            ]
            providers.extend(
                provider
                for provider in macro_reset_providers
                if provider in input_set
                and order_index[provider] < current_index
                and provider not in providers
            )
            if not providers:
                continue
            dependency_symbols.add(f"macro:{macro}")
            # Preserve every compile-order mutation source (define, undef, and
            # undefineall) rather than guessing the state visible to the use.
            for provider in providers:
                if provider not in selected:
                    selected.add(provider)
                    queue.append(provider)

        if len(selected) > _MAX_PROJECTED_INPUT_COUNT:
            return _full_manifest_decision(
                manifest,
                "projection_input_budget_exceeded",
                seed_symbol_count=len(seeds),
                dependency_symbol_count=len(dependency_symbols),
            )

    selected_inputs = tuple(
        original
        for original, canonical in zip(full_inputs, canonical_inputs)
        if canonical in selected
        and Path(original).suffix.lower() in _FRONTEND_HDL_SUFFIXES
    )
    excluded_count = len(full_inputs) - len(selected_inputs)
    if (
        not selected_inputs
        or excluded_count < _MIN_EXCLUDED_INPUT_COUNT
        or len(selected_inputs) / len(full_inputs) > _MAX_PROJECTED_RATIO
    ):
        return _full_manifest_decision(
            manifest,
            "insufficient_projection_reduction",
            seed_symbol_count=len(seeds),
            dependency_symbol_count=len(dependency_symbols),
        )

    projection = SourceGraphCompileProjection(
        mode=CompileProjectionMode.HIERARCHY_DEPENDENCY_CLOSURE,
        ordered_inputs=selected_inputs,
        full_input_count=len(full_inputs),
        seed_symbol_count=len(seeds),
        dependency_symbol_count=len(dependency_symbols),
    )
    return CompileProjectionDecision(
        projection=projection,
        mode=projection.mode.value,
        input_count=len(selected_inputs),
        excluded_input_count=projection.excluded_input_count,
        seed_symbol_count=projection.seed_symbol_count,
        dependency_symbol_count=projection.dependency_symbol_count,
        fallback_reason=None,
        gap_codes=(COMPILE_PROJECTION_GAP,),
    )
