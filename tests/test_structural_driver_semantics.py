import pytest

from src.structural_driver_facts import driver_fact, validate_driver_fact
from src.structural_semantics import ScanLimits, scan_semantic_session
from tests.test_structural_semantics import session

CATEGORIES = ['multiple_driver', 'undriven_signal']


def test_driver_source_paths_are_reused_only_within_one_inventory(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS
    from src import structural_slang_drivers as drivers
    pytest.importorskip('pyslang')
    filename=str(tmp_path/'dut.sv')
    manager=NS(getFileName=lambda _:filename,getLineNumber=lambda location:location)
    frontend=NS(driver=NS(sourceManager=manager))
    calls=[];original=drivers.os.path.realpath
    def resolve(path):
        calls.append(path)
        return original(path)
    with monkeypatch.context() as scoped:
        scoped.setattr(drivers.os.path,'realpath',resolve)
        first=drivers._Inventory(frontend,None,CATEGORIES,ScanLimits())
        assert first.location(NS(location=3))=={'file':filename,'line':3}
        assert first.location(NS(location=8))=={'file':filename,'line':8}
        second=drivers._Inventory(frontend,None,CATEGORIES,ScanLimits())
        assert second.location(NS(location=11))=={'file':filename,'line':11}
    assert calls==[filename,filename]


def scan(tmp_path, text, **kwargs):
    return scan_semantic_session(session(tmp_path, text), categories=CATEGORIES, **kwargs)


def test_ascending_output_slices_report_cc31_overlap_and_consumed_gap(tmp_path):
    result = scan(tmp_path, '''
module box(input [1:4] a, output reg [1:4] q);always @* q=a;endmodule
module core(input [1:4] a,b, output [1:12] y);
wire [1:12] S;
box u1(a,S[6:9]); box u2(b,S[9:12]);
assign y=S;
endmodule
module top(input [1:4] a,b, output [1:12] y);core c(a,b,y);endmodule
''', scope='top.c')
    overlap = next(f for f in result['facts'] if f['signal_path']=='top.c.S' and f['kind']=='multiple_driver')
    assert result['status']=='complete',result['gaps']
    assert overlap['target_bits'] == [9]
    assert overlap['driver_count'] == 2
    assert {d['instance'] for d in overlap['drivers']} == {'top.c.u1','top.c.u2'}
    missing = next(f for f in result['facts'] if f['signal_path']=='top.c.S' and f['kind']=='undriven_signal')
    assert missing['target_bits'] == [1,2,3,4,5]
    assert missing['driver_inventory_complete'] is True
    for fact in result['facts']:
        validate_driver_fact(fact)


def test_process_branches_are_one_writer_and_two_processes_are_two(tmp_path):
    result = scan(tmp_path, '''
module top(input a,b,clk, output reg q,r);
always @* begin if(a) q=1; else q=0; q=b; end
always @(posedge clk) r<=a;
always @(negedge clk) r<=b;
endmodule
''')
    assert not any(f['signal']=='q' for f in result['facts'])
    assert next(f for f in result['facts'] if f['signal']=='r')['driver_count']==2


def test_net_port_collapse_does_not_create_false_drivers_or_undriven(tmp_path):
    result = scan(tmp_path, '''
module leaf(input wire a, output wire y); assign y=a; endmodule
module empty(output wire y);endmodule
module top(input a,output b,c);
wire x; assign x=a;
leaf l(x,b);
empty e(c); assign c=1'b0;
endmodule
''')
    assert result['facts'] == []
    assert result['status']=='complete',result['gaps']


@pytest.mark.parametrize('parent_count,child_count',[(1,1),(1,2),(2,1),(2,2)])
def test_net_port_collapse_merges_all_independent_sources(tmp_path,parent_count,child_count):
    child=''.join(f"assign q=1'b{index%2};" for index in range(child_count))
    parent=''.join(f"assign y=1'b{index%2};" for index in range(parent_count))
    result=scan(tmp_path,'module leaf(output wire q);'+child+'''endmodule
module top(output wire y);'''+parent+'leaf l(y);endmodule')
    assert result['status']=='complete',result['gaps']
    facts={f['signal_path']:f for f in result['facts']}
    assert set(facts)=={'top.y','top.l.q'}
    for fact in facts.values():
        assert fact['kind']=='multiple_driver'
        assert fact['driver_count']==parent_count+child_count
        assert len({d['driver_id'] for d in fact['drivers']})==parent_count+child_count


def test_cross_hierarchy_writes_are_included_for_scoped_output(tmp_path):
    result = scan(tmp_path, '''
module leaf(output wire [3:0] y);wire [3:0] n;assign y=n;endmodule
module top(output [3:0] y); leaf l(y); assign l.n=4'hf;endmodule
''', scope='top.l')
    assert result['facts'] == []
    assert result['status']=='complete',result['gaps']


def test_indexed_select_and_parameterized_generate_use_elaborated_writers(tmp_path):
    result = scan(tmp_path, '''
module top(input [7:0] a,b, output [7:0] y);
localparam N=4;
for(genvar i=0;i<2;i++)begin:g assign y[i*N +: N]=a[i*N +: N];end
if(0)begin:unused assign y=b;end
endmodule
''')
    assert result['facts'] == []
    assert result['status']=='complete',result['gaps']


def test_only_read_bits_are_reported_and_unused_nets_are_not(tmp_path):
    result = scan(tmp_path, '''module top(output y);wire [7:0] n;wire unused;assign y=n[3];endmodule''')
    assert [(f['signal'],f['target_bits']) for f in result['facts']] == [('n',[3])]


@pytest.mark.parametrize('limit,gap', [
    (ScanLimits(max_instances=1),'driver_instance_limit'),
    (ScanLimits(max_propagation_bits=1),'driver_bit_limit'),
    (ScanLimits(max_ast_nodes=1),'driver_ast_limit'),
])
def test_incomplete_inventory_never_proves_absence(tmp_path, limit, gap):
    result = scan(tmp_path, '''
module leaf(output wire y); wire n; assign y=n; endmodule
module top(output y);leaf l(y);assign l.n=1;endmodule
''', limits=limit)
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])
    assert gap in result['gaps']


