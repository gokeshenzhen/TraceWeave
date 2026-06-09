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
from src.handshake_suggest import suggest_handshakes, suggest_protocol_bundles
from src.verify_condition import inspect_handshake

DEFAULT_MAX_INTERFACES = 64

# Compact fact subset carried per interface (the full HandshakeInspectResult is
# intentionally not echoed — the sweep returns a comparative table, not N dumps).
_FACT_KEYS = (
    "sample_count", "transfer_count", "stall_count", "max_stall_cycles",
    "max_stall_begin_ps", "ended_in_stall", "final_stall_cycles",
    "payload_hold_violations", "valid_deassert_violations",
    "x_while_valid_violations", "write_data_hold_violations",
    "ready_without_valid_cycles", "unknown_sample_cycles",
    "coverage",
)


def _is_clocking_block_scope(scope: str) -> bool:
    """A SystemVerilog clocking block (e.g. ``tb.m_if0.mdrv_cb``) mirrors its parent
    interface's signals for TB sampling — it is not a distinct bus and has no clock
    of its own, so handshake discovery would at best re-inspect a duplicate of the
    parent interface and at worst only ``skip`` it (dragging coverage to 'degraded').
    Drop it from the sweep. Heuristic on the scope leaf: the SV ``_cb`` clocking-block
    suffix convention (mdrv_cb/smon_cb/...), or a ``*clocking`` name."""
    leaf = (scope.rsplit(".", 1)[-1]).split("[", 1)[0].lower()
    return leaf.endswith("_cb") or leaf == "cb" or leaf.endswith("clocking")


def _normalize_vr(b: dict[str, Any]) -> dict[str, Any]:
    """A-class (valid/ready) bundle from suggest_handshakes -> common shape."""
    return {
        "scope": b.get("scope", ""), "clock": b.get("clock"),
        "valid": b["valid"], "ready": b["ready"], "kind": "valid_ready",
        "payload": b.get("payload") or [], "confidence": b.get("confidence"),
        "inspect_kwargs": {"valid": b["valid"]},
    }


def _normalize_ahb(b: dict[str, Any]) -> dict[str, Any]:
    """AHB bundle from suggest_protocol_bundles -> common shape. AHB has no
    literal valid; inspect_handshake derives it from htrans (valid_htrans)."""
    return {
        "scope": b.get("scope", ""), "clock": b.get("clock"),
        "valid": b.get("valid_htrans"), "ready": b.get("ready"), "kind": "ahb",
        "payload": b.get("payload") or [], "confidence": b.get("confidence"),
        "inspect_kwargs": {
            "valid_htrans": b.get("valid_htrans"),
            "htrans_rule": b.get("htrans_rule") or "active",
            # hwrite + write_data enable the write data-phase HWDATA-hold check;
            # both are None on a bundle that did not locate them (check stays off).
            **({"hwrite": b["hwrite"]} if b.get("hwrite") else {}),
            **({"write_data": b["write_data"]} if b.get("write_data") else {}),
        },
    }


def _flags(res: dict[str, Any], kind: str) -> list[str]:
    """Factual tags describing what the cycle scan observed — NOT verdicts.

    ``ready_without_valid`` is suppressed for an ``ahb`` row: there it means
    HREADY high while HTRANS is idle — an idle bus, not backpressure — so flagging
    it would be a false anomaly that trains the reader to distrust the row. The raw
    ``ready_without_valid_cycles`` count is still carried in the facts."""
    flags: list[str] = []
    if res.get("ended_in_stall"):
        flags.append("ended_in_stall")
    if res.get("x_while_valid_violations"):
        flags.append("x_while_valid")
    if res.get("payload_hold_violations"):
        flags.append("payload_hold_violation")
    if res.get("write_data_hold_violations"):
        flags.append("write_data_hold_violation")
    if res.get("valid_deassert_violations"):
        flags.append("premature_valid_deassertion")
    if any(f.get("type") == "long_stall" for f in res.get("findings", [])):
        flags.append("long_stall")
    if kind != "ahb" and res.get("ready_without_valid_cycles"):
        flags.append("ready_without_valid")
    if res.get("unknown_sample_cycles"):
        flags.append("unknown_samples")
    return flags


