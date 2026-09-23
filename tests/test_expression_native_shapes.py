"""Opt-in actual simulator dumps: memory names, packed layout and fields."""
import os
from pathlib import Path
import subprocess

import pytest

from src.dynamic_observe import observe_step
from src.dynamic_binding import signal_expression
from src.expression_observe import ExpressionParser
from src.fsdb_parser import FSDBParser
from src.vcd_parser import VCDParser
from tests.test_dynamic_evidence import project

DESIGN = '''module expression_shapes;
logic [1:0][3:0] packed_data;
logic [7:0] mem [1:3];
logic single_mem [1:3];
typedef struct packed {logic [3:0] data;logic flag;} item_t;
item_t entries [0:3];
item_t [3:2] packed_entries;
int i,j;
wire [7:0] y = mem[i] + packed_data[i-2];
wire field_y = entries[i].data[j];
wire single_y = single_mem[i];
wire packed_field_y = packed_entries[i].data[j];
wire [9:0] packed_elements_bits = packed_entries;
'''


@pytest.mark.eda_integration
def test_vcs_dumped_array_and_struct_expressions(tmp_path):
    if os.environ.get('TRACEWEAVE_TEST_EXPRESSION_ORACLE') != '1':
        pytest.skip('set TRACEWEAVE_TEST_EXPRESSION_ORACLE=1 for simulator dump check')
    # Keep initialization outside the source projection: it supplies recorded
    # storage values, not a model that reconstructs storage writes.
    src=tmp_path/'shapes.sv'
    src.write_text(DESIGN+'''initial begin
$dumpfile("shapes.vcd"); $dumpvars(0,expression_shapes);
$dumpvars(0,mem[2],mem[3],entries[2],entries[3]);
$fsdbDumpfile("shapes.fsdb"); $fsdbDumpvars(0,expression_shapes); $fsdbDumpMDA();
packed_data=8'ha5; i=2; j=1; mem[2]=8'h0f; mem[3]=8'h10;
entries[2]=5'b10101; entries[3]=5'b01010; single_mem[2]=1; single_mem[3]=0;
packed_entries={5'b01010,5'b10101};
#5 i=3;
#5 j=0;
#5 $finish;
end
endmodule
''')
    build=subprocess.run(['vcs','-full64','-sverilog','-timescale=1ns/1ps','-debug_access+all',
        str(src),'-top','expression_shapes','-o','simv'],cwd=tmp_path,capture_output=True,text=True,timeout=60)
    assert build.returncode==0,build.stdout+build.stderr
    run=subprocess.run([str(tmp_path/'simv'),'-no_save'],cwd=tmp_path,capture_output=True,text=True,timeout=30)
    assert run.returncode==0,run.stdout+run.stderr
    backend=project(DESIGN+'endmodule')
    for cls,suffix in ((VCDParser,'vcd'),(FSDBParser,'fsdb')):
        parser=cls(str(tmp_path/f'shapes.{suffix}'))
        try:
            public = ExpressionParser(parser)
            # FSDB may expose a packed struct array only as separate fields;
            # this explicitly declared vector view is actually dumped in both
            # formats. Its layout comes from the test's declaration above.
            bindings = {'packed_entries': parser.search_signals('packed_elements_bits',
                         max_results=20)['results'][0]['path']}
            # i/j searches use exact declaration identity rather than prefix matches.
            for name in ('i', 'j'):
                candidates = parser.search_signals('expression_shapes.'+name, max_results=100)['results']
                bindings[name] = next(r['path'] for r in candidates
                    if r['path'].split('.')[-1].split('[')[0] == name)
            key = public.bind(dict(expr='packed_entries[i].data[j]', bindings=bindings,
                types={'packed_entries': dict(width=10, packed=[[3,2],[4,0]], members=[
                    dict(name='data', lsb=1, type=dict(width=4, packed=[[3,0]])),
                    dict(name='flag', lsb=0, type=dict(width=1))]),
                    'i':dict(width=32,signed=True),'j':dict(width=32,signed=True)}))
            for at, expected in zip((2000,7000,12000),(1,0,1)):
                actual = public.get_value_at_time(key,at)
                assert actual['value']['dec'] == expected
            assert public.expression_receipts()[0]['coverage_status'] == 'complete'
            for target,values in [('y',[20,26,26]),('field_y',[1,0,1]),('single_y',[1,0,0])]:
                step=backend.get_dynamic_step('expression_shapes.'+target)
                assert step['complete'],step
                symbol = 'expression_shapes.' + {'y':'mem','field_y':'entries','single_y':'single_mem'}[target]
                dumped = any(r['path'] == symbol or r['path'].startswith(symbol+'[')
                    for r in parser.search_signals(symbol,max_results=8)['results'])
                if suffix=='fsdb' and target in {'y','single_y'}:
                    assert dumped  # The native positive array oracle is required.
                for at,expected in zip((2000,7000,12000),values):
                    state=step['state']
                    dumped_target=signal_expression(parser,state['signal'],state['bits'],state['declared_bits']).signal
                    actual=parser.get_value_at_time(dumped_target,at)['value']['dec']
                    assert actual==expected
                    observed=observe_step(step,get_parser=lambda _:parser,wave=parser.file_path,
                        time=at,history_start=0)
                    if dumped:
                        assert observed['complete'],(suffix,target,observed)
                        assert int(observed['value'],2)==expected
                    else:
                        assert observed['value'] is None and 'signal_not_dumped' in observed['gaps']
        finally:
            if hasattr(parser,'close'):parser.close()
