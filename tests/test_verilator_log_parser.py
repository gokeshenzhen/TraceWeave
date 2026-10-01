"""Actual Verilator 5.034 native/SoC/cocotb output and adversarial boundaries."""
from pathlib import Path
import threading

import pytest

import server
from src.log_parser import SimLogParser
from src.analyzer import WaveformAnalyzer
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.verilator_log_parser import time_fields

FIXTURES = Path(__file__).parent / 'fixtures/verilator_logs'


def parser(name, unit=None):
    return SimLogParser(str(FIXTURES / (name + '.log')), 'verilator', unit)


def log(tmp_path, text, unit=None):
    path = tmp_path / 'sim.log'
    path.write_text(text)
    return SimLogParser(str(path), 'verilator', unit)


@pytest.mark.parametrize('name', ['xheep_pass', 'opentitan_pass', 'cocotb_warnings', 'led_normal'])
def test_actual_nonfailures(name):
    assert parser(name).parse_failure_events() == []


def test_actual_native_and_stop_are_one_event():
    events = parser('native_fail').parse_failure_events()
    assert len(events) == 1
    event = events[0]
    assert (event['raw_time'], event['time_ps'], event['raw_time_unit'], event['time_parse_status']) == ('1000', None, None, 'unresolved')
    assert (event['source_file'], event['source_line'], event['instance_path']) == ('native.sv', 10, 'TOP.native.check_data')
    assert (event['expected'], event['actual']) == ('10', '11')
    event = parser('native_fail', 'ps').parse_failure_events()[0]
    assert event['time_ps'] == 1000
    assert event['structured_fields']['native_time_unit_source'] == 'caller_verified'


def test_xheep_software_exit_and_checker_are_separate():
    events = parser('xheep_fail').parse_failure_events()
    assert len(events) == 5
    assert events[0]['time_ps'] == 1553035000
    assert events[0]['structured_fields']['software_exit_value'] == 1
    assert [e['expected'] for e in events[1:]] == ['0x10', '0x1f', '0xfe', '0xbb']
    assert all(e['time_ps'] is None for e in events[1:])
    summary = parser('xheep_fail').parse(max_groups=1)
    assert summary['runtime_total_errors'] == 5 and summary['total_groups'] == 2
    assert summary['truncated']
    assert parser('xheep_timeout').parse_failure_events()[0]['time_ps'] == 50000020000


def test_actual_opentitan_detectors_are_not_rtl_fault_locations():
    events = parser('opentitan_fail', 'ps').parse_failure_events()
    assert len(events) == 3
    assert events[0]['time_ps'] == 2641452
    assert events[0]['source_file'] == 'sw_test_status_if.sv'
    assert events[1]['source_file'] == 'aes_testutils.c'
    assert (events[1]['expected'], events[1]['actual']) == ('0xf3', '0xf2')
    assert all(e['time_ps'] is None for e in events[1:])


def test_actual_cocotb_traceback():
    event, = parser('cocotb_fail').parse_failure_events()
    assert event['time_ps'] == 1250
    assert (event['expected'], event['actual']) == ('0x10', '0x11')
    assert event['source_file'].endswith('test_data.py') and event['source_line'] == 7
    assert 'Traceback' in event['message_text']


@pytest.mark.parametrize('text', [
    '%Warning-WIDTH: dut.sv:1: error in example\n    1 | $error("bad");\n',
    'ERROR:cocotb:%Error-PINNOTFOUND: dut.sv:1: Pin not found\n',
    '%Error: dut.sv:1: syntax error, unexpected end\n%Error: Exiting due to 1 error(s)\n',
    'ERROR:cocotb: DeprecationWarning: error API\n',
    '$error("[1] %Error: dut.sv:2: Assertion failed in TOP.dut: bad");\n',
    '- dut.sv:9: Verilog $finish\nProgram Finished with value 0\n',
    'Simulation finished after 200 clock cycles\nTEST PASSED\n',
])
def test_compile_echo_and_host_severity_do_not_define_runtime(tmp_path, text):
    assert log(tmp_path, text).parse_failure_events() == []


def test_independent_same_time_errors_and_unpaired_stop(tmp_path):
    p = log(tmp_path, '[5 ns] %Error: a.sv:1: Assertion failed in TOP.a: bad\n'
            '[5 ns] %Error: a.sv:2: Assertion failed in TOP.b: bad\n'
            '%Error: a.sv:2: Verilog $stop\n%Error: a.sv:3: Verilog $stop\n')
    events = p.parse_failure_events()
    assert len(events) == 3 and events[0]['time_ps'] == events[1]['time_ps'] == 5000
    assert events[2]['time_ps'] is None


@pytest.mark.parametrize('raw,unit,expected', [('9007199254740993.125','ns',9007199254740993125), ('0.0001','ns',1), ('1001','fs',2), ('0','ps',0), ('1.25','ns',1250)])
def test_decimal_time_is_exact_and_ceils_to_ps(raw, unit, expected):
    assert time_fields(raw, unit)['time_ps'] == expected