def test_initializers_force_and_dynamic_writes_are_not_undriven(tmp_path):
    result = scan(tmp_path, '''
module top(input clk,input [1:0] index,output [3:0] y);
reg [3:0] a=0; reg [3:0] b; reg [3:0] c;
always @(posedge clk) b[index]<=1;
initial force c=4'hf;
assign y=a^b^c;
endmodule
''')
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])


def test_driver_fact_requires_complete_negative_and_distinct_positive():
    with pytest.raises(ValueError):
        driver_fact('undriven_signal','top.n',[0],[],complete=False)
    with pytest.raises(ValueError):
        driver_fact('multiple_driver','top.n',[0],[{'driver_id':'same'},{'driver_id':'same'}],complete=True)


def test_subroutine_side_effects_and_increment_are_not_missing_writers(tmp_path):
    result = scan(tmp_path, '''
module top(input clk,output q,y);reg state;reg [3:0] counter;
function void write_state();state=1;endfunction
always @(posedge clk)begin write_state();counter++;end
assign q=state;assign y=counter[0];
endmodule
''')
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])
    assert 'driver_bits_unresolved' in result['gaps']


def test_blocking_frontend_diagnostics_prevent_negative_proof(tmp_path):
    result = scan(tmp_path, 'module top(output y);wire n;assign y=n; missing_module u();endmodule')
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])
    assert 'driver_inventory_incomplete' in result['gaps']


def test_interface_runtime_state_is_not_proved_undriven(tmp_path):
    result = scan(tmp_path, '''
interface bus(input clk);logic d;clocking cb @(posedge clk);output d;endclocking endinterface
module top(input clk,output y);bus b(clk);assign y=b.d;endmodule
''')
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])
    assert 'driver_interface_runtime_boundary' in result['gaps']


@pytest.mark.parametrize('variable',[False,True])
def test_two_output_bits_aliased_onto_one_net_are_distinct_sources(tmp_path,variable):
    body='output reg [1:0] y);always @* y=a;' if variable else 'output wire [1:0] y);assign y=a;'
    result=scan(tmp_path,'module leaf(input [1:0] a,'+body+'''
endmodule
module top(input [1:0] a,output q);leaf l(a,{q,q});endmodule
''')
    fact=next(f for f in result['facts'] if f['kind']=='multiple_driver' and f['signal_path']=='top.q')
    assert fact['driver_count']==2
    assert fact['target_bits']==[0]


