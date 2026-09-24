from collections import Counter

import pytest

from src.expression_binding import bind_expression
from tests.test_waveform_selection import dump


def observe(bound,parser,time=0):
    reads = Counter()
    def value(path):
        reads[path] += 1
        return parser.get_value_at_time(path,time)
    return bound.evaluate(value), reads


def test_exact_binding_type_modes_and_selected_coordinates(tmp_path):
    p = dump(tmp_path)
    b = bind_expression(p,dict(expr='a[i]',typing='wave_bits',bindings={'a':'tb.negative[-2:-9]'},constants={'i':"-5"}))
    result,reads = observe(b,p)
    assert result.value == '1' and len(reads) == 1
    assert result.dependencies[0]['bits'] == [-5]


def test_explicit_type_and_wave_view_are_distinct(tmp_path):
    p = dump(tmp_path)
    raw = dict(expr='a >>> 1', bindings={'a':'tb.gen[2].data[3:0]'})
    with pytest.raises(ValueError,match='type_unresolved'):
        bind_expression(p,raw)
    signed = bind_expression(p,{**raw,'types':{'a':{'width':4,'signed':True,'packed':[[3,0]]}}})
    bits = bind_expression(p,{**raw,'typing':'wave_bits'})
    assert observe(signed,p)[0].value == '1110'
    assert observe(bits,p)[0].value == '0110'
    assert signed.receipt()['type_sources'] == {'a':'explicit'}
    with pytest.raises(ValueError,match='signal_unresolved'):
        bind_expression(p,dict(expr='bus',typing='wave_bits'))


def test_packed_dimensions_and_struct_members(tmp_path):
    p = dump(tmp_path)
    b = bind_expression(p,dict(expr='a[0][2]',bindings={'a':'tb.bus'},
        types={'a':{'width':8,'packed':[[0,1],[3,0]]}}))
    assert observe(b,p)[0].value == '1'
    b = bind_expression(p,dict(expr='s.data[i]',bindings={'s':'tb.bus'},constants={'i':"2'd1"},
        types={'s':{'width':8,'members':[{'name':'data','lsb':0,'type':{'width':4,'packed':[[3,0]]}}]}}))
    assert observe(b,p)[0].value == '1'


def sparse_spec(expr='mem[addr]'):
    return dict(expr=expr,bindings={
        'mem':{'elements':[{'indices':[2],'signal':'tb.gen[2].data[3:0]'},
                           {'indices':[3],'signal':'tb.pieces[3:0]'}]}},
        types={'mem':{'width':4,'unpacked':[[0,1000000]],'packed':[[3,0]]}},
        constants={'addr':"32'd2"})


def test_sparse_memory_selects_one_element_without_enumerating_shape(tmp_path):
    p = dump(tmp_path)
    spec = sparse_spec()
    b = bind_expression(p,spec)
    result,reads = observe(b,p)
    assert result.value == '1100'
    assert reads == {'tb.gen[2].data[3:0]':1}
    spec['constants']['addr'] = "32'd3"
    assert observe(bind_expression(p,spec),p)[0].value == '0110'
    spec['constants']['addr'] = "32'd9"
    result,reads = observe(bind_expression(p,spec),p)
    assert result.value is None and 'array_element_not_dumped' in result.gaps and not reads


@pytest.mark.parametrize('two_state,expected',[(False,'xxxx'),(True,'0000')])
def test_array_invalid_index_is_typed_default_not_missing_dump(tmp_path,two_state,expected):
    p = dump(tmp_path)
    spec = sparse_spec()
    spec['types']['mem']['two_state'] = two_state
    for index,gap in [("32'd1000001",'index_out_of_range'),("32'bx",'index_unknown')]:
        spec['constants']['addr'] = index
        result,reads = observe(bind_expression(p,spec),p)
        assert result.value == expected and gap in result.gaps and not reads


def test_nested_sparse_array_field_and_dynamic_packed_selection(tmp_path):
    p = dump(tmp_path)
    spec = sparse_spec('mem[i][j].data[k]')
    spec['types']['mem'] = {'width':4,'unpacked':[[1,0],[-1,1]],
        'members':[{'name':'data','lsb':1,'type':{'width':2,'packed':[[1,0]]}}]}
    spec['bindings']['mem']['elements'] = [{'indices':[0,-1],'signal':'tb.gen[2].data[3:0]'}]
    spec['constants'] = {'i':"1'b0",'j':"32'shffffffff",'k':"1'b1"}
    result,reads = observe(bind_expression(p,spec),p)
    assert result.value == '1' and sum(reads.values()) == 1


def test_type_queries_do_not_read_memory_and_mapping_validation(tmp_path):
    p = dump(tmp_path)
    spec = sparse_spec('$size(mem)')
    b = bind_expression(p,spec)
    assert not b.paths
    assert int(observe(b,p)[0].value,2) == 1000001
    spec['bindings']['mem']['elements'].append({'indices':[2],'signal':'tb.bit[3]'})
    with pytest.raises(ValueError,match='mapping_invalid'):
        bind_expression(p,spec)


def test_decisive_branch_does_not_read_inactive_memory(tmp_path):
    p = dump(tmp_path)
    spec = sparse_spec("1'b0 && mem[addr]")
    spec['constants']['addr'] = "32'd9"
    result,reads = observe(bind_expression(p,spec),p)
    assert result.value == '0' and not result.gaps and not reads
    # The inactive expression still needs valid static syntax and shape.
    with pytest.raises(ValueError,match='array_requires_selection'):
        bind_expression(p,{**spec,'expr':"1'b0 && mem"})


