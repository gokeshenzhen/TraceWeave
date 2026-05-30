"""verify_window — evaluate a small set of temporal predicate templates over a
clock window (P3, the "small templated version").

The anti-thickening primitive: inspect_handshake / period / diff_first_divergence
are all special cases of "evaluate a temporal predicate over a clock window". This
lets the LLM PROPOSE a predicate and get a precise verdict-against-data (with a
concrete witness / counterexample) in one call — perception+precision the LLM
cannot do itself (it can't open the waveform, can't count thousands of cycles).

Deliberately TEMPLATES, not a DSL (the Lark-grammar red line stays uncrossed):
  - a *term* is one condition over one signal: {signal, op, value}
    op ∈ eq | ne | gt | ge | lt | le | is_x | is_known
  - a *predicate* is a list of terms = implicit AND (no OR, no nesting — run two
    calls for OR)
  - four *modes*: always(P), never(P), eventually(P),
    implication (A |-> B within N cycles)

Faithfulness: x/z cycles are reported as unknown_cycles, never silently counted as
pass; an implication whose response window runs past end-of-trace is reported as
inconclusive, never silently counted as a violation; unresolved signals are loud.
Reads existing waveforms only — does NOT rerun simulation.
"""

from __future__ import annotations

from typing import Any, Callable

from .cursor_store import CursorStore
from .cycle_query import sample_signals_on_edges
from .verify_condition import _resolve_signal_path

_MODES = ("always", "never", "eventually", "implication")
_TERM_OPS = ("eq", "ne", "gt", "ge", "lt", "le", "is_x", "is_known")
_VALUE_OPS = ("eq", "ne", "gt", "ge", "lt", "le")


def verify_window(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    clock: str,
    mode: str,
    predicate: list[dict] | None = None,
    antecedent: list[dict] | None = None,
    consequent: list[dict] | None = None,
    within_cycles: int = 1,
    edge: str = "posedge",
    start_ps: int = 0,
    end_ps: int = -1,
    cursor_store: CursorStore | None = None,
    cursor_name: str | None = None,
    cursor_note: str | None = None,
) -> dict[str, Any]:
    parser = get_parser(wave_path)
    result: dict[str, Any] = {
        "wave_path": wave_path,
        "clock": clock,
        "edge": edge,
        "mode": mode,
        "start_ps": int(start_ps),
        "end_ps": int(end_ps),
        "within_cycles": int(within_cycles) if mode == "implication" else None,
        "signals": [],
        "holds": False,
        "cycles_evaluated": 0,
        "unknown_cycles": 0,
        "antecedent_count": 0,
        "violation_count": 0,
        "inconclusive_count": 0,
        "counterexample": None,
        "witness": None,
        "cursor": None,
        "reason": None,
        "warnings": [],
        "signal_errors": {},
    }
    warnings: list[str] = result["warnings"]

    # --- validate mode + templates (loud, before any I/O work) ---------------
    if mode not in _MODES:
        result["reason"] = f"mode must be one of {_MODES}, got {mode!r}"
        return result
    if mode == "implication":
        terms_sets = {"antecedent": antecedent, "consequent": consequent}
        if predicate:
            result["reason"] = "implication uses antecedent + consequent, not predicate"
            return result
        if within_cycles < 0:
            result["reason"] = "within_cycles must be >= 0"
            return result
    else:
        terms_sets = {"predicate": predicate}
        if antecedent or consequent:
            result["reason"] = f"mode {mode!r} uses predicate, not antecedent/consequent"
            return result

    all_terms: list[dict] = []
    for label, terms in terms_sets.items():
        if not terms:
            result["reason"] = f"{label} must be a non-empty list of terms"
            return result
        err = _validate_terms(terms, label)
        if err:
            result["reason"] = err
            return result
        all_terms.extend(terms)

    # --- resolve signals (auto-correct bus bit-range; loud on miss) ----------
    clock = _resolve_signal_path(parser, clock)
    sig_map: dict[str, str] = {}
    for t in all_terms:
        raw = t["signal"]
        if raw not in sig_map:
            sig_map[raw] = _resolve_signal_path(parser, raw)
    resolved_signals = sorted(set(sig_map.values()))
    result["clock"] = clock
    result["signals"] = resolved_signals

    sampled = sample_signals_on_edges(
        parser, clock, resolved_signals, start_ps=start_ps, end_ps=end_ps, edge=edge,
    )
    signal_errors = sampled.get("signal_errors", {})
    result["signal_errors"] = signal_errors
    unresolved = [s for s in resolved_signals if s in signal_errors]
    if unresolved:
        result["reason"] = (
            "signal(s) not found: " + ", ".join(unresolved)
            + " — FSDB buses usually need an explicit bit range (e.g. 'top.addr[7:0]')."
        )
        return result

    samples = sampled.get("samples", [])
    if not samples:
        result["reason"] = (
            f"no {edge} edges of clock {clock!r} in the window — nothing to evaluate"
        )
        return result

    # Rewrite term signal paths to their resolved form so eval keys match samples.
    def _resolve_set(terms: list[dict]) -> list[dict]:
        return [{**t, "signal": sig_map[t["signal"]]} for t in terms]

    if mode == "implication":
        _eval_implication(
            result, samples, _resolve_set(antecedent), _resolve_set(consequent),
            within_cycles,
        )
    else:
        _eval_simple(result, samples, mode, _resolve_set(predicate))

    if result["unknown_cycles"]:
        warnings.append(
            f"{result['unknown_cycles']} cycle(s) had x/z on a referenced signal and "
            "could not be evaluated — not counted as pass or fail."
        )
    if result["inconclusive_count"]:
        warnings.append(
            f"{result['inconclusive_count']} antecedent(s) fired too close to the window "
            "end to confirm a response within the window — reported as inconclusive, "
            "not as violations. Extend the window to check them."
        )

    _attach_cursor(result, cursor_store, wave_path, edge, cursor_name, cursor_note)
    return result


