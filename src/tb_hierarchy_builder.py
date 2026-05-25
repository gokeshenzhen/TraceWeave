"""
tb_hierarchy_builder.py
扫描用户源文件并构建 testbench hierarchy。
"""

import os
import re
from collections import defaultdict


_CLASS_EXTENDS_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+(\w+)", re.IGNORECASE)
_CLASS_RE = re.compile(r"\bclass\s+(\w+)(?:\s+extends\s+\w+)?", re.IGNORECASE)
_MODULE_RE = re.compile(r"^\s*module\s+(\w+)\b", re.IGNORECASE | re.MULTILINE)
_INTERFACE_RE = re.compile(r"^\s*interface\s+(\w+)\b", re.IGNORECASE | re.MULTILINE)
_CREATE_RE = re.compile(r'(\w+)\s*=\s*(\w+)::type_id::create\s*\(\s*"([^"]+)"', re.IGNORECASE)
_MODULE_INSTANCE_RE = re.compile(r"^\s*(\w+)\s+(\w+)\s*\(", re.MULTILINE)
_VIRTUAL_IF_RE = re.compile(r"\bvirtual\s+(\w+)\s+(\w+)", re.IGNORECASE)

_MODULE_INSTANCE_EXCLUDES = {
    "if", "for", "while", "case", "function", "task", "module", "class",
    "interface", "package", "return", "assign", "always", "initial",
    "else", "repeat", "generate", "begin", "end",
}


def _classify_node(module_name: str, instance_name: str) -> str:
    lower = f"{module_name}.{instance_name}".lower()
    if any(token in lower for token in ("assert", "checker", "scoreboard", "uvm", "monitor", "agent")):
        return "helper"
    if any(token in lower for token in ("dut", "rtl", "core", "design")):
        return "dut"
    return "tb"


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    text = re.sub(r"//.*", "", text)
    return text


def scan_sv_file(file_path: str) -> dict:
    with open(file_path, "r", errors="replace") as f:
        raw = f.read()
    text = _strip_comments(raw)

    class_extends = {child: parent for child, parent in _CLASS_EXTENDS_RE.findall(text)}
    classes = list(dict.fromkeys(_CLASS_RE.findall(text)))
    modules = list(dict.fromkeys(_MODULE_RE.findall(text)))
    interfaces = list(dict.fromkeys(_INTERFACE_RE.findall(text)))
    creates = [
        {"var_name": var, "class_name": cls, "instance_name": inst}
        for var, cls, inst in _CREATE_RE.findall(text)
    ]

    module_instances = []
    for module_name, instance_name in _MODULE_INSTANCE_RE.findall(text):
        if module_name.lower() in _MODULE_INSTANCE_EXCLUDES:
            continue
        if instance_name in modules:
            continue
        module_instances.append({
            "module_name": module_name,
            "instance_name": instance_name,
        })

    virtual_interfaces = [
        {"interface_name": if_name, "var_name": var_name}
        for if_name, var_name in _VIRTUAL_IF_RE.findall(text)
    ]

    file_type = "unknown"
    if modules:
        file_type = "module"
    elif classes:
        file_type = "class"
    elif interfaces:
        file_type = "interface"

    return {
        "path": file_path,
        "name": os.path.basename(file_path),
        "type": file_type,
        "source_text": raw,
        "classes": classes,
        "class_extends": class_extends,
        "modules": modules,
        "interfaces": interfaces,
        "creates": creates,
        "module_instances": module_instances,
        "virtual_interfaces": virtual_interfaces,
    }


def build_class_hierarchy(scan_results: list[dict]) -> list[str]:
    extends_map = {}
    for result in scan_results:
        extends_map.update(result["class_extends"])

    chains = []
    for child in sorted(extends_map):
        chain = [child]
        parent = extends_map[child]
        seen = {child}
        while parent and parent not in seen:
            chain.append(parent)
            seen.add(parent)
            parent = extends_map.get(parent)
        chains.append(" -> ".join(chain))
    return chains


def _add_module_children(module_name: str, module_to_scan: dict, seen: set[str]) -> dict:
    if module_name in seen:
        return {}
    seen = seen | {module_name}
    tree = {}
    result = module_to_scan.get(module_name)
    if not result:
        return tree
    # Baseline provenance comes from the compile_log file list (the parent
    # module is declared in result["path"]). B2's NPI pass may later
    # overwrite ``source_file`` / ``source_line`` with elaborated-netlist
    # truth and flip ``source_info_origin`` to "npi".
    parent_path = result.get("path") or None
    for item in result["module_instances"]:
        child_scan = module_to_scan.get(item["module_name"])
        child_src = child_scan["name"] if child_scan else ""
        node = {
            "type": "module",
            "class": item["module_name"],
            "src": child_src,
            "role": _classify_node(item["module_name"], item["instance_name"]),
            "source_file": parent_path,
            "source_line": None,
            "source_info_origin": "compile_log" if parent_path else None,
        }
        descendants = _add_module_children(item["module_name"], module_to_scan, seen)
        if descendants:
            node["children"] = descendants
        tree[item["instance_name"]] = node
    return tree


