"""Admission, independent run attribution and bounded isolated property requests."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
import hashlib
import json
import os
from pathlib import Path
import shlex
import re
import time

from .property_model import ExplainPropertyOptions, ExplainPropertyResult, blocked, gap
from .source_graph_runtime import _PROCESS_COLD_BUILD_LOCK, _terminate_process_group
from .source_graph_session_runtime import _process_rss_kib
from .cancellation import check_cancelled


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def _stamp(path):
    try:
        s=os.stat(path)
        return (s.st_dev,s.st_ino,s.st_size,s.st_mtime_ns,s.st_ctime_ns)
    except OSError:
        return None


class PropertyRunRegistry:
    """Small independent discovery receipts; never a most-recent mode switch."""
    def __init__(self):
        self.entries=OrderedDict()

    def note(self, mode, result):
        r=result.model_dump() if hasattr(result,'model_dump') else result
        groups=('compile_logs','sim_logs','wave_files') if mode=='simulation' else ('formal_logs','wave_files')
        paths={os.path.realpath(e['path']) for group in groups for e in r.get(group,[]) if e.get('path')}
        # Only a selected run directory is an anchor, never an entire ambiguous root.
        directory=r.get('case_dir') if mode=='simulation' else r.get('selected_project_dir')
        if directory: paths.add(os.path.realpath(directory))
        identity=digest([mode,sorted(paths),directory])
        self.entries[identity]=dict(mode=mode, paths=sorted(paths), directory=directory,
                                    stamps={p:_stamp(p) for p in paths if not os.path.isdir(p)})
        self.entries.move_to_end(identity)
        while len(self.entries)>32: self.entries.popitem(last=False)

    def resolve(self, mode, anchor):
        check_cancelled()
        if mode!='auto':
            return dict(value=mode,basis='caller_explicit',run_identity=None)
        if not anchor:
            return dict(value='unknown',basis='no_run_association',run_identity=None)
        anchor=os.path.realpath(anchor)
        matches=[(key,e) for key,e in self.entries.items() if anchor in e['paths']]
        if len(matches)!=1:
            return dict(value='unknown',basis='ambiguous_run_association' if matches else 'run_not_discovered',
                        run_identity=None,candidate_count=len(matches))
        key,e=matches[0]
        for p,s in e['stamps'].items():
            check_cancelled()
            if _stamp(p)!=s:
                return dict(value='unknown',basis='discovery_artifacts_changed',run_identity=key)
        return dict(value=e['mode'],basis='associated_discovery',run_identity=key,
                    discovery_tool='get_sim_paths' if e['mode']=='simulation' else 'get_formal_paths')


def roles(row, mode, role_context):
    row['mode']=dict(mode)
    d=row['directive']; k=d.get('kind','unknown')
    simulation={'Assert':'trajectory_property_check','Assume':'trajectory_property_check',
                'CoverProperty':'observe_property_success','CoverSequence':'observe_sequence_matches',
                'Expect':'blocking_trajectory_property_check','Restrict':'not_a_simulation_constraint'}
    formal={'Assert':'proof_or_counterexample_goal','Assume':'constraint_if_enabled',
            'CoverProperty':'property_witness_search','CoverSequence':'sequence_witness_search',
            'Expect':'tool_model_support_required','Restrict':'constraint_if_supported_and_enabled'}
    d['conditional_roles']={'simulation':simulation.get(k,'definition_or_fragment_only'),
                            'formal':formal.get(k,'definition_or_fragment_only')}
    d['role']=d['conditional_roles'].get(mode['value'],'mode_unknown')
    d['actual_formal_role']='not_confirmed'
    d['proof_status']='not_observed'
    if k=='Assume': d['simulation_note']='Same property truth/checking semantics as assert; does not constrain ordinary simulation stimulus. Success reporting may differ.'
    if role_context:
        d['supplied_role_context']=role_context
        d['role_context_status']='script_declaration_not_execution' if role_context['provenance']=='script_declaration' else 'caller_asserted_not_tool_verified'
    if mode['value']=='formal' and row['effects']:
        d['procedural_effects_formal']='source_effects_only; inclusion_in_formal_model_not_confirmed'


def prepare_source(item, options, prepared=None):
    """Read-only exact input observation using existing compile replay/identity rules."""
    from .compile_log_parser import parse_compile_log
    from .design_identity import DesignIdentityReader
    from .source_graph_adapter import _build_compile_manifest
    if prepared is not None:
        compile_result=prepared.compile_result()
        log=prepared.ordered_compile_logs[0]
    elif options.compile_log:
        log=os.path.realpath(options.compile_log)
        compile_result=parse_compile_log(log,options.simulator)
    else:
        path=os.path.realpath(item.get('source_file') or '')
        if not item.get('source_file') or not os.path.isfile(path):
            raise ValueError('source_context_required')
        if os.stat(path).st_size>32*1024*1024: raise ValueError('source_input_limit')
        # This is an explicit standalone frontend invocation, not observed
        # simulator evidence; the public identity retains that distinction.
        compile_result={'simulator':'vcs','compile_cwd':str(Path(path).parent),
                        'files':{'user':[{'path':path}]},'top_modules':[],
                        'compile_command':'vcs -sverilog '+shlex.quote(path)}
        identity=DesignIdentityReader().capture(path,compile_result)
        if len(identity.records)>256 or sum(r.size for p,r in identity.records)>32*1024*1024:
            raise ValueError('source_input_limit')
        return {'frontend_args':[path,*(['--top',options.top_hint] if options.top_hint else [])],
                'identity':{'origin':'standalone_source','source_sha256':identity.digest,
                            'compile_association':'unverified','source_files':[p for p,r in identity.records]},
                'context_gaps':list(identity.issues)+([] if identity.include_resolution_complete else ['include_resolution_incomplete'])},identity
    manifest,gaps,_=_build_compile_manifest(log,compile_result,
        content_snapshot=prepared.snapshot if prepared else None)
    identity=DesignIdentityReader().capture(log,compile_result)
    if len(identity.records)>256 or sum(r.size for p,r in identity.records)>32*1024*1024:
        raise ValueError('source_input_limit')
    if item.get('source_file') and os.path.realpath(item['source_file']) not in {r.path for p,r in identity.records}:
        raise ValueError('source_not_in_compile_context')
    tops=[options.top_hint] if options.top_hint else list(manifest.ordered_tops)
    if prepared and prepared.selected_top: tops=[prepared.selected_top]
    if options.top_hint and manifest.ordered_tops and options.top_hint not in manifest.ordered_tops:
        raise ValueError('top_not_in_compile_context')
    args=[*manifest.ordered_options,*manifest.ordered_inputs]
    for top in tops: args.extend(['--top',top])
    return {'frontend_args':args,'identity':{'origin':'prepared_design' if prepared else 'compile_source_snapshot',
        'context_sha256':prepared.identity_sha256 if prepared else identity.digest,
        'source_sha256':identity.digest,'compile_log':log,
        'compile_association':'current_source_observation; original_runtime_source_bytes_not_certified',
        'source_files':[p for p,r in identity.records]},
        'context_gaps':list(gaps)+list(identity.issues)+([] if identity.include_resolution_complete else ['include_resolution_incomplete'])},identity


async def run_worker(payload, python_bin, deadline):
    process=None; reader=None; acquired=False; launch=None
    async def bounded_read(stream):
        chunks=[]; size=0
        while chunk:=await stream.read(65536):
            size+=len(chunk)
            if size>8*1024*1024: raise ValueError('worker_output_limit')
            chunks.append(chunk)
        return b''.join(chunks)
    try:
        while not acquired:
            if time.monotonic()>=deadline: raise TimeoutError
            acquired=_PROCESS_COLD_BUILD_LOCK.acquire(blocking=False)
            if not acquired: await asyncio.sleep(.01)
        payload['timeout_sec']=max(.001,deadline-time.monotonic())
        launch=asyncio.create_task(asyncio.create_subprocess_exec(python_bin,str(Path(__file__).with_name('property_worker.py')),
            stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True))
        # A cancellation during spawn must still collect and terminate the child.
        process=await asyncio.shield(launch)
        reader=asyncio.create_task(bounded_read(process.stdout))
        process.stdin.write(json.dumps(payload).encode()); await process.stdin.drain(); process.stdin.close()
        while not reader.done():
            remaining=deadline-time.monotonic()
            if remaining<=0: raise TimeoutError
            await asyncio.wait({reader},timeout=min(.05,remaining))
            rss=_process_rss_kib(process.pid)
            if rss is not None and rss>512*1024: raise ValueError('worker_rss_limit')
        raw=await reader
        await asyncio.wait_for(process.wait(),max(.001,deadline-time.monotonic()))
        if process.returncode: raise ValueError('worker_failed')
        return json.loads(raw)
    finally:
        async def cleanup():
            try:
                child=process
                if child is None and launch is not None:
                    try: child=await launch
                    except (OSError,ValueError): pass
                if child is not None: await _terminate_process_group(child)
            finally:
                try:
                    if reader is not None:
                        if not reader.done(): reader.cancel()
                        await asyncio.gather(reader,return_exceptions=True)
                finally:
                    if acquired: _PROCESS_COLD_BUILD_LOCK.release()
        finalizer=asyncio.create_task(cleanup())
        cancelled=False
        while not finalizer.done():
            try: await asyncio.shield(finalizer)
            except asyncio.CancelledError: cancelled=True
        finalizer.result()
        if cancelled: raise asyncio.CancelledError


def _trim(result, options):
    for row in result['results']:
        row['gap_codes']=list(dict.fromkeys(g['code'] for g in row['gaps']))
    if options.detail=='summary':
        for row in result['results']:
            row['output_truncated']|=bool(row['nodes'] or row['effects'])
            row['nodes']=[]; row['effects']=[]
            errors=[d for d in row['diagnostics'] if d.get('severity')=='error']
            warnings=[d for d in row['diagnostics'] if d.get('severity')!='error']
            if len(warnings)>8:
                target=row.get('target',{}).get('source',{})
                warnings.sort(key=lambda d:(d.get('file')!=target.get('file'),
                    abs(d.get('line',0)-target.get('line',0))))
                row['diagnostics']=[*errors,*warnings[:8]]
                row['diagnostics_truncated']=True
                row['output_truncated']=True
    def size(): return len(json.dumps(result,separators=(',',':'),ensure_ascii=True).encode())
    # Retain status and gaps for EVERY item. Never pretend trimmed analysis is incomplete.
    for row in sorted(result['results'],key=lambda r:len(json.dumps(r)),reverse=True):
        if size()<=options.max_output_bytes: break
        for key in ('nodes','effects','references','variables','clocks','candidates','diagnostics'):
            row[key]=[]
        row['target']={k:v for k,v in row['target'].items() if k!='source'}
        row['identity']={k:v for k,v in row['identity'].items() if k!='source_files'}
        row['summary']=['Display trimmed; repeat this explicit item with detail=full and a larger max_output_bytes.']
        for g in row['gaps']:
            g['source']={k:v for k,v in g['source'].items() if k!='text'}
        row['output_truncated']=True
    if size()>options.max_output_bytes:
        for row in result['results']:
            row['directive']={k:v for k,v in row['directive'].items() if k in {'kind','form','role','proof_status','actual_formal_role'}}
            row['identity']={k:v for k,v in row['identity'].items() if k.endswith('sha256') or k=='origin'}
            row['gaps_truncated']=len(row['gaps'])>1
            row['gaps']=row['gaps'][:1]
            for g in row['gaps']:
                g['source']={}
                g['message']=g['message'][:128];g['recovery']=g['recovery'][:128]
            row['output_truncated']=True
    if size()>options.max_output_bytes:
        # Every retained item needs a minimum receipt. An impossible envelope
        # is explicit instead of silently dropping independent IDs.
        raise ValueError('max_output_bytes_too_small_for_per_item_receipts')
    result['output_truncated']=any(r['output_truncated'] for r in result['results'])
    return result


async def explain_properties(arguments, *, registry, config, run_thread, prepared=None, validate=None, source_blocker=None, plan_store=None):
    options=ExplainPropertyOptions.model_validate(arguments)
    started=time.monotonic(); deadline=started+options.timeout_sec
    entries=[i.model_dump() for i in options.entries()]
    units={u.id:u.model_dump() for u in options.units}
    anchor=options.run_anchor or options.compile_log or (prepared.ordered_compile_logs[0] if prepared else None)
    try:
        mode=await asyncio.wait_for(run_thread(lambda:registry.resolve(options.mode,anchor)),max(.001,deadline-time.monotonic()))
    except TimeoutError:
        mode={'value':'unknown','basis':'total_timeout','run_identity':None}
    results=[]
    for item in entries:
        payload={'item':item,'limits':{'max_nodes':options.max_nodes,'max_depth':options.max_depth},
                 'frontend_version':config.frontend_version,'execution_plan':options.include_check_plan}
        identity=None
        try:
            if time.monotonic()>=deadline: raise TimeoutError
            if not config.valid or not config.enabled:
                raise ValueError('frontend_disabled_or_configuration_invalid')
            if item.get('text') is not None and any(item.get(k) for k in ('source_file','instance_path','line')):
                raise ValueError('conflicting_text_and_source_target')
            if item.get('unit') and item['unit'] not in units: raise ValueError('shared_unit_not_found')
            payload['unit']=units.get(item.get('unit'))
            if item.get('text') is not None:
                declared=(payload['unit'] or {}).get('declarations','')+'\n'+item['text']
                if re.search(r'`include\b',re.sub(r'/\*.*?\*/|//[^\n]*','',declared,flags=re.S)):
                    raise ValueError('text_include_requires_source_context')
                payload['identity']={'origin':'caller_text','input_sha256':digest([item,payload['unit'],config.frontend_version]),
                                     'compile_association':'not_claimed'}
            else:
                if source_blocker: raise ValueError(source_blocker)
                if item.get('unit'): raise ValueError('text_unit_cannot_override_compiled_source')
                if validate: await validate(deadline)
                source,identity=await asyncio.wait_for(run_thread(lambda:prepare_source(item,options,prepared)),
                                                      max(.001,deadline-time.monotonic()))
                payload.update(source)
                if not identity.complete: raise ValueError('source_identity_incomplete')
            row=await asyncio.wait_for(run_worker(payload,config.python_bin,deadline),max(.001,deadline-time.monotonic()))
            if identity is not None:
                if not await asyncio.wait_for(run_thread(identity.current),max(.001,deadline-time.monotonic())):
                    raise ValueError('source_identity_changed')
                if validate: await validate(deadline)
            for code in payload.get('context_gaps',[]):
                row['gaps'].append(gap(code,'context','Compile replay or dependency identity is incomplete.',
                                      'Provide the exact compile inputs, macros, include paths, top and original definitions.'))
                row['context_status']='incomplete';row['semantic_status']='partial'
        except TimeoutError:
            row=blocked(item,'total_timeout','limit','The total request deadline was reached; no fallback was started.',
                        'Submit remaining IDs separately or increase timeout_sec within its bound.')
        except (OSError,ValueError) as exc:
            code=str(exc) if isinstance(exc,ValueError) and re_safe_code(str(exc)) else 'input_or_worker_unavailable'
            category='identity' if 'identity' in code or 'context_changed' in code else ('limit' if 'limit' in code else 'context')
            row=blocked(item,code,category,'This item could not be bound to its requested input/context.',
                        'Select a valid target and provide matching source/shared definitions; prepare a new context if the old identity changed.')
        if mode.get('basis')=='associated_discovery' and time.monotonic()<deadline:
            try:
                current=await asyncio.wait_for(run_thread(lambda:registry.resolve(options.mode,anchor)),max(.001,deadline-time.monotonic()))
            except TimeoutError:
                current={'value':'unknown','basis':'total_timeout','run_identity':None}
            if current!=mode:
                mode={'value':'unknown','basis':'run_association_changed_during_call','run_identity':mode.get('run_identity')}
        roles(row,mode,options.role_context.model_dump() if options.role_context else None)
        if options.include_check_plan and plan_store is not None:
            from .property_plan import prepare_plan
            try:
                prepared_plan=await asyncio.wait_for(run_thread(lambda:prepare_plan(row)),
                                                     max(.001,deadline-time.monotonic()))
                # Publish before display trimming; only publication touches the
                # session store on the event loop. Compilation stays cancellable.
                row['check_plan']=plan_store.publish(row,current=identity.current if identity is not None else None,
                                                     validate=validate,prepared=prepared_plan)
            except TimeoutError:
                row['check_plan']=dict(status='unavailable',gap_codes=['property_plan_timeout'])
        results.append(row)
    result={'schema_version':'traceweave.property.v1',
            'status':'complete' if all(r['semantic_status']=='complete' for r in results) else
                     ('not_run' if all(r['semantic_status']=='not_run' for r in results) else 'partial'),
            'results':results,'limits':{'items':16,'text_bytes':262144,'max_nodes_per_item':options.max_nodes,
            'max_depth':options.max_depth,'timeout_sec':options.timeout_sec,'worker_rss_mib':512,
            'max_output_bytes':options.max_output_bytes},
            'frontend':{'name':'pyslang','version':config.frontend_version,'isolation':'worker_process',
                        'cache':'none'},'runtime_analysis':'not_performed','output_truncated':False}
    return ExplainPropertyResult.model_validate(await run_thread(lambda:_trim(result,options)))


def re_safe_code(text):
    return bool(text) and len(text)<100 and all(c.islower() or c.isdigit() or c=='_' for c in text)
