"""Protocol controls only: no scheduler, license or native SDK is invoked."""
from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace as NS

import pytest

import config
from src import npi_lsf
from src import npi_structural_lsf as scan
from src.native_query import read_native_inputs
from src.native_query_codec import encode_native_inputs
from src.kdb_anchor import capture_kdb_anchor
from src.npi_structural_scan import scan_budget, scan_native_ports
from src.structural_semantic_runtime import validate_semantic_options
from tests.native_first_helpers import project
from tests.test_npi_structural_scan import Provider, port, const


def request(tmp_path):
    source, log, kdb = project(tmp_path, top='top')
    inputs = read_native_inputs(dict(compile_log=str(log), top_hint='top'))
    anchor = capture_kdb_anchor(str(kdb))
    options = validate_semantic_options(dict(semantic_categories=['constant_connection', 'open_input']))[1]
    req = npi_lsf.StructuralScanWorkerRequest(kdb_path=str(kdb), top='top',
        kdb_identity_sha256=anchor.identity_sha256, metadata_payload=encode_native_inputs(inputs), options=options)
    return req, inputs, anchor, source


def worker(req, monkeypatch, ports=None):
    backend = NS(kdb_load_quality='clean', bind_native_inputs=lambda i: None, bind_kdb_anchor=lambda a: None)
    def query(backend, options, *, top, budget, state=None):
        p = Provider(ports or [port('a', const('1010'))], options=options); p.budget = budget
        p.artifact_identity = hashlib.sha256((req.kdb_identity_sha256 + '\0' + req.top).encode()).hexdigest()
        result = scan_native_ports(p, options, state=state)
        result.pop('query_metrics')
        result['metrics'].update(native_call_count=budget.call_count, native_instance_count=budget.scope_count,
                                 native_handle_count=1, estimated_fact_bytes=budget.fact_bytes)
        result['kdb_binding'] = dict(capture_kdb_anchor(req.kdb_path).receipt(), load_status='loaded', load_quality='clean')
        return result
    monkeypatch.setattr(scan, 'query_native_scan', query)
    return npi_lsf.execute_worker_request(req, backend=backend)


def test_new_operation_validates_and_preserves_old_protocol(tmp_path, monkeypatch):
    req, _, _, _ = request(tmp_path)
    raw = req.model_dump()
    assert npi_lsf.parse_worker_request_bytes(req.model_dump_json().encode()).model_dump_json() == req.model_dump_json()
    raw['protocol_version'] = '4.0'
    with pytest.raises(ValueError): npi_lsf.parse_worker_request_bytes(json.dumps(raw).encode())
    r = worker(req, monkeypatch)
    assert r.status == 'ok' and r.protocol_version == '5.0'
    assert scan.validate_scan_worker_result(req, r.result)['total_facts'] == 1


def test_worker_driver_categories_and_negative_proof_validation(tmp_path, monkeypatch):
    from tests.test_npi_structural_drivers import provider, overlap_reader, CATEGORIES
    from src.npi_structural_drivers import writer_identity
    req,_,_,_=request(tmp_path)
    options=validate_semantic_options(dict(semantic_scope='top.u',semantic_categories=CATEGORIES))[1]
    req=req.model_copy(update={'options':options})
    assert npi_lsf.parse_worker_request_bytes(req.model_dump_json().encode()).options==options
    def query(backend, options, *, top, budget, state=None):
        p=provider();p.budget=budget
        p.artifact_identity=hashlib.sha256((req.kdb_identity_sha256+'\0'+req.top).encode()).hexdigest()
        reader=overlap_reader()
        for index,row in enumerate(reader.values['top.u.S[9]'][0]):
            name=f'top.u.child{index}.out'
            row.update(driver_id=hashlib.sha256(writer_identity(name).encode()).hexdigest(),
                       signal_path=name,kind='instance_port')
        result=scan_native_ports(p,options,state=state,driver_reader=reader)
        result.pop('query_metrics')
        result['metrics'].update(native_call_count=budget.call_count,native_instance_count=budget.scope_count,
                                 native_handle_count=1,estimated_fact_bytes=budget.fact_bytes)
        result['kdb_binding']=dict(capture_kdb_anchor(req.kdb_path).receipt(),load_status='loaded',load_quality='clean')
        return result
    monkeypatch.setattr(scan,'query_native_scan',query)
    backend=NS(kdb_load_quality='clean',bind_native_inputs=lambda _:None,bind_kdb_anchor=lambda _:None)
    response=npi_lsf.execute_worker_request(req,backend=backend)
    assert response.status=='ok'
    assert scan.validate_scan_worker_result(req,response.result)['total_facts']==2
    raw=deepcopy(response.result)
    next(f for f in raw['facts'] if f['kind']=='undriven_signal')['driver_inventory_complete']=False
    assert npi_lsf._validate_operation_result(req,raw) is None
    raw=deepcopy(response.result)
    next(f for f in raw['facts'] if f['kind']=='multiple_driver')['drivers'][0]['driver_id']='forged'
    assert npi_lsf._validate_operation_result(req,raw) is None


@pytest.mark.parametrize('field', ['artifact_identity', 'selected_top', 'scope', 'request_fingerprint',
                                   'category_results', 'facts', 'counts', 'work', 'backend_status'])
