"""
tb_hierarchy_builder.py
扫描用户源文件并构建 testbench hierarchy。
"""

import os
import re
import time
from collections import defaultdict

from .cancellation import check_cancelled
from .compile_session_snapshot import (
    CompileSessionSnapshot,
    CompileSessionSnapshotBuilder,
)
from .compile_source_index import CompileSourceIndex
from .operation_metrics import read_process_rss_kib
from .sv_preprocessor import (
    PreprocessedSource,
    SystemVerilogPreprocessor,
    merge_resolved_include_evidence,
)


_CLASS_EXTENDS_RE = re.compile(r"\bclass\s+(\w+)\s+extends\s+(\w+)", re.IGNORECASE)
_CLASS_RE = re.compile(r"\bclass\s+(\w+)(?:\s+extends\s+\w+)?", re.IGNORECASE)
_MODULE_RE = re.compile(r"^[ \t]*module\s+(\w+)\b", re.IGNORECASE | re.MULTILINE)
_INTERFACE_RE = re.compile(
    r"^[ \t]*interface\s+(\w+)\b", re.IGNORECASE | re.MULTILINE
)
_PACKAGE_RE = re.compile(r"^[ \t]*package\s+(\w+)\b", re.IGNORECASE | re.MULTILINE)
_PACKAGE_IMPORT_RE = re.compile(
    r"\bimport\s+([A-Za-z_][A-Za-z0-9_$]*)\s*::", re.IGNORECASE
)
_PACKAGE_QUALIFIER_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*::", re.IGNORECASE
)
_INCLUDE_DIRECTIVE_RE = re.compile(r"`include\s+[\"<]([^\">]+)[\">]")
_CONDITIONAL_DIRECTIVE_RE = re.compile(
    r"`(?:ifdef|ifndef|elsif)\b", re.IGNORECASE
)
_MACRO_DEFINE_RE = re.compile(r"`define\s+([A-Za-z_][A-Za-z0-9_$]*)")
_MACRO_UNDEF_RE = re.compile(r"`undef\s+([A-Za-z_][A-Za-z0-9_$]*)")
_MACRO_CONDITION_RE = re.compile(
    r"`(?:ifdef|ifndef|elsif)\s+([A-Za-z_][A-Za-z0-9_$]*)", re.IGNORECASE
)

_NPI_SOURCE_MAP_METRIC_KEYS = frozenset({
    "status",
    "lookup_mode",
    "compile_parse_wall_ms",
    "kdb_probe_wall_ms",
    "design_load_wall_ms",
    "top_list_wall_ms",
    "instance_walk_wall_ms",
    "instance_lookup_wall_ms",
    "total_wall_ms",
    "top_instance_count",
    "requested_instance_count",
    "lookup_error_count",
    "instance_visited_count",
    "source_entry_count",
    "depth_limit_count",
    "full_name_error_count",
    "child_list_error_count",
    "design_load_cache_hit",
    "rss_start_kib",
    "rss_after_load_kib",
    "rss_peak_kib",
    "rss_end_kib",
})
_NPI_AUTO_SOURCE_OVERLAY_MAX_PATHS = 4_096
_NPI_TARGETED_SOURCE_OVERLAY_HARD_MAX_PATHS = 100_000


def _source_overlay_metrics(status: str) -> dict:
    rss_kib = read_process_rss_kib()
    return {
        "status": status,
        "probe_wall_ms": 0.0,
        "backend_select_wall_ms": 0.0,
        "collect_wall_ms": 0.0,
        "merge_wall_ms": 0.0,
        "policy_mode": "auto",
        "target_path_collect_wall_ms": 0.0,
        "target_instance_path_count": 0,
        "target_path_limit": 0,
        "target_path_limit_exceeded": 0,
        "npi_map_entry_count": 0,
        "hierarchy_node_count": 0,
        "annotated_node_count": 0,
        "annotation_coverage_ppm": 0,
        "rss_start_kib": rss_kib,
        "rss_peak_kib": rss_kib,
        "rss_end_kib": rss_kib,
    }


_MACRO_TOKEN_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_$]*)")
_PREPROCESSOR_KEYWORDS = {
    "begin_keywords",
    "celldefine",
    "default_nettype",
    "define",
    "else",
    "elsif",
    "end_keywords",
    "endcelldefine",
    "endif",
    "ifdef",
    "ifndef",
    "include",
    "line",
    "nounconnected_drive",
    "pragma",
    "resetall",
    "timescale",
    "unconnected_drive",
    "undef",
    "undefineall",
}
_CREATE_RE = re.compile(r'(\w+)\s*=\s*(\w+)::type_id::create\s*\(\s*"([^"]+)"', re.IGNORECASE)
_VIRTUAL_IF_RE = re.compile(r"\bvirtual\s+(\w+)\s+(\w+)", re.IGNORECASE)
_UVM_IMPORT_RE = re.compile(r"\bimport\s+uvm_pkg\s*::", re.IGNORECASE)
_UVM_EXTENDS_RE = re.compile(r"\bextends\s+uvm_\w+", re.IGNORECASE)
_SV_STRING_RE = re.compile(r'"(?:\\.|[^"\\])*"')
_SV_TOKEN_RE = re.compile(
    # Keep every non-whitespace delimiter instead of silently dropping
    # operators.  The instance recognizer relies on adjacency: collapsing
    # ``lhs = func(...)`` into ``lhs func(...)`` makes an assignment look
    # exactly like ``module_type instance_name(...)``.  Multi-character scope
    # tokens stay grouped; all remaining punctuation is intentionally opaque.
    r"[A-Za-z_][A-Za-z0-9_$]*|::|\S"
)
_SV_IDENTIFIER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*\Z")

