"""Bounded single-clock property matching over already sampled, typed facts.

No parser, source frontend or waveform I/O lives here. Sequence alternatives are
existential matches; implication creates an obligation for EVERY antecedent end.
Observed X/Z and absent evidence follow different paths throughout the engine.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
import time

from .cancellation import check_cancelled
from .dynamic_evidence import Expr, evaluate
from .expression_values import Value
from .expression_parser import literal


class EvaluationLimit(Exception):
    pass


class UnsupportedPlan(ValueError):
    pass


@dataclass(frozen=True)
class Match:
    end: int
    env: dict = field(default_factory=dict)
    writes: frozenset = frozenset()


@dataclass
class Matches:
    matches: list[Match] = field(default_factory=list)
    pending: bool = False
    unknown: bool = False
    dead_at: int | None = None
    reason: str = ''


@dataclass
class Outcome:
    status: str
    end: int | None = None
    node: str | None = None
    vacuous: bool = False
    reason: str = ''
    time_fs: int | None = None


class PropertyEngine:
    def __init__(self, plan, samples, *, controls=None, history_complete=False,
                 actual_end=False, timeout_sec=20, max_work=200000,
                 max_matches=16384, max_evidence=256):
        self.plan,self.samples=plan,samples
        self.nodes={n['id']:n for n in plan['nodes']}
        self.n=len(samples);self.controls=controls or {}
        self.history_complete,self.actual_end=history_complete,actual_end
        self.deadline=time.monotonic()+timeout_sec
        self.max_work,self.max_matches=max_work,max_matches
        self.work=self.match_count=0
        self.max_evidence=max_evidence;self.evidence=[];self.evidence_truncated=False
        self.sampled_values={};self.sampled_bytes=0;self.sampled_values_truncated=False
        self.obligations=[];self.obligations_truncated=False
        self.gaps=set();self.history_cache={};self.expr_cache={};self.seq_cache={}
        self.locals={v['symbol']:v for v in plan.get('variables',[])}
        self.unknown_values=0
        self.probing=False
        self.offset_cache={}
        self._validate_effects()

    def charge(self):
        check_cancelled();self.work+=1
        if self.work>self.max_work or time.monotonic()>=self.deadline:
            raise EvaluationLimit('property_work_limit' if self.work>self.max_work else 'property_timeout')

    def _validate_effects(self):
        for n in self.nodes.values():
            for effect in n['facts'].get('match_items',[]):
                if (effect.get('kind')!='assignment' or effect.get('nonblocking') or effect.get('compound')
                        or len(effect.get('writes',[]))!=1 or effect['writes'][0] not in self.locals
                        or effect.get('target',{}).get('execution',{}).get('expression',{}).get('op')!='signal'):
                    raise UnsupportedPlan('property_match_effect_unsupported')
            for binding in n['facts'].get('bindings',[]):
                if binding.get('local') and binding.get('direction') in {'Out','InOut'}:
                    raise UnsupportedPlan('property_local_output_binding_unsupported')
        for h in self.plan.get('histories',{}).values():
            stack=[h['expression']]
            while stack:
                e=stack.pop()
                if e.get('signal') in self.locals:
                    raise UnsupportedPlan('property_local_history_unsupported')
                stack.extend(e.get('args',()))

    def _expr(self, raw):
        key=id(raw)
        if key not in self.expr_cache:self.expr_cache[key]=Expr.from_dict(raw)
        return self.expr_cache[key]

    def read(self, raw, tick, env):
        self.charge()
        if not 0<=tick<self.n:return None
        expr=self._expr(raw)
        def sample(node):
            self.charge()
            if node.signal.startswith('@history:'):
                value=self.history(node.signal,tick,env)
                return {'value':None if value is None else value.bits,
                        'unavailable_reason':'property_history_value_unavailable'}
            if node.signal in self.locals:
                value=env.get(node.signal)
                return {'value':None if value is None else value.bits,
                        'unavailable_reason':'property_local_value_unavailable'}
            value=self.samples[tick]['signals'].get(node.signal)
            bits=value.get('bin') if isinstance(value,dict) else value
            shape=self.plan['signals'].get(node.signal)
            if bits is None or shape is None or len(bits)!=shape['width']:
                return {'value':None}
            # Preserve the bound input vector, separately from the evaluated
            # expression result. Prefix probes must not invent dependencies.
            key=(tick,node.signal)
            if not self.probing and key not in self.sampled_values:
                cost=128+len(bits)+len(node.signal.encode())
                if len(self.sampled_values)<self.max_evidence and self.sampled_bytes+cost<=65536:
                    self.sampled_values[key]=dict(source=node.signal,tick=tick,
                        time_ps=self.samples[tick]['time_ps'],
                        time_fs=self.samples[tick].get('time_fs',self.samples[tick]['time_ps']*1000),
                        value={'bin':bits})
                    self.sampled_bytes+=cost
                else:self.sampled_values_truncated=True
            positions={bit:i for i,bit in enumerate(shape['bits'])}
            try:bits=''.join(bits[positions[b]] for b in node.bits)
            except KeyError:return {'value':None}
            return {'value':bits}
        result=evaluate(expr,sample,allow_partial_values=True)
        if result.value is not None and any(d.get('value') is None for d in result.dependencies):
            # A value may be determined without every input being observed.
            # Preserve those gaps; the proof does not certify their contents.
            self.gaps.update(result.gaps)
        if result.value is None:
            self.gaps.update(result.gaps or ['property_value_unavailable'])
            return None
        value=Value(result.value,expr.signed)
        if not value.known:self.unknown_values+=1
        return value

    def value(self, spec, tick, env, node):
        raw=spec.get('execution',{}).get('expression')
        if raw is None:
            self.gaps.add('property_value_projection_missing');return None
        value=self.read(raw,tick,env)
        if self.probing:return value
        if len(self.evidence)<self.max_evidence:
            self.evidence.append(dict(node=node,tick=tick,time_ps=self.samples[tick]['time_ps'] if 0<=tick<self.n else None,
                expression=spec.get('text',''),value=None if value is None else value.public(),
                locals={k:v.public() for k,v in env.items()}))
        else:self.evidence_truncated=True
        return value

    def history(self,key,tick,env):
        self.charge()
        cache=(key,tick)
        if cache in self.history_cache:return self.history_cache[cache]
        h=self.plan['histories'][key];name=h['function']
        current=self.read(h['expression'],tick,env)
        if name=='$sampled':result=current
        else:
            remaining=h.get('ticks',1);prior=None;missing=False
            for i in range(tick-1,-1,-1):
                self.charge()
                gate=self.read(h['gate'],i,env) if h.get('gate') else Value('1')
                if gate is None:missing=True;break
                if gate.truth=='1':
                    remaining-=1
                    if remaining==0:
                        prior=self.read(h['expression'],i,env);break
            if remaining and not missing:
                default=h.get('default',{})
                if self.history_complete and default.get('status')=='resolved':
                    try:
                        val=literal(default['value']).expr
                        prior=Value(val.value,val.signed)
                    except (ValueError,KeyError):self.gaps.add('history_default_unresolved')
                else:self.gaps.add('history_before_capture_unavailable')
            if missing:self.gaps.add('history_gate_unavailable')
            if name=='$past':result=prior
            elif name in {'$rose','$fell'} and current is not None and current.bits[-1]!=('1' if name=='$rose' else '0'):
                result=Value('0')
            elif prior is None or current is None:result=None
            elif name in {'$rose','$fell'}:
                old,new=prior.bits[-1],current.bits[-1]
                bit=(new=='1' and old!='1') if name=='$rose' else (new=='0' and old!='0')
                result=Value(str(int(bit)))
            else:
                same=current.bits==prior.bits
                result=Value(str(int(same if name=='$stable' else not same)))
        self.history_cache[cache]=result
        return result

    def _add(self, target, match):
        self.charge();self.match_count+=1
        if self.match_count>self.max_matches:raise EvaluationLimit('property_match_limit')
        target.append(match)

    def _env_key(self,env):
        return tuple(sorted((k,v.bits,v.signed) for k,v in env.items()))

    def sequence(self,node_id,start,env,*,base=False):
        self.charge()
        key=(node_id,start,self._env_key(env),base)
        if key in self.seq_cache:return self.seq_cache[key]
        n=self.nodes[node_id];f=n['facts']
        if not base and f.get('repetition'):
            result=self._repeat(n,start,env)
        else:result=self._sequence(n,start,env)
        if len(self.seq_cache)<4096:self.seq_cache[key]=result
        return result

    def _sequence(self,n,start,env):
        op=n['op'];children=n['children'];f=n['facts'];node=n['id']
        if op=='sample':
            if start>=self.n:return Matches(pending=True)
            if start<0:return Matches(unknown=True)
            value=self.value(f['expression'],start,env,node)
            if value is None:return Matches(unknown=True)
            return Matches([Match(start,dict(env))]) if value.truth=='1' else Matches(dead_at=start)
        if op in {'clock','reference'}:
            if not children:raise UnsupportedPlan('recursive_property_plan_unsupported')
            bound=self._reference(n,start,env) if op=='reference' else env
            if bound is None:return Matches(unknown=True)
            return self.sequence(children[0],start,bound)
        if op=='concat':return self._concat(n,start,env)
        if op in {'first_match','match_items'}:
            result=self.sequence(children[0],start,env)
            matches=result.matches
            if op=='first_match':
                if result.unknown:return Matches(unknown=True,pending=result.pending)
                if matches:
                    first=min(m.end for m in matches)
                    matches=[m for m in matches if m.end==first]
                    result=Matches(matches)
            output=[];unknown=result.unknown
            for m in matches:
                state=dict(m.env);writes=set(m.writes)
                for effect in f.get('match_items',[]):
                    value=self.value(effect['expression'],m.end,state,node)
                    if value is None:unknown=True;break
                    raw=effect['target']['execution']['expression']
                    state[effect['writes'][0]]=value.resize(raw['width'],signed=raw['signed'],two_state=raw['two_state'])
                    writes.add(effect['writes'][0])
                else:self._add(output,Match(m.end,state,frozenset(writes)))
            return Matches(output,result.pending,unknown,result.dead_at)
        if op=='Throughout':return self._throughout(n,start,env)
        if op=='Within':return self._within(n,start,env)
        if op in {'And','Or','Intersect'}:
            if op=='Intersect':
                left=self.possible_offsets(children[0]);right=self.possible_offsets(children[1])
                if left is not None and right is not None and not left&right:
                    return Matches(dead_at=start,reason='disjoint_sequence_endpoints')
            a=self.sequence(children[0],start,env);b=self.sequence(children[1],start,env)
            if op=='Or':
                return Matches([*a.matches,*b.matches],a.pending or b.pending,a.unknown or b.unknown,
                               self._latest(a.dead_at,b.dead_at))
            for part in (a,b):
                if not part.matches and not part.pending and not part.unknown:
                    return Matches(dead_at=part.dead_at)
            output=[];unknown=a.unknown or b.unknown
            for left in a.matches:
                for right in b.matches:
                    self.charge()
                    if op=='Intersect' and left.end!=right.end:continue
                    merged=self._merge(env,left,right)
                    if merged is None:unknown=True;continue
                    self._add(output,Match(max(left.end,right.end),merged,left.writes|right.writes))
            pending=(a.pending and b.pending) if op=='Intersect' else (a.pending or b.pending)
            return Matches(output,pending,unknown,self._latest(a.dead_at,b.dead_at,
                *(m.end for m in a.matches),*(m.end for m in b.matches)))
        raise UnsupportedPlan('sequence_operator_unsupported:'+op)

    def possible_offsets(self,key,base=False):
        """A bounded superset of legal endpoint offsets, independent of values.

        None means the static inventory is too large or unbounded. Only complete
        disjoint endpoint inventories prove a statically impossible intersection.
        """
        cache=(key,base)
        if cache in self.offset_cache:return self.offset_cache[cache]
        self.charge();n=self.nodes[key];op=n['op'];f=n['facts'];children=n['children']
        parts=[self.possible_offsets(c) for c in children]
        if not base and f.get('repetition'):
            rep=f['repetition'];lo=rep['count']['min'];hi=rep['count']['max']
            body=self.possible_offsets(key,True)
            if rep['kind']!='Consecutive' or hi is None or hi>256 or body is None:result=None
            else:
                result=set();current={-1}
                for count in range(hi+1):
                    self.charge()
                    if count>=lo:result|=current
                    if len(result)>4096:result=None;break
                    if len(current)*len(body)>4096:result=None;break
                    current={a+b+1 for a in current for b in body}
        elif op=='sample':result={0}
        elif op in {'clock','reference','first_match','match_items'}:result=parts[0] if parts else None
        elif op=='concat':
            result={0};joins={j['right']:j['delay'] for j in f['joins']}
            for i,(child,part) in enumerate(zip(children,parts)):
                delay=joins.get(child,dict(min=0,max=0))
                lo,hi=delay['min'],delay['max']
                if part is None or hi is None or len(result)*len(part)*(hi-lo+1)>4096:
                    result=None;break
                result={a+d+b for a in result for b in part for d in range(lo,hi+1)
                        if not (i and d==0 and (a<0 or b<0))}
        elif op=='Intersect' and None not in parts:result=parts[0]&parts[1]
        elif op=='Or' and None not in parts:result=parts[0]|parts[1]
        elif op=='And' and None not in parts:
            result=None if len(parts[0])*len(parts[1])>4096 else {max(a,b) for a in parts[0] for b in parts[1]}
        elif op in {'Throughout','Within'}:result=parts[-1]
        else:result=None
        self.offset_cache[cache]=result
        return result

    @staticmethod
    def _latest(*ticks):
        return max((i for i in ticks if i is not None),default=None)

    def _merge(self,env,a,b):
        state=dict(env)
        for key in a.writes&b.writes:
            if a.env.get(key)!=b.env.get(key):
                self.gaps.add('parallel_local_write_conflict');return None
        for m in (a,b):
            for key in m.writes:state[key]=m.env[key]
        return state

    def _delay(self,bounds,origin):
        lo=bounds['min'];hi=bounds['max']
        # An empty operand may start just after the last captured event and
        # end on that last event. Required samples at that start stay pending.
        last=max(lo-1,self.n-origin)
        upper=last if hi is None else min(hi,last)
        return range(lo,upper+1),hi is None or hi>last

    def _concat(self,n,start,env):
        states=[Match(start,dict(env))];pending=unknown=False;dead=None
        joins={j['right']:j['delay'] for j in n['facts']['joins']}
        for index,child in enumerate(n['children']):
            next_states=[]
            bounds=joins.get(child,dict(min=0,max=0))
            for left in states:
                origin=start if index==0 else left.end
                delays,future=self._delay(bounds,origin)
                pending|=future
                for delay in delays:
                    self.charge()
                    if index and left.end<start and delay==0:
                        dead=self._latest(dead,start);continue
                    at=origin+delay
                    right=self.sequence(child,at,left.env)
                    pending|=right.pending;unknown|=right.unknown
                    dead=self._latest(dead,right.dead_at)
                    for m in right.matches:
                        if index and m.end<at and delay==0:
                            dead=self._latest(dead,at);continue
                        self._add(next_states,Match(m.end,m.env,left.writes|m.writes))
            states=next_states
            if not states:break
        return Matches(states,pending,unknown,dead)

    def _repeat(self,n,start,env):
        rep=n['facts']['repetition'];lo=rep['count']['min'];hi=rep['count']['max']
        if rep['kind']!='Consecutive':return self._occurrences(n,start,env)
        output=[];states=[Match(start-1,dict(env))];count=0;pending=unknown=False;dead=None
        maximum=hi if hi is not None else self.n-start+1
        while states:
            self.charge()
            if count>=lo:
                for m in states:self._add(output,m)
            if count>=maximum:break
            next_states=[]
            for prefix in states:
                result=self.sequence(n['id'],prefix.end+1,prefix.env,base=True)
                pending|=result.pending;unknown|=result.unknown;dead=self._latest(dead,result.dead_at)
                for m in result.matches:
                    if m.end<prefix.end+1:
                        raise UnsupportedPlan('empty_repetition_progress_unsupported')
                    self._add(next_states,Match(m.end,m.env,prefix.writes|m.writes))
            states=next_states;count+=1
        return Matches(output,pending,unknown,dead)

    def _occurrences(self,n,start,env):
        if n['op']!='sample':raise UnsupportedPlan('occurrence_operand_unsupported')
        rep=n['facts']['repetition'];lo=rep['count']['min'];hi=rep['count']['max']
        trailing=rep['kind']=='Nonconsecutive';count=0;output=[]
        if lo==0:self._add(output,Match(start-1,dict(env)))
        if hi==0 and not trailing:return Matches(output)
        for tick in range(start,self.n):
            value=self.value(n['facts']['expression'],tick,env,n['id'])
            if value is None:return Matches(output,unknown=True)
            true=value.truth=='1'
            if value.truth=='x':return Matches(output,dead_at=tick)
            if true:count+=1
            if hi is not None and count>hi:return Matches(output,dead_at=tick)
            if count>=lo and (true or trailing):self._add(output,Match(tick,dict(env)))
            if hi is not None and count==hi and not trailing:return Matches(output)
        return Matches(output,pending=True)

    def _throughout(self,n,start,env):
        left,right=n['children'];result=self.sequence(right,start,env)
        output=[];unknown=result.unknown;dead=result.dead_at
        checks={}
        def check(tick):
            if tick not in checks:checks[tick]=self.sequence(left,tick,env)
            return checks[tick]
        for m in result.matches:
            valid=True
            for tick in range(start,m.end+1):
                r=check(tick)
                if not r.matches:
                    unknown|=r.unknown;dead=self._latest(dead,r.dead_at);valid=False;break
            if valid:self._add(output,m)
        pending=result.pending
        if pending:
            for tick in range(start,self.n):
                r=check(tick)
                if not r.matches and not r.unknown:
                    pending=False;dead=self._latest(dead,tick);break
                unknown|=r.unknown
        return Matches(output,pending,unknown,dead)

    def _within(self,n,start,env):
        inner,outer=n['children'];rhs=self.sequence(outer,start,env)
        output=[];unknown=rhs.unknown;dead=rhs.dead_at
        for out in rhs.matches:
            for at in range(start,out.end+1):
                lhs=self.sequence(inner,at,env);unknown|=lhs.unknown
                for inside in lhs.matches:
                    if inside.end<=out.end:
                        merged=self._merge(env,inside,out)
                        if merged is not None:self._add(output,Match(out.end,merged,inside.writes|out.writes))
                dead=self._latest(dead,min(out.end,lhs.dead_at) if lhs.dead_at is not None else out.end)
        return Matches(output,rhs.pending,unknown,dead)

    def _reference(self,n,start,env):
        state=dict(env)
        for v in self.locals.values():
            if v['owner']==n['id']:state.pop(v['symbol'],None)
        for binding in n['facts'].get('bindings',[]):
            if binding.get('local') and binding.get('direction')=='In':
                value=self.value(binding['actual'],start,env,n['id'])
                if value is None:return None
                state[binding['formal_symbol']]=value
        return state

    def property(self,node_id,start,env=None):
        self.charge();env={} if env is None else env
        n=self.nodes[node_id];op=n['op'];f=n['facts'];children=n['children']
        if op in {'clock','reference'}:
            state=self._reference(n,start,env) if op=='reference' else env
            if state is None:return Outcome('unknown',node=node_id)
            return self.property(children[0],start,state)
        if op in {'disable_iff','accept_on','reject_on','sync_accept_on','sync_reject_on'}:
            result=self.property(children[0],start,env)
            return self._abort(n,start,env,result)
        if op in {'OverlappedImplication','NonOverlappedImplication'}:
            antecedent=self.sequence(children[0],start,env);results=[]
            advance=int(op=='NonOverlappedImplication')
            for m in antecedent.matches:
                out=self.property(children[1],m.end+advance,m.env);results.append(out)
                row=dict(node=node_id,antecedent_end=m.end,consequent_start=m.end+advance,
                         status=out.status,end=out.end,decisive_node=out.node)
                if not self.probing:
                    if len(self.obligations)<256:self.obligations.append(row)
                    else:self.obligations_truncated=True
                if out.status=='false':return out
            failures=[r for r in results if r.status=='false']
            if failures:return min(failures,key=lambda r:r.end if r.end is not None else self.n)
            if antecedent.unknown or any(r.status=='unknown' for r in results):return Outcome('unknown',node=node_id)
            if antecedent.pending or any(r.status=='pending' for r in results):return Outcome('pending',node=node_id)
            if any(r.status=='disabled' for r in results):return Outcome('disabled',node=node_id)
            return Outcome('true',self._latest(antecedent.dead_at,*(r.end for r in results)),node_id,
                           vacuous=not antecedent.matches,reason='no_antecedent_match' if not antecedent.matches else 'all_obligations_satisfied')
        if op=='Not':
            r=self.property(children[0],start,env)
            return replace(r,status={'true':'false','false':'true'}.get(r.status,r.status),node=node_id)
        if op=='if':
            value=self.value(f['condition'],start,env,node_id)
            if value is None:return Outcome('unknown',node=node_id)
            index=0 if value.truth=='1' else 1
            return self.property(children[index],start,env) if index<len(children) else Outcome('true',start,node_id,True)
        if op in {'And','Or','Iff','Implies'} and n['domain']=='property':
            a=self.property(children[0],start,env);b=self.property(children[1],start,env)
            return self._boolean(op,a,b,node_id)
        if op in {'NextTime','SNextTime'}:
            offset=(f.get('range') or {}).get('min',1)
            return self.property(children[0],start+offset,env) if start+offset<self.n else Outcome('pending',node=node_id)
        if op in {'Always','SAlways','Eventually','SEventually'}:
            bounds=f.get('range') or dict(min=0,max=None)
            offsets,future=self._delay(bounds,start)
            results=[self.property(children[0],start+i,env) for i in offsets]
            universal=op in {'Always','SAlways'}
            decisive='false' if universal else 'true'
            found=[r for r in results if r.status==decisive]
            if found:return min(found,key=lambda r:r.end if r.end is not None else self.n)
            if any(r.status=='unknown' for r in results):return Outcome('unknown',node=node_id)
            if future or any(r.status=='pending' for r in results):return Outcome('pending',node=node_id)
            return Outcome('true' if universal else 'false',self._latest(*(r.end for r in results)),node_id)
        if op in {'Until','SUntil','UntilWith','SUntilWith'}:
            return self._until(n,start,env)
        sequence=children[0] if op in {'strong','weak'} else node_id
        result=self.sequence(sequence,start,env)
        nonempty=[m for m in result.matches if m.end>=start]
        if nonempty:return Outcome('true',min(m.end for m in nonempty),node_id)
        if result.unknown:return Outcome('unknown',node=node_id)
        if result.pending:
            if self.actual_end:
                strength=op if op in {'strong','weak'} else f.get('sequence_property',{}).get('effective_strength')
                if strength in {'strong','weak'}:return Outcome('false' if strength=='strong' else 'true',self.n-1,node_id)
            return Outcome('pending',node=node_id)
        return Outcome('false',result.dead_at if result.dead_at is not None else start,node_id,
                       reason=result.reason or 'no_sequence_match')

    def _boolean(self,op,a,b,node):
        if op=='Implies':a=replace(a,status={'true':'false','false':'true'}.get(a.status,a.status));op='Or'
        if op=='Iff':
            if a.status in {'true','false'} and b.status in {'true','false'}:
                return Outcome('true' if a.status==b.status else 'false',self._latest(a.end,b.end),node)
        else:
            decisive='false' if op=='And' else 'true'
            found=[r for r in (a,b) if r.status==decisive]
            if found:return replace(min(found,key=lambda r:r.end if r.end is not None else self.n),node=node)
            if a.status==b.status and a.status in {'true','false'}:
                return Outcome(a.status,self._latest(a.end,b.end),node,a.vacuous and b.vacuous)
        status='unknown' if 'unknown' in {a.status,b.status} else 'pending' if 'pending' in {a.status,b.status} else 'disabled'
        return Outcome(status,node=node)

    def _until(self,n,start,env):
        left,right=n['children'];inclusive=n['op'].endswith('With')
        prefix=Outcome('true',start,n['id'])
        choices=Outcome('false',start,n['id'])
        for tick in range(start,self.n):
            b=self.property(right,tick,env)
            a=self.property(left,tick,env)
            if a.status=='disabled' or b.status=='disabled':
                self.gaps.add('until_disabled_operand_unresolved')
                a=replace(a,status='unknown') if a.status=='disabled' else a
                b=replace(b,status='unknown') if b.status=='disabled' else b
            # Each possible right-hand start is one alternative. All preceding
            # left properties are obligations, including any delayed endpoints.
            required=self._boolean('And',prefix,a,n['id'])
            candidate=self._boolean('And',required if inclusive else prefix,b,n['id'])
            choices=self._boolean('Or',choices,candidate,n['id'])
            if choices.status=='true':return choices
            prefix=required
            if prefix.status=='false':
                if choices.status=='false':return replace(choices,end=self._latest(choices.end,prefix.end))
                return choices
        return Outcome('unknown' if choices.status=='unknown' or prefix.status=='unknown' else 'pending',node=n['id'])

    def _abort(self,n,start,env,result):
        if start>=self.n:return result
        if result.status in {'true','false','disabled'}:
            result=self._settle(n['children'][0],start,env,result)
        end=min(result.end if result.end is not None else self.n-1,self.n-1)
        sync=n['op'].startswith('sync_')
        if sync:
            rows=[(i,self.value(n['facts']['condition'],i,env,n['id'])) for i in range(start,end+1)]
        else:
            timeline=self.controls.get(n['id'])
            if timeline is None:
                self.gaps.add('async_control_timeline_unavailable');return Outcome('unknown',node=n['id'])
            begin=self.samples[start].get('time_fs',self.samples[start]['time_ps']*1000)
            finish=self.samples[end].get('time_fs',self.samples[end]['time_ps']*1000)
            prior=[(at,v) for at,v in timeline if at<=begin]
            rows=([(begin,prior[-1][1])] if prior else [(begin,None)])
            rows += [(at,v) for at,v in timeline if begin<at<=finish]
        for tick,value in rows:
            self.charge()
            if value is None:
                self.gaps.add('abort_condition_unavailable');return Outcome('unknown',node=n['id'])
            if value.truth=='1':
                status='disabled' if n['op']=='disable_iff' else 'true' if 'accept' in n['op'] else 'false'
                at=self.samples[tick].get('time_fs',self.samples[tick]['time_ps']*1000) if sync else tick
                index=next((i for i in range(start,self.n)
                            if self.samples[i].get('time_fs',self.samples[i]['time_ps']*1000)>=at),end)
                return Outcome(status,index,n['id'],reason=n['op'],time_fs=at)
        return result

    def _settle(self,node,start,env,result):
        """Locate the first decisive prefix, including impossible endpoint joins.

        Completion is monotone for supported finite-prefix rules. Looking at
        prefixes also prevents a later reset from cancelling an already completed
        failure. Work, match and cancellation budgets cover every prefix probe.
        """
        if result.status not in {'true','false','disabled'}:return result
        full=self.n;lo=max(1,start+1);hi=full;was_probing=self.probing;actual_end=self.actual_end
        try:
            self.probing=True
            while lo<hi:
                self.charge();mid=(lo+hi)//2;self.n=mid;self.actual_end=False;self.seq_cache.clear()
                probe=self.property(node,start,env)
                if probe.status in {'true','false','disabled'}:hi=mid
                else:lo=mid+1
            self.n=lo;self.actual_end=actual_end and lo==full;self.seq_cache.clear()
            return self.property(node,start,env)
        finally:
            self.n=full;self.actual_end=actual_end;self.seq_cache.clear();self.probing=was_probing

    def attempt(self,start):
        self.seq_cache.clear()
        self.evidence=[];self.obligations=[];self.evidence_truncated=False;self.obligations_truncated=False
        self.sampled_values={};self.sampled_bytes=0;self.sampled_values_truncated=False
        try:
            result=self.property(self.plan['root'],start)
            result=self._settle(self.plan['root'],start,{},result)
            limited=False
        except EvaluationLimit as exc:
            self.gaps.add(str(exc));result=Outcome('unknown',reason=str(exc));limited=True
        return dict(start_index=start,status=result.status,end_index=result.end,decisive_node=result.node,
            end_time_fs=result.time_fs,
            vacuous=result.vacuous,reason=result.reason,analysis_limited=limited,
            evidence=list(self.evidence),evidence_truncated=self.evidence_truncated,
            sampled_values=list(self.sampled_values.values()),sampled_values_truncated=self.sampled_values_truncated,
            obligations=list(self.obligations),obligations_truncated=self.obligations_truncated)
