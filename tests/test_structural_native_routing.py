"""Public dispatch/MCP controls: mocks are never evidence of a live NPI run."""
import asyncio
import json
from types import SimpleNamespace as NS
import time

import pytest

import server
from src import structural_native_runtime as native
from src.design_backend_binding import ExecutionBindingChanged
from src.design_context_runtime import ContextUnavailable
from src.design_identity import DesignIdentity
from src.kdb_anchor import KdbBindingChanged
from src.npi_hierarchy_source import NpiHierarchyUnavailable
from src.structural_scan_runtime import StructuralScanRuntime
from src.structural_semantic_runtime import SemanticScanRuntime
from tests.test_structural_scan_runtime import context


@pytest.mark.anyio
@pytest.mark.parametrize('mode',['auto','deep'])
async def test_driver_categories_use_native_before_any_source_build(setup,monkeypatch,mode):
    from src.structural_driver_facts import driver_fact
    args,calls,_=setup
    categories=['multiple_driver','undriven_signal']
    def query(*args,**kwargs):
        calls.append('native_driver_scan')
        fact=driver_fact('multiple_driver','top.n',[0],
                         [{'driver_id':'one'},{'driver_id':'two'}],complete=True)
        return dict(status='complete',categories_checked=categories,gaps=[],
                    category_results={c:dict(status='complete',gaps=[]) for c in categories},
                    facts=[fact],total_facts=1,metrics={})
    async def source(**kwargs):pytest.fail('complete native driver scan started source supplementation')
    monkeypatch.setattr(native,'query_native_scan',query)
    monkeypatch.setattr(server._semantic_scan_runtime,'scan',source)
    result=await server._dispatch('scan_structural_risks',
        {**args,'analysis_mode':mode,'semantic_categories':categories})
    assert result.semantic.status=='complete'
    assert result.semantic.backend=='verdi_npi'
    assert 'native_driver_scan' in calls


@pytest.fixture
def setup(tmp_path, monkeypatch):
    log, source, _ = context(tmp_path, 'module top; wire [3:0] x; endmodule\n')
    log.write_text(f"Command: vcs -sverilog {source} -top top\nParsing design file '{source}'\nTop Level Modules:\n top\n")
    calls = []
    monkeypatch.setattr(server, '_structural_scan_runtime', StructuralScanRuntime())
    monkeypatch.setattr(server, '_semantic_scan_runtime', SemanticScanRuntime())
    monkeypatch.setattr(server, '_structural_native_runtime', native.StructuralNativeRuntime())
    monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION', 'local')
    monkeypatch.delenv('TRACEWEAVE_CONNECTIVITY_ROUTE', raising=False)
    inputs = NS(selected_top='top', require_current=lambda **kw: None)
    binding = NS(npi_candidate=True, anchor=NS(identity_sha256='kdb'), execution='local', route='auto',
                 runtime_environment=(), allow_degraded=False, metrics=(),
                 backend_status=lambda: {'kdb_path':'candidate'}, require_current=lambda **kw: None)
    def read(*a, **kw): calls.append('metadata'); return inputs
    def probe(*a, **kw): calls.append('probe'); return binding
    backend = NS(bind_native_inputs=lambda i: calls.append('bind'), bind_kdb_anchor=lambda a: None)
    def query(*a, **kw):
        calls.append('native')
        return dict(status='complete', categories_checked=['constant_connection', 'open_input'],
                    category_results={c:{'status':'complete','gaps':[]} for c in ['constant_connection','open_input']},
                    facts=[{'kind':'constant_connection', 'instance':'top.u', 'port':'a'}], total_facts=1,
                    artifact_identity='native-artifact', gaps=[], metrics={'native_call_count':4})
    monkeypatch.setattr(native, 'read_native_inputs', read)
    monkeypatch.setattr(native, 'prepare_native_binding', probe)
    monkeypatch.setattr(native, 'select_backend', lambda *a,**kw: backend)
    monkeypatch.setattr(native, 'query_native_scan', query)
    return dict(compile_log=str(log), simulator='vcs'), calls, source