_MODULE_INSTANCE_EXCLUDES = {
    "if", "for", "while", "case", "function", "task", "module", "class",
    "interface", "package", "return", "assign", "always", "initial",
    "else", "repeat", "generate", "begin", "end", "unique", "priority",
    "always_comb", "always_ff", "always_latch", "typedef", "property",
    "sequence", "covergroup", "constraint", "assert", "assume", "cover",
    "expect", "logic", "wire", "reg", "bit", "byte", "shortint", "int",
    "longint", "integer", "time", "realtime", "string", "void", "event",
    "input", "output", "inout", "ref", "const", "var", "parameter",
    "localparam", "genvar", "automatic", "static", "rand", "randc",
}

_MODULE_INSTANCE_PRECEDING_EXCLUDES = {
    "function", "task", "typedef", "class", "property", "sequence",
    "covergroup", "constraint", "import", "export", "return", "new",
    "automatic", "static",
}
_HIERARCHY_RSS_SAMPLE_STRIDE = 32
_INTERFACE_REFERENCE_PATTERN_MAX_NAMES = 256


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


def _sv_tokens(text: str) -> list[str]:
    # The old combined regex returned every token and then rejected quoted
    # strings in Python. Replacing the same string grammar with one whitespace
    # character preserves token boundaries while allowing one C-level findall
    # to return only structural tokens.
    return _SV_TOKEN_RE.findall(_SV_STRING_RE.sub(" ", text))


def _skip_balanced(
    tokens: list[str], index: int, opener: str, closer: str
) -> int | None:
    if index >= len(tokens) or tokens[index] != opener:
        return None
    depth = 0
    for cursor in range(index, len(tokens)):
        token = tokens[cursor]
        if token == opener:
            depth += 1
        elif token == closer:
            depth -= 1
            if depth == 0:
                return cursor + 1
    return None


def _module_bodies(tokens: list[str]):
    index = 0
    while index < len(tokens):
        if tokens[index].lower() != "module":
            index += 1
            continue
        name_index = index + 1
        if (
            name_index < len(tokens)
            and tokens[name_index].lower() in {"automatic", "static"}
        ):
            name_index += 1
        if (
            name_index >= len(tokens)
            or not _SV_IDENTIFIER_RE.fullmatch(tokens[name_index])
        ):
            index += 1
            continue
        module_name = tokens[name_index]
        header_end = index + 1
        nesting = 0
        while header_end < len(tokens):
            token = tokens[header_end]
            if token in {"(", "[", "{"}:
                nesting += 1
            elif token in {")", "]", "}"} and nesting:
                nesting -= 1
            elif token == ";" and nesting == 0:
                break
            header_end += 1
        if header_end >= len(tokens):
            return
        body_end = header_end + 1
        while body_end < len(tokens) and tokens[body_end].lower() != "endmodule":
            body_end += 1
        yield module_name, tokens[header_end + 1 : body_end]
        index = body_end + 1


def _parse_instance_statement(
    tokens: list[str], index: int
) -> tuple[list[dict], int] | None:
    module_name = tokens[index]
    if not _SV_IDENTIFIER_RE.fullmatch(module_name):
        return None
    if module_name.lower() in _MODULE_INSTANCE_EXCLUDES:
        return None
    if index and tokens[index - 1].lower() in _MODULE_INSTANCE_PRECEDING_EXCLUDES:
        return None

    cursor = index + 1
    if cursor < len(tokens) and tokens[cursor] == "#":
        cursor = _skip_balanced(tokens, cursor + 1, "(", ")") or -1
        if cursor < 0:
            return None

    instances: list[dict] = []
    while cursor < len(tokens):
        instance_name = tokens[cursor]
        if (
            not _SV_IDENTIFIER_RE.fullmatch(instance_name)
            or instance_name.lower() in _MODULE_INSTANCE_EXCLUDES
        ):
            return None
        cursor += 1
        while cursor < len(tokens) and tokens[cursor] == "[":
            cursor = _skip_balanced(tokens, cursor, "[", "]") or -1
            if cursor < 0:
                return None
        port_end = _skip_balanced(tokens, cursor, "(", ")")
        if port_end is None:
            return None
        instances.append(
            {"module_name": module_name, "instance_name": instance_name}
        )
        cursor = port_end
        if cursor < len(tokens) and tokens[cursor] == ",":
            cursor += 1
            continue
        if cursor < len(tokens) and tokens[cursor] == ";":
            return instances, cursor + 1
        return None
    return None


def _extract_module_instances(text: str) -> tuple[list[dict], dict[str, list[dict]]]:
    instances: list[dict] = []
    by_module: dict[str, list[dict]] = {}
    for module_name, body in _module_bodies(_sv_tokens(text)):
        module_instances: list[dict] = []
        index = 0
        while index < len(body):
            parsed = _parse_instance_statement(body, index)
            if parsed is None:
                index += 1
                continue
            found, index = parsed
            instances.extend(found)
            module_instances.extend(found)
        by_module.setdefault(module_name, []).extend(module_instances)
    return instances, by_module


