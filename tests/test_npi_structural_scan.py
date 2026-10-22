"""Native semantics are checked separately on a real KDB; these test controls."""
from types import SimpleNamespace as NS
import time

import pytest

from src.cancellation import OperationCancelled
from src.hierarchy_source_ir import HierarchyBudgetExceeded
from src.npi_structural_scan import scan_budget, scan_native_ports
from src.structural_semantic_runtime import validate_semantic_options


class Handle:
    def __init__(self, **data):
        self.data = data
        self.cps_obj = self


def const(bits):
    return Handle(type='npiConstant', size=len(bits), binary=bits)


def port(name, actual=None, **data):
    return Handle(name=name, direction='npiInput', size=4, high_conn_handle=actual,
                  file='definition.sv', line_no=1, port_inst_file='binding.sv', port_inst_line_no=5,
                  low_conn_handle=Handle(full_name='top.u.' + name), **data)


class Provider:
    def __init__(self, ports, *, options=None):
        self.options = options or validate_semantic_options({})[1]
        self.budget = scan_budget(self.options)
        self.top = 'top'; self.degraded = False; self.artifact_identity = 'kdb'
        self.root = Handle(type='npiModule', full_name='top', InternalScope=[])
        self.child = Handle(type='npiModule', full_name='top.u', scope_handle=self.root, Port=ports, InternalScope=[])
        self.root.data['InternalScope'] = [self.child]
        self.iterators = set(); self.releases = 0; self.iterator_releases = 0
        self.low = NS(Port='Port', InternalScope='InternalScope', Operand='Operand',
                      BinStrVal='bin', iterate=self.iterate, scan=self.scan,
                      release_handle=self.release, get_value=self.value)

    def require_owner(self): pass
    def check(self): self.budget.checkpoint()
    def _require_top(self): self.check()
    def _under_top(self, h): return True
    def lookup(self, path): return {'top': self.root, 'top.u': self.child}.get(path)
    def scalar(self, h, name): self.budget.call(); return h.data.get(name)
    def one(self, h, name): self.budget.call(); return h.data.get(name)
    def wrap(self, raw): return raw
    def iterate(self, relation, ref): return iter(ref.data.get(relation, []))
    def scan(self, it): return next(it, None)
    def release(self, raw): self.releases += 1; return 1
    def close_iterator(self, it): it.close()
    def value(self, h, fmt, default):
        bits = h.data.get('binary')
        return NS(get_str_val=lambda: bits) if bits is not None else None


def run(ports, **arguments):
    p = Provider(ports, options=validate_semantic_options(arguments)[1])
    return p, scan_native_ports(p, p.options)


def test_literal_xz_and_empty_binding_keep_positive_native_evidence():
    p, r = run([port('a', const('xz01')), port('b')])
    assert r['facts'][0]['constant_regions'] == [{'lsb_offset': 0, 'width': 4, 'bits': 'xz01'}]
    assert r['facts'][1]['connection_status'] == 'explicit_empty'
    assert r['status'] == 'partial'  # Defaults cannot be inferred from NULL.
    assert all(f['source_association'] == 'unverified' for f in r['facts'])
    assert not p.iterators


def test_null_actual_without_instance_syntax_never_proves_open():
    x = port('a'); x.data['port_inst_file'] = None
    _, r = run([x], semantic_categories=['open_input'])
    assert r['facts'] == [] and r['status'] == 'partial'
    assert 'npi_missing_actual_or_default_unresolved' in r['gaps']


def test_only_requested_categories_and_no_top_input_false_positive():
    x = port('a', const('1010'))
    p = Provider([x]); p.root.data['Port'] = [port('boundary')]
    r = scan_native_ports(p, p.options)
    assert len(r['facts']) == 1 and r['facts'][0]['instance'] == 'top.u'
    _, r = run([x], semantic_categories=['open_input'])
    assert r['facts'] == [] and r['status'] == 'complete'
    _, r = run([], semantic_categories=['constant_connection', 'open_input'])
    assert r['status'] == 'complete' and r['total_facts'] == 0


