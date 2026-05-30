"""sweep_handshakes — whole-design handshake anomaly sweep (P1).

Reframed per the MCP/LLM boundary: this is NOT a "localize the bug" verdict tool.
It is an M-way fan-out **context reducer**. The LLM physically cannot open the
waveform, and on a design with many near-identical handshake interfaces it would
otherwise pay N round-trips of suggest -> read bundle -> inspect, and carry N
result payloads in context, just to find which interface misbehaves. This
collapses that into one call that returns a **comparative fact table**: every
discovered valid/ready interface with its cycle-by-cycle handshake facts, ordered
by a transparent, mechanical sort key.

It draws no causal conclusion. The ordering is a convenience; every raw fact is
exposed so the LLM ranks and overrides. ``flags`` are factual observations
(e.g. the window ended mid-stall), never verdicts ("deadlock").

Built entirely from the two existing primitives:
  - ``suggest_handshakes`` — discovers valid/ready/clock/payload bundles by name
  - ``inspect_handshake`` — per-interface cycle classification (incl. the
    ``ended_in_stall`` deadlock signature)
Reads existing waveforms only — does NOT rerun simulation.
"""

from __future__ import annotations

from typing import Any, Callable

from src.cursor_store import CursorStore
from src.handshake_suggest import suggest_handshakes
from src.verify_condition import inspect_handshake

DEFAULT_MAX_INTERFACES = 64

# Compact fact subset carried per interface (the full HandshakeInspectResult is
# intentionally not echoed — the sweep returns a comparative table, not N dumps).
_FACT_KEYS = (
    "sample_count", "transfer_count", "stall_count", "max_stall_cycles",
    "max_stall_begin_ps", "ended_in_stall", "final_stall_cycles",
    "payload_hold_violations", "ready_without_valid_cycles", "unknown_sample_cycles",
)


def _flags(res: dict[str, Any]) -> list[str]:
    """Factual tags describing what the cycle scan observed — NOT verdicts."""
    flags: list[str] = []
    if res.get("ended_in_stall"):
        flags.append("ended_in_stall")
    if res.get("payload_hold_violations"):
        flags.append("payload_hold_violation")
    if any(f.get("type") == "long_stall" for f in res.get("findings", [])):
        flags.append("long_stall")
    if res.get("ready_without_valid_cycles"):
        flags.append("ready_without_valid")
    if res.get("unknown_sample_cycles"):
        flags.append("unknown_samples")
    return flags


def _sort_key(iface: dict[str, Any]) -> tuple:
    """Transparent mechanical ordering (documented in the result note). Surfaces
    the most-likely-interesting facts first; it is NOT a causal ranking.
      deadlock signature -> hold violations -> longest stall -> backpressure."""
    return (
        0 if iface.get("ended_in_stall") else 1,
        -int(iface.get("payload_hold_violations") or 0),
        -int(iface.get("max_stall_cycles") or 0),
        -int(iface.get("ready_without_valid_cycles") or 0),
        iface.get("valid") or "",
    )


_SORT_DESC = (
    "ordered (convenience, not a verdict) by: ended_in_stall, then "
    "payload_hold_violations, then max_stall_cycles, then "
    "ready_without_valid_cycles. Raw facts are exposed per interface — re-rank "
    "as the symptom warrants."
)


def sweep_handshake_anomalies(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    scope: str | None = None,
    edge: str = "posedge",
    start_ps: int = 0,
    end_ps: int = -1,
    max_wait_cycles: int = 16,
    max_interfaces: int = DEFAULT_MAX_INTERFACES,
    cursor_store: CursorStore | None = None,
) -> dict[str, Any]:
    """Discover every valid/ready interface and inspect each over the window.

    Returns a comparative fact table sorted by ``_sort_key``. Registers exactly
    ONE cursor, at the top interface's longest-stall begin, when that interface
    shows any anomaly flag (so a follow-up call can jump straight there)."""
    sug = suggest_handshakes(
        get_parser=get_parser, wave_path=wave_path, scope=scope,
        max_candidates=max_interfaces,
    )
    bundles = sug.get("candidates", [])
    # Total interfaces discovered before the max_interfaces cap. suggest orders by
    # confidence then scope then path, so a cap silently drops the *tail* of that
    # ordering — which on a uniform pipeline is the higher-numbered stages, often
    # exactly where the root sits. Surface truncation LOUDLY so the LLM knows
    # coverage was partial and can re-run with a higher max_interfaces.
    discovered = int(sug.get("candidate_count", len(bundles)))
    truncated = discovered > len(bundles)

    interfaces: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for b in bundles:
        base = {"scope": b.get("scope", ""), "valid": b["valid"], "ready": b["ready"]}
        if not b.get("clock"):
            skipped.append({**base, "reason": "no clock found in scope/ancestors"})
            continue
        res = inspect_handshake(
            get_parser=get_parser, wave_path=wave_path,
            clock=b["clock"], valid=b["valid"], ready=b["ready"],
            payload=b.get("payload") or [],
            edge=edge, start_ps=start_ps, end_ps=end_ps,
            max_wait_cycles=max_wait_cycles,
            cursor_store=None,  # the sweep sets ONE cursor, not one per interface
        )
        if res.get("reason"):
            skipped.append({**base, "reason": res["reason"]})
            continue
        interfaces.append({
            "scope": b.get("scope", ""),
            "clock": b.get("clock"),
            "valid": b["valid"],
            "ready": b["ready"],
            "payload": b.get("payload") or [],
            "confidence": b.get("confidence"),
            "flags": _flags(res),
            **{k: res.get(k) for k in _FACT_KEYS},
        })

    interfaces.sort(key=_sort_key)

    cursor = None
    if interfaces and interfaces[0]["flags"] and cursor_store is not None:
        top = interfaces[0]
        anchor = top.get("max_stall_begin_ps")
        if anchor is not None:
            note = (
                f"sweep: {', '.join(top['flags'])} on {top['valid']}/{top['ready']} "
                f"(scope {top['scope'] or '(top)'})"
            )
            ref = cursor_store.auto_set(
                int(anchor), prefix="hs",
                note=note,
                metadata={"source": "sweep_handshakes", "valid": top["valid"],
                          "ready": top["ready"], "edge": edge, "wave_path": wave_path},
                seed=f"sweep|{wave_path}|{top['valid']}|{top['ready']}|{edge}",
            )
            cursor = ref.as_dict()

    n_flagged = sum(1 for i in interfaces if i["flags"])
    if not bundles:
        reason = sug.get("reason") or "no valid/ready handshake interfaces found by name"
    elif not interfaces:
        reason = "handshake interfaces found but none could be inspected (see skipped)"
    else:
        reason = None

    note = _SORT_DESC if interfaces else None
    if truncated:
        warn = (
            f"COVERAGE TRUNCATED: {discovered} interfaces discovered but only "
            f"{len(bundles)} swept (max_interfaces={max_interfaces}). The dropped "
            f"interfaces are the tail of suggest's ordering — re-run with "
            f"max_interfaces>={discovered} for full coverage before trusting the ranking."
        )
        note = warn if note is None else f"{warn} | {note}"

    return {
        "wave_path": wave_path,
        "scope": scope,
        "edge": edge,
        "start_ps": int(start_ps),
        "end_ps": int(end_ps),
        "discovered_count": discovered,
        "interface_count": len(interfaces),
        "flagged_count": n_flagged,
        "truncated": truncated,
        "interfaces": interfaces,
        "skipped": skipped,
        "cursor": cursor,
        "note": note,
        "reason": reason,
    }