def test_forged_result_rejected(tmp_path, monkeypatch, field):
    req, _, _, _ = request(tmp_path)
    response = worker(req, monkeypatch)
    raw = deepcopy(response.result)
    if field == 'facts': raw['facts'][0]['instance'] = 'foreign.u'
    elif field == 'work': raw['metrics']['native_call_count'] = 100001
    elif field == 'category_results': raw['category_results'] = {}
    elif field == 'counts': raw['counts'] = {}
    elif field == 'scope': raw[field] = 'top.foreign'
    else: raw[field] = 'foreign'
    assert npi_lsf._validate_operation_result(req, raw) is None


def test_wrong_options_and_extra_budget_fields_rejected(tmp_path):
    req, _, _, _ = request(tmp_path)
    for extra in ({'scope': '*'}, {'categories': []}, {'arbitrary_budget': 1}, {'timeout_sec': float('nan')}):
        raw = req.model_dump(); raw['options'].update(extra)
        with pytest.raises(ValueError): npi_lsf.parse_worker_request_bytes(json.dumps(raw).encode())


def test_parent_uses_existing_transport_and_never_local_backend(tmp_path, monkeypatch):
    req, inputs, anchor, _ = request(tmp_path)
    response = worker(req, monkeypatch)
    monkeypatch.setenv('TRACEWEAVE_NPI_EXECUTION', 'lsf')
    monkeypatch.setenv('TRACEWEAVE_NPI_LSF_QUEUE', 'controlled')
    received = []
    class Transport:
        def execute(self, value, *, deadline=None):
            assert deadline is not None
            received.append(value)
            result = deepcopy(response.result)
            result['request_fingerprint'] = scan._fingerprint(value)
            return npi_lsf.LsfExecutionResult(result, 'completed', 'completed', kdb_load_quality='clean')
    backend = npi_lsf.LsfConnectivityBackend(config.get_npi_execution_config(), transport=Transport())
    backend.bind_native_inputs(inputs); backend.bind_kdb_anchor(anchor)
    result = scan.query_scan_over_lsf(backend, inputs, req.options, scan_budget(req.options))
    assert result['native_execution'] == {'execution_mode': 'lsf', 'scheduler_status': 'completed', 'worker_status': 'completed'}
    assert received[0].options['timeout_sec'] < req.options['timeout_sec']


def test_worker_metadata_change_is_terminal(tmp_path, monkeypatch):
    req, _, _, source = request(tmp_path)
    # Sources are outside the metadata observation; changed log is fatal.
    source.unlink()
    inputs = scan.decode_native_inputs(req.metadata_payload)
    from pathlib import Path
    Path(inputs.ordered_compile_logs[0]).write_text('changed')
    response = npi_lsf.execute_worker_request(req)
    assert response.status == 'error' and response.error_code == 'context_identity_changed'


def test_bounded_handle_stop_preserves_exact_attempt_count(tmp_path, monkeypatch):
    req, _, _, _ = request(tmp_path)
    response = worker(req, monkeypatch); raw = deepcopy(response.result)
    raw['metrics']['native_handle_count'] = 16385
    assert npi_lsf._validate_operation_result(req, raw) is None
    raw['status'] = 'partial'; raw['gaps'] = ['native_handle_limit']
    for row in raw['category_results'].values():
        row.update(status='partial', gaps=['native_handle_limit'])
    assert scan.validate_scan_worker_result(req, raw)['metrics']['native_handle_count'] == 16385


def test_worker_rejects_unbounded_or_unrelated_diagnostics(tmp_path, monkeypatch):
    req, _, _, _ = request(tmp_path)
    raw = deepcopy(worker(req, monkeypatch).result)
    row = dict(reason='npi_expression_unmodeled', occurrences=1, affected_ports=1,
               examples=[dict(instance='top.u', port='a')], examples_truncated=False)
    raw['gap_summary'] = [row]
    with pytest.raises(ValueError): scan.validate_scan_worker_result(req, raw)
    raw['gaps'] = [row['reason']]; raw['status'] = 'partial'
    row['examples'] *= 3
    with pytest.raises(ValueError): scan.validate_scan_worker_result(req, raw)


def test_worker_continuation_validates_page_delta_and_frontier(tmp_path, monkeypatch):
    req, _, _, _ = request(tmp_path)
    payload = req.model_dump()
    payload['options']['scope'] = 'top.u'
    payload['options']['limits']['max_facts'] = 1
    req = npi_lsf.parse_worker_request_bytes(json.dumps(payload).encode())
    ports = [port('a', const('1010')), port('b', const('0101'))]
    first = worker(req, monkeypatch, ports)
    assert first.status == 'ok' and first.result['scan_progress']['status'] == 'available'
    payload = req.model_dump(); payload['scan_state'] = first.result['_scan_state']
    req2 = npi_lsf.parse_worker_request_bytes(json.dumps(payload).encode())
    final = worker(req2, monkeypatch, ports)
    assert final.status == 'ok' and final.result['total_facts'] == 2 and len(final.result['facts']) == 1
    raw = deepcopy(final.result); raw['facts'][0]['kind'] = 'open_input'
    with pytest.raises(ValueError): scan.validate_scan_worker_result(req2, raw)
    payload['scan_state']['pending'][0]['path'] = 'foreign'
    with pytest.raises(ValueError): npi_lsf.parse_worker_request_bytes(json.dumps(payload).encode())
