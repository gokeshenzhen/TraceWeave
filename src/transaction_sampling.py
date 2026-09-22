"""Request-local, bounded input for the existing column sampler.

This is a read adapter, not a protocol engine or cross-request result cache.
Modern readers cap events before materialization. Legacy readers keep their
existing single-call scratch allocation, reported explicitly in the receipt.
"""
from dataclasses import dataclass
from itertools import chain
import time

from .cancellation import check_cancelled
from .event_pages import PAGE_BYTES, PAGE_EVENTS
from .scope_metadata import file_identity
from .waveform_batch import event_readers, EventPagingUnavailable
from .waveform_selection import SelectionParser


@dataclass(frozen=True)
class TransactionLimits:
    samples: int = 262144
    sample_cells: int = 2_097_152
    signals: int = 128
    events: int = 1_000_000
    decoded_bytes: int = 256 * 1024 * 1024
    pending: int = 16384
    early_data: int = 16384
    state_bytes: int = 32 * 1024 * 1024
    result_bytes: int = 16 * 1024 * 1024
    timeout_sec: float = 30.0


class TransactionBudgetExceeded(Exception):
    pass


class TransactionBudget:
    def __init__(self, limits=None):
        self.limits = limits or TransactionLimits()
        self.started = time.monotonic()
        self.events = self.events_read = self.value_bytes_read = self.decoded_bytes = self.output_bytes = self.pages = 0
        self.read_calls = self.legacy_reads = 0
        self.value_reuse_hits = self.value_reuse_misses = 0
        self.value_table_peak_entries = self.value_table_peak_bytes = 0
        self.stop_reason = None

    def check(self):
        check_cancelled()
        if time.monotonic() - self.started > self.limits.timeout_sec:
            self.stop_reason = "timeout"
            raise TransactionBudgetExceeded("timeout")

    def admit(self, value):
        self.check()
        size = 512 + 4 * len(value or "")
        reason = ("event_budget" if self.events >= self.limits.events else
                  "decoded_byte_budget" if self.decoded_bytes + size > self.limits.decoded_bytes else None)
        if reason:
            self.stop_reason = reason
            return False
        self.events += 1
        self.decoded_bytes += size
        return True


class _ReadAdapter:
    def __init__(self, parser, budget):
        self.parser, self.budget = parser, budget

    def __getattr__(self, name):
        return getattr(self.parser, name)

    def get_value_at_time(self, path, time_ps):
        # A missing predecessor is unknown, including within an active FSDB
        # group. Never sample an unrelated later event as a prefix seed.
        return {"value": None}

    def get_transitions(self, path, start_ps=0, end_ps=-1):
        from .vcd_parser import _enrich_value
        b = self.budget
        b.check()
        rows, previous = [], None
        values, value_bytes = {}, 0
        def normalize(raw):
            nonlocal value_bytes
            if raw in values:
                b.value_reuse_hits += 1
                return values[raw]
            b.value_reuse_misses += 1
            value = _enrich_value(raw)
            size = 512 + 4 * len(raw or "")
            if len(values) < 256 and value_bytes + size <= PAGE_BYTES:
                values[raw] = value
                value_bytes += size
                b.value_table_peak_entries = max(b.value_table_peak_entries, len(values))
                b.value_table_peak_bytes = max(b.value_table_peak_bytes, value_bytes)
            return value
        result = dict(transitions=rows, predecessor=None, truncated=False)
        if b.stop_reason:
            return {**result, "truncated": True}
        b.read_calls += 1
        try:
            with event_readers([(self.parser, path)], start_ps, end_ps,
                               max_events=PAGE_EVENTS, max_bytes=PAGE_BYTES) as readers:
                reader = readers[0]
                while True:
                    b.check()
                    page = reader.read_page()
                    b.check()
                    b.pages += 1
                    b.output_bytes += page.output_bytes
                    b.events_read += len(page.events) + bool(page.predecessor)
                    b.value_bytes_read += sum(len(e.value or "") for e in page.events)
                    if page.predecessor:
                        b.value_bytes_read += len(page.predecessor.value or "")
                    for event in chain((page.predecessor,) if page.predecessor else (), page.events):
                        if not b.admit(event.value):
                            result["truncated"] = True
                            break
                        row = {"time_ps": event.time_ps, "time_fs": event.time_fs,
                               "value": normalize(event.value)}
                        if event is page.predecessor:
                            previous = row
                        else:
                            rows.append(row)
                    if result["truncated"] or page.truncated or page.complete:
                        result["truncated"] |= page.truncated
                        break
        except EventPagingUnavailable:
            # An old native ABI has one bounded native output buffer. Its
            # decoded single-read scratch is not governed by our page budget.
            b.legacy_reads += 1
            raw = self.parser.get_transitions(path, start_ps=start_ps, end_ps=end_ps)
            b.events_read += len(raw.get("transitions", ())) + bool(raw.get("predecessor"))
            b.value_bytes_read += sum(len((row.get("value") or {}).get("bin") or "") for row in
                chain((raw["predecessor"],) if raw.get("predecessor") else (), raw.get("transitions", ())))
            scale = getattr(self.parser, "get_header", lambda: {})().get("scale_fs_per_tick")
            if isinstance(scale, int) and scale < 1000:
                return {"transitions": [], "predecessor": None, "truncated": True,
                        "sampling_gaps": ["legacy_sub_ps_order_unavailable"]}
            for row in chain((raw["predecessor"],) if raw.get("predecessor") else (), raw.get("transitions", ())):
                if not b.admit((row.get("value") or {}).get("bin")):
                    result["truncated"] = True
                    break
                if row is raw.get("predecessor"):
                    previous = row
                else:
                    rows.append(row)
            result["truncated"] |= bool(raw.get("truncated") or raw.get("transition_count_is_lower_bound"))
        result["predecessor"] = previous
        return result


    _sampling_transitions = get_transitions


