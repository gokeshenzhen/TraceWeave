"""Typed dynamic reads bind dump declarations without changing A/B identities."""
from dataclasses import asdict
from pathlib import Path

import pytest

from src.cancellation import OperationCancelled
from src.dynamic_evidence import Expr, TRUE
from src.dynamic_observe import observe_step
from src.fsdb_parser import FSDBParser
from src.vcd_parser import VCDParser


def observation(parser, expr, *, time=9):
    step = dict(signal="tb.result", width=expr.width, bits=list(range(expr.width - 1, -1, -1)),
                boundary="combinational", complete=True, gaps=[],
                branches=[dict(id="assign", order=0, guard=asdict(TRUE), value=asdict(expr))])
    return observe_step(step, get_parser=lambda _: parser, wave=parser.file_path,
                        time=time, history_start=0)


@pytest.mark.parametrize("left,right", [(12, 9), (9, 12), (-1, -4)])
def test_exact_range_binding_preserves_declared_coordinates_and_source_identity(tmp_path, left, right):
    path = tmp_path / "range.vcd"
    path.write_text(f"""$timescale 1ps $end
$scope module tb $end
$scope module gen[2] $end
$var wire 4 ! bus [{left}:{right}] $end
$var wire 4 ! alias [{left}:{right}] $end
$upscope $end
$upscope $end
$enddefinitions $end
#0
b1010 !
#10
""")
    inner = VCDParser(str(path))
    source = "tb.gen[2].alias"
    exact = f"{source}[{left}:{right}]"
    class ExactParser:
        file_path = str(path)
        def get_signal_declaration(self, name):
            if name != exact:
                raise KeyError(name)
            return {**inner.get_signal_declaration(name), 'path': exact}
        def get_transitions(self, name, *args):
            assert name == exact
            return inner.get_transitions(name, *args)
        def __getattr__(self, name):
            return getattr(inner, name)
    declared = tuple(range(left, right + (-1 if left > right else 1), -1 if left > right else 1))
    expr = Expr("signal", 2, signal=source, bits=(right, left), declared_bits=declared)
    result = observation(ExactParser(), expr)
    assert result['complete'] and result['value'] == '01'
    assert result['dependencies'][0]['signal'] == source
    assert result['dependencies'][0]['bits'] == [right, left]


def test_real_native_vector_without_range_is_observed_with_exact_binding():
    parser = FSDBParser(str(Path(__file__).parent / 'fixtures/scale_1ns.fsdb'))
    source = 'scale_1ns_tb.addr'
    expr = Expr('signal', 4, signal=source, bits=(31, 29, 30, 28),
                declared_bits=tuple(range(31, -1, -1)))
    try:
        result = observation(parser, expr, time=109000)
        # Addr=B at 101 ns; ordered declared bits produce 1101.
        assert result['complete'] and result['value'] == '1101'
        assert result['dependencies'][0]['signal'] == source
        assert not parser._transition_group_active
    finally:
        parser.close()


def test_binding_never_uses_an_unrelated_same_basename_or_guesses_bad_bits(tmp_path):
    path = tmp_path / 'missing.vcd'
    path.write_text('''$timescale 1ps $end
$scope module other $end
$var wire 4 ! data [3:0] $end
$upscope $end
$enddefinitions $end
#0
b1010 !
#10
''')
    parser = VCDParser(str(path))
    absent = observation(parser, Expr('signal', 4, signal='tb.data', bits=(3,2,1,0), declared_bits=(3,2,1,0)))
    assert not absent['complete'] and 'signal_not_dumped' in absent['gaps']
    invalid = observation(parser, Expr('signal', 1, signal='other.data', bits=(8,), declared_bits=(3,2,1,0)))
    assert not invalid['complete'] and 'dynamic_bit_mapping_unavailable' in invalid['gaps']


def test_binding_cancellation_is_not_missing_evidence():
    class CancelParser:
        file_path = 'cancelled'
        def get_signal_declaration(self, path):
            raise OperationCancelled()
    with pytest.raises(OperationCancelled):
        observation(CancelParser(), Expr('signal', 1, signal='d', bits=(0,), declared_bits=(0,)))
