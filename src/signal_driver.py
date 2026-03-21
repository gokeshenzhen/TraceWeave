"""
signal_driver.py
轻量信号驱动定位：从层级路径回到最可能的 RTL 驱动位置。
"""

from __future__ import annotations

import os
import re
from typing import Any

from .compile_log_parser import parse_compile_log
from .tb_hierarchy_builder import scan_sv_file


_OUTPUT_DECL_RE = re.compile(r"\boutput\b[^;\n]*\b(?P<name>[A-Za-z_]\w*)\b", re.IGNORECASE)
_ASSIGN_RE_TEMPLATE = r"assign\s+{name}\s*=\s*(?P<expr>[^;]+);"
_ALWAYS_BLOCK_RE = re.compile(r"always(?:_comb|_ff)?(?:\s*@\s*\([^)]*\))?\s*begin(?P<body>.*?)end", re.IGNORECASE | re.DOTALL)
_ASSIGNMENT_RE_TEMPLATE = r"\b{name}\b\s*(?:<=|=)\s*(?P<expr>[^;]+);"
_INSTANCE_RE = re.compile(r"(?P<module>\w+)\s+(?P<inst>\w+)\s*\((?P<body>.*?)\);", re.DOTALL)
_PORT_CONN_RE = re.compile(r"\.(?P<port>\w+)\s*\(\s*(?P<expr>[^)]+)\)")


def explain_signal_driver(
    signal_path: str,
    wave_path: str,
    compile_log: str,
    top_hint: str | None = None,
) -> dict[str, Any]:
    compile_result = parse_compile_log(compile_log, "auto")
    file_entries = compile_result.get("files", {}).get("user", [])
    scans = [scan_sv_file(entry["path"]) for entry in file_entries if os.path.exists(entry["path"])]
    module_index = {
        module_name: scan
        for scan in scans
        for module_name in scan["modules"]
    }

    top_module = top_hint or (compile_result.get("top_modules") or [""])[0]
    resolved = _resolve_instance_module(signal_path, top_module, module_index)
    if resolved is None:
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": signal_path.split(".")[-1],
            "driver_status": "unsupported",
            "unsupported_reason": "complex_generate_or_unresolved_hierarchy",
        }

    rtl_name = signal_path.split(".")[-1]
    module_name, instance_path, scan = resolved

    exact = _find_local_driver(scan, rtl_name)
    if exact:
        exact.update(
            {
                "signal_path": signal_path,
                "wave_path": wave_path,
                "resolved_rtl_name": rtl_name,
                "resolved_module": module_name,
                "resolved_instance_path": instance_path,
            }
        )
        return exact

    port_hit = _find_output_port(scan, rtl_name)
    if port_hit:
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": rtl_name,
            "resolved_module": module_name,
            "resolved_instance_path": instance_path,
            "driver_status": "resolved",
            "driver_kind": "instance_port",
            "source_file": scan["path"],
            "source_line": port_hit["source_line"],
            "expression_summary": f"output {rtl_name} declared in module {module_name}",
            "upstream_signals": [],
            "confidence": "heuristic",
        }

    inst_ports = _find_instance_port_driver(scan, rtl_name)
    if inst_ports:
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": rtl_name,
            "resolved_module": module_name,
            "resolved_instance_path": instance_path,
            "driver_status": "resolved",
            "driver_kind": "instance_ports",
            "source_file": scan["path"],
            "source_line": inst_ports[0]["source_line"],
            "instance_port_connections": inst_ports,
            "expression_summary": f"{rtl_name} driven by {len(inst_ports)} instance port(s)",
            "upstream_signals": [f"{item['instance_name']}.{item['port_name']}" for item in inst_ports],
            "confidence": "heuristic",
        }

    return {
        "signal_path": signal_path,
        "wave_path": wave_path,
        "resolved_rtl_name": rtl_name,
        "resolved_module": module_name,
        "resolved_instance_path": instance_path,
        "driver_status": "partial",
        "driver_kind": "unknown",
        "source_file": scan["path"],
        "source_line": None,
        "expression_summary": f"signal {rtl_name} found under module {module_name}, but no simple driver matched",
        "upstream_signals": [],
        "confidence": "low",
    }


