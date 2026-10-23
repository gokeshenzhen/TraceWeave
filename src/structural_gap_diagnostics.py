"""Bounded native gap observations, separate from telemetry and proof coverage."""
from __future__ import annotations

import copy


class GapDiagnostics:
    MAX_REASONS = 64
    MAX_EXAMPLES = 16
    PER_REASON = 2
    DETAILS = {'expression_kind', 'parent_kind', 'operator', 'actual_width', 'port_width'}

    def __init__(self):
        self.rows = {}
        self.example_count = 0
        self.port = None
        self.port_reasons = set()

    def begin_port(self, instance, port):
        self.port = {'instance': instance[:256], 'port': port[:128]}
        self.port_reasons.clear()

    def end_port(self):
        self.port = None
        self.port_reasons.clear()

    def add(self, reason, details=None):
        # Multiple unsupported subexpressions on one port do not inflate the
        # count of affected ports. Global stops are observations, not ports.
        if self.port is not None and reason in self.port_reasons:
            return
        self.port_reasons.add(reason)
        row = self.rows.setdefault(reason, dict(reason=reason, occurrences=0,
            affected_ports=0, examples=[], examples_truncated=False))
        row['occurrences'] += 1
        row['affected_ports'] += int(self.port is not None)
        if self.port is None:
            return
        if len(row['examples']) >= self.PER_REASON or self.example_count >= self.MAX_EXAMPLES:
            row['examples_truncated'] = True
            return
        example = dict(self.port)
        for key, value in (details or {}).items():
            if key not in self.DETAILS:
                continue
            if key.endswith('width') and type(value) is int:
                example[key] = value
            elif not key.endswith('width') and isinstance(value, str):
                example[key] = value[:64]
        row['examples'].append(example)
        self.example_count += 1

    def result(self):
        # Reasons are fixed in the scanner; this is a defensive display bound.
        return copy.deepcopy([self.rows[k] for k in sorted(self.rows)[:self.MAX_REASONS]])

    def merge(self, rows):
        for incoming in rows:
            row = self.rows.setdefault(incoming['reason'], dict(reason=incoming['reason'],
                occurrences=0, affected_ports=0, examples=[], examples_truncated=False))
            row['occurrences'] += incoming['occurrences']
            row['affected_ports'] += incoming['affected_ports']
            row['examples_truncated'] |= incoming['examples_truncated']
            for example in incoming['examples']:
                if len(row['examples']) < self.PER_REASON and self.example_count < self.MAX_EXAMPLES:
                    row['examples'].append(copy.deepcopy(example)); self.example_count += 1
                else:
                    row['examples_truncated'] = True
