"""Controlled loader callbacks through the actual tracked evidence wrapper.

The catalog payload is hand authored; these controls do not launch a frontend.
"""
from dataclasses import replace
import hashlib
import json
import traceback

import pytest

from src.cancellation import OperationCancelled
from src.source_graph_contract import compute_source_graph_build_key
from src.source_graph_runtime import SourceGraphRuntime, WorkerBuildResult
from tests.test_prepared_source_connectivity import isolation
from tests.test_slang_source_catalog import _catalog_artifact_fixture
from tests.test_source_graph_runtime import ImmediateWorker


@pytest.mark.parametrize('outcome', ['return', 'cancel', 'invalid'])
def test_ingress_saved_before_forwarded_checkpoint(tmp_path, outcome):
    request, ir, receipt = _catalog_artifact_fixture()
    worker = WorkerBuildResult.ready(ir, receipt)
    if outcome == 'invalid':
        worker = replace(worker, ir_fingerprint_sha256='0' * 64)
    key = compute_source_graph_build_key(request)
    observed = {'outcome': outcome, 'callback_calls': 0, 'inputs_before_callback': None}
    (tmp_path / 'control.request.json').write_text(json.dumps(request.to_dict()))
    (tmp_path / 'control.ir.raw.json').write_bytes(worker.ir_json_bytes)

    def checkpoint():
        observed['callback_calls'] += 1
        if observed['inputs_before_callback'] is None:
            directories = list((tmp_path / 'cache_load_evidence').glob('load-*'))
            observed['inputs_before_callback'] = len(directories) == 1 and all(
                (directories[0] / name).is_file()
                for name in ('request.json', 'key.json', 'worker.json', 'ir.raw.json'))
            (tmp_path / 'callback.raw.json').write_text(json.dumps(observed))
        if outcome == 'cancel':
            raise OperationCancelled('controlled loader checkpoint cancellation')

    def load():
        try:
            return SourceGraphRuntime(ImmediateWorker())._load_cache_entry(
                request, key, worker, checkpoint=checkpoint)
        except BaseException as exc:
            (tmp_path / 'control.exception.json').write_text(json.dumps({
                'type': type(exc).__name__, 'message': str(exc)}))
            (tmp_path / 'control.exception.txt').write_text(traceback.format_exc())
            raise
        finally:
            (tmp_path / 'control.observed.json').write_text(json.dumps(observed))

    if outcome == 'return':
        entry = load()
        assert entry.ir_json_bytes == worker.ir_json_bytes
    else:
        error, message = ((OperationCancelled, 'controlled loader checkpoint cancellation')
                          if outcome == 'cancel' else (ValueError, 'worker IR fingerprint mismatch'))
        with pytest.raises(error, match=message):
            load()
    assert observed['callback_calls'] >= 1 and observed['inputs_before_callback'] is True
    directory, = (tmp_path / 'cache_load_evidence').glob('load-*')
    manifest = json.loads((directory / 'manifest.json').read_text())
    assert manifest['outcome'] == ('returned' if outcome == 'return' else 'raised')
    assert (directory / 'ir.raw.json').read_bytes() == worker.ir_json_bytes
    assert json.loads((directory / 'request.json').read_text()) == request.to_dict()
    assert json.loads((directory / 'key.json').read_text()) == key.to_dict()
    if outcome != 'return':
        assert json.loads((directory / 'exception.json').read_text())['type'] == error.__name__
        assert message in (directory / 'exception.txt').read_text()
    for name, row in manifest['files'].items():
        raw = (directory / name).read_bytes()
        assert row == {'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
