"""Conservative, resource-bounded hierarchy proof for one connectivity target.

The normal hierarchy builder remains the only producer of a browsing handle and
whole-testbench panorama.  This module is a narrow fallback: it proves just the
top-to-target ancestor chain, selects only the source/package/include closure
needed by that chain, and constructs an intentionally incomplete compile
context for the existing Source Graph adapter.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import time
from typing import Any, Mapping, Sequence

from .cancellation import check_cancelled
from .tb_hierarchy_builder import scan_sv_file


_HDL_UNIT_SUFFIXES = {".v", ".sv", ".vh", ".svh"}
_SOURCE_UNIT_SUFFIXES = {".v", ".sv"}
_DEFINITION_RE = re.compile(
    r"\b(module|interface|package)\s+(?:(?:automatic|static)\s+)?"
    r"([A-Za-z_][A-Za-z0-9_$]*)",
    re.IGNORECASE,
)
_MACRO_DEFINE_RE = re.compile(r"`define\s+([A-Za-z_][A-Za-z0-9_$]*)")
_INVENTORY_CANCEL_STRIDE = 256


@dataclass(frozen=True)
class BoundedBootstrapResult:
    status: str
    compile_result: dict | None
    hierarchy_result: dict | None
    receipt: dict[str, Any]


class _BootstrapBlocked(Exception):
    def __init__(self, code: str, stage: str):
        super().__init__(code)
        self.code = code
        self.stage = stage


def _deadline_check(deadline: float) -> None:
    check_cancelled()
    if time.monotonic() >= deadline:
        raise _BootstrapBlocked("bootstrap_timeout", "target_scope")


def _ordered_unit_records(compile_result: Mapping[str, Any]) -> list[dict]:
    evidence = compile_result.get("compile_evidence")
    records = evidence.get("ordered_compilation_units") if isinstance(evidence, Mapping) else None
    if isinstance(records, Sequence) and not isinstance(records, (str, bytes)):
        result = [
            deepcopy(item)
            for item in records
            if isinstance(item, Mapping)
            and isinstance(item.get("path"), str)
            and Path(str(item["path"])).suffix.lower() in _HDL_UNIT_SUFFIXES
            and str(item.get("role") or "project") == "project"
        ]
        if result:
            return result
    files = compile_result.get("files")
    user = files.get("user") if isinstance(files, Mapping) else None
    if not isinstance(user, Sequence) or isinstance(user, (str, bytes)):
        return []
    return [
        {
            "path": str(item["path"]),
            "type": str(item.get("type") or "unknown"),
            "role": "project",
        }
        for item in user
        if isinstance(item, Mapping)
        and isinstance(item.get("path"), str)
        and Path(str(item["path"])).suffix.lower() in _HDL_UNIT_SUFFIXES
    ]


def _seed_definitions(
    compile_result: Mapping[str, Any],
    allowed_paths: set[str],
) -> dict[str, dict[str, list[str]]]:
    result: dict[str, dict[str, list[str]]] = {
        "modules": {},
        "interfaces": {},
        "packages": {},
    }
    raw = compile_result.get("definitions")
    if not isinstance(raw, Mapping):
        return result
    for kind in result:
        values = raw.get(kind)
        if not isinstance(values, Mapping):
            continue
        for name, paths in values.items():
            if not isinstance(paths, Sequence) or isinstance(paths, (str, bytes)):
                continue
            selected: list[str] = []
            for path in paths:
                canonical = os.path.realpath(str(path))
                if canonical in allowed_paths and canonical not in selected:
                    selected.append(canonical)
            if selected:
                result[kind][str(name)] = selected
    return result


def _code_only(line: str, in_block_comment: bool) -> tuple[str, bool]:
    """Remove comments/strings without walking large comment bodies bytewise.

    Generated sources often contain long padding, protected, or generated
    comment lines.  Jumping between lexical delimiters keeps the conservative
    inventory proportional to useful tokens while retaining block-comment
    state across lines.
    """

    output: list[str] = []
    index = 0
    length = len(line)
    while index < length:
        if in_block_comment:
            end = line.find("*/", index)
            if end < 0:
                return "".join(output), True
            in_block_comment = False
            index = end + 2
            output.append(" ")
            continue

        line_comment = line.find("//", index)
        block_comment = line.find("/*", index)
        string_start = line.find('"', index)
        candidates = [
            position
            for position in (line_comment, block_comment, string_start)
            if position >= 0
        ]
        if not candidates:
            output.append(line[index:])
            break
        special = min(candidates)
        output.append(line[index:special])
        if special == line_comment:
            break
        if special == block_comment:
            in_block_comment = True
            index = special + 2
            output.append(" ")
            continue

        # Skip a quoted string, respecting escaped quotes.  A newline ends an
        # unterminated source string for this lightweight inventory; the real
        # frontend remains responsible for diagnostics.
        cursor = special + 1
        while cursor < length:
            quote = line.find('"', cursor)
            if quote < 0:
                cursor = length
                break
            backslashes = 0
            probe = quote - 1
            while probe >= special and line[probe] == "\\":
                backslashes += 1
                probe -= 1
            if backslashes % 2 == 0:
                cursor = quote + 1
                break
            cursor = quote + 1
        output.append(" ")
        index = cursor
    return "".join(output), in_block_comment


def _inventory_file(
    path: str,
    *,
    deadline: float,
) -> tuple[dict[str, set[str]], set[str]]:
    definitions = {"modules": set(), "interfaces": set(), "packages": set()}
    macros: set[str] = set()
    carry = ""
    in_block_comment = False
    with open(path, "r", errors="replace") as stream:
        for line_number, line in enumerate(stream, 1):
            if line_number % _INVENTORY_CANCEL_STRIDE == 0:
                _deadline_check(deadline)
            code, in_block_comment = _code_only(line, in_block_comment)
            window = carry + code
            for kind, name in _DEFINITION_RE.findall(window):
                definitions[f"{kind.lower()}s"].add(name)
            macros.update(_MACRO_DEFINE_RE.findall(window))
            carry = window[-256:]
    _deadline_check(deadline)
    return definitions, macros


def _compile_command_macro_definitions(compile_result: Mapping[str, Any]) -> set[str]:
    rendered_commands: list[str] = []
    for field in ("compile_replay_command", "compile_command"):
        value = compile_result.get(field)
        if isinstance(value, str) and value:
            rendered_commands.append(value)
    evidence = compile_result.get("compile_evidence")
    phases = evidence.get("source_phases") if isinstance(evidence, Mapping) else None
    if isinstance(phases, Sequence) and not isinstance(phases, (str, bytes)):
        for phase in phases:
            if not isinstance(phase, Mapping):
                continue
            for field in ("expanded_replay_command", "compile_replay_command", "compile_command"):
                value = phase.get(field)
                if isinstance(value, str) and value:
                    rendered_commands.append(value)
    definitions: set[str] = set()
    for command in rendered_commands:
        try:
            tokens = shlex.split(command, comments=True, posix=True)
        except ValueError:
            continue
        index = 0
        while index < len(tokens):
            token = tokens[index]
            if token.startswith("+define+"):
                for item in token[len("+define+") :].split("+"):
                    name = item.split("=", 1)[0]
                    if name:
                        definitions.add(name)
            elif token in {"-define", "-D"} and index + 1 < len(tokens):
                definitions.add(tokens[index + 1].split("=", 1)[0])
                index += 1
            elif token.startswith("-D") and len(token) > 2:
                definitions.add(token[2:].split("=", 1)[0])
            index += 1
    return definitions


def _trim_compile_result(
    compile_result: Mapping[str, Any],
    *,
    selected_source_paths: set[str],
    selected_support_paths: set[str],
    ordered_records: list[dict],
    objective_exclusions: Sequence[str],
) -> dict:
    result = deepcopy(dict(compile_result))
    files = result.get("files")
    user = files.get("user") if isinstance(files, dict) else []
    if isinstance(user, list):
        result["files"] = {
            **files,
            "user": [
                item
                for item in user
                if isinstance(item, dict)
                and os.path.realpath(str(item.get("path") or ""))
                in selected_source_paths | selected_support_paths
            ],
        }
    include_tree = result.get("include_tree")
    if isinstance(include_tree, dict):
        result["include_tree"] = {
            parent: [
                child
                for child in children
                if os.path.realpath(str(child)) in selected_support_paths
            ]
            for parent, children in include_tree.items()
            if os.path.realpath(str(parent))
            in selected_source_paths | selected_support_paths
            and isinstance(children, list)
        }
    evidence = result.get("compile_evidence")
    evidence = deepcopy(evidence) if isinstance(evidence, dict) else {}
    ordered_includes = evidence.get("ordered_includes")
    evidence["ordered_compilation_units"] = ordered_records
    evidence["ordered_includes"] = [
        item
        for item in (ordered_includes if isinstance(ordered_includes, list) else [])
        if isinstance(item, dict)
        and os.path.realpath(str(item.get("parent") or ""))
        in selected_source_paths | selected_support_paths
        and os.path.realpath(str(item.get("path") or ""))
        in selected_support_paths
    ]
    # Filelists and per-phase commands describe the full compilation.  The
    # normal command is retained for defines/include options, while the exact
    # simulator-recorded subset above becomes the bounded input truth.
    evidence["filelists"] = []
    evidence.pop("source_phases", None)
    evidence["unit_order_source"] = "bootstrap_subset"
    result["compile_evidence"] = evidence
    result["bootstrap_context"] = {
        "used": True,
        "objective_exclusions": list(objective_exclusions),
    }
    return result


def build_bounded_connectivity_context(
    *,
    compile_result: Mapping[str, Any],
    hierarchy_snapshot_sha256: str,
    signal_path: str,
    top_hint: str | None,
    config: Any,
) -> BoundedBootstrapResult:
    """Build a bounded Source Graph context or a fixed structured blocker."""

    started = time.monotonic()
    deadline = started + float(config.timeout_sec)
    inventory_files = 0
    inventory_bytes = 0
    selected_bytes = 0
    inventory_used = False
    selected_paths: set[str] = set()
    selected_source_paths: set[str] = set()
    selected_support_paths: set[str] = set()
    scan_cache: dict[str, dict] = {}
    ancestors: list[str] = []
    objective_exclusions = {
        "bootstrap_hierarchy_scoped",
        "bootstrap_compile_inputs_scoped",
    }

    def blocked(code: str, stage: str) -> BoundedBootstrapResult:
        elapsed_ms = (time.monotonic() - started) * 1000.0
        return BoundedBootstrapResult(
            status="blocked",
            compile_result=None,
            hierarchy_result=None,
            receipt={
                "used": True,
                "status": "blocked",
                "scope": "single_endpoint",
                "ancestor_chain_proved": False,
                "coverage_status": "inconclusive",
                "objective_exclusions": sorted(objective_exclusions),
                "metrics": {
                    "inventory_file_count": inventory_files,
                    "inventory_bytes": inventory_bytes,
                    "selected_file_count": len(selected_paths),
                    "selected_source_bytes": selected_bytes,
                    "ancestor_count": len(ancestors),
                    "wall_time_ms": round(elapsed_ms, 3),
                },
                "blocker": {"code": code, "stage": stage},
            },
        )

    try:
        normalized_signal = str(signal_path).strip()
        if not normalized_signal or "." not in normalized_signal:
            raise _BootstrapBlocked("bootstrap_signal_path_unscoped", "target_scope")
        top = normalized_signal.split(".", 1)[0]
        tops = [str(item) for item in compile_result.get("top_modules", []) if item]
        primary_top = str(compile_result.get("primary_top") or "")
        if primary_top and primary_top not in tops:
            tops.insert(0, primary_top)
        if top_hint and (top_hint != top or top_hint not in tops):
            raise _BootstrapBlocked("bootstrap_top_unresolved", "target_scope")
        if top not in tops:
            raise _BootstrapBlocked("bootstrap_top_unresolved", "target_scope")

        ordered_records = _ordered_unit_records(compile_result)
        if not ordered_records:
            raise _BootstrapBlocked("bootstrap_compile_inputs_unavailable", "compile_manifest")
        canonical_records = [
            {**record, "path": os.path.realpath(str(record["path"]))}
            for record in ordered_records
        ]
        allowed_paths = {str(record["path"]) for record in canonical_records}
        unique_inventory_paths = list(dict.fromkeys(str(record["path"]) for record in canonical_records))
        definitions = _seed_definitions(compile_result, allowed_paths)
        inventory_macros: dict[str, set[str]] = {}
        used_definitions: list[tuple[str, str, str]] = []

        def load_inventory() -> None:
            nonlocal inventory_files, inventory_bytes, inventory_used
            if inventory_used:
                return
            inventory_used = True
            for path in unique_inventory_paths:
                _deadline_check(deadline)
                if inventory_files >= int(config.max_inventory_files):
                    raise _BootstrapBlocked("bootstrap_inventory_file_limit_exceeded", "definition_inventory")
                try:
                    size = os.stat(path).st_size
                except OSError:
                    continue
                if inventory_bytes + size > int(config.max_inventory_bytes):
                    raise _BootstrapBlocked("bootstrap_inventory_byte_limit_exceeded", "definition_inventory")
                found, macros = _inventory_file(path, deadline=deadline)
                inventory_files += 1
                inventory_bytes += size
                for kind, names in found.items():
                    for name in names:
                        paths = definitions[kind].setdefault(name, [])
                        if path not in paths:
                            paths.append(path)
                for macro in macros:
                    inventory_macros.setdefault(macro, set()).add(path)

        def unique_definition(kind: str, name: str) -> str:
            paths = list(definitions[kind].get(name, ()))
            if not paths:
                load_inventory()
                paths = list(definitions[kind].get(name, ()))
            if not paths:
                raise _BootstrapBlocked("bootstrap_definition_unresolved", "definition_inventory")
            if len(paths) != 1:
                raise _BootstrapBlocked("bootstrap_definition_ambiguous", "definition_inventory")
            proof = (kind, name, paths[0])
            if proof not in used_definitions:
                used_definitions.append(proof)
            return paths[0]

        def unique_design_definition(name: str) -> tuple[str, str]:
            candidates = [
                (kind, path)
                for kind in ("modules", "interfaces")
                for path in definitions[kind].get(name, ())
            ]
            if not candidates:
                load_inventory()
                candidates = [
                    (kind, path)
                    for kind in ("modules", "interfaces")
                    for path in definitions[kind].get(name, ())
                ]
            if not candidates:
                raise _BootstrapBlocked(
                    "bootstrap_definition_unresolved", "definition_inventory"
                )
            if len(candidates) != 1:
                raise _BootstrapBlocked(
                    "bootstrap_definition_ambiguous", "definition_inventory"
                )
            kind, path = candidates[0]
            proof = (kind, name, path)
            if proof not in used_definitions:
                used_definitions.append(proof)
            return kind, path

        def select_and_scan(path: str, *, source_unit: bool) -> dict:
            nonlocal selected_bytes
            canonical = os.path.realpath(path)
            cached = scan_cache.get(canonical)
            if cached is not None:
                if source_unit:
                    selected_source_paths.add(canonical)
                return cached
            _deadline_check(deadline)
            if len(selected_paths) >= int(config.max_source_inputs):
                raise _BootstrapBlocked("bootstrap_source_input_limit_exceeded", "source_closure")
            try:
                size = os.stat(canonical).st_size
            except OSError:
                raise _BootstrapBlocked("bootstrap_source_unreadable", "source_closure") from None
            if selected_bytes + size > int(config.max_source_bytes):
                raise _BootstrapBlocked("bootstrap_source_byte_limit_exceeded", "source_closure")
            scan = scan_sv_file(canonical, retain_source_text=False)
            selected_paths.add(canonical)
            selected_bytes += size
            scan_cache[canonical] = scan
            if source_unit:
                selected_source_paths.add(canonical)
            else:
                selected_support_paths.add(canonical)
            return scan

        top_path = unique_definition("modules", top)
        current_module = top
        current_path = top_path
        current_scan = select_and_scan(current_path, source_unit=True)
        ancestors = [top]
        component_tree: dict[str, dict] = {top: {}}
        children_cursor = component_tree[top]
        parts = normalized_signal.split(".")
        for index, instance_name in enumerate(parts[1:-1], 1):
            _deadline_check(deadline)
            if index > int(config.max_hierarchy_depth):
                raise _BootstrapBlocked("bootstrap_hierarchy_depth_exceeded", "target_scope")
            by_module = current_scan.get("module_instance_map") or {}
            instances = by_module.get(current_module, []) if isinstance(by_module, dict) else []
            matches = [
                item
                for item in instances
                if isinstance(item, dict) and item.get("instance_name") == instance_name
            ]
            if not matches:
                break
            if len(matches) != 1:
                raise _BootstrapBlocked("bootstrap_instance_ambiguous", "target_scope")
            child_module = str(matches[0].get("module_name") or "")
            if not child_module:
                raise _BootstrapBlocked("bootstrap_child_type_unresolved", "target_scope")
            child_kind, child_path = unique_design_definition(child_module)
            node = {
                "type": "interface" if child_kind == "interfaces" else "module",
                "class": child_module,
                "source_file": current_path,
                "source_line": None,
                "source_info_origin": "compile_log",
            }
            children_cursor[instance_name] = node
            nested: dict[str, dict] = {}
            node["children"] = nested
            children_cursor = nested
            ancestors.append(".".join(parts[: index + 1]))
            current_module = child_module
            current_path = child_path
            current_scan = select_and_scan(current_path, source_unit=True)

        include_tree = compile_result.get("include_tree")
        include_tree = include_tree if isinstance(include_tree, Mapping) else {}
        pending = [(path, 0) for path in scan_cache]
        processed: set[str] = set()
        while pending:
            _deadline_check(deadline)
            path, path_include_depth = pending.pop(0)
            if path in processed:
                continue
            processed.add(path)
            scan = scan_cache[path]
            children = include_tree.get(path)
            include_children = [
                os.path.realpath(str(item))
                for item in children
            ] if isinstance(children, Sequence) and not isinstance(children, (str, bytes)) else []
            if scan.get("include_directives") and not include_children:
                raise _BootstrapBlocked("bootstrap_include_context_unproved", "source_closure")
            for child in include_children:
                child_depth = path_include_depth + 1
                if child_depth > int(config.max_include_depth):
                    raise _BootstrapBlocked(
                        "bootstrap_include_depth_exceeded", "source_closure"
                    )
                select_and_scan(child, source_unit=False)
                pending.append((child, child_depth))
            for package in scan.get("package_imports") or []:
                if package == "std":
                    continue
                if package == "uvm_pkg":
                    # Do not replay the simulator's full UVM library in a
                    # resource-bounded artifact.  The existing Source Graph
                    # coverage contract already treats UVM as dynamic/opaque;
                    # frontend diagnostics keep coverage inconclusive while
                    # still allowing an unrelated, mechanically proved local
                    # RTL assignment or binding to survive as a positive fact.
                    objective_exclusions.add("uvm_dynamic_connectivity")
                    continue
                package_path = unique_definition("packages", str(package))
                select_and_scan(package_path, source_unit=True)
                pending.append((package_path, 0))

        selected_macro_defs = _compile_command_macro_definitions(compile_result)
        selected_macro_uses: set[str] = set()
        for scan in scan_cache.values():
            selected_macro_defs.update(str(item) for item in scan.get("macro_definitions") or [])
            selected_macro_uses.update(str(item) for item in scan.get("macro_uses") or [])
            selected_macro_uses.update(str(item) for item in scan.get("conditional_macros") or [])
        unresolved_macros = selected_macro_uses - selected_macro_defs
        if unresolved_macros:
            load_inventory()
            for kind, name, proved_path in used_definitions:
                if definitions[kind].get(name, []) != [proved_path]:
                    raise _BootstrapBlocked(
                        "bootstrap_definition_ambiguous",
                        "definition_inventory",
                    )
            externally_defined = {
                macro
                for macro in unresolved_macros
                if inventory_macros.get(macro)
                and not inventory_macros[macro].issubset(selected_paths)
            }
            if externally_defined:
                raise _BootstrapBlocked("bootstrap_preprocessor_context_unproved", "source_closure")

        selected_ordered_records = [
            record
            for record in canonical_records
            if str(record["path"]) in selected_source_paths
        ]
        ordered_selected_paths = {str(record["path"]) for record in selected_ordered_records}
        if ordered_selected_paths != selected_source_paths:
            raise _BootstrapBlocked("bootstrap_source_order_unproved", "compile_manifest")
        bounded_compile_result = _trim_compile_result(
            compile_result,
            selected_source_paths=selected_source_paths,
            selected_support_paths=selected_support_paths,
            ordered_records=selected_ordered_records,
            objective_exclusions=sorted(objective_exclusions),
        )
        hierarchy_result = {
            "project": {
                "top_module": top,
                "simulator": compile_result.get("simulator", "unknown"),
                "source_info_overlay": "compile_log",
                "source_info_overlay_reason": "bounded_bootstrap",
            },
            "component_tree": component_tree,
            "compile_result": bounded_compile_result,
            "_hierarchy_snapshot_sha256": hierarchy_snapshot_sha256,
        }
        elapsed_ms = (time.monotonic() - started) * 1000.0
        receipt = {
            "used": True,
            "status": "ready",
            "scope": "single_endpoint",
            "ancestor_chain_proved": True,
            "coverage_status": "inconclusive",
            "objective_exclusions": sorted(objective_exclusions),
            "metrics": {
                "inventory_file_count": inventory_files,
                "inventory_bytes": inventory_bytes,
                "selected_file_count": len(selected_paths),
                "selected_source_input_count": len(selected_source_paths),
                "selected_support_file_count": len(selected_support_paths),
                "selected_source_bytes": selected_bytes,
                "ancestor_count": len(ancestors),
                "wall_time_ms": round(elapsed_ms, 3),
            },
        }
        return BoundedBootstrapResult(
            status="ready",
            compile_result=bounded_compile_result,
            hierarchy_result=hierarchy_result,
            receipt=receipt,
        )
    except _BootstrapBlocked as exc:
        return blocked(exc.code, exc.stage)
