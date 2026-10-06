"""Default wire migration, same-dispatch receipts and legacy consumers."""
import json
from types import SimpleNamespace

import pytest

import server
from src import schemas
from src.evidence_output import expand_compact_result, serialize_compact_result
from tests.driver_compact_fixtures import canonical, driver_fixture, public


@pytest.fixture
def anyio_backend():
    return 'asyncio'


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    from src.hierarchy_handles import HandleStore
    from src.hierarchy_recovery import HierarchyRecoveryRuntime
    server.reset_session_state()
    monkeypatch.setenv('TRACEWEAVE_CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(server, '_handle_store', HandleStore())
    monkeypatch.setattr(server, '_hierarchy_recovery_runtime', HierarchyRecoveryRuntime())
    yield
    server.reset_session_state()


@pytest.mark.anyio
@pytest.mark.parametrize('backend', ['source_graph', 'verdi_npi', 'static'])
async def test_default_explicit_compact_and_full_same_model(monkeypatch, backend):
    if backend == 'source_graph':
        model, _ = driver_fixture('bad')
    else:
        model = schemas.ExplainDriverResult(signal_path='t.q', wave_path='w', resolved_rtl_name='q',
            driver_status='unsupported' if backend == 'static' else 'testbench_driven', backend=backend,
            cross_check=schemas.DriverLoadCrossCheck(conflict=True, performed=True) if backend == 'verdi_npi' else None)
    calls = []
    async def dispatch(name, args):
        assert 'output_format' not in args
        calls.append(name)
        return model
    monkeypatch.setattr(server, '_dispatch', dispatch)
    for args in ({}, {'output_format': 'compact'}):
        text = (await server.call_tool('explain_signal_driver', args))[0].text
        wire = json.loads(text)
        assert wire.get('format') == 'traceweave.driver.compact.v1'
        assert canonical(expand_compact_result(wire)) == canonical(public(model))
        if backend != 'source_graph':
            assert not any(wire['evidence_tables'].values())
    full = (await server.call_tool('explain_signal_driver', {'output_format': 'full'}))[0].text
    assert full == server._serialize_result(model)
    assert len(calls) == 3


@pytest.mark.anyio
async def test_catalog_and_runtime_default_share_per_tool_options():
    catalog = {t.name: t for t in await server.list_tools()}
    assert catalog['explain_signal_driver'].inputSchema['properties']['output_format']['default'] == 'compact'
    for name in schemas.COMPACT_OUTPUT_TOOLS:
        options = schemas.output_options_for_tool(name)
        assert catalog[name].inputSchema['properties']['output_format'] == options.model_json_schema()['properties']['output_format']
        assert options().output_format == ('compact' if name == 'explain_signal_driver' else 'full')
    desc = catalog['explain_signal_driver'].description
    assert 'traceweave.driver.compact.v1' in desc and 'statement_evidence_ref' in desc and 'full' in desc
    assert catalog['explain_signal_driver'].inputSchema['examples']


@pytest.mark.anyio
async def test_only_successful_driver_model_gets_envelope(monkeypatch):
    values = [schemas.ToolErrorResult(error='unknown signal'),
        schemas.PrerequisiteBlockResult(missing_step='build_tb_hierarchy', required_before='explain_signal_driver', reason='missing'),
        schemas.FstCapabilityErrorResult(error='unsupported', supported_tools=[], suggested_call={}),
        {'error': 'early future error shape', 'detail': None}]
    for model in values:
        async def dispatch(*_): return model
        monkeypatch.setattr(server, '_dispatch', dispatch)
        for args in ({}, {'output_format': 'compact'}, {'output_format': 'full'}):
            assert (await server.call_tool('explain_signal_driver', args))[0].text == server._serialize_result(model)
    bad = json.loads((await server.call_tool('explain_signal_driver', {'output_format': 'summary'}))[0].text)
    assert 'error' in bad and 'format' not in bad


@pytest.mark.anyio
async def test_real_dispatch_recovery_dependency_binding_receipts_and_next_action(tmp_path, monkeypatch):
    from src.divergence_context import driver_action
    from src.scope_metadata import file_identity
    from src.wave_design_binding import RootBinding
    from tests.test_hierarchy_recovery import design
    _, context = design(tmp_path)
    wave = tmp_path / 'synthetic.vcd'; wave.write_text('synthetic binding identity')
    binding = RootBinding('TOP.top', 'top', file_identity(str(wave)))
    async def bind(*_): return binding
    calls = []
    model, _ = driver_fixture('bad')
    async def route(*, args, **_):
        assert args['_bound_context'].current()
        assert args['signal_path'] == 'top.q'
        calls.append('backend')
        result = public(model)
        result.update(signal_path='top.q', wave_path=str(wave))
        return result, dict(actual_backend='source_graph', single_backend_provenance=True)
    monkeypatch.setattr(server, 'apply_npi_source_overlay', lambda *_: None)
    monkeypatch.setattr(server, '_bind_wave_design_root', bind)
    monkeypatch.setattr(server, '_route_public_connectivity', route)
    action = driver_action(result=dict(wave_path_a=str(wave), signal_a='TOP.top.q', first_divergence_time_ps=10),
        side='a', context=context)
    assert 'output_format' not in action['arguments']
    action['arguments']['include_dependencies'] = True
    original = server._dispatch
    captured = []
    async def capture(name, args):
        result = await original(name, args)
        if name == 'explain_signal_driver': captured.append(result)
        return result
    monkeypatch.setattr(server, '_dispatch', capture)
    text = (await server.call_tool(action['tool'], action['arguments']))[0].text
    wire = json.loads(text)
    assert wire.get('format') == 'traceweave.driver.compact.v1', wire
    assert len(captured) == len(calls) == 1
    expanded = expand_compact_result(text)
    assert canonical(expanded) == canonical(public(captured[0]))
    assert expanded['hierarchy_recovery']['status'] == 'rebuilt'
    assert expanded['backend_status']['actual_backend'] == 'source_graph'
    assert expanded['dependency_context']['gaps']
    assert expanded['wave_design_binding']['source_signal'] == 'top.q'
    assert len(expanded['driver_chain']) == 8


def test_soak_script_decodes_new_driver_response():
    from scripts.soak_source_graph_soc import _parse_call_tool_result
    model, _ = driver_fixture('bad')
    text = serialize_compact_result('explain_signal_driver', model)
    assert canonical(_parse_call_tool_result([SimpleNamespace(type='text', text=text)])) == canonical(public(model))
