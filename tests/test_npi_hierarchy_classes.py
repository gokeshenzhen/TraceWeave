"""Class-provider controls with handwritten namespaces and invalidating capsules.

This fake SDK verifies ownership, accounting and projection, not installed NPI
semantics, declaration visibility, license use or public-tool acceptance.
"""
from contextlib import contextmanager, nullcontext
from dataclasses import FrozenInstanceError
import subprocess
import sys
import threading
from types import ModuleType, SimpleNamespace

import pytest

from src import hierarchy_source_ir as ir
from src import npi_hierarchy_source as native
from src import verdi_npi_backend as npi
from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.compile_input_capture import CaptureAborted
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_service import HierarchySourceService
from src.kdb_anchor import capture_kdb_anchor


TOP, GEN = 'top', 'top.g[0]'
LOCAL = 'top.g[0].\\class.with.dot '
DECLARATIONS = {LOCAL, 'left.Base', 'left.Child', 'left.Child#(3)', 'right.Base'}
INVENTORY_GAPS = {
    'npi_uninstantiated_declaration_inventory_unverified',
    'npi_class_specialization_inventory_unverified',
}


def forbidden(*args, **kwargs):
    pytest.fail('real SDK, eager enumeration, parsing, build or fallback reached')


@pytest.fixture(autouse=True)
def isolated_control_only(monkeypatch):
    monkeypatch.setattr(native, '_OWNER_LOCK', threading.Lock())
    monkeypatch.setattr(npi, '_import_pynpi', forbidden)
    monkeypatch.setattr(npi, 'parse_compile_log', forbidden)
    monkeypatch.setattr(npi, 'probe_verdi_backend', forbidden)
    monkeypatch.setattr(npi, '_silence_native_stdio', nullcontext)
    monkeypatch.setattr(npi, '_install_shutdown_banner_silencer', lambda: None)
    monkeypatch.setattr(npi, '_NPI_INITIALIZED_IDS', set())
    monkeypatch.setattr(npi, '_NPI_ACTIVE_DESIGNS', {})
    monkeypatch.setattr(npi, '_NPI_LOAD_GENERATIONS', {})
    monkeypatch.setattr(subprocess, 'Popen', forbidden)
    monkeypatch.setattr(subprocess, 'run', forbidden)
    previous = sys.getprofile()
    def guard(frame, event, arg):
        if event == 'call':
            path = frame.f_code.co_filename.replace('\\', '/')
            assert not any(part in path for part in (
                '/pynpi/', '/src/tb_hierarchy_builder.py', '/src/structural_scanner.py',
                '/src/slang_', '/src/source_graph_worker.py',
            )), path
    sys.setprofile(guard)
    try:
        yield
    finally:
        sys.setprofile(previous)


class Raw:
    def __init__(self, sdk, data):
        self.sdk, self.data, self.generation, self.valid = sdk, data, sdk.generation, True
        sdk.raws.append(self)


class Handle:
    def __init__(self, raw):
        self.cps_obj = raw

    def scalar(self, key):
        raw = self.cps_obj
        raw.sdk.validate(raw)
        value = raw.data.get(key)
        raw.sdk.emit(('get', raw.data.get('full_name'), key))
        return value

    def type(self): return self.scalar('type')
    def full_name(self): return self.scalar('full_name')
    def name(self): return self.scalar('name')
    def file(self): return self.scalar('file')
    def line_no(self): return self.scalar('line_no')
    def value(self): return self.scalar('value')

    def reference(self, key):
        raw = self.cps_obj
        raw.sdk.validate(raw)
        target = raw.data.get(key)
        result = Handle(raw.sdk.raw(target)) if target is not None else None
        raw.sdk.emit(('reference', raw.data.get('full_name'), key))
        return result

    def scope_handle(self): return self.reference('parent')
    def extends_handle(self): return self.reference('extends')
    def class_defn_handle(self): return self.reference('class_defn')
    def lhs_handle(self): return self.reference('lhs')
    def rhs_handle(self): return self.reference('rhs')
    def actual_handle(self): return self.reference('actual')