@pytest.mark.anyio
async def test_fast_does_not_discover_load_submit_or_launch(setup, monkeypatch):
    args, calls, _ = setup
    def forbidden(*a, **kw): pytest.fail('fast invoked a semantic backend')
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', forbidden)
    r = await server._dispatch('scan_structural_risks', {**args, 'analysis_mode':'fast'})
    assert not calls and r.semantic.status == 'not_run'


@pytest.mark.anyio
async def test_auto_is_native_first_and_default_comparison_remains_missing(setup):
    args, calls, _ = setup
    r = await server._dispatch('scan_structural_risks', args)
    assert calls[:2] == ['metadata','probe'] and calls.count('native') == 1
    assert r.semantic.status == 'partial' and r.semantic.facts[0]['backend'] == 'verdi_npi'
    assert next(c for c in r.semantic.category_coverage if c.category == 'constant_comparison').status == 'not_run'
    assert r.lexical_coverage_status == 'complete' and r.coverage_warnings
    # Go through the actual MCP wrapper/normalizer as well as dispatch.
    texts = await server.call_tool('scan_structural_risks', {**args, 'semantic_categories':['constant_connection','open_input']})
    payload = json.loads(texts[0].text)
    assert payload['semantic']['status'] == 'complete'
    summary = server._extract_structural_scan_summary(server._result_cache['scan_structural_risks'])
    assert summary['semantic_category_coverage'][0]['backend'] == 'verdi_npi'


@pytest.mark.anyio
async def test_auto_cache_miss_checks_source_once_after_native(setup, monkeypatch):
    args, _, _ = setup
    original_current, query = DesignIdentity.current, native.query_native_scan
    in_semantic, validations = False, []
    def current(identity):
        if in_semantic:
            validations.append(identity.digest)
        return original_current(identity)
    def run(*a, **kw):
        nonlocal in_semantic
        result = query(*a, **kw)
        in_semantic = True
        return result
    monkeypatch.setattr(DesignIdentity, 'current', current)
    monkeypatch.setattr(native, 'query_native_scan', run)
    r = await server._dispatch('scan_structural_risks', args)
    assert r.semantic.status == 'partial' and not r.semantic.termination_reason
    assert len(validations) == 1
    missing = next(c for c in r.semantic.category_coverage if c.category == 'constant_comparison')
    assert missing.status == 'not_run' and 'semantic_cache_miss' in missing.gaps


