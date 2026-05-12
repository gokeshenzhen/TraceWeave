"""
signal_load.py
Static shallow load/fanout finder. Mirrors signal_driver.py but in the
opposite direction: given a hierarchical signal path, list places that
consume it within the resolving module.

Static scope (shallow_only):
- module_input        : signal connects to an input port of a child instance
- rhs_expr            : signal appears in RHS of an assign / procedural
                        assignment inside the same module
- always_sensitivity  : signal appears in an always block sensitivity list

Out of scope for static path (returned as stopped_at when applicable):
- cross-hierarchy fanout (signal is an output port — loads live in the
  parent's scope, requires NPI/Verdi backend)
- generate / interface modport / bind injected logic
"""

from __future__ import annotations

import os
import re
from typing import Any

from .compile_log_parser import parse_compile_log
from .signal_driver import (
    _ALWAYS_BLOCK_RE,
    _INSTANCE_RE,
    _PORT_CONN_RE,
    _UPSTREAM_FILTER_KEYWORDS,
    _build_module_index,
    _compact_expr,
    _find_input_port,
    _find_output_port,
    _line_of_offset,
    _resolve_instance_module,
)
from .tb_hierarchy_builder import scan_sv_file


_ALWAYS_HEADER_RE = re.compile(
    r"always(?P<kind>_comb|_ff|_latch)?\s*(?:@\s*\((?P<sens>[^)]*)\))?\s*begin",
    re.IGNORECASE,
)
_ASSIGN_STMT_RE = re.compile(r"assign\s+(?P<lhs>[\w\.\[\]:]+)\s*=\s*(?P<rhs>[^;]+);")
_PROC_ASSIGN_RE = re.compile(
    r"\b(?P<lhs>[\w]+(?:\.[\w]+)*(?:\[[^\]]*\])?)\s*(?P<op><=|=)\s*(?P<rhs>[^;]+);"
)


def find_signal_loads(
    signal_path: str,
    compile_log: str,
    top_hint: str | None = None,
    max_depth: int = 1,
    include_expr: bool = True,
    kind_filter: list[str] | None = None,
    simulator: str = "auto",
) -> dict[str, Any]:
    """Return a shallow_only list of loads consuming ``signal_path``.

    See module docstring for scope. ``max_depth`` is reserved for future
    transitive walks; the static implementation always behaves as
    depth=1.
    """
    if max_depth != 1:
        # Reserved; static path is shallow only.
        max_depth = 1

    module_index, top_module = _build_module_index(
        compile_log, top_hint, simulator, signal_path=signal_path,
    )
    resolved = _resolve_instance_module(signal_path, top_module, module_index)
    rtl_name = signal_path.split(".")[-1]
    base = {
        "signal_path": signal_path,
        "resolved_rtl_name": rtl_name,
        "resolved_module": None,
        "resolved_instance_path": None,
        "loads": [],
        "completeness": "shallow_only",
        "stopped_at": None,
        "unsupported_reason": None,
    }

    if resolved is None:
        base["stopped_at"] = "signal_path_unresolved"
        base["unsupported_reason"] = (
            f"Could not resolve {signal_path!r} against compile log hierarchy."
        )
        return base

    module_name, instance_path, scan = resolved
    base["resolved_module"] = module_name
    base["resolved_instance_path"] = instance_path

    # If the signal is an output port of this module, the loads live in
    # the parent's scope. Static path cannot walk upward reliably.
    if _find_output_port(scan, rtl_name) is not None:
        base["stopped_at"] = "output_port_loads_in_parent_scope"
        base["unsupported_reason"] = (
            f"{rtl_name!r} is an output port of {module_name}; static backend "
            f"does not trace cross-hierarchy fanout."
        )
        return base

    keep_kinds = set(kind_filter) if kind_filter else None
    raw_loads: list[dict[str, Any]] = []
    if keep_kinds is None or "module_input" in keep_kinds:
        raw_loads.extend(
            _find_instance_input_loads(scan, rtl_name, instance_path, module_index, include_expr)
        )
    if keep_kinds is None or "rhs_expr" in keep_kinds:
        raw_loads.extend(_find_local_rhs_loads(scan, rtl_name, instance_path, include_expr))
    if keep_kinds is None or "always_sensitivity" in keep_kinds:
        raw_loads.extend(_find_sensitivity_loads(scan, rtl_name, instance_path, include_expr))

    base["loads"] = _dedup_loads(raw_loads)
    if not base["loads"]:
        # Either signal truly has no static loads, or static path is blind
        # (interface / generate / bind). The dispatch layer surfaces
        # backend_status so the caller can see whether NPI was even
        # active; when it was Static-only, retry with a Verdi KDB
        # (run build_kdb for Xcelium flows) to engage NPI fan-out.
        base["stopped_at"] = "no_static_load_found"
    return base


# ---------------------------------------------------------------------------
# Internal scanners
# ---------------------------------------------------------------------------


