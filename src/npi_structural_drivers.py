"""Copied, bounded direct-driver evidence within the existing native owner.

Language enumerates declarations. Netlist trace_driver/trace_load resolve the
actual bit aliases; Language-level Driver iteration alone is not authoritative
for slice nets. SDK calls remain synchronous and checkpoint on return.
"""
from __future__ import annotations

import hashlib
import re
from contextlib import contextmanager

from .cancellation import OperationCancelled
from .hierarchy_source_ir import HierarchyBudgetExceeded
from .npi_hierarchy_source import NpiHierarchyUnavailable

MAX_ENDPOINTS = 256
_UNRESOLVED = object()
_TERMINAL = (OperationCancelled, HierarchyBudgetExceeded, NpiHierarchyUnavailable, TimeoutError)


def writer_identity(raw):
    # A synthesized process can expose one pin per branch/assignment. Count
    # its enclosing process once, while separate output ports stay separate.
    process = re.match(r'^(.*?:Always\d+)(?:#|:)', raw)
    if process:
        return process.group(1)
    # Different output bits can be connected to the same parent bit. Their
    # selectors are source identities, not redundant display decorations.
    return raw


class NpiBitDrivers:
    def __init__(self, provider, backend):
        self.p, self.backend = provider, backend
        self.netlist = backend._npi_modules[1]
        self._signal = None

    @contextmanager
    def signal(self, path, left, right):
        """Keep one verified vector handle inside this declaration's owner span."""
        self._signal = dict(path=path, left=left, right=right, net=_UNRESOLVED)
        try:
            yield
        finally:
            self._signal = None

    def net(self, path):
        signal = self._signal
        if signal is not None and signal['left'] != signal['right']:
            prefix = signal['path'] + '['
            suffix = path[len(prefix):-1] if path.startswith(prefix) and path.endswith(']') else ''
            if re.fullmatch(r'-?\d+', suffix):
                bit = int(suffix)
                left, right = signal['left'], signal['right']
                if min(left, right) <= bit <= max(left, right):
                    if signal['net'] is _UNRESOLVED:
                        signal['net'] = None
                        try:
                            parent = self.call(self.netlist.get_net, signal['path'])
                            if parent is not None and callable(getattr(parent, 'index', None)):
                                name = self.call(parent.full_name)
                                bounds = (self.call(parent.left), self.call(parent.right))
                                size = self.call(parent.size)
                                if (name in {signal['path'], f"{signal['path']}[{left}:{right}]"}
                                        and all(type(value) is int for value in (*bounds, size))
                                        and bounds == (left, right) and size == abs(left-right)+1):
                                    signal['net'] = parent
                        except _TERMINAL:
                            raise
                        except Exception:
                            pass
                    if signal['net'] is not None:
                        try:
                            indexed = self.call(signal['net'].index, bit)
                            if indexed is not None:
                                return indexed
                        except _TERMINAL:
                            raise
                        except Exception:
                            pass
        return self.call(self.netlist.get_net, path)

    def call(self, fn, *args):
        self.p.check()
        self.p.budget.call()
        result = fn(*args)
        self.p.check()
        return result

    def read(self, path, *, categories):
        from .verdi_npi_backend import driver_is_load_alias
        gaps = set()
        try:
            net = self.net(path)
            if net is None:
                return [], False, False, {'npi_driver_net_unresolved'}
            original = self.call(net.driver_list)
            if not isinstance(original, list):
                return [], False, False, {'npi_driver_list_unavailable'}
            if len(original) == 1:
                # No multiple-driver positive or empty-driver negative can be
                # established here; avoid large load inventories for clocks.
                return [], True, False, set()
            if not original and 'undriven_signal' not in categories:
                return [], True, False, set()
            loads = self.call(net.load_list)
            if not isinstance(loads, list) or len(loads) > MAX_ENDPOINTS:
                return [], False, False, {'npi_driver_load_inventory_limit'}
            load_names = [self.call(h.full_name) for h in loads]
            if any(not isinstance(n, str) or not n or len(n)>16384 for n in load_names):
                return [], False, False, {'npi_driver_load_identity_unavailable'}
            if not original:
                return [], True, bool(loads), set()
            complete = len(original) <= MAX_ENDPOINTS
            if not complete:
                gaps.add('npi_driver_endpoint_limit')
            rows = {}
            for handle in original[:MAX_ENDPOINTS]:
                raw = self.call(handle.full_name)
                if not isinstance(raw, str) or not raw or len(raw)>16384:
                    gaps.add('npi_driver_identity_unavailable'); complete = False
                    continue
                if driver_is_load_alias(raw, load_names):
                    gaps.add('npi_driver_load_alias'); complete = False
                    continue
                row = self.call(self.backend._format_driver, handle)
                if row is None or row.get('driver_kind') in {'initial', 'unknown', None}:
                    gaps.add('npi_driver_kind_unmodeled'); complete = False
                    continue
                identity = writer_identity(raw)
                key = hashlib.sha256(identity.encode()).hexdigest()
                if key in rows and rows[key]['signal_path'] != raw:
                    # Same-process pins are not independent writers. Their
                    # logical-bit equivalence is not proved by the process
                    # prefix alone, so do not claim exhaustive classification.
                    gaps.add('npi_driver_process_mapping_unverified'); complete = False
                rows[key] = dict(driver_id=key, signal_path=raw, kind=row['driver_kind'],
                    source=(dict(file=row['source_file'],line=row['source_line'])
                            if row.get('source_file') and row.get('source_line') else None))
            return list(rows.values()), complete, bool(loads), gaps
        except _TERMINAL:
            raise
        except Exception:
            return [], False, False, {'npi_driver_query_failed'}
