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

_MODES = ("always", "never", "eventually", "implication", "sequence")
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
    delta: dict | None = None,
    within_cycles: int = 1,
    overlap: bool = True,
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
        "overlap": bool(overlap) if mode == "implication" else None,
        "signals": [],
        "holds": False,
        "vacuous": False,
        "cycles_evaluated": 0,
        "unknown_cycles": 0,
        "antecedent_count": 0,
        "beats_evaluated": 0,
        "violation_count": 0,
        "inconclusive_count": 0,
        "counterexample": None,
        "witness": None,
        "violating_signal": None,
        "next_actions": [],
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
        if delta:
            result["reason"] = "implication does not use delta (only 'sequence' does)"
            return result
        if within_cycles < 0:
            result["reason"] = "within_cycles must be >= 0"
            return result
        if not overlap and within_cycles < 1:
            result["reason"] = (
                "overlap=false (non-overlapping: response window starts the NEXT "
                "cycle) requires within_cycles >= 1"
            )
            return result
    else:
        # sequence reuses `predicate` as the accepted-beat gate.
        terms_sets = {"predicate": predicate}
        if antecedent or consequent:
            result["reason"] = f"mode {mode!r} uses predicate, not antecedent/consequent"
            return result
        if mode == "sequence":
            derr = _validate_delta(delta)
            if derr:
                result["reason"] = derr
                return result
        elif delta:
            result["reason"] = f"mode {mode!r} does not use delta (only 'sequence' does)"
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
    seq_raws: list[str] = []
    if mode == "sequence":
        seq_raws = [delta["signal"], *(t["signal"] for t in (delta.get("restart_when") or []))]
    for raw in [t["signal"] for t in all_terms] + seq_raws:
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
            within_cycles, overlap,
        )
    elif mode == "sequence":
        _eval_sequence(result, samples, _resolve_set(predicate), _resolve_delta(delta, sig_map))
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
    if result.get("vacuous"):
        warnings.append(
            f"VACUOUS PASS: all {result['antecedent_count']} antecedent(s) satisfied the "
            "consequent on the SAME cycle, so within_cycles never mattered — this 'holds' "
            "does NOT prove the consequent on any LATER cycle and must not be used as "
            "exclusion evidence. If you meant a stability/hold property ('must STILL hold "
            "the next cycle', e.g. valid/HTRANS held through a wait state), re-run with "
            "overlap=false to start the response window at the next cycle."
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
    consequent: list[dict], within: int, overlap: bool = True,
) -> None:
    """Overlapping (`|->`, default) vs non-overlapping (`|=>`, overlap=False).

    ``overlap=True`` checks the consequent on the inclusive window [i, i+within]
    (the antecedent cycle itself may satisfy it). ``overlap=False`` starts at the
    NEXT cycle, [i+1, i+within] — the right shape for a stability/hold property
    ("must STILL hold next cycle", e.g. AHB HTRANS held through a wait state),
    where the antecedent already implies the consequent on its own cycle.
    """
    n = len(samples)
    result["cycles_evaluated"] = n
    unknown = 0
    ant_count = 0
    violations = 0
    inconclusive = 0
    responded_same = 0  # satisfied on the antecedent's OWN cycle (overlap only)
    first_violation: dict | None = None
    for i, s in enumerate(samples):
        a = _eval_predicate(antecedent, s["signals"])
        if a is None:
            unknown += 1
            continue
        if not a:
            continue
        ant_count += 1
        start = i if overlap else i + 1  # non-overlapping starts next cycle
        last = i + within  # inclusive response window end
        found = False
        for j in range(start, min(last, n - 1) + 1):
            if _eval_predicate(consequent, samples[j]["signals"]) is True:
                found = True
                if j == i:
                    responded_same += 1
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
    # Vacuity guard (overlapping only): if EVERY antecedent satisfied the
    # consequent on its OWN cycle, the within window never contributed — a PASS
    # here does NOT prove the consequent on any LATER cycle. The classic trap is
    # a hold property whose antecedent already implies the consequent (e.g.
    # antecedent has HTRANS==2 and consequent IS HTRANS==2): it would trivially
    # hold. Keyed on responded_same==ant_count so an inconclusive-at-end PASS
    # (which is NOT a same-cycle satisfaction) is never mislabeled vacuous.
    # Surface it so the verdict is not misread as exclusion evidence; the fix is
    # overlap=false.
    result["vacuous"] = bool(
        overlap and within >= 1 and ant_count > 0 and responded_same == ant_count
    )


