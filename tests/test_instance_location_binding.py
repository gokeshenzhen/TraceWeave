"""Raw vendor locations bind only to handwritten captured spellings.

These metadata controls prove no file existence, NPI/source association or
native semantics. The helper intentionally mutates row gaps in place.
"""
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
import builtins
import os
from pathlib import Path
import threading

import pytest

from src import hierarchy_source_metadata as metadata
from src.cancellation import OperationCancelled, push_cancel_event, pop_cancel_event
from src.compile_input_capture import CaptureAborted
from src.compile_session_snapshot import CompileSessionSnapshot, FileContentSnapshot
from src.design_context import DesignContext


V1, V2 = '/releases/v1/shared.sv', '/releases/v2/shared.sv'
ALIAS = '/captured/links/shared_link.sv'
EXTERNAL = '/sdk/current_directory/shared.sv'
INSTANCE_GAP, DEFINITION_GAP = 'npi_instance_location_unbound', 'npi_definition_location_unbound'


def context(*, captured=(V1, V2), aliases=((ALIAS, V1),), unavailable=()):
    records = tuple(FileContentSnapshot(path=path, device=7, inode=index + 1, size=10,
                                       mtime_ns=11, ctime_ns=12, sha256='a' * 64)
                    for index, path in enumerate(captured))
    return DesignContext(
        ordered_compile_logs=('/support/comp.log',), simulator='vcs', selected_top='top',
        backend_preference='auto', compile_result_json='{}',
        snapshot=CompileSessionSnapshot(records, not unavailable, (), 'b' * 64),
        aliases=tuple(aliases), unavailable=tuple(unavailable), include_resolutions=(),
        environment=(), identity_sha256='c' * 64, gaps=(), include_resolution_complete=True, metrics=())


def result(instance, definition, *, gaps=()):
    return {'schema_version': 2, 'facts_origin': 'verdi_npi', 'artifact_identity': 'vendor-design',
            'count_status': 'exact', 'total': 1, 'count_lower_bound': 1,
            'coverage_status': 'complete', 'coverage_gaps': [], 'analysis_limits': [],
            'hits': [{'scope_id': 'instance-id', 'path': 'top.u', 'parent': 'top',
                      'definition_id': 'definition-id', 'module': 'leaf',
                      'source_file': instance, 'source_line': 484 if instance is not None else None,
                      'definition_file': definition, 'definition_line': 1 if definition is not None else None,
                      'gaps': list(gaps)}]}


@contextmanager
def no_io(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('location binding attempted live I/O or context validation')
    # Restore before pytest inspects assertion tracebacks or formats paths.
    with monkeypatch.context() as patch:
        patch.setattr(builtins, 'open', forbidden)
        for name in ('open', 'stat', 'lstat', 'scandir', 'listdir', 'readlink'):
            patch.setattr(os, name, forbidden)
        for name in ('realpath', 'abspath', 'exists', 'isfile', 'isdir'):
            patch.setattr(os.path, name, forbidden)
        for name in ('open', 'read_text', 'read_bytes', 'stat', 'lstat', 'resolve', 'exists'):
            patch.setattr(Path, name, forbidden)
        patch.setattr(DesignContext, 'current', forbidden)
        patch.setattr(DesignContext, 'metadata_current', forbidden)
        yield


@pytest.mark.parametrize('instance,definition,expected', [
    (V1, V2, []), (ALIAS, V2, []), (V1, ALIAS, []),
    (EXTERNAL, V2, [INSTANCE_GAP]), (V1, EXTERNAL, [DEFINITION_GAP]),
    (EXTERNAL, '/outside/other.sv', [DEFINITION_GAP, INSTANCE_GAP]),
    (None, V2, []), (V1, None, []), (None, None, []),
])
def test_exact_record_and_captured_alias_bind_roles_independently_without_io(monkeypatch, instance, definition, expected):
    ctx, payload = context(), result(instance, definition)
    before, metadata_before = deepcopy(payload), deepcopy(ctx)
    with no_io(monkeypatch):
        returned = metadata.mark_unbound_instance_locations(payload, ctx)
    assert returned is None
    assert payload['hits'][0]['gaps'] == expected
    # Everything except the intended gaps mutation remains byte-for-byte facts.
    actual, original = deepcopy(payload), deepcopy(before)
    actual['hits'][0].pop('gaps')
    original['hits'][0].pop('gaps')
    assert actual == original
    assert ctx == metadata_before


@pytest.mark.parametrize('captured', [(V1,), (V1, V2)])
def test_unique_or_ambiguous_basename_never_maps_external_vendor_path(monkeypatch, captured):
    payload = result(EXTERNAL, V1)
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, context(captured=captured))
    assert payload['hits'][0]['gaps'] == [INSTANCE_GAP]
    assert payload['hits'][0]['source_file'] == EXTERNAL
    assert payload['hits'][0]['source_line'] == 484


