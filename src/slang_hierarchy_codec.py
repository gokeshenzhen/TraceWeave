"""Strict capability/domain codec for topology-free hierarchy artifacts."""
from __future__ import annotations

import json
import hashlib
import re

from .connectivity_ir import SourceLocation
from .connectivity_ir import CoverageStatus
from .hierarchy_source_ir import HierarchyScope

_KEY = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z][a-z0-9_]*")
_REASONS = {"source_hierarchy_member_limit", "source_hierarchy_scope_limit",
            "source_hierarchy_depth_limit", "source_hierarchy_byte_limit",
            "source_hierarchy_parameter_limit"}


def _fields(value, fields):
    if type(value) is not dict or set(value) != set(fields.split()):
        raise ValueError("hierarchy artifact fields invalid")


def _text(value):
    return isinstance(value, str) and bool(value) and "\x00" not in value


def _labels(value):
    if (type(value) is not list or any(not isinstance(item, str) or not _LABEL.fullmatch(item)
                                     for item in value) or value != sorted(set(value))):
        raise ValueError("hierarchy artifact gaps invalid")


def decode_hierarchy(raw, search, top, *, checkpoint=lambda: None):
    if not isinstance(raw, str) or len(raw.encode('utf-8')) > search.hierarchy_max_bytes:
        raise ValueError('hierarchy serialized payload exceeds byte envelope')
    def pairs(items):
        value = dict(items)
        if len(items) != len(value):
            raise ValueError("duplicate hierarchy object key")
        return value
    payload = json.loads(raw, object_pairs_hook=pairs)
    _fields(payload, "version capability domain selected_top semantic_tops search_sha256 rows complete_children "
                     "member_visits retained_bytes limited stop_reason")
    if (type(payload['version']) is not int or payload['version'] != search.hierarchy_version
            or payload['version'] != 2 or payload['capability'] != 'instance_hierarchy'
            or payload['domain'] != 'selected_top_elaborated_scopes'
            or payload['selected_top'] != top
            or payload['search_sha256'] != hashlib.sha256(json.dumps(search.to_dict(), sort_keys=True,
                                                        separators=(',', ':')).encode()).hexdigest()
            or not search.roots):
        raise ValueError("hierarchy capability, top or request envelope mismatch")
    for key, maximum in (('member_visits', search.max_members),
                         ('retained_bytes', search.hierarchy_max_bytes)):
        if type(payload[key]) is not int or not 0 <= payload[key] <= maximum:
            raise ValueError("hierarchy work accounting invalid")
    tops = payload['semantic_tops']
    if (type(tops) is not list or len(tops) > payload['member_visits']
            or any(not _text(item) for item in tops)
            or tops != sorted(set(tops)) or top not in tops
            ):
        raise ValueError("hierarchy actual top evidence invalid")
    if (type(payload['limited']) is not bool or payload['stop_reason'] not in _REASONS | {None}
            or payload['limited'] != (payload['stop_reason'] is not None)):
        raise ValueError("hierarchy frontier evidence invalid")
    rows, paths = payload['rows'], {}
    if type(rows) is not list or len(rows) > min(search.max_instances, payload['member_visits']):
        raise ValueError("hierarchy row accounting invalid")
    retained_minimum = 512 + sum(64 + 4 * len(json.dumps(item, ensure_ascii=True)) for item in tops)
    allowed_files = {pair[1] for pair in search.hierarchy_aliases}
    for row in rows:
        checkpoint()
        _fields(row, "path name kind parent_path definition_id definition_name location definition_location parameterization gaps")
        path, parent = row['path'], row['parent_path']
        if (not _text(path) or path in paths or not (path == top or path.startswith(top + '.'))
                or not _text(row['name']) or parent is not None and (not _text(parent) or parent not in paths)
                or path == top and parent is not None
                or parent is None and path != top
                or parent is not None and not path.startswith((parent + '.', parent + '['))):
            raise ValueError("hierarchy parent or selected-top domain invalid")
        for field in ('location', 'definition_location'):
            value = row[field]
            if value is not None:
                _fields(value, 'file line column')
                if (value['file'] not in allowed_files or type(value['line']) is not int or value['line'] < 1
                        or type(value['column']) is not int or value['column'] < 0):
                    raise ValueError("hierarchy physical source ownership invalid")
        definition = row['definition_id']
        if definition is not None and (not isinstance(definition, str) or not _KEY.fullmatch(definition)):
            raise ValueError("hierarchy definition identity invalid")
        parameters = row['parameterization']
        if (type(parameters) is not list or len(parameters) > 128 or
                any(type(pair) is not list or len(pair) != 2 or not _text(pair[0])
                    or not isinstance(pair[1], str) for pair in parameters)
                or len({pair[0] for pair in parameters}) != len(parameters)):
            raise ValueError("hierarchy parameter evidence invalid")
        _labels(row['gaps'])
        if (('source_instance_location_unavailable' in row['gaps']) != (row['location'] is None)
                or ('source_definition_location_unavailable' in row['gaps']) !=
                    (definition is not None and row['definition_location'] is None)):
            raise ValueError('hierarchy missing location coverage invalid')
        if ((row['kind'] in {'module', 'interface', 'program'}) != (definition is not None)
                or definition is None and (row['parameterization'] or row['definition_location'] is not None)):
            raise ValueError("hierarchy scope definition role invalid")
        if definition is not None:
            expected_id = hashlib.sha256(json.dumps([row['definition_name'], row['kind'].capitalize(),
                row['definition_location'], parameters], sort_keys=True).encode()).hexdigest()
            if definition != expected_id:
                raise ValueError('hierarchy definition identity does not match stored evidence')
        HierarchyScope('validated', path, row['name'], row['kind'], parent,
            definition, row['definition_name'],
            SourceLocation(**row['location']) if row['location'] else None,
            SourceLocation(**row['definition_location']) if row['definition_location'] else None,
            tuple(map(tuple, parameters)), tuple(row['gaps']))
        paths[path] = row
        retained_minimum += 512 + 4 * len(json.dumps(row, ensure_ascii=True, sort_keys=True))
    if retained_minimum > payload['retained_bytes']:
        raise ValueError("hierarchy retained payload not charged")
    complete = payload['complete_children']
    if (type(complete) is not list or len(complete) > len(rows)
            or any(not _text(path) for path in complete) or complete != sorted(set(complete))
            or any(path not in paths or paths[path]['gaps'] for path in complete)):
        raise ValueError("hierarchy child exhaustion evidence invalid")
    # Every non-ancestor row must be reachable within the exact root/depth
    # domain. A bounded frontier has no child-exhaustion proof at that depth.
    levels = {}
    roots = set(search.roots)
    for path, row in paths.items():
        if path in roots:
            levels[path] = 0
        elif row['parent_path'] in levels:
            levels[path] = levels[row['parent_path']] + 1
        elif not any(root.startswith((path + '.', path + '[')) for root in roots):
            raise ValueError("hierarchy row outside requested domain")
        if path in levels:
            if levels[path] > search.depth:
                raise ValueError("hierarchy row beyond depth budget")
            if search.hierarchy_mode == 'bounded' and levels[path] == search.depth and path in complete:
                raise ValueError("hierarchy frontier falsely exhausted")
    return payload


