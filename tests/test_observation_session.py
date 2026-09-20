"""Independent sampling values and cache-bypass coverage oracles."""
from pathlib import Path
import threading

import pytest

from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.dynamic_evidence import Expr
from src.dynamic_observe import ObservationReader
from src.observation_session import ObservationSession, selection_identity
from src.vcd_parser import VCDParser
from src.waveform_selection import SelectionParser


def fixture(tmp_path):
    path=tmp_path/'sample.vcd'
    path.write_text('''$timescale 1ps $end
$scope module top $end
$var wire 4 ! data [7:4] $end
$var wire 4 ! alias [7:4] $end
$var wire 1 @ clk $end
$upscope $end
$enddefinitions $end
#0
b1001 !
0@
#5
b1010 !
1@
#10
bxxz1 !
0@
#20
''')
    parser=VCDParser(str(path))
    counts=[]
    original=parser.get_transitions
    def read(*a,**kw):
        counts.append(a)
        return original(*a,**kw)
    parser.get_transitions=read
    return parser,counts


def expr(signal='top.data', bits=(7,6,5,4)):
    return Expr('signal',len(bits),signal=signal,bits=bits,declared_bits=(7,6,5,4))


def reader(p, session, start=0, end=20):
    return ObservationReader(lambda _:p,p.file_path,start,end,session=session)


def test_request_cache_reuses_alias_and_subwindow_without_changing_predecessor(tmp_path):
    p,counts=fixture(tmp_path);session=ObservationSession()
    assert reader(p,session).sample(expr(),6)['value']=='1010'
    sub=reader(p,session,6,8)
    value=sub.sample(expr('top.alias'),7)
    assert value['value']=='1010' and value['predecessor_time_ps']==5
    assert len(counts)==1 and session.hits
    stream=sub.stream('top.alias')
    assert not stream.transitions and stream.predecessor['time_ps']==5
    with pytest.raises(TypeError):stream.predecessor['value']['bin']='0000'


def test_phase_time_offset_and_ordered_bits_remain_separate(tmp_path):
    p,_=fixture(tmp_path);session=ObservationSession();r=reader(p,session)
    assert r.sample(expr(),5,'after')['value']=='1010'
    before=r.sample(expr(),5,'before')
    assert before['value']=='1001' and before['gaps']==['sampling_order_unresolved']
    assert r.sample(expr(),4,'after')['value']=='1001'
    assert r.sample(expr(),4,'after',offset=1)['value']=='1010'
    assert r.sample(expr(bits=(4,7)),5)['value']=='01'
    assert r.sample(expr(bits=(7,4)),5)['value']=='10'
    assert r.sample(expr(),11)['value']=='xxz1'
    cached=r.sample(expr(),5,'after');cached['value']='1111'
    assert r.sample(expr(),5,'after')['value']=='1010'


@pytest.mark.parametrize('limits',[dict(max_bytes=1),dict(max_events=1),dict(max_bytes=6000,max_events=4)])
def test_budget_bypass_eviction_preserves_values_and_gaps(tmp_path,limits):
    p,counts=fixture(tmp_path);session=ObservationSession(**limits)
    for time in (4,5,11,4):
        actual=reader(p,session).sample(expr(),time,'before')
        expected=reader(p,None).sample(expr(),time,'before')
        assert actual==expected
    assert session.peak_bytes<=session.max_bytes and session.peak_events<=session.max_events
    assert session.bypasses or session.evictions


def test_new_request_and_artifact_restart_do_not_share_observations(tmp_path):
    p,counts=fixture(tmp_path);session=ObservationSession()
    session.bind(('design','backend','artifact1'))
    reader(p,session).sample(expr(),4)
    reader(p,session).sample(expr(),4)
    assert len(counts)==1
    session.bind(('design','backend','artifact2'))
    assert not session.entries
    reader(p,session).sample(expr(),4)
    reader(p,ObservationSession()).sample(expr(),4)
    assert len(counts)==3
    session.clear()
    assert session.events==session.nbytes==len(session.entries)==0


def test_replacement_and_parser_generation_do_not_return_cached_value(tmp_path):
    p,counts=fixture(tmp_path);session=ObservationSession()
    assert reader(p,session).sample(expr(),4)['value']=='1001'
    path=Path(p.file_path)
    path.write_text(path.read_text().replace('b1001','b1111'))
    fresh=VCDParser(str(path))
    assert reader(fresh,session).sample(expr(),4)['value']=='1111'
    again=VCDParser(str(path))
    before=session.misses
    assert reader(again,session).sample(expr(),4)['value']=='1111'
    assert session.misses==before+1


def test_display_key_is_not_complete_projection_identity(tmp_path):
    raw,_=fixture(tmp_path)
    a,b=SelectionParser(raw),SelectionParser(raw)
    ka=a.bind({'path':'top.data','bits':[4,7]})
    kb=b.bind({'path':'top.data','bits':[7,4]})
    # A display collision cannot alias two different declared-coordinate maps.
    b.projections[ka]=b.projections[kb]
    assert selection_identity(a,ka)!=selection_identity(b,ka)


def test_cached_hit_observes_cancellation(tmp_path):
    p,_=fixture(tmp_path);session=ObservationSession()
    reader(p,session).sample(expr(),4)
    event=threading.Event();token=push_cancel_event(event)
    try:
        event.set()
        with pytest.raises(OperationCancelled):reader(p,session).sample(expr(),4)
    finally:pop_cancel_event(token)


def test_truncated_windows_never_enter_cache_or_prove_known_tail(tmp_path):
    p,counts=fixture(tmp_path);session=ObservationSession()
    original=p.get_transitions
    def truncated(*a,**kw):
        return {**original(*a,**kw),'truncated':True}
    p.get_transitions=truncated
    for _ in range(2):
        result=reader(p,session).sample(expr(),11)
        assert result['value'] is None and 'transition_data_truncated' in result['gaps']
    assert len(counts)==2 and not session.entries