@pytest.mark.anyio
@pytest.mark.parametrize('phase', ['native', 'cache_probe'])
@pytest.mark.parametrize('change', ['bytes', 'include_choice'])
async def test_auto_miss_final_guard_discards_changed_source(setup, monkeypatch, phase, change):
    from src.compile_session_snapshot import FileContentSnapshot
    args, _, source = setup
    if change == 'include_choice':
        source.write_text('`include "late.svh"\n' + source.read_text())
    def mutate():
        if change == 'bytes':
            source.write_text(source.read_text().replace(' x;', ' y;'))
        else:
            (source.parent / 'late.svh').write_text('`define NEW 1\n')
    # Full hashing and include-choice checks must survive timestamp coarsening.
    monkeypatch.setattr(FileContentSnapshot, 'current', lambda _: True)
    query = native.query_native_scan
    def run(*a, **kw):
        result = query(*a, **kw)
        if phase == 'native':
            mutate()
        return result
    async def probe(*a, **kw):
        if phase == 'cache_probe':
            mutate()
        return None, None
    monkeypatch.setattr(native, 'query_native_scan', run)
    monkeypatch.setattr(server, '_structural_query_context', probe)
    r = await server._dispatch('scan_structural_risks', args)
    assert r.semantic.termination_reason == 'compile_metadata_or_native_identity_changed'
    assert not r.semantic.facts and not r.semantic.continuation
    assert server._structural_native_runtime._backend is None


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['before_reuse', 'during_reuse'])
async def test_auto_cached_source_has_content_guards_before_and_after_reuse(setup, monkeypatch, change):
    from src import structural_semantic_runtime
    args, _, source = setup
    async def build(payload, python):
        return dict(status='complete', categories_checked=payload['categories'],
                    facts=[{'kind':'constant_comparison'}], total_facts=1,
                    counts={'constant_comparison':1}, gaps=[])
    monkeypatch.setattr(structural_semantic_runtime, 'run_isolated_scan', build)
    first = await server._dispatch('scan_structural_risks', {**args, 'analysis_mode':'deep'})
    assert first.semantic.status == 'complete'
    original_current, query = DesignIdentity.current, native.query_native_scan
    in_semantic, validations = False, []
    def current(identity):
        if in_semantic:
            validations.append(identity.digest)
        return original_current(identity)
    def run(*a, **kw):
        nonlocal in_semantic
        result = query(*a, **kw)
        in_semantic = True
        return result
    monkeypatch.setattr(DesignIdentity, 'current', current)
    monkeypatch.setattr(native, 'query_native_scan', run)
    second = await server._dispatch('scan_structural_risks', args)
    assert second.semantic.status == 'complete' and second.semantic.backend == 'mixed'
    assert second.semantic.cache_disposition == 'hit_memory' and len(validations) == 2
    cache = server._semantic_scan_runtime._cache
    if change == 'before_reuse':
        def changed_query(*a, **kw):
            result = run(*a, **kw)
            source.write_text(source.read_text().replace(' x;', ' y;'))
            return result
        monkeypatch.setattr(native, 'query_native_scan', changed_query)
    class ChangeDuringRead(type(cache)):
        def __getitem__(self, key):
            assert change == 'during_reuse', 'a stale source cache was consumed'
            data = super().__getitem__(key)
            source.write_text(source.read_text().replace(' x;', ' y;'))
            return data
    monkeypatch.setattr(server._semantic_scan_runtime, '_cache', ChangeDuringRead(cache))
    third = await server._dispatch('scan_structural_risks', args)
    assert third.semantic.termination_reason == 'compile_metadata_or_native_identity_changed'
    assert not third.semantic.facts


@pytest.mark.anyio
@pytest.mark.parametrize('change', ['native_policy', 'source_policy', 'cancel', 'timeout'])
async def test_auto_cache_probe_retains_terminal_request_guards(setup, monkeypatch, change):
    args, _, _ = setup
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '1')
    async def probe(*a, **kw):
        if change == 'native_policy':
            monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION', 'lsf')
        elif change == 'source_policy':
            monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
        elif change == 'cancel':
            raise asyncio.CancelledError()
        else:
            await asyncio.sleep(.1)
        return None, None
    def forbidden(*a, **kw):
        pytest.fail('a terminal cache probe advanced source work')
    monkeypatch.setattr(server, '_structural_query_context', probe)
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', forbidden)
    if change == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            await server._dispatch('scan_structural_risks', args)
    else:
        r = await server._dispatch('scan_structural_risks', {**args, 'semantic_timeout_sec':.05})
        expected = 'semantic_timeout' if change == 'timeout' else 'npi_execution_policy_changed'
        assert r.semantic.termination_reason == expected and not r.semantic.facts


@pytest.mark.anyio
async def test_deep_does_not_build_completed_native_categories_with_frontend_disabled(setup, monkeypatch):
    args, calls, source = setup
    monkeypatch.setenv('TRACEWEAVE_CONNECTIVITY_ROUTE', 'auto')
    monkeypatch.setenv('TRACEWEAVE_SOURCE_GRAPH', '0')
    def forbidden(*a, **kw): pytest.fail('completed NPI categories launched source')
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', forbidden)
    source.unlink()  # Incomplete source capture must not suppress NPI.
    r = await server._dispatch('scan_structural_risks', {**args,'analysis_mode':'deep',
                          'semantic_categories':['constant_connection','open_input']})
    assert r.semantic.status == 'complete' and calls.count('native') == 1
    assert r.lexical_coverage_status != 'complete'


