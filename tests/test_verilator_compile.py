from pathlib import Path
import pytest
from src.compile_log_parser import parse_compile_log, detect_simulator
from src.path_discovery import discover_sim_paths
from src.tb_hierarchy_builder import build_hierarchy
from src.source_graph_adapter import _compile_manifest, build_source_graph_plan
from src.source_graph_contract import QueryOperation
from src.hierarchy_handles import compute_snapshot_fingerprint


def context(tmp_path):
    (tmp_path/'rtl').mkdir(); (tmp_path/'inc').mkdir()
    (tmp_path/'rtl/dut.sv').write_text('module dut(input logic a, output logic y); assign y = a; endmodule\n')
    (tmp_path/'rtl/tb.sv').write_text('module tb; logic a, y; dut u_dut(.a(a), .y(y)); endmodule\n')
    (tmp_path/'rtl/child.f').write_text('dut.sv\n')
    (tmp_path/'model.cpp').write_text('int main(){}\n')
    (tmp_path/'waiver.vlt').write_text('`verilator_config\nlint_off -rule WIDTH -file "*/dut.sv" \\\n -match "Width mismatch"\nsplit_var -module "dut" -var "y"\n')
    (tmp_path/'design.vc').write_text('+incdir+inc\n-DVERIFIED=1\n-F rtl/child.f\nrtl/tb.sv\nwaiver.vlt\nmodel.cpp\n--top-module tb\n')
    path=tmp_path/'hdl-compile.log'
    path.write_text(f"make: Entering directory '{tmp_path}'\nverilator --xml-only -f design.vc --cc --exe model.cpp --trace --trace-fst --no-timing --threads 4 -CFLAGS 'ignored.v' -LDFLAGS '-pthread' -GUNUSED=1\nmake: Leaving directory '{tmp_path}'\n")
    return path


def test_real_hdl_command_cwd_top_order_and_native_exclusion(tmp_path):
    path=context(tmp_path); result=parse_compile_log(str(path))
    assert result['simulator']=='verilator' and result['primary_top']=='tb'
    assert [Path(f['path']).name for f in result['files']['user']]==['dut.sv','tb.sv']
    assert result['include_dirs']==[str(tmp_path/'inc')]
    assert result['defines']==['VERILATOR=1','VERIFIED=1']
    assert result['parse_warnings']==[]
    assert [Path(f['path']).name for f in result['compile_evidence']['filelists']]==['design.vc','child.f']
    assert 'verilator --xml-only' in result['compile_command']


def test_source_graph_options_and_support_identity(tmp_path):
    path=context(tmp_path); result=parse_compile_log(str(path))
    manifest,gaps,exclusions,*_= _compile_manifest(str(path),result)
    assert manifest.inputs_complete and manifest.options_complete
    assert '-GUNUSED=1' in manifest.ordered_options
    assert 'dpi_runtime' in exclusions
    assert not any('configuration_unmodeled' in g for g in gaps)
    # Optimizer/waiver content must still be part of compile identity.
    before=manifest.fingerprint
    (tmp_path/'waiver.vlt').write_text('`verilator_config\nlint_off -rule UNUSED\n')
    after,*_= _compile_manifest(str(path),result)
    assert after.fingerprint != before


def test_hierarchy_and_source_graph_route_from_same_context(tmp_path):
    path=context(tmp_path); result=parse_compile_log(str(path)); hierarchy=build_hierarchy(result, str(path), apply_source_overlay=False)
    hierarchy['_hierarchy_snapshot_sha256'] = compute_snapshot_fingerprint(str(path),'verilator')
    plan=build_source_graph_plan(compile_log=str(path),compile_result=result,hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(str(path),'verilator'),
        operation=QueryOperation.DRIVER, signal_path='tb.u_dut.y', top_hint='tb',max_hops=4,frontend_version='11.0.0')
    assert plan.request is not None
    assert '-DVERILATOR=1' in plan.request.identity.compile_inputs.ordered_options
    (tmp_path/'rtl/dut.sv').write_text('module dut(input logic a, output logic y); assign y = ~a; endmodule\n')
    changed=build_source_graph_plan(compile_log=str(path),compile_result=result,hierarchy_result=hierarchy,
        hierarchy_snapshot_sha256=compute_snapshot_fingerprint(str(path),'verilator'),
        operation=QueryOperation.DRIVER,signal_path='tb.u_dut.y',top_hint='tb',max_hops=4,frontend_version='11.0.0')
    assert changed.request is None and 'compile_session_snapshot_changed' in changed.receipt.gap_codes


def test_runtime_banner_after_1000_lines_and_bounded_discovery(tmp_path):
    path=context(tmp_path)
    sim=tmp_path/'sim.log';sim.write_text('preamble\n'*8200+'INFO:cocotb: 0.00ns INFO cocotb Running on Verilator version 5.034\n')
    assert detect_simulator(str(sim))=='verilator'
    (tmp_path/'waveform.fst').write_bytes(b'placeholder')
    result=discover_sim_paths(str(tmp_path),sim_log=str(sim),compile_log=str(path),wave_file=str(tmp_path/'waveform.fst'))
    assert result['simulator']=='verilator'
    assert result['compile_logs'][0]['phase']=='elaborate'


def test_tool_mention_or_cpp_build_is_not_hdl_evidence(tmp_path):
    path=tmp_path/'compile.log';path.write_text('g++ -I/tools/verilator/5.034/include model.cpp -o model\n')
    assert detect_simulator(str(path))=='unknown'
    result=parse_compile_log(str(path),'verilator')
    assert result['files']['user']==[] and result['primary_top'] is None
    assert 'HDL command missing' in result['parse_warnings'][0]
    path.write_text('README: use verilator --cc to compile this\n')
    assert detect_simulator(str(path))=='unknown'


def test_missing_and_recursive_filelists_are_explicit(tmp_path):
    path=context(tmp_path)
    (tmp_path/'design.vc').write_text('-f design.vc\n-f missing.f\n--top-module tb\n')
    result=parse_compile_log(str(path))
    assert len(result['parse_warnings'])==2
    assert any('depth/cycle' in w for w in result['parse_warnings'])


def test_actual_cocotb_test_runner_command_wrapper(tmp_path):
    path=context(tmp_path)
    path.write_text(f"INFO:cocotb:Running command: perl /tools/verilator/bin/verilator -cc -f design.vc in directory {tmp_path}\n")
    result=parse_compile_log(str(path))
    assert result['simulator']=='verilator' and result['primary_top']=='tb'
    assert result['compile_cwd']==str(tmp_path)
    assert len(result['files']['user'])==2
    assert result['parse_warnings']==[]