# ---------------------------------------------------------------------------
# evaluation
# ---------------------------------------------------------------------------
def _eval_simple(result: dict, samples: list[dict], mode: str, terms: list[dict]) -> None:
    result["cycles_evaluated"] = len(samples)
    first_true: dict | None = None
    first_false: dict | None = None
    unknown = 0
    for idx, s in enumerate(samples):
        r = _eval_predicate(terms, s["signals"])
        if r is None:
            unknown += 1
            continue
        if r and first_true is None:
            first_true = _evidence(s, idx, terms)
        if (not r) and first_false is None:
            first_false = _evidence(s, idx, terms)
    result["unknown_cycles"] = unknown

    if mode == "always":
        result["holds"] = first_false is None
        result["counterexample"] = first_false
    elif mode == "never":
        result["holds"] = first_true is None
        result["counterexample"] = first_true
    else:  # eventually
        result["holds"] = first_true is not None
        result["witness"] = first_true


def _eval_implication(
    result: dict, samples: list[dict], antecedent: list[dict],
    consequent: list[dict], within: int,
) -> None:
    n = len(samples)
    result["cycles_evaluated"] = n
    unknown = 0
    ant_count = 0
    violations = 0
    inconclusive = 0
    first_violation: dict | None = None
    for i, s in enumerate(samples):
        a = _eval_predicate(antecedent, s["signals"])
        if a is None:
            unknown += 1
            continue
        if not a:
            continue
        ant_count += 1
        last = i + within  # inclusive response window [i, i+within]
        found = False
        for j in range(i, min(last, n - 1) + 1):
            if _eval_predicate(consequent, samples[j]["signals"]) is True:
                found = True
                break
        if found:
            continue
        if last > n - 1:
            # response window extends past the captured trace — cannot conclude.
            inconclusive += 1
        else:
            violations += 1
            if first_violation is None:
                first_violation = _evidence(s, i, antecedent)
    result["unknown_cycles"] = unknown
    result["antecedent_count"] = ant_count
    result["violation_count"] = violations
    result["inconclusive_count"] = inconclusive
    result["counterexample"] = first_violation
    result["holds"] = violations == 0