def validate_hierarchy_artifact(ir, scope, *, checkpoint=lambda: None, return_payload=False):
    metadata, search = dict(ir.metadata), scope.semantic_search
    raw = metadata.get('hierarchy_scopes_v2')
    if search is None or search.purpose != 'hierarchy':
        if raw is not None or 'hierarchy_scopes_v1' in metadata:
            raise ValueError("hierarchy metadata requires its capability scope")
        return False
    if (raw is None or 'hierarchy_scopes_v1' in metadata or 'source_catalog_v1' in metadata
            or ir.instances or ir.definitions or ir.bindings or ir.top_instances):
        raise ValueError("hierarchy artifact contains missing or mixed facts")
    payload = decode_hierarchy(raw, search, scope.top, checkpoint=checkpoint)
    codes = {gap.code for gap in ir.coverage.gaps}
    if payload['limited'] and (ir.coverage.status is CoverageStatus.COMPLETE
                              or payload['stop_reason'] not in codes):
        raise ValueError('hierarchy limit missing from artifact coverage')
    if any(row['gaps'] for row in payload['rows']) and (
            ir.coverage.status is CoverageStatus.COMPLETE or
            ('source_hierarchy_facts_incomplete' not in codes and not
             (payload['limited'] and payload['stop_reason'] in codes))):
        raise ValueError('hierarchy partial facts missing from artifact coverage')
    return payload if return_payload else True
