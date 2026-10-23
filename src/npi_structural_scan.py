"""Bounded input-port observations from an exclusive NPI Language owner.

Verified against Verdi V-2023.12-SP2: Port/HighConn/LowConn, PortInstFile/
LineNo, InternalScope and BinStrVal. A missing HighConn alone is NOT open.
Default-port semantics and contextual width conversion remain explicit gaps.
No source reading, driver traversal, waveform access or design building occurs.
"""
from __future__ import annotations

from collections import Counter
import json
import re
import time

from .hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget
from .npi_hierarchy_source import _LanguageIterator, NpiHierarchyUnavailable
from .structural_semantic_router import NPI_PORT_CATEGORIES
from .structural_gap_diagnostics import GapDiagnostics
from .structural_native_values import vector, value as abstract_value

CAPABILITY_VERSION = "npi_input_ports_v3"
_DYNAMIC = {"npiNet", "npiReg", "npiLogicVar", "npiIntVar", "npiIntegerVar", "npiBitVar", "npiStructVar", "npiStructNet", "npiPackedArrayVar", "npiPackedArrayNet"}
_CONSTANTS = {'npiConstant', 'npiParameter', 'npiSpecParam', 'npiEnumConst', 'npiPackedArrayParameter'}
_SELECTS = {'npiBitSelect', 'npiRegBit', 'npiNetBit', 'npiParameterBit', 'npiPartSelect'}
_SCOPES = {"npiModule", "npiInterface", "npiProgram", "npiGenScope"}
# InternalScope also exposes procedural statement scopes. They cannot contain
# elaborated module input bindings; generate scopes have their own npiGenScope
# kind and must still be traversed.
_IGNORE_SCOPES = {"npiTask", "npiFunction", "npiNamedBegin", "npiNamedFork", "npiClassDefn",
                  "npiFor", "npiBegin", "npiFork"}


class _UnsupportedPort(Exception):
    def __init__(self, reason, **details):
        self.reason = reason
        self.details = details
        super().__init__(reason)


def scan_budget(options, *, deadline=None):
    limits = options['limits']
    budget = HierarchyQueryBudget(timeout_sec=options['timeout_sec'],
                                  max_scopes=limits['max_instances'], max_calls=100000,
                                  max_symbols=100000, max_bytes=8 * 1024 * 1024, max_depth=128)
    if deadline is not None:
        budget.deadline = min(budget.deadline, deadline)
    return budget


