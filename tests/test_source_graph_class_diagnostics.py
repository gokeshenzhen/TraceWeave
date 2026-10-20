"""Copied diagnostic controls at the worker boundary; no SDK or API probe.

The independently executed real missing-base case remains unchanged. These
controls establish phase ordering and coverage publication when class binding
adds diagnostics after the frontend session's initial snapshot.
"""
from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import traceback
from types import SimpleNamespace as NS

import pytest

from src import source_graph_worker as worker
from src.connectivity_ir import ConnectivityIR, CoverageGap, CoverageReport, CoverageStatus, SourceLocation
from src.slang_connectivity_projector import ProjectionReceipt, SlangProjection
from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch
from tests.test_slang_class_catalog import search, class_payload
from tests.test_source_graph_runtime import _request, _scope


def diagnostics(severity=None):
    items = [] if severity is None else [dict(code='UnknownClass', severity=severity,
        message='controlled missing class definition', file='/rtl/a.sv', line=8, column=1)]
    return dict(total=len(items), blocking_error_count=int(severity in {'Error', 'Fatal'}),
                items=items, items_truncated=False)


def projection(*, existing=False, positive=False):
    payload = class_payload()
    if positive:
        payload['files']['/rtl/a.sv'].update(has_uvm_import=True, uses_uvm=True)
    gap = CoverageGap('frontend_diagnostic:UnknownClass', 'controlled missing class definition',
        CoverageStatus.INCONCLUSIVE, ('frontend_diagnostic',), ('*',), SourceLocation('/rtl/a.sv', 8, 1))
    ir = ConnectivityIR(frontend_name='hand_oracle', frontend_version='1.0', definitions=(),
        instances=(), bindings=(), top_instances=(), coverage=CoverageReport(
            CoverageStatus.INCONCLUSIVE if existing else CoverageStatus.COMPLETE,
            1, 1, gaps=(gap,) if existing else (), diagnostic_count=int(existing),
            blocking_diagnostic_count=int(existing)),
        metadata=(('source_catalog_v1', json.dumps(payload, sort_keys=True)),))
    receipt = ProjectionReceipt(0, 0, 0, 0, 0, 0, int(existing), False, 'focused_instances', 1, 0)
    return SlangProjection(ir, receipt)