@pytest.mark.parametrize('spelling', ['shared.sv', '/releases/v1/./shared.sv',
                                    '/releases/v2/../v1/shared.sv', '/RELEASES/v1/shared.sv'])
def test_unobserved_relative_normalized_and_case_spellings_are_not_inferred(monkeypatch, spelling):
    payload = result(spelling, V1)
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, context())
    assert payload['hits'][0]['gaps'] == [INSTANCE_GAP]
    assert payload['hits'][0]['source_file'] == spelling


def test_alias_requires_its_exact_canonical_target_to_have_been_captured(monkeypatch):
    # An alias observation alone, a missing record, or another version is not
    # evidence of captured source bytes for that target.
    ctx = context(captured=(V2,), aliases=((ALIAS, V1),), unavailable=((V1, ('missing',)),))
    payload = result(ALIAS, V1)
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, ctx)
    assert payload['hits'][0]['gaps'] == [DEFINITION_GAP, INSTANCE_GAP]


def test_record_binding_is_an_observation_not_a_live_existence_check(monkeypatch):
    ctx = context(captured=('/captured_at_prepare/no_longer_accessible.sv',), aliases=())
    payload = result('/captured_at_prepare/no_longer_accessible.sv', None)
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, ctx)
    assert payload['hits'][0]['gaps'] == []
    # Currentness belongs to the enclosing query final validation, not helper.
    assert ctx.snapshot.complete is True


def test_existing_gaps_and_unknown_positions_survive_idempotent_marking(monkeypatch):
    payload = result(EXTERNAL, None, gaps=('protected_region', INSTANCE_GAP, 'npi_location_unavailable'))
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, context())
        first = deepcopy(payload)
        metadata.mark_unbound_instance_locations(payload, context())
    assert payload == first
    assert payload['hits'][0]['gaps'] == [INSTANCE_GAP, 'npi_location_unavailable', 'protected_region']
    assert payload['hits'][0]['definition_file'] is payload['hits'][0]['definition_line'] is None
    assert payload['coverage_status'] == 'complete' and payload['total'] == 1


def test_rows_do_not_share_location_gaps_or_upgrade_source_association(monkeypatch):
    payload = result(EXTERNAL, V1)
    payload['hits'].append(result(V2, '/unobserved/definition.sv')['hits'][0])
    payload['hits'][1].update(scope_id='second-id', path='top.v')
    payload.update(total=2, count_lower_bound=2)
    payload['kdb_binding'] = {'source_association': 'unverified'}
    ctx = replace(context(), gaps=('protected_region',), include_resolution_complete=False)
    with no_io(monkeypatch):
        metadata.mark_unbound_instance_locations(payload, ctx)
    assert payload['hits'][0]['gaps'] == [INSTANCE_GAP]
    assert payload['hits'][1]['gaps'] == [DEFINITION_GAP]
    assert payload['kdb_binding'] == {'source_association': 'unverified'}
    assert ctx.gaps == ('protected_region',) and ctx.include_resolution_complete is False


@pytest.mark.parametrize('error', [CaptureAborted('timeout'), TimeoutError('explicit timeout')])
def test_typed_checkpoint_errors_propagate_as_original_objects(monkeypatch, error):
    def fail():
        raise error
    monkeypatch.setattr(metadata, 'check_cancelled', fail)
    with pytest.raises(type(error)) as raised:
        metadata.mark_unbound_instance_locations(result(EXTERNAL, V1), context())
    assert raised.value is error


def test_cancelled_binding_does_not_return_a_fabricated_success():
    event = threading.Event()
    event.set()
    token = push_cancel_event(event)
    try:
        with pytest.raises(OperationCancelled):
            metadata.mark_unbound_instance_locations(result(EXTERNAL, V1), context())
    finally:
        pop_cancel_event(token)