def _eval_sequence(result: dict, samples: list[dict], gate: list[dict], delta: dict) -> None:
    """Check the per-accepted-beat increment of one signal (e.g. AHB haddr stride).

    The increment is delta_signal[beat n] - [beat n-1] over the cycles where the
    ``gate`` predicate holds (accepted beats). ``op``/``value`` compare it (default
    eq). ``modulo`` (WRAP region bytes = size*len, supplied by the caller who
    decoded the burst) takes the increment modulo so a legal wrap-around is not a
    violation. ``restart_when`` (a predicate, e.g. htrans==NONSEQ) re-seeds the
    sequence so a new burst's base address is not compared to the previous burst.

    Faithfulness: the first accepted beat (and each restart beat) has no
    predecessor → seed only, never a violation. A wait-state (gate false) keeps
    the predecessor (it is not a new beat). Any x/z on the gate or the tracked
    signal breaks continuity → counted unknown, predecessor reset (never a delta
    across an unknown). WRAP boundaries beyond ``modulo`` and bursts without
    ``restart_when`` are the caller's responsibility (kept anti-thick on purpose).
    """
    sig = delta["signal"]
    op = delta["op"]
    target = delta["value"]
    modulo = delta["modulo"]
    restart = delta["restart_when"]
    result["cycles_evaluated"] = len(samples)
    unknown = beats = violations = 0
    first_violation: dict | None = None
    prev_val: int | None = None
    prev_idx: int | None = None
    for i, s in enumerate(samples):
        sigs = s["signals"]
        g = _eval_predicate(gate, sigs)
        if g is None:
            unknown += 1
            prev_val = None
            continue
        if not g:
            continue  # not an accepted beat (e.g. a wait state) — predecessor holds
        cur = _seq_dec(sigs.get(sig))
        if cur is None:  # x/z on the tracked signal — cannot compute a delta
            unknown += 1
            prev_val = None
            continue
        rs = _eval_predicate(restart, sigs) if restart else False
        if rs is None:
            unknown += 1
            prev_val = None
            continue
        if rs or prev_val is None:
            prev_val, prev_idx = cur, i  # new burst / first beat — seed, no check
            continue
        beats += 1
        actual = cur - prev_val
        if modulo is not None:
            actual %= modulo
        if not _cmp(actual, op, target):
            violations += 1
            if first_violation is None:
                first_violation = _seq_evidence(
                    s, i, sig, cur, prev_val, prev_idx, actual, op, target, modulo,
                )
        prev_val, prev_idx = cur, i
    result["unknown_cycles"] = unknown
    result["beats_evaluated"] = beats
    result["violation_count"] = violations
    result["counterexample"] = first_violation
    result["holds"] = violations == 0
    if first_violation is not None:
        result["violating_signal"] = sig
        result["next_actions"] = [_driver_next_action(
            sig,
            "attribute this address/stride violation: the stepped signal is "
            "master-driven, so a wrong increment points at the master's address "
            "logic — confirm the driving instance (master vs slave).",
        )]


def _seq_dec(v: Any) -> int | None:
    return v.get("dec") if isinstance(v, dict) else None


def _cmp(a: int, op: str, b: int) -> bool:
    if op == "eq":
        return a == b
    if op == "ne":
        return a != b
    if op == "gt":
        return a > b
    if op == "ge":
        return a >= b
    if op == "lt":
        return a < b
    return a <= b  # le (validated upfront)


def _seq_evidence(sample: dict, idx: int, sig: str, cur: int, prev_val: int,
                  prev_idx: int | None, actual: int, op: str, target: int,
                  modulo: int | None) -> dict[str, Any]:
    expected = f"{op} {target}" + (f" (mod {modulo})" if modulo is not None else "")
    return {
        "time_ps": sample["time_ps"],
        "cycle_index": idx,
        "signal_values": {
            sig: hex(cur),
            f"{sig}@prev_beat": hex(prev_val),
            "prev_beat_cycle": str(prev_idx),
            "actual_increment": str(actual),
            "expected_increment": expected,
        },
    }


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


def _validate_delta(delta: Any) -> str | None:
    if not isinstance(delta, dict):
        return ("sequence mode needs a 'delta' object "
                "{signal, value, op?, modulo?, restart_when?}")
    if not isinstance(delta.get("signal"), str) or not delta["signal"]:
        return "delta needs a 'signal' (string) to track the per-beat increment of"
    if "value" not in delta:
        return "delta needs a 'value' (the expected per-beat increment, e.g. 1 for byte)"
    try:
        _coerce(delta["value"])
    except (ValueError, TypeError):
        return f"delta value {delta.get('value')!r} is not an integer"
    op = delta.get("op", "eq")
    if op not in _VALUE_OPS:
        return f"delta op must be one of {_VALUE_OPS}, got {op!r}"
    if delta.get("modulo") is not None:
        try:
            if _coerce(delta["modulo"]) <= 0:
                return "delta modulo must be a positive integer (WRAP region size)"
        except (ValueError, TypeError):
            return f"delta modulo {delta.get('modulo')!r} is not an integer"
    rw = delta.get("restart_when")
    if rw is not None:
        return _validate_terms(rw, "delta.restart_when")
    return None


def _resolve_delta(delta: dict, sig_map: dict[str, str]) -> dict:
    """Coerce numeric fields and rewrite signal paths to their resolved form."""
    out = {
        "signal": sig_map[delta["signal"]],
        "op": delta.get("op", "eq"),
        "value": _coerce(delta["value"]),
        "modulo": _coerce(delta["modulo"]) if delta.get("modulo") is not None else None,
        "restart_when": [
            {**t, "signal": sig_map[t["signal"]]} for t in (delta.get("restart_when") or [])
        ],
    }
    return out


def _driver_next_action(signal: str, reason: str) -> dict[str, Any]:
    """Bridge a bus fact to RTL tracing. Bus-fact tools do NOT self-attribute
    master vs slave; this points the caller at the drive-direction lookup."""
    return {"tool": "explain_signal_driver", "reason": reason, "signal_path": signal}


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