def test_continuous_concat_keeps_distinct_driver_positions(tmp_path):
    result=scan(tmp_path,'''module top(input a,b,output wire q);
assign {q,q}={a,b};endmodule''')
    fact=next(f for f in result['facts'] if f['signal_path']=='top.q')
    assert result['status']=='complete',result['gaps']
    assert fact['kind']=='multiple_driver' and fact['driver_count']==2
    assert {d['assignment_lsb_offset'] for d in fact['drivers']}=={0,1}


def test_resized_output_binding_does_not_claim_complete_bit_mapping(tmp_path):
    result=scan(tmp_path,'''module leaf(input [1:0] a,output reg [1:0] y);
always @* y=a;endmodule
module top(input [1:0] a,output q);leaf l(a,{q,q,q});endmodule''')
    assert result['status']=='partial'
    assert 'driver_output_width_conversion_boundary' in result['gaps']
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])


def test_automatic_and_initialized_subroutine_locals_are_not_undriven(tmp_path):
    result=scan(tmp_path,'''module top(input a,output y,z);
function automatic logic f(input b);logic t;return t;endfunction
function logic g(input b);logic t=0;return t;endfunction
assign y=f(a);assign z=g(a);endmodule''')
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])


def test_automatic_local_writes_do_not_invalidate_module_absence_proofs(tmp_path):
    result=scan(tmp_path,'''module top(input a,output y,z);
wire missing;
function automatic logic f(input b);logic t; t=b;return t;endfunction
assign y=f(a);assign z=missing;endmodule''')
    assert result['status']=='complete',result['gaps']
    assert [(f['signal'],f['target_bits']) for f in result['facts']]==[('missing',[0])]


def test_driver_and_propagation_checks_share_the_ast_budget(tmp_path):
    design=session(tmp_path,'''module top(output y,z);
wire missing,c;assign c=1;assign y=c;assign z=missing;endmodule''')
    limits=ScanLimits(max_ast_nodes=40)
    drivers=scan_semantic_session(design,categories=CATEGORIES,limits=limits)
    propagation=scan_semantic_session(design,categories=['propagated_constant'],limits=limits)
    assert drivers['status']==propagation['status']=='complete'
    combined=scan_semantic_session(design,categories=[*CATEGORIES,'propagated_constant'],limits=limits)
    assert combined['status']=='partial'
    assert combined['counts']=={'undriven_signal':1}
    assert 'propagation_ast_limit' in combined['gaps']
    assert not combined['propagation']['inventory_complete']


@pytest.mark.anyio
async def test_public_deep_driver_categories_and_auto_result_reuse(tmp_path, monkeypatch):
    import server
    from src.structural_semantic_runtime import SemanticScanRuntime
    from tests.test_structural_scan_runtime import context
    log, _, compiled = context(tmp_path, '''
module top(input a,b,output y,z);
wire n;assign y=a;assign y=b;assign z=n;
endmodule
''')
    monkeypatch.setenv('TRACEWEAVE_CONNECTIVITY_ROUTE','source_graph')
    monkeypatch.setattr(server,'_parse_merged_compile_context',lambda **_: (compiled,'vcs'))
    monkeypatch.setattr(server,'_semantic_scan_runtime',SemanticScanRuntime())
    args=dict(compile_log=str(log),simulator='vcs',semantic_categories=CATEGORIES)
    result=await server._dispatch('scan_structural_risks',{**args,'analysis_mode':'deep'})
    assert result.semantic.status=='complete',result.semantic
    assert result.semantic.counts=={'multiple_driver':1,'undriven_signal':1}
    assert all(c.evidence_domain=='elaborated_static_drivers' for c in result.semantic.category_coverage)
    warm=await server._dispatch('scan_structural_risks',args)
    assert warm.semantic.cache_disposition=='hit_memory'
    assert warm.semantic.facts==result.semantic.facts
    tools=await server.list_tools()
    tool=next(t for t in tools if t.name=='scan_structural_risks')
    assert set(CATEGORIES)<=set(tool.inputSchema['properties']['semantic_categories']['items']['enum'])
