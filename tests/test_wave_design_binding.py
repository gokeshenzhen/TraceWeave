from pathlib import Path
import pytest
from src.wave_design_binding import bind_root
from src.vcd_parser import VCDParser


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