def observe(root, request, action):
    (root / 'request.json').write_text(json.dumps(request.to_dict()) + '\n')
    try:
        result = action()
        ir, receipt = result[0], result[1]
        (root / 'ir.raw.json').write_bytes(ir.to_json_bytes())
        (root / 'receipt.raw.json').write_text(json.dumps(receipt) + '\n')
        (root / 'ir.decoded.json').write_text(json.dumps(json.loads(ir.to_json_bytes())) + '\n')
        return result
    except BaseException as exc:
        (root / 'exception.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc())) + '\n')
        raise


@pytest.mark.parametrize('severity', [None, 'Warning', 'Error'])
def test_worker_refreshes_after_projection_and_reconciles_catalog_ir_and_scope_receipt(tmp_path, monkeypatch, severity):
    request = _request(scope=replace(_scope(), semantic_search=search())).artifact_build_request
    before, current = diagnostics(), diagnostics(severity)
    phases = []
    def get_all():
        phases.append('diagnostics')
        assert phases[0] == 'project'
        return ['controlled live diagnostics']
    session = worker.SemanticFrontendSession(driver=NS(sourceManager=object()),
        compilation=NS(getAllDiagnostics=get_all), root=object(),
        diagnostic_payload=before, frontend_version='1.0')
    def project(**arguments):
        phases.append('project')
        assert arguments['options'].diagnostic_total == 0
        return projection()
    monkeypatch.setattr(worker, 'project_slang_design', project)
    monkeypatch.setattr(worker, '_diagnostics_payload', lambda driver, values: current)
    (tmp_path / 'request.json').write_text(json.dumps(request.to_dict()) + '\n')
    try:
        raw, fingerprint, scope, receipt = worker.project_semantic_frontend_session(session, request)
        (tmp_path / 'ir.raw.json').write_bytes(raw)
        (tmp_path / 'scope.raw.json').write_text(json.dumps(scope.to_dict()) + '\n')
        (tmp_path / 'receipt.raw.json').write_text(json.dumps(receipt) + '\n')
        ir = ConnectivityIR.from_json_bytes(raw)
        (tmp_path / 'ir.decoded.json').write_text(json.dumps(json.loads(raw)) + '\n')
    except BaseException as exc:
        (tmp_path / 'exception.json').write_text(json.dumps(dict(type=type(exc).__name__,
            message=str(exc), traceback=traceback.format_exc())) + '\n')
        raise
    assert phases == ['project', 'diagnostics'] and session.diagnostic_payload == current
    assert ir.fingerprint_sha256() == fingerprint
    assert scope.coverage_status == ir.coverage.status
    assert ir.coverage.diagnostic_count == int(severity is not None)
    assert ir.coverage.blocking_diagnostic_count == int(severity == 'Error')
    payload = decode_source_catalog(dict(ir.metadata)['source_catalog_v1'], search())
    evidence = json.loads(dict(ir.metadata)['source_class_diagnostics_v1'])
    assert evidence['observation'] == 'post_class_binding' and evidence['initial_total'] == 0
    assert evidence['total'] == int(severity is not None)
    if severity == 'Error':
        assert ir.coverage.status is CoverageStatus.INCONCLUSIVE
        assert not payload['classes']['complete']
        assert payload['files']['/rtl/a.sv']['has_uvm_import'] is None
        assert payload['files']['/rtl/a.sv']['uses_uvm'] is None
        assert 'source_class_frontend_diagnostics' in payload['classes']['gaps']
        assert 'frontend_diagnostic_unknownclass' in scope.gap_codes
        assert receipt['projection_gap_count'] == len(ir.coverage.gaps) == 1
    else:
        assert ir.coverage.status is CoverageStatus.COMPLETE
        assert payload['classes']['complete']
        assert payload['files']['/rtl/a.sv']['has_uvm_import'] is False
        assert receipt['projection_gap_count'] == 0


def test_late_class_diagnostics_preserve_initial_gaps_and_positive_import_facts(tmp_path, monkeypatch):
    request = _request(scope=replace(_scope(), semantic_search=search())).artifact_build_request
    current = diagnostics('Error')
    session = NS(diagnostic_payload=current, driver=object(), compilation=NS(getAllDiagnostics=lambda: []))
    monkeypatch.setattr(worker, '_diagnostics_payload', lambda *args: current)
    original = projection(existing=True, positive=True)
    ir, receipt = observe(tmp_path, request, lambda: worker._refresh_class_projection_diagnostics(
        session, request, original, Path('/unrelated-root')))
    assert ir.coverage.gaps == original.ir.coverage.gaps
    assert receipt['projection_gap_count'] == 1
    payload = decode_source_catalog(dict(ir.metadata)['source_catalog_v1'], search())
    assert payload['files']['/rtl/a.sv']['has_uvm_import'] is True
    assert payload['files']['/rtl/a.sv']['uses_uvm'] is True


@pytest.mark.parametrize('kind', ['connectivity', 'file_only'])
def test_ordinary_artifacts_do_not_refresh_or_change_diagnostic_execution(tmp_path, kind):
    scope = _scope()
    if kind == 'file_only':
        scope = replace(scope, semantic_search=SemanticScopeSearch(purpose='source_catalog',
            source_files=('/rtl/a.sv',), source_capabilities=('file_declarations',)))
    request = _request(scope=scope).artifact_build_request
    def forbidden(): raise AssertionError('ordinary artifact refreshed class diagnostics')
    original = projection()
    session = NS(compilation=NS(getAllDiagnostics=forbidden))
    ir, receipt = observe(tmp_path, request, lambda: worker._refresh_class_projection_diagnostics(
        session, request, original, Path('/unrelated-root')))
    assert ir is original.ir and receipt == original.receipt.to_dict()


def test_class_diagnostic_counts_cannot_regress_inside_a_reused_session(tmp_path, monkeypatch):
    request = _request(scope=replace(_scope(), semantic_search=search())).artifact_build_request
    session = NS(diagnostic_payload=diagnostics('Error'), driver=object(),
                 compilation=NS(getAllDiagnostics=lambda: []))
    monkeypatch.setattr(worker, '_diagnostics_payload', lambda *args: diagnostics())
    with pytest.raises(ValueError, match='regressed'):
        observe(tmp_path, request, lambda: worker._refresh_class_projection_diagnostics(
            session, request, projection(), Path('/unrelated-root')))


def test_late_diagnostics_preserve_bounded_gap_output_with_separate_total(tmp_path, monkeypatch):
    request = _request(scope=replace(_scope(), semantic_search=search())).artifact_build_request
    current = diagnostics('Error')
    session = NS(diagnostic_payload=diagnostics(), driver=object(), compilation=NS(getAllDiagnostics=lambda: []))
    monkeypatch.setattr(worker, '_diagnostics_payload', lambda *args: current)
    original = projection()
    gaps = tuple(CoverageGap('old_' + str(index), 'controlled existing exclusion', CoverageStatus.INCONCLUSIVE)
                 for index in range(256))
    original = replace(original, ir=replace(original.ir, coverage=replace(original.ir.coverage,
        status=CoverageStatus.INCONCLUSIVE, gaps=gaps)),
        receipt=replace(original.receipt, projection_gap_count=256))
    ir, receipt = observe(tmp_path, request, lambda: worker._refresh_class_projection_diagnostics(
        session, request, original, Path('/unrelated-root')))
    assert ir.coverage.gaps == gaps and len(ir.coverage.gaps) == 256
    assert ir.coverage.blocking_diagnostic_count == 1
    assert receipt['projection_gap_count'] == 257 and receipt['projection_gaps_truncated']


def test_late_diagnostic_display_bytes_cannot_overrun_catalog_envelope(tmp_path, monkeypatch):
    request = _request(scope=replace(_scope(), semantic_search=search())).artifact_build_request
    current = diagnostics('Error')
    current['items'][0]['message'] = 'x' * (search().source_max_bytes // 4)
    session = NS(diagnostic_payload=diagnostics(), driver=object(), compilation=NS(getAllDiagnostics=lambda: []))
    monkeypatch.setattr(worker, '_diagnostics_payload', lambda *args: current)
    ir, receipt = observe(tmp_path, request, lambda: worker._refresh_class_projection_diagnostics(
        session, request, projection(), Path('/unrelated-root')))
    payload = decode_source_catalog(dict(ir.metadata)['source_catalog_v1'], search())
    assert payload['coordinate_bytes'] <= search().source_max_bytes
    assert ir.coverage.blocking_diagnostic_count == 1 and ir.coverage.gaps == ()
    assert receipt['projection_gap_count'] == 1 and receipt['projection_gaps_truncated']
    marker = json.loads(dict(ir.metadata)['source_class_diagnostics_v1'])
    assert marker['details_status'] == 'truncated' and 'items' not in marker
    assert len(ir.to_json_bytes()) < 10000
    assert not payload['classes']['complete']
