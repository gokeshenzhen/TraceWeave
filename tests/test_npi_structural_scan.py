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


def const(bits, signed=0):
    return Handle(type='npiConstant', size=len(bits), binary=bits, signed=signed)


def integer(value):
    return const(format(value & 0xffffffff, '032b'), signed=1)


def packed(kind, left, right, **data):
    r = Handle(left_range_handle=integer(left), right_range_handle=integer(right))
    return Handle(type=kind, size=abs(left-right)+1,
                  typespec_handle=Handle(type='npiLogicTypespec', Range=[r]), **data)


def part(parent, left, right):
    return Handle(type='npiPartSelect', size=abs(left-right)+1, parent_handle=parent,
                  left_range_handle=integer(left), right_range_handle=integer(right))


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
        self.low = NS(Port='Port', InternalScope='InternalScope', Operand='Operand', Range='Range',
                      BinStrVal='bin', iterate=self.iterate, scan=self.scan,
                      release_handle=self.release, get_value=self.value,
                      handle_by_index=lambda h, i: h.data.get('bits', {}).get(i),
                      compare_objects=lambda a, b: int(a is b))

    def require_owner(self): pass
    def check(self): self.budget.checkpoint()
    def _require_top(self): self.check()
    def _under_top(self, h): return True
    def lookup(self, path):
        pending, seen = [self.root], set()
        while pending:
            h = pending.pop()
            if id(h) in seen:
                continue
            seen.add(id(h))
            if h.data.get('full_name') == path:
                return h
            pending.extend(h.data.get('InternalScope', []))
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