def _eval_predicate(terms: list[dict], sig_values: dict[str, Any]) -> bool | None:
    """Three-valued AND: False if any term False, else None if any unknown, else True."""
    saw_none = False
    for t in terms:
        r = _eval_term(t, sig_values)
        if r is False:
            return False
        if r is None:
            saw_none = True
    return None if saw_none else True


def _eval_term(term: dict, sig_values: dict[str, Any]) -> bool | None:
    op = term["op"]
    v = sig_values.get(term["signal"])
    dec = v.get("dec") if isinstance(v, dict) else None
    if op == "is_x":
        return dec is None
    if op == "is_known":
        return dec is not None
    if dec is None:
        return None  # cannot compare an x/z value
    val = _coerce(term["value"])
    if op == "eq":
        return dec == val
    if op == "ne":
        return dec != val
    if op == "gt":
        return dec > val
    if op == "ge":
        return dec >= val
    if op == "lt":
        return dec < val
    if op == "le":
        return dec <= val
    raise ValueError(f"unknown op {op!r}")  # unreachable; validated upfront


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _validate_terms(terms: Any, label: str) -> str | None:
    if not isinstance(terms, list):
        return f"{label} must be a list of {{signal, op, value}} terms"
    for t in terms:
        if not isinstance(t, dict) or "signal" not in t or "op" not in t:
            return f"each {label} term needs at least 'signal' and 'op'"
        if t["op"] not in _TERM_OPS:
            return f"term op must be one of {_TERM_OPS}, got {t['op']!r}"
        if t["op"] in _VALUE_OPS:
            if "value" not in t:
                return f"term op {t['op']!r} on {t['signal']!r} requires a 'value'"
            try:
                _coerce(t["value"])
            except (ValueError, TypeError):
                return f"term value {t.get('value')!r} on {t['signal']!r} is not an integer"
    return None


def _coerce(value: Any) -> int:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value, 0)  # handles 0x.., 0b.., decimal
    raise TypeError(f"value {value!r} is not an integer")


def _evidence(sample: dict, idx: int, terms: list[dict]) -> dict[str, Any]:
    sigs = sample["signals"]
    referenced = {t["signal"] for t in terms}
    return {
        "time_ps": sample["time_ps"],
        "cycle_index": idx,
        "signal_values": {s: _repr_val(sigs.get(s)) for s in sorted(referenced)},
    }


def _repr_val(v: Any) -> str | None:
    if not isinstance(v, dict):
        return None
    if v.get("hex") is not None:
        return str(v["hex"])
    if v.get("bin") is not None:
        return f"0b{v['bin']}"
    if v.get("dec") is not None:
        return str(v["dec"])
    return None


def _attach_cursor(
    result: dict, cursor_store: CursorStore | None, wave_path: str, edge: str,
    cursor_name: str | None, cursor_note: str | None,
) -> None:
    if cursor_store is None:
        return
    ev = result["counterexample"] or result["witness"]
    if ev is None:
        return
    kind = "counterexample" if result["counterexample"] else "witness"
    note = cursor_note or f"verify_window {result['mode']} {kind} @cycle {ev['cycle_index']}"
    metadata = {"source": "verify_window", "mode": result["mode"], "kind": kind,
                "edge": edge, "wave_path": wave_path}
    if cursor_name:
        ref = cursor_store.set(cursor_name, ev["time_ps"], note=note, metadata=metadata)
    else:
        ref = cursor_store.auto_set(
            ev["time_ps"], prefix="vw", note=note, metadata=metadata,
            seed=f"vw|{wave_path}|{result['mode']}|{ev['time_ps']}",
        )
    result["cursor"] = ref.as_dict()
