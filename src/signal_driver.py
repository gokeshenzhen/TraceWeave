"""
signal_driver.py
轻量信号驱动定位：从层级路径回到最可能的 RTL 驱动位置。
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
from typing import Any

from .compile_log_parser import parse_compile_log
from .tb_hierarchy_builder import scan_sv_file


_PORT_DECL_RE = re.compile(r"\b(?P<dir>input|output)\b(?P<rest>[^;\n)]*)", re.IGNORECASE)
_IDENT_RE = re.compile(r"\b(?P<name>[A-Za-z_]\w*)\b")
_ASSIGN_RE_TEMPLATE = r"assign\s+{name}\s*=\s*(?P<expr>[^;]+);"
_ALWAYS_BLOCK_RE = re.compile(r"always(?:_comb|_ff)?(?:\s*@\s*\([^)]*\))?\s*begin(?P<body>.*?)end", re.IGNORECASE | re.DOTALL)
_ASSIGNMENT_RE_TEMPLATE = r"\b{name}\b\s*(?:<=|=)\s*(?P<expr>[^;]+);"
_INSTANCE_RE = re.compile(r"(?P<module>\w+)\s+(?P<inst>\w+)\s*\((?P<body>.*?)\);", re.DOTALL)
_PORT_CONN_RE = re.compile(r"\.(?P<port>\w+)\s*\(\s*(?P<expr>[^)]+)\)")
_SIGNAL_REF_RE = re.compile(r"(?P<ref>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)(?:\[[^\]]+\])*")
_UPSTREAM_FILTER_KEYWORDS = {
    "assign", "if", "else", "begin", "end", "case", "endcase",
    "reg", "wire", "logic", "signed", "unsigned", "input", "output",
    "posedge", "negedge", "always", "always_ff", "always_comb",
}


@dataclass
class _HierarchyContext:
    instance_path: str
    module_name: str
    parent_instance_path: str | None
    parent_module_name: str | None


@dataclass
class _TraceDecision:
    should_trace: bool
    next_signal_name: str | None = None
    branch_candidates: list[str] | None = None
    stop_reason: str | None = None
    summary_reason: str | None = None


def explain_signal_driver(
    signal_path: str,
    wave_path: str,
    compile_log: str,
    top_hint: str | None = None,
    recursive: bool = False,
    max_depth: int = 10,
    simulator: str = 'auto',
) -> dict[str, Any]:
    module_index, top_module = _build_module_index(compile_log, top_hint, simulator)
    if recursive:
        return _explain_recursive(signal_path, wave_path, top_module, module_index, max_depth)
    return _explain_single(signal_path, wave_path, top_module, module_index)


def _build_module_index(
    compile_log: str,
    top_hint: str | None = None,
    simulator: str = 'auto',
) -> tuple[dict[str, dict[str, Any]], str]:
    compile_result = parse_compile_log(compile_log, simulator)
    file_entries = compile_result.get("files", {}).get("user", [])
    scans = [scan_sv_file(entry["path"]) for entry in file_entries if os.path.exists(entry["path"])]
    module_index = {
        module_name: scan
        for scan in scans
        for module_name in scan["modules"]
    }
    top_module = top_hint or (compile_result.get("top_modules") or [""])[0]
    return module_index, top_module


def _explain_single(
    signal_path: str,
    wave_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    hop, _ = _resolve_single_hop(signal_path, top_module, module_index)
    result = {
        "signal_path": signal_path,
        "wave_path": wave_path,
        "resolved_rtl_name": signal_path.split(".")[-1],
        "recursive": False,
        "driver_chain": None,
        "chain_summary": None,
    }
    result.update(_strip_internal_fields(hop))
    result["stopped_at"] = _single_hop_stop_reason(result)
    return result


def _explain_recursive(
    signal_path: str,
    wave_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
    max_depth: int,
) -> dict[str, Any]:
    chain, final_stop = _trace_driver_chain(signal_path, top_module, module_index, max(0, max_depth))
    if not chain:
        base = _unsupported_result(signal_path)
        base.update(
            {
                "wave_path": wave_path,
                "recursive": True,
                "driver_chain": [],
                "chain_summary": None,
                "stopped_at": final_stop,
            }
        )
        return base

    head = chain[0]
    result = {
        "signal_path": signal_path,
        "wave_path": wave_path,
        "resolved_rtl_name": head.get("resolved_rtl_name", signal_path.split(".")[-1]),
        "resolved_module": head.get("resolved_module"),
        "resolved_instance_path": head.get("resolved_instance_path"),
        "driver_status": head.get("driver_status"),
        "driver_kind": head.get("driver_kind"),
        "source_file": head.get("source_file"),
        "source_line": head.get("source_line"),
        "expression_summary": head.get("expression_summary"),
        "upstream_signals": list(head.get("upstream_signals", [])),
        "instance_port_connections": head.get("instance_port_connections"),
        "confidence": head.get("confidence"),
        "unsupported_reason": head.get("unsupported_reason"),
        "stopped_at": final_stop,
        "recursive": True,
        "driver_chain": [_strip_chain_hop(hop) for hop in chain],
        "chain_summary": _build_chain_summary(chain),
    }
    return result


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


def _resolve_single_hop(
    signal_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], _HierarchyContext | None]:
    resolved = _resolve_instance_module(signal_path, top_module, module_index)
    if resolved is None:
        return _unsupported_result(signal_path), None

    rtl_name = signal_path.split(".")[-1]
    module_name, instance_path, scan = resolved
    ctx = _build_hierarchy_context(instance_path, top_module, module_index, module_name)

    exact = _find_local_driver(scan, rtl_name)
    if exact:
        exact.update(
            {
                "signal_path": signal_path,
                "resolved_rtl_name": rtl_name,
                "resolved_module": module_name,
                "resolved_instance_path": instance_path,
            }
        )
        return exact, ctx

    port_hit = _find_output_port(scan, rtl_name)
    if port_hit:
        return (
            {
                "signal_path": signal_path,
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
            },
            ctx,
        )

    inst_ports = _find_instance_port_driver(scan, rtl_name, module_index)
    if inst_ports:
        return (
            {
                "signal_path": signal_path,
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
            },
            ctx,
        )

    input_hit = _find_input_port(scan, rtl_name)
    if input_hit:
        return (
            {
                "signal_path": signal_path,
                "resolved_rtl_name": rtl_name,
                "resolved_module": module_name,
                "resolved_instance_path": instance_path,
                "driver_status": "partial",
                "driver_kind": "input_port",
                "source_file": scan["path"],
                "source_line": input_hit["source_line"],
                "expression_summary": f"input {rtl_name} declared in module {module_name}",
                "upstream_signals": [],
                "confidence": "heuristic",
            },
            ctx,
        )

    return (
        {
            "signal_path": signal_path,
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
        },
        ctx,
    )


def _build_hierarchy_context(
    instance_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
    module_name: str,
) -> _HierarchyContext:
    if instance_path == top_module or "." not in instance_path:
        return _HierarchyContext(
            instance_path=instance_path,
            module_name=module_name,
            parent_instance_path=None,
            parent_module_name=None,
        )
    parent_instance_path = instance_path.rsplit(".", 1)[0]
    parent_resolved = _resolve_instance_module(f"{parent_instance_path}.__parent__", top_module, module_index)
    parent_module_name = parent_resolved[0] if parent_resolved else None
    return _HierarchyContext(
        instance_path=instance_path,
        module_name=module_name,
        parent_instance_path=parent_instance_path,
        parent_module_name=parent_module_name,
    )


def _trace_driver_chain(
    signal_path: str,
    top_module: str,
    module_index: dict[str, dict[str, Any]],
    max_depth: int,
) -> tuple[list[dict[str, Any]], str | None]:
    chain: list[dict[str, Any]] = []
    visited: set[str] = set()
    current_signal = signal_path
    final_stop: str | None = None

    for depth in range(max_depth + 1):
        visited.add(current_signal)

        hop, ctx = _resolve_single_hop(current_signal, top_module, module_index)
        hop["depth"] = depth
        hop["signal_path"] = current_signal
        if ctx is not None:
            hop["_hierarchy_context"] = ctx

        kind = hop.get("driver_kind")
        status = hop.get("driver_status")

        if depth >= max_depth:
            hop["stopped_at"] = "max_depth"
            chain.append(hop)
            return chain, "max_depth"

        if kind == "input_port":
            if ctx is not None and ctx.parent_module_name is None:
                hop["stopped_at"] = "primary_input"
                chain.append(hop)
                return chain, "primary_input"

            traversal = _traverse_upward(hop["resolved_rtl_name"], ctx, module_index)
            if traversal is not None:
                parent_signal, parent_ctx = traversal
                parent_name = _signal_name_from_expr(parent_signal.split(".")[-1]) or parent_signal.split(".")[-1]
                hop["expression_summary"] = (
                    f"input {hop['resolved_rtl_name']} <- connected to {parent_name} in parent "
                    f"{parent_ctx.module_name}"
                )
                hop["upstream_signals"] = [parent_name]
                hop["_hierarchy_context"] = ctx
                chain.append(hop)
                current_signal = parent_signal
                continue

            hop["stopped_at"] = "port_boundary"
            chain.append(hop)
            return chain, "port_boundary"

        if status in ("unsupported", "partial"):
            hop["stopped_at"] = "unresolved"
            chain.append(hop)
            return chain, "unresolved"

        if kind == "always_ff":
            hop["stopped_at"] = "register_boundary"
            chain.append(hop)
            return chain, "register_boundary"

        if kind in ("instance_ports", "instance_port"):
            hop["stopped_at"] = "port_boundary"
            chain.append(hop)
            return chain, "port_boundary"

        chain.append(hop)
        decision = _decide_next_upstream(hop)
        if decision.branch_candidates is not None:
            chain[-1]["branch_candidates"] = decision.branch_candidates
        if not decision.should_trace:
            chain[-1]["stopped_at"] = decision.stop_reason or "unresolved"
            if decision.summary_reason:
                chain[-1]["expression_summary"] = _annotate_summary(
                    chain[-1].get("expression_summary"),
                    decision.summary_reason,
                )
            return chain, chain[-1]["stopped_at"]
        next_signal = f"{hop['resolved_instance_path']}.{decision.next_signal_name}"
        if next_signal in visited:
            chain[-1]["stopped_at"] = "cycle_detected"
            chain[-1]["expression_summary"] = _annotate_summary(
                chain[-1].get("expression_summary"),
                "cycle_detected",
            )
            return chain, "cycle_detected"
        current_signal = next_signal

    if chain:
        chain[-1]["stopped_at"] = chain[-1].get("stopped_at") or final_stop or "max_depth"
        return chain, chain[-1]["stopped_at"]
    return chain, final_stop


def _traverse_upward(
    signal_name: str,
    ctx: _HierarchyContext | None,
    module_index: dict[str, dict[str, Any]],
) -> tuple[str, _HierarchyContext] | None:
    if ctx is None or ctx.parent_module_name is None or ctx.parent_instance_path is None:
        return None

    parent_scan = module_index.get(ctx.parent_module_name)
    if parent_scan is None:
        return None

    instance_name = ctx.instance_path.split(".")[-1]
    for inst_match in _INSTANCE_RE.finditer(parent_scan["source_text"]):
        if inst_match.group("inst") != instance_name:
            continue
        for port_match in _PORT_CONN_RE.finditer(inst_match.group("body")):
            if port_match.group("port") != signal_name:
                continue
            upstream_names = _extract_upstream_signals(port_match.group("expr"))
            if not upstream_names:
                return None
            parent_signal_name = upstream_names[0].split(".")[-1]
            parent_signal_path = f"{ctx.parent_instance_path}.{parent_signal_name}"
            parent_parent_path = (
                ctx.parent_instance_path.rsplit(".", 1)[0]
                if "." in ctx.parent_instance_path
                else None
            )
            parent_parent_module = None
            if parent_parent_path is not None:
                parent_parent_resolved = _resolve_instance_module(
                    f"{parent_parent_path}.__parent__", ctx.parent_instance_path.split(".")[0], module_index
                )
                if parent_parent_resolved is not None:
                    parent_parent_module = parent_parent_resolved[0]
            return (
                parent_signal_path,
                _HierarchyContext(
                    instance_path=ctx.parent_instance_path,
                    module_name=ctx.parent_module_name,
                    parent_instance_path=parent_parent_path,
                    parent_module_name=parent_parent_module,
                ),
            )
    return None


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
            "_trace_expr": assign_match.group("expr"),
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
            "_trace_expr": match.group("expr"),
            "confidence": "heuristic",
        }
    return None


def _find_output_port(scan: dict[str, Any], signal_name: str) -> dict[str, Any] | None:
    for name, source_line in _find_port_names(scan["source_text"], "output").items():
        if name == signal_name:
            return {"source_line": source_line}
    return None


def _find_input_port(scan: dict[str, Any], signal_name: str) -> dict[str, Any] | None:
    for name, source_line in _find_port_names(scan["source_text"], "input").items():
        if name == signal_name:
            return {"source_line": source_line}
    return None


def _find_port_names(source_text: str, direction: str) -> dict[str, int]:
    result: dict[str, int] = {}
    for match in _PORT_DECL_RE.finditer(source_text):
        if match.group("dir").lower() != direction:
            continue
        line = _line_of_offset(source_text, match.start())
        for ident in _IDENT_RE.finditer(match.group("rest")):
            name = ident.group("name")
            if name.lower() in _UPSTREAM_FILTER_KEYWORDS:
                continue
            result.setdefault(name, line)
    return result


def _find_instance_port_driver(
    scan: dict[str, Any],
    signal_name: str,
    module_index: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]] | None:
    results: list[dict[str, Any]] = []
    sig_re = re.compile(rf"^{re.escape(signal_name)}(?:\s*\[[^\]]*\])?$")
    for inst_match in _INSTANCE_RE.finditer(scan["source_text"]):
        child_scan = module_index.get(inst_match.group("module")) if module_index else None
        body = inst_match.group("body")
        for port_match in _PORT_CONN_RE.finditer(body):
            expr = port_match.group("expr").strip()
            if not sig_re.match(expr):
                continue
            if child_scan is not None and _find_output_port(child_scan, port_match.group("port")) is None:
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
    for match in _SIGNAL_REF_RE.finditer(expr):
        token = match.group("ref")
        lower = token.lower()
        if lower in _UPSTREAM_FILTER_KEYWORDS:
            continue
        base = token.split(".")[0].lower()
        if base in _UPSTREAM_FILTER_KEYWORDS:
            continue
        if token not in names:
            names.append(token)
    return names[:12]


def _decide_next_upstream(hop: dict[str, Any]) -> _TraceDecision:
    expr = hop.get("_trace_expr")
    if not expr:
        return _TraceDecision(should_trace=False, stop_reason="unresolved")

    refs = _extract_upstream_signals(expr)
    if not refs:
        return _TraceDecision(should_trace=False, stop_reason="unresolved")

    hierarchical_refs = [ref for ref in refs if "." in ref]
    local_refs = [ref for ref in refs if "." not in ref]

    if hierarchical_refs:
        reason = "hierarchical_rhs_not_traced" if len(refs) == 1 else "ambiguous_rhs_not_traced"
        return _TraceDecision(
            should_trace=False,
            stop_reason="trace_boundary",
            summary_reason=reason,
        )

    if len(local_refs) == 1:
        return _TraceDecision(should_trace=True, next_signal_name=local_refs[0])

    return _TraceDecision(
        should_trace=False,
        branch_candidates=local_refs,
        stop_reason="trace_boundary",
        summary_reason="ambiguous_rhs_not_traced",
    )


def _build_chain_summary(chain: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for hop in chain:
        sig = hop.get("resolved_rtl_name", hop["signal_path"].split(".")[-1])
        marker = hop.get("stopped_at") or hop.get("driver_kind") or "unknown"
        parts.append(f"{sig} ->[{marker}]")
    return " ".join(parts)


def _strip_internal_fields(hop: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in hop.items() if not key.startswith("_")}


def _strip_chain_hop(hop: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        "depth",
        "signal_path",
        "resolved_module",
        "resolved_instance_path",
        "driver_kind",
        "source_file",
        "source_line",
        "expression_summary",
        "upstream_signals",
        "instance_port_connections",
        "branch_candidates",
        "stopped_at",
    }
    return {key: value for key, value in hop.items() if key in allowed}


def _single_hop_stop_reason(result: dict[str, Any]) -> str | None:
    kind = result.get("driver_kind")
    status = result.get("driver_status")
    if kind in ("instance_ports", "instance_port"):
        return "port_boundary"
    if kind == "always_ff":
        return "register_boundary"
    if kind == "input_port":
        return "primary_input" if _is_top_level_instance(result.get("resolved_instance_path")) else "port_boundary"
    if status in ("unsupported", "partial"):
        return "unresolved"
    return None


def _is_top_level_instance(instance_path: str | None) -> bool:
    if not instance_path:
        return False
    return "." not in instance_path


def _unsupported_result(signal_path: str) -> dict[str, Any]:
    return {
        "signal_path": signal_path,
        "resolved_rtl_name": signal_path.split(".")[-1],
        "driver_status": "unsupported",
        "driver_kind": None,
        "unsupported_reason": "complex_generate_or_unresolved_hierarchy",
        "upstream_signals": [],
        "confidence": None,
        "resolved_module": None,
        "resolved_instance_path": None,
        "source_file": None,
        "source_line": None,
        "expression_summary": None,
        "instance_port_connections": None,
    }


def _signal_name_from_expr(expr: str) -> str | None:
    names = _extract_upstream_signals(expr)
    return names[0].split(".")[-1] if names else None


def _annotate_summary(summary: str | None, reason: str) -> str:
    if summary:
        return f"{summary}; trace stopped: {reason}"
    return f"trace stopped: {reason}"
