"""Response-local lossless driver encoding. No analysis, I/O, handles or caches.

Only fixed typed fields are interned. Optimization exhaustion leaves full values
inline. Decoder limits bound allocation, not analysis or client token budgets.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json

from .cancellation import check_cancelled
from .schemas import DriverCompactEvidenceResult, ExplainDriverResult

FORMAT = 'traceweave.driver.compact.v1'
SECTIONS = ('driver_chain', 'bit_provenance')
TABLES = ('statement_semantics', 'statement_evidence', 'locations', 'evidence_indices')
_JSON = json.JSONEncoder(ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class EncodeLimits:
    memo_bytes: int = 16 * 1024 * 1024
    memo_entries: int = 8192
    table_entries: int = 8192


@dataclass(frozen=True)
class DecodeLimits:
    max_bytes: int = 64 * 1024 * 1024
    max_nodes: int = 2_000_000
    max_rows: int = 200_000
    max_depth: int = 128


class DriverCodecLimitError(ValueError):
    """An input/expanded result exceeds an explicit decoder resource limit."""


def _json(value):
    return _JSON.encode(value)


def _key(value, remaining):
    """Stop constructing an optimization key as soon as its budget is spent."""
    parts = bytearray()
    for i, chunk in enumerate(_JSON.iterencode(value)):
        if i % 256 == 0:
            check_cancelled()
        part = chunk.encode('utf-8')
        if len(parts) + len(part) > remaining:
            return None
        parts.extend(part)
    return bytes(parts)


@dataclass
class _Candidate:
    value: object
    size: int
    uses: list = field(default_factory=list)
    ref: int | None = None


class _Memo:
    def __init__(self, limits):
        self.limits = limits
        self.remaining = limits.memo_bytes
        self.count = 0
        self.tables = {name: {} for name in TABLES}

    def intern(self, table, value, *, created_bytes=0):
        key = _key(value, max(0, self.remaining - created_bytes - 128))
        if key is None:
            return None
        existing = self.tables[table].get(key)
        if existing is not None:
            return existing
        cost = len(key) + created_bytes + 128
        if self.count >= self.limits.memo_entries or cost > self.remaining:
            return None
        entry = _Candidate(value, len(key))
        self.tables[table][key] = entry
        self.count += 1
        self.remaining -= cost
        return entry

    def use(self, candidate, section, index, name):
        if candidate is not None and self.remaining >= 96:
            candidate.uses.append((section, index, name))
            self.remaining -= 96


def _locations(evidence, remaining):
    # Newly allocated row/list overhead is charged separately from keys. Large
    # singletons never demand a giant key merely to discover they cannot fit.
    if len(evidence) <= 1 or len(evidence) * 160 > remaining:
        return None
    rows, indices = [], []
    for i, item in enumerate(evidence):
        if i % 256 == 0:
            check_cancelled()
        if (type(item) is not dict or not {'source_line', 'evidence_index'} <= item.keys()
                or item.keys() - {'source_line', 'source_column', 'evidence_index'}
                or type(item['source_line']) is not int or item['source_line'] < 1
                or type(item['evidence_index']) is not int or item['evidence_index'] < 0
                or ('source_column' in item and (type(item['source_column']) is not int or item['source_column'] < 0))):
            # In particular, explicit null cannot mean absence in inline input.
            return None
        rows.append([item['source_line'], item.get('source_column')])
        indices.append(item['evidence_index'])
    return {'columns': ['source_line', 'source_column'], 'rows': rows}, indices


def encode_driver_result(result: ExplainDriverResult | dict, *, limits: EncodeLimits | None = None) -> dict:
    """Project one public result; never call SchemaModel.get() or repeat export.

    Dict inputs are already-public JSON: validation does not insert defaults or
    erase nulls. Models are exported once with the full serializer's rules.
    Returned values are read-only projections until serialization.
    """
    check_cancelled()
    limits = limits or EncodeLimits()
    if min(limits.memo_bytes, limits.memo_entries, limits.table_entries) < 0:
        raise ValueError('driver_compact_encode_limits_invalid')
    if isinstance(result, ExplainDriverResult):
        public = result.model_dump(mode='json', exclude_none=True)
    elif type(result) is dict:
        ExplainDriverResult.model_validate(result, strict=True)
        public = result
    else:
        raise ValueError('driver_compact_requires_explain_driver_result')
    memo = _Memo(limits)
    # Only candidate storage is bounded. The input and complete output naturally
    # scale with retained evidence; these limits never drop a fact.
    for section in SECTIONS:
        for index, group in enumerate(public.get(section) or ()):
            check_cancelled()
            sem = group.get('statement_semantics')
            if isinstance(sem, dict):
                memo.use(memo.intern('statement_semantics', sem), section, index, 'statement_semantics')
            evidence = group.get('statement_evidence')
            if not isinstance(evidence, list) or group.get('statement_count') != len(evidence):
                continue
            split = _locations(evidence, memo.remaining)
            if split is None:
                continue
            locations, indices = split
            loc = memo.intern('locations', locations, created_bytes=128 * len(evidence))
            ids = memo.intern('evidence_indices', indices, created_bytes=16 * len(evidence))
            if loc is None or ids is None:
                continue
            # Exact list key preserves both the locations and this array's own
            # sparse/repeated/reversed evidence indices. No writer ID is a key.
            pair = memo.intern('statement_evidence', evidence)
            if pair is not None:
                pair.value = (loc, ids)
                memo.use(pair, section, index, 'statement_evidence')
    tables = {name: [] for name in TABLES}
    selected = []
    table_count = 0

    def cost(name, value):
        # Include first table-name overhead conservatively (the empty table is
        # present even on no-sharing responses), plus actual ID widths/commas.
        return len(_json(value).encode()) + (1 if tables[name] else len(_json(name)) + 3)

    for name in ('statement_semantics', 'statement_evidence'):
        for candidate in memo.tables[name].values():
            check_cancelled()
            if not candidate.uses:
                continue
            ref = len(tables[name])
            additions = []
            if name == 'statement_semantics':
                value = candidate.value
            else:
                loc, ids = candidate.value
                loc_id = loc.ref if loc.ref is not None else len(tables['locations'])
                ids_id = ids.ref if ids.ref is not None else len(tables['evidence_indices'])
                for table, entry in (('locations', loc), ('evidence_indices', ids)):
                    if entry.ref is None:
                        additions.append((table, entry, entry.value))
                value = dict(locations_ref=loc_id, evidence_indices_ref=ids_id)
            additions.append((name, candidate, value))
            inline_cost = len(candidate.uses) * (len(_json(name)) + 1 + candidate.size)
            ref_cost = len(candidate.uses) * (len(_json(name + '_ref')) + 1 + len(str(ref)))
            if (table_count + len(additions) > limits.table_entries
                    or inline_cost <= ref_cost + sum(cost(t, v) for t, _, v in additions)):
                continue
            for table, entry, item in additions:
                entry.ref = len(tables[table])
                tables[table].append(item)
                table_count += 1
            selected.append(candidate)
    payload = dict(public)
    for section in SECTIONS:
        if isinstance(public.get(section), list):
            payload[section] = []
            for group in public[section]:
                check_cancelled()
                payload[section].append(dict(group))
    for candidate in selected:
        for section, index, name in candidate.uses:
            check_cancelled()
            group = payload[section][index]
            del group[name]
            group[name + '_ref'] = candidate.ref
    check_cancelled()
    return dict(format=FORMAT, tool='explain_signal_driver', result=payload, evidence_tables=tables)


@dataclass(frozen=True)
class _Size:
    nodes: int = 0
    bytes: int = 0

    def __add__(self, other):
        return _Size(self.nodes + other.nodes, self.bytes + other.bytes)


def _check(size, limits):
    if size.nodes > limits.max_nodes or size.bytes > limits.max_bytes:
        raise DriverCodecLimitError('driver_compact_decode_size_limit')


def _overhead(value):
    if type(value) is dict:
        if any(type(key) is not str for key in value):
            raise ValueError('driver_compact_json_key_invalid')
        return _Size(1, 2 + max(0, len(value) - 1) + sum(len(_json(k).encode()) + 1 for k in value))
    if type(value) is list:
        return _Size(1, 2 + max(0, len(value) - 1))
    if value is None or type(value) in (str, int, float, bool):
        return _Size(1, len(_json(value).encode()))
    raise ValueError('driver_compact_json_value_invalid')


class _Budget:
    def __init__(self, limits):
        self.limits = limits
        self.size = _Size()
        self.calls = 0

    def add(self, size, depth):
        self.calls += 1
        if self.calls % 256 == 1:
            check_cancelled()
        if depth > self.limits.max_depth:
            raise DriverCodecLimitError('driver_compact_decode_depth_limit')
        self.size += size
        _check(self.size, self.limits)


def _walk(value, budget, depth=0, *, copy=False):
    budget.add(_overhead(value), depth)
    if type(value) is dict:
        if copy:
            return {k: _walk(v, budget, depth + 1, copy=True) for k, v in value.items()}
        for v in value.values():
            _walk(v, budget, depth + 1)
    elif type(value) is list:
        if copy:
            return [_walk(v, budget, depth + 1, copy=True) for v in value]
        for v in value:
            _walk(v, budget, depth + 1)
    else:
        return value


def _measure(value, limits):
    budget = _Budget(limits)
    _walk(value, budget)
    return budget.size


def parse_compact_json(text: str, *, limits: DecodeLimits | None = None):
    """Raw-text boundary: duplicate keys/nonfinite numbers are rejected here.

    A caller-supplied dict has already lost duplicate-key information.
    """
    limits = limits or DecodeLimits()
    if len(text.encode('utf-8')) > limits.max_bytes:
        raise DriverCodecLimitError('driver_compact_decode_input_limit')

    def pairs(items):
        out = {}
        for k, v in items:
            if k in out:
                raise ValueError('compact_duplicate_json_key')
            out[k] = v
        return out

    def nonfinite(_):
        raise ValueError('compact_nonfinite_json_number')

    check_cancelled()
    try:
        result = json.loads(text, object_pairs_hook=pairs, parse_constant=nonfinite)
    except RecursionError as exc:
        raise DriverCodecLimitError('driver_compact_decode_depth_limit') from exc
    check_cancelled()
    return result


def _evidence_rows(pair, tables):
    locations = tables['locations'][pair['locations_ref']]['rows']
    indices = tables['evidence_indices'][pair['evidence_indices_ref']]
    for i, (location, index) in enumerate(zip(locations, indices)):
        if i % 256 == 0:
            check_cancelled()
        row = dict(source_line=location[0], evidence_index=index)
        if location[1] is not None:
            row['source_column'] = location[1]
        yield row


def expand_driver_result(payload: dict, *, limits: DecodeLimits | None = None) -> dict:
    """Validate the fixed DAG and preflight expansion before allocating output."""
    check_cancelled()
    limits = limits or DecodeLimits()
    _measure(payload, limits)  # bound untrusted input before schema allocations
    DriverCompactEvidenceResult.model_validate(payload, strict=True)
    result, tables = payload['result'], payload['evidence_tables']
    sem_sizes = [_measure(s, limits) for s in tables['statement_semantics']]
    evidence_sizes, evidence_counts = [], []
    for pair in tables['statement_evidence']:
        size, count = _Size(1, 2), 0
        for row in _evidence_rows(pair, tables):
            size += _measure(row, limits) + _Size(0, bool(count))
            count += 1
            _check(size, limits)
            if count > limits.max_rows:
                raise DriverCodecLimitError('driver_compact_decode_row_limit')
        evidence_sizes.append(size)
        evidence_counts.append(count)
    predicted = _measure(result, limits)
    total_rows = 0
    for section in SECTIONS:
        for group in result.get(section) or ():
            check_cancelled()
            for name, sizes in (('statement_semantics', sem_sizes), ('statement_evidence', evidence_sizes)):
                ref_key = name + '_ref'
                if ref_key in group:
                    ref = group[ref_key]
                    old = _overhead(ref)
                    new = sizes[ref]
                    predicted += _Size(new.nodes - old.nodes, new.bytes - old.bytes - 4)  # remove _ref
                    _check(predicted, limits)
            total_rows += (evidence_counts[group['statement_evidence_ref']]
                           if 'statement_evidence_ref' in group else len(group.get('statement_evidence') or ()))
            if total_rows > limits.max_rows:
                raise DriverCodecLimitError('driver_compact_decode_row_limit')
    # Actual allocation counts use the same accounting, independently of the
    # preflight. Repeated references create independent mutable values.
    budget = _Budget(limits)
    budget.add(_overhead(result), 0)
    out = {}
    for key, value in result.items():
        if key not in SECTIONS or value is None:
            out[key] = _walk(value, budget, 1, copy=True)
            continue
        budget.add(_overhead(value), 1)
        groups = []
        for group in value:
            check_cancelled()
            # Ref and inline keys have the same field count; fix key byte cost.
            overhead = _overhead(group)
            ref_count = sum(name + '_ref' in group for name in ('statement_semantics', 'statement_evidence'))
            budget.add(_Size(overhead.nodes, overhead.bytes - 4 * ref_count), 2)
            row = {}
            for field, item in group.items():
                if field == 'statement_semantics_ref':
                    row['statement_semantics'] = _walk(tables['statement_semantics'][item], budget, 3, copy=True)
                elif field == 'statement_evidence_ref':
                    count = evidence_counts[item]
                    budget.add(_Size(1, 2 + max(0, count - 1)), 3)
                    row['statement_evidence'] = [_walk(r, budget, 4, copy=True)
                        for r in _evidence_rows(tables['statement_evidence'][item], tables)]
                else:
                    row[field] = _walk(item, budget, 3, copy=True)
            groups.append(row)
        out[key] = groups
    check_cancelled()
    return out
