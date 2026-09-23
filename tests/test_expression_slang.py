"""Real Slang projection, worker roundtrip, and independent SV expectations."""
import pytest

from src.dynamic_evidence import DYNAMIC_VERSION, Expr, evaluate, validate_step
from src.dynamic_observe import observe_step
from src.vcd_parser import VCDParser
from tests.expression_oracle import cases
from tests.test_dynamic_evidence import project


def sample_values(values):
    def sample(expr):
        value = values[expr.signal]
        declared = expr.declared_bits or tuple(range(len(value)-1,-1,-1))
        return {'value': ''.join(value[declared.index(bit)] for bit in expr.bits)}
    return sample


@pytest.mark.parametrize('case', cases(), ids=lambda c:c['id'])
def test_slang_expression_matches_independent_oracle(case):
    ports = [f"input logic {'signed' if v.get('signed') else ''} [{v['width']-1}:0] {n}"
             for n,v in case['inputs'].items()]
    ports.append(f"output wire [{case['expected']['width']-1}:0] y")
    backend = project(f"module t({','.join(ports)}); assign y=({case['expression']}); endmodule")
    step = validate_step(backend.get_dynamic_step('t.y'))
    assert step['complete'], step
    result = evaluate(Expr.from_dict(step['branches'][0]['value']),
                      sample_values({'t.'+n:v['value'] for n,v in case['inputs'].items()}))
    assert result.value == case['expected']['bin'], result


@pytest.mark.parametrize('decl,expr,expected', [
    ('logic [1:0][3:0] a', 'a[i]', '1010'),
    ('logic [0:1][0:3] a', 'a[i]', '0101'),
    ('logic [-2:-9] a', 'a[-3 -: 3]', '010'),
    ('logic [1:0][3:0] a', 'a[i][j]', '0'),
    ('logic [1:0][3:0] a', 'a[1][j]', '0'),
])
def test_multidimensional_and_nonzero_packed_selections(decl,expr,expected):
    width = len(expected)
    backend = project(f'module t(input {decl}, input int i,j, output wire [{width-1}:0] y); assign y={expr}; endmodule')
    step = backend.get_dynamic_step('t.y')
    assert step['complete'], step
    result = evaluate(Expr.from_dict(step['branches'][0]['value']),
        sample_values({'t.a':'10100101','t.i':f'{1:032b}','t.j':f'{0:032b}'}))
    assert result.value == expected


def test_multidimensional_static_connection_uses_correct_bit_mapping():
    backend = project('module t(input logic [1:0][3:0] a, output logic [3:0] y); assign y=a[1]; endmodule')
    match = backend._entry.query_engine.query_driver('t.y').matches[0]
    # A packed element is a four-bit chunk, not a single flattened bit.
    assert match.dependencies[0].source.bits == (7,6,5,4)


def test_fixed_arrays_read_only_selected_dump_element(tmp_path):
    backend = project('''module t(input int i,j, input logic clk, input logic [7:0] d,
        output logic [7:0] y); logic [7:0] mem[1:3][-1:1];
        always @(posedge clk) mem[2][0] <= d;
        assign y = mem[i][j]+8'd1; endmodule''')
    step = backend.get_dynamic_step('t.y')
    assert step['complete'], step
    path = tmp_path/'array.vcd'
    path.write_text('''$timescale 1ps $end
$scope module t $end
$var wire 32 ! i [31:0] $end
$var wire 32 " j [31:0] $end
$var wire 8 # mem[2][0] [7:0] $end
$var wire 8 $ mem[3][0] [7:0] $end
$upscope $end
$enddefinitions $end
#0
b10 !
b0 "
b00001111 #
b10101010 $
#10
b11 !
#20
''')
    parser = VCDParser(str(path))
    calls = []
    original = parser.get_transitions
    def reads(signal,*args):
        calls.append(signal)
        return original(signal,*args)
    parser.get_transitions = reads
    result = observe_step(step,get_parser=lambda _:parser,wave=str(path),time=9,history_start=0)
    assert result['complete'] and result['value']=='00010000', result
    assert not any('mem[3]' in c for c in calls)
    assert {d['signal'] for d in result['dependencies']} == {'t.i','t.j','t.mem[2][0]'}
    later = observe_step(step,get_parser=lambda _:parser,wave=str(path),time=19,history_start=0)
    assert later['complete'] and later['value']=='10101011'


def test_array_struct_field_and_selection_composition():
    backend = project('''module t(input int i,j, output logic y);
      typedef struct packed {logic [3:0] data; logic flag;} item_t;
      item_t entries [0:7]; assign y=entries[i].data[j]; endmodule''')
    step = backend.get_dynamic_step('t.y')
    assert step['complete'], step
    expr = Expr.from_dict(step['branches'][0]['value'])
    result = evaluate(expr,sample_values({'t.i':f'{2:032b}','t.j':f'{1:032b}','t.entries[2]':'10101'}))
    assert result.value == '1'
    assert {d['signal'] for d in result.dependencies if d['role']=='index'} == {'t.i','t.j'}


