import asyncio
import json
import os
from pathlib import Path
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import server
from src.property_model import ExplainPropertyResult

UNIT={'id':'u','declarations':'bit clk,a,b;','default_clock':'posedge clk'}


@pytest.fixture(autouse=True)
def reset():
    server.reset_session_state()
    yield
    server.reset_session_state()


@pytest.mark.anyio
async def test_registered_schema_and_real_stdio_roundtrip(tmp_path):
    pytest.importorskip('pyslang')
    params=StdioServerParameters(command=sys.executable,args=[str(Path(server.__file__).resolve())],cwd=str(tmp_path),
        env={**os.environ,'TRACEWEAVE_TELEMETRY':'0','TRACEWEAVE_CACHE_DIR':str(tmp_path/'cache')})
    with anyio.fail_after(30):
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as client:
                await client.initialize()
                catalog=await client.list_tools()
                tool=next(t for t in catalog.tools if t.name=='explain_property')
                assert tool.inputSchema['additionalProperties'] is False
                assert tool.inputSchema['properties']['items']['anyOf'][0]['maxItems']==16
                response=await client.call_tool('explain_property',{'items':[
                    {'id':'one','text':'a |=> b','unit':'u'},
                    {'id':'two','text':'cover property(a |-> b);','unit':'u'}],'units':[UNIT]})
                assert not response.isError
                result=ExplainPropertyResult.model_validate_json(response.content[0].text)
                assert result.status=='complete'
                assert [r.id for r in result.results]==['one','two']
                assert result.results[1].directive['kind']=='CoverProperty'


@pytest.mark.anyio
async def test_public_discoveries_coexist_and_associate_exact_runs(tmp_path):
    pytest.importorskip('pyslang')
    sim=tmp_path/'sim.log';sim.write_text('Chronologic VCS simulator\n')
    formal=tmp_path/'jg_console.log';formal.write_text('JasperGold\n')
    await server._dispatch('get_sim_paths',{'verif_root':str(tmp_path),'sim_log':str(sim)})
    await server._dispatch('get_formal_paths',{'formal_root':str(tmp_path),'formal_log':str(formal)})
    for path,mode in [(None,'unknown'),(str(sim),'simulation'),(str(formal),'formal')]:
        args={'text':'a |=> b','unit':'u','units':[UNIT]}
        if path:args['run_anchor']=path
        r=await server._dispatch('explain_property',args)
        assert r.results[0].mode['value']==mode
    assert server._session_state['get_sim_paths'] is not None
    assert server._formal_result_cache is not None


def compile_fixture(tmp_path):
    src=tmp_path/'top.sv';src.write_text('module top; bit clk,a,b; ap: assert property(@(posedge clk) a |=> b); endmodule\n')
    log=tmp_path/'compile.log';log.write_text(f'Command: vcs -sverilog {src} -top top\nParsing design file "{src}"\nTop Level Modules:\n top\n')
    return src,log


@pytest.mark.anyio
async def test_compile_and_prepared_identity_are_checked_without_native_or_wave_queries(tmp_path,monkeypatch):
    pytest.importorskip('pyslang')
    src,log=compile_fixture(tmp_path)
    def forbidden(*args,**kwargs):raise AssertionError('no wave/native tool allowed')
    monkeypatch.setattr(server,'_get_parser',forbidden)
    monkeypatch.setattr(server,'_probe_connectivity_backend',forbidden,raising=False)
    r=await server._dispatch('explain_property',{'compile_log':str(log),'source_file':str(src),'target':'top.ap','simulator':'vcs'})
    assert r.results[0].semantic_status=='complete',r.results[0].gaps
    prep=await server._dispatch('prepare_design_context',{'compile_log':str(log),'simulator':'vcs','top_hint':'top'})
    handle=prep.design_handle
    r=await server._dispatch('explain_property',{'design_handle':handle,'target':'top.ap'})
    assert r.results[0].semantic_status=='complete',r.results[0].gaps
    assert r.results[0].identity['origin']=='prepared_design'
    src.write_text(src.read_text()+'// changed\n')
    r=await server._dispatch('explain_property',{'design_handle':handle,'target':'top.ap'})
    assert r.results[0].gaps[0].code=='design_context_changed'
    assert not r.results[0].nodes


@pytest.mark.anyio
async def test_expired_handle_blocks_source_but_preserves_independent_text():
    pytest.importorskip('pyslang')
    r=await server._dispatch('explain_property',{'design_handle':'expired','items':[
        {'id':'source','target':'top.ap'},{'id':'text','text':'a |=> b','unit':'u'}],'units':[UNIT]})
    assert r.results[0].gaps[0].code=='design_context_expired'
    assert r.results[1].semantic_status=='complete'


@pytest.mark.anyio
async def test_heavy_property_worker_leaves_event_loop_responsive(monkeypatch):
    import src.property_runtime as runtime
    from src.property_model import PropertyExplanation
    started=asyncio.Event();release=asyncio.Event()
    async def slow(payload,*args):
        started.set();await release.wait()
        return PropertyExplanation(id=payload['item']['id'],semantic_status='complete').model_dump()
    monkeypatch.setattr(runtime,'run_worker',slow)
    heavy=asyncio.create_task(server._dispatch('explain_property',{'text':'1'}))
    await started.wait()
    catalog=await asyncio.wait_for(server.list_tools(),.5)
    assert any(t.name=='explain_property' for t in catalog)
    release.set();await heavy