def scan_sv_text(
    file_path: str,
    raw: str,
    *,
    retain_source_text: bool = True,
    scan_module_instances: bool = True,
) -> dict:
    """Extract hierarchy metadata from already-loaded SystemVerilog text.

    Keeping this text entry point separate lets hierarchy construction splice
    active include fragments before instance extraction without reading a
    compilation unit twice.  Direct callers continue to use ``scan_sv_file``.
    """

    check_cancelled()
    text = _strip_comments(raw)
    lower_text = text.lower()
    has_class = "class" in lower_text
    has_module = "module" in lower_text
    has_interface = "interface" in lower_text
    has_package = "package" in lower_text
    has_scope_operator = "::" in text
    has_backtick = "`" in text

    class_extends = (
        {
            child: parent
            for child, parent in _CLASS_EXTENDS_RE.findall(text)
        }
        if has_class and "extends" in lower_text
        else {}
    )
    classes = (
        list(dict.fromkeys(_CLASS_RE.findall(text))) if has_class else []
    )
    modules = (
        list(dict.fromkeys(_MODULE_RE.findall(text))) if has_module else []
    )
    interfaces = (
        list(dict.fromkeys(_INTERFACE_RE.findall(text)))
        if has_interface
        else []
    )
    creates = [
        {"var_name": var, "class_name": cls, "instance_name": inst}
        for var, cls, inst in _CREATE_RE.findall(text)
    ] if "::type_id::create" in lower_text else []

    if scan_module_instances and has_module:
        module_instances, module_instance_map = _extract_module_instances(text)
    else:
        module_instances, module_instance_map = [], {}

    virtual_interfaces = [
        {"interface_name": if_name, "var_name": var_name}
        for if_name, var_name in _VIRTUAL_IF_RE.findall(text)
    ] if "virtual" in lower_text else []

    packages = (
        list(dict.fromkeys(_PACKAGE_RE.findall(text))) if has_package else []
    )
    package_imports = (
        list(dict.fromkeys(_PACKAGE_IMPORT_RE.findall(text)))
        if has_scope_operator and "import" in lower_text
        else []
    )
    package_qualifiers = (
        list(dict.fromkeys(_PACKAGE_QUALIFIER_RE.findall(text)))
        if has_scope_operator
        else []
    )
    include_directives = (
        list(dict.fromkeys(_INCLUDE_DIRECTIVE_RE.findall(text)))
        if "`include" in text
        else []
    )
    conditional_macros = (
        list(dict.fromkeys(_MACRO_CONDITION_RE.findall(text)))
        if has_backtick
        else []
    )
    macro_uses = (
        list(
            dict.fromkeys(
                name
                for name in _MACRO_TOKEN_RE.findall(text)
                if name.lower() not in _PREPROCESSOR_KEYWORDS
                and name not in {"__FILE__", "__LINE__"}
            )
        )
        if has_backtick
        else []
    )

    file_type = "unknown"
    if modules:
        file_type = "module"
    elif classes:
        file_type = "class"
    elif interfaces:
        file_type = "interface"

    result = {
        "path": file_path,
        "name": os.path.basename(file_path),
        "type": file_type,
        "has_uvm_import": bool(_UVM_IMPORT_RE.search(text)),
        "classes": classes,
        "class_extends": class_extends,
        "modules": modules,
        "interfaces": interfaces,
        "structural_modules": modules,
        "structural_interfaces": interfaces,
        "packages": packages,
        "package_imports": package_imports,
        # This is dependency-planning evidence, not a semantic classification:
        # ``name::`` can also name a class or nested scope.  Source Graph uses
        # an entry only when ``name`` exactly matches a compile-proved package
        # definition; every other qualifier is ignored conservatively.
        "package_qualifiers": package_qualifiers,
        "include_directives": include_directives,
        "has_conditional_preprocessing": bool(
            has_backtick and _CONDITIONAL_DIRECTIVE_RE.search(text)
        ),
        "macro_definitions": list(
            dict.fromkeys(_MACRO_DEFINE_RE.findall(text))
        ) if "`define" in text else [],
        "macro_undefinitions": list(
            dict.fromkeys(_MACRO_UNDEF_RE.findall(text))
        ) if "`undef" in text else [],
        "has_macro_undefineall": bool(
            "`undefineall" in text
            and re.search(r"`undefineall\b", text)
        ),
        "conditional_macros": conditional_macros,
        "macro_uses": macro_uses,
        "creates": creates,
        "module_instances": module_instances,
        "module_instance_map": module_instance_map,
        "virtual_interfaces": virtual_interfaces,
    }
    if retain_source_text:
        result["source_text"] = raw
    return result


def scan_sv_file(file_path: str, *, retain_source_text: bool = True) -> dict:
    """Extract hierarchy metadata from one SystemVerilog source.

    ``retain_source_text`` defaults to the historical behavior for direct
    callers such as Legacy Static.  Full hierarchy construction sets it only
    long enough to derive cross-file metadata, then drops the text before the
    result enters the handle store.
    """

    check_cancelled()
    with open(file_path, "r", errors="replace") as f:
        raw = f.read()
    check_cancelled()
    return scan_sv_text(
        file_path,
        raw,
        retain_source_text=retain_source_text,
    )


