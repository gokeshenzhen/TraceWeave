"""Compile ownership for the shared hierarchy/source service.

This index copies only already captured metadata. It does not open files,
resolve live symlinks, infer HDL declarations, load NPI or run a frontend.
Compiler file-type hints are deliberately separate from semantic file types.
The caller pins and validates the DesignContext before and after using it.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
import time

from .cancellation import check_cancelled
from .design_context import DesignContext
from .hierarchy_source_ir import HierarchyBudgetExceeded, semantic_fact_id


def mark_unbound_source_locations(result: dict, context: DesignContext) -> None:
    """Retain raw provider locations, but flag paths outside captured inputs.

    Native File can be relative to the SDK's current directory even when
    DefFile identifies a captured input. Only exact captured spellings/aliases
    establish a binding; existence or a unique basename does not. This check
    neither opens paths nor upgrades the separate KDB/source association claim.
    Work is bounded by captured metadata plus the already bounded result rows.
    """
    classes = "roots" in result or result.get("section") == "class_hierarchy_full"
    roles = ((("source_file", "npi_class_location_unbound"),) if classes else
             (("source_file", "npi_instance_location_unbound"),
              ("definition_file", "npi_definition_location_unbound")))
    # Only inspect validated, already bounded output rows. Do not enumerate
    # native children to bind locations, including undisplayed descendants.
    rows = list(result.get("hits", []))
    if result.get("section") in {"component_tree_full", "class_hierarchy_full"}:
        data = result.get("data")
        pending = list(data or []) if classes else [data] if data is not None else []
    elif classes:
        pending = list(result["roots"])
    else:
        pending = [result["node"]] if result.get("node") is not None else []
    while pending:
        check_cancelled()
        row = pending.pop()
        rows.append(row)
        pending.extend(row["children"])
    wanted = {row[key] for row in rows for key, _ in roles if row[key] is not None}
    resolved = {path: path for path in wanted}
    for spelling, canonical in context.aliases:
        check_cancelled()
        if spelling in wanted:
            resolved[spelling] = canonical
    targets, captured = set(resolved.values()), set()
    for record in context.snapshot.records:
        check_cancelled()
        if record.path in targets:
            captured.add(record.path)
    for row in rows:
        check_cancelled()
        gaps = set(row["gaps"])
        for key, gap in roles:
            if row[key] is not None and resolved[row[key]] not in captured:
                gaps.add(gap)
        row["gaps"] = sorted(gaps)


def mark_unbound_instance_locations(result: dict, context: DesignContext) -> None:
    """Compatibility name for callers holding instance-only query results."""
    mark_unbound_source_locations(result, context)


@dataclass(frozen=True)
class CompileSourceFile:
    file_id: str
    path: str
    aliases: tuple[str, ...]
    categories: tuple[str, ...]
    compiler_types: tuple[str, ...]
    roles: tuple[str, ...]
    includes: tuple[str, ...]
    is_compilation_unit: bool
    is_reported_include: bool
    is_dependency_include: bool
    source_status: str

    def row(self) -> dict:
        return {
            "file_id": self.file_id, "path": self.path, "aliases": list(self.aliases),
            "ownership_origin": "compile_metadata", "compile_categories": list(self.categories),
            "compiler_file_types": list(self.compiler_types), "compilation_roles": list(self.roles),
            "includes": list(self.includes), "include_origin": "compile_log",
            "is_compilation_unit": self.is_compilation_unit,
            "is_reported_include": self.is_reported_include,
            "is_dependency_include": self.is_dependency_include, "source_status": self.source_status,
        }


@dataclass(frozen=True)
class CompileSourceCatalog:
    context_identity: str
    files: tuple[CompileSourceFile, ...]
    gaps: tuple[str, ...]
    metadata_bytes: int
    preparation_ms: float
    ownership_basis: str = "captured_source"

    def find_file(self, path: str, *, checkpoint=check_cancelled) -> CompileSourceFile | None:
        """Only an observed spelling is an alias; never query current realpath."""
        if not isinstance(path, str) or not path:
            raise ValueError("a nonempty source path is required")
        for item in self.files:
            checkpoint()
            if path == item.path or path in item.aliases:
                return item
        return None

    def suggest_files(self, path: str, *, limit: int = 50, checkpoint=check_cancelled) -> list[str]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
            raise ValueError("invalid suggestion limit")
        name, suggestions = os.path.basename(path), []
        for item in self.files:
            checkpoint()
            if any(os.path.basename(spelling) == name for spelling in (item.path, *item.aliases)):
                suggestions.append(item.path)
                if len(suggestions) == limit:
                    break
        return suggestions

    def select_files(self, *, basename=None, name_contains=None, path_contains=None,
                     checkpoint=check_cancelled):
        """Yield ownership matches. Semantic filters require a semantic provider.

        Matching uses the canonical compile member and its captured spellings,
        so a recorded alias stays usable without collapsing different versions
        that happen to have the same basename. This is not a public total count.
        """
        filters = (basename, name_contains, path_contains)
        if all(value is None for value in filters):
            raise ValueError("a metadata file filter is required")
        if any(value is not None and (not isinstance(value, str) or not value) for value in filters):
            raise ValueError("metadata file filters must be nonempty strings")
        for item in self.files:
            checkpoint()
            spellings = (item.path, *item.aliases)
            if basename is not None and not any(os.path.basename(p) == basename for p in spellings):
                continue
            if name_contains is not None and not any(name_contains in os.path.basename(p) for p in spellings):
                continue
            if path_contains is not None and not any(path_contains in p for p in spellings):
                continue
            yield item


def build_compile_source_catalog(context: DesignContext, *, max_files=32768,
                                 max_bytes=16 * 1024 * 1024,
                                 checkpoint=check_cancelled) -> CompileSourceCatalog:
    """Build a bounded ownership index without enumerating HDL definitions.

    Includes discovered during preparation join the observed source set, even
    if a simulator omitted them from its user-file list. Log/config/filelist
    snapshot records alone never become HDL files. Include edges remain the
    compiler's reported edges: a dependency lookup without its parent is not
    invented into an include tree. Gaps describe inventory evidence, not an
    empty or failed semantic query.
    """
    if not isinstance(context, DesignContext):
        raise TypeError("a prepared DesignContext is required")
    return _build_catalog(context.compile_result(), context.identity_sha256,
        context.aliases, {r.path for r in context.snapshot.records},
        context.unavailable, context.include_resolutions, context.gaps,
        context.include_resolution_complete, max_files=max_files,
        max_bytes=max_bytes, checkpoint=checkpoint)


def build_native_source_catalog(inputs, *, max_files=32768, max_bytes=16 * 1024 * 1024,
                                checkpoint=check_cancelled):
    if inputs.compile_result_json is None:
        raise ValueError("compile_log is required for compile file ownership")
    return _build_catalog(inputs.compile_result(), inputs.compile_metadata_identity_sha256,
        (), set(), (), (), inputs.gaps, False, max_files=max_files,
        max_bytes=max_bytes, checkpoint=checkpoint, ownership_basis="compile_metadata")


def observation_identity(context):
    from .native_query import NativeQueryInputs
    return ((context.compile_metadata_identity_sha256 or "unavailable") if isinstance(context, NativeQueryInputs)
            else context.identity_sha256)


def _build_catalog(result, identity, observed_aliases, available, unavailable_observations,
                   include_resolutions, context_gaps, include_complete, *, max_files, max_bytes,
                   checkpoint, ownership_basis="captured_source"):
    for value, ceiling in ((max_files, 100000), (max_bytes, 64 * 1024 * 1024)):
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= ceiling:
            raise ValueError("invalid metadata catalog limit")
    checkpoint()
    started = time.monotonic()
    aliases = dict(observed_aliases)
    cwd = result.get("compile_cwd")
    records, gaps = {}, set()
    retained_bytes = 0

    def charge(value):
        nonlocal retained_bytes
        checkpoint()
        size = 128 + 4 * len(json.dumps(value, ensure_ascii=True))
        if retained_bytes + size > max_bytes:
            raise HierarchyBudgetExceeded("metadata_byte_limit")
        retained_bytes += size

    def canonical(spelling):
        if not isinstance(spelling, str) or not spelling:
            gaps.add("compile_source_path_unresolved")
            return None
        if spelling in aliases:
            return aliases[spelling]
        if not os.path.isabs(spelling):
            if not isinstance(cwd, str) or not os.path.isabs(cwd):
                gaps.add("compile_source_path_unresolved")
                return None
            spelling = os.path.join(cwd, spelling)
        # Do not normalize away '..' through an unobserved symlink. Only an
        # exact captured spelling can establish a canonical alias binding.
        if spelling in aliases:
            return aliases[spelling]
        if any(part in {".", ".."} for part in spelling.split(os.sep)):
            gaps.add("compile_source_path_unresolved")
            return None
        return spelling

    def add(spelling, *, category=None, compiler_type=None, role=None, unit=False,
            included=False, dependency=False):
        checkpoint()
        path = canonical(spelling)
        if path is None:
            return None
        if path not in records:
            if len(records) >= max_files:
                raise HierarchyBudgetExceeded("metadata_file_limit")
            charge(("file", path, identity))
            records[path] = {"aliases": set(), "categories": set(), "types": set(),
                             "roles": set(), "includes": [], "unit": False,
                             "included": False, "dependency": False}
        record = records[path]
        for key, value in (("aliases", spelling), ("categories", category),
                           ("types", compiler_type), ("roles", role)):
            if isinstance(value, str) and value and value not in record[key]:
                charge(value)
                record[key].add(value)
        record["unit"] |= unit
        record["included"] |= included
        record["dependency"] |= dependency
        return path

    for item in result.get("files", {}).get("user", []):
        add(item.get("path"), category=item.get("category"), compiler_type=item.get("type"))
    evidence = result.get("compile_evidence") or {}
    for item in evidence.get("ordered_compilation_units", []):
        add(item.get("path"), compiler_type=item.get("type"), role=item.get("role"), unit=True)

    def include_edge(parent, child):
        parent = add(parent)
        child = add(child, included=True)
        if parent is not None and child is not None and child not in records[parent]["includes"]:
            charge(child)
            records[parent]["includes"].append(child)

    for parent, children in (result.get("include_tree") or {}).items():
        for child in children:
            include_edge(parent, child)
    for item in evidence.get("ordered_includes", []):
        include_edge(item.get("parent"), item.get("path"))
    for item in include_resolutions:
        if item.selected is not None:
            add(item.selected_spelling or item.selected, dependency=True)
    for spelling, path in observed_aliases:
        checkpoint()
        if path in records and spelling not in records[path]["aliases"]:
            charge(spelling)
            records[path]["aliases"].add(spelling)

    unavailable = {canonical(path) for path, _ in unavailable_observations}
    files = []
    for path, item in records.items():
        checkpoint()
        files.append(CompileSourceFile(
            semantic_fact_id("compile_metadata", identity, "file", path), path,
            tuple(sorted(item["aliases"])), tuple(sorted(item["categories"])),
            tuple(sorted(item["types"])), tuple(sorted(item["roles"])), tuple(item["includes"]),
            item["unit"], item["included"], item["dependency"],
            "captured" if path in available else "unavailable" if path in unavailable else "not_captured",
        ))
    if "compile_inputs_incomplete" in context_gaps:
        gaps.add("compile_inputs_incomplete")
    if not include_complete:
        gaps.add("include_inventory_incomplete")
    checkpoint()
    return CompileSourceCatalog(identity, tuple(files), tuple(sorted(gaps)),
                                retained_bytes, (time.monotonic() - started) * 1000, ownership_basis)


def export_compile_metadata(context: DesignContext, section: str, *, checkpoint=check_cancelled) -> dict:
    """Return an exact copy of a recorded section, not an inferred full tree."""
    if section not in {"compile_result", "include_tree", "filelist_tree"}:
        raise ValueError("section requires a semantic hierarchy/source provider")
    checkpoint()
    result = context.compile_result()
    data = result if section == "compile_result" else result.get(section, {})
    checkpoint()
    from .native_query import NativeQueryInputs
    gaps = sorted(set(context.gaps) | ({"include_inventory_incomplete"} if isinstance(context, NativeQueryInputs) else set()))
    return {
        "section": section, "data": data, "facts_origin": "compile_metadata",
        "artifact_identity": observation_identity(context), "semantic_status": "not_run",
        "coverage_domain": "reported_compile_record", "record_export_complete": True,
        "coverage_status": "partial" if gaps else "complete",
        "coverage_gaps": gaps,
    }