class Sdk:
    """Nine namespaces, five declarations, two same-named but distinct bases."""
    def __init__(self):
        def node(path, kind, parent=None, name=None):
            return dict(full_name=path, name=name or path.rsplit('.', 1)[-1],
                        type=kind, parent=parent, file='declarations.sv', line_no=17)
        self.nodes = {
            TOP: node(TOP, 'npiModule'), GEN: node(GEN, 'npiGenScope', TOP),
            LOCAL: node(LOCAL, 'npiClassDefn', GEN, '\\class.with.dot '),
            'left': node('left', 'npiPackage'), 'right': node('right', 'npiPackage'),
            'left.Base': node('left.Base', 'npiClassDefn', 'left', 'Base'),
            'left.Child': node('left.Child', 'npiClassDefn', 'left', 'Child'),
            'left.Child#(3)': node('left.Child#(3)', 'npiClassDefn', 'left', 'Child'),
            'right.Base': node('right.Base', 'npiClassDefn', 'right', 'Base'),
            'base-spec': node('base-spec', 'npiClassTypespec', name='Base'),
            'child-spec': node('child-spec', 'npiClassTypespec', name='Child'),
        }
        # Bare "Base" deliberately disagrees with the lexical package guess.
        self.nodes['left.Child']['extends'] = 'base-spec'
        self.nodes['base-spec']['class_defn'] = 'right.Base'
        self.nodes['left.Child#(3)']['extends'] = 'child-spec'
        self.nodes['child-spec']['class_defn'] = 'left.Child'
        self.roots = [TOP, 'left', 'right']
        self.children = {TOP: [GEN], GEN: [LOCAL],
                         'left': ['left.Base', 'left.Child', 'left.Child#(3)'],
                         'right': ['right.Base']}
        self.parameters = {'left.Child#(3)': [('W', 3)]}
        self.events, self.raws, self.iterators = [], [], []
        self.generation, self.hook = 1, None
        self.global_count, self.release_rc, self.global_rc = 0, 1, 1
        self.init_count = self.load_count = 0
        self.load_error = None
        self.low = SimpleNamespace(Instance='Instance', InternalScope='InternalScope',
                                   Parameter='Parameter', iterate=self.iterate,
                                   scan=self.scan, release_handle=self.release,
                                   handle_by_name=self.lookup)
        self.lang = SimpleNamespace(release_all_handles=self.release_all,
                                    get_top_inst_list=forbidden)
        self.util = SimpleNamespace(handle=Handle, get_1m_list=forbidden,
                                    GET_VALUE_ERROR='FAILED_TO_GET_VALUE')
        self.system = SimpleNamespace(init=self.init, load_design=self.load)

    def emit(self, event):
        self.events.append(event)
        if self.hook:
            self.hook(event)

    def validate(self, raw):
        assert raw.valid and raw.generation == self.generation, 'stale native capsule'

    def raw(self, path):
        return Raw(self, dict(self.nodes[path]))

    def lookup(self, path, scope):
        assert scope is None
        result = self.raw(path) if path in self.nodes else None
        self.emit(('lookup', path))
        return result

    def iterate(self, relation, reference):
        if relation == 'Instance':
            assert reference is None
            path, items = None, self.roots
        else:
            self.validate(reference)
            path = reference.data['full_name']
            assert relation in ('InternalScope', 'Parameter')
            items = (self.children if relation == 'InternalScope' else self.parameters).get(path, [])
        raw = Raw(self, dict(relation=relation, path=path, items=list(items), index=0)) if items else None
        if raw:
            self.iterators.append(raw)
        self.emit(('iterate', relation, path))
        return raw

    def scan(self, raw):
        self.validate(raw)
        data = raw.data
        index = data['index']
        if index == len(data['items']):
            raw.valid = False  # Official scan(NULL result) auto-release contract.
            result = None
        else:
            item = data['items'][index]
            data['index'] += 1
            if data['relation'] == 'Parameter':
                result = Raw(self, dict(name=item[0], value=item[1]))
            else:
                result = self.raw(item)
        self.emit(('scan', data['relation'], data['path'], index, result is None))
        return result

    def release(self, raw):
        self.validate(raw)
        assert 'relation' in raw.data, 'borrowed scope/base must not be freed individually'
        self.emit(('release', raw.data['relation'], raw.data['path']))
        if self.release_rc == 1:
            raw.valid = False
        return self.release_rc

    def release_all(self):
        self.global_count += 1
        if self.global_rc == 1:
            for raw in self.raws:
                raw.valid = False
        self.emit(('global_cleanup',))
        return self.global_rc

    def init(self, args):
        self.init_count += 1
        return 1

    def load(self, args):
        self.load_count += 1
        self.generation += 1
        if self.load_error:
            raise self.load_error
        return 1