def scan_preprocessed_sv(
    file_path: str,
    source: PreprocessedSource,
    *,
    retain_source_text: bool = True,
) -> dict:
    """Scan file-local metadata plus include-aware module instances."""

    # Keep class/UVM metadata file-local. Expanding class-heavy headers into
    # one synthetic source would make every class appear to own every factory
    # ``create`` in that compilation unit. Only module-instance extraction
    # needs the textual include view for this hierarchy gap.
    hierarchy_text = (
        source.text if source.complete else source.trusted_hierarchy_text
    )
    replace_root_hierarchy = bool(hierarchy_text) and (
        not source.complete or hierarchy_text != source.root_text
    )
    discard_root_hierarchy = bool(source.issues) and not hierarchy_text
    result = scan_sv_text(
        file_path,
        source.root_text,
        retain_source_text=retain_source_text,
        # Expanded/trusted hierarchy facts below replace this result in full.
        # Avoid parsing root-local instances only to discard them immediately.
        scan_module_instances=not (
            replace_root_hierarchy or discard_root_hierarchy
        ),
    )
    if replace_root_hierarchy:
        structural_text = _strip_comments(hierarchy_text)
        structural_lower = structural_text.lower()
        (
            result["module_instances"],
            result["module_instance_map"],
        ) = (
            _extract_module_instances(structural_text)
            if "module" in structural_lower
            else ([], {})
        )
        result["structural_modules"] = (
            list(dict.fromkeys(_MODULE_RE.findall(structural_text)))
            if "module" in structural_lower
            else []
        )
        result["structural_interfaces"] = (
            list(dict.fromkeys(_INTERFACE_RE.findall(structural_text)))
            if "interface" in structural_lower
            else []
        )
    elif source.issues:
        # A hard uncertainty before any trusted structural text is safer as a
        # no-fact scan than a root-only scan that can see both sides of an
        # unresolved conditional.
        result["module_instances"] = []
        result["module_instance_map"] = {}
        result["structural_modules"] = []
        result["structural_interfaces"] = []
    result["include_directives"] = list(source.root_include_directives)
    result["active_include_directives"] = list(
        source.active_include_directives
    )
    result["resolved_include_paths"] = list(source.included_paths)
    result["include_context_complete"] = source.complete
    result["include_resolution_issues"] = list(source.issues)
    result["hierarchy_evidence_status"] = source.hierarchy_evidence_status
    result["has_conditional_preprocessing"] = (
        result["has_conditional_preprocessing"]
        or source.has_conditional_preprocessing
    )
    result["conditional_macros"] = list(
        dict.fromkeys(
            [
                *result["conditional_macros"],
                *source.conditional_macros,
            ]
        )
    )
    return result


def _find_interface_references(
    source_text: str,
    interface_names: list[str],
) -> set[str]:
    r"""Find interface-name tokens with bounded whole-file regex passes.

    Interface definitions are ``\w+`` names, so an alternation wrapped in
    word boundaries is equivalent to the historical per-name
    ``re.search(r"\bNAME\b", source_text)`` checks.  Chunking bounds regex
    compilation size while changing the scan count from one pass per
    interface to one pass per bounded group.
    """

    found: set[str] = set()
    for offset in range(
        0,
        len(interface_names),
        _INTERFACE_REFERENCE_PATTERN_MAX_NAMES,
    ):
        check_cancelled()
        chunk = interface_names[
            offset : offset + _INTERFACE_REFERENCE_PATTERN_MAX_NAMES
        ]
        pattern = re.compile(
            rf"\b(?:{'|'.join(re.escape(name) for name in chunk)})\b"
        )
        chunk_found: set[str] = set()
        for match in pattern.finditer(source_text):
            chunk_found.add(match.group(0))
            if len(chunk_found) == len(chunk):
                break
        found.update(chunk_found)
    return found


def _bind_referenced_interfaces(
    *,
    source_text: str,
    source_name: str,
    interface_defs: dict[str, dict],
    interface_bindings: dict[str, str],
) -> None:
    # A binding is first-wins. Once set, later source files cannot change it,
    # so rescanning those names has no observable effect. Preserve the old
    # basename substring exclusion and interface-definition insertion order.
    candidates = [
        interface_name
        for interface_name in interface_defs
        if interface_name not in interface_bindings
        and interface_name not in source_name
    ]
    if not candidates:
        return
    referenced = _find_interface_references(source_text, candidates)
    for interface_name in candidates:
        if interface_name in referenced:
            interface_bindings.setdefault(interface_name, source_name)


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


def _add_module_children(
    module_name: str,
    design_to_scan: dict,
    definition_kinds: dict[str, str],
    seen: set[str],
) -> dict:
    if module_name in seen:
        return {}
    seen = seen | {module_name}
    tree = {}
    result = design_to_scan.get(module_name)
    if not result:
        return tree
    # Baseline provenance comes from the compile_log file list (the parent
    # module is declared in result["path"]). B2's NPI pass may later
    # overwrite ``source_file`` / ``source_line`` with elaborated-netlist
    # truth and flip ``source_info_origin`` to "npi".
    parent_path = result.get("path") or None
    by_module = result.get("module_instance_map")
    items = (
        by_module.get(module_name, [])
        if isinstance(by_module, dict)
        else result.get("module_instances", [])
    )
    for item in items:
        child_name = item["module_name"]
        child_scan = design_to_scan.get(child_name)
        child_kind = definition_kinds.get(child_name)
        if child_scan is None or child_kind is None:
            continue
        child_src = child_scan["name"]
        node = {
            "type": child_kind,
            "class": child_name,
            "src": child_src,
            "role": _classify_node(child_name, item["instance_name"]),
            "source_file": parent_path,
            "source_line": None,
            "source_info_origin": "compile_log" if parent_path else None,
        }
        descendants = _add_module_children(
            child_name,
            design_to_scan,
            definition_kinds,
            seen,
        )
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
    design_to_scan, definition_kinds, class_to_scan = _build_symbol_indexes(
        scan_results
    )

    component_tree = {}
    top_node = _add_module_children(
        top_module,
        design_to_scan,
        definition_kinds,
        set(),
    )
    if top_node:
        component_tree[top_module] = top_node

    test_class = _pick_uvm_test_class(scan_results)
    if test_class:
        component_tree["uvm_test_top"] = _build_uvm_tree(test_class, class_to_scan, set())

    return component_tree


