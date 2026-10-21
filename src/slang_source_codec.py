"""Fail-closed primitive codec for the worker's source-catalog capability."""
from __future__ import annotations

import json
import re

from .hierarchy_source_symbols import SourceSymbol, ClassFact
from .connectivity_ir import SourceLocation

CATALOG_VERSION = 1
DECLARATION_DOMAIN = "compilation_declarations_and_elaborated_scopes"
USAGE_DOMAIN = "resolved_types_imports_and_expression_references"
_KEY = re.compile(r"[0-9a-f]{64}")
_LABEL = re.compile(r"[a-z][a-z0-9_]*")


def _fields(value, names):
    if not isinstance(value, dict) or set(value) != set(names.split()):
        raise ValueError("source catalog fields invalid")


def _count(value, maximum):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError("source catalog count invalid")


def _labels(value):
    if (not isinstance(value, list) or any(not isinstance(item, str) or not _LABEL.fullmatch(item)
                                         for item in value) or value != sorted(set(value))):
        raise ValueError("source catalog gaps invalid")


def decode_source_catalog(raw, search, *, checkpoint=lambda: None):
    # Reject duplicates at every object level, including per-file mappings.
    def object_pairs(pairs):
        value = dict(pairs)
        if len(value) != len(pairs):
            raise ValueError("duplicate source catalog object key")
        return value
    payload = json.loads(raw, object_pairs_hook=object_pairs)
    class_capable = "class_inheritance" in search.source_capabilities
    _fields(payload, "version capabilities source_files semantic_tops declaration_domain usage_domain member_visits "
                     "scope_visits limited stop_reason coordinate_bytes limits files" +
                     (" classes" if class_capable else ""))
    if (type(payload['version']) is not int or payload['version'] != CATALOG_VERSION
            or payload['version'] != search.source_version
            or payload['source_files'] != list(search.source_files)
            or payload['capabilities'] != list(search.source_capabilities)
            or payload['declaration_domain'] != DECLARATION_DOMAIN
            or payload['usage_domain'] != USAGE_DOMAIN):
        raise ValueError("source catalog capability or domain mismatch")
    _fields(payload['limits'], "members scopes depth coordinate_bytes")
    expected = dict(members=search.max_members, scopes=search.source_max_scopes, depth=search.source_max_depth,
                    coordinate_bytes=search.source_max_bytes)
    if any(type(payload['limits'][key]) is not int for key in expected) or payload['limits'] != expected:
        raise ValueError("source catalog limits mismatch")
    _count(payload['member_visits'], search.max_members)
    _count(payload['scope_visits'], search.source_max_scopes)
    _count(payload['coordinate_bytes'], search.source_max_bytes)
    tops = payload['semantic_tops']
    if (not isinstance(tops, list) or len(tops) > payload['member_visits']
            or any(not isinstance(top, str) or not top.strip() for top in tops)
            or tops != sorted(set(tops))
            or sum(len(top.encode('utf-8')) + 64 for top in tops) > payload['coordinate_bytes']):
        raise ValueError('source catalog semantic top evidence invalid')
    if payload['scope_visits'] > payload['member_visits']:
        raise ValueError('source catalog scope accounting invalid')
    if type(payload['limited']) is not bool:
        raise ValueError("source catalog limit status invalid")
    reason = payload['stop_reason']
    if reason not in {None, 'source_catalog_visit_limit', 'source_catalog_scope_limit', 'source_catalog_depth_limit',
                      'source_catalog_coordinate_byte_limit', 'source_class_parameter_limit'}:
        raise ValueError("source catalog stop reason invalid")
    if payload['limited'] != (reason is not None):
        raise ValueError("source catalog limit evidence invalid")
    if not isinstance(payload['files'], dict) or set(payload['files']) != set(search.source_files):
        raise ValueError("source catalog file domain mismatch")
    all_keys = set()
    for path, row in payload['files'].items():
        checkpoint()
        _fields(row, "symbols inventory_gaps import_gaps usage_gaps gaps complete has_uvm_import uses_uvm")
        for name in ('inventory_gaps', 'import_gaps', 'usage_gaps', 'gaps'):
            _labels(row[name])
        if row['gaps'] != sorted(set(row['inventory_gaps'] + row['import_gaps'] + row['usage_gaps'])):
            raise ValueError("source catalog coverage union invalid")
        if type(row['complete']) is not bool or row['complete'] != (not row['inventory_gaps']):
            raise ValueError("source catalog inventory coverage invalid")
        if reason is not None and any(reason not in row[name] for name in ('inventory_gaps', 'import_gaps', 'usage_gaps')):
            raise ValueError("source catalog lost its analysis frontier")
        for name, domain in (('has_uvm_import', 'import_gaps'), ('uses_uvm', 'usage_gaps')):
            flag = row[name]
            if flag is not None and type(flag) is not bool:
                raise ValueError("source catalog UVM evidence invalid")
            if (flag is False and row[domain]) or (flag is None and not row[domain]):
                raise ValueError("source catalog UVM coverage invalid")
        if row['has_uvm_import'] is True and row['uses_uvm'] is not True:
            raise ValueError("source catalog import does not imply usage")
        if not isinstance(row['symbols'], list) or len(row['symbols']) > payload['member_visits']:
            raise ValueError("source catalog symbols invalid")
        for symbol in row['symbols']:
            checkpoint()
            _fields(symbol, "key name qualified_name kind location scope_key declaration_key type_key "
                            "parameterization external gaps declaration_status")
            for name in ('key', 'scope_key', 'declaration_key', 'type_key'):
                value = symbol[name]
                if value is None and name in {'scope_key', 'type_key'}:
                    continue
                if not isinstance(value, str) or not _KEY.fullmatch(value):
                    raise ValueError("source catalog declaration identity invalid")
            if symbol['key'] in all_keys:
                raise ValueError("duplicate source catalog declaration identity")
            all_keys.add(symbol['key'])
            if len(all_keys) > payload['member_visits']:
                raise ValueError('source catalog symbol accounting invalid')
            _fields(symbol['location'], "file line column")
            where = symbol['location']
            if where['file'] != path or type(where['line']) is not int or where['line'] < 1:
                raise ValueError("source catalog declaration ownership invalid")
            if type(where['column']) is not int or where['column'] < 0:
                raise ValueError("source catalog declaration column invalid")
            _labels(symbol['gaps'])
            if type(symbol['external']) is not bool or not isinstance(symbol['parameterization'], list):
                raise ValueError("source catalog declaration primitives invalid")
            SourceSymbol(symbol['key'], symbol['name'], symbol['qualified_name'], symbol['kind'],
                location=SourceLocation(**where), scope_id=symbol['scope_key'],
                declaration_id=symbol['declaration_key'], type_id=symbol['type_key'],
                parameterization=tuple(symbol['parameterization']), external=symbol['external'],
                gaps=tuple(symbol['gaps']), declaration_status=symbol['declaration_status'])
    if class_capable:
        _decode_classes(payload['classes'], payload, checkpoint)
    return payload


