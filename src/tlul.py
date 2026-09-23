"""A/D role adaptation over the existing handshake and transaction engines."""
from __future__ import annotations

import time

from .cancellation import check_cancelled
from .cycle_query import sample_signals_on_edges, iter_sample_rows
from .transaction_sampling import (TransactionBudget, TransactionBudgetExceeded,
    TransactionSampleInput, bounded_parser, role_identity, sample_identity, sample_limit, limit_to_budget_prefix)
from .txn_reconstruct import reconstruct_transactions
from .verify_condition import inspect_handshake, _hs_truth
from .waveform_selection import prepare_selections, attach_selections

REQUIRED_FIELDS = ("a_valid", "a_ready", "a_source", "d_valid", "d_ready", "d_source")
OPTIONAL_FIELDS = ("a_opcode", "a_param", "a_size", "a_address", "a_mask", "a_data", "a_user",
                   "d_opcode", "d_param", "d_size", "d_sink", "d_data", "d_user", "d_error")


def inspect_tlul(*, get_parser, wave_path, clock, fields=None, reset=None,
                 reset_active_low=True, start_ps=0, end_ps=-1, edge="posedge",
                 max_cycles=65536, max_transactions=256, max_wait_cycles=16, _limits=None):
    """Explicit mappings only; bit widths come from declarations, never a profile.

    No carry-in state is invented. Pairing before a known reset is window-local;
    unknown reset / controls / IDs split correlation history conservatively.
    """
    from .schemas import TlulFields
    fields = TlulFields.model_validate(fields or {}).model_dump(exclude_none=True)
    missing = [name for name in REQUIRED_FIELDS if name not in fields]
    result = {"wave_path": wave_path, "clock": clock, "start_ps": start_ps, "end_ps": end_ps,
              "sampling_phase": "before",
              "coverage_status": "zero_coverage", "checks": [], "gaps": [],
              "required_fields": missing, "unmapped_fields": [f for f in OPTIONAL_FIELDS if f not in fields],
              "mapping_status": "explicit" if not missing else "mapping_required",
              "channels": {}, "transactions": None, "field_facts": {},
              "sample_count": 0, "sample_limit_reached": False,
              "transition_data_truncated": False, "unknown_fields": {},
              "reset_cycles": 0, "unknown_reset_cycles": 0, "unknown_control_cycles": 0,
              "correlation_breaks": 0, "carry_in": "unknown", "tail": "window_boundary",
              "warnings": ["Facts cover the mapped A/D channels; opcode legality, response size/opcode "
                           "agreement, source uniqueness, integrity coding and mask/alignment are not checked."]}
    if missing:
        result["gaps"] = ["explicit_field_mapping_required"]
        return result
    if not 1 <= max_cycles <= 65536 or not 1 <= max_transactions <= 65536:
        raise ValueError("max_cycles and max_transactions must be between 1 and 65536")
    if start_ps < 0 or (end_ps != -1 and end_ps < start_ps):
        raise ValueError("invalid time window")
    parser, mapped = prepare_selections(get_parser(wave_path), {**fields, "clock": clock, "reset": reset})
    clock, reset = mapped.pop("clock"), mapped.pop("reset")
    for name, path in {**mapped, "clock": clock, **({"reset": reset} if reset else {})}.items():
        width = parser.get_signal_width(path)
        if name in ("a_valid", "a_ready", "d_valid", "d_ready", "clock", "reset", "d_error") and width != 1:
            if path in getattr(parser, 'expressions', {}):
                from .expression_errors import ExpressionError
                raise ExpressionError('expression_control_width_invalid', parameter=name,
                    message=f"{name} must select one bit")
            raise ValueError(f"{name} must select one bit")
    for left, right in (("a_source", "d_source"), ("a_size", "d_size")):
        if left in mapped and right in mapped and parser.get_signal_width(mapped[left]) != parser.get_signal_width(mapped[right]):
            raise ValueError(f"{left} and {right} widths disagree")
    budget = TransactionBudget(_limits)
    all_signals = list(dict.fromkeys([*mapped.values(), *([reset] if reset else [])]))
    identity = sample_identity(parser, wave_path, clock, all_signals, start_ps, end_ps, edge)
    sampling_started = time.perf_counter()
    try:
        sampled = sample_signals_on_edges(bounded_parser(parser, budget), clock, all_signals,
            start_ps=start_ps, end_ps=end_ps, edge=edge, compact=True,
            sample_offset_ps=0, sample_phase="before",
            max_edges=sample_limit(parser, all_signals, budget, max_cycles), safe_prefix_only=True)
        sampled = limit_to_budget_prefix(sampled, budget)
    except TransactionBudgetExceeded:
        result.update(coverage_status="partial", gaps=[budget.stop_reason], tail=budget.stop_reason)
        return attach_selections(result, parser)
    sampling_ms = (time.perf_counter() - sampling_started) * 1000
    sample_count = len(sampled["edge_times"])
    result.update(sample_count=sample_count, clock=clock,
                  sample_limit_reached=sampled["sample_limit_reached"],
                  transition_data_truncated=sampled["transition_data_truncated"])
    if not sample_count or sampled["signal_errors"]:
        result["gaps"] = ["no_clock_edges" if not sample_count else "signal_read_failed"]
        if budget.stop_reason:
            result.update(coverage_status="partial", gaps=[budget.stop_reason], tail=budget.stop_reason)
        elif sampled["sample_limit_reached"] or sampled["transition_data_truncated"]:
            reason = "sample_limit" if sampled["sample_limit_reached"] else "transition_data_truncated"
            result.update(coverage_status="partial", gaps=[reason], tail=reason)
        result["gaps"].extend(sampled.get("sampling_gaps", []))
        return attach_selections(result, parser)

    inactive = {}
    boundaries, uncertain_boundaries = bytearray(), bytearray()
    saw_reset = False
    active_before_reset = False
    control_unknown = 0
    unknown_ids = 0
    accepted = {"a": 0, "d": 0}
    field_facts = {name: {"known_accepted": 0, "unknown_accepted": 0, "nonzero_accepted": 0}
                   for name in OPTIONAL_FIELDS if name in mapped}
    unknown_fields = {name: 0 for name in mapped}
    for i, (_, sig) in enumerate(iter_sample_rows(sampled)):
        check_cancelled()
        rst = _hs_truth(sig.get(reset), not reset_active_low) if reset else False
        reset_boundary = rst is not False
        if rst is True:
            result["reset_cycles"] += 1
            saw_reset = True
            inactive[i] = "reset"
        elif rst is None:
            result["unknown_reset_cycles"] += 1
            inactive[i] = "unknown_reset"
        elif not saw_reset:
            active_before_reset = True
        uncertain = False
        if not reset_boundary:
            for channel in ("a", "d"):
                v = _hs_truth(sig.get(mapped[channel + "_valid"]), True)
                r = _hs_truth(sig.get(mapped[channel + "_ready"]), True)
                if v is None or (v is True and r is None):
                    uncertain = True
                if v is True:
                    for name, path in mapped.items():
                        if name.startswith(channel + "_") and not name.endswith("_ready"):
                            if (sig.get(path) or {}).get("dec") is None:
                                unknown_fields[name] += 1
                if v is True and r is True:
                    accepted[channel] += 1
                    if (sig.get(mapped[channel + "_source"]) or {}).get("dec") is None:
                        unknown_ids += 1
                        uncertain = True
                    for name, fact in field_facts.items():
                        if name.startswith(channel + "_"):
                            number = (sig.get(mapped[name]) or {}).get("dec")
                            fact["known_accepted" if number is not None else "unknown_accepted"] += 1
                            fact["nonzero_accepted"] += int(number is not None and number != 0)
            if uncertain:
                control_unknown += 1
        # The existing engine already clears its queues on an asserted reset.
        # Insert that same boundary for uncertain acceptance/ID history. These
        # clears are separately reported, never described as observed resets.
        boundary = reset_boundary or uncertain
        boundaries.append(boundary)
        uncertain_boundaries.append(uncertain or rst is None)
        if uncertain or rst is None:
            result["correlation_breaks"] += 1
    result["carry_in"] = "reset_observed_before_activity" if saw_reset and not active_before_reset else "unknown"
    result["unknown_control_cycles"] = control_unknown
    result["unknown_fields"] = {k: v for k, v in unknown_fields.items() if v}
    result["field_facts"] = field_facts
    handshake_samples = {**sampled, "inactive_cycles": inactive}
    for channel in ("a", "d"):
        payload = [path for name, path in mapped.items() if name.startswith(channel + "_") and
                   name not in (channel + "_valid", channel + "_ready")]
        result["channels"][channel] = inspect_handshake(
            get_parser=lambda _: parser, wave_path=wave_path, clock=clock,
            valid=mapped[channel + "_valid"], ready=mapped[channel + "_ready"], payload=payload,
            edge=edge, start_ps=start_ps, end_ps=end_ps, max_wait_cycles=max_wait_cycles,
            _sampled=handshake_samples,
            _compact_sampling=True,
        )
        result["checks"].extend([channel + "_acceptance", channel + "_stall",
                                 channel + "_valid_hold", channel + "_payload_hold_during_stall"])

    req_fields = [mapped[n] for n in OPTIONAL_FIELDS if n.startswith("a_") and n in mapped]
    cmp_fields = [mapped[n] for n in OPTIONAL_FIELDS if n.startswith("d_") and n in mapped]
    roles = dict(req_valid=mapped["a_valid"], req_ready=mapped["a_ready"], req_id=mapped["a_source"],
                 cmp_valid=mapped["d_valid"], cmp_ready=mapped["d_ready"], cmp_id=mapped["d_source"])
    if reset:
        roles["reset"] = reset
    transactions = reconstruct_transactions(
        get_parser=lambda _: parser, wave_path=wave_path, clock=clock, **roles,
        req_fields=req_fields, cmp_fields=cmp_fields,
        reset_active_low=reset_active_low, max_transactions=max_transactions,
        start_ps=start_ps, end_ps=end_ps, edge=edge,
        _sampled=TransactionSampleInput(sampled,
            identity,
            role_identity(roles, req_fields, cmp_fields, [], True, reset_active_low),
            bytes(boundaries), bytes(uncertain_boundaries), budget, sampling_ms),
    )
    # A cut window is evidence of pending work, not a hang verdict.
    transactions["warnings"] = [w for w in transactions["warnings"] if "hang/deadlock" not in w]
    transactions["warnings"].append("Pairing is window-local and split at reset or uncertain history; "
                                    "unmatched endpoints do not prove a missing or illegal response.")
    result["transactions"] = transactions
    result["accepted_a_count"], result["accepted_d_count"] = accepted["a"], accepted["d"]
    result["unknown_id_beats"] = unknown_ids
    result["checks"].extend(["source_id_pairing_within_known_segments", "matched_latency"])
    if field_facts:
        result["checks"].append("accepted_field_capture")
    if reset:
        result["checks"].append("sampled_reset_boundaries")
    gaps = result["gaps"]
    gaps.extend(sampled.get("sampling_gaps", []))
    if budget.stop_reason:
        gaps.append(budget.stop_reason)
        result["tail"] = budget.stop_reason
    if result["carry_in"] == "unknown":
        gaps.append("window_carry_in_unknown")
    if not reset:
        gaps.append("reset_not_mapped")
    if result["correlation_breaks"] or result["unknown_fields"]:
        gaps.append("unknown_fields_or_history")
    if sampled["sample_limit_reached"]:
        gaps.append("sample_limit")
        result["tail"] = "sample_limit"
    if sampled["transition_data_truncated"]:
        gaps.append("transition_data_truncated")
        result["tail"] = "transition_prefix"
    result["coverage_status"] = "partial" if gaps else "complete"
    return attach_selections(result, parser)