@pytest.mark.anyio
async def test_source_supplies_only_missing_categories_and_keeps_provenance(setup, monkeypatch):
    args, calls, _ = setup
    async def source(**kw):
        calls.append('source')
        assert kw['options']['categories'] == ['constant_comparison']
        assert kw['deadline'] > time.monotonic()
        return dict(status='complete', categories_checked=['constant_comparison'], gaps=[],
                    total_facts=1, facts=[{'kind':'constant_comparison'}], metrics={'frontend_build_count':0})
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', source)
    r = await server._dispatch('scan_structural_risks', {**args,'analysis_mode':'deep'})
    assert r.semantic.status == 'complete' and r.semantic.backend == 'mixed'
    assert calls.index('native') < calls.index('source')
    assert [f['backend'] for f in r.semantic.facts] == ['slang', 'verdi_npi']  # normalized category order
    shrunk = server._shrink_scan_structural_risks_terminal(r)
    assert not shrunk.semantic.facts and shrunk.semantic.total_facts == 2
    assert shrunk.semantic.category_coverage == r.semantic.category_coverage
    assert shrunk.semantic.result_groups == r.semantic.result_groups


@pytest.mark.anyio
@pytest.mark.parametrize('failure,reason', [
    (KdbBindingChanged(), 'kdb_identity_changed'),
    (ExecutionBindingChanged(), 'npi_execution_policy_changed'),
    (TimeoutError(), 'semantic_timeout'),
    (ContextUnavailable('npi_lsf_result_invalid'), 'npi_lsf_result_invalid'),
])
async def test_terminal_failures_never_fallback_or_publish_native(setup, monkeypatch, failure, reason):
    args, calls, _ = setup
    def fail(*a, **kw): calls.append('native'); raise failure
    monkeypatch.setattr(native, 'query_native_scan', fail)
    def forbidden(*a, **kw): pytest.fail('terminal failure advanced source fallback')
    monkeypatch.setattr(server._semantic_scan_runtime, 'scan', forbidden)
    r = await server._dispatch('scan_structural_risks', {**args,'analysis_mode':'deep'})
    assert r.semantic.termination_reason == reason and not r.semantic.facts
    assert server._structural_native_runtime._backend is None


@pytest.mark.anyio
async def test_ordinary_load_failure_can_use_source_with_remaining_budget(setup, monkeypatch):
    args, calls, _ = setup
    def fail(*a, **kw): raise NpiHierarchyUnavailable('npi_load_failed')
    monkeypatch.setattr(native, 'query_native_scan', fail)
    r = await server._dispatch('scan_structural_risks', args)
    assert r.semantic.backend_attempts[0]['reason'] == 'npi_load_failed'
    assert r.semantic.backend_attempts[1]['backend'] == 'slang'


@pytest.mark.anyio
async def test_late_native_call_uses_total_deadline_and_cancellation_does_not_fallback(setup, monkeypatch):
    args, _, _ = setup
    def late(*a, **kw): time.sleep(.02); return {'status':'complete','gaps':[]}
    monkeypatch.setattr(native, 'query_native_scan', late)
    r = await server._dispatch('scan_structural_risks', {**args,'semantic_timeout_sec':.01})
    assert r.semantic.termination_reason == 'semantic_timeout' and not r.semantic.facts
    def cancel(*a, **kw): raise asyncio.CancelledError()
    monkeypatch.setattr(native, 'query_native_scan', cancel)
    with pytest.raises(asyncio.CancelledError): await server._dispatch('scan_structural_risks', args)


