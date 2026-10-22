"""Bounded input-port observations from an exclusive NPI Language owner.

Verified against Verdi V-2023.12-SP2: Port/HighConn/LowConn, PortInstFile/
LineNo, InternalScope and BinStrVal. A missing HighConn alone is NOT open.
Default-port semantics and contextual width conversion remain explicit gaps.
No source reading, driver traversal, waveform access or design building occurs.
"""
from __future__ import annotations

from collections import Counter
import json
import time

from .hierarchy_source_ir import HierarchyBudgetExceeded, HierarchyQueryBudget
from .npi_hierarchy_source import _LanguageIterator, NpiHierarchyUnavailable
from .structural_semantic_router import NPI_PORT_CATEGORIES

CAPABILITY_VERSION = "npi_input_ports_v1"
_DYNAMIC = {"npiNet", "npiReg", "npiLogicVar", "npiIntVar", "npiIntegerVar", "npiBitVar"}
_SCOPES = {"npiModule", "npiInterface", "npiProgram", "npiGenScope"}
_IGNORE_SCOPES = {"npiTask", "npiFunction", "npiNamedBegin", "npiNamedFork", "npiClassDefn"}


class _UnsupportedPort(Exception):
    def __init__(self, reason):
        self.reason = reason
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

    def gap(reason, affected=None):
        for c in categories if affected is None else categories & set(affected):
            gaps[c].add(reason)

    def width(handle):
        value = p.scalar(handle, 'size')
        if type(value) is not int or value < 1:
            raise _UnsupportedPort('npi_width_unavailable')
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

    def parts(handle, depth=0):
        if depth > 64:
            raise HierarchyBudgetExceeded('native_expression_depth_limit')
        kind, size = p.scalar(handle, 'type'), width(handle)
        if kind in {'npiConstant', 'npiParameter', 'npiSpecParam', 'npiEnumConst'}:
            bits = binary(handle, size)
            if bits is None:
                gap('npi_constant_value_unavailable', ['constant_connection'])
            return size, ([] if bits is None else [{'lsb_offset': 0, 'width': size, 'bits': bits}]), bits is not None
        if kind in _DYNAMIC:
            return size, [], False
        if kind != 'npiOperation':
            gap('npi_expression_unmodeled', ['constant_connection'])
            return size, [], False
        op = p.scalar(handle, 'op_type')
        it = _LanguageIterator(p, p.low.Operand, handle)
        operands = []
        try:
            for _ in range(257):
                item = it.read()
                if item is None:
                    break
                if len(operands) == 256:
                    raise HierarchyBudgetExceeded('native_operand_limit')
                operands.append(parts(item, depth + 1))
        finally:
            p.close_iterator(it)
        if not operands:
            gap('npi_expression_unmodeled', ['constant_connection'])
            return size, [], False
        if all(complete for _, _, complete in operands):
            bits = binary(handle, size)
            if bits is not None:
                return size, [{'lsb_offset': 0, 'width': size, 'bits': bits}], True
            gap('npi_constant_value_unavailable', ['constant_connection'])
        if op == 'npiConcatOp' and sum(w for w, _, _ in operands) == size:
            regions, offset = [], 0
            for w, rows, _ in reversed(operands):
                regions.extend(dict(r, lsb_offset=r['lsb_offset'] + offset) for r in rows)
                offset += w
            return size, regions, False
        gap('npi_expression_unmodeled', ['constant_connection'])
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
                direction = p.scalar(port, 'direction')
                if direction != 'npiInput':
                    if direction not in {'npiOutput', 'npiInout', 'npiRef'}:
                        gap('npi_port_direction_unavailable')
                    continue
                name = p.scalar(port, 'name')
                if not isinstance(name, str) or not name:
                    gap('npi_port_identity_unavailable'); continue
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
                    gap(exc.reason, ['constant_connection'])
                    continue
                if size != port_width:
                    gap('npi_contextual_width_conversion_unverified', ['constant_connection'])
                    continue
                if regions:
                    save('constant_connection', **base, width=port_width, constant_regions=regions)
        finally:
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
