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
}


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
    for item in result["module_instances"]:
        child_scan = module_to_scan.get(item["module_name"])
        child_src = child_scan["name"] if child_scan else ""
        node = {
            "type": "module",
            "class": item["module_name"],
            "src": child_src,
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


def build_hierarchy(compile_result: dict) -> dict:
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

    return {
        "project": {
            "top_module": top_module,
            "source_root": source_root,
            "simulator": compile_result.get("simulator", "unknown"),
        },
        "files": dict(grouped_files),
        "component_tree": build_component_tree(scan_results, top_module) if top_module else {},
        "class_hierarchy": build_class_hierarchy(scan_results),
        "interfaces": interfaces,
        "compile_result": compile_result,
    }


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
