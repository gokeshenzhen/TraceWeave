"""Native assertion logs are independent references for waveform plan replay."""
import asyncio
import os
from pathlib import Path

import pytest
import server
from src.log_parser import SimLogParser

SOURCE=Path(__file__).parent/'fixtures/property_waveform/oracle.sv'
# Reviewed native VCS first failures. Xcelium reports the statically impossible
# intersection at 15ns instead of 5ns for the same attempt started at 5ns.
# Positive/disabled cases are replayed too, rather than treating no log as proof.
FAIL_NS={
    'ap_exact':35,'ap_all':35,'ap_throughout':15,'ap_intersect':5,
    'ap_within_bad':35,'ap_repeat':15,'ap_occurrence':35,'ap_preponed':5,
    'ap_x':5,'ap_stable':15,'ap_past':45,'ap_tied':25,
    'ap_until_right_fail':15,'ap_until_left_fail':15,
}
PASS={'ap_range','ap_first','ap_and','ap_within','ap_empty','ap_case','ap_reset','ap_sampled_reset',
      'ap_until_right_pass'}


def build_oracle():
    from scripts.test_fixtures import current_cache
    return current_cache().vcs_design('property-temporal-oracle',{'oracle.sv':SOURCE.read_text()},
                                       'property_oracle',simulate=True,kdb=False)


def fsdb_oracle(path):
    from scripts.test_fixtures import current_cache,identity,run
    converter=Path(os.environ['VERDI_HOME'])/'bin/vcd2fsdb'
    inputs=identity([path/'oracle.vcd',converter],format='fsdb',sequence_numbers=True)
    def build(directory):
        return run([converter,path/'oracle.vcd','-seq_num','-o',directory/'oracle.fsdb'],directory,
            env=dict(os.environ,FSDB_ENV_MAX_GLITCH_NUM='0'),timeout=30)
    return current_cache().get('property-temporal-fsdb',inputs,build)/'oracle.fsdb'


@pytest.mark.eda_integration
@pytest.mark.parametrize('format',['vcd','fsdb'])
def test_vcs_native_outcomes_replay_over_waveform(format):
    path=build_oracle()
    waveform=path/'oracle.vcd' if format=='vcd' else fsdb_oracle(path)
    events=SimLogParser(str(path/'sim.log'),'vcs').parse_failure_events()
    first={}
    for event in events:
        name=(event.get('instance_path') or '').rsplit('.',1)[-1]
        if name in FAIL_NS:first.setdefault(name,event)
    assert set(first)==set(FAIL_NS),[(e.get('instance_path'),e.get('message_text')) for e in events]
    assert {name:e['time_ps'] for name,e in first.items()}=={k:v*1000 for k,v in FAIL_NS.items()}
    assert not {e.get('instance_path','').rsplit('.',1)[-1] for e in events}&PASS
    async def replay():
        server.reset_session_state()
        cases=dict(first)
        cases.update({name:dict(time_ps=35000,instance_path='property_oracle.'+name,
            structured_fields=dict(start_time_ps=5000)) for name in PASS})
        for name,event in cases.items():
            explained=await server._dispatch('explain_property',dict(source_file=str(path/'oracle.sv'),
                target='property_oracle.'+name,include_check_plan=True,detail='summary'))
            plan=explained.results[0].check_plan
            assert plan and plan['status']=='prepared',(name,explained)
            result=await server._dispatch('verify_window',dict(mode='property',property_plan=plan['handle'],
                wave_path=str(waveform),failure_event=event))
            if name in first:
                assert result.status=='failure_reproduced',(name,result)
                assert result.reproduced_count>=1
                assert any(a['matches_failure_event'] for a in result.attempts)
            else:
                assert result.status=='not_reproduced',(name,result)
                expected='disabled' if name in {'ap_reset','ap_sampled_reset'} else 'true'
                assert result.attempts[0]['status']==expected,(name,result)
        server.reset_session_state()
    asyncio.run(replay())


def test_oracle_has_disjoint_identified_outcomes():
    assert not set(FAIL_NS)&PASS
    assert len(FAIL_NS)+len(PASS)==23
    text=SOURCE.read_text()
    for name in set(FAIL_NS)|PASS:assert name+': assert property' in text