def bounded_parser(parser, budget):
    if isinstance(parser, SelectionParser):
        adapter = SelectionParser(_ReadAdapter(parser.parser, budget))
        adapter.projections = dict(parser.projections)
        adapter._declarations = dict(parser._declarations)
        return adapter
    return _ReadAdapter(parser, budget)


def sample_limit(parser, paths, budget, requested=None):
    limit = min(budget.limits.samples, requested or budget.limits.samples,
                budget.limits.sample_cells // max(1, len(paths)))
    if isinstance(parser, SelectionParser):
        from .waveform_selection import MAX_PROJECTED_BITS, MAX_PROJECTED_CELLS
        projected = [parser.projections[p] for p in paths if p in parser.projections]
        limit = min(limit, MAX_PROJECTED_CELLS // max(1, len(projected)),
                    MAX_PROJECTED_BITS // max(1, sum(p.selection.width for p in projected)))
    return max(0, limit)


def limit_to_budget_prefix(sampled, budget):
    """A read-budget stop admits only the common proven column prefix.

    The sampler masks unfinished time groups with None. In particular, exhausting
    a budget before reading the last control cannot count the earlier columns
    over the whole window. Native truncation without a budget stop retains its
    existing explicit unknown-tail semantics for the TL-UL inspector.
    """
    if not budget.stop_reason:
        return sampled
    end = len(sampled.get("edge_times", ()))
    for path in sampled.get("transition_signals_truncated", ()):
        column = sampled.get("signal_columns", {}).get(path)
        if column is not None:
            end = min(end, next((i for i, value in enumerate(column) if value is None), len(column)))
    return {**sampled, "edge_times": sampled.get("edge_times", [])[:end],
            "signal_columns": {p: values[:end] for p, values in sampled.get("signal_columns", {}).items()}}


def sample_identity(parser, wave, clock, signals, start, end, edge):
    from .observation_session import selection_identity
    owner = parser.parser if isinstance(parser, SelectionParser) else parser
    return (id(owner), getattr(owner, "_scope_epoch", None), file_identity(wave),
            selection_identity(parser, clock), tuple((p, selection_identity(parser, p)) for p in sorted(set(signals))),
            int(start), int(end), edge, 0, "strict_before_edge_v1")


@dataclass(frozen=True)
class TransactionSampleInput:
    """Typed, source-bound reuse from TL-UL; never accept an unproven row dict.

    Columns are owned by this request and only read. The two optional byte
    vectors encode correlation boundaries, without changing sampled signals.
    """
    sampled: dict
    identity: tuple
    roles: tuple
    boundaries: bytes
    uncertain: bytes
    budget: TransactionBudget
    sampling_ms: float = 0.0

    def validate(self, parser, wave, clock, signals, start, end, edge, roles):
        self.budget.check()
        if (self.identity != sample_identity(parser, wave, clock, signals, start, end, edge)
                or self.roles != roles or self.sampled.get("sample_phase") != "before"
                or self.sampled.get("sample_offset_ps") != 0):
            raise ValueError("incompatible transaction samples: source, selection, roles or time semantics changed")
        n = len(self.sampled.get("edge_times", ()))
        if len(self.boundaries) != n or len(self.uncertain) != n:
            raise ValueError("incomplete transaction correlation boundary vectors")


def role_identity(resolved, req_fields, cmp_fields, data_fields, active_high, reset_active_low):
    return (tuple(sorted(resolved.items())), tuple(req_fields), tuple(cmp_fields), tuple(data_fields),
            bool(active_high), bool(reset_active_low), "transaction_correlation_v1")