def _pick_uvm_test_class(scan_results: list[dict]) -> str | None:
    extends_map = {}
    for result in scan_results:
        extends_map.update(result["class_extends"])

    candidates = []
    for child in extends_map:
        parent = extends_map[child]
        while parent:
            if parent == "uvm_test":
                candidates.append(child)
                break
            parent = extends_map.get(parent)

    if not candidates:
        return None

    non_bases = [name for name in candidates if name not in extends_map.values()]
    return sorted(non_bases or candidates)[0]


def _build_uvm_tree(class_name: str, class_to_scan: dict, seen: set[str]) -> dict:
    if class_name in seen:
        return {}
    seen = seen | {class_name}
    result = class_to_scan.get(class_name)
    if not result:
        return {}

    tree = {}
    for item in result["creates"]:
        child_scan = class_to_scan.get(item["class_name"])
        child_node = {
            "class": item["class_name"],
            "src": child_scan["name"] if child_scan else "",
            "role": _classify_node(item["class_name"], item["instance_name"]),
        }
        descendants = _build_uvm_tree(item["class_name"], class_to_scan, seen)
        if descendants:
            child_node["children"] = descendants
        tree[item["instance_name"]] = child_node
    return tree


def build_component_tree(scan_results: list[dict], top_module: str) -> dict:
    module_to_scan, class_to_scan = _build_symbol_indexes(scan_results)

    component_tree = {}
    top_node = _add_module_children(top_module, module_to_scan, set())
    if top_node:
        component_tree[top_module] = top_node

    test_class = _pick_uvm_test_class(scan_results)
    if test_class:
        component_tree["uvm_test_top"] = _build_uvm_tree(test_class, class_to_scan, set())

    return component_tree


def build_hierarchy(compile_result: dict, compile_log_path: str | None = None) -> dict:
    file_entries = compile_result.get("files", {}).get("user", [])
    scan_results, scan_by_path, source_text_cache = _scan_user_files(file_entries)
    grouped_files = _group_files_by_category(file_entries, scan_by_path)
    source_root = _compute_source_root(file_entries)
    interface_defs, interface_bindings = _collect_interface_metadata(scan_results, source_text_cache)

    top_module = compile_result.get("top_modules", [""])[0] if compile_result.get("top_modules") else ""
    interfaces = []
    for interface_name in sorted(set(compile_result.get("interfaces", [])) | set(interface_defs)):
        src = interface_defs.get(interface_name, {}).get("name", "")
        interfaces.append({
            "name": interface_name,
            "src": src,
            "bound_in": interface_bindings.get(interface_name, ""),
        })

    component_tree = build_component_tree(scan_results, top_module) if top_module else {}

    # B2 enrichment: when a Verdi KDB is available, walk the elaborated
    # netlist and overwrite each component_tree node's source info with
    # NPI's truth. Failures here must never break the compile-log
    # baseline; ``_npi_annotate_component_tree`` swallows everything.
    if compile_log_path and top_module and component_tree:
        _npi_annotate_component_tree(
            component_tree=component_tree,
            top_module=top_module,
            compile_result=compile_result,
            compile_log_path=compile_log_path,
        )

    return {
        "project": {
            "top_module": top_module,
            "source_root": source_root,
            "simulator": compile_result.get("simulator", "unknown"),
        },
        "files": dict(grouped_files),
        "component_tree": component_tree,
        "class_hierarchy": build_class_hierarchy(scan_results),
        "interfaces": interfaces,
        "compile_result": compile_result,
        # Internal: kept on the full result so slim-payload helpers can
        # derive per-file metadata (e.g. uvm_file_count) without re-reading
        # source. Stripped from the LLM-facing slim payload in
        # build_slim_payload(). Underscore prefix marks it not part of the
        # public schema.
        "_scan_results": scan_results,
    }