@pytest.mark.parametrize('bounds', [[2,3], [3,2], [-2,-1]])
def test_template_matches_explicit_elements_in_both_range_directions(tmp_path, bounds):
    p = dump(tmp_path)
    original = p.get_signal_declaration
    left, right = bounds
    paths = {f'tb.mem[{left}][3:0]': 'tb.gen[2].data[3:0]',
             f'tb.mem[{right}][3:0]': 'tb.pieces[3:0]'}
    # Alias metadata to real dumped words; values still come from the VCD reader.
    p.get_signal_declaration = lambda path: original(paths.get(path, path))
    raw = dict(expr='mem[i]', bindings={'mem': {'path_template': 'tb.mem[{index}][3:0]'}},
               types={'mem': {'width':4, 'unpacked':[bounds]}}, constants={'i':str(left)})
    explicit = {**raw, 'bindings': {'mem': {'elements': [
        {'indices':[index], 'signal':path} for index,path in zip(bounds, paths)]}}}
    for index in bounds:
        raw['constants']['i'] = explicit['constants']['i'] = str(index)
        actual = observe(bind_expression(p, raw), p)
        expected = observe(bind_expression(p, explicit), p)
        assert actual[0].value == expected[0].value and actual[1] == expected[1]


def test_template_missing_dump_is_not_observed_x_or_zero(tmp_path):
    p = dump(tmp_path)
    raw = dict(expr='mem[0]', bindings={'mem': {'path_template':'tb.absent[{index}]'}},
               types={'mem': {'width':4, 'unpacked':[[0,1]], 'two_state':True}})
    result, reads = observe(bind_expression(p,raw), p)
    assert result.value is None and 'array_element_not_dumped' in result.gaps and not reads
    raw['expr'] = '$size(mem)'
    assert int(observe(bind_expression(p,raw),p)[0].value,2) == 2


@pytest.mark.parametrize('mapping,bounds,reason', [
    ({'path_template':'tb.mem'}, [[0,3]], 'input_invalid'),
    ({'path_template':'tb.mem[{index}][{index}]'}, [[0,3]], 'input_invalid'),
    ({'path_template':'tb.mem[{index}][{other}]'}, [[0,3]], 'input_invalid'),
    ({'path_template':'tb.mem[{index}]','elements':[{'indices':[0],'signal':'tb.a'}]}, [[0,3]], 'input_invalid'),
    ({'path_template':'tb.mem[{index}]'}, [[0,2],[0,2]], 'mapping_invalid'),
    ({'path_template':'tb.mem[{index}]'}, [[0,128]], 'dependency_limit'),
])
def test_template_rejects_unsupported_or_unbounded_expansion_without_reads(mapping, bounds, reason):
    class Unreadable:
        def get_signal_declaration(self, path):
            pytest.fail('invalid template must fail before reading waveform metadata')
    with pytest.raises(ValueError, match=reason + '|path_template'):
        bind_expression(Unreadable(), dict(expr='mem[i]', bindings={'mem':mapping}, constants={'i':'0'},
            types={'mem':{'width':8,'unpacked':bounds}}))


def test_generated_hierarchy_index_is_constant_identity(tmp_path):
    p = dump(tmp_path)
    b = bind_expression(p,dict(expr='tb.gen[2].data',typing='wave_bits'))
    assert observe(b,p)[0].value == '1100'
    with pytest.raises((KeyError,ValueError)):
        bind_expression(p,dict(expr='tb.gen[i].data',typing='wave_bits',constants={'i':'2'}))


@pytest.mark.parametrize('name', ['$size','$left','$right','$low','$high','$increment'])
def test_dimension_parameter_reads_only_its_runtime_index(name):
    from src.expression_binding import bind_expression
    class Metadata:
        def get_signal_declaration(self,path):
            assert path=='top.dim'
            return dict(path=path,width=3,declared_range=dict(left=2,right=0))
    p=Metadata()
    bound=bind_expression(p,dict(expr=name+'(mem, dim)',bindings={'dim':'top.dim'},types={
        'mem':dict(width=8,packed=[[7,0]],unpacked=[[1,2],[-1,1]]),'dim':dict(width=3)}))
    assert bound.paths==('top.dim',)
    assert bound.type_sources['mem']=='explicit'
    expected = {'$size':[2,3,8],'$left':[1,-1,7],'$right':[2,1,0],
        '$low':[1,-1,0],'$high':[2,1,7],'$increment':[-1,-1,1]}[name]
    for i,want in enumerate(expected,1):
        result=bound.evaluate(lambda path: f'{i:03b}')
        assert result.value==f'{want & 0xffffffff:032b}'
        assert {d['role'] for d in result.dependencies}=={'index'}
    assert bound.evaluate(lambda _: 'xxx').value=='x'*32
    assert bound.evaluate(lambda _: '000').value=='x'*32


def test_type_query_does_not_require_dump_for_explicit_type():
    from src.expression_binding import bind_expression
    class Unreadable:
        def get_signal_declaration(self,path):
            pytest.fail('a pure explicit type query must not request dump metadata')
    p=Unreadable()
    b=bind_expression(p,dict(expr='$bits(mem)',types={'mem':dict(width=8,unpacked=[[0,7]])}))
    assert b.evaluate(lambda _:pytest.fail('no values needed')).value==f'{64:032b}'
    assert not b.paths and b.type_sources=={'mem':'explicit'}