@pytest.mark.anyio
async def test_lsf_mode_never_executes_parent_licensed_core(setup, monkeypatch):
    from src import npi_structural_lsf
    args, calls, _ = setup
    monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION', 'lsf')
    monkeypatch.setenv('TRACEWEAVE_NPI_LSF_QUEUE', 'controlled')
    def parent(*a, **kw): pytest.fail('licensed NPI in parent')
    monkeypatch.setattr(native, 'query_native_scan', parent)
    def remote(*a, **kw):
        calls.append('remote')
        return dict(status='complete', categories_checked=['constant_connection','open_input'],
                    category_results={c:{'status':'complete','gaps':[]} for c in ['constant_connection','open_input']},
                    facts=[], gaps=[], native_execution={'execution_mode':'lsf','scheduler_status':'completed','worker_status':'completed'})
    monkeypatch.setattr(npi_structural_lsf, 'query_scan_over_lsf', remote)
    r = await server._dispatch('scan_structural_risks', {**args,'semantic_categories':['constant_connection','open_input']})
    assert calls.count('remote') == 1 and r.semantic.native_execution['execution_mode'] == 'lsf'


@pytest.mark.anyio
async def test_warm_source_superset_still_attempts_native_then_supplements(setup, monkeypatch):
    from src import structural_semantic_runtime
    args, calls, _ = setup
    good = native.query_native_scan
    def unavailable(*a, **kw): raise NpiHierarchyUnavailable('npi_load_failed')
    monkeypatch.setattr(native, 'query_native_scan', unavailable)
    builds = []
    async def build(payload, python):
        builds.append(payload)
        cats = payload['categories']
        return dict(status='complete', categories_checked=cats, facts=[{'kind':c} for c in cats],
                    total_facts=len(cats), counts={c:1 for c in cats}, gaps=[], metrics={'frontend_build_count':1})
    monkeypatch.setattr(structural_semantic_runtime, 'run_isolated_scan', build)
    first = await server._dispatch('scan_structural_risks', {**args,'analysis_mode':'deep'})
    assert first.semantic.status == 'complete' and len(builds) == 1
    monkeypatch.setattr(native, 'query_native_scan', good)
    second = await server._dispatch('scan_structural_risks', {**args,'semantic_timeout_sec':14})
    assert calls.count('native') == 1 and len(builds) == 1
    assert second.semantic.status == 'complete' and second.semantic.backend == 'mixed'
    assert second.semantic.metrics['frontend_build_count'] == 0
    assert second.semantic.cache_disposition == 'hit_memory'


@pytest.mark.anyio
async def test_policy_change_during_source_never_publishes_new_cache(setup, monkeypatch):
    from src import structural_semantic_runtime
    args, _, _ = setup
    async def build(*a):
        monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION','lsf')
        return dict(status='complete', categories_checked=['constant_comparison'], facts=[], gaps=[])
    monkeypatch.setattr(structural_semantic_runtime, 'run_isolated_scan', build)
    r = await server._dispatch('scan_structural_risks', {**args,'analysis_mode':'deep'})
    assert r.semantic.termination_reason == 'npi_execution_policy_changed' and not r.semantic.facts
    assert not server._semantic_scan_runtime._cache


@pytest.mark.anyio
async def test_source_only_category_skips_native_discovery(setup):
    args, calls, _ = setup
    r = await server._dispatch('scan_structural_risks', {**args,'semantic_categories':['constant_comparison']})
    assert not calls and r.semantic.status == 'not_run'
    assert r.semantic.backend_attempts[0]['reason'] == 'npi_category_unsupported'


@pytest.mark.anyio
async def test_failed_reused_backend_is_not_permanently_sticky(setup, monkeypatch):
    args, _, _ = setup
    await server._dispatch('scan_structural_risks', args)
    assert server._structural_native_runtime._backend is not None
    def fail(*a, **kw): raise NpiHierarchyUnavailable('npi_load_failed')
    monkeypatch.setattr(native, 'query_native_scan', fail)
    r = await server._dispatch('scan_structural_risks', args)
    assert server._structural_native_runtime._backend is None
    assert r.semantic.result_groups[0].checked_scope is None