def _npi_annotate_component_tree(
    component_tree: dict,
    top_module: str,
    compile_result: dict,
    compile_log_path: str,
) -> None:
    """Overlay NPI-derived file:line onto an already-built component_tree.

    Guarded against every known failure mode (missing VERDI_HOME, no KDB,
    pynpi unimportable, design load failure, individual node walk
    failure). Mutates ``component_tree`` in place; never raises.
    """
    try:
        from .connectivity_backend import select_backend  # noqa: PLC0415
        from .verdi_backend import probe_verdi_backend  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return
    try:
        backend_status = probe_verdi_backend(
            compile_result, compile_log_path=compile_log_path
        )
    except Exception:  # noqa: BLE001
        return
    if backend_status.get("kdb_flow", "none") == "none":
        return
    try:
        backend = select_backend(backend_status)
    except Exception:  # noqa: BLE001
        return
    if getattr(backend, "name", None) != "verdi_npi":
        return
    collector = getattr(backend, "collect_instance_src_map", None)
    if collector is None:
        return
    simulator = compile_result.get("simulator") or "auto"
    try:
        inst_map = collector(compile_log_path, simulator)
    except Exception:  # noqa: BLE001
        return
    if not inst_map:
        return

    # component_tree shape: {top: {inst_name: node, ...}} where each node
    # may contain "children": {inst_name: node, ...}. Top-module key is
    # not a node and has no annotation to apply.
    children = component_tree.get(top_module)
    if isinstance(children, dict):
        _overlay_npi_on_subtree(children, top_module, inst_map)


def _overlay_npi_on_subtree(
    children: dict,
    parent_path: str,
    inst_map: dict,
) -> None:
    for inst_name, node in children.items():
        if not isinstance(node, dict):
            continue
        full_path = f"{parent_path}.{inst_name}"
        npi_entry = inst_map.get(full_path)
        if npi_entry is not None:
            file_val, line_val = npi_entry
            if file_val is not None:
                node["source_file"] = file_val
            if line_val is not None:
                node["source_line"] = line_val
            if file_val is not None or line_val is not None:
                node["source_info_origin"] = "npi"
        sub = node.get("children")
        if isinstance(sub, dict):
            _overlay_npi_on_subtree(sub, full_path, inst_map)


def _scan_user_files(file_entries: list[dict]) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    scan_results = []
    scan_by_path = {}
    source_text_cache: dict[str, str] = {}
    for entry in file_entries:
        path = entry["path"]
        if not os.path.exists(path):
            continue
        result = scan_sv_file(path)
        scan_results.append(result)
        scan_by_path[path] = result
        source_text_cache[path] = result["source_text"]
    return scan_results, scan_by_path, source_text_cache


def _group_files_by_category(file_entries: list[dict], scan_by_path: dict[str, dict]) -> dict[str, list[dict]]:
    grouped_files = defaultdict(list)
    for entry in file_entries:
        path = entry["path"]
        result = scan_by_path.get(path)
        grouped_files[entry["category"]].append({
            "name": os.path.basename(path),
            "path": path,
            "type": result["type"] if result else entry["type"],
        })
    return dict(grouped_files)


def _compute_source_root(file_entries: list[dict]) -> str:
    if not file_entries:
        return ""
    return os.path.commonpath([item["path"] for item in file_entries])


def _build_symbol_indexes(scan_results: list[dict]) -> tuple[dict[str, dict], dict[str, dict]]:
    module_to_scan = {}
    class_to_scan = {}
    for result in scan_results:
        for module_name in result["modules"]:
            module_to_scan[module_name] = result
        for class_name in result["classes"]:
            class_to_scan[class_name] = result
    return module_to_scan, class_to_scan


def _collect_interface_metadata(
    scan_results: list[dict], source_text_cache: dict[str, str]
) -> tuple[dict[str, dict], dict[str, str]]:
    interface_defs = {}
    interface_bindings = {}
    for result in scan_results:
        for interface_name in result["interfaces"]:
            interface_defs[interface_name] = result
        for binding in result["virtual_interfaces"]:
            interface_bindings.setdefault(binding["interface_name"], result["name"])
        _bind_interfaces_by_reference(result, interface_defs, interface_bindings, source_text_cache)
    return interface_defs, interface_bindings


def _bind_interfaces_by_reference(
    scan_result: dict,
    interface_defs: dict[str, dict],
    interface_bindings: dict[str, str],
    source_text_cache: dict[str, str],
):
    source_text = source_text_cache.get(scan_result["path"], "")
    for interface_name in interface_defs:
        if interface_name in scan_result["name"]:
            continue
        if re.search(rf"\b{re.escape(interface_name)}\b", source_text):
            interface_bindings.setdefault(interface_name, scan_result["name"])


# ---------------------------------------------------------------------------
# Slim payload helpers (phase 2)
#
# build_hierarchy() still returns the full result dict. The helpers below
# derive the LLM-facing slim payload from that full result plus a handle
# string produced by src/hierarchy_handles.compute_handle(). They are pure
# functions over the full_result; the server layer (phase 3) is responsible
# for wiring them in and for caching/serving the full result via handles.
# ---------------------------------------------------------------------------