def install(monkeypatch, sdk):
    module = ModuleType('pynpi')
    module.__path__ = []
    module.lang, module.lang_l0, module.lang_l0_util = sdk.lang, sdk.low, sdk.util
    monkeypatch.setitem(sys.modules, 'pynpi', module)


@contextmanager
def owned(monkeypatch, sdk, *, budget=None, degraded=False, failures=None, identity='maps-A'):
    install(monkeypatch, sdk)
    generation, thread = sdk.generation, threading.get_ident()
    def current():
        if sdk.generation != generation or threading.get_ident() != thread:
            raise native.NpiHierarchyUnavailable('npi_loaded_generation_changed')
    with native.language_query_owner(
        top=TOP, artifact_identity=identity, budget=budget or HierarchyQueryBudget(),
        require_owner=current, degraded=degraded,
        cleanup_failed=lambda: failures.append('failed') if failures is not None else None,
    ) as provider:
        yield provider


def observe(provider):
    with provider.open_classes(provider.budget) as stream:
        facts = list(stream)
        return facts, stream.complete, set(stream.gaps)


def assert_closed(sdk):
    assert not any(raw.valid for raw in sdk.raws)
    assert sdk.global_count == 1


def rows(result):
    pending, result_rows = list(result['roots']), {}
    while pending:
        row = pending.pop()
        result_rows[row['qualified_name']] = row
        pending.extend(row['children'])
    return result_rows


