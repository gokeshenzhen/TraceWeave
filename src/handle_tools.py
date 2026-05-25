"""Implementations for the 6 hierarchy handle tools.

Each public function takes a resolved full_result dict (the cached output of
build_hierarchy) plus tool arguments, and returns a dict matching the
corresponding schema in src/schemas.py. Resolution failures (missing handle,
unknown file path, missing filter) are signaled by returning a dict shaped
like ``HandleErrorResult``. The server layer is responsible for converting
between handle strings and full_result via HandleStore.

These functions are intentionally pure (no I/O, no global state) so they
can be tested directly against constructed full_result fixtures.
"""

from __future__ import annotations

import os
from collections import defaultdict
from typing import Any, Iterable


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SUBTREE_HARD_NODE_LIMIT = 5000  # absolute ceiling regardless of caller arg


def _error(error: str, **fields: Any) -> dict[str, Any]:
    out: dict[str, Any] = {"error": error}
    out.update({k: v for k, v in fields.items() if v is not None})
    return out


def _walk_path(component_tree: dict, dotted_path: str) -> tuple[str, dict] | None:
    """Resolve a dotted instance path against component_tree.

    component_tree's top-level key is the top module name; its value is a
    children-dict keyed by instance name. Returns ``(name, node)`` where
    node is a dict containing at least 'children' (possibly absent), or
    None on miss. The synthetic top-level entry returned for root has
    {'children': top_children_dict} so callers can iterate uniformly.
    """
    if not component_tree:
        return None
    parts = [p for p in (dotted_path or "").split(".") if p]
    if not parts:
        top_module = next(iter(component_tree))
        top_children = component_tree.get(top_module)
        if not isinstance(top_children, dict):
            return None
        return top_module, {"children": top_children, "class": top_module}

    top_module = parts[0]
    top_children = component_tree.get(top_module)
    if not isinstance(top_children, dict):
        return None
    if len(parts) == 1:
        return top_module, {"children": top_children, "class": top_module}

    cursor = top_children
    node = None
    name = ""
    for inst in parts[1:]:
        if not isinstance(cursor, dict):
            return None
        node = cursor.get(inst)
        if not isinstance(node, dict):
            return None
        name = inst
        cursor = node.get("children")
    return name, node or {}


def _emit_node(
    inst_name: str,
    node: dict,
    depth_remaining: int,
    counter: list[int],
    max_nodes: int,
) -> dict:
    children = node.get("children") if isinstance(node, dict) else None
    child_count = len(children) if isinstance(children, dict) else 0
    out = {
        "inst": inst_name,
        "module": node.get("class", "") if isinstance(node, dict) else "",
        "source_file": node.get("source_file") or "" if isinstance(node, dict) else "",
        "source_line": node.get("source_line") or 0 if isinstance(node, dict) else 0,
        "child_count": child_count,
        "truncated": False,
        "children": [],
    }
    if child_count == 0:
        return out
    if depth_remaining == 0:
        out["truncated"] = True
        return out
    for cname, cnode in children.items():
        if counter[0] >= max_nodes:
            out["truncated"] = True
            break
        counter[0] += 1
        out["children"].append(
            _emit_node(cname, cnode, depth_remaining - 1, counter, max_nodes)
        )
    return out


def _count_descendants(node: dict) -> int:
    children = node.get("children") if isinstance(node, dict) else None
    if not isinstance(children, dict):
        return 0
    total = 0
    for child in children.values():
        total += 1 + _count_descendants(child)
    return total


# ---------------------------------------------------------------------------
# get_tb_subtree
# ---------------------------------------------------------------------------


def get_tb_subtree(
    full_result: dict,
    handle: str,
    *,
    root: str = "",
    depth: int = 1,
    max_nodes: int = 500,
) -> dict:
    component_tree = full_result.get("component_tree") or {}
    hit = _walk_path(component_tree, root)
    if hit is None:
        return _error(
            "instance_not_found",
            hint=f"path {root!r} does not exist in component_tree",
            current_handle=handle,
        )
    inst_name, node = hit
    effective_depth = depth if depth >= 0 else 10**6
    cap = min(max_nodes, _SUBTREE_HARD_NODE_LIMIT) if max_nodes > 0 else _SUBTREE_HARD_NODE_LIMIT
    counter = [0]
    rendered = _emit_node(inst_name, node, effective_depth, counter, cap)
    total_descendants = _count_descendants(node)
    return {
        "handle": handle,
        "root": root,
        "node": rendered,
        "truncated": rendered["truncated"] or counter[0] >= cap,
        "total_descendants": total_descendants,
    }


# ---------------------------------------------------------------------------
# lookup_tb_files
# ---------------------------------------------------------------------------


