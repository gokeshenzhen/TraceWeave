"""Waveform execution for trusted property plans under the existing wave lock."""
from __future__ import annotations

from bisect import bisect_right
import json
import time

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from .cancellation import check_cancelled
from .cycle_query import sample_signals_on_edges, _read_sweep_transition_result
from .dynamic_binding import signal_expression
from .dynamic_evidence import Expr, evaluate
from .expression_values import Value
from .event_pages import sampling_anchor
from .property_engine import PropertyEngine, UnsupportedPlan
from .property_evidence import attach_evidence_references
from .scope_metadata import file_identity
from .waveform_selection import SelectionParser


class PropertyReadIncomplete(ValueError):
    def __init__(self,code,gaps=()):
        super().__init__(code)
        self.gap_codes=sorted({code,*gaps})


class PropertyCheckOptions(BaseModel):
    model_config=ConfigDict(extra='forbid')
    wave_path: str = Field(min_length=1,max_length=4096)
    mode: str = 'property'
    property_plan: str = Field(min_length=1,max_length=128)
    failure_event: dict | None = None
    failure_time_ps: StrictInt | str | None = None
    log_path: str | None = Field(default=None,max_length=4096)
    simulator: str = 'auto'
    property_scope: str | None = Field(default=None,max_length=1024)
    property_bindings: dict[str,str|dict] = Field(default_factory=dict,max_length=128)
    max_edges: StrictInt = Field(default=16384,ge=16,le=65536)
    max_attempts: StrictInt = Field(default=1024,ge=1,le=4096)
    max_work: StrictInt = Field(default=200000,ge=100,le=2000000)
    max_output_bytes: StrictInt = Field(default=131072,ge=4096,le=524288)
    timeout_sec: float = Field(default=20,gt=0,le=120,allow_inf_nan=False)

    @model_validator(mode='after')
    def check(self):
        if self.mode!='property':raise ValueError('property_mode_required')
        if self.failure_event is None and self.failure_time_ps is None and not self.log_path:
            raise ValueError('property_failure_anchor_required')
        if self.failure_event is not None and self.failure_time_ps is not None:
            raise ValueError('conflicting_property_failure_anchors')
        if self.simulator not in {'auto','vcs','xcelium','verilator'}:raise ValueError('invalid_simulator')
        return self


def failure_from_log(options,plan):
    from .log_parser import SimLogParser
    requested=(options.failure_event or {}).get('instance_path')
    target=plan.get('target') or {}
    if requested is None and target.get('kind')=='directive':
        requested=target.get('path')
        if requested and requested.startswith('__tw_property.'):
            requested=(options.property_scope+'.'+target['name']) if options.property_scope and target.get('name')!='__tw_target' else None
    if not requested:raise ValueError('property_failure_instance_required')
    events=SimLogParser(options.log_path,options.simulator).parse_failure_events()
    selected=[e for e in events if e.get('instance_path')==requested and
              (e.get('failure_source')=='assertion' or e.get('group_signature','').startswith('ASSERTION_FAIL:'))]
    if not selected:raise ValueError('property_failure_event_not_found')
    if any(type(e.get('time_ps')) is not int or e.get('time_parse_status')!='exact' for e in selected):
        raise ValueError('property_failure_time_uncertain')
    first=min(selected,key=lambda e:(e['time_ps'],e.get('line_num',0)))
    if options.failure_time_ps is not None and first['time_ps']!=options.failure_time_ps:
        raise ValueError('property_failure_anchor_mismatch')
    if options.failure_event is not None and any(options.failure_event.get(k) is not None and
        options.failure_event[k]!=first.get(k) for k in ('event_id','time_ps','instance_path')):
        raise ValueError('property_failure_anchor_mismatch')
    return dict(first,selection_basis='first_target_failure_from_log')