_UVM_IMPORT_RE = re.compile(r"\bimport\s+uvm_pkg\s*::", re.IGNORECASE)
_UVM_EXTENDS_RE = re.compile(r"\bextends\s+uvm_\w+", re.IGNORECASE)
# Skeleton depth must stay small; raising this risks pulling token usage
# back toward the bloated payload we are trying to escape.
_DEFAULT_SKELETON_DEPTH = 2


def _is_uvm_scan(scan: dict) -> bool:
    """Return True if a scan result indicates UVM content.

    Two signals, either is sufficient:
    - source_text imports uvm_pkg
    - any class extends a uvm_* base class
    """
    text = scan.get("source_text", "") or ""
    if _UVM_IMPORT_RE.search(text):
        return True
    if _UVM_EXTENDS_RE.search(text):
        return True
    for parent in scan.get("class_extends", {}).values():
        if parent and parent.lower().startswith("uvm_"):
            return True
    return False


def _walk_component_tree(component_tree: dict):
    """Yield every (inst_name, node_dict) pair in the component_tree.

    The tree shape is irregular: the top-level value for the root module is
    a children-dict, not a node. We treat each (key, value) where value is a
    dict-with-children as a node. Synthetic top-level entries (the root
    module name and the optional "uvm_test_top" anchor) are walked as
    container dicts whose entries are real nodes.
    """
    for top_key, top_val in component_tree.items():
        if not isinstance(top_val, dict):
            continue
        # top_val is a children-dict (inst_name -> node)
        for inst_name, node in top_val.items():
            if not isinstance(node, dict):
                continue
            yield inst_name, node
            sub = node.get("children")
            if isinstance(sub, dict):
                yield from _walk_children(sub)


def _walk_children(children: dict):
    for inst_name, node in children.items():
        if not isinstance(node, dict):
            continue
        yield inst_name, node
        sub = node.get("children")
        if isinstance(sub, dict):
            yield from _walk_children(sub)


def _tree_depth(component_tree: dict) -> int:
    """Maximum depth of the component_tree. Root counts as 1."""
    if not component_tree:
        return 0

    def _depth_of_children(children: dict) -> int:
        if not children:
            return 0
        best = 0
        for node in children.values():
            if not isinstance(node, dict):
                continue
            sub = node.get("children")
            d = 1 + (_depth_of_children(sub) if isinstance(sub, dict) else 0)
            if d > best:
                best = d
        return best

    overall = 0
    for top_val in component_tree.values():
        if isinstance(top_val, dict):
            d = 1 + _depth_of_children(top_val)
            if d > overall:
                overall = d
    return overall


def compute_stats(full_result: dict) -> dict:
    """Return the `stats` block of the slim payload.

    Counts are derived from the full hierarchy result. `_scan_results` (set
    by build_hierarchy) is consulted for uvm_file_count; if absent the
    counter falls back to 0 rather than re-reading source files.
    """
    compile_result = full_result.get("compile_result", {}) or {}
    user_files = (compile_result.get("files", {}) or {}).get("user", []) or []
    file_count = len(user_files)

    component_tree = full_result.get("component_tree", {}) or {}
    nodes = list(_walk_component_tree(component_tree))
    instance_count = len(nodes)
    module_count = len({
        node.get("class") for _, node in nodes if node.get("class")
    })

    interfaces = full_result.get("interfaces", []) or []
    class_hierarchy = full_result.get("class_hierarchy", []) or []

    scan_results = full_result.get("_scan_results") or []
    uvm_file_count = sum(1 for scan in scan_results if _is_uvm_scan(scan))

    return {
        "file_count": file_count,
        "module_count": module_count,
        "instance_count": instance_count,
        "tree_depth": _tree_depth(component_tree),
        "class_count": len(class_hierarchy),
        "interface_count": len(interfaces),
        "uvm_file_count": uvm_file_count,
    }


def _skeleton_node(
    inst_name: str,
    node: dict,
    depth_remaining: int,
) -> dict:
    children = node.get("children") if isinstance(node, dict) else None
    child_count = len(children) if isinstance(children, dict) else 0
    skel = {
        "inst": inst_name,
        "module": node.get("class", "") if isinstance(node, dict) else "",
        "source_file": node.get("source_file") or "" if isinstance(node, dict) else "",
        "source_line": node.get("source_line") or 0 if isinstance(node, dict) else 0,
        "child_count": child_count,
        "truncated": False,
        "children": [],
    }
    if child_count == 0 or depth_remaining <= 0:
        skel["truncated"] = child_count > 0 and depth_remaining <= 0
        return skel
    for cname, cnode in children.items():
        skel["children"].append(_skeleton_node(cname, cnode, depth_remaining - 1))
    return skel