def test_unambiguous_extension_and_ambiguous_one_bit_literal():
    _, r = run([port('a', const('10'))], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '0010'
    _, r = run([port('a', const('1'))], semantic_categories=['constant_connection'])
    assert not r['facts'] and 'npi_contextual_width_conversion_unverified' in r['gaps']
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


@pytest.mark.parametrize('kind', ['npiRefObj', 'npiExtRef'])
def test_references_resolve_actual_type_without_evaluating_state(kind):
    live = Handle(type='npiReg', size=4, binary='1111')
    ref = Handle(type=kind, size=-1, actual_handle=live)
    _, r = run([port('a', ref)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']
    ref.data['actual_handle'] = const('10xz')
    _, r = run([port('a', ref)], semantic_categories=['constant_connection'])
    assert r['facts'][0]['constant_regions'][0]['bits'] == '10xz'


def test_missing_or_cyclic_reference_never_completes_negative():
    ref = Handle(type='npiRefObj', size=-1)
    _, r = run([port('a', ref)], semantic_categories=['constant_connection'])
    assert r['status'] == 'partial' and 'npi_reference_unresolved' in r['gaps']
    ref.data['actual_handle'] = ref
    p, r = run([port('a', ref)], semantic_categories=['constant_connection'])
    assert r['status'] == 'partial' and not r['facts']
    assert 'native_expression_depth_limit' in r['gaps'] and not p.iterators


@pytest.mark.parametrize('left,right,sel_left,sel_right,expected', [
    (7, 0, 5, 2, 'xz01'), (1, 8, 2, 5, '0xz0'), (-1, -8, -2, -5, '0xz0')])
def test_parameter_slices_preserve_declared_direction_and_four_state(
        left, right, sel_left, sel_right, expected):
    parent = packed('npiParameter', left, right, binary='10xz0101')
    _, r = run([port('a', part(parent, sel_left, sel_right))],
               semantic_categories=['constant_connection'])
    assert r['status'] == 'complete'
    assert r['facts'][0]['constant_regions'] == [{'lsb_offset': 0, 'width': 4, 'bits': expected}]


def test_dynamic_slice_in_concat_does_not_evaluate_register_initial_value():
    live = packed('npiReg', 7, 0, binary='11111111')
    expr = Handle(type='npiOperation', size=4, op_type='npiConcatOp',
                  Operand=[const('10'), part(live, 1, 0)])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete'
    assert r['facts'][0]['constant_regions'] == [{'lsb_offset': 2, 'width': 2, 'bits': '10'}]


@pytest.mark.parametrize('kind', ['npiBitSelect', 'npiRegBit', 'npiNetBit', 'npiParameterBit'])
def test_fixed_bit_native_identity_is_checked_against_parent_range(kind):
    parent_kind = 'npiParameter' if kind == 'npiParameterBit' else 'npiReg'
    parent = packed(parent_kind, 7, 0, full_name='top.live', binary='10xz0101')
    bit = Handle(type=kind, size=-1 if kind == 'npiBitSelect' else 1,
                 parent_handle=parent, full_name='top.live[2]', constant_select=1)
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete'
    if kind == 'npiParameterBit':
        assert r['facts'][0]['constant_regions'][0]['bits'] == '1'
    else:
        assert not r['facts']
    for name in ['top.live[9]', 'top.other[2]', 'top.live[index]', 'top.live[' + '9'*200 + ']']:
        bit.data['full_name'] = name
        p, r = run([one], semantic_categories=['constant_connection'])
        assert r['status'] == 'partial' and not r['facts'] and not p.iterators


def test_native_integer_bit_has_intrinsic_packed_range():
    parent = Handle(type='npiIntegerVar', size=32, full_name='top.i', binary='1'*32,
                    typespec_handle=Handle(type='npiIntegerTypespec'))
    bit = Handle(type='npiBitSelect', size=-1, parent_handle=parent,
                 full_name='top.i[0]', constant_select=1)
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']


def test_ambiguous_or_out_of_range_slices_preserve_partial_coverage():
    live = packed('npiReg', 7, 0)
    bad = [part(live, 9, 6), part(live, 2, 5)]
    for expr in bad:
        _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
        assert r['status'] == 'partial' and not r['facts']
    live.data['typespec_handle'].data['Range'] *= 2
    p, r = run([port('a', part(live, 5, 2))], semantic_categories=['constant_connection'])
    assert 'npi_selection_range_unavailable' in r['gaps'] and not p.iterators
    live.data['typespec_handle'].data['Range'] = [Handle(left_range_handle=const('xx'))]
    _, r = run([port('a', part(live, 5, 2))], semantic_categories=['constant_connection'])
    assert r['status'] == 'partial' and not r['facts']


def test_gap_summary_counts_ports_once_and_bounds_examples():
    unknown = Handle(type='npiFuncCall', size=2)
    expr = Handle(type='npiOperation', op_type='npiConcatOp', size=4, Operand=[unknown, unknown])
    _, r = run([port(str(i), expr) for i in range(20)], semantic_categories=['constant_connection'])
    row, = r['gap_summary']
    assert row['reason'] == 'npi_expression_unmodeled'
    assert row['occurrences'] == row['affected_ports'] == 20
    assert len(row['examples']) == 2 and row['examples_truncated']
    assert row['examples'][0] == dict(instance='top.u', port='0', expression_kind='npiFuncCall')


def test_global_work_stop_is_not_an_affected_port():
    p = Provider([]); p.budget.max_scopes = 1
    r = scan_native_ports(p, p.options)
    assert r['gap_summary'] == [dict(reason='scope_limit', occurrences=1,
        affected_ports=0, examples=[], examples_truncated=False)]


@pytest.mark.parametrize('bits,signed,expected', [('10', 1, '1110'), ('xz', 1, 'xxxz'),
                                                 ('xz', 0, '00xz'), ('101010', 0, '1010')])
def test_assignment_conversion_preserves_signedness_and_four_state(bits, signed, expected):
    _, r = run([port('a', const(bits, signed))], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == expected


def test_packed_struct_state_is_dynamic_but_packed_parameter_is_constant():
    live = Handle(type='npiStructVar', size=4, binary='1111',
                  typespec_handle=Handle(type='npiStructTypespec', packed=1))
    parameter = Handle(type='npiPackedArrayParameter', size=4, binary='10xz')
    _, r = run([port('live', live), port('param', parameter)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and [f['port'] for f in r['facts']] == ['param']
    live.data['typespec_handle'].data['packed'] = 0
    _, r = run([port('live', live)], semantic_categories=['constant_connection'])
    assert 'npi_packed_type_unavailable' in r['gaps']


def test_packed_array_slice_uses_element_stride():
    parent = packed('npiPackedArrayParameter', 3, 0, binary='11001010')
    parent.data['size'] = 8
    parent.data['typespec_handle'].data['type'] = 'npiPackedArrayTypespec'
    expr = part(parent, 2, 1); expr.data['size'] = 4
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '0010'
    expr = part(parent, 3, 3); expr.data['size'] = 2
    one = port('a', expr); one.data['size'] = 2
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '11'


def test_native_identity_resolves_symbolic_index_without_parsing_it():
    parent = packed('npiReg', 1, 0, full_name='top.live')
    bit = Handle(type='npiRegBit', size=1, parent_handle=parent, full_name='top.live[pkg::P]')
    parent.data['bits'] = {0: Handle(), 1: bit}
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']
    parent.data['bits'] = {0: bit, 1: bit}
    _, r = run([one], semantic_categories=['constant_connection'])
    assert 'npi_selection_index_unavailable' in r['gaps']


def test_sized_cast_in_concat_can_supply_missing_native_width():
    live = Handle(type='npiStructVar', size=2, typespec_handle=Handle(type='npiStructTypespec', packed=1, signed=0))
    cast = Handle(type='npiOperation', op_type='npiExprCastOp', size=-1, Operand=[integer(4), live])
    expr = Handle(type='npiOperation', op_type='npiConcatOp', size=34, Operand=[const('11'), cast])
    one = port('a', expr); one.data['size'] = 6
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete'
    assert r['facts'][0]['constant_regions'] == [dict(lsb_offset=2, width=4, bits='1100')]


def test_sized_cast_signedness_comes_from_data_not_width_operand():
    cast = Handle(type='npiOperation', op_type='npiExprCastOp', size=32, signed=1,
                  Operand=[integer(4), const('1001', signed=0)])
    one = port('a', cast); one.data['size'] = 8
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '00001001'


def test_dynamic_operand_can_produce_constant_bits_without_initial_evaluation():
    live = Handle(type='npiReg', size=4, signed=0, binary='1111')
    expr = Handle(type='npiOperation', op_type='npiBitAndOp', size=4, Operand=[live, const('0000')])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '0000'
    expr.data['Operand'][1] = const('1111')
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']


def test_mixed_signed_bitwise_operands_zero_extend_before_operation():
    a = const('11', signed=1)
    b = const('1111', signed=0)
    expr = Handle(type='npiOperation', op_type='npiBitAndOp', size=4, Operand=[a, b])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['facts'][0]['constant_regions'][0]['bits'] == '0011'


def test_replication_counts_are_bounded_before_allocation():
    expr = Handle(type='npiOperation', op_type='npiMultiConcatOp', size=-1, Operand=[integer(2), const('10')])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '1010'
    expr.data['Operand'][0] = integer(100000)
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert 'native_width_limit' in r['gaps'] and not r['facts']


@pytest.mark.parametrize('kind', ['npiEnumVar', 'npiEnumNet'])
def test_enum_state_values_never_become_constant_connections(kind):
    live = Handle(type=kind, size=4, binary='1001')
    _, r = run([port('a', live)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']


def test_missing_part_size_is_derived_from_verified_bounds():
    parent = packed('npiParameter', 7, 0, binary='10xz0101')
    expr = part(parent, 5, 2); expr.data['size'] = -1
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == 'xz01'


def test_packed_struct_bit_has_flat_integral_range():
    live = Handle(type='npiStructVar', size=4, full_name='top.st', binary='1111',
                  typespec_handle=Handle(type='npiStructTypespec', packed=1))
    bit = Handle(type='npiRegBit', size=1, parent_handle=live, full_name='top.st[0]')
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']


def test_equal_width_enum_branches_need_no_missing_signedness_metadata():
    a, b = [Handle(type='npiEnumConst', size=4, signed=-1, binary=bits) for bits in ['1001', '0110']]
    expr = Handle(type='npiOperation', op_type='npiConditionOp', size=4,
                  Operand=[Handle(type='npiReg', size=1), a, b])
    _, r = run([port('a', expr)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and not r['facts']


def test_dynamic_parameter_selection_cannot_use_sdk_materialized_element_value():
    array = packed('npiArrayParameter', 1, 0, full_name='pkg.TABLE')
    selected = Handle(type='npiParameter', size=4, binary='1111', parent_handle=array,
                      full_name='pkg.TABLE[index]', index_handle=Handle(type='npiReg', size=1))
    _, r = run([port('a', selected)], semantic_categories=['constant_connection'])
    assert not r['facts'] and 'npi_constant_selection_unverified' in r['gaps']
    selected.data['full_name'] = 'pkg.TABLE[1]'; selected.data['index_handle'] = integer(1)
    _, r = run([port('a', selected)], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '1111'


def test_symbolic_constant_bit_needs_constant_index_proof_even_if_sdk_aliases_it():
    parent = packed('npiParameter', 1, 0, full_name='pkg.P', binary='10')
    bit = Handle(type='npiParameterBit', size=1, parent_handle=parent, full_name='pkg.P[index]')
    parent.data['bits'] = {0: bit, 1: Handle()}
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert not r['facts'] and 'npi_selection_index_unavailable' in r['gaps']


def test_one_element_array_pattern_uses_verified_formal_type():
    elem = Handle(type='npiStructTypespec', packed=1)
    array = Handle(type='npiPackedArrayTypespec', elem_typespec_handle=elem,
        Range=[Handle(left_range_handle=integer(0), right_range_handle=integer(0))])
    expr = Handle(type='npiOperation', op_type='npiAssignmentPatternOp', size=-1, Operand=[const('1010')])
    one = port('a', expr); one.data['low_conn_handle'].data['typespec_handle'] = array
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '1010'
    expr.data['Operand'][0] = const('xxzz')
    _, r = run([one], semantic_categories=['constant_connection'])
    assert not r['facts'] and 'npi_pattern_four_state_conversion_unverified' in r['gaps']
    array.data['Range'][0].data['left_range_handle'] = integer(1)
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'partial'  # More than one element is not pass-through.


def test_enum_range_with_leading_zero_needs_no_signedness_guess():
    parent = packed('npiParameter', 4, 0, full_name='top.P', binary='10101')
    parent.data['typespec_handle'].data['Range'][0].data['right_range_handle'] = Handle(
        type='npiEnumConst', size=3, signed=-1, binary='000')
    bit = Handle(type='npiParameterBit', size=1, parent_handle=parent, full_name='top.P[1]')
    one = port('a', bit); one.data['size'] = 1
    _, r = run([one], semantic_categories=['constant_connection'])
    assert r['status'] == 'complete' and r['facts'][0]['constant_regions'][0]['bits'] == '0'
    # 100 could mean either +4 or -4. Never silently choose a range layout.
    parent.data['typespec_handle'].data['Range'][0].data['right_range_handle'].data['binary'] = '100'
    _, r = run([one], semantic_categories=['constant_connection'])
    assert not r['facts'] and 'npi_signedness_unavailable' in r['gaps']
