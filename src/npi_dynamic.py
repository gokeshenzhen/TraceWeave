"""Bounded NPI cell/pin single-step evidence; never a recursive fan-in cone.

Synthetic nets are expanded using NPI's is_generated/is_literal properties.
Real declared nets are observation boundaries, irrespective of their names.
"""
from __future__ import annotations

from dataclasses import asdict
import re

from .cancellation import check_cancelled, OperationCancelled
from .dynamic_evidence import DYNAMIC_VERSION, Expr, TRUE, expression_gaps, unsupported_step
from .dynamic_selection import select_expression

_MAX_STATES = 256
_MAX_DEPTH = 32
_MAX_PINS = 64
_SELECT = re.compile(r"(?:\[-?\d+(?::-?\d+)?\]#)?\[-?\d+(?::-?\d+)?\]$")


class ProjectionGap(Exception):
    pass


def query_step(backend, signal: str) -> dict:
    _, netlist = backend._npi_modules
    root = backend._resolve_net(netlist, signal)
    if root is None:
        return unsupported_step(signal, "verdi_npi", "signal_path_unresolved_in_npi")
    result = dict(version=DYNAMIC_VERSION, signal=signal, backend="verdi_npi", width=int(root.size()),
                  bits=[], boundary="combinational", complete=False, branches=[], clock=None,
                  gaps=[], sources=[], cross_check=None)
    visited = 0
    active = set()

    def bounded(items):
        if len(items) > _MAX_PINS:
            raise ProjectionGap("driver_set_incomplete")
        return items

    def reference(net):
        width = int(net.size())
        left, right = int(net.left()), int(net.right())
        bits = tuple(range(left, right + (-1 if left > right else 1), -1 if left > right else 1))
        base = _SELECT.sub("", net.full_name())
        declaration = netlist.get_net(base)
        if declaration is None:
            raise ProjectionGap("signal_declaration_unavailable")
        dl, dr = int(declaration.left()), int(declaration.right())
        declared = tuple(range(dl, dr + (-1 if dl > dr else 1), -1 if dl > dr else 1))
        return Expr("signal", width, signal=base, bits=bits, declared_bits=declared,
                    signed=bool(net.is_signed()))

    def drivers(net):
        original = bounded(net.driver_list() or [])
        # Preserve the normal tool's original-driver vs own-load alias guard.
        loads = [pin.full_name() for pin in bounded(net.load_list() or [])]
        pairs = [(d, backend._format_driver(d)) for d in original]
        formatted = [f for _, f in pairs if f is not None]
        if formatted:
            verdict = backend._loadcheck_head(formatted, loads)
            if verdict == "testbench":
                from .verdi_npi_backend import _testbench_verdict
                result["cross_check"] = _testbench_verdict(formatted[0])
                raise ProjectionGap("testbench_driven")
        from .verdi_npi_backend import _norm_raw
        own_loads = {_norm_raw(name) for name in loads}
        genuine = [d for d, f in pairs if f is not None and f.get("driver_kind") != "initial"
                   and _norm_raw(f.get("_npi_raw", "")) not in own_loads]
        # Preserve undecodable native handles as an explicit incompleteness gap.
        if any(f is None for _, f in pairs):
            raise ProjectionGap("driver_set_incomplete")
        return genuine

    def expand(net, depth=0, force=False):
        nonlocal visited
        check_cancelled()
        visited += 1
        if visited > _MAX_STATES or depth > _MAX_DEPTH:
            raise ProjectionGap("dynamic_expression_limit")
        width = int(net.size())
        if net.is_literal():
            return Expr("const", width, value=str(net.value()).lower(), signed=bool(net.is_signed()))
        if net.type() == "npiNlConcatNet":
            names = bounded(net.actual_name_list() or [])
            children = [netlist.get_net(n) for n in names]
            if not children or any(n is None for n in children):
                raise ProjectionGap("dynamic_bit_mapping_unavailable")
            return Expr("concat", width, tuple(expand(n, depth+1) for n in children))
        if not force and not net.is_generated():
            return reference(net)
        key = net.full_name()
        if key in active:
            raise ProjectionGap("combinational_cycle")
        active.add(key)
        try:
            ds = drivers(net)
            if len(ds) != 1:
                raise ProjectionGap("multiple_driver_candidates" if ds else "dynamic_evidence_unavailable")
            pin = ds[0]
            scope = pin.scope_inst()
            if scope is None or scope.inst_type() != "npiNlRTLInst":
                if pin.type() in {"npiNlPort", "npiNlPseudoPort"} and pin.direction() == "npiNlInput" and scope and scope.full_name() == backend._loaded_top:
                    if depth == 0:
                        result["boundary"] = "input"
                    return reference(net)
                other = pin.connected_pin()
                linked = other.connected_net() if other is not None else None
                if linked is None or linked.full_name() == key:
                    raise ProjectionGap("dynamic_evidence_unavailable")
                return expand(linked, depth+1)
            cell_type = scope.cell_type()
            pins = bounded(scope.instport_list() or [])
            outputs = [p.connected_net() for p in pins if p.direction() == "npiNlOutput"]
            outputs = [n for n in outputs if n is not None and
                       _SELECT.sub('', n.full_name()) == _SELECT.sub('', net.full_name())]
            if len(outputs) != 1:
                raise ProjectionGap('dynamic_bit_mapping_unavailable')
            output = outputs[0]
            if output is not None and (int(output.left()), int(output.right()), int(output.size())) != (
                    int(net.left()), int(net.right()), width):
                # A pseudo net may select only part of a generated cell output.
                # Expand the full cell first, then project exact native indices;
                # using the pseudo width as the mux's width selects wrong lanes.
                if _SELECT.sub('', output.full_name()) != _SELECT.sub('', net.full_name()):
                    raise ProjectionGap('dynamic_bit_mapping_unavailable')
                left, right = int(output.left()), int(output.right())
                declared = tuple(range(left, right + (-1 if left > right else 1), -1 if left > right else 1))
                left, right = int(net.left()), int(net.right())
                selected = tuple(range(left, right + (-1 if left > right else 1), -1 if left > right else 1))
                if len(declared) != int(output.size()) or len(selected) != width or not set(selected).issubset(declared):
                    raise ProjectionGap('dynamic_bit_mapping_unavailable')
                return select_expression(expand(output, depth + 1, force=True),
                                         tuple(declared.index(bit) for bit in selected))
            inputs = [p for p in pins if p.direction() == "npiNlInput"]
            result["sources"].append({"cell": scope.full_name(), "kind": cell_type, "location": scope.src_info()})
            def linked(p):
                n = p.connected_net()
                if n is None:
                    raise ProjectionGap("dynamic_bit_mapping_unavailable")
                return expand(n, depth+1)
            if cell_type == "npiNlFlipFlopCell":
                clocks = [p for p in inputs if p.port_type() == "npiNlClockPort"]
                data = [p for p in inputs if p.port_type() == "npiNlDataPort"]
                if depth != 0 or len(clocks) != 1 or len(data) != 1 or len(inputs) != 2:
                    raise ProjectionGap("temporal_context_unavailable")
                edge = {"npiNlRisingActive": "posedge", "npiNlFallingActive": "negedge"}.get(clocks[0].port_state())
                clock = linked(clocks[0])
                if edge is None or clock.op != "signal" or clock.width != 1:
                    raise ProjectionGap("temporal_context_unavailable")
                result.update(boundary="sequential", clock={"expression": asdict(clock), "edge": edge})
                return linked(data[0])
            if cell_type == "npiNlMuxCell":
                controls = [p for p in inputs if p.port_type() == "npiNlControlPort"]
                data = [p for p in inputs if p.port_type() == "npiNlDataPort"]
                if len(controls) != 1 or len(data) != 2 or len(inputs) != 3:
                    raise ProjectionGap("guard_unresolved")
                cond = linked(controls[0])
                annotated = {p.cond_annot(): p for p in data}
                if cond.width != 1 or set(annotated) != {"1'b1", "1'b0"}:
                    raise ProjectionGap("guard_unresolved")
                operands = []
                for annotation in ("1'b1", "1'b0"):
                    value = linked(annotated[annotation])
                    if value.width < width:
                        raise ProjectionGap('operand_type_unresolved')
                    # A directly assigned narrow output also truncates wider
                    # mux inputs. Keep only the contributing declared bits.
                    operands.append(select_expression(value, tuple(range(value.width - width, value.width))))
                return Expr("mux", width, (cond, *operands))
            op = {"npiNlLogAndCell": "and", "npiNlLogOrCell": "or", "npiNlNotCell": "not",
                  "npiNlEqCompCell": "eq", "npiNlNotEqCompCell": "ne"}.get(cell_type)
            operands = tuple(linked(p) for p in sorted(inputs, key=lambda p: p.port_order() or 0))
            if cell_type in {'npiNlLogAndCell', 'npiNlLogOrCell'}:
                ordered = sorted(inputs, key=lambda p: p.port_order() or 0)
                if any(p.port_state() not in {'npiNlHighActive', 'npiNlLowActive'} for p in ordered):
                    raise ProjectionGap('guard_unresolved')
                operands = tuple(Expr('not', 1, (value,)) if p.port_state() == 'npiNlLowActive' else value
                                 for p, value in zip(ordered, operands))
            if cell_type == 'npiNlShiftRightCell' and len(inputs) == 2:
                if {p.port_order() for p in inputs} != {0, 1}:
                    raise ProjectionGap('dynamic_bit_mapping_unavailable')
                data, amount = operands
                if data.width != width or amount.op != 'const' or any(c in amount.value for c in 'xz'):
                    raise ProjectionGap('dynamic_evidence_unavailable')
                shift = int(amount.value, 2)
                if not shift:
                    return data
                if shift >= width:
                    return Expr('const', width, value='0' * width)
                return Expr('concat', width, (Expr('const', shift, value='0' * shift),
                    select_expression(data, tuple(range(width - shift)))))
            if cell_type in {"npiNlBufCell", "npiNlAssignCell"} and len(operands) == 1 and operands[0].width == width:
                return operands[0]
            if op and (len(operands) == 2 or op == "not" and len(operands) == 1):
                if op == "not" and operands[0].width != 1:
                    raise ProjectionGap("dynamic_evidence_unavailable")
                return Expr(op, width, operands)
            return Expr("unsupported", width, operands, reason="dynamic_evidence_unavailable")
        finally:
            active.remove(key)

    try:
        state = reference(root)
        result["bits"] = list(state.bits)
        result["state"] = asdict(state)
        value = expand(root, force=True)
        result["gaps"].extend(expression_gaps(value))
        if result["boundary"] != "input":
            result["branches"] = [dict(id="npi_step", guard=asdict(TRUE), value=asdict(value), order=0)]
        if backend.kdb_load_quality != "clean":
            result["gaps"].append("driver_set_incomplete")
        result["complete"] = not result["gaps"]
    except OperationCancelled:
        raise
    except ProjectionGap as exc:
        result["gaps"].append(str(exc))
    except Exception:
        result["gaps"].append("npi_dynamic_query_failed")
    result["traversal"] = dict(visited_state_count=visited, state_limit=_MAX_STATES,
                                search_exhaustive=result["complete"], incomplete_reasons=result["gaps"])
    return result