def test_previous_dynamic_wire_protocol_is_rejected():
    assert DYNAMIC_VERSION == '2.0'
    with pytest.raises(ValueError,match='version'):
        validate_step({'version':'1.0','backend':'source_graph'})


def test_missing_array_element_cannot_read_base_or_nearby_element(tmp_path):
    from tests.test_dynamic_binding import observation
    from src.expression_values import number
    array = Expr('array_ref',8,signal='t.mem',bits=tuple(range(7,-1,-1)),
                 declared_bits=tuple(range(7,-1,-1)),dimensions=((0,3),))
    selected = Expr('array_select',8,(array,Expr('const',32,value=number(1).bits)),bounds=(0,3))
    path = tmp_path/'missing.vcd'
    path.write_text('''$timescale 1ps $end
$scope module t $end
$var wire 8 ! mem [7:0] $end
$var wire 8 " mem[2] [7:0] $end
$upscope $end
$enddefinitions $end
#0
b11111111 !
b00000000 "
#10
''')
    result = observation(VCDParser(str(path)),selected)
    assert not result['complete'] and result['value'] is None
    assert 'signal_not_dumped' in result['gaps']


@pytest.mark.parametrize('two_state,expected',[(True,'00000000'),(False,'xxxxxxxx')])
def test_semantic_array_invalid_index_uses_leaf_default(two_state,expected):
    array = Expr('array_ref',8,signal='t.mem',bits=tuple(range(7,-1,-1)),
                 declared_bits=tuple(range(7,-1,-1)),dimensions=((2,3),),two_state=two_state)
    node = Expr('array_select',8,(array,Expr('const',1,value='x')),bounds=(2,3),two_state=two_state)
    def unread(_):
        pytest.fail('unknown index cannot choose a memory element')
    result = evaluate(node,unread)
    assert result.value == expected and 'index_unknown' in result.gaps


@pytest.mark.parametrize('escaped',[False,True])
def test_net_initializer_reads_exact_vcd_memory_identity(tmp_path,escaped):
    backend=project('module t(input int i); logic [7:0] mem[0:3]; wire [7:0] y=mem[i]+8\'d1; endmodule')
    step=backend.get_dynamic_step('t.y')
    assert step['complete'],step
    path=tmp_path/'memory.vcd'
    name=('\\' if escaped else '')+'mem[2]'
    path.write_text(f'''$timescale 1ps $end
$scope module t $end
$var wire 32 ! i [31:0] $end
$var wire 8 " {name} [7:0] $end
$upscope $end
$enddefinitions $end
#0
b10 !
b00001111 "
#10
''')
    parser=VCDParser(str(path))
    result=observe_step(step,get_parser=lambda _:parser,wave=str(path),time=9,history_start=0)
    assert result['complete'] and result['value']=='00010000',result


def test_scalar_memory_index_is_not_a_packed_bit_suffix(tmp_path):
    backend=project('module t(input int i); logic mem[1:3]; wire y=mem[i]; endmodule')
    step=backend.get_dynamic_step('t.y')
    assert step['complete']
    path=tmp_path/'scalar.vcd'
    path.write_text('''$timescale 1ps $end
$scope module t $end
$var wire 32 ! i [31:0] $end
$var wire 1 " mem[2] $end
$upscope $end
$enddefinitions $end
#0
b10 !
1"
#10
''')
    parser=VCDParser(str(path))
    result=observe_step(step,get_parser=lambda _:parser,wave=str(path),time=9,history_start=0)
    assert result['complete'] and result['value']=='1',result


def test_conflicting_escaped_memory_spellings_are_not_guessed(tmp_path):
    backend=project('module t(input int i); logic [7:0] mem[1:3]; wire [7:0] y=mem[i]; endmodule')
    path=tmp_path/'conflict.vcd'
    path.write_text('''$timescale 1ps $end
$scope module t $end
$var wire 32 ! i [31:0] $end
$var wire 8 " mem[2] [7:0] $end
$var wire 8 # \\mem[2] [7:0] $end
$upscope $end
$enddefinitions $end
#0
b10 !
b00000000 "
b11111111 #
#10
''')
    parser=VCDParser(str(path))
    result=observe_step(backend.get_dynamic_step('t.y'),get_parser=lambda _:parser,
        wave=str(path),time=9,history_start=0)
    assert not result['complete'] and result['value'] is None
    assert 'dynamic_bit_mapping_unavailable' in result['gaps']
