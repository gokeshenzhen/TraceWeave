from pathlib import Path
import pytest
from src.wave_design_binding import bind_root
from src.vcd_parser import VCDParser
from src.dynamic_evidence import Expr, evaluate
from src.wave_design_binding import bind_source_expression
from tests.test_dynamic_evidence import project


def dump(tmp_path, body):
    path=tmp_path/'roots.vcd'
    path.write_text('$timescale 1ps $end\n'+body+'\n$enddefinitions $end\n#0\n0!\n')
    return VCDParser(str(path))


def test_wrapper_needs_compile_top_and_exact_declaration(tmp_path):
    p=dump(tmp_path,'$scope module TOP $end\n$scope module chip $end\n$var wire 1 ! q $end\n$upscope $end\n$upscope $end')
    cr=dict(simulator='verilator',top_modules=['chip'])
    binding=bind_root(p,'TOP.chip.q',cr)
    assert binding.to_design('TOP.chip.q')=='chip.q'
    assert binding.to_wave('chip.gen[0].u.a[2].field[7:0]')=='TOP.chip.gen[0].u.a[2].field[7:0]'
    assert binding.to_design('TOP.chip2.q')=='TOP.chip2.q'
    assert bind_root(p,'TOP.chip.no_dump',cr) is None
    assert bind_root(p,'TOP.chip.q',{**cr,'simulator':'vcs'}) is None
    assert bind_root(p,'TOP.chip.q',{**cr,'top_modules':['TOP']}) is None
    assert bind_root(p,'TOP.chip.q',{**cr,'top_modules':['TOP','chip']}) is None
    assert bind_root(p,'TOP.chip.q',{**cr,'top_modules':['chip','other']}) is None
    assert bind_root(p,'TOP.chip.q',cr,top_hint='other') is None


def test_real_top_and_escaped_identifier_never_stripped(tmp_path):
    p=dump(tmp_path,'$scope module TOP $end\n$var wire 1 ! \\chip.q $end\n$upscope $end')
    assert bind_root(p,'TOP.\\chip.q',dict(simulator='verilator',top_modules=['TOP'])) is None
    assert bind_root(p,'TOP.chip.q',dict(simulator='verilator',top_modules=['chip'])) is None


def test_selected_dynamic_rhs_binds_typed_field_dump(tmp_path):
    backend = project('''
      typedef struct packed {logic gnt; logic [3:0] data;} req_t;
      module chip(input req_t [1:0] a);
        req_t [5:3] b;
        assign b[4] = {1'b0, a[0].data ^ 4'h1};
      endmodule
    ''')
    step = backend.get_dynamic_step('chip.b[4].data')
    assert step['width'] == 4 and 'dynamic_bit_mapping_unavailable' not in step['gaps']
    p = dump(tmp_path, '$scope module TOP $end\n$scope module chip $end\n'
             '$var wire 4 ! a[0].data [3:0] $end\n$upscope $end\n$upscope $end')
    binding = bind_root(p, 'TOP.chip.a[0].data[3:0]', dict(simulator='verilator', top_modules=['chip']))
    seen = []
    def sample(e):
        bound = bind_source_expression(p, e, binding, engine=backend._entry.query_engine)
        seen.append(bound)
        return {'value':'0010'}
    result = evaluate(Expr.from_dict(step['branches'][0]['value']), sample)
    assert result.value == '0011'
    assert len(seen) == 1 and seen[0].signal == 'TOP.chip.a[0].data[3:0]'
    assert seen[0].bits == (3,2,1,0)
    constant = backend.get_dynamic_step('chip.b[4].gnt')
    assert evaluate(Expr.from_dict(constant['branches'][0]['value']),
                    lambda _: pytest.fail('constant gnt read unrelated data')).value == '0'


def test_aggregate_dump_and_field_shape_mismatch_are_not_guessed(tmp_path):
    backend = project('''typedef struct packed {logic gnt; logic [3:0] data;} req_t;
      module chip; req_t [1:0] a; endmodule''')
    engine = backend._entry.query_engine
    e = Expr('signal', 4, signal='chip.a', bits=(3,2,1,0), declared_bits=tuple(range(9,-1,-1)))
    p=dump(tmp_path,'$scope module chip $end\n$var wire 10 ! a [9:0] $end\n$upscope $end')
    assert bind_source_expression(p,e,engine=engine).bits == (3,2,1,0)
    p=dump(tmp_path,'$scope module chip $end\n$var wire 8 ! a[0].data [7:0] $end\n$upscope $end')
    with pytest.raises(KeyError):
        bind_source_expression(p,e,engine=engine)


@pytest.mark.anyio
async def test_dynamic_route_uses_design_targets_and_returns_wave_dependencies(tmp_path):
    from types import SimpleNamespace
    from src.divergence_routing import DynamicRoute
    backend = project('module chip(input logic a, output logic q); assign q=a; endmodule')
    p=dump(tmp_path,'$scope module TOP $end\n$scope module chip $end\n'
           '$var wire 1 ! q $end\n$var wire 1 " a $end\n$upscope $end\n$upscope $end')
    async def worker(fn): return fn()
    async def wave(path,fn): return fn()
    class Budget:
        queries=0
        def check(self): pass
        async def run(self,fn): return await fn()
    class Route(DynamicRoute):
        async def _prepare_source(self):
            assert self.targets == ['chip.q']
            self.backend = backend
    services=SimpleNamespace(_run_in_cancellable_thread=worker,_run_in_wave_thread=wave,
        _get_parser=lambda _:p,probe_verdi_backend=lambda *a,**k:{})
    bound=SimpleNamespace(compile_result=dict(simulator='verilator',top_modules=['chip']),
                          context=dict(compile_log='unused',simulator='verilator'))
    route=Route(services,'A',dict(signal_path='TOP.chip.q',wave_path=p.file_path),bound,Budget())
    step=await route.query('TOP.chip.q')
    assert step['branches'][0]['value']['signal']=='TOP.chip.a'
    assert step['signal']=='TOP.chip.q'
    assert route.current()
    with Path(p.file_path).open('a') as f: f.write('#2\n1!\n')
    assert not route.current()