def _decode_classes(value, catalog, checkpoint):
    _fields(value, "domain rows type_keys gaps complete")
    if value['domain'] != 'declarations_and_referenced_specializations':
        raise ValueError('class inventory domain mismatch')
    _labels(value['gaps'])
    if type(value['complete']) is not bool or value['complete'] != (not value['gaps']):
        raise ValueError('class inventory completeness invalid')
    if catalog['limited'] and ('source_class_inventory_limited' not in value['gaps'] or value['complete']):
        raise ValueError('class inventory lost analysis frontier')
    rows, types = value['rows'], value['type_keys']
    if (not isinstance(types, list) or len(types) > catalog['member_visits']
            or any(not isinstance(key, str) or not _KEY.fullmatch(key) for key in types)
            or len(set(types)) != len(types)
            or not isinstance(rows, list) or len(rows) > catalog['member_visits']):
        raise ValueError('class inventory accounting invalid')
    types, identities = set(types), set()
    for row in rows:
        checkpoint()
        _fields(row, "key declaration_key type_key name qualified_name location scope_key declaration_status "
                     "parameterization type_parameter_bindings base_status base_key base_qualified_name "
                     "base_relation base_value_parameters base_type_parameter_bindings base_parameter_status gaps")
        for field in ('key', 'declaration_key', 'type_key', 'scope_key', 'base_key'):
            key = row[field]
            if key is None and field in {'type_key', 'scope_key', 'base_key'}:
                continue
            if not isinstance(key, str) or not _KEY.fullmatch(key):
                raise ValueError('class inventory identity invalid')
        if row['key'] in identities:
            raise ValueError('duplicate class inventory identity')
        identities.add(row['key'])
        _fields(row['location'], 'file line column')
        where = row['location']
        if (where['file'] not in catalog['files'] or type(where['line']) is not int or where['line'] < 1
                or type(where['column']) is not int or where['column'] < 0):
            raise ValueError('class declaration ownership invalid')
        _labels(row['gaps'])
        for field in ('parameterization', 'type_parameter_bindings', 'base_value_parameters',
                      'base_type_parameter_bindings'):
            pairs = row[field]
            if (not isinstance(pairs, list) or len(pairs) > 128 or
                    any(not isinstance(pair, list) or len(pair) != 2 or
                        not all(isinstance(item, str) for item in pair) or not pair[0] for pair in pairs)
                    or len({pair[0] for pair in pairs}) != len(pairs)):
                raise ValueError('class parameter binding invalid')
            if field.endswith('type_parameter_bindings') and any(pair[1] not in types for pair in pairs):
                raise ValueError('class parameter type identity not interned')
        if row['type_key'] is not None and row['type_key'] not in types:
            raise ValueError('class canonical type not interned')
        if row['declaration_status'] == 'specialization' and (
                row['type_key'] is None or row['key'] != row['type_key']):
            raise ValueError('class specialization requires canonical identity')
        if row['type_key'] is None and row['key'] != row['declaration_key']:
            raise ValueError('uninstantiated class declaration identity invalid')
        if row['type_key'] is not None and row['key'] != row['type_key']:
            raise ValueError('actual class requires canonical identity')
        if row['base_relation'] not in {None, 'declaration', 'specialization'}:
            raise ValueError('class base relation invalid')
        if (row['base_relation'] is not None) != (row['base_status'] == 'resolved'):
            raise ValueError('class relation requires resolved base')
        if row['base_status'] == 'resolved' and row['base_key'] not in types:
            raise ValueError('class base canonical identity not interned')
        symbol = SourceSymbol(row['key'], row['name'], row['qualified_name'], 'class',
            location=SourceLocation(**where), scope_id=row['scope_key'],
            declaration_id=row['declaration_key'], type_id=row['type_key'],
            parameterization=tuple(row['parameterization']), gaps=tuple(row['gaps']),
            declaration_status=row['declaration_status'])
        ClassFact(symbol, row['base_status'], row['base_key'], row['base_qualified_name'],
            tuple(row['gaps']), tuple(row['base_value_parameters']), row['base_parameter_status'],
            row['base_relation'] or 'declaration', tuple(row['type_parameter_bindings']),
            tuple(row['base_type_parameter_bindings']), 'artifact_canonical')


