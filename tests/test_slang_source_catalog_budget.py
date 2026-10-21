"""Native catalog regressions for declaration priority and shared work caps.

These finite sources and physical coordinates are hand-written oracles. Raw
catalogs/exception evidence are saved before assertions; no backend is mocked.
"""
import json
import threading
import traceback
from types import SimpleNamespace

import pytest

from src.cancellation import OperationCancelled, pop_cancel_event, push_cancel_event
from src.slang_connectivity_projector import ProjectionOptions
from src.slang_source_catalog import collect_source_catalog
from src.slang_source_codec import decode_source_catalog
from src.source_graph_contract import SemanticScopeSearch


def _native(tmp_path, sources, selected, *, max_members=4096, source_max_bytes=2 * 1024 * 1024):
    import pyslang

    paths = []
    for name, text in sources:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding='utf-8')
        paths.append(str(path))
    tree = pyslang.syntax.SyntaxTree.fromFiles(paths)
    compilation = pyslang.ast.Compilation()
    compilation.addSyntaxTree(tree)
    root = compilation.getRoot()
    manager = tree.sourceManager
    kinds = [str(manager.getBufferKind(buffer).name) for buffer in manager.getAllBuffers()]
    (tmp_path / 'native_input_evidence.json').write_text(json.dumps({
        'inputs': paths, 'selected': selected, 'buffer_count': len(kinds),
        'macro_buffer_count': sum(kind in {'Macro', 'MacroArg'} for kind in kinds)}) + '\n')
    search = SemanticScopeSearch(purpose='source_catalog',
        source_files=tuple(sorted(str(tmp_path / name) for name in selected)),
        source_capabilities=('file_declarations',), source_aliases=tuple((path, path) for path in paths),
        max_members=max_members, source_max_bytes=source_max_bytes)
    options = ProjectionOptions(source_root=tmp_path, semantic_search=search,
        files_total=len(paths), files_projected=len(paths), blocking_diagnostic_total=0)
    projector = SimpleNamespace(source_manager=manager, options=options)
    # Keep the owning frontend objects alive for every copied native scope.
    return projector, root, search, (compilation, tree)


def _collect(tmp_path, projector, root, search):
    try:
        raw = json.dumps(collect_source_catalog(projector, root), sort_keys=True)
        (tmp_path / 'catalog.raw.json').write_text(raw + '\n')
        return decode_source_catalog(raw, search)
    except BaseException as exc:
        (tmp_path / 'catalog.exception.json').write_text(json.dumps({
            'type': type(exc).__name__, 'message': str(exc), 'traceback': traceback.format_exc()}) + '\n')
        raise


def test_native_macro_buffer_frontier_retains_requested_definition(tmp_path):
    noise = '`define ID(v) v\nmodule noise;\n' + ''.join(
        f"  wire n{index} = `ID(1'b0);\n" for index in range(512)) + 'endmodule\n'
    sources = [('noise.sv', noise), ('target.sv', '// target physical file\nmodule target;\nendmodule\n')]
    projector, root, search, owners = _native(tmp_path, sources, ['target.sv'], max_members=128)
    result = _collect(tmp_path, projector, root, search)
    evidence = json.loads((tmp_path / 'native_input_evidence.json').read_text())
    assert evidence['macro_buffer_count'] > search.max_members
    assert result['limited'] and result['stop_reason'] == 'source_catalog_visit_limit'
    assert result['member_visits'] == search.max_members and result['scope_visits'] == 0
    row = result['files'][str(tmp_path / 'target.sv')]
    modules = [item for item in row['symbols'] if item['kind'] == 'module']
    assert [(item['name'], item['location']) for item in modules] == [
        ('target', {'file': str(tmp_path / 'target.sv'), 'line': 2, 'column': 8})]
    assert not row['complete'] and 'source_catalog_visit_limit' in row['inventory_gaps']
    assert row['uses_uvm'] is None and row['has_uvm_import'] is None


def test_native_lazy_coordinates_keep_physical_file_and_byte_offsets(tmp_path):
    sources = [('a/unit.sv', '// α physical bytes\n\nmodule first;\n  logic value;\nendmodule\n'),
               ('b/unit.sv', '`line 800 "display_only.sv" 0\n// β\n\nmodule second;\nendmodule\n')]
    projector, root, search, owners = _native(tmp_path, sources, ['a/unit.sv', 'b/unit.sv'])
    result = _collect(tmp_path, projector, root, search)
    expected = {'a/unit.sv': ('first', 3), 'b/unit.sv': ('second', 4)}
    assert not result['limited']
    for relative, (name, line) in expected.items():
        path = str(tmp_path / relative)
        modules = [item for item in result['files'][path]['symbols'] if item['kind'] == 'module']
        assert [(item['name'], item['location']) for item in modules] == [
            (name, {'file': path, 'line': line, 'column': 8})]


@pytest.mark.parametrize('limit', ['visits', 'coordinate_bytes'])
def test_native_declaration_priority_preserves_small_budget_partial(tmp_path, limit):
    kwargs = {'max_members': 1} if limit == 'visits' else {'source_max_bytes': 200}
    projector, root, search, owners = _native(tmp_path,
        [('target.sv', 'module target;\nendmodule\n')], ['target.sv'], **kwargs)
    result = _collect(tmp_path, projector, root, search)
    reason = ('source_catalog_visit_limit' if limit == 'visits'
              else 'source_catalog_coordinate_byte_limit')
    assert result['limited'] and result['stop_reason'] == reason
    assert result['member_visits'] <= search.max_members
    assert result['coordinate_bytes'] <= search.source_max_bytes
    row = result['files'][str(tmp_path / 'target.sv')]
    assert row['symbols'] == [] and not row['complete']
    assert reason in row['inventory_gaps'] and row['uses_uvm'] is None
    if limit == 'visits':
        assert result['member_visits'] == 1


def test_native_lazy_coordinate_index_propagates_mid_scan_cancellation(tmp_path, monkeypatch):
    from src import slang_source_catalog

    projector, root, search, owners = _native(tmp_path,
        [('target.sv', '\n' * 64 + 'module target;\nendmodule\n')], ['target.sv'])
    event = threading.Event()
    original = slang_source_catalog.check_cancelled
    calls = 0
    def cancel_during_index():
        nonlocal calls
        calls += 1
        if calls == 12:
            event.set()
        original()
    monkeypatch.setattr(slang_source_catalog, 'check_cancelled', cancel_during_index)
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            _collect(tmp_path, projector, root, search)
    finally:
        pop_cancel_event(token)
    assert calls == 12
    assert not (tmp_path / 'catalog.raw.json').exists()
    assert json.loads((tmp_path / 'catalog.exception.json').read_text())['type'] == 'OperationCancelled'
