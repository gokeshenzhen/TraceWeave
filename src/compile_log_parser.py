"""
compile_log_parser.py
Extract user files, filelist relationships, include relationships, and top information
from compile and elaborate logs.
"""

import os
import re
from pathlib import Path


EDA_LIB_PREFIXES = [
    "/tools/synopsys/",
    "/tools/cadence/",
    "/tools/mentor/",
    "$VCS_HOME",
    "$XCELIUM_HOME",
    "$XLM_ROOT",
    "$UVM_HOME",
]


_VCS_FILE_RE = re.compile(r"Parsing design file '([^']+)'")
_VCS_INC_RE = re.compile(r"Parsing included file '([^']+)'")
_VCS_BACK_RE = re.compile(r"Back to file '([^']+)'")
_VCS_TOP_RE = re.compile(r"^\s+([A-Za-z_]\w*)\s*$")
_VCS_MODULE_RE = re.compile(r"recompiling module (\w+)", re.IGNORECASE)
_VCS_IF_RE = re.compile(r"recompiling interface (\w+)", re.IGNORECASE)

_XCE_FILE_RE = re.compile(r"^file:\s+(.+)$")
_XCE_ENTITY_RE = re.compile(r"^\s*(module|interface|package)\s+worklib\.(\w+):", re.IGNORECASE)
_TOP_RE = re.compile(r"(?:^|\s)-top\s+(\w+)")
_FILELIST_RE = re.compile(r"(?:^|\s)-f\s+(\S+)")


def _normalize_path(path: str, parent: str | None = None) -> str:
    path = path.strip().strip("'\"").rstrip(".")
    path = os.path.expandvars(path)
    if parent and not os.path.isabs(path):
        path = os.path.join(parent, path)
    return os.path.normpath(os.path.realpath(path))


def _is_eda_lib(path: str) -> bool:
    normalized = path.replace("\\", "/")
    for prefix in EDA_LIB_PREFIXES:
        expanded = os.path.expandvars(prefix).replace("\\", "/")
        if normalized.startswith(expanded):
            return True
    return False


def _categorize(path: str) -> str:
    lower = path.lower()
    if "tb" in lower or "testbench" in lower or "verif" in lower:
        return "tb"
    if "rtl" in lower or "dut" in lower or "design" in lower or "des_" in lower:
        return "rtl"
    if "assert" in lower or "sva" in lower:
        return "assertion"
    return "other"


def detect_simulator(log_path: str) -> str:
    with open(log_path, "r", errors="replace") as f:
        for _, line in zip(range(20), f):
            lower = line.lower()
            if "chronologic vcs" in lower or "parsing design file" in lower:
                return "vcs"
            if "xrun" in lower or "xmvlog" in lower or "xmelab" in lower:
                return "xcelium"
    return "unknown"


def _collect_user_files(file_info: dict[str, dict]) -> tuple[list[dict], int]:
    user = []
    filtered_count = 0
    for path in sorted(file_info):
        if _is_eda_lib(path):
            filtered_count += 1
            continue
        info = file_info[path]
        user.append({
            "path": path,
            "type": info.get("type", "unknown"),
            "category": _categorize(path),
        })
    return user, filtered_count


