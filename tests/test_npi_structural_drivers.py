from copy import deepcopy
from types import SimpleNamespace as NS

import pytest

from src.hierarchy_source_ir import HierarchyBudgetExceeded
from src.cancellation import OperationCancelled
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from src.npi_structural_drivers import NpiBitDrivers, writer_identity
from src.npi_structural_scan import scan_native_ports
from src.structural_driver_facts import validate_driver_fact
from src.structural_semantic_runtime import validate_semantic_options
from tests.test_npi_structural_scan import Provider, packed, port


CATEGORIES=['multiple_driver','undriven_signal']


class Reader:
    def __init__(self, values=None, stop=None):
        self.values,self.stop,self.seen=values or {},stop,[]

    def read(self,path,*,categories):
        if path==self.stop:
            raise HierarchyBudgetExceeded('native_call_limit')
        self.seen.append(path)
        return deepcopy(self.values.get(path,([],True,False,set())))


def provider(**kw):
    opts=validate_semantic_options(dict(semantic_scope='top.u',semantic_categories=CATEGORIES,**kw))[1]
    p=Provider([],options=opts)
    p.low.Net='Net';p.low.Reg='Reg'
    p.child.data.update(is_black_box=0,Reg=[],Net=[packed('npiNet',1,12,
        name='S',full_name='top.u.S',net_type='npiWire',file='design.sv',line_no=3)])
    return p


def overlap_reader():
    return Reader({'top.u.S[5]':([],True,True,set()),
                   'top.u.S[9]':([{'driver_id':'one'},{'driver_id':'two'}],True,True,set())})


def test_bit_facts_and_resume_do_not_replay_the_prefix():
    p=provider(semantic_max_facts=1);first_reader=overlap_reader()
    first=scan_native_ports(p,p.options,driver_reader=first_reader)
    assert first['scan_progress']['status']=='available'
    assert first['facts'][0]['target_bits']==[5]
    checkpoint=deepcopy(first['_scan_state'])
    p=provider(semantic_max_facts=1);second_reader=overlap_reader()
    second=scan_native_ports(p,p.options,state=checkpoint,driver_reader=second_reader)
    assert second['status']=='complete'
    assert second['counts']=={'undriven_signal':1,'multiple_driver':1}
    assert second['facts'][0]['target_bits']==[9]
    assert second_reader.seen[0]=='top.u.S[9]'
    assert first['_scan_state']==checkpoint
    for fact in [*first['facts'],*second['facts']]:validate_driver_fact(fact)


def test_progress_without_a_finding_is_resumable():
    p=provider();r=Reader(stop='top.u.S[3]')
    first=scan_native_ports(p,p.options,driver_reader=r)
    assert first['facts']==[] and first['scan_progress']['status']=='available'
    p=provider();r=Reader()
    final=scan_native_ports(p,p.options,state=first['_scan_state'],driver_reader=r)
    assert r.seen[0]=='top.u.S[3]'
    assert final['scan_progress']['status']=='complete'


def test_changed_current_range_cannot_consume_a_checkpoint():
    p=provider();first=scan_native_ports(p,p.options,driver_reader=Reader(stop='top.u.S[3]'))
    p=provider();p.child.data['Net']=[packed('npiNet',12,1,name='S',full_name='top.u.S',net_type='npiWire')]
    with pytest.raises(NpiHierarchyUnavailable,match='npi_scan_position_changed'):
        scan_native_ports(p,p.options,state=first['_scan_state'],driver_reader=Reader())


@pytest.mark.parametrize('case',['degraded','interface','input','pull','incomplete'])
def test_unverified_boundaries_never_become_undriven(case):
    p=provider();r=overlap_reader()
    if case=='degraded':p.degraded=True
    elif case=='interface':p.child.data['type']='npiInterface'
    elif case=='input':p.child.data['Port']=[port('S')]
    elif case=='pull':p.child.data['Net'][0].data['net_type']='npiTri0'
    else:r.values['top.u.S[5]']=([],False,True,{'npi_driver_query_failed'})
    result=scan_native_ports(p,p.options,driver_reader=r)
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])


def test_native_alias_guard_and_process_grouping():
    class Pin:
        def __init__(self,name):self.name=name
        def full_name(self):return self.name
    names=['top.u.M:Always0#branch1:Assignment.O[0]',
           'top.u.M:Always0#branch2:Assignment.O[0]',
           'top.consumer.port[0]']
    net=NS(driver_list=lambda:[Pin(n) for n in names],load_list=lambda:[Pin('top.consumer.port[7:0]')])
    p=provider()
    backend=NS(_npi_modules=(None,NS(get_net=lambda _:net)),
        _format_driver=lambda h:dict(driver_kind='assign',source_file='design.sv',source_line=2))
    rows,complete,_,gaps=NpiBitDrivers(p,backend).read('top.u.S[9]',categories=set(CATEGORIES))
    assert len(rows)==1
    assert not complete and 'npi_driver_load_alias' in gaps
    assert writer_identity(names[0])==writer_identity(names[1])
    assert writer_identity('top.u.out[1:0]#[0]')!=writer_identity('top.u.out[1:0]#[1]')