def _scan_has_uvm(scan: dict) -> bool:
    text = (scan.get("source_text") or "")
    if "uvm_pkg" in text and "import" in text:
        return True
    for parent in (scan.get("class_extends") or {}).values():
        if isinstance(parent, str) and parent.lower().startswith("uvm_"):
            return True
    return False


def lookup_tb_files(
    full_result: dict,
    handle: str,
    *,
    basename: str | None = None,
    name_contains: str | None = None,
    path_contains: str | None = None,
    has_module: str | None = None,
    contains_uvm: bool | None = None,
    file_type: str | None = None,
    limit: int = 50,
) -> dict:
    # At least one filter is required to keep callers from pulling the full
    # list through this endpoint. dump_tb_section(section="files_full") is
    # the explicit escape hatch.
    if not any(
        v is not None
        for v in (basename, name_contains, path_contains, has_module, contains_uvm, file_type)
    ):
        return _error(
            "filter_required",
            hint="provide at least one filter; use dump_tb_section(section='files_full') for full enumeration",
            current_handle=handle,
        )

    scans = full_result.get("_scan_results") or []
    matches: list[dict] = []
    for scan in scans:
        path = scan.get("path") or ""
        name = scan.get("name") or os.path.basename(path)
        if basename is not None and name != basename:
            continue
        if name_contains is not None and name_contains not in name:
            continue
        if path_contains is not None and path_contains not in path:
            continue
        if has_module is not None and has_module not in (scan.get("modules") or []):
            continue
        scan_type = scan.get("type") or ""
        if file_type is not None and scan_type != file_type:
            continue
        has_uvm = _scan_has_uvm(scan)
        if contains_uvm is True and not has_uvm:
            continue
        if contains_uvm is False and has_uvm:
            continue
        matches.append({
            "path": path,
            "file_type": scan_type,
            "modules": list(scan.get("modules") or []),
            "classes": list(scan.get("classes") or []),
            "has_uvm_import": has_uvm,
        })

    total = len(matches)
    truncated = total > limit
    return {
        "handle": handle,
        "matches": matches[:limit],
        "total": total,
        "truncated": truncated,
    }


# ---------------------------------------------------------------------------
# find_tb_instance
# ---------------------------------------------------------------------------


def _iter_instances(component_tree: dict) -> Iterable[tuple[str, str, dict]]:
    """Yield (full_path, parent_path, node) for every instance in the tree."""
    for top_module, top_children in component_tree.items():
        if not isinstance(top_children, dict):
            continue
        for inst, node in top_children.items():
            yield f"{top_module}.{inst}", top_module, node
            sub = node.get("children") if isinstance(node, dict) else None
            if isinstance(sub, dict):
                yield from _iter_instances_inner(sub, f"{top_module}.{inst}")


def _iter_instances_inner(children: dict, parent: str) -> Iterable[tuple[str, str, dict]]:
    for inst, node in children.items():
        full = f"{parent}.{inst}"
        yield full, parent, node
        sub = node.get("children") if isinstance(node, dict) else None
        if isinstance(sub, dict):
            yield from _iter_instances_inner(sub, full)


def find_tb_instance(
    full_result: dict,
    handle: str,
    *,
    path: str | None = None,
    module: str | None = None,
    limit: int = 100,
) -> dict:
    if path is None and module is None:
        return _error(
            "filter_required",
            hint="provide either path (exact) or module (matches all instances)",
            current_handle=handle,
        )
    if path is not None and module is not None:
        return _error(
            "mutually_exclusive_filters",
            hint="path and module cannot be combined",
            current_handle=handle,
        )

    component_tree = full_result.get("component_tree") or {}
    hits: list[dict] = []
    for full_path, parent, node in _iter_instances(component_tree):
        if path is not None and full_path != path:
            continue
        if module is not None and node.get("class") != module:
            continue
        hits.append({
            "path": full_path,
            "module": node.get("class") or "",
            "parent": parent,
            "source_file": node.get("source_file") or "",
            "source_line": node.get("source_line") or 0,
        })

    total = len(hits)
    return {
        "handle": handle,
        "hits": hits[:limit],
        "total": total,
        "truncated": total > limit,
    }


# ---------------------------------------------------------------------------
# get_tb_file_detail
# ---------------------------------------------------------------------------


def _build_path_index(full_result: dict) -> dict[str, dict]:
    scans = full_result.get("_scan_results") or []
    return {scan.get("path"): scan for scan in scans if scan.get("path")}