def parse_vcs_compile_log(log_path: str) -> dict:
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()

    command_text = "".join(lines[:40])
    incdirs = [
        _normalize_path(item, os.path.dirname(log_path))
        for item in re.findall(r"\+incdir\+([^\s\\]+)", command_text)
    ]
    filelist_tree: dict[str, list[str]] = {}
    for item in _FILELIST_RE.findall(command_text):
        path = _normalize_path(item, os.path.dirname(log_path))
        filelist_tree.setdefault(os.path.basename(path), [])

    include_tree: dict[str, list[str]] = {}
    file_info: dict[str, dict] = {}
    interfaces: set[str] = set()
    top_modules: list[str] = []
    stack: list[str] = []
    in_top_section = False

    for line in lines:
        if line.startswith("Top Level Modules:"):
            in_top_section = True
            continue
        if in_top_section:
            match = _VCS_TOP_RE.match(line)
            if match:
                top_modules.append(match.group(1))
                continue
            in_top_section = False

        match = _VCS_FILE_RE.search(line)
        if match:
            path = _normalize_path(match.group(1), os.path.dirname(log_path))
            stack = [path]
            file_info.setdefault(path, {"type": "module"})
            continue

        match = _VCS_INC_RE.search(line)
        if match and stack:
            parent = stack[-1]
            raw_child = match.group(1)
            child = _normalize_path(raw_child, os.path.dirname(parent))
            if not os.path.isabs(raw_child):
                for incdir in incdirs:
                    candidate = _normalize_path(raw_child, incdir)
                    if os.path.exists(candidate):
                        child = candidate
                        break
            include_tree.setdefault(parent, [])
            if child not in include_tree[parent]:
                include_tree[parent].append(child)
            file_info.setdefault(child, {"type": "unknown"})
            stack.append(child)
            continue

        match = _VCS_BACK_RE.search(line)
        if match:
            target = _normalize_path(match.group(1), os.path.dirname(log_path))
            while stack and stack[-1] != target:
                stack.pop()
            continue

        match = _VCS_IF_RE.search(line)
        if match:
            interfaces.add(match.group(1))
            continue

        _VCS_MODULE_RE.search(line)

    user, filtered_count = _collect_user_files(file_info)
    return {
        "simulator": "vcs",
        "top_modules": top_modules,
        "files": {
            "user": user,
            "filtered_count": filtered_count,
        },
        "include_tree": include_tree,
        "filelist_tree": filelist_tree,
        "interfaces": sorted(interfaces),
    }


def parse_xcelium_compile_log(log_path: str) -> dict:
    with open(log_path, "r", errors="replace") as f:
        lines = f.readlines()

    file_info: dict[str, dict] = {}
    include_tree: dict[str, list[str]] = {}
    filelist_tree: dict[str, list[str]] = {}
    interfaces: set[str] = set()
    top_modules: list[str] = []
    command_started = False
    current_file: str | None = None
    filelist_stack: list[tuple[int, str]] = []

    for line in lines:
        stripped = line.strip()
        if stripped == "xrun":
            command_started = True
        if not command_started:
            continue
        if _XCE_FILE_RE.match(line):
            break

        top_match = _TOP_RE.search(line)
        if top_match and top_match.group(1) not in top_modules:
            top_modules.append(top_match.group(1))

        if not stripped or stripped.startswith(("+define", "-incdir", "+incdir")):
            continue

        indent = len(line) - len(line.lstrip(" \t"))
        filelist_match = re.search(r"-f\s+(\S+)", stripped)
        if filelist_match:
            path = _normalize_path(filelist_match.group(1), os.path.dirname(log_path))
            name = os.path.basename(path)
            filelist_tree.setdefault(name, [])
            while filelist_stack and filelist_stack[-1][0] >= indent:
                filelist_stack.pop()
            if filelist_stack:
                parent_name = os.path.basename(filelist_stack[-1][1])
                filelist_tree.setdefault(parent_name, [])
                if name not in filelist_tree[parent_name]:
                    filelist_tree[parent_name].append(name)
            filelist_stack.append((indent, path))
            continue

        if stripped.startswith("-") or stripped.startswith("+"):
            continue

        if any(stripped.endswith(ext) for ext in (".sv", ".svh", ".v", ".vh")):
            path = _normalize_path(stripped, os.path.dirname(log_path))
            file_info.setdefault(path, {"type": "unknown"})

    for line in lines:
        match = _XCE_FILE_RE.match(line)
        if match:
            current_file = _normalize_path(match.group(1), os.path.dirname(log_path))
            file_info.setdefault(current_file, {"type": "unknown"})
            continue
        match = _XCE_ENTITY_RE.match(line)
        if match and current_file:
            entity_type, entity_name = match.group(1).lower(), match.group(2)
            file_info[current_file]["type"] = entity_type
            if entity_type == "interface":
                interfaces.add(entity_name)

    user, filtered_count = _collect_user_files(file_info)
    return {
        "simulator": "xcelium",
        "top_modules": top_modules,
        "files": {
            "user": user,
            "filtered_count": filtered_count,
        },
        "include_tree": include_tree,
        "filelist_tree": filelist_tree,
        "interfaces": sorted(interfaces),
    }


def parse_compile_log(log_path: str, simulator: str = "auto") -> dict:
    sim_type = simulator.lower()
    if sim_type == "auto":
        sim_type = detect_simulator(log_path)
    if sim_type == "vcs":
        return parse_vcs_compile_log(log_path)
    if sim_type == "xcelium":
        return parse_xcelium_compile_log(log_path)
    raise ValueError(f"Unable to determine simulator type from compile log: {log_path}")