def scan_native_ports(provider, options):
    p, budget = provider, provider.budget
    categories = set(options['categories']) & NPI_PORT_CATEGORIES
    gaps = {c: set() for c in categories}
    facts, seen = [], set()
    started = time.monotonic()
    ports = 0
    diagnostics = GapDiagnostics()

    def gap(reason, affected=None, **details):
        for c in categories if affected is None else categories & set(affected):
            gaps[c].add(reason)
        diagnostics.add(reason, details)

    def width(handle):
        value = p.scalar(handle, 'size')
        if type(value) is not int or value < 1:
            raise _UnsupportedPort('npi_width_unavailable', expression_kind=p.scalar(handle, 'type'), actual_width=value)
        if value > options['limits']['max_width']:
            raise _UnsupportedPort('native_width_limit')
        return value

    def binary(handle, size):
        p.check(); budget.call()
        value = p.low.get_value(handle.cps_obj, p.low.BinStrVal, False)
        budget.checkpoint()
        if value is None:
            return None
        # get_str_val copies a scalar from the value capsule while still owned.
        budget.call(); bits = value.get_str_val(); budget.checkpoint()
        if not isinstance(bits, str) or len(bits) != size or set(bits.lower()) - set('01xz'):
            return None
        return bits.lower()

    def index_value(handle, depth):
        if handle is None:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        size, rows, complete = parts(handle, depth + 1)
        if not complete or len(rows) != 1 or set(rows[0]['bits']) - set('01'):
            raise _UnsupportedPort('npi_selection_range_unavailable')
        signed = signedness(handle, depth)
        bits = rows[0]['bits']
        value = int(bits, 2)
        return value - (1 << size) if signed == 1 and bits[0] == '1' else value

    def packed_range(handle, size, depth):
        ts = p.one(handle, 'typespec_handle')
        if ts is None:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        kind = p.scalar(ts, 'type')
        # The installed SDK exposes packed dimensions under the typespec, not
        # LeftRange/RightRange directly on a Reg/Parameter. Reject aggregate and
        # multidimensional types rather than guessing their flattened order.
        if kind in {'npiIntTypespec', 'npiIntegerTypespec'} and size == 32:
            return 31, 0, 1
        if kind not in {'npiLogicTypespec', 'npiBitTypespec', 'npiPackedArrayTypespec'}:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        it = _LanguageIterator(p, p.low.Range, ts)
        try:
            r = it.read()
            if r is None or it.read() is not None:
                raise _UnsupportedPort('npi_selection_range_unavailable')
            left = index_value(p.one(r, 'left_range_handle'), depth)
            right = index_value(p.one(r, 'right_range_handle'), depth)
        finally:
            p.close_iterator(it)
        count = abs(left - right) + 1
        if size % count or kind != 'npiPackedArrayTypespec' and count != size:
            raise _UnsupportedPort('npi_selection_range_unavailable')
        return left, right, size // count

    def selection(handle, kind, depth):
        parent = p.one(handle, 'parent_handle')
        parent_kind = p.scalar(parent, 'type') if parent is not None else None
        if parent_kind not in _DYNAMIC | _CONSTANTS:
            raise _UnsupportedPort('npi_selection_parent_unmodeled', expression_kind=kind, parent_kind=parent_kind)
        parent_size, rows, complete = parts(parent, depth + 1)
        decl_left, decl_right, stride = packed_range(parent, parent_size, depth)
        if kind == 'npiPartSelect':
            size = width(handle)
            left = index_value(p.one(handle, 'left_range_handle'), depth)
            right = index_value(p.one(handle, 'right_range_handle'), depth)
            if ((abs(left - right) + 1) * stride != size
                    or left != right and (left > right) != (decl_left > decl_right)):
                raise _UnsupportedPort('npi_selection_range_unavailable')
        else:
            size = stride
            if kind != 'npiBitSelect' and width(handle) != size:
                raise _UnsupportedPort('npi_width_unavailable')
            index = p.one(handle, 'index_handle')
            if index is not None:
                left = right = index_value(index, depth)
            else:
                # Fixed bit objects in V-2023.12-SP2 have no Index relationship.
                # Accept only a canonical decimal suffix of the EXACT parent's
                # native identity, never decompile/parse an expression or name
                # a new object by a guessed path. Variable indexes remain gaps.
                path, parent_path = p.scalar(handle, 'full_name'), p.scalar(parent, 'full_name')
                suffix = path[len(parent_path):] if (isinstance(path, str)
                         and isinstance(parent_path, str) and parent_path and path.startswith(parent_path)) else ''
                if (len(suffix) <= 32 and re.fullmatch(r'\[-?(?:0|[1-9][0-9]*)\]', suffix)
                        and (kind != 'npiBitSelect' or p.scalar(handle, 'constant_select') == 1)):
                    left = right = int(suffix[1:-1])
                else:
                    # Elaborated package/genvar selections retain symbolic
                    # spellings in this SDK. Native identity, not string
                    # evaluation, proves the selected declared bit.
                    matches = []
                    if (stride == 1 and parent_size <= 256
                            and callable(getattr(p.low, 'handle_by_index', None))
                            and callable(getattr(p.low, 'compare_objects', None))):
                        for index in range(min(decl_left, decl_right), max(decl_left, decl_right) + 1):
                            p.check(); budget.call()
                            bit = p.wrap(p.low.handle_by_index(parent.cps_obj, index))
                            budget.checkpoint()
                            if bit is not None:
                                budget.call()
                                same = p.low.compare_objects(handle.cps_obj, bit.cps_obj)
                                budget.checkpoint()
                                if type(same) is not int or same not in (0, 1):
                                    raise _UnsupportedPort('npi_selection_identity_unavailable')
                                if same == 1:
                                    matches.append(index)
                    if len(matches) != 1:
                        raise _UnsupportedPort('npi_selection_index_unavailable', expression_kind=kind, parent_kind=parent_kind)
                    left = right = matches[0]
        if not (min(decl_left, decl_right) <= min(left, right)
                <= max(left, right) <= max(decl_left, decl_right)):
            raise _UnsupportedPort('npi_selection_out_of_range')
        offset = abs(right - decl_right) * stride
        selected = []
        for row in rows:
            lo = max(offset, row['lsb_offset'])
            hi = min(offset + size, row['lsb_offset'] + row['width'])
            if lo < hi:
                end = row['width'] - (lo - row['lsb_offset'])
                selected.append({'lsb_offset': lo - offset, 'width': hi - lo,
                                 'bits': row['bits'][end - (hi - lo):end]})
        return size, selected, complete

    def signedness(handle, depth=0):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        kind = p.scalar(handle, 'type')
        if kind in {'npiRefObj', 'npiExtRef'}:
            actual = p.one(handle, 'actual_handle')
            if actual is None:
                raise _UnsupportedPort('npi_reference_unresolved')
            return signedness(actual, depth + 1)
        if kind == 'npiOperation' and p.scalar(handle, 'op_type') == 'npiExprCastOp':
            it = _LanguageIterator(p, p.low.Operand, handle)
            try:
                size_expr, operand, extra = it.read(), it.read(), it.read()
                if size_expr is None or operand is None or extra is not None:
                    raise _UnsupportedPort('npi_signedness_unavailable', expression_kind=kind)
                # A sized cast retains the data operand's signedness; SDK
                # metadata can instead describe its width-expression operand.
                return signedness(operand, depth + 1)
            finally:
                p.close_iterator(it)
        signed = p.scalar(handle, 'signed')
        if signed in (0, 1):
            return signed
        ts = p.one(handle, 'typespec_handle')
        signed = p.scalar(ts, 'signed') if ts is not None else None
        if signed not in (0, 1):
            raise _UnsupportedPort('npi_signedness_unavailable', expression_kind=kind)
        return signed

    def resize(handle, data, target):
        size, rows, _ = data
        bits = vector(size, rows)
        if target == size:
            return data
        if target < size:
            return abstract_value(bits[-target:])
        resolved = handle
        for _ in range(65):
            kind = p.scalar(resolved, 'type')
            if kind not in {'npiRefObj', 'npiExtRef'}:
                break
            resolved = p.one(resolved, 'actual_handle')
            if resolved is None:
                raise _UnsupportedPort('npi_reference_unresolved')
        else:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        signed = signedness(resolved)
        # This SDK does not distinguish an unbased unsized '1/'x/'z from a
        # one-bit unsigned literal. Their context extension differs. Keep that
        # case unresolved; '0 and 1'b0 have identical extension semantics.
        if (size == 1 and signed == 0 and kind == 'npiConstant'
                and bits != '0'):
            raise _UnsupportedPort('npi_contextual_width_conversion_unverified', actual_width=size, port_width=target)
        return abstract_value((bits[0] if signed else '0') * (target-size) + bits)

    def operation(handle, depth):
        op = p.scalar(handle, 'op_type')
        it = _LanguageIterator(p, p.low.Operand, handle)
        handles = []
        try:
            for _ in range(257):
                item = it.read()
                if item is None:
                    break
                if len(handles) == 256:
                    raise HierarchyBudgetExceeded('native_operand_limit')
                handles.append(item)
        finally:
            p.close_iterator(it)
        operands = [parts(item, depth + 1) for item in handles]
        if op == 'npiExprCastOp' and len(handles) == 2:
            size = index_value(handles[0], depth)
            if not 1 <= size <= options['limits']['max_width']:
                raise _UnsupportedPort('native_width_limit')
            return resize(handles[1], operands[1], size)
        size = p.scalar(handle, 'size')
        # Concat operands are self-determined. The SDK can return an incorrect
        # positive parent Size as well as -1 for a nested sized cast.
        if op == 'npiConcatOp' and operands:
            size = sum(w for w, _, _ in operands)
        if type(size) is not int or size < 1:
            raise _UnsupportedPort('npi_width_unavailable', expression_kind='npiOperation', operator=op, actual_width=size)
        if size > options['limits']['max_width']:
            raise _UnsupportedPort('native_width_limit')
        if not operands:
            gap('npi_expression_unmodeled', ['constant_connection'], expression_kind='npiOperation', operator=op)
            return size, [], False
        if op == 'npiConcatOp' and sum(w for w, _, _ in operands) == size:
            return abstract_value(''.join(vector(w, rows) for w, rows, _ in operands))
        if all(complete for _, _, complete in operands):
            bits = binary(handle, size)
            if bits is not None:
                return size, [{'lsb_offset': 0, 'width': size, 'bits': bits}], True
            gap('npi_constant_value_unavailable', ['constant_connection'])
        gap('npi_expression_unmodeled', ['constant_connection'], expression_kind='npiOperation', operator=op)
        return size, [], False

    def parts(handle, depth=0):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        kind = p.scalar(handle, 'type')
        if kind in {'npiRefObj', 'npiExtRef'}:
            # These are references, not packed values: size is -1 in the SDK.
            # Resolve the documented relationship before inspecting type/width;
            # in particular, never evaluate an initial value of referenced state.
            actual = p.one(handle, 'actual_handle')
            if actual is None:
                raise _UnsupportedPort('npi_reference_unresolved')
            return parts(actual, depth + 1)
        if kind in _SELECTS:
            return selection(handle, kind, depth)
        if kind == 'npiOperation':
            return operation(handle, depth)
        size = width(handle)
        if kind in _CONSTANTS:
            bits = binary(handle, size)
            if bits is None:
                gap('npi_constant_value_unavailable', ['constant_connection'])
            return size, ([] if bits is None else [{'lsb_offset': 0, 'width': size, 'bits': bits}]), bits is not None
        if kind in _DYNAMIC:
            if kind in {'npiStructVar', 'npiStructNet'}:
                ts = p.one(handle, 'typespec_handle')
                if ts is None or p.scalar(ts, 'packed') != 1:
                    raise _UnsupportedPort('npi_packed_type_unavailable', expression_kind=kind)
            return size, [], False
        gap('npi_expression_unmodeled', ['constant_connection'], expression_kind=kind)
        return size, [], False

    def location(handle, inst=False):
        file = p.scalar(handle, 'port_inst_file' if inst else 'file')
        line = p.scalar(handle, 'port_inst_line_no' if inst else 'line_no')
        return {'file': file, 'line': line} if isinstance(file, str) and file and type(line) is int and line > 0 else None

    def save(kind, **data):
        if kind not in categories:
            return
        if len(facts) >= options['limits']['max_facts']:
            raise HierarchyBudgetExceeded('fact_limit')
        fact = dict(kind=kind, **data)
        size = len(json.dumps(fact, ensure_ascii=True)) * 4 + 256
        if budget.fact_bytes + size > budget.max_bytes:
            raise HierarchyBudgetExceeded('fact_byte_limit')
        budget.fact_bytes += size
        facts.append(fact)

    def inspect_ports(handle, path):
        nonlocal ports
        # The selected top is a design boundary, including for scoped requests.
        if path == p.top:
            return
        it = _LanguageIterator(p, p.low.Port, handle)
        try:
            while True:
                port = it.read()
                if port is None:
                    break
                if ports >= budget.max_symbols:
                    raise HierarchyBudgetExceeded('native_port_limit')
                ports += 1
                diagnostics.end_port()
                direction = p.scalar(port, 'direction')
                if direction != 'npiInput':
                    if direction not in {'npiOutput', 'npiInout', 'npiRef'}:
                        gap('npi_port_direction_unavailable')
                    continue
                name = p.scalar(port, 'name')
                if not isinstance(name, str) or not name:
                    gap('npi_port_identity_unavailable'); continue
                diagnostics.begin_port(path, name)
                actual = p.one(port, 'high_conn_handle')
                loc = location(port, True)
                base = dict(instance=path, port=name, source=loc,
                            definition_source=location(port), source_association='unverified', origin='input_binding')
                if actual is None:
                    low = p.one(port, 'low_conn_handle')
                    if (loc is not None and low is not None
                            and p.scalar(low, 'full_name') == path + '.' + name):
                        # Positive port-instance syntax + exact low binding,
                        # not a missing driver or an empty relationship alone.
                        save('open_input', **base, connection_status='explicit_empty',
                             interpretation='Explicit empty parent actual; default-port behavior unverified. No implicit 0 or Z conclusion.')
                        gap('npi_empty_actual_default_semantics_unverified')
                    else:
                        gap('npi_missing_actual_or_default_unresolved')
                    continue
                if 'constant_connection' not in categories:
                    continue
                try:
                    size, regions, _ = parts(actual)
                    port_width = width(port)
                except _UnsupportedPort as exc:
                    gap(exc.reason, ['constant_connection'], **exc.details)
                    continue
                if size != port_width:
                    try:
                        size, regions, _ = resize(actual, (size, regions, False), port_width)
                    except _UnsupportedPort as exc:
                        gap(exc.reason, ['constant_connection'], **exc.details)
                        continue
                if regions:
                    save('constant_connection', **base, width=port_width, constant_regions=regions)
        finally:
            diagnostics.end_port()
            p.close_iterator(it)

    def visit(handle, path, depth):
        p.check()
        if depth > budget.max_depth:
            raise HierarchyBudgetExceeded('scope_depth_limit')
        if path in seen:
            gap('npi_scope_alias_or_cycle'); return
        if budget.scope_count >= budget.max_scopes:
            raise HierarchyBudgetExceeded('scope_limit')
        seen.add(path); budget.scope_count += 1
        kind = p.scalar(handle, 'type')
        if kind not in _SCOPES:
            gap('npi_scope_kind_unmodeled'); return
        if kind in {'npiModule', 'npiInterface', 'npiProgram'} and p.scalar(handle, 'is_black_box') == 1:
            gap('npi_blackbox_boundary')
        inspect_ports(handle, path)
        it = _LanguageIterator(p, p.low.InternalScope, handle)
        try:
            while True:
                child = it.read()
                if child is None:
                    break
                kind = p.scalar(child, 'type')
                if kind in _IGNORE_SCOPES:
                    continue
                child_path = p.scalar(child, 'full_name')
                parent = p.one(child, 'scope_handle')
                if (not isinstance(child_path, str) or not child_path or parent is None
                        or p.scalar(parent, 'full_name') != path):
                    raise NpiHierarchyUnavailable('npi_scope_parent_mismatch')
                visit(child, child_path, depth + 1)
        finally:
            p.close_iterator(it)

    try:
        root = options.get('scope') or p.top
        if not (root == p.top or root.startswith(p.top + '.')):
            gap('npi_scope_outside_selected_top')
        else:
            p._require_top()
            handle = p.lookup(root)
            if handle is None:
                gap('npi_scope_not_found')
            elif p.scalar(handle, 'full_name') != root or not p._under_top(handle):
                raise NpiHierarchyUnavailable('npi_scope_changed_during_query')
            else:
                visit(handle, root, 0)
    except HierarchyBudgetExceeded as exc:
        gap(exc.reason)
    if p.degraded:
        gap('npi_degraded_design')
    results = {c: {'status': 'partial' if gaps[c] else 'complete', 'gaps': sorted(gaps[c])} for c in categories}
    for c in set(options['categories']) - categories:
        results[c] = {'status': 'unsupported', 'gaps': ['npi_category_unsupported']}
    all_gaps = sorted({g for row in results.values() for g in row['gaps']})
    return dict(status='partial' if all_gaps else 'complete', scope=options.get('scope'),
                artifact_identity=p.artifact_identity, capability_version=CAPABILITY_VERSION,
                categories_checked=sorted(categories), category_results=results, gaps=all_gaps,
                gap_summary=diagnostics.result(),
                facts=facts, total_facts=len(facts), counts=dict(Counter(f['kind'] for f in facts)),
                instances_visited=budget.scope_count, query_metrics={}, analysis_limits=[], coverage_status='partial' if all_gaps else 'complete',
                limitations=['kdb_source_association_unverified', 'native_positions_not_current_source_validation',
                             'input_ports_only', 'local_native_rss_not_hard_isolated'],
                metrics={'native_port_count': ports, 'native_traversal_ms': (time.monotonic()-started)*1000})


def query_native_scan(backend, options, *, top, budget):
    result = backend._query_language(lambda p: scan_native_ports(p, options), top=top, budget=budget)
    if result.get('analysis_limits'):
        result['status'] = 'partial'
        for row in result['category_results'].values():
            if row['status'] == 'complete':
                row['status'] = 'partial'
                row['gaps'] = sorted(set(row['gaps']) | set(result['analysis_limits']))
    result['metrics'].update(result.pop('query_metrics'))
    result['metrics']['native_instance_count'] = budget.scope_count
    result['metrics']['native_call_count'] = budget.call_count
    result['kdb_binding'] = dict(backend._bound_kdb_anchor.receipt(), load_status='loaded', load_quality=backend.kdb_load_quality)
    return result