def _sort_key(iface: dict[str, Any]) -> tuple:
    """Transparent mechanical ordering (documented in the result note). Surfaces
    the most-likely-interesting facts first; it is NOT a causal ranking.
      deadlock signature -> x-while-valid -> payload-hold -> write-data-hold ->
      premature deassertion -> longest stall -> backpressure.
    ready_without_valid does NOT weight an ahb row (idle-bus, not backpressure)."""
    rwv = int(iface.get("ready_without_valid_cycles") or 0) if iface.get("kind") != "ahb" else 0
    return (
        0 if iface.get("ended_in_stall") else 1,
        -int(iface.get("x_while_valid_violations") or 0),
        -int(iface.get("payload_hold_violations") or 0),
        -int(iface.get("write_data_hold_violations") or 0),
        -int(iface.get("valid_deassert_violations") or 0),
        -int(iface.get("max_stall_cycles") or 0),
        -rwv,
        iface.get("valid") or "",
    )


_SORT_DESC = (
    "ordered (convenience, not a verdict) by: ended_in_stall, then "
    "x_while_valid_violations, then payload_hold_violations, then "
    "write_data_hold_violations, then valid_deassert_violations, then "
    "max_stall_cycles, then ready_without_valid_cycles (ahb rows excluded: "
    "idle-bus, not backpressure). "
    "Raw facts are exposed per interface — re-rank as the symptom warrants."
)


def _retry_without_scope_action(
    *,
    wave_path: str,
    edge: str,
    start_ps: int,
    end_ps: int,
    max_wait_cycles: int,
    max_interfaces: int,
) -> dict[str, Any]:
    return {
        "tool": "sweep_handshakes",
        "reason": (
            "Retry without scope to establish design-level protocol coverage; "
            "the scoped sweep checked zero interfaces."
        ),
        "arguments": {
            "wave_path": wave_path,
            "edge": edge,
            "start_time_ps": int(start_ps),
            "end_time_ps": int(end_ps),
            "max_wait_cycles": int(max_wait_cycles),
            "max_interfaces": int(max_interfaces),
        },
    }


def _retry_truncated_action(
    *,
    wave_path: str,
    edge: str,
    start_ps: int,
    end_ps: int,
    max_wait_cycles: int,
    discovered_count: int,
    max_interfaces: int,
) -> dict[str, Any]:
    return {
        "tool": "sweep_handshakes",
        "reason": (
            f"Previous sweep discovered {discovered_count} interfaces but only "
            f"swept {max_interfaces} due to cap. Re-run with max_interfaces>={discovered_count} "
            "for complete coverage before trusting the interface ranking."
        ),
        "arguments": {
            "wave_path": wave_path,
            "edge": edge,
            "start_time_ps": int(start_ps),
            "end_time_ps": int(end_ps),
            "max_wait_cycles": int(max_wait_cycles),
            "max_interfaces": int(discovered_count),
        },
    }


# AXI channel markers, most-specific first: the address channels (AR/AW) share
# their trailing r/w letter with the data/response channels (R/W/B), so they MUST
# be tested before R/W to avoid an arvalid being mis-tagged as R. Each entry is
# (channel, substring-markers). A canonical AXI valid is `*arvalid`/`*wvalid`/...;
# the `_x_` infix form covers names like `m0_w_valid`.
_AXI_CHANNEL_MARKERS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("AR", ("arvalid", "_ar_")),
    ("AW", ("awvalid", "_aw_")),
    ("W", ("wvalid", "_w_")),
    ("R", ("rvalid", "_r_")),
    ("B", ("bvalid", "_b_")),
)


def _infer_channel_hint(valid_or_htrans: str) -> str:
    """Infer the AXI channel a valid/htrans signal belongs to, from its name.

    Returns 'AR'/'AW'/'W'/'R'/'B' for the five AXI channels, or 'other' when the
    name carries no recognizable AXI channel marker (generic valid/ready, AHB
    htrans, credit interfaces). This is a naming heuristic that only groups the
    finding_summary — it never drives a verdict, and degrades to 'other' rather
    than guessing.
    """
    s = valid_or_htrans.lower()
    for channel, markers in _AXI_CHANNEL_MARKERS:
        if any(m in s for m in markers):
            return channel
    return "other"