def build_hierarchy(
    compile_result: dict,
    compile_log_path: str | None = None,
    *,
    apply_source_overlay: bool = True,
    source_index: CompileSourceIndex | None = None,
    source_index_disposition: str | None = None,
) -> dict:
    total_started = time.perf_counter()
    rss_start_kib = read_process_rss_kib()
    file_entries = compile_result.get("files", {}).get("user", [])
    scan_started = time.perf_counter()
    (
        scan_results,
        scan_by_path,
        interface_defs,
        interface_bindings,
        scan_metrics,
        resolved_include_tree,
        resolved_ordered_includes,
        compile_session_snapshot,
    ) = _scan_user_files(
        file_entries,
        compile_result,
        source_index=source_index,
    )
    effective_compile_result = merge_resolved_include_evidence(
        compile_result,
        include_tree=resolved_include_tree,
        ordered_includes=resolved_ordered_includes,
    )
    scan_wall_ms = (time.perf_counter() - scan_started) * 1000.0
    grouped_files = _group_files_by_category(file_entries, scan_by_path)
    source_root = _compute_source_root(file_entries)

    top_module = compile_result.get("primary_top") or (
        compile_result.get("top_modules", [""])[0]
        if compile_result.get("top_modules")
        else ""
    )
    interfaces = []
    for interface_name in sorted(set(compile_result.get("interfaces", [])) | set(interface_defs)):
        src = interface_defs.get(interface_name, {}).get("name", "")
        interfaces.append({
            "name": interface_name,
            "src": src,
            "bound_in": interface_bindings.get(interface_name, ""),
        })

    tree_started = time.perf_counter()
    component_tree = build_component_tree(scan_results, top_module) if top_module else {}
    tree_wall_ms = (time.perf_counter() - tree_started) * 1000.0

    source_info_overlay = "compile_log"
    source_info_overlay_reason = None
    source_overlay_metrics = _source_overlay_metrics("disabled")

    # B2 enrichment: when a Verdi KDB is available, walk the elaborated
    # netlist and overwrite each component_tree node's source info with
    # NPI's truth. Failures here must never break the compile-log
    # baseline; ``_npi_annotate_component_tree`` swallows everything.
    overlay_started = time.perf_counter()
    if apply_source_overlay and compile_log_path and top_module and component_tree:
        (
            source_info_overlay,
            source_info_overlay_reason,
            source_overlay_metrics,
        ) = _npi_annotate_component_tree(
            component_tree=component_tree,
            top_module=top_module,
            compile_result=effective_compile_result,
            compile_log_path=compile_log_path,
        )
    elif apply_source_overlay:
        source_overlay_metrics["status"] = "skipped_missing_prerequisite"
    overlay_wall_ms = (time.perf_counter() - overlay_started) * 1000.0
    rss_end_kib = read_process_rss_kib()
    sampled_rss = [
        value
        for value in (
            rss_start_kib,
            scan_metrics.get("rss_peak_kib"),
            rss_end_kib,
        )
        if isinstance(value, int)
    ]
    build_metrics = {
        "status": "completed",
        **{
            key: value
            for key, value in scan_metrics.items()
            if key != "rss_peak_kib"
        },
        "scan_wall_ms": round(scan_wall_ms, 3),
        "tree_wall_ms": round(tree_wall_ms, 3),
        "source_overlay_wall_ms": round(overlay_wall_ms, 3),
        "source_overlay_metrics": source_overlay_metrics,
        "total_wall_ms": round(
            (time.perf_counter() - total_started) * 1000.0, 3
        ),
        "rss_start_kib": rss_start_kib,
        "rss_peak_kib": max(sampled_rss) if sampled_rss else None,
        "rss_end_kib": rss_end_kib,
    }
    if source_index_disposition is not None:
        build_metrics["compile_source_index_disposition"] = (
            source_index_disposition
        )

    return {
        "project": {
            "top_module": top_module,
            "source_root": source_root,
            "simulator": effective_compile_result.get("simulator", "unknown"),
            "source_info_overlay": source_info_overlay,
            "source_info_overlay_reason": source_info_overlay_reason,
        },
        "files": dict(grouped_files),
        "component_tree": component_tree,
        "class_hierarchy": build_class_hierarchy(scan_results),
        "interfaces": interfaces,
        "compile_result": effective_compile_result,
        "build_metrics": build_metrics,
        # Internal: kept on the full result so slim-payload helpers can
        # derive per-file metadata (e.g. uvm_file_count) without re-reading
        # source. Stripped from the LLM-facing slim payload in
        # build_slim_payload(). Underscore prefix marks it not part of the
        # public schema.
        "_scan_results": scan_results,
        # Private immutable digest/stat facts captured through the same reads
        # that produced the hierarchy. Source Graph may reuse them only while
        # every recorded stat identity remains current.
        "_compile_session_snapshot": compile_session_snapshot,
    }


def apply_npi_source_overlay(
    hierarchy_result: dict,
    compile_log_path: str,
) -> dict:
    """Apply the optional local NPI source overlay after lock-free scanning.

    The server uses this split so compile-log parsing and thousands of source
    scans can run in a cancellable worker thread while the existing local NPI
    execution model remains on the dispatch thread.  Direct callers retain the
    historical one-call behavior through ``build_hierarchy``'s default.
    """

    project = hierarchy_result.get("project")
    component_tree = hierarchy_result.get("component_tree")
    compile_result = hierarchy_result.get("compile_result")
    if not isinstance(project, dict):
        return hierarchy_result
    top_module = str(project.get("top_module") or "")
    started = time.perf_counter()
    overlay = "compile_log"
    reason = None
    overlay_metrics = _source_overlay_metrics("skipped_missing_prerequisite")
    if (
        top_module
        and isinstance(component_tree, dict)
        and component_tree
        and isinstance(compile_result, dict)
    ):
        overlay, reason, overlay_metrics = _npi_annotate_component_tree(
            component_tree=component_tree,
            top_module=top_module,
            compile_result=compile_result,
            compile_log_path=compile_log_path,
        )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    project["source_info_overlay"] = overlay
    project["source_info_overlay_reason"] = reason
    metrics = hierarchy_result.get("build_metrics")
    if isinstance(metrics, dict):
        previous_overlay_ms = float(metrics.get("source_overlay_wall_ms") or 0.0)
        metrics["source_overlay_wall_ms"] = round(
            previous_overlay_ms + elapsed_ms, 3
        )
        metrics["source_overlay_metrics"] = overlay_metrics
        metrics["total_wall_ms"] = round(
            float(metrics.get("total_wall_ms") or 0.0) + elapsed_ms, 3
        )
        rss_end_kib = read_process_rss_kib()
        metrics["rss_end_kib"] = rss_end_kib
        if isinstance(rss_end_kib, int):
            metrics["rss_peak_kib"] = max(
                int(metrics.get("rss_peak_kib") or 0), rss_end_kib
            )
    return hierarchy_result


