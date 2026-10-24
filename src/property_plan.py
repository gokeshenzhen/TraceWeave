"""Trusted static plans and a bounded process-session handle store.

Only server-owned frontend observations are registered. Public callers cannot
submit an AST. Plans retain source validation across display trimming and expire
after ten idle minutes, reset or eviction. No waveform or optional frontend I/O.
"""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import secrets
import time

from .cancellation import check_cancelled
from .dynamic_evidence import Expr

PLAN_VERSION='traceweave.property-plan.v1'
SEQUENCE_OPS=frozenset({'sample','clock','reference','concat','first_match','match_items',
    'And','Or','Intersect','Throughout','Within'})
PROPERTY_OPS=SEQUENCE_OPS|{'OverlappedImplication','NonOverlappedImplication','disable_iff',
    'strong','weak','Not','if','Iff','Implies','NextTime','SNextTime','Always','SAlways',
    'Eventually','SEventually','Until','SUntil','UntilWith','SUntilWith',
    'accept_on','reject_on','sync_accept_on','sync_reject_on'}


class PlanUnavailable(ValueError):
    pass


def prepare_plan(row):
    """Validate the complete worker result and inventory typed dependencies."""
    gaps=[]
    if row.get('semantic_status')!='complete' or row.get('analysis_limited') or not row.get('root'):
        return None, ['property_semantics_incomplete']
    if row.get('directive',{}).get('form')=='immediate':
        return None,['immediate_assertion_plan_unsupported']
    clocks=row.get('clocks',[])
    if len(clocks)!=1:
        return None,['single_clock_plan_required']
    clock=clocks[0]
    clock_expr=clock.get('expression',{}).get('execution',{}).get('expression',{})
    if clock.get('edge') not in {'PosEdge','NegEdge'} or clock_expr.get('op')!='signal' or clock_expr.get('width')!=1:
        return None,['direct_single_bit_clock_required']
    if clock.get('iff'):
        gaps.append('clock_iff_plan_unsupported')
    signals={};histories={}
    local_symbols={v['symbol'] for v in row.get('variables',[])}
    def expression(raw):
        check_cancelled()
        ir=Expr.from_dict(raw)
        stack=[ir]
        while stack:
            part=stack.pop();check_cancelled()
            if part.op=='unsupported':
                gaps.append(part.reason or 'property_value_unsupported')
            if part.op in {'array_ref','array_select','array'}:
                gaps.append('property_array_plan_unsupported')
            if part.op=='signal' and part.signal not in local_symbols and not part.signal.startswith('@history:'):
                prior=signals.get(part.signal)
                shape=dict(width=part.width,bits=list(part.bits),signed=part.signed,two_state=part.two_state)
                if prior is not None and prior!=shape:
                    gaps.append('property_signal_shape_conflict')
                signals[part.signal]=shape
            stack.extend(part.args)
    def walk(value):
        if isinstance(value,dict):
            if 'execution' in value:
                executable=value['execution']
                expression(executable['expression'])
                for key,h in executable['histories'].items():
                    histories[key]=h
                    expression(h['expression'])
                    if h.get('gate'):expression(h['gate'])
                    if h.get('gap'):gaps.append(h['gap'])
            for key,child in value.items():
                if key!='execution':walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    try:
        for n in row['nodes']:
            if n['op'] not in PROPERTY_OPS:gaps.append('property_operator_unsupported:'+n['op'])
            if n['facts'].get('back_to'):gaps.append('recursive_property_plan_unsupported')
            if n['op']=='sample' and 'execution' not in n['facts'].get('expression',{}):
                gaps.append('property_value_projection_missing')
            walk(n['facts'])
        walk(clocks)
    except (ValueError,TypeError,KeyError):
        gaps.append('property_value_projection_invalid')
    if len(signals)>128:gaps.append('property_signal_limit')
    if gaps:return None,sorted(set(gaps))
    plan=deepcopy({k:row.get(k) for k in ('id','root','nodes','clocks','variables','directive','identity','target','mode')})
    plan.update(version=PLAN_VERSION,signals=signals,histories=histories)
    from .property_engine import PropertyEngine, UnsupportedPlan
    try:PropertyEngine(plan,[])
    except UnsupportedPlan as exc:return None,[str(exc)]
    raw=json.dumps(plan,sort_keys=True,separators=(',',':')).encode()
    if len(raw)>4*1024*1024:return None,['property_plan_size_limit']
    plan['fingerprint']=hashlib.sha256(raw).hexdigest()
    return plan,[]


@dataclass
class PlanEntry:
    plan: dict
    current: object
    validate: object
    touched: float
    size: int


class PropertyPlanStore:
    def __init__(self,*,ttl=600,max_entries=32,max_bytes=32*1024*1024,clock=time.monotonic):
        self.ttl,self.max_entries,self.max_bytes,self.clock=ttl,max_entries,max_bytes,clock
        self.entries=OrderedDict();self.size=0

    def clear(self):
        self.entries.clear();self.size=0

    def _drop(self,key):
        self.size-=self.entries.pop(key).size

    def _expire(self):
        for key,entry in list(self.entries.items()):
            if self.clock()-entry.touched>=self.ttl:self._drop(key)

    def publish(self,row,*,current=None,validate=None,prepared=None):
        self._expire()
        plan,gaps=prepare_plan(row) if prepared is None else prepared
        if plan is None:
            return dict(status='unavailable',gap_codes=gaps,version=PLAN_VERSION,runtime_analysis='not_performed')
        size=len(json.dumps(plan,separators=(',',':')).encode())
        if size>self.max_bytes or self.max_entries<1:
            return dict(status='unavailable',gap_codes=['property_plan_store_limit'],version=PLAN_VERSION)
        while self.entries and (len(self.entries)>=self.max_entries or self.size+size>self.max_bytes):
            self._drop(next(iter(self.entries)))
        key='pp_'+secrets.token_hex(16)
        self.entries[key]=PlanEntry(plan,current,validate,self.clock(),size);self.size+=size
        return dict(status='prepared',handle=key,version=PLAN_VERSION,fingerprint=plan['fingerprint'],
            signal_count=len(plan['signals']),history_count=len(plan['histories']),
            runtime_analysis='not_performed',idle_ttl_sec=self.ttl)

    def resolve(self,key):
        self._expire()
        if not isinstance(key,str) or key not in self.entries:
            raise PlanUnavailable('property_plan_expired')
        entry=self.entries[key];entry.touched=self.clock();self.entries.move_to_end(key)
        return entry