def validate_failure(event,plan):
    if type(event.get('time_ps')) is not int or not 0<=event['time_ps']<2**63:
        raise ValueError('property_failure_time_required')
    if event.get('time_parse_status','exact')!='exact':raise ValueError('property_failure_time_uncertain')
    if event.get('failure_source','assertion')!='assertion':raise ValueError('property_assertion_event_required')
    fields=event.get('structured_fields') or event.get('parsed_fields') or {}
    start=fields.get('start_time_ps',event.get('start_time_ps'))
    if start is not None and (type(start) is not int or not 0<=start<=event['time_ps']):
        raise ValueError('property_attempt_start_invalid')
    target=plan.get('target') or {}
    if (target.get('kind')=='directive' and target.get('path') and
            not target['path'].startswith('__tw_property.') and event.get('instance_path') and
            event['instance_path']!=target['path']):
        raise ValueError('property_failure_target_mismatch')


def _transparent(plan,node):
    nodes={n['id']:n for n in plan['nodes']}
    for _ in range(128):
        if node['op'] not in {'clock','reference','disable_iff'}:return node
        node=nodes[node['children'][0]]
    raise UnsupportedPlan('property_depth_limit')


def horizons(plan):
    nodes={n['id']:n for n in plan['nodes']};memo={}
    def visit(key):
        check_cancelled()
        if key in memo:return memo[key]
        n=nodes[key];f=n['facts'];op=n['op'];children=n['children']
        spans=[visit(c) for c in children]
        if op=='sample':value=0
        elif op=='concat':
            value=0
            joins={j['right']:j['delay']['max'] for j in f['joins']}
            for i,c in enumerate(children):
                delay=joins.get(c,0)
                if value is None or delay is None or spans[i] is None:value=None
                else:value+=delay+spans[i]
        elif op in {'OverlappedImplication','NonOverlappedImplication'}:
            value=None if None in spans else sum(spans)+int(op=='NonOverlappedImplication')
        elif op in {'Throughout','Within'}:value=spans[-1]
        elif op in {'NextTime','SNextTime'}:
            offset=(f.get('range') or {}).get('max',1)
            value=None if offset is None or spans[0] is None else offset+spans[0]
        elif op in {'Always','SAlways','Eventually','SEventually','Until','SUntil','UntilWith','SUntilWith'}:
            offset=(f.get('range') or {}).get('max')
            value=None if offset is None or None in spans else offset+max(spans)
        else:value=None if None in spans else max(spans,default=0)
        rep=f.get('repetition')
        if rep:
            count=rep['count']['max']
            value=None if rep['kind']!='Consecutive' or count is None or value is None else count*(value+1)-1
        memo[key]=value
        return value
    maximum=visit(plan['root'])
    root=_transparent(plan,nodes[plan['root']]);exact=None
    if root['op'] in {'OverlappedImplication','NonOverlappedImplication'}:
        a=_transparent(plan,nodes[root['children'][0]])
        b=_transparent(plan,nodes[root['children'][1]])
        if a['op']=='sample' and not a['facts'].get('repetition'):
            offset=int(root['op']=='NonOverlappedImplication')
            if b['op']=='sample' and not b['facts'].get('repetition'):exact=offset
            elif b['op']=='concat' and len(b['children'])==1:
                leaf=_transparent(plan,nodes[b['children'][0]])
                joins=b['facts']['joins']
                delay=joins[0]['delay']['max'] if joins else 0
                if leaf['op']=='sample' and not leaf['facts'].get('repetition') and delay is not None:
                    exact=offset+delay
    history=0
    for h in plan.get('histories',{}).values():
        if h.get('gate'):return maximum,exact,None
        history+=h.get('ticks',1)
    return maximum,exact,history