def _npi_annotate_component_tree(
    component_tree: dict,
    top_module: str,
    compile_result: dict,
    compile_log_path: str,
) -> tuple[str, str | None, dict]:
    """Overlay NPI-derived file:line onto an already-built component_tree.

    Guarded against every known failure mode (missing VERDI_HOME, no KDB,
    pynpi unimportable, design load failure, individual node walk
    failure). Mutates ``component_tree`` in place, never raises, and returns
    the fixed-label public overlay provenance.
    """
    started = time.perf_counter()
    metrics = _source_overlay_metrics("started")

    def _sample_rss() -> int | None:
        rss_kib = read_process_rss_kib()
        if isinstance(rss_kib, int):
            peak_kib = metrics.get("rss_peak_kib")
            metrics["rss_peak_kib"] = max(
                int(peak_kib) if isinstance(peak_kib, int) else 0,
                rss_kib,
            )
        return rss_kib

    def _finish(
        status: str,
        *,
        overlay: str = "compile_log",
        reason: str | None = None,
    ) -> tuple[str, str | None, dict]:
        metrics["status"] = status
        metrics["total_wall_ms"] = round(
            (time.perf_counter() - started) * 1000.0,
            3,
        )
        rss_end_kib = _sample_rss()
        metrics["rss_end_kib"] = rss_end_kib
        rss_start_kib = metrics.get("rss_start_kib")
        if isinstance(rss_start_kib, int) and isinstance(rss_end_kib, int):
            metrics["rss_delta_kib"] = rss_end_kib - rss_start_kib
        return overlay, reason, metrics

    def _copy_backend_metrics(backend: object) -> None:
        raw = getattr(backend, "instance_src_map_metrics", None)
        if not isinstance(raw, dict):
            return
        sanitized: dict[str, object] = {}
        for key in _NPI_SOURCE_MAP_METRIC_KEYS:
            value = raw.get(key)
            if key in {"status", "lookup_mode"}:
                if isinstance(value, str) and re.fullmatch(r"[a-z0-9_]{1,64}", value):
                    sanitized[key] = value
            elif isinstance(value, (int, float)) and not isinstance(value, bool):
                sanitized[key] = value
            elif value is None and key.startswith("rss_"):
                sanitized[key] = None
        metrics["npi_backend"] = sanitized
        backend_peak_kib = sanitized.get("rss_peak_kib")
        if isinstance(backend_peak_kib, int):
            current_peak_kib = metrics.get("rss_peak_kib")
            metrics["rss_peak_kib"] = max(
                int(current_peak_kib) if isinstance(current_peak_kib, int) else 0,
                backend_peak_kib,
            )

    try:
        from config import (  # noqa: PLC0415
            get_connectivity_route_config,
            get_hierarchy_npi_overlay_config,
        )

        if get_connectivity_route_config().mode == "source_graph":
            return _finish(
                "skipped_by_policy",
                reason="npi_skipped_by_policy",
            )
        overlay_config = get_hierarchy_npi_overlay_config()
    except Exception:  # noqa: BLE001
        return _finish(
            "skipped_config_unavailable",
            reason="npi_overlay_config_unavailable",
        )
    metrics["policy_mode"] = overlay_config.mode
    if not overlay_config.valid:
        return _finish(
            "skipped_config_invalid",
            reason="npi_overlay_config_invalid",
        )
    if overlay_config.mode == "off":
        return _finish(
            "skipped_by_policy",
            reason="npi_overlay_disabled",
        )

    try:
        from .connectivity_backend import select_backend  # noqa: PLC0415
        from .verdi_backend import probe_verdi_backend  # noqa: PLC0415
    except Exception:  # noqa: BLE001
        return _finish("backend_import_failed")
    phase_started = time.perf_counter()
    try:
        backend_status = probe_verdi_backend(
            compile_result, compile_log_path=compile_log_path
        )
    except Exception:  # noqa: BLE001
        metrics["probe_wall_ms"] = round(
            (time.perf_counter() - phase_started) * 1000.0,
            3,
        )
        return _finish("probe_failed")
    metrics["probe_wall_ms"] = round(
        (time.perf_counter() - phase_started) * 1000.0,
        3,
    )
    if backend_status.get("kdb_flow", "none") == "none":
        return _finish("skipped_no_kdb")
    if (
        overlay_config.mode == "auto"
        and backend_status.get("kdb_validation_status") == "elaboration_error"
    ):
        return _finish(
            "skipped_degraded_kdb",
            reason="npi_overlay_degraded_kdb_skipped",
        )
    phase_started = time.perf_counter()
    try:
        backend = select_backend(backend_status)
    except Exception:  # noqa: BLE001
        metrics["backend_select_wall_ms"] = round(
            (time.perf_counter() - phase_started) * 1000.0,
            3,
        )
        return _finish("backend_select_failed")
    metrics["backend_select_wall_ms"] = round(
        (time.perf_counter() - phase_started) * 1000.0,
        3,
    )
    if getattr(backend, "name", None) != "verdi_npi":
        return _finish("skipped_nonlocal_backend")
    collector = getattr(backend, "collect_instance_src_map", None)
    if collector is None:
        return _finish("collector_unavailable")
    if not getattr(backend, "supports_targeted_instance_src_map", False):
        return _finish(
            "skipped_targeted_lookup_unavailable",
            reason="npi_overlay_targeted_lookup_unavailable",
        )
    simulator = compile_result.get("simulator") or "auto"
    path_limit = (
        _NPI_TARGETED_SOURCE_OVERLAY_HARD_MAX_PATHS
        if overlay_config.mode == "force"
        else _NPI_AUTO_SOURCE_OVERLAY_MAX_PATHS
    )
    metrics["target_path_limit"] = path_limit
    phase_started = time.perf_counter()
    instance_paths, path_limit_exceeded = _collect_component_instance_paths(
        component_tree,
        top_module,
        max_paths=path_limit,
    )
    metrics["target_path_collect_wall_ms"] = round(
        (time.perf_counter() - phase_started) * 1000.0,
        3,
    )
    metrics["target_instance_path_count"] = len(instance_paths)
    metrics["target_path_limit_exceeded"] = int(path_limit_exceeded)
    if path_limit_exceeded:
        return _finish(
            "skipped_path_budget",
            reason="npi_overlay_path_budget_exceeded",
        )
    if not instance_paths:
        return _finish("empty_target_set")
    phase_started = time.perf_counter()
    try:
        inst_map = collector(
            compile_log_path,
            simulator,
            instance_paths=instance_paths,
        )
    except Exception:  # noqa: BLE001
        metrics["collect_wall_ms"] = round(
            (time.perf_counter() - phase_started) * 1000.0,
            3,
        )
        _copy_backend_metrics(backend)
        return _finish("collection_failed")
    metrics["collect_wall_ms"] = round(
        (time.perf_counter() - phase_started) * 1000.0,
        3,
    )
    _copy_backend_metrics(backend)
    if not isinstance(inst_map, dict) or not inst_map:
        return _finish("empty_source_map")
    metrics["npi_map_entry_count"] = len(inst_map)
    degraded_overlay = getattr(backend, "kdb_load_quality", "clean") == "degraded"

    # component_tree shape: {top: {inst_name: node, ...}} where each node
    # may contain "children": {inst_name: node, ...}. Top-module key is
    # not a node and has no annotation to apply.
    children = component_tree.get(top_module)
    if isinstance(children, dict):
        merge_stats = {
            "hierarchy_node_count": 0,
            "annotated_node_count": 0,
        }
        phase_started = time.perf_counter()
        annotated_count = _overlay_npi_on_subtree(
            children,
            top_module,
            inst_map,
            stats=merge_stats,
        )
        metrics["merge_wall_ms"] = round(
            (time.perf_counter() - phase_started) * 1000.0,
            3,
        )
        metrics.update(merge_stats)
        hierarchy_node_count = merge_stats["hierarchy_node_count"]
        if hierarchy_node_count:
            metrics["annotation_coverage_ppm"] = round(
                annotated_count * 1_000_000 / hierarchy_node_count
            )
        if annotated_count:
            if degraded_overlay:
                return _finish(
                    "completed_partial",
                    overlay="npi_partial",
                    reason="npi_degraded_kdb",
                )
            return _finish("completed", overlay="npi")
    return _finish("no_matching_nodes")