def extract_tree_skeleton(
    component_tree: dict,
    top_module: str,
    depth: int = _DEFAULT_SKELETON_DEPTH,
) -> dict:
    """Truncated view of component_tree starting from the top module.

    Returns a single root node with up to ``depth`` levels of descendants.
    Each node carries `child_count` so the LLM can decide whether the
    truncated branch is worth expanding via get_tb_subtree.
    """
    if not component_tree or not top_module:
        return {}

    top_children = component_tree.get(top_module)
    if not isinstance(top_children, dict):
        return {}

    child_count = len(top_children)
    root = {
        "inst": top_module,
        "module": top_module,
        "source_file": "",
        "source_line": 0,
        "child_count": child_count,
        "truncated": False,
        "children": [],
    }
    if depth <= 0:
        root["truncated"] = child_count > 0
        return root
    for inst_name, node in top_children.items():
        root["children"].append(_skeleton_node(inst_name, node, depth - 1))
    return root


def detect_ambiguous_basenames(file_entries: list[dict]) -> list[dict]:
    """Find files whose basename collides across multiple paths.

    The compile_log records exactly which path was linked into this run, so
    when several `xxx.v` (e.g. xxx_v1.v plus an unrelated xxx.v in a vendor
    dir, or rtl/foo.sv vs syn/foo.sv) show up we surface them as a warning
    block in the slim payload. The "picked" path is whichever copy the
    compile_log enumerated; downstream tools should treat it as ground
    truth and prompt the LLM to verify intent.
    """
    by_basename: dict[str, list[str]] = defaultdict(list)
    for entry in file_entries or []:
        path = entry.get("path")
        if not path:
            continue
        by_basename[os.path.basename(path)].append(path)

    out: list[dict] = []
    for basename, paths in sorted(by_basename.items()):
        if len(paths) < 2:
            continue
        out.append({
            "basename": basename,
            "paths": paths,
            "picked": paths[0],
        })
    return out


# Names of handle tools advertised in the slim payload. Kept here (next to
# the slim builder) so it tracks tool renames; server.py will register the
# actual MCP tools in phase 4.
HANDLE_TOOL_NAMES: dict[str, str] = {
    "subtree": "get_tb_subtree",
    "lookup_files": "lookup_tb_files",
    "find_instance": "find_tb_instance",
    "file_detail": "get_tb_file_detail",
    "class_hierarchy": "get_tb_class_hierarchy",
    "dump_section": "dump_tb_section",
}


# Cap on compile_command bytes in the slim payload. Xcelium `xrun
# -elaborate` lines routinely run 30+ KB after macro expansion; the LLM
# only needs the head for flow identification (simulator, top flags). The
# full command is still available via dump_tb_section(section="compile_result").
_COMPILE_COMMAND_BUDGET = 1024


def _trim_compile_command(cmd: str) -> str:
    if not cmd:
        return ""
    if len(cmd) <= _COMPILE_COMMAND_BUDGET:
        return cmd
    head = cmd[:_COMPILE_COMMAND_BUDGET]
    return (
        f"{head}\n…[truncated; {len(cmd) - _COMPILE_COMMAND_BUDGET} more bytes; "
        f"use dump_tb_section(section=\"compile_result\") for the full command]"
    )


def build_slim_payload(
    full_result: dict,
    handle: str,
    kdb_hint: dict | None = None,
) -> dict:
    """Project a full hierarchy result into the LLM-facing slim payload.

    The full result remains the authoritative source served via handles;
    this builder only chooses what crosses the wire.
    """
    project = full_result.get("project", {}) or {}
    compile_result = full_result.get("compile_result", {}) or {}
    user_files = (compile_result.get("files", {}) or {}).get("user", []) or []
    top_module = project.get("top_module", "")

    return {
        "hierarchy_handle": handle,
        "project": dict(project),
        "compile_command": _trim_compile_command(
            compile_result.get("compile_command", "") or ""
        ),
        "stats": compute_stats(full_result),
        "tree_skeleton": extract_tree_skeleton(
            full_result.get("component_tree", {}) or {},
            top_module,
        ),
        "interfaces": list(full_result.get("interfaces", []) or []),
        "ambiguous_basenames": detect_ambiguous_basenames(user_files),
        "kdb_hint": kdb_hint,
        "handle_tools": dict(HANDLE_TOOL_NAMES),
    }