def test_large_load_inventory_is_not_used_as_an_exhaustive_alias_check():
    class Pin:
        def full_name(self):return 'top.u.pin'
    net=NS(driver_list=lambda:[Pin(),Pin()],load_list=lambda:[Pin()]*257)
    p=provider();backend=NS(_npi_modules=(None,NS(get_net=lambda _:net)))
    rows,complete,_,gaps=NpiBitDrivers(p,backend).read('top.u.S[9]',categories=set(CATEGORIES))
    assert not rows and not complete and 'npi_driver_load_inventory_limit' in gaps


def test_scalar_declared_at_nonzero_index_keeps_its_coordinate():
    p=provider();p.child.data['Net']=[packed('npiNet',5,5,name='S',full_name='top.u.S',net_type='npiWire')]
    r=overlap_reader();result=scan_native_ports(p,p.options,driver_reader=r)
    assert r.seen==['top.u.S[5]']
    assert result['facts'][0]['target_bits']==[5]


def test_generate_scope_uses_its_real_module_boundary():
    p=provider();p.child.data['type']='npiGenScope';p.root.data['is_black_box']=0
    result=scan_native_ports(p,p.options,driver_reader=overlap_reader())
    assert any(f['kind']=='undriven_signal' for f in result['facts'])
    p=provider();p.child.data['type']='npiGenScope';p.root.data['is_black_box']=1
    result=scan_native_ports(p,p.options,driver_reader=overlap_reader())
    assert not any(f['kind']=='undriven_signal' for f in result['facts'])


def test_cancelled_bit_resume_preserves_the_saved_frontier():
    p=provider(semantic_max_facts=1)
    first=scan_native_ports(p,p.options,driver_reader=overlap_reader())
    checkpoint=deepcopy(first['_scan_state'])
    class Cancel:
        def read(self,*args,**kwargs):raise OperationCancelled('cancelled')
    p=provider(semantic_max_facts=1)
    with pytest.raises(OperationCancelled):
        scan_native_ports(p,p.options,state=checkpoint,driver_reader=Cancel())
    assert first['_scan_state']==checkpoint


def vector_reader(left=1,right=4,mutate=None):
    p=provider();lookups=[];indices=[]
    bit=NS(driver_list=lambda:[object()])
    def index(value):
        indices.append(value)
        return bit
    vector=NS(full_name=lambda:f'top.u.S[{left}:{right}]',left=lambda:left,
              right=lambda:right,size=lambda:abs(left-right)+1,index=index)
    if mutate:mutate(vector)
    def get_net(path):
        lookups.append(path)
        return vector if path=='top.u.S' else bit
    backend=NS(_npi_modules=(None,NS(get_net=get_net)))
    return NpiBitDrivers(p,backend),lookups,indices


@pytest.mark.parametrize('left,right',[(1,4),(3,0),(-1,-4),(-4,-1)])
def test_verified_vector_lookup_keeps_declared_bit_coordinates(left,right):
    reader,lookups,indices=vector_reader(left,right)
    bits=list(range(left,right+(-1 if left>right else 1),-1 if left>right else 1))
    for _ in range(2):
        with reader.signal('top.u.S',left,right):
            for bit in bits:
                assert reader.read(f'top.u.S[{bit}]',categories=set(CATEGORIES))==([],True,False,set())
        assert reader._signal is None
    assert lookups==['top.u.S','top.u.S']
    assert indices==bits*2


@pytest.mark.parametrize('case',['name','bounds','size','metadata_type','missing_index','index_none','index_error','metadata_error'])
def test_unverified_vector_queries_preserve_exact_name_fallback(case):
    def mutate(vector):
        if case=='name':vector.full_name=lambda:'top.other.S[1:4]'
        elif case=='bounds':vector.left=lambda:4
        elif case=='size':vector.size=lambda:8
        elif case=='metadata_type':vector.left=lambda:True
        elif case=='missing_index':vector.index=None
        elif case=='index_none':vector.index=lambda _:None
        elif case=='index_error':
            def unsupported(_):raise NotImplementedError()
            vector.index=unsupported
        else:
            def missing():raise ValueError()
            vector.full_name=missing
    reader,lookups,_=vector_reader(mutate=mutate)
    with reader.signal('top.u.S',1,4):
        reader.read('top.u.S[1]',categories=set(CATEGORIES))
        reader.read('top.u.S[2]',categories=set(CATEGORIES))
    assert lookups==['top.u.S','top.u.S[1]','top.u.S[2]']
    assert reader._signal is None


@pytest.mark.parametrize('error',[OperationCancelled('cancelled'),HierarchyBudgetExceeded('native_call_limit'),TimeoutError()])
def test_vector_terminal_failures_clear_handles_without_name_fallback(error):
    def mutate(vector):
        def stop(_):raise error
        vector.index=stop
    reader,lookups,_=vector_reader(mutate=mutate)
    with pytest.raises(type(error)):
        with reader.signal('top.u.S',1,4):
            reader.read('top.u.S[1]',categories=set(CATEGORIES))
    assert reader._signal is None
    assert lookups==['top.u.S']