def test_explicit_unit_wins_and_wallclock_is_not_simulation_time(tmp_path):
    p = log(tmp_path, '[2026-10-01T10:11:12Z INFO host] [1.25 ns] %Error: a.sv:1: Assertion failed in TOP.a: expected=1 actual=2\n', 'us')
    event, = p.parse_failure_events()
    assert event['time_ps'] == 1250 and event['raw_time_unit'] == 'ns'
    assert event['structured_fields']['host_timestamp'].startswith('2026-10-01T')
    event, = log(tmp_path, 'Program Finished with value 2\nSimulation finished after 200 cycles\n').parse_failure_events()
    assert event['time_ps'] is None


def test_uvm_and_custom_keep_verified_time_rules(tmp_path):
    p = log(tmp_path, 'UVM_ERROR a.sv(1) @ 1.25 ns: top [DMA] expected=1 actual=2\n')
    assert p.parse_failure_events()[0]['time_ps'] == 1250
    p = log(tmp_path, 'UVM_ERROR a.sv(1) @ 100: top [DMA] expected=1 actual=2\n')
    assert p.parse_failure_events()[0]['time_ps'] is None
    p = log(tmp_path, 'CHK expected=1 actual=2 time=9007199254740993.125 ns\n')
    import re
    p._custom_patterns = [{'name':'checker','compiled':re.compile('CHK.*')}]
    assert p.parse_failure_events()[0]['time_ps'] == 9007199254740993125


def test_unknown_time_never_queries_wave_zero_and_zero_is_valid(tmp_path):
    class Wave:
        def __init__(self): self.times = []
        def get_signals_around_time(self, signals, center, window, extra):
            self.times.append(center)
            return {}
    p = log(tmp_path, 'Program Finished with value 1\n')
    wave = Wave()
    result = WaveformAnalyzer(p.log_path, wave, 'verilator').analyze(['TOP.sig'])
    assert wave.times == [] and result['wave_context'] is None
    p = log(tmp_path, '[0 ps] %Error: a.sv:1: Assertion failed in TOP.a: bad\n')
    WaveformAnalyzer(p.log_path, wave, 'verilator').analyze(['TOP.sig'])
    assert wave.times == [0]


def test_diff_checker_groups_do_not_depend_on_value(tmp_path):
    p = log(tmp_path, '[0] Expected: 10 Got : 11\n[4] Expected: 1f Got : 1e\n')
    other = tmp_path / 'other.log'; other.write_text('[0] Expected: 10 Got : 12\n')
    diff = p.diff_against(str(other))
    assert diff['persistent_events'] and not diff['new_events']
    assert parser('xheep_fail').diff_against(str(FIXTURES/'xheep_pass.log'))['resolved_events']


def test_snapshot_identity_and_overwrite_diff_include_time_settings(tmp_path):
    p = log(tmp_path, '[10] %Error: a.sv:1: Assertion failed in TOP.a: bad\n')
    args = {'log_path':p.log_path, 'simulator':'verilator', 'native_time_unit':'ps'}
    first = server._handle_parse_sim_log(args)
    p = log(tmp_path, 'Program Finished with value 0\n')
    base, _ = server._resolve_base_events_for_diff({'new_log_path':p.log_path, 'native_time_unit':'ps'}, 'verilator')
    assert base[0]['time_ps'] == 10
    second = server._handle_parse_sim_log(args)
    assert second.previous_log_snapshot_id == first.log_snapshot_id
    with pytest.raises(ValueError, match='native_time_unit differs'):
        server._snapshot_events(first.log_snapshot_id, 'verilator', 'ns')
    assert server._handle_parse_sim_log({**args,'native_time_unit':'ns'}).log_snapshot_id != second.log_snapshot_id


def test_multiline_and_cancellation_budget(tmp_path, monkeypatch):
    import src.verilator_log_parser as vp
    monkeypatch.setattr(vp, 'MAX_UVM_CONTINUATION_LINES', 2)
    event, = log(tmp_path, '1ns INFO cocotb.regression test failed\n Traceback\n continuation\n ignored\n').parse_failure_events()
    assert event['structured_fields']['continuation_truncated']
    cancel = threading.Event(); cancel.set(); token = push_cancel_event(cancel)
    try:
        with pytest.raises(OperationCancelled): parser('xheep_fail').parse_failure_events()
    finally: pop_cancel_event(token)


def test_invalid_unit_is_not_silently_accepted(tmp_path):
    with pytest.raises(ValueError): log(tmp_path, '', 'ticks')
    with pytest.raises(ValueError): SimLogParser('unused', 'vcs', 'ps')


def test_checker_aggregate_without_rows_still_detects_failure(tmp_path):
    event, = log(tmp_path, 'DMA failure: 4 errors out of 16 elements checked\n').parse_failure_events()
    assert event['structured_fields']['comparison_errors'] == 4
    assert event['time_ps'] is None