def _resolve_instance_module(
    signal_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
) -> tuple[str, str, dict[str, Any]] | None:
    parts = signal_path.split(".")
    if len(parts) < 2:
        return None
    start_idx = 0
    if top_module and parts[0] == top_module:
        current_module = top_module
        start_idx = 1
        current_path = parts[0]
    else:
        current_module = top_module or parts[0]
        current_path = parts[0]

    current_scan = module_index.get(current_module)
    if current_scan is None:
        return None

    for instance_name in parts[start_idx:-1]:
        next_module = None
        for item in current_scan["module_instances"]:
            if item["instance_name"] == instance_name:
                next_module = item["module_name"]
                break
        if next_module is None:
            return current_module, current_path, current_scan
        current_module = next_module
        current_scan = module_index.get(current_module)
        if current_scan is None:
            return None
        current_path = f"{current_path}.{instance_name}"
    return current_module, current_path, current_scan


def _find_local_driver(scan: dict[str, Any], signal_name: str) -> dict[str, Any] | None:
    source = scan["source_text"]

    assign_re = re.compile(_ASSIGN_RE_TEMPLATE.format(name=re.escape(signal_name)))
    assign_match = assign_re.search(source)
    if assign_match:
        return {
            "driver_status": "resolved",
            "driver_kind": "assign",
            "source_file": scan["path"],
            "source_line": _line_of_offset(source, assign_match.start()),
            "expression_summary": f"assign {signal_name} = {_compact_expr(assign_match.group('expr'))}",
            "upstream_signals": _extract_upstream_signals(assign_match.group("expr")),
            "confidence": "heuristic",
        }

    proc_re = re.compile(_ASSIGNMENT_RE_TEMPLATE.format(name=re.escape(signal_name)))
    for block in _ALWAYS_BLOCK_RE.finditer(source):
        match = proc_re.search(block.group("body"))
        if not match:
            continue
        block_text = source[block.start():block.start() + 32].lower()
        kind = "always_ff" if "always_ff" in block_text else "always_comb"
        return {
            "driver_status": "resolved",
            "driver_kind": kind,
            "source_file": scan["path"],
            "source_line": _line_of_offset(source, block.start()),
            "expression_summary": f"{kind} drives {signal_name} from {_compact_expr(match.group('expr'))}",
            "upstream_signals": _extract_upstream_signals(match.group("expr")),
            "confidence": "heuristic",
        }
    return None


def _find_output_port(scan: dict[str, Any], signal_name: str) -> dict[str, Any] | None:
    for match in _OUTPUT_DECL_RE.finditer(scan["source_text"]):
        if match.group("name") == signal_name:
            return {"source_line": _line_of_offset(scan["source_text"], match.start())}
    return None


def _find_instance_port_driver(scan: dict[str, Any], signal_name: str) -> list[dict[str, Any]] | None:
    results: list[dict[str, Any]] = []
    sig_re = re.compile(rf"^{re.escape(signal_name)}(?:\s*\[[^\]]*\])?$")
    for inst_match in _INSTANCE_RE.finditer(scan["source_text"]):
        body = inst_match.group("body")
        for port_match in _PORT_CONN_RE.finditer(body):
            expr = port_match.group("expr").strip()
            if not sig_re.match(expr):
                continue
            results.append(
                {
                    "instance_module": inst_match.group("module"),
                    "instance_name": inst_match.group("inst"),
                    "port_name": port_match.group("port"),
                    "connected_expression": expr,
                    "source_line": _line_of_offset(scan["source_text"], inst_match.start()),
                }
            )
    return results or None


def _line_of_offset(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1


def _compact_expr(expr: str) -> str:
    return " ".join(expr.split())[:160]


def _extract_upstream_signals(expr: str) -> list[str]:
    names: list[str] = []
    for token in re.findall(r"[A-Za-z_]\w*", expr):
        lower = token.lower()
        if lower in {"assign", "if", "else", "begin", "end"}:
            continue
        if token not in names:
            names.append(token)
    return names[:12]
