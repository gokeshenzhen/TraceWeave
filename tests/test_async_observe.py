"""Independent event tables for typed asynchronous boundary observations."""
from dataclasses import asdict
import threading

import pytest

from src.async_observe import observe_async_controls
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.dynamic_evidence import Expr, validate_step
from src.vcd_parser import VCDParser


def step(active='0', kind='reset'):
    ref = lambda name: asdict(Expr('signal', 1, signal='top.' + name, bits=(0,), declared_bits=(0,)))
    return dict(version='1.0', backend='verdi_npi', signal='top.q', bits=[0], width=1,
                state=ref('q'), boundary='sequential', complete=False, branches=[],
                clock=dict(expression=ref('clk'), edge='posedge'),
                async_controls=[dict(expression=ref('mode'), kind=kind, active_value=active,
                                     assertion_edge='negedge' if active == '0' else 'posedge')],
                gaps=['temporal_context_unavailable', 'async_control_value_unmodeled'])


def fixture(tmp_path, *, active='0', missing=None, uncertain=False, scale='1ps', repeat=False):
    inactive = '1' if active == '0' else '0'
    rows = {0: dict(clk='0', mode='x' if uncertain else inactive, q='0'),
            5: dict(clk='1'), 10: dict(clk='0'), 12: dict(mode=active, q='x'),
            15: dict(clk='1'), 20: dict(clk='0')}
    if repeat:
        rows.update({13: dict(mode=inactive), 14: dict(mode=active)})
    text = f'$timescale {scale} $end\n$scope module top $end\n'
    symbols = dict(clk='!', mode='@', q='#')
    text += ''.join(f'$var wire 1 {symbol} {name} $end\n' for name, symbol in symbols.items() if name != missing)
    text += '$upscope $end\n$enddefinitions $end\n'
    for at, values in sorted(rows.items()):
        text += f'#{at}\n' + ''.join(f'{value}{symbols[name]}\n' for name, value in values.items() if name != missing)
    path = tmp_path / 'async.vcd'
    path.write_text(text)
    return VCDParser(str(path))


def observe(parser, native_step=None, **kwargs):
    args = dict(get_parser=lambda _: parser, wave=parser.file_path, start=0, time=13,
                phase='after', unknown_onset_fs=12000)
    args.update(kwargs)
    return observe_async_controls(native_step or step(), **args)


@pytest.mark.parametrize('active,kind', [('0', 'reset'), ('1', 'reset'), ('0', 'set'), ('1', 'set')])
def test_typed_polarity_records_async_edge_without_guessing_assignment(tmp_path, active, kind):
    result = observe(fixture(tmp_path, active=active), step(active, kind))
    assert result['status'] == 'partial'
    assert result['assignment_value_status'] == 'unmodeled' and result['inference_status'] == 'not_run'
    assert result['clock']['last_observed_edge_ps'] == 5
    assert result['clock']['edge_at_unknown_onset'] is False
    control = result['controls'][0]
    assert control['active'] and control['assertion_status'] == 'observed'
    assert control['last_observed_edge_ps'] == 12 and control['observed_edge_count'] == 1
    assert control['edge_at_unknown_onset'] is True
    assert 'value' not in result  # Pin reset/set type must not become a forced Q value.


@pytest.mark.parametrize('missing', ['clk', 'mode'])
def test_missing_control_or_clock_is_not_absence_evidence(tmp_path, missing):
    result = observe(fixture(tmp_path, missing=missing))
    fact = result['clock'] if missing == 'clk' else result['controls'][0]
    assert fact['status'] == 'partial' and fact['edge_at_unknown_onset'] is None
    assert 'signal_not_dumped' in result['gaps']
    if missing == 'mode':
        assert fact['assertion_status'] == 'inconclusive' and fact['active'] is None
        assert 'async_control_value_unavailable' in result['gaps']


def test_unknown_control_and_sub_ps_order_do_not_prove_an_assertion(tmp_path):
    result = observe(fixture(tmp_path, uncertain=True))
    assert result['controls'][0]['assertion_status'] == 'inconclusive'
    assert result['controls'][0]['edge_at_unknown_onset'] is None
    result = observe(fixture(tmp_path, scale='100fs'), time=2, unknown_onset_fs=1200)
    assert 'sampling_order_unresolved' in result['gaps']
    assert result['controls'][0]['edge_at_unknown_onset'] is None


def test_window_prefix_and_reassertion_keep_distinct_evidence(tmp_path):
    parser = fixture(tmp_path, repeat=True)
    result = observe(parser, time=16)
    assert result['controls'][0]['observed_edge_count'] == 2
    assert result['controls'][0]['last_observed_edge_ps'] == 14
    assert result['controls'][0]['edge_at_unknown_onset'] is True
    result = observe(parser, start=16, time=18)
    assert result['controls'][0]['assertion_status'] == 'active_without_observed_assertion'
    assert result['controls'][0]['edge_at_unknown_onset'] is None
    assert 'async_assertion_before_window_or_unrecorded' in result['gaps']


def test_async_observation_cancellation_and_private_contract_validation(tmp_path):
    parser = fixture(tmp_path)
    cancel = threading.Event()
    token = push_cancel_event(cancel)
    cancel.set()
    try:
        with pytest.raises(OperationCancelled):
            observe(parser)
    finally:
        pop_cancel_event(token)
    for bad in ({**step(), 'complete': True}, {**step(), 'clock': None},
                {**step(), 'async_controls': step()['async_controls'] * 3}):
        with pytest.raises(ValueError, match='dynamic_async_controls_invalid'):
            validate_step(bad)
    bad = step()
    bad['async_controls'][0]['assertion_edge'] = 'posedge'
    with pytest.raises(ValueError, match='dynamic_async_controls_invalid'):
        validate_step(bad)