def _collect_component_instance_paths(
    component_tree: dict,
    top_module: str,
    *,
    max_paths: int,
) -> tuple[tuple[str, ...], bool]:
    """Collect proved component paths without crossing a hard admission cap."""

    children = component_tree.get(top_module)
    if not isinstance(children, dict):
        return (), False
    paths: list[str] = []
    stack = [
        (f"{top_module}.{inst_name}", node)
        for inst_name, node in reversed(children.items())
        if isinstance(node, dict)
    ]
    while stack:
        full_path, node = stack.pop()
        paths.append(full_path)
        if len(paths) > max_paths:
            return tuple(paths[:max_paths]), True
        if len(paths) % 1024 == 0:
            check_cancelled()
        sub = node.get("children")
        if not isinstance(sub, dict):
            continue
        stack.extend(
            (f"{full_path}.{inst_name}", child)
            for inst_name, child in reversed(sub.items())
            if isinstance(child, dict)
        )
    return tuple(paths), False


def _overlay_npi_on_subtree(
    children: dict,
    parent_path: str,
    inst_map: dict,
    *,
    stats: dict[str, int] | None = None,
) -> int:
    annotated_count = 0
    for inst_name, node in children.items():
        if not isinstance(node, dict):
            continue
        if stats is not None:
            stats["hierarchy_node_count"] += 1
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
                annotated_count += 1
                if stats is not None:
                    stats["annotated_node_count"] += 1
        sub = node.get("children")
        if isinstance(sub, dict):
            annotated_count += _overlay_npi_on_subtree(
                sub,
                full_path,
                inst_map,
                stats=stats,
            )
    return annotated_count


