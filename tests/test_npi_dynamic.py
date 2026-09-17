"""Opt-in real KDB tests plus transport and native-state regressions."""
from dataclasses import asdict
import json
import os
from pathlib import Path
import shutil
import subprocess
from types import SimpleNamespace

import pytest

from src.dynamic_evidence import Expr, evaluate, validate_step
from src.npi_dynamic import query_step
from src.npi_lsf import (DynamicStepWorkerRequest, LsfConnectivityBackend, LsfExecutionResult,
                         execute_worker_request, parse_worker_request_bytes, _validate_operation_result)
from src.verdi_npi_backend import VerdiNpiBackend
from tests.test_npi_lsf import _lsf_config


def test_dynamic_lsf_request_and_result_validation():
    request=DynamicStepWorkerRequest(kdb_path="/shared/kdb",top="top",signal_path="top.q")
    assert isinstance(parse_worker_request_bytes(request.model_dump_json().encode()), DynamicStepWorkerRequest)
    malformed={"version":"1.0","backend":"verdi_npi","boundary":"combinational",
               "branches":[{"guard":{"op":"__import__","width":1},"value":{}}]}
    assert _validate_operation_result(request,malformed) is None


def test_dynamic_lsf_receipt_and_parent_only_fallback(tmp_path,monkeypatch):
    received=[]
    result={"version":"1.0","backend":"verdi_npi","signal":"top.q","width":1,
            "boundary":"input","branches":[],"clock":None,"complete":True,"gaps":[]}
    def execute(request):
        received.append(request)
        return LsfExecutionResult(result,"completed","completed",kdb_load_quality="clean")
    backend=LsfConnectivityBackend(_lsf_config(tmp_path),transport=SimpleNamespace(execute=execute))
    monkeypatch.setattr(backend,"_resolve_target",lambda *a:(("/shared/kdb","top"),"ready"))
    r=backend.get_dynamic_step("top.q","/shared/compile.log")
    assert isinstance(received[0],DynamicStepWorkerRequest)
    assert r["_npi_execution_status"]["execution_mode"]=="lsf"
    assert r["_npi_execution_status"]["scheduler_status"]=="completed"


@pytest.fixture(scope="module")
def real_kdbs(tmp_path_factory):
    if os.environ.get("TRACEWEAVE_TEST_NPI_DYNAMIC") != "1":
        pytest.skip("set TRACEWEAVE_TEST_NPI_DYNAMIC=1 for real VCS/NPI evidence")
    assert shutil.which("vcs"), "VCS is required for the explicitly requested live regression"
    root=tmp_path_factory.mktemp("npi_dynamic")
    fixture=Path(__file__).parent / "fixtures/divergence/drivers.sv"
    outputs=[]
    for index in (0,1):
        case=root/str(index);case.mkdir()
        src=case/"drivers.sv"
        src.write_text(fixture.read_text().replace("8'h00", "8'h11" if index else "8'h00"))
        command=["vcs","-full64","-sverilog","-timescale=1ns/1ps","-debug_access+all","-kdb",
                 str(src),"-top","tw_div_probe","-o","simv","-l","compile.log"]
        run=subprocess.run(command,cwd=case,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,timeout=90)
        assert run.returncode == 0, run.stdout[-3000:]
        outputs.append(case/"simv.daidir/kdb.elab++")
    return outputs


def load(path):
    backend=VerdiNpiBackend()
    assert backend._ensure_loaded(str(path),"tw_div_probe")
    assert backend.kdb_load_quality == "clean"
    return backend


def value(step, values):
    return evaluate(Expr.from_dict(step["branches"][0]["value"]),lambda e:{"value":values[e.signal]})


def test_real_npi_mux_enable_reset_edges_and_synthetic_conditions(real_kdbs):
    b=load(real_kdbs[0])
    for name in ("mux_out","q","qn","qnested","a"):
        r=query_step(b,"tw_div_probe."+name)
        validate_step(json.loads(json.dumps(r)))
        assert r["complete"], r
        if name in {"q","qn","qnested"}:
            assert r["boundary"] == "sequential"
            assert r["clock"]["edge"] == ("negedge" if name=="qn" else "posedge")
    q=query_step(b,"tw_div_probe.q")
    reset=value(q,{"tw_div_probe.rst":"1"})
    assert reset.value == "00000000" and len(reset.dependencies)==1
    hold=value(q,{"tw_div_probe.rst":"0","tw_div_probe.en":"0","tw_div_probe.q":"11001100"})
    assert hold.value=="11001100"
    assert not any("GEN" in dep["signal"] for dep in hold.dependencies)
    nested=query_step(b,"tw_div_probe.qnested")
    selected=value(nested,{"tw_div_probe.en":"1","tw_div_probe.sel":"0","tw_div_probe.b":"00110011"})
    assert selected.value=="00110011"
    packed=query_step(b,"tw_div_probe.packed_out")
    assert packed["complete"], packed
    assert value(packed,{"tw_div_probe.a":"0011"}).value == "00111010"
    eq=query_step(b,"tw_div_probe.eq_out")
    assert eq["complete"], eq
    assert value(eq,{"tw_div_probe.a":"00000011","tw_div_probe.b":"11001100"}).value == "11001100"
    async_step=query_step(b,"tw_div_probe.qasync")
    assert not async_step["complete"] and "temporal_context_unavailable" in async_step["gaps"]


def test_real_npi_lsf_worker_uses_same_dynamic_core(real_kdbs):
    request=DynamicStepWorkerRequest(kdb_path=str(real_kdbs[0]),top="tw_div_probe",signal_path="tw_div_probe.q")
    response=execute_worker_request(request)
    assert response.status == "ok" and response.result["complete"]
    assert response.result["clock"]["edge"]=="posedge"


def test_real_alternating_backend_instances_reload_correct_kdb(real_kdbs):
    a=load(real_kdbs[0]); b=load(real_kdbs[1])
    for backend,path,expected in ((a,real_kdbs[0],"00000000"),(b,real_kdbs[1],"00010001"),(a,real_kdbs[0],"00000000")):
        assert backend._ensure_loaded(str(path),"tw_div_probe")
        assert value(query_step(backend,"tw_div_probe.q"),{"tw_div_probe.rst":"1"}).value==expected