def bind_signals(parser,plan,options,event):
    adapter=SelectionParser(parser);keys={};receipts=[]
    supplied=options.property_bindings
    leaves={}
    for source in plan['signals']:leaves.setdefault(source.rsplit('.',1)[-1],[]).append(source)
    names={}
    for name,value in supplied.items():
        source=name if name in plan['signals'] else leaves.get(name,[None])[0] if len(leaves.get(name,[]))==1 else None
        if source is None or source in names:raise ValueError('property_binding_name_ambiguous')
        names[source]=value
    scope=options.property_scope
    if scope is None and event.get('instance_path') and any(s.startswith('__tw_property.') for s in plan['signals']):
        scope=event['instance_path'].rsplit('.',1)[0]
    original_scope=(plan.get('target') or {}).get('path','').rsplit('.',1)[0]
    for source,shape in plan['signals'].items():
        check_cancelled()
        target=names.get(source)
        if target is None:
            target=source
            prefix='__tw_property' if source.startswith('__tw_property.') else original_scope
            if scope and prefix and source.startswith(prefix+'.'):target=scope+source[len(prefix):]
        if isinstance(target,str):
            bound=signal_expression(parser,target,bits=tuple(shape['bits']),declared_bits=tuple(shape['bits']))
            selection=dict(path=bound.signal,bits=list(bound.bits))
        else:selection=target
        key=adapter.bind(selection)
        if adapter.projections[key].selection.width!=shape['width']:raise ValueError('property_binding_width_mismatch')
        keys[source]=key
        receipts.append(dict(source=source,selection=selection,width=shape['width']))
    return adapter,keys,receipts


