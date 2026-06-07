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
    "ready_without_valid_cycles", "unknown_sample_cycles",
    "coverage",
)


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
        },
    }


def _flags(res: dict[str, Any]) -> list[str]:
    """Factual tags describing what the cycle scan observed — NOT verdicts."""
    flags: list[str] = []
    if res.get("ended_in_stall"):
        flags.append("ended_in_stall")
    if res.get("payload_hold_violations"):
        flags.append("payload_hold_violation")
    if res.get("valid_deassert_violations"):
        flags.append("premature_valid_deassertion")
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
      deadlock signature -> hold violations -> premature deassertion ->
      longest stall -> backpressure."""
    return (
        0 if iface.get("ended_in_stall") else 1,
        -int(iface.get("payload_hold_violations") or 0),
        -int(iface.get("valid_deassert_violations") or 0),
        -int(iface.get("max_stall_cycles") or 0),
        -int(iface.get("ready_without_valid_cycles") or 0),
        iface.get("valid") or "",
    )


_SORT_DESC = (
    "ordered (convenience, not a verdict) by: ended_in_stall, then "
    "payload_hold_violations, then valid_deassert_violations, then "
    "max_stall_cycles, then ready_without_valid_cycles. Raw facts are exposed "
    "per interface — re-rank as the symptom warrants."
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


def _infer_channel_hint(valid_or_htrans: str) -> str:
    """Infer AXI channel from valid/valid_htrans signal name.

    Returns one of: 'R', 'W', 'AR', 'AW', 'B', 'other'.
    """
    sig_lower = valid_or_htrans.lower()
    # Check for AXI channel markers in the signal name
    if "_r_" in sig_lower or "_rvalid" in sig_lower or sig_lower.endswith("_r"):
        return "R"
    if "_w_" in sig_lower or "_wvalid" in sig_lower or sig_lower.endswith("_w"):
        return "W"
    if "_ar_" in sig_lower or "_arvalid" in sig_lower or sig_lower.endswith("_ar"):
        return "AR"
    if "_aw_" in sig_lower or "_awvalid" in sig_lower or sig_lower.endswith("_aw"):
        return "AW"
    if "_b_" in sig_lower or "_bvalid" in sig_lower or sig_lower.endswith("_b"):
        return "B"
    # Fallback: look for channel name anywhere in the string
    for ch in ["_r", "_w", "_ar", "_aw", "_b"]:
        if ch in sig_lower:
            return ch.lstrip("_").upper()
    return "other"


def _compute_finding_summary(
    interfaces: list[dict[str, Any]],
) -> dict[str, Any]:
    """Compute compact factual summary of flagged findings.

    Returns dict with:
      by_flag: count of interfaces with each flag
      by_channel_hint: count of flagged interfaces by inferred AXI channel
      top_scopes: list of top 3 scope paths by sort order (most interesting first)
    """
    by_flag: dict[str, int] = {}
    by_channel: dict[str, int] = {}
    top_scopes: list[str] = []

    for iface in interfaces:
        flags = iface.get("flags", [])
        if not flags:
            continue
        # Count each flag occurrence
        for flag in flags:
            by_flag[flag] = by_flag.get(flag, 0) + 1
        # Count by channel (only for flagged interfaces)
        channel = _infer_channel_hint(iface.get("valid", ""))
        by_channel[channel] = by_channel.get(channel, 0) + 1
        # Collect top 3 scopes
        if len(top_scopes) < 3:
            scope = iface.get("scope", "(top)")
            if scope:
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
    normalized = (
        [_normalize_vr(b) for b in vr.get("candidates", [])]
        + [_normalize_ahb(b) for b in ahb.get("candidates", [])]
    )
    # Total interfaces discovered across both families before the max_interfaces
    # cap. A cap silently drops the *tail* of each ordering — on a uniform pipeline
    # the higher-numbered stages, often exactly where the root sits. Surface
    # truncation LOUDLY so the LLM knows coverage was partial.
    discovered = int(vr.get("candidate_count", 0)) + int(ahb.get("candidate_count", 0))
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
            "flags": _flags(res),
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