def _scan_user_files(
    file_entries: list[dict],
    compile_result: dict,
    *,
    source_index: CompileSourceIndex | None = None,
) -> tuple[
    list[dict],
    dict[str, dict],
    dict[str, dict],
    dict[str, str],
    dict[str, int],
    dict[str, list[str]],
    list[dict[str, str]],
    CompileSessionSnapshot,
]:
    """Scan every compile input once without retaining its source body.

    Interface binding historically considered definitions encountered up to
    the current compile-order file.  Derive that fact while the current raw
    text is still available, then discard the text before storing the compact
    scan result.  This preserves the old ordering semantics and prevents handle
    memory from scaling with the sum of all source-file bytes.
    """

    scan_results: list[dict] = []
    scan_by_path: dict[str, dict] = {}
    interface_defs: dict[str, dict] = {}
    interface_bindings: dict[str, str] = {}
    requested_count = len(file_entries)
    scanned_count = 0
    missing_count = 0
    source_bytes_scanned = 0
    largest_source_bytes = 0
    rss_peak_kib = read_process_rss_kib() or 0
    content_snapshot_builder = CompileSessionSnapshotBuilder(
        indexed_reader=source_index.read if source_index is not None else None,
    )
    preprocessor = SystemVerilogPreprocessor(
        compile_result,
        source_loader=content_snapshot_builder.read_text,
    )
    resolved_include_tree: dict[str, list[str]] = {}
    resolved_ordered_includes: list[dict[str, str]] = []
    resolved_include_paths: set[str] = set()
    include_resolution_issues: set[str] = set()
    for index, entry in enumerate(file_entries):
        check_cancelled()
        path = entry["path"]
        try:
            stat_result = os.stat(path)
        except OSError:
            missing_count += 1
            content_snapshot_builder.mark_issue("compile_source_unavailable")
            continue
        try:
            preprocessed = preprocessor.preprocess(path)
        except OSError:
            missing_count += 1
            content_snapshot_builder.mark_issue("compile_source_unavailable")
            continue
        result = scan_preprocessed_sv(path, preprocessed)
        for parent, children in preprocessed.include_tree.items():
            destination = resolved_include_tree.setdefault(parent, [])
            for child in children:
                if child not in destination:
                    destination.append(child)
                resolved_include_paths.add(child)
        resolved_ordered_includes.extend(preprocessed.ordered_includes)
        include_resolution_issues.update(preprocessed.issues)
        scanned_count += 1
        source_bytes_scanned += stat_result.st_size
        largest_source_bytes = max(largest_source_bytes, stat_result.st_size)
        source_text = result.pop("source_text", "")
        for interface_name in result.get(
            "structural_interfaces", result["interfaces"]
        ):
            interface_defs[interface_name] = result
        for binding in result["virtual_interfaces"]:
            interface_bindings.setdefault(
                binding["interface_name"], result["name"]
            )
        _bind_referenced_interfaces(
            source_text=source_text,
            source_name=result["name"],
            interface_defs=interface_defs,
            interface_bindings=interface_bindings,
        )
        scan_results.append(result)
        scan_by_path[path] = result
        if (index + 1) % _HIERARCHY_RSS_SAMPLE_STRIDE == 0:
            sampled = read_process_rss_kib()
            if sampled is not None:
                rss_peak_kib = max(rss_peak_kib, sampled)
    check_cancelled()
    sampled = read_process_rss_kib()
    if sampled is not None:
        rss_peak_kib = max(rss_peak_kib, sampled)
    compile_session_snapshot = content_snapshot_builder.finish()
    return (
        scan_results,
        scan_by_path,
        interface_defs,
        interface_bindings,
        {
            "source_file_count_requested": requested_count,
            "source_file_count_scanned": scanned_count,
            "source_file_count_missing": missing_count,
            "source_bytes_scanned": source_bytes_scanned,
            "source_text_bytes_retained": 0,
            "largest_source_file_bytes": largest_source_bytes,
            "resolved_include_file_count": len(resolved_include_paths),
            "include_resolution_issue_count": len(include_resolution_issues),
            "include_resolution_issue_categories": sorted(
                include_resolution_issues
            ),
            "include_context_complete": not include_resolution_issues,
            "content_snapshot_file_count": compile_session_snapshot.file_count,
            "content_snapshot_bytes": compile_session_snapshot.total_bytes,
            "content_snapshot_issue_count": len(
                compile_session_snapshot.issue_codes
            ),
            "content_snapshot_complete": compile_session_snapshot.complete,
            **preprocessor.metrics_snapshot(),
            **(
                source_index.metrics_snapshot()
                if source_index is not None
                else {}
            ),
            "rss_peak_kib": rss_peak_kib,
        },
        resolved_include_tree,
        resolved_ordered_includes,
        compile_session_snapshot,
    )


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


def _build_symbol_indexes(
    scan_results: list[dict],
) -> tuple[dict[str, dict], dict[str, str], dict[str, dict]]:
    design_to_scan = {}
    definition_kinds: dict[str, str] = {}
    class_to_scan = {}
    for result in scan_results:
        for module_name in result.get("structural_modules", result["modules"]):
            design_to_scan[module_name] = result
            definition_kinds[module_name] = "module"
        for interface_name in result.get(
            "structural_interfaces", result["interfaces"]
        ):
            design_to_scan[interface_name] = result
            definition_kinds[interface_name] = "interface"
        for class_name in result["classes"]:
            class_to_scan[class_name] = result
    return design_to_scan, definition_kinds, class_to_scan


# ---------------------------------------------------------------------------
# Slim payload helpers (phase 2)
#
# build_hierarchy() still returns the full result dict. The helpers below
# derive the LLM-facing slim payload from that full result plus a handle
# string produced by src/hierarchy_handles.compute_handle(). They are pure
# functions over the full_result; the server layer (phase 3) is responsible
# for wiring them in and for caching/serving the full result via handles.
# ---------------------------------------------------------------------------


# Skeleton depth must stay small; raising this risks pulling token usage
# back toward the bloated payload we are trying to escape.
_DEFAULT_SKELETON_DEPTH = 2


def _is_uvm_scan(scan: dict) -> bool:
    """Return True if a scan result indicates UVM content.

    Two signals, either is sufficient:
    - source_text imports uvm_pkg
    - any class extends a uvm_* base class
    """
    if scan.get("has_uvm_import") is True:
        return True
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
        "build_metrics": dict(full_result.get("build_metrics", {}) or {}),
        "ambiguous_basenames": detect_ambiguous_basenames(user_files),
        "kdb_hint": kdb_hint,
        "handle_tools": dict(HANDLE_TOOL_NAMES),
    }
