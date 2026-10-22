"""Controlled codec/transport checks; no scheduler or license claim."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import config
from src import npi_lsf
from src.design_context_runtime import ContextUnavailable
from src.hierarchy_source_ir import HierarchyQueryBudget
from src.hierarchy_source_request import prepare_native_source_query
from src.kdb_anchor import capture_kdb_anchor
from src.native_query import read_native_inputs
from src.native_query_codec import encode_native_inputs
from src.npi_source_worker import validate_source_worker_result
from tests.native_first_helpers import project
from tests.test_native_source_services import isolate, use_sdk
from tests.test_npi_hierarchy_source import FakeSdk


def request(tmp_path, *, pure=False):
    source, log, kdb = project(tmp_path, top="test_top")
    inputs = read_native_inputs({"kdb_path": str(kdb), "top_hint": "test_top",
                                **({} if pure else {"compile_log": str(log)})})
    anchor = capture_kdb_anchor(str(kdb))
    budget = HierarchyQueryBudget()
    limits = {k: getattr(budget, k) for k in ("max_scopes", "max_calls", "max_bytes", "max_depth", "max_symbols", "max_files")}
    value = npi_lsf.NativeSourceWorkerRequest(kdb_path=str(kdb), top="test_top",
        kdb_identity_sha256=anchor.identity_sha256, tool="find_tb_instance",
        arguments={"path": "test_top.g[0].u"}, metadata_payload=encode_native_inputs(inputs),
        limits=npi_lsf.SourceWorkerLimits(timeout_sec=30.0, **limits))
    return value, inputs, anchor, source


@pytest.mark.parametrize("pure", [False, True])
def test_native_worker_metadata_only_payload_and_real_owner_oracle(tmp_path, monkeypatch, pure):
    value, inputs, anchor, source = request(tmp_path, pure=pure)
    sdk = FakeSdk()
    use_sdk(monkeypatch, sdk)
    source.unlink()
    wire = value.model_dump_json().encode()
    assert b'context_payload' not in wire and b'include_resolutions' not in wire
    assert b'source_snapshot' not in wire and b'module leaf(' not in wire
    decoded = npi_lsf.parse_worker_request_bytes(wire)
    result = npi_lsf.execute_worker_request(decoded)
    assert result.status == "ok", result
    assert result.protocol_version == "5.0"
    assert result.result["hits"][0]["parent"] == "test_top.g[0]"
    assert "context_identity_sha256" not in result.result
    validate_source_worker_result(decoded, result.result)
    assert sdk.global_count == 1


def test_version_four_accepts_old_operations_and_rejects_native(tmp_path):
    value, _, _, _ = request(tmp_path)
    raw = value.model_dump()
    raw["protocol_version"] = "4.0"
    with pytest.raises(ValueError):
        npi_lsf.parse_worker_request_bytes(json.dumps(raw).encode())
    old = npi_lsf.parse_worker_request_bytes(json.dumps(dict(protocol_version="4.0",
        operation="find_loads", kdb_path="/kdb", top="top", signal_path="top.y")).encode())
    backend = SimpleNamespace(_ensure_loaded=lambda *a: False)
    response = npi_lsf.execute_worker_request(old, backend=backend)
    assert response.protocol_version == "4.0" and response.status == "npi_unavailable"


@pytest.mark.parametrize("field", ["artifact_identity", "context_identity_sha256", "ownership_identity", "native_route", "hits", "work"])
def test_parent_rejects_foreign_facts_or_parent_state(tmp_path, monkeypatch, field):
    value, _, _, _ = request(tmp_path)
    use_sdk(monkeypatch, FakeSdk())
    response = npi_lsf.execute_worker_request(value)
    assert response.status == "ok", response
    raw = deepcopy(response.result)
    if field == "work":
        raw["query_metrics"]["native_call_count"] = value.limits.max_calls + 1
    elif field == "hits":
        raw["hits"][0]["path"] = "test_top.wrong"
    else:
        raw[field] = "f" * 64 if field != "native_route" else {}
    with pytest.raises(ValueError):
        validate_source_worker_result(value, raw)


def test_lsf_parent_transfers_metadata_only_and_counts_remote_work(tmp_path, monkeypatch):
    value, inputs, anchor, _ = request(tmp_path)
    use_sdk(monkeypatch, FakeSdk())
    monkeypatch.setenv("TRACEWEAVE_NPI_EXECUTION", "lsf")
    monkeypatch.setenv("TRACEWEAVE_NPI_LSF_QUEUE", "controlled")
    received = []
    class Transport:
        def execute(self, request, *, deadline=None):
            received.append(request)
            assert deadline is not None
            response = npi_lsf.execute_worker_request(request)
            assert response.status == "ok", response
            return npi_lsf.LsfExecutionResult(response.result, "completed", "completed",
                                             kdb_load_quality=response.kdb_load_quality)
    backend = npi_lsf.LsfConnectivityBackend(config.get_npi_execution_config(), transport=Transport())
    backend.bind_native_inputs(inputs)
    backend.bind_kdb_anchor(anchor)
    plan = prepare_native_source_query(inputs, value.tool, value.arguments)
    raw = backend.query_source(plan)
    assert isinstance(received[0], npi_lsf.NativeSourceWorkerRequest)
    assert raw["_npi_execution_status"]["execution_mode"] == "lsf"
    assert raw["query_metrics"]["context_transport_bytes"] < 8 * 1024 * 1024
    assert plan.budget.call_count > 0 and raw["hits"][0]["path"] == "test_top.g[0].u"


def test_worker_metadata_mutation_is_terminal(tmp_path, monkeypatch):
    value, inputs, _, _ = request(tmp_path)
    use_sdk(monkeypatch, FakeSdk())
    from pathlib import Path
    Path(inputs.ordered_compile_logs[0]).write_text("another compile")
    response = npi_lsf.execute_worker_request(value)
    assert response.status == "error" and response.error_code == "context_identity_changed"