def validate_source_catalog_artifact(ir, scope, *, checkpoint=lambda: None, return_payload=False):
    """Validate the topology-free capability envelope at every load boundary.

    Return False for ordinary IR, which still requires the caller's normal
    topology/top check. A catalog cannot become connectivity IR by changing
    its scope, and ordinary topology cannot be mixed into a catalog artifact.
    """
    search = scope.semantic_search
    raw = dict(ir.metadata).get('source_catalog_v1')
    if search is None or search.purpose != 'source_catalog':
        if raw is not None:
            raise ValueError('source catalog metadata requires its capability scope')
        return False
    if (search.source_capabilities not in {('file_declarations',), ('class_inheritance', 'file_declarations')}
            or type(search.source_version) is not int or search.source_version != CATALOG_VERSION):
        raise ValueError('source catalog artifact capability mismatch')
    if ir.definitions or ir.instances or ir.bindings or ir.top_instances:
        raise ValueError('source catalog artifact contains topology facts')
    if raw is None:
        raise ValueError('source catalog artifact has no declaration projection')
    payload = decode_source_catalog(raw, search, checkpoint=checkpoint)
    if scope.top not in payload['semantic_tops']:
        raise ValueError('source catalog frontend did not elaborate the requested top')
    return payload if return_payload else True
