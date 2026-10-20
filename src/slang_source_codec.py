"""Fail-closed primitive codec for the worker's source-catalog capability."""
from __future__ import annotations

import json
import re

from .hierarchy_source_symbols import SourceSymbol
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
    _fields(payload, "version capabilities source_files declaration_domain usage_domain member_visits "
                     "scope_visits limited stop_reason limits files")
    if (type(payload['version']) is not int or payload['version'] != CATALOG_VERSION
            or payload['version'] != search.source_version
            or payload['source_files'] != list(search.source_files)
            or payload['capabilities'] != list(search.source_capabilities)
            or payload['declaration_domain'] != DECLARATION_DOMAIN
            or payload['usage_domain'] != USAGE_DOMAIN):
        raise ValueError("source catalog capability or domain mismatch")
    _fields(payload['limits'], "members scopes depth")
    expected = dict(members=search.max_members, scopes=search.source_max_scopes, depth=search.source_max_depth)
    if any(type(payload['limits'][key]) is not int for key in expected) or payload['limits'] != expected:
        raise ValueError("source catalog limits mismatch")
    _count(payload['member_visits'], search.max_members)
    _count(payload['scope_visits'], search.source_max_scopes)
    if payload['scope_visits'] > payload['member_visits']:
        raise ValueError('source catalog scope accounting invalid')
    if type(payload['limited']) is not bool:
        raise ValueError("source catalog limit status invalid")
    reason = payload['stop_reason']
    if reason not in {None, 'source_catalog_visit_limit', 'source_catalog_scope_limit', 'source_catalog_depth_limit'}:
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
    return payload
