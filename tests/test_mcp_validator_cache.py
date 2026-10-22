"""Retain SDK JSON Schema semantics while reusing schema validation work."""
import json

import jsonschema
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import server
from src.mcp_validation import ToolInputValidators


SCHEMA = {
    '$defs': {'count': {'type': 'integer', 'minimum': 1}},
    'type': 'object', 'required': ['value'], 'additionalProperties': False,
    'properties': {'value': {'anyOf': [{'$ref': '#/$defs/count'}, {'type': 'string', 'pattern': '^ok'}]},
                   'flags': {'type': 'array', 'items': {'type': 'boolean'}, 'maxItems': 2}},
}


@pytest.mark.parametrize('arguments', [
    {'value': 1}, {'value': 'okay', 'flags': [True, False]}, {}, {'value': 0},
    {'value': True}, {'value': 'bad'}, {'value': []}, {'value': 1, 'unexpected': 3},
    {'value': 1, 'flags': [1]}, {'value': 1, 'flags': [True] * 3},
])
def test_cached_and_sdk_validation_choose_identical_errors(arguments):
    cache = ToolInputValidators()
    # Include hits as well as the first schema-checking call.
    for _ in range(2):
        try:
            jsonschema.validate(arguments, SCHEMA)
        except jsonschema.ValidationError as expected:
            with pytest.raises(jsonschema.ValidationError) as actual:
                cache.validate('test', SCHEMA, arguments)
            assert actual.value.message == expected.message
            assert list(actual.value.absolute_path) == list(expected.absolute_path)
            assert list(actual.value.absolute_schema_path) == list(expected.absolute_schema_path)
        else:
            cache.validate('test', SCHEMA, arguments)


def test_in_place_schema_change_cannot_reuse_old_rules(monkeypatch):
    schema = {'type': 'integer', 'minimum': 1}
    cls = jsonschema.validators.validator_for(schema)
    original = cls.check_schema
    checked = []
    def check(value):
        checked.append(json.dumps(value, sort_keys=True))
        return original(value)
    monkeypatch.setattr(cls, 'check_schema', staticmethod(check))
    cache = ToolInputValidators()
    cache.validate('tool', schema, 1)
    cache.validate('tool', schema, 2)
    schema['minimum'] = 3
    with pytest.raises(jsonschema.ValidationError):
        cache.validate('tool', schema, 2)
    cache.validate('tool', schema, 3)
    assert len(checked) == 2
    schema['type'] = 'not-a-json-schema-type'
    with pytest.raises(jsonschema.SchemaError):
        cache.validate('tool', schema, 3)


def test_lru_bounds_and_oversized_bypass_preserve_validation():
    schema = {'type': 'integer'}
    size = len(json.dumps(schema, sort_keys=True, separators=(',', ':')).encode())
    cache = ToolInputValidators(max_entries=2, max_bytes=2 * size)
    for name in ['a', 'b', 'a', 'c']:
        cache.validate(name, schema, 1)
    assert list(cache.entries) == ['a', 'c'] and cache.bytes == 2 * size
    huge = {'type': 'string', 'description': 'x' * (3 * size)}
    cache.validate('huge', huge, 'private-request-value')
    assert 'huge' not in cache.entries and 'private-request-value' not in repr(cache.entries)
    with pytest.raises(jsonschema.ValidationError):
        cache.validate('huge', huge, 1)
    assert cache.bytes <= cache.max_bytes


@pytest.mark.anyio
async def test_repeated_mcp_requests_are_each_validated_before_dispatch(monkeypatch):
    dispatched = []
    async def dispatch(name, arguments):
        dispatched.append((name, arguments))
        return {'ok': True}
    monkeypatch.setattr(server, '_dispatch', dispatch)
    async with create_connected_server_and_client_session(server.app) as session:
        for _ in range(2):
            valid = await session.call_tool('find_signal_loads', {'signal_path': 'top.a', 'compile_log': '/unused/log'})
            invalid = await session.call_tool('find_signal_loads', {'signal_path': ['bad'], 'compile_log': '/unused/log'})
            assert not valid.isError
            assert invalid.isError and invalid.content[0].text.startswith('Input validation error:')
    assert len(dispatched) == 2