def test_qualified_root_is_observed_before_global_inventory_and_not_duplicated(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as provider:
        provider._class_query_root = 'left.Child'
        facts, complete, gaps = observe(provider)
        assert facts[0].symbol.qualified_name == 'left.Child'
        assert facts[0].base_qualified_name == 'right.Base'
        assert len(facts) == len({fact.symbol.symbol_id for fact in facts}) == 5
        assert not complete and INVENTORY_GAPS <= gaps
    assert_closed(sdk)


def test_qualified_root_survives_bounded_unrelated_inventory(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk, budget=HierarchyQueryBudget(max_scopes=1)) as provider:
        provider._class_query_root = 'left.Child'
        result = HierarchySourceService(provider).class_hierarchy(root_class='left.Child')
        assert result['roots'][0]['qualified_name'] == 'left.Child'
        assert result['roots'][0]['base_qualified_name'] == 'right.Base'
        assert result['coverage_status'] == 'partial'
        assert 'scope_limit' in result['analysis_limits']
        assert not result['class_inventory_complete']
    assert_closed(sdk)


def test_qualified_root_conflict_with_later_inventory_is_rejected(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as provider:
        provider._class_query_root = 'left.Child'
        stream = native._Classes(provider)
        try:
            seed = next(stream)
            assert seed.symbol.qualified_name == 'left.Child'
            sdk.nodes['left.Child']['line_no'] += 1
            with pytest.raises(native.NpiHierarchyUnavailable, match='npi_class_identity_conflict'):
                list(stream)
        finally:
            stream.close()
    assert_closed(sdk)


class BaseArgumentSdk(Sdk):
    """Declared base W=3 and an independent extends Typespec actual W=5."""
    def __init__(self):
        super().__init__()
        self.low.ParamAssign = 'ParamAssign'
        self.nodes.update({
            'arg-W': dict(full_name='arg-W', type='npiParamAssign', lhs='formal-W', rhs='actual-W'),
            'formal-W': dict(full_name='formal-W', type='npiParameter', name='W', value=3),
            'actual-W': dict(full_name='actual-W', type='npiConstant', value=5),
        })
        self.parameters['right.Base'] = [('W', 3)]
        self.assignments = ['arg-W']

    def iterate(self, relation, reference):
        if relation != 'ParamAssign':
            return super().iterate(relation, reference)
        self.validate(reference)
        path = reference.data['full_name']
        items = self.assignments if path == 'base-spec' else []
        raw = Raw(self, dict(relation=relation, path=path, items=list(items), index=0)) if items else None
        if raw is not None:
            self.iterators.append(raw)
        self.emit(('iterate', relation, path))
        return raw


@pytest.mark.parametrize('reference', [False, True])
def test_extends_actual_value_is_separate_from_default_declaration(monkeypatch, reference):
    from src.schemas import SourceClassNode
    sdk = BaseArgumentSdk()
    if reference:
        sdk.nodes['ref-W'] = dict(full_name='ref-W', type='npiRefObj', actual='formal-W')
        sdk.nodes['arg-W']['lhs'] = 'ref-W'
    with owned(monkeypatch, sdk) as provider:
        result = HierarchySourceService(provider).class_hierarchy(root_class='right.Base')
        base = result['roots'][0]
        child = next(row for row in base['children'] if row['qualified_name'] == 'left.Child')
        assert base['parameterization'] == {'W': '3'}
        assert child['base_value_parameters'] == {'W': '5'}
        assert child['base_parameter_status'] == 'observed'
        assert child['base_relation'] == 'declaration'
        assert child['base_symbol_id'] == base['symbol_id']
        assert 'npi_class_base_specialization_unverified' in child['binding_gaps']
        assert not result['class_inventory_complete']
        assert SourceClassNode.model_validate(child).base_value_parameters == {'W': '5'}
        with pytest.raises(ValueError):
            SourceClassNode.model_validate(dict(child, base_parameter_status='not_observed'))
    assert_closed(sdk)


@pytest.mark.parametrize('mode', ['nonconstant', 'type_parameter', 'duplicate',
                                 'invalid_first', 'invalid_last', 'limit', 'cancel'])
def test_extends_actuals_do_not_invent_values_and_release_iterators(monkeypatch, mode):
    sdk = BaseArgumentSdk()
    if mode == 'nonconstant':
        sdk.nodes['actual-W']['type'] = 'npiOperation'
    elif mode == 'type_parameter':
        sdk.nodes['formal-W']['type'] = 'npiTypeParameter'
    elif mode == 'duplicate':
        sdk.assignments *= 2
    elif mode in {'invalid_first', 'invalid_last'}:
        sdk.nodes['bad-value'] = dict(full_name='bad-value', type='npiOperation')
        sdk.nodes['bad-arg'] = dict(full_name='bad-arg', type='npiParamAssign',
                                   lhs='formal-W', rhs='bad-value')
        sdk.assignments = ['bad-arg', 'arg-W'] if mode == 'invalid_first' else ['arg-W', 'bad-arg']
    elif mode == 'limit':
        # Unknown formal objects consume work without positive names/facts.
        sdk.assignments *= 130
        sdk.nodes['formal-W']['type'] = 'npiTypeParameter'
    elif mode == 'cancel':
        def cancel(event):
            if event[:3] == ('scan', 'ParamAssign', 'base-spec'):
                raise OperationCancelled()
        sdk.hook = cancel
    with owned(monkeypatch, sdk) as provider:
        if mode in {'duplicate', 'invalid_first', 'invalid_last', 'cancel'}:
            expected = OperationCancelled if mode == 'cancel' else native.NpiHierarchyUnavailable
            with pytest.raises(expected):
                observe(provider)
        else:
            facts, _, _ = observe(provider)
            child = next(f for f in facts if f.symbol.qualified_name == 'left.Child')
            assert child.base_value_parameters == ()
            assert child.base_parameter_status == 'partial'
            if mode == 'limit':
                assert 'npi_class_base_parameter_limit' in child.gaps
    assert_closed(sdk)


def test_global_namespaces_real_base_identity_and_generate_scope_are_copied(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as p:
        facts, complete, gaps = observe(p)
        by_name = {f.symbol.qualified_name: f for f in facts}
        assert set(by_name) == DECLARATIONS
        assert p.budget.scope_count == 9  # top/g/local + left/3 classes + right/Base.
        assert not complete and INVENTORY_GAPS <= gaps
        assert by_name['left.Child'].base_symbol_id == by_name['right.Base'].symbol.symbol_id
        assert by_name['left.Child'].base_symbol_id != by_name['left.Base'].symbol.symbol_id
        assert by_name['left.Child'].base_qualified_name == 'right.Base'
        assert by_name['left.Child#(3)'].base_symbol_id == by_name['left.Child'].symbol.symbol_id
        assert len({f.symbol.symbol_id for f in facts}) == 5
        assert by_name['left.Base'].symbol.scope_id == by_name['left.Child'].symbol.scope_id
        assert by_name['right.Base'].symbol.scope_id != by_name['left.Base'].symbol.scope_id
        local = by_name[LOCAL].symbol
        scope = p.lookup_scope(GEN, p.budget).fact
        assert local.scope_id == scope.scope_id
        assert local.name == '\\class.with.dot ' and local.qualified_name == LOCAL
        assert local.location.file == 'declarations.sv' and local.location.line == 17
        spec = by_name['left.Child#(3)'].symbol
        assert spec.parameterization == (('W', '3'),) and spec.type_id is None
        assert spec.declaration_id == spec.symbol_id
        assert 'npi_type_parameter_inventory_unverified' in spec.gaps
        assert 'npi_class_specialization_identity_unverified' in spec.gaps
        with pytest.raises(FrozenInstanceError):
            spec.name = 'changed'
    assert_closed(sdk)
    assert local.row()['qualified_name'] == LOCAL


def test_class_renderer_keeps_native_inventory_partial_and_rendered_count_exact(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as p:
        result = HierarchySourceService(p).class_hierarchy()
        found = rows(result)
        assert set(found) == DECLARATIONS and result['total'] == 5
        assert [r['qualified_name'] for r in result['roots']] == ['left.Base', 'right.Base', LOCAL]
        assert [r['qualified_name'] for r in found['right.Base']['children']] == ['left.Child']
        assert result['facts_origin'] == 'verdi_npi' and result['artifact_identity'] == 'maps-A'
        assert result['view'] == 'static_class_inheritance'
        assert result['class_inventory_domain'] == 'provider_declarations'
        assert result['class_inventory_count'] is None
        assert result['class_inventory_lower_bound'] == 5
        assert not result['class_inventory_complete']
        assert result['coverage_status'] == 'partial' and result['analysis_limits'] == []
        assert INVENTORY_GAPS <= set(result['coverage_gaps'])
        assert result['query_metrics']['symbol_count'] == 5
        assert result['query_metrics']['scope_count'] == 9
    assert_closed(sdk)


def test_class_display_limit_does_not_reduce_namespace_work(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as p:
        result = HierarchySourceService(p).class_hierarchy(root_class='right.Base', depth=0)
        assert result['total'] == 1 and result['output_truncated']
        assert result['class_inventory_lower_bound'] == 5
        assert result['query_metrics']['scope_count'] == 9
        assert result['analysis_limits'] == []


def test_early_close_releases_only_live_iterators_and_exhaustion_does_not_double_free(monkeypatch):
    sdk = Sdk()
    with owned(monkeypatch, sdk) as p:
        with p.open_classes(p.budget) as stream:
            assert next(stream).symbol.qualified_name == LOCAL
        assert [e for e in sdk.events if e[0] == 'release'] == [
            ('release', 'InternalScope', GEN), ('release', 'InternalScope', TOP),
            ('release', 'Instance', None),
        ]
        assert ('iterate', 'InternalScope', LOCAL) not in sdk.events
        release_count = sum(e[0] == 'release' for e in sdk.events)
        facts, complete, _ = observe(p)
        assert len(facts) == 5 and not complete
        assert sum(e[0] == 'release' for e in sdk.events) == release_count
        assert not any(raw.valid for raw in sdk.iterators)
    assert_closed(sdk)


@pytest.mark.parametrize('mode,gap', [
    ('no_roots', 'npi_class_roots_unavailable'),
    ('unknown', 'npi_namespace_kind_unmodeled'),
    ('array', 'npi_array_container_children_unavailable'),
    ('duplicate', 'npi_namespace_alias_or_cycle'),
    ('degraded', 'npi_degraded_design'),
])
def test_namespace_gaps_never_become_complete_empty_inventory(monkeypatch, mode, gap):
    sdk = Sdk()
    if mode == 'no_roots':
        sdk.roots = []
    elif mode in ('unknown', 'array'):
        sdk.nodes['extra'] = dict(full_name='extra', name='extra', parent=None,
                                  type='npiOpaqueFutureScope' if mode == 'unknown' else 'npiModuleArray')
        sdk.roots.append('extra')
        sdk.children['extra'] = ['left.Base']  # Must not traverse unsupported relation.
    elif mode == 'duplicate':
        sdk.roots.append('left')
    with owned(monkeypatch, sdk, degraded=mode == 'degraded') as p:
        result = HierarchySourceService(p).class_hierarchy(root_class='absent')
        assert result['error'] == 'class_unresolved'
        assert result['total'] == 0 and result['class_inventory_count'] is None
        assert gap in result['coverage_gaps'] and result['coverage_status'] == 'partial'
        assert result['class_inventory_lower_bound'] == (0 if mode == 'no_roots' else 5)
        if mode in ('unknown', 'array'):
            assert ('iterate', 'InternalScope', 'extra') not in sdk.events
        if mode == 'duplicate':
            assert p.budget.scope_count == 10  # duplicate visit is work too.
    assert_closed(sdk)


@pytest.mark.parametrize('field,value,reason', [
    ('parent', 'right', 'npi_namespace_parent_mismatch'),
    ('full_name', '', 'npi_namespace_identity_unavailable'),
    ('type', None, 'npi_namespace_identity_unavailable'),
])
def test_invalid_namespace_identity_or_parent_aborts_and_closes_stack(monkeypatch, field, value, reason):
    sdk = Sdk()
    sdk.nodes[LOCAL][field] = value
    with pytest.raises(native.NpiHierarchyUnavailable, match=reason):
        with owned(monkeypatch, sdk) as p:
            observe(p)
    assert_closed(sdk)
    assert ('release', 'Instance', None) in sdk.events


@pytest.mark.parametrize('kind,missing', [('npiClassTypespec', True), ('npiUnsupportedTypespec', False)])
def test_unbound_typespec_bare_name_cannot_bind_to_local_class(monkeypatch, kind, missing):
    sdk = Sdk()
    sdk.nodes['base-spec']['type'] = kind
    if missing:
        sdk.nodes['base-spec'].pop('class_defn')
    with owned(monkeypatch, sdk) as p:
        facts, _, _ = observe(p)
        child = next(f for f in facts if f.symbol.qualified_name == 'left.Child')
        assert child.base_status == 'unresolved'
        assert child.base_symbol_id is child.base_qualified_name is None
        assert child.gaps == ('npi_class_base_binding_unavailable',)


def test_resolved_base_outside_visited_namespaces_is_reference_only_not_declaration(monkeypatch):
    sdk = Sdk()
    sdk.roots.remove('right')
    with owned(monkeypatch, sdk) as p:
        result = HierarchySourceService(p).class_hierarchy(root_class='right.Base')
        found = rows(result)
        assert set(found) == {'right.Base', 'left.Child', 'left.Child#(3)'}
        assert found['right.Base']['external'] and found['right.Base']['reference_only']
        assert result['total'] == 3 and result['class_inventory_lower_bound'] == 4
        assert 'class_base_outside_inventory' in result['coverage_gaps']
        assert result['class_inventory_count'] is None and not result['class_inventory_complete']


def test_declaration_ids_are_stable_per_artifact_and_change_with_artifact(monkeypatch):
    sdk = Sdk()
    inventories = []
    for identity in ('maps-A', 'maps-A', 'maps-B'):
        with owned(monkeypatch, sdk, identity=identity) as p:
            facts, _, _ = observe(p)
            inventories.append({f.symbol.qualified_name: (f.symbol.symbol_id, f.symbol.scope_id)
                                for f in facts})
    assert inventories[0] == inventories[1]
    assert all(inventories[0][name][0] != inventories[2][name][0] for name in DECLARATIONS)
    assert all(inventories[0][name][1] != inventories[2][name][1] for name in DECLARATIONS)
    assert sdk.global_count == 3 and not any(raw.valid for raw in sdk.raws)


@pytest.mark.parametrize('limits,reason,observed', [
    ({'max_scopes': 1}, 'scope_limit', 0),
    ({'max_depth': 1}, 'scope_depth_limit', 0),
    ({'max_calls': 1}, 'native_call_limit', 0),
    ({'max_bytes': 1}, 'fact_byte_limit', 0),
    ({'max_symbols': 2}, 'symbol_limit', 2),
])
def test_namespace_and_declaration_work_limits_close_resources_without_negative_proof(
    monkeypatch, limits, reason, observed,
):
    sdk = Sdk()
    budget = HierarchyQueryBudget(**limits)
    with owned(monkeypatch, sdk, budget=budget) as p:
        result = HierarchySourceService(p).class_hierarchy()
        assert result['class_inventory_lower_bound'] == observed
        assert result['class_inventory_count'] is None and not result['class_inventory_complete']
        assert result['coverage_status'] == 'partial'
        assert result['analysis_limits'] == [reason]
        assert INVENTORY_GAPS <= set(result['coverage_gaps']) or reason == 'native_call_limit'
    assert_closed(sdk)


@pytest.mark.parametrize('values,gap', [
    ([('W', 'FAILED_TO_GET_VALUE')] * 130, 'npi_parameter_value_unavailable'),
    ([('W', 3)] * 130, 'npi_parameter_name_ambiguous'),
])
def test_parameter_scan_cap_applies_to_invalid_and_duplicate_values(monkeypatch, values, gap):
    sdk = Sdk()
    sdk.parameters[LOCAL] = values
    with owned(monkeypatch, sdk) as p:
        facts, _, _ = observe(p)
        local = next(f.symbol for f in facts if f.symbol.qualified_name == LOCAL)
        assert {'npi_parameter_limit', gap, 'npi_type_parameter_inventory_unverified'} <= set(local.gaps)
        assert local.type_id is None
        assert sum(e[:3] == ('scan', 'Parameter', LOCAL) for e in sdk.events) == 129
        assert ('release', 'Parameter', LOCAL) in sdk.events
        assert local.parameterization == (() if gap == 'npi_parameter_value_unavailable' else (('W', '3'),))
    assert_closed(sdk)


@pytest.mark.parametrize('error', [TimeoutError('explicit getter timeout'),
                                 CaptureAborted('input_changed'),
                                 OperationCancelled('cancelled'), RuntimeError('SDK failed')])
def test_typed_and_ordinary_getter_errors_propagate_original_and_close_stack(monkeypatch, error):
    sdk = Sdk()
    def hook(event):
        if event == ('get', LOCAL, 'name'):
            raise error
    sdk.hook = hook
    with pytest.raises(type(error)) as caught:
        with owned(monkeypatch, sdk) as p:
            HierarchySourceService(p).class_hierarchy()
    assert caught.value is error
    assert_closed(sdk)


def test_cancellation_after_scan_still_releases_live_namespace_stack(monkeypatch):
    sdk, event = Sdk(), threading.Event()
    sdk.hook = lambda e: event.set() if e[:3] == ('scan', 'InternalScope', GEN) else None
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            with owned(monkeypatch, sdk) as p:
                observe(p)
    finally:
        pop_cancel_event(token)
    assert_closed(sdk)


def test_changed_owner_does_not_release_old_or_other_design_handles(monkeypatch):
    sdk, failures = Sdk(), []
    with pytest.raises(native.NpiHierarchyUnavailable, match='npi_loaded_generation_changed'):
        with owned(monkeypatch, sdk, failures=failures) as p:
            with p.open_classes(p.budget) as stream:
                assert next(stream).symbol.qualified_name == LOCAL
                sdk.generation += 1
                with pytest.raises(native.NpiHierarchyUnavailable, match='npi_loaded_generation_changed'):
                    next(stream)
    assert not [e for e in sdk.events if e[0] in ('release', 'global_cleanup')]
    assert failures == ['failed']
    assert not p.active


def test_foreign_thread_cannot_consume_live_class_stream(monkeypatch):
    sdk, errors = Sdk(), []
    with owned(monkeypatch, sdk) as p:
        with p.open_classes(p.budget) as stream:
            assert next(stream).symbol.qualified_name == LOCAL
            before = list(sdk.events)
            def foreign_read():
                try:
                    next(stream)
                except BaseException as exc:
                    errors.append(exc)
            thread = threading.Thread(target=foreign_read)
            thread.start()
            thread.join(timeout=5)
            assert not thread.is_alive()
            assert len(errors) == 1 and isinstance(errors[0], native.NpiHierarchyUnavailable)
            assert errors[0].reason == 'npi_language_owner_unavailable'
            assert sdk.events == before
            assert next(stream).symbol.qualified_name == 'left.Base'
    assert_closed(sdk)


def test_late_null_scan_is_budget_stop_and_never_double_released(monkeypatch):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    sdk = Sdk()
    def hook(event):
        if event == ('scan', 'Instance', None, 3, True):
            clock[0] = 12.0
    sdk.hook = hook
    with owned(monkeypatch, sdk, budget=HierarchyQueryBudget(timeout_sec=1)) as p:
        result = HierarchySourceService(p).class_hierarchy()
        assert result['class_inventory_lower_bound'] == 5
        assert result['class_inventory_count'] is None and not result['class_inventory_complete']
        assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
        assert ('release', 'Instance', None) not in sdk.events
    assert_closed(sdk)


def test_iterator_cleanup_failure_is_not_class_success_and_global_cleanup_still_runs(monkeypatch):
    sdk, failures = Sdk(), []
    sdk.release_rc = 0  # Unconfirmed cleanup is neither silently accepted nor retried.
    with pytest.raises(native.NpiHierarchyUnavailable, match='npi_iterator_cleanup_unconfirmed'):
        with owned(monkeypatch, sdk, failures=failures) as p:
            with p.open_classes(p.budget) as stream:
                next(stream)
    assert_closed(sdk)
    assert len([e for e in sdk.events if e[0] == 'release']) == 3
    assert failures == ['failed']


def backend(monkeypatch, tmp_path):
    sdk = Sdk()
    install(monkeypatch, sdk)
    monkeypatch.setattr(npi, '_import_pynpi', lambda: (sdk.system, SimpleNamespace()))
    directory = tmp_path / 'kdb'
    directory.mkdir()
    (directory / '_libFileMap').write_bytes(b'lib-A')
    (directory / '_stnElabDB').write_bytes(b'elab-A')
    anchor = capture_kdb_anchor(str(directory))
    fallback = SimpleNamespace(explain_signal_driver=forbidden, find_signal_loads=forbidden,
                               trace_signal_path=forbidden, get_dynamic_step=forbidden)
    result = npi.VerdiNpiBackend(fallback)
    result.bind_compile_context({'simulator': 'vcs', 'primary_top': TOP, 'files': {'user': []}})
    result.bind_kdb_anchor(anchor)
    return result, sdk, anchor


@pytest.mark.parametrize('arguments', [{'extra': True}, {'root_class': ''}, {'depth': True}, {'max_nodes': 0}])
def test_class_prevalidation_does_not_load_or_clean_language(monkeypatch, tmp_path, arguments):
    target, sdk, _ = backend(monkeypatch, tmp_path)
    with pytest.raises(ValueError):
        target.query_hierarchy('get_tb_class_hierarchy', arguments, top=TOP)
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0


def test_busy_language_owner_prevents_class_load(monkeypatch, tmp_path):
    target, sdk, _ = backend(monkeypatch, tmp_path)
    assert native._OWNER_LOCK.acquire(False)
    try:
        with pytest.raises(native.NpiHierarchyUnavailable, match='npi_language_owner_busy'):
            target.query_hierarchy('get_tb_class_hierarchy', {}, top=TOP)
    finally:
        native._OWNER_LOCK.release()
    assert sdk.init_count == sdk.load_count == sdk.global_count == 0


def test_class_load_typed_timeout_does_not_fallback_or_cleanup_unowned_handles(monkeypatch, tmp_path):
    target, sdk, _ = backend(monkeypatch, tmp_path)
    error = TimeoutError('load timed out')
    sdk.load_error = error
    with pytest.raises(TimeoutError) as caught:
        target.query_hierarchy('get_tb_class_hierarchy', {}, top=TOP)
    assert caught.value is error
    assert sdk.load_count == 1 and sdk.global_count == 0
    assert not sdk.iterators


@pytest.mark.parametrize('late_phase', ['cleanup', 'final_validation'])
def test_late_class_query_deadline_keeps_rendered_total_but_no_inventory_proof(monkeypatch, tmp_path, late_phase):
    clock = [10.0]
    monkeypatch.setattr(ir.time, 'monotonic', lambda: clock[0])
    target, sdk, anchor = backend(monkeypatch, tmp_path)
    budget = HierarchyQueryBudget(timeout_sec=1)
    if late_phase == 'cleanup':
        sdk.hook = lambda e: clock.__setitem__(0, 12.0) if e == ('global_cleanup',) else None
    else:
        original = type(anchor).require_current
        def validate(self):
            value = original(self)
            if sdk.global_count == 1:
                clock[0] = 12.0
            return value
        monkeypatch.setattr(type(anchor), 'require_current', validate)
    result = target.query_hierarchy('get_tb_class_hierarchy', {}, top=TOP, budget=budget)
    assert result['total'] == 5 and set(rows(result)) == DECLARATIONS
    assert result['class_inventory_lower_bound'] == 5
    assert result['class_inventory_count'] is None and not result['class_inventory_complete']
    assert result['coverage_status'] == 'partial' and result['analysis_limits'] == ['timeout_sec']
    assert 'count_status' not in result  # Rendered count is not an instance total.
    assert result['query_metrics']['query_with_cleanup_validation_ms'] >= 2000
    assert sdk.init_count == sdk.load_count == 1
    assert_closed(sdk)