def test_mixed_concat_lsb_regions_and_no_register_initial_evaluation():
    dyn = Handle(type='npiReg', size=2, binary='11')
    expr = Handle(type='npiOperation', size=4, op_type='npiConcatOp', Operand=[const('10'), dyn])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['facts'][0]['constant_regions'] == [{'lsb_offset': 2, 'width': 2, 'bits': '10'}]
    assert r['status'] == 'complete'


def test_contextual_widths_and_unmodeled_shapes_are_gaps():
    _, r = run([port('a', const('10'))], semantic_categories=['constant_connection'])
    assert r['facts'] == [] and 'npi_contextual_width_conversion_unverified' in r['gaps']
    _, r = run([port('a', Handle(type='npiFuncCall', size=4, binary='0000'))])
    assert not r['facts'] and 'npi_expression_unmodeled' in r['gaps']


@pytest.mark.parametrize('category', ['constant_comparison', 'propagated_constant', 'constant_control'])
def test_unsupported_categories_are_not_completed(category):
    _, r = run([], semantic_categories=[category])
    assert r['category_results'][category]['status'] == 'unsupported'
    assert r['categories_checked'] == []


def test_fact_call_instance_width_limits_close_iterators():
    p, r = run([port('a', const('1010')), port('b', const('0101'))], semantic_max_facts=1)
    assert len(r['facts']) == 1 and 'fact_limit' in r['gaps'] and not p.iterators
    p = Provider([port('a')]); p.budget.max_calls = 7
    r = scan_native_ports(p, p.options)
    assert 'native_call_limit' in r['gaps'] and not p.iterators
    p = Provider([]); p.budget.max_scopes = 1
    assert 'scope_limit' in scan_native_ports(p, p.options)['gaps'] and not p.iterators
    _, r = run([port('a', const('0'*4097))])
    assert 'native_width_limit' in r['gaps']


def test_cancellation_and_late_native_return_are_not_clean():
    p = Provider([port('a')]); old = p.low.scan
    def cancel(it): raise OperationCancelled()
    p.low.scan = cancel
    with pytest.raises(OperationCancelled): scan_native_ports(p, p.options)
    assert not p.iterators
    p = Provider([port('a')]); old = p.low.scan
    def late(it): p.budget.deadline = time.monotonic()-1; return old(it)
    p.low.scan = late
    r = scan_native_ports(p, p.options)
    assert r['status'] == 'partial' and 'timeout_sec' in r['gaps'] and not p.iterators


def test_degraded_binding_survives_early_work_limit():
    p = Provider([]); p.degraded = True; p.budget.max_scopes = 1
    r = scan_native_ports(p, p.options)
    assert {'scope_limit', 'npi_degraded_design'} <= set(r['gaps'])


def test_blackbox_zero_ports_cannot_report_complete_negative():
    p = Provider([]); p.child.data['is_black_box'] = 1
    r = scan_native_ports(p, p.options)
    assert r['status'] == 'partial' and r['total_facts'] == 0
    assert 'npi_blackbox_boundary' in r['gaps']


def test_procedural_scopes_excluded_but_generate_bindings_traversed():
    p = Provider([])
    procedural = [Handle(type=k) for k in ['npiFor', 'npiBegin', 'npiFork']]
    gen = Handle(type='npiGenScope', full_name='top.g', scope_handle=p.root)
    child = Handle(type='npiModule', full_name='top.g.u', scope_handle=gen,
                   Port=[port('a', const('1010'))])
    gen.data['InternalScope'] = [child]
    p.root.data['InternalScope'] = procedural + [gen]
    p.options['categories'] = ['constant_connection', 'open_input']
    r = scan_native_ports(p, p.options)
    assert r['status'] == 'complete' and r['instances_visited'] == 3
    assert r['facts'][0]['instance'] == 'top.g.u'
    p.root.data['InternalScope'].append(
        Handle(type='npiUnknownScope', full_name='top.unknown', scope_handle=p.root))
    r = scan_native_ports(p, p.options)
    assert 'npi_scope_kind_unmodeled' in r['gaps']