def _clock_window(parser,clock,edge,fail,needed,max_edges,deadline,required_start=None,history_edges=0):
    width=max(1,min(fail or 1,1000));seen=set();gaps=set()
    def read(start):
        check_cancelled()
        if time.monotonic()>=deadline:raise TimeoutError('property_timeout')
        result=sample_signals_on_edges(parser,clock,[],start_ps=start,end_ps=fail,edge=edge,
            sample_phase='before',sample_offset_ps=0,compact=True,max_edges=max_edges)
        gaps.update(result.get('sampling_gaps',[]))
        return result
    if required_start is not None and history_edges==0:
        # The logged attempt bounds all required clock evidence. Include the
        # preceding public ps because labels are ceilings of physical fs times.
        start=max(0,required_start-1)
        sampled=read(start)
        if (not sampled.get('transition_data_truncated') and not sampled.get('sample_limit_reached')
                and not sampled.get('sampling_gaps')
                and sampled['edge_times'].count(required_start)==1):
            return start,sampled
    for _ in range(64):
        check_cancelled()
        if time.monotonic()>=deadline:raise TimeoutError('property_timeout')
        start=max(0,fail-width)
        if start in seen:raise PropertyReadIncomplete('property_clock_read_limit',gaps)
        seen.add(start)
        sampled=read(start)
        if sampled.get('transition_data_truncated') or sampled.get('sample_limit_reached'):
            if width<=1:raise PropertyReadIncomplete('property_clock_read_limit',gaps)
            width=max(1,width//2);continue
        if (len(sampled['edge_times'])>=needed or start==0) and (required_start is None or start<=required_start):return start,sampled
        width*=2
    raise PropertyReadIncomplete('property_clock_read_limit',gaps)


def _async_controls(parser,plan,keys,engine,start,end,deadline):
    controls={};records=nbytes=0
    for node in plan['nodes']:
        if node['op'] not in {'disable_iff','accept_on','reject_on'}:continue
        raw=node['facts']['condition'].get('execution',{}).get('expression')
        if raw is None:raise UnsupportedPlan('abort_expression_unavailable')
        expression=Expr.from_dict(raw);dependencies=set();stack=[expression];sampled_functions=False
        visited_history=set()
        while stack:
            part=stack.pop()
            if part.op=='signal' and part.signal.startswith('@history:'):
                sampled_functions=True
                if part.signal not in visited_history:
                    visited_history.add(part.signal)
                    history=plan['histories'][part.signal]
                    if history['function']=='$sampled':stack.append(engine._expr(history['expression']))
            elif part.op=='signal':
                if part.signal in engine.locals:raise UnsupportedPlan('local_abort_condition_unsupported')
                dependencies.add(part.signal)
            stack.extend(part.args)
        states={};events={}
        for source in dependencies:
            check_cancelled()
            if time.monotonic()>=deadline:raise TimeoutError('property_timeout')
            result=_read_sweep_transition_result(parser,keys[source],start,end,kind='signal',sample_phase='before')
            if result.get('truncated') or result.get('sampling_gaps'):
                raise PropertyReadIncomplete('property_abort_transitions_incomplete',result.get('sampling_gaps',[]))
            anchor=sampling_anchor(result)
            states[source]=(anchor.get('value') or {}).get('bin') if anchor else None
            for row in result.get('transitions',[]):
                engine.charge();records+=1
                nbytes+=64+len((row.get('value') or {}).get('bin') or '')
                if records>65536 or nbytes>16*1024*1024:raise ValueError('property_abort_event_limit')
                at=row.get('time_fs',row['time_ps']*1000)
                events.setdefault(at,[]).append((source,(row.get('value') or {}).get('bin')))
        edges=[s['time_fs'] for s in engine.samples]
        if sampled_functions:
            for at in edges:events.setdefault(at,[])
        def condition(at,pre_states):
            tick=bisect_right(edges,at)-1
            def sample(part,values=states):
                engine.charge()
                if part.signal.startswith('@history:'):
                    history=plan['histories'][part.signal]
                    if history['function']=='$sampled':
                        # Async conditions are unsampled expressions. $sampled
                        # observes the preponed state of this physical time slot,
                        # rather than retaining the last assertion clock sample.
                        observed=evaluate(engine._expr(history['expression']),lambda p:sample(p,pre_states))
                        return dict(value=observed.value,gaps=observed.gaps,
                                    unavailable_reason='property_history_value_unavailable')
                    value=engine.history(part.signal,tick,{}) if tick>=0 else None
                    return dict(value=None if value is None else value.bits,
                                unavailable_reason='property_history_value_unavailable')
                bits=values.get(part.signal);shape=plan['signals'][part.signal]
                if bits is None:return dict(value=None)
                positions={bit:i for i,bit in enumerate(shape['bits'])}
                return dict(value=''.join(bits[positions[b]] for b in part.bits))
            observed=evaluate(expression,sample)
            return None if observed.value is None else Value(observed.value,expression.signed)
        timeline=[(start*1000-1,condition(start*1000-1,states))]
        for at,changes in sorted(events.items()):
            check_cancelled()
            pre_states=states.copy()
            seen={};ambiguous=False
            for source,bits in changes:
                ambiguous|=source in seen and seen[source]!=bits
                seen[source]=bits;states[source]=bits
            timeline.append((at,None if ambiguous else condition(at,pre_states)))
        controls[node['id']]=timeline
    return controls


def _trim(result,max_bytes):
    def size():return len(json.dumps(result,separators=(',',':'),ensure_ascii=False).encode())
    if size()<=max_bytes:return result
    result['output_truncated']=True
    for attempt in reversed(result['attempts']):
        attempt['evidence']=[];attempt['evidence_truncated']=True
        attempt['obligations']=[];attempt['obligations_truncated']=True
        if size()<=max_bytes:return result
        observed=attempt.get('sampled_values',[])
        if observed:
            attempt['sampled_values']=[];attempt['sampled_values_truncated']=True
            if size()<=max_bytes:
                lo,hi=0,len(observed)
                while lo<hi:
                    check_cancelled();mid=(lo+hi+1)//2
                    attempt['sampled_values']=observed[:mid]
                    if size()<=max_bytes:lo=mid
                    else:hi=mid-1
                attempt['sampled_values']=observed[:lo]
                return result
        if attempt.get('clock_sample') is not None:
            attempt['clock_sample']=None;attempt['clock_sample_truncated']=True
            if size()<=max_bytes:return result
    result['bindings']=[];result['bindings_truncated']=True
    result['suggested_next_action']=None
    while result['attempts'] and size()>max_bytes:
        result['attempts'].pop();result['attempts_truncated']=True
    if size()>max_bytes:raise ValueError('property_output_limit')
    return result


def check_property(parser,plan,options,event,*,deadline):
    check_cancelled()
    if time.monotonic()>=deadline:raise TimeoutError('property_timeout')
    before=file_identity(options.wave_path)
    adapter,keys,bindings=bind_signals(parser,plan,options,event)
    clock=plan['clocks'][0];source=clock['expression']['execution']['expression']['signal']
    edge='posedge' if clock['edge']=='PosEdge' else 'negedge'
    fail=event['time_ps']
    fields=event.get('structured_fields') or event.get('parsed_fields') or {}
    started=fields.get('start_time_ps',event.get('start_time_ps'))
    maximum,exact,history=horizons(plan)
    needed=(maximum+1 if maximum is not None else options.max_attempts)+(history or 0)+2
    requested=min(options.max_edges,max(2,needed))
    required_start=0 if history is None else started
    start,probe=_clock_window(adapter,keys[source],edge,fail,requested,options.max_edges,deadline,required_start,history)
    times=probe['edge_times'];anchors=[i for i,t in enumerate(times) if t==fail]
    if len(anchors)!=1:raise ValueError('property_failure_clock_ambiguous' if anchors else 'property_failure_clock_not_found')
    target=anchors[0]
    if history is not None and maximum is not None and target>=maximum+history:
        first=target-maximum-(history or 0)
        start=probe['edge_times_fs'][first]//1000
        if started is not None:start=min(start,started)
    sampled=sample_signals_on_edges(adapter,keys[source],list(keys.values()),start_ps=start,end_ps=fail,
        edge=edge,sample_phase='before',sample_offset_ps=0,compact=True,max_edges=options.max_edges)
    if sampled.get('transition_data_truncated') or sampled.get('sample_limit_reached') or sampled.get('sampling_gaps'):
        raise PropertyReadIncomplete('property_sampling_incomplete',sampled.get('sampling_gaps',[]))
    if sampled.get('signal_errors') or any(k not in sampled['signal_columns'] for k in keys.values()):
        raise ValueError('property_signal_unavailable')
    times=sampled['edge_times'];raw_times=sampled['edge_times_fs']
    anchors=[i for i,t in enumerate(times) if t==fail]
    if len(anchors)!=1:raise ValueError('property_failure_clock_ambiguous')
    target=anchors[0]
    samples=[dict(time_ps=at,time_fs=raw_times[i],signals={s:sampled['signal_columns'][key][i] for s,key in keys.items()})
             for i,at in enumerate(times)]
    # No arbitrary read-window boundary becomes the first simulation clock.
    engine=PropertyEngine(plan,samples,history_complete=False,
        timeout_sec=max(.001,deadline-time.monotonic()),max_work=options.max_work)
    engine.controls=_async_controls(adapter,plan,keys,engine,start,fail,deadline)
    complete=True;basis='bounded_temporal_horizon'
    if started is not None:
        candidates=[i for i,t in enumerate(times) if t==started]
        if len(candidates)!=1:raise ValueError('property_start_clock_not_found_or_ambiguous')
        basis='logged_attempt_start'
    elif exact is not None:
        candidates=[target-exact] if target>=exact else []
        basis='response_deadline'
        complete=bool(candidates)
    else:
        first=max(0,target-maximum) if maximum is not None else 0
        candidates=list(range(first,target+1))
        complete=maximum is not None and (target>=maximum or start==0)
    if len(candidates)>options.max_attempts:
        candidates=candidates[-options.max_attempts:];complete=False
    attempts=[];reproduced=0
    for i in candidates:
        check_cancelled()
        attempt=engine.attempt(i)
        # This is the actual pre-edge column value, not the posedge witness 1.
        clock_value=samples[i]['signals'][source]
        clock_bits=clock_value.get('bin') if isinstance(clock_value,dict) else clock_value
        attempt['clock_sample']=(dict(source=source,tick=i,time_ps=times[i],time_fs=raw_times[i],
            value={'bin':clock_bits}) if clock_bits is not None else None)
        attempt['clock_sample_truncated']=False
        observations=attempt['sampled_values']+([attempt['clock_sample']] if attempt['clock_sample'] else [])
        for observed in observations:
            projection=adapter.projections[keys[observed['source']]]
            observed['signal']=projection.path
            if projection.selection.bits!=projection.declared.indices:
                observed['bits']=list(projection.selection.bits)
        attempt['start_time_ps']=times[i]
        end=attempt['end_index']
        raw_end=attempt.get('end_time_fs')
        if raw_end is None and end is not None and 0<=end<len(times):raw_end=raw_times[end]
        attempt['end_time_fs']=raw_end
        attempt['end_time_ps']=(raw_end+999)//1000 if raw_end is not None else None
        same_tick=attempt['end_time_ps']==fail
        same_attempt=started is not None and attempt['end_time_ps'] is not None and attempt['end_time_ps']<=fail
        attempt['matches_failure_event']=attempt['status']=='false' and (same_tick or same_attempt)
        attempt['report_timing_exact']=same_tick
        attempt['report_alignment']='same_tick' if same_tick else 'same_logged_attempt' if same_attempt else 'not_aligned'
        reproduced+=int(attempt['matches_failure_event'])
        attempts.append(attempt)
        if attempt['analysis_limited']:complete=False;break
    uncertain=any(a['status'] in {'unknown','pending'} for a in attempts)
    if started is None and any(a['status']=='false' and a['end_time_ps'] is not None and a['end_time_ps']<fail for a in attempts):
        uncertain=True;engine.gaps.add('property_failure_attempt_correlation_required')
    status='failure_reproduced' if reproduced else 'not_reproduced' if complete and not uncertain else 'insufficient_evidence'
    if not candidates:status='insufficient_evidence'
    result=dict(mode='property',wave_path=options.wave_path,clock=bindings[list(keys).index(source)]['selection']['path'],
        edge=edge,status=status,execution_status='completed' if complete and not uncertain else 'partial',
        coverage_status='complete' if complete and not uncertain else 'partial' if attempts else 'zero_coverage',
        plan=dict(version=plan['version'],fingerprint=plan['fingerprint'],identity={k:v for k,v in (plan.get('identity') or {}).items()
            if k not in {'source_files','compile_log'}}),
        target={k:v for k,v in (plan.get('target') or {}).items() if k!='source'},
        failure_event={k:event[k] for k in ('event_id','time_ps','instance_path','source_file','source_line','selection_basis') if k in event},
        attempt_selection=dict(basis=basis,candidate_count=len(candidates),checked_count=len(attempts),complete=complete),
        sampling=dict(phase='before',offset_ps=0,time_domain='raw_fs',
            equivalence='physical_pre_edge; simulator_delta_regions_not_recorded',
            async_sampled='pre_timestamp_group_witnesses',
            expression_values='evaluated_expression_results; not raw signal values',
            sampled_values='raw bound input reads; bounded, not an exhaustive dependency inventory',
            missing_values='unavailable evidence is not an observed X/Z value',
            history_start_ps=start,read_end_ps=fail,actual_simulation_end='not_assumed'),
        bindings=bindings,bindings_truncated=False,attempts=attempts,attempts_truncated=False,
        reproduced_count=reproduced,gap_codes=sorted(engine.gaps),
        work_steps=engine.work,match_count=engine.match_count,output_truncated=False)
    if started is not None:result['failure_event']['start_time_ps']=started
    if not complete:result['gap_codes'].append('property_candidate_history_incomplete')
    if any(a['status']=='pending' for a in attempts):
        result['gap_codes'].append('property_pending_at_report_cutoff')
        def selection(key):
            p=adapter.projections[key]
            return p.path if p.selection.bits==p.declared.indices else dict(path=p.path,bits=list(p.selection.bits))
        result['suggested_next_action']=dict(tool='get_signals_by_cycle',
            reason='property_pending_at_report_cutoff',
            note='Inspect the same attempt beyond the report cutoff if captured. Check coverage and temporal endpoints; pending is not a pass, and later failure is not failure at the reported timestamp.',
            arguments=dict(wave_path=options.wave_path,clock_path=selection(keys[source]),
                signal_paths=[selection(k) for k in dict.fromkeys(keys.values())],edge=edge,
                start_time_ps=max(0,min(a['start_time_ps'] for a in attempts)-1),
                cycle_index_origin='window',num_cycles=min(256,options.max_edges,max(16,(maximum or 0)+(history or 0)+1)),
                sample_phase='before',sample_offset_ps=0))
    attach_evidence_references(result,deadline=deadline)
    if before!=file_identity(options.wave_path):raise ValueError('property_wave_identity_changed')
    for source,key in keys.items():
        adapter._validate(adapter.projections[key])
    return _trim(result,options.max_output_bytes-512)