def _find_instance_input_loads(
    scan: dict[str, Any],
    signal_name: str,
    instance_path: str,
    module_index: dict[str, dict[str, Any]],
    include_expr: bool,
) -> list[dict[str, Any]]:
    """Signal feeds a child instance's input port within this module."""
    results: list[dict[str, Any]] = []
    sig_re = re.compile(rf"^{re.escape(signal_name)}(?:\s*\[[^\]]*\])?$")
    source = scan["source_text"]
    for inst_match in _INSTANCE_RE.finditer(source):
        child_module = inst_match.group("module")
        if child_module.lower() in _UPSTREAM_FILTER_KEYWORDS:
            continue
        child_scan = module_index.get(child_module)
        body = inst_match.group("body")
        for port_match in _PORT_CONN_RE.finditer(body):
            expr = port_match.group("expr").strip()
            if not sig_re.match(expr):
                continue
            port_name = port_match.group("port")
            # Only count it as a load if the connected port is actually
            # an input (or unknown direction in absence of child_scan —
            # mark approximate but include).
            if child_scan is not None:
                if _find_input_port(child_scan, port_name) is None:
                    continue
            inst_name = inst_match.group("inst")
            results.append(
                {
                    "load_path": f"{instance_path}.{inst_name}.{port_name}",
                    "kind": "module_input",
                    "expr": f".{port_name}({expr})" if include_expr else None,
                    "source_file": scan["path"],
                    "source_line": _line_of_offset(source, inst_match.start()),
                    "source_info_origin": "compile_log",
                    "backend": "static",
                    "confidence": "approximate",
                }
            )
    return results


def _find_local_rhs_loads(
    scan: dict[str, Any],
    signal_name: str,
    instance_path: str,
    include_expr: bool,
) -> list[dict[str, Any]]:
    """Signal appears in RHS of an assign or procedural assignment."""
    results: list[dict[str, Any]] = []
    source = scan["source_text"]
    sig_word_re = re.compile(rf"\b{re.escape(signal_name)}\b")

    # Continuous assigns
    for m in _ASSIGN_STMT_RE.finditer(source):
        rhs = m.group("rhs")
        if not sig_word_re.search(rhs):
            continue
        lhs = m.group("lhs").strip()
        if sig_word_re.fullmatch(lhs):
            # Self-assignment (`assign a = a`); skip.
            continue
        results.append(
            {
                "load_path": f"{instance_path}.{lhs}",
                "kind": "rhs_expr",
                "expr": f"assign {lhs} = {_compact_expr(rhs)}" if include_expr else None,
                "source_file": scan["path"],
                "source_line": _line_of_offset(source, m.start()),
                "source_info_origin": "compile_log",
                "backend": "static",
                "confidence": "approximate",
            }
        )

    # Procedural assignments inside always blocks
    for block in _ALWAYS_BLOCK_RE.finditer(source):
        body = block.group("body")
        block_offset = block.start()
        for stmt in _PROC_ASSIGN_RE.finditer(body):
            rhs = stmt.group("rhs")
            if not sig_word_re.search(rhs):
                continue
            lhs = stmt.group("lhs").strip()
            if sig_word_re.fullmatch(lhs):
                continue
            op = stmt.group("op")
            line = _line_of_offset(source, block_offset + stmt.start())
            results.append(
                {
                    "load_path": f"{instance_path}.{lhs}",
                    "kind": "rhs_expr",
                    "expr": f"{lhs} {op} {_compact_expr(rhs)}" if include_expr else None,
                    "source_file": scan["path"],
                    "source_line": line,
                    "source_info_origin": "compile_log",
                    "backend": "static",
                    "confidence": "approximate",
                }
            )
    return results


def _find_sensitivity_loads(
    scan: dict[str, Any],
    signal_name: str,
    instance_path: str,
    include_expr: bool,
) -> list[dict[str, Any]]:
    """Signal appears in an always block @(...) sensitivity list."""
    results: list[dict[str, Any]] = []
    source = scan["source_text"]
    sig_word_re = re.compile(rf"\b{re.escape(signal_name)}\b")
    for header in _ALWAYS_HEADER_RE.finditer(source):
        sens = header.group("sens") or ""
        if not sens:
            continue
        if not sig_word_re.search(sens):
            continue
        line = _line_of_offset(source, header.start())
        results.append(
            {
                "load_path": f"{instance_path}:always@line{line}",
                "kind": "always_sensitivity",
                "expr": f"always @({_compact_expr(sens)})" if include_expr else None,
                "source_file": scan["path"],
                "source_line": line,
                "source_info_origin": "compile_log",
                "backend": "static",
                "confidence": "approximate",
            }
        )
    return results


def _dedup_loads(loads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[tuple[str, str, str | None, int | None]] = set()
    out: list[dict[str, Any]] = []
    for entry in loads:
        key = (
            entry["load_path"],
            entry["kind"],
            entry.get("source_file"),
            entry.get("source_line"),
        )
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out