def get_tb_file_detail(
    full_result: dict,
    handle: str,
    *,
    path: str,
) -> dict:
    index = _build_path_index(full_result)
    scan = index.get(path)
    if scan is None:
        # Suggest by basename — common multi-version mistake (xxx_v1 vs xxx_v2).
        target_base = os.path.basename(path)
        suggestions = [p for p in index if os.path.basename(p) == target_base]
        return _error(
            "file_not_in_compile_set",
            hint="path was not compiled in this session; the slim payload's "
                 "ambiguous_basenames section reports multi-version collisions",
            current_handle=handle,
            did_you_mean=suggestions,
        )

    symbols: list[dict] = []
    for mod in scan.get("modules") or []:
        symbols.append({"name": mod, "kind": "module", "line": 0})
    for cls in scan.get("classes") or []:
        symbols.append({"name": cls, "kind": "class", "line": 0})
    for ifc in scan.get("interfaces") or []:
        symbols.append({"name": ifc, "kind": "interface", "line": 0})

    return {
        "handle": handle,
        "path": path,
        "file_type": scan.get("type") or "",
        "symbols": symbols,
        "includes": [],
        "has_uvm_import": _scan_has_uvm(scan),
    }


# ---------------------------------------------------------------------------
# get_tb_class_hierarchy
# ---------------------------------------------------------------------------


def _build_class_graph(full_result: dict) -> tuple[dict[str, str], dict[str, str]]:
    """Return (extends, owner_file) maps.

    extends[child] = parent_name. owner_file[class_name] = scan path where
    the class is defined (best-effort; first scan that lists it wins).
    """
    extends: dict[str, str] = {}
    owner_file: dict[str, str] = {}
    for scan in full_result.get("_scan_results") or []:
        extends.update(scan.get("class_extends") or {})
        path = scan.get("path") or ""
        for cls in scan.get("classes") or []:
            owner_file.setdefault(cls, path)
    return extends, owner_file


def get_tb_class_hierarchy(
    full_result: dict,
    handle: str,
    *,
    root_class: str | None = None,
    depth: int = -1,
) -> dict:
    extends, owner_file = _build_class_graph(full_result)
    if not extends and not owner_file:
        return {"handle": handle, "roots": [], "total": 0}

    children_map: dict[str, list[str]] = defaultdict(list)
    all_classes: set[str] = set()
    for child, parent in extends.items():
        children_map[parent].append(child)
        all_classes.add(child)
        all_classes.add(parent)
    all_classes.update(owner_file.keys())

    if root_class is not None:
        roots = [root_class] if root_class in all_classes else []
    else:
        # A "root" here = a class that nothing extends, OR an external base
        # class (e.g. uvm_test) referenced as a parent but not defined in
        # the compile set.
        roots = sorted(c for c in all_classes if c not in extends)

    effective_depth = depth if depth >= 0 else 10**6
    counter = [0]

    def emit(name: str, remaining: int) -> dict:
        counter[0] += 1
        node = {
            "name": name,
            "source_file": owner_file.get(name, ""),
            "source_line": 0,
            "children": [],
        }
        if remaining == 0:
            return node
        for child in sorted(children_map.get(name, [])):
            node["children"].append(emit(child, remaining - 1))
        return node

    rendered = [emit(r, effective_depth) for r in roots]
    return {"handle": handle, "roots": rendered, "total": counter[0]}


# ---------------------------------------------------------------------------
# dump_tb_section
# ---------------------------------------------------------------------------


_SECTION_MAP = {
    "compile_result": ("compile_result", None),
    "include_tree": ("compile_result", "include_tree"),
    "filelist_tree": ("compile_result", "filelist_tree"),
    "interfaces": ("interfaces", None),
    "files_full": ("files", None),
    "component_tree_full": ("component_tree", None),
    "class_hierarchy_full": ("class_hierarchy", None),
}

_LARGE_SECTION_BYTES = 50_000


def dump_tb_section(
    full_result: dict,
    handle: str,
    *,
    section: str,
) -> dict:
    spec = _SECTION_MAP.get(section)
    if spec is None:
        return _error(
            "unknown_section",
            hint=f"valid sections: {sorted(_SECTION_MAP)}",
            current_handle=handle,
        )
    top_key, sub_key = spec
    data: Any = full_result.get(top_key)
    if sub_key is not None and isinstance(data, dict):
        data = data.get(sub_key)
    if data is None:
        data = {} if section.endswith("_tree") or section in {
            "compile_result", "files_full", "component_tree_full"
        } else []

    try:
        approx_bytes = len(repr(data))
    except Exception:
        approx_bytes = 0
    warning = (
        f"section is large ({approx_bytes} bytes); prefer targeted handle tools"
        if approx_bytes > _LARGE_SECTION_BYTES
        else ""
    )

    return {
        "handle": handle,
        "section": section,
        "data": data,
        "warning": warning,
    }