def _compute_finding_summary(
    interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute a compact factual summary of flagged findings (assumes
    ``interfaces`` is already sorted by ``_sort_key``).

    Returns dict with:
      by_flag: count of flagged interfaces carrying each flag.
      by_channel_hint: per-AXI-channel flagged count, SEEDED with 0 for every
        channel present among inspected interfaces — so a clean channel shows
        explicitly as ``R: 0`` rather than silently vanishing. The explicit zero
        is the "W is dirty while R is clean" contrast this summary exists to make
        loud; an absent key is ambiguous (not checked vs. no such channel vs. clean).
      top_scopes: up to 3 DISTINCT scope paths in sort order (most interesting
        first); empty scope (a top-level interface) renders as "(top)".
    """
    by_flag: dict[str, int] = {}
    # Seed every inspected channel at 0 so a clean channel is visible, not absent.
    by_channel: dict[str, int] = {}
    for iface in interfaces:
        by_channel.setdefault(_infer_channel_hint(iface.get("valid") or ""), 0)

    top_scopes: list[str] = []
    for iface in interfaces:
        flags = iface.get("flags") or []
        if not flags:
            continue
        for flag in flags:
            by_flag[flag] = by_flag.get(flag, 0) + 1
        by_channel[_infer_channel_hint(iface.get("valid") or "")] += 1
        scope = iface.get("scope") or "(top)"
        if scope not in top_scopes and len(top_scopes) < 3:
            top_scopes.append(scope)

    return {
        "by_flag": by_flag,
        "by_channel_hint": by_channel,
        "top_scopes": top_scopes,
    }


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
    # Discover BOTH families: valid/ready (A-class: AXI / generic / req-ack, via
    # suggest_handshakes) AND AHB (no literal valid, via suggest_protocol_bundles).
    # Each returns empty on a design that lacks it, so running both is safe and
    # gives a whole-design sweep regardless of bus type. APB is deliberately not
    # scanned: its candidates carry no inspect_handshake args (they need a derived
    # psel&&penable valid that inspect_handshake doesn't accept yet), so they would
    # only land in `skipped`.
    vr = suggest_handshakes(
        get_parser=get_parser, wave_path=wave_path, scope=scope,
        max_candidates=max_interfaces,
    )
    ahb = suggest_protocol_bundles(
        get_parser=get_parser, wave_path=wave_path, protocol="ahb", scope=scope,
        max_candidates=max_interfaces,
    )
    # Drop clocking-block scopes (TB sampling mirrors of a parent interface, no own
    # clock) BEFORE counting/inspecting, so they neither inflate discovered_count nor
    # land in `skipped` and pull coverage to 'degraded'. The real interface (parent)
    # is always discovered alongside them.
    vr_cands = [b for b in vr.get("candidates", []) if not _is_clocking_block_scope(b.get("scope") or "")]
    ahb_cands = [b for b in ahb.get("candidates", []) if not _is_clocking_block_scope(b.get("scope") or "")]
    dropped_cb = ((len(vr.get("candidates", [])) - len(vr_cands))
                  + (len(ahb.get("candidates", [])) - len(ahb_cands)))
    normalized = (
        [_normalize_vr(b) for b in vr_cands]
        + [_normalize_ahb(b) for b in ahb_cands]
    )
    # Total real interfaces discovered across both families before the max_interfaces
    # cap (clocking-block mirrors excluded). A cap silently drops the *tail* of each
    # ordering — on a uniform pipeline the higher-numbered stages, often exactly where
    # the root sits. Surface truncation LOUDLY so the LLM knows coverage was partial.
    discovered = (int(vr.get("candidate_count", 0)) + int(ahb.get("candidate_count", 0))
                  - dropped_cb)
    truncated = discovered > len(normalized) or len(normalized) > max_interfaces
    to_inspect = normalized[:max_interfaces]

    interfaces: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for nb in to_inspect:
        base = {"scope": nb["scope"], "valid": nb["valid"] or "", "ready": nb["ready"] or ""}
        if not nb.get("clock"):
            skipped.append({**base, "reason": "no clock found in scope/ancestors"})
            continue
        res = inspect_handshake(
            get_parser=get_parser, wave_path=wave_path,
            clock=nb["clock"], ready=nb["ready"], payload=nb["payload"],
            edge=edge, start_ps=start_ps, end_ps=end_ps,
            max_wait_cycles=max_wait_cycles,
            cursor_store=None,  # the sweep sets ONE cursor, not one per interface
            **nb["inspect_kwargs"],
        )
        if res.get("reason"):
            skipped.append({**base, "reason": res["reason"]})
            continue
        # Carry side attribution only for one-sided rows (payload-hold / premature
        # deassertion → valid_driver side). A clean or two-sided-stall row leaves
        # it None so the table is not bloated with empty blocks.
        attribution = res.get("attribution") or {}
        row_attribution = attribution if attribution.get("violating_side") else None
        interfaces.append({
            "scope": nb["scope"],
            "clock": nb["clock"],
            "valid": nb["valid"],
            "ready": nb["ready"],
            "kind": nb["kind"],
            "payload": nb["payload"],
            "confidence": nb["confidence"],
            "flags": _flags(res, nb["kind"]),
            "attribution": row_attribution,
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
    if not normalized:
        parts = [r for r in (vr.get("reason"), ahb.get("reason")) if r]
        reason = " | ".join(parts) if parts else (
            "no valid/ready or AHB handshake interfaces found by name"
        )
    elif not interfaces:
        reason = "handshake interfaces found but none could be inspected (see skipped)"
    else:
        reason = None

    coverage_warnings: list[str] = []
    suggested_next_actions: list[dict[str, Any]] = []
    if not normalized:
        coverage_status = "zero_coverage"
        warning = (
            "ZERO COVERAGE: sweep_handshakes did not discover any valid/ready or "
            "AHB interfaces, so no protocol interfaces were checked. flagged_count=0 "
            "is not a protocol pass."
        )
        if scope:
            warning += (
                f" The requested scope {scope!r} may be below, above, or separate "
                "from the dumped interface signals; retry without scope or with a "
                "parent/interface scope."
            )
            suggested_next_actions.append(
                _retry_without_scope_action(
                    wave_path=wave_path,
                    edge=edge,
                    start_ps=start_ps,
                    end_ps=end_ps,
                    max_wait_cycles=max_wait_cycles,
                    max_interfaces=max_interfaces,
                )
            )
        coverage_warnings.append(warning)
    elif truncated:
        coverage_status = "truncated"
        coverage_warnings.append(
            f"COVERAGE TRUNCATED: {discovered} interfaces discovered but only "
            f"{len(to_inspect)} swept (max_interfaces={max_interfaces}). The dropped "
            "interfaces are the tail of suggest's ordering; re-run with "
            f"max_interfaces>={discovered} for full coverage before trusting the ranking."
        )
        suggested_next_actions.append(
            _retry_truncated_action(
                wave_path=wave_path,
                edge=edge,
                start_ps=start_ps,
                end_ps=end_ps,
                max_wait_cycles=max_wait_cycles,
                discovered_count=discovered,
                max_interfaces=max_interfaces,
            )
        )
    elif skipped:
        coverage_status = "degraded"
        coverage_warnings.append(
            "COVERAGE DEGRADED: one or more discovered handshake interfaces could "
            "not be inspected; see skipped. flagged_count=0 only covers inspected rows."
        )
    else:
        coverage_status = "complete"

    finding_summary = _compute_finding_summary(interfaces) if n_flagged > 0 else None

    note = _SORT_DESC if interfaces else None
    if truncated:
        warn = coverage_warnings[0]
        note = warn if note is None else f"{warn} | {note}"
    elif coverage_status == "zero_coverage":
        note = coverage_warnings[0]
    elif coverage_status == "degraded":
        note = coverage_warnings[0] if note is None else f"{coverage_warnings[0]} | {note}"

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
        "coverage_status": coverage_status,
        "coverage_warnings": coverage_warnings,
        "suggested_next_actions": suggested_next_actions,
        "finding_summary": finding_summary,
        "interfaces": interfaces,
        "skipped": skipped,
        "cursor": cursor,
        "note": note,
        "reason": reason,
    }
