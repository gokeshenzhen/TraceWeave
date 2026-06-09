"""suggest_handshakes — propose ready-to-use inspect_handshake bundles.

T2 of the protocol-debug ROI plan (the "self-serve multiplier"): instead of the
LLM hand-assembling {clock, valid, ready, payload} signal paths before it can
call ``inspect_handshake``, this scans the waveform's signal universe and
proposes candidate handshake bundles — pairing ``*valid``/``*ready`` by scope +
stem, locating the clock, and grouping the channel payload (the buses that must
hold steady during a stall).

The pairing core (``propose_handshake_bundles``) is a pure function over a flat
list of ``{path, name, width}`` signal descriptors so it is fully unit-testable;
the I/O wrapper (``suggest_handshakes``) gathers that list from a parser via
``search_signals`` and calls the core.

Scope note: ``suggest_handshakes`` covers AXI ``*valid``/``*ready``, generic
``x_valid``/``x_ready`` and ``req``/``ack`` handshakes. Protocols without a
literal valid are handled by ``suggest_protocol_bundles``: AHB bundles use
``valid_htrans`` for ``inspect_handshake``; APB bundles are reported as
``psel``/``penable``/``pready`` facts because ``inspect_handshake`` does not
accept a derived ``psel && penable`` valid expression yet.
"""

from __future__ import annotations

import re
from typing import Any, Callable

# Role vocabulary, aligned with analyzer._ROLE_KEYWORDS["handshake"].
VALID_MARKERS = ("valid", "vld", "req")
READY_MARKERS = ("ready", "rdy", "ack", "grant", "gnt")
CLOCK_TOKENS = ("clk", "clock")
RESET_TOKENS = ("rst", "reset", "resetn", "rst_n")
# var_types that are never protocol payload (testbench bookkeeping, not signals).
_NON_SIGNAL_VARTYPES = ("integer", "real", "time", "parameter", "string", "event")

_RANGE_RE = re.compile(r"\[[^\]]*\]\s*$")


def _leaf(path: str) -> str:
    """Last hierarchy component, without a trailing bit range."""
    leaf = path.rsplit(".", 1)[-1]
    return _RANGE_RE.sub("", leaf)


def _parent(path: str) -> str:
    return path.rsplit(".", 1)[0] if "." in path else ""


def _matched_marker(leaf_lower: str, markers: tuple[str, ...]) -> str | None:
    """Return the marker that ``leaf_lower`` ends with (longest first)."""
    for m in sorted(markers, key=len, reverse=True):
        if leaf_lower.endswith(m):
            return m
    return None


def _stem(leaf_lower: str, marker: str) -> str:
    """Channel stem = leaf minus the trailing role marker, separators trimmed."""
    return leaf_lower[: -len(marker)].rstrip("_.")


def _classify(leaf_lower: str, width: int) -> tuple[str | None, str | None]:
    """Return (role, marker). role in {clock, reset, valid, ready, None}."""
    if any(tok in leaf_lower for tok in CLOCK_TOKENS) and width == 1:
        return "clock", None
    if leaf_lower in RESET_TOKENS or any(leaf_lower.endswith(t) for t in ("rst_n", "reset", "resetn")):
        return "reset", None
    # ready is checked before valid only for clarity; the marker sets are disjoint.
    m = _matched_marker(leaf_lower, READY_MARKERS)
    if m and width == 1:
        return "ready", m
    m = _matched_marker(leaf_lower, VALID_MARKERS)
    if m and width == 1:
        return "valid", m
    return None, None


def propose_handshake_bundles(
    signals: list[dict],
    scope: str | None = None,
    max_payload: int = 8,
) -> list[dict]:
    """Pure core: from a flat signal list, return ranked handshake bundles.

    Each signal is ``{path, name?, width?}``. A bundle is
    ``{scope, clock, valid, ready, payload[], confidence, rationale, needs}``
    where ``clock``/``valid``/``ready`` are full signal paths ready to pass to
    ``inspect_handshake`` and ``payload`` are same-channel buses.
    """
    # Index by role.
    valids: list[dict] = []
    readys: list[dict] = []
    clocks: list[dict] = []
    by_scope: dict[str, list[dict]] = {}

    for s in signals:
        path = s.get("path")
        if not path:
            continue
        width = int(s.get("width") or 1)
        leaf = _leaf(path).lower()
        role, marker = _classify(leaf, width)
        rec = {"path": path, "leaf": leaf, "width": width, "scope": _parent(path),
               "role": role, "marker": marker,
               "var_type": (s.get("var_type") or "").lower()}
        by_scope.setdefault(rec["scope"], []).append(rec)
        if role == "valid":
            valids.append(rec)
        elif role == "ready":
            readys.append(rec)
        elif role == "clock":
            clocks.append(rec)

    bundles: list[dict] = []
    for v in valids:
        if scope and not v["path"].startswith(scope):
            continue
        v_stem = _stem(v["leaf"], v["marker"])
        # Find a ready in the same scope with a matching stem.
        match = None
        for r in readys:
            if r["scope"] != v["scope"]:
                continue
            if _stem(r["leaf"], r["marker"]) == v_stem:
                match = r
                break
        if match is None:
            continue

        clock = _pick_clock(clocks, v["scope"])
        payload = _pick_payload(by_scope.get(v["scope"], []), v_stem, max_payload)

        needs = []
        if clock is None:
            needs.append("clock")
        confidence = "high" if (clock and payload) else "medium" if clock else "low"
        rationale = (
            f"{v['leaf']}/{match['leaf']} pair in scope {v['scope'] or '(top)'}"
            + (f", channel stem '{v_stem}'" if v_stem else "")
            + (f", clock {_leaf(clock)}" if clock else ", no clock found in scope/ancestors")
        )
        bundles.append({
            "scope": v["scope"],
            "clock": clock,
            "valid": v["path"],
            "ready": match["path"],
            "payload": payload,
            "confidence": confidence,
            "rationale": rationale,
            "needs": needs,
        })

    # Rank by confidence, then shallower scope first (a top-level interface net
    # beats the same channel seen again as a per-instance port), then path for
    # stability. (Payload count is NOT a ranking term — more payload often just
    # means more same-scope noise.)
    rank = {"high": 0, "medium": 1, "low": 2}
    bundles.sort(key=lambda b: (rank[b["confidence"]], b["valid"].count("."), b["valid"]))
    return bundles


def _pick_clock(clocks: list[dict], scope: str) -> str | None:
    """Prefer a clock in the same scope, else the nearest ancestor scope."""
    same = [c for c in clocks if c["scope"] == scope]
    if same:
        return same[0]["path"]
    ancestors = [c for c in clocks if scope.startswith(c["scope"] + ".") or c["scope"] == ""]
    if ancestors:
        # nearest ancestor = longest scope prefix
        ancestors.sort(key=lambda c: len(c["scope"]), reverse=True)
        return ancestors[0]["path"]
    return None


def _pick_payload(scope_sigs: list[dict], stem: str, cap: int) -> list[str]:
    """Same-scope buses (width>1) that aren't handshake/clock/reset signals,
    preferring those sharing the channel stem prefix."""
    cands = [
        s for s in scope_sigs
        if s["width"] > 1 and s["role"] not in ("valid", "ready", "clock", "reset")
        and s.get("var_type") not in _NON_SIGNAL_VARTYPES
    ]
    if stem:
        prefixed = [s for s in cands if s["leaf"].startswith(stem)]
        if prefixed:
            cands = prefixed
    cands.sort(key=lambda s: (-s["width"], s["leaf"]))
    return [s["path"] for s in cands[:cap]]


# Keywords used to gather the candidate signal set from a parser.
_GATHER_KEYWORDS = (*VALID_MARKERS, *READY_MARKERS, *CLOCK_TOKENS)

_COMMON_PROTOCOL_ROLES = {"clk", "clock", "rst", "reset", "resetn", "rst_n"}
_AHB_ROLES = {
    "htrans", "hready", "hreadyout", "haddr", "hwrite", "hsize", "hburst",
    "hprot", "hwdata", "hrdata", "hresp", "hclk", "hresetn", "hreset",
} | _COMMON_PROTOCOL_ROLES
# Address-phase signals ONLY. inspect_handshake's payload-hold runs during an
# address-phase stall (htrans-derived valid high, HREADY low), where AHB requires
# HADDR + control to stay stable — so a hold violation on these is a true positive.
# HWDATA/HRDATA are DATA-phase signals: they belong to the *previous* transfer's
# data phase and legitimately change a cycle later, so checking their hold during
# an address-phase stall is phase-mismatched and would false-positive on any
# multi-cycle stall. They are still discovered (in _AHB_ROLES) but never enter the
# hold check. Data-integrity-during-wait belongs to reconstruct_transactions /
# verify_window, not here.
_AHB_PAYLOAD_ROLES = ("haddr", "hwrite", "hsize", "hburst", "hprot")
_APB_ROLES = {
    "psel", "penable", "pready", "paddr", "pwrite", "pwdata", "pstrb",
    "pprot", "prdata", "pslverr", "pclk", "presetn", "preset",
} | _COMMON_PROTOCOL_ROLES
_APB_PAYLOAD_ROLES = ("paddr", "pwrite", "pwdata", "pstrb", "pprot")
_PROTOCOL_GATHER_KEYWORDS = {
    "ahb": tuple(sorted(_AHB_ROLES | set(CLOCK_TOKENS) | set(RESET_TOKENS))),
    "apb": tuple(sorted(_APB_ROLES | set(CLOCK_TOKENS) | set(RESET_TOKENS))),
}


def suggest_handshakes(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    scope: str | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Scan a waveform and propose inspect_handshake bundles. Reads existing
    waveforms only — does NOT rerun simulation."""
    parser = get_parser(wave_path)
    sigs: dict[str, dict] = {}

    def _add(results: list[dict]) -> None:
        for r in results or []:
            p = r.get("path")
            if p and p not in sigs:
                sigs[p] = r

    # Pass 1: gather handshake + clock candidates by role keyword.
    for kw in _GATHER_KEYWORDS:
        try:
            _add(parser.search_signals(kw).get("results", []))
        except Exception:
            continue

    # Pass 2: for each scope that has a valid/ready pair, pull siblings so the
    # payload buses (which won't match a role keyword) are visible to the core.
    # Search by the FULL scope path, not just its leaf name: a leaf like
    # "axi4_master_drv_bfm_h" repeats across every instance, so a leaf keyword
    # under search_signals' result cap returns only the first instance's
    # siblings and deeper instances' payload buses (e.g. master[3].wdata) are
    # silently dropped -> empty payload -> payload-hold never runs on them.
    seed = propose_handshake_bundles(list(sigs.values()), scope=scope)
    searched_scopes: set[str] = set()
    for b in seed:
        scope_path = b["scope"]
        if scope_path and scope_path not in searched_scopes:
            searched_scopes.add(scope_path)
            try:
                _add(parser.search_signals(scope_path, max_results=512).get("results", []))
            except Exception:
                pass

    bundles = propose_handshake_bundles(list(sigs.values()), scope=scope)
    return {
        "wave_path": wave_path,
        "scope": scope,
        "candidate_count": len(bundles),
        "candidates": bundles[:max_candidates],
        "reason": None if bundles else _empty_result_reason(parser, wave_path, scope),
    }


def _probe_present(parser: Any, token: str, scope: str | None) -> bool:
    """True if a signal whose leaf is exactly ``token`` (or ``*_token``/``*.token``)
    exists under ``scope``. Pure name-existence fact via search_signals."""
    try:
        results = parser.search_signals(token).get("results", [])
    except Exception:
        return False
    for r in results:
        p = r.get("path", "")
        if scope and not p.startswith(scope):
            continue
        leaf = _leaf(p).lower()
        if leaf == token or leaf.endswith("_" + token) or leaf.endswith("." + token):
            return True
    return False


def _empty_result_reason(parser: Any, wave_path: str, scope: str | None) -> str:
    """Empty-result hint for suggest_handshakes. A lightweight mechanical probe
    for the strongest bus-family discriminators (htrans → AHB; psel+penable →
    APB) upgrades the generic pointer into a copy-paste-ready
    suggest_protocol_bundles call. Detection is name-existence fact, never a
    guess — if no discriminator matches we keep the generic pointer rather than
    asserting a protocol type."""
    base = "no valid/ready handshake pairs found by name" + (
        f" under scope {scope}" if scope else ""
    )
    has_ahb = _probe_present(parser, "htrans", scope)
    has_apb = _probe_present(parser, "psel", scope) and _probe_present(parser, "penable", scope)
    scope_arg = f', scope="{scope}"' if scope else ""
    cmds: list[str] = []
    if has_ahb:
        cmds.append(f'suggest_protocol_bundles(wave_path="{wave_path}", protocol="ahb"{scope_arg})')
    if has_apb:
        cmds.append(f'suggest_protocol_bundles(wave_path="{wave_path}", protocol="apb"{scope_arg})')
    if cmds:
        family = "AHB/APB" if len(cmds) > 1 else ("AHB" if has_ahb else "APB")
        return (
            base
            + f" — this looks like an {family} bus (no literal valid signal). Run: "
            + "  ".join(cmds)
        )
    return (
        base
        + " — for AHB/APB use suggest_protocol_bundles (there is no literal valid signal)."
    )


def propose_protocol_bundles(
    signals: list[dict],
    protocol: str,
    scope: str | None = None,
    max_payload: int = 8,
) -> list[dict]:
    """Pure core for protocol-specific bundle discovery.

    Direction tags are discovery-layer facts only. They come from mechanical
    name/hierarchy markers such as ``mst_``/``slv_``; absent or conflicting
    markers degrade to ``unknown`` rather than guessing protocol ownership.
    """
    protocol = protocol.lower()
    if protocol not in ("ahb", "apb"):
        raise ValueError("protocol must be 'ahb' or 'apb'")

    records = [_protocol_rec(s) for s in signals if s.get("path")]
    records = [r for r in records if r is not None]
    if scope:
        records = [r for r in records if r["path"].startswith(scope)]

    if protocol == "ahb":
        bundles = _propose_ahb_bundles(records, max_payload=max_payload)
    else:
        bundles = _propose_apb_bundles(records, max_payload=max_payload)

    rank = {"high": 0, "medium": 1, "low": 2}
    bundles.sort(key=lambda b: (
        rank.get(b["confidence"], 9),
        b["scope"].count("."),
        b.get("valid_htrans") or b.get("psel") or "",
    ))
    return bundles


def _protocol_rec(s: dict) -> dict | None:
    path = s.get("path")
    if not path:
        return None
    leaf = _leaf(path)
    return {
        "path": path,
        "leaf": leaf,
        "leaf_lower": leaf.lower(),
        "width": int(s.get("width") or 1),
        "scope": _parent(path),
        "var_type": (s.get("var_type") or "").lower(),
        "direction": (s.get("direction") or "").lower(),
    }


def _role_for(leaf_lower: str, roles: set[str]) -> str | None:
    for role in sorted(roles, key=len, reverse=True):
        if leaf_lower == role or leaf_lower.endswith("_" + role) or leaf_lower.endswith("." + role):
            return role
    return None


def _prefix_for_role(leaf_lower: str, role: str) -> str:
    if leaf_lower == role:
        return ""
    for sep in ("_", "."):
        suffix = sep + role
        if leaf_lower.endswith(suffix):
            return leaf_lower[: -len(suffix) + 1]
    return ""


def _same_prefix(a: dict, b: dict, role_b: str) -> bool:
    return _prefix_for_role(a["leaf_lower"], a["role"]) == _prefix_for_role(b["leaf_lower"], role_b)


def _by_scope_and_role(records: list[dict], roles: set[str]) -> dict[str, dict[str, list[dict]]]:
    grouped: dict[str, dict[str, list[dict]]] = {}
    for rec in records:
        role = _role_for(rec["leaf_lower"], roles)
        if role is None:
            continue
        rec = {**rec, "role": role}
        grouped.setdefault(rec["scope"], {}).setdefault(role, []).append(rec)
    return grouped


def _pick_protocol_clock(scope_records: dict[str, list[dict]], clock_roles: tuple[str, ...]) -> str | None:
    for role in clock_roles:
        vals = scope_records.get(role, [])
        if vals:
            return vals[0]["path"]
    all_clocks = []
    for role in clock_roles:
        all_clocks.extend(scope_records.get(role, []))
    return all_clocks[0]["path"] if all_clocks else None


def _pick_protocol_payload(
    scope_records: dict[str, list[dict]],
    source: dict,
    payload_roles: tuple[str, ...],
    cap: int,
) -> list[str]:
    payload: list[str] = []
    source_prefix = _prefix_for_role(source["leaf_lower"], source["role"])
    for role in payload_roles:
        candidates = scope_records.get(role, [])
        if source_prefix:
            prefixed = [c for c in candidates if _prefix_for_role(c["leaf_lower"], role) == source_prefix]
            candidates = prefixed or candidates
        for c in candidates:
            if c["path"] not in payload:
                payload.append(c["path"])
            if len(payload) >= cap:
                return payload
    return payload


def _direction_from_names(paths: list[str]) -> tuple[str, str, str, list[str]]:
    initiator_markers: list[str] = []
    responder_markers: list[str] = []
    for path in paths:
        text = path.lower()
        init_match = (
            re.search(r"(^|[._])(mst|master|mstr)([._]|$)", text)
            or re.search(r"(^|[._])m_(ahb|apb)([._]|$)", text)
        )
        resp_match = (
            re.search(r"(^|[._])(slv|slave)([._]|$)", text)
            or re.search(r"(^|[._])s_(ahb|apb)([._]|$)", text)
        )
        if init_match:
            initiator_markers.append(init_match.group(0).strip("._"))
        if resp_match:
            responder_markers.append(resp_match.group(0).strip("._"))

    if initiator_markers and not responder_markers:
        return "initiator_side", f"name_marker:{initiator_markers[0]}", "high", []
    if responder_markers and not initiator_markers:
        return "responder_side", f"name_marker:{responder_markers[0]}", "high", []
    if initiator_markers and responder_markers:
        return "unknown", "conflicting_name_markers", "unknown", [
            "direction marker conflict; caller must not treat this bundle as side-qualified"
        ]
    return "unknown", "no mechanical side marker found", "unknown", [
        "direction unknown; caller must state this rather than infer a side"
    ]


def _pick_prefixed_path(roles: dict, role: str, anchor: dict) -> str | None:
    """The role-signal sharing ``anchor``'s interface prefix (else the first), or
    None if absent — used to attach HWRITE/HWDATA to the right AHB interface."""
    cands = roles.get(role, [])
    if not cands:
        return None
    prefixed = [c for c in cands if _same_prefix(anchor, c, c["role"])]
    return (prefixed or cands)[0]["path"]


def _propose_ahb_bundles(records: list[dict], max_payload: int) -> list[dict]:
    grouped = _by_scope_and_role(records, _AHB_ROLES)
    bundles: list[dict] = []
    for scope_name, roles in grouped.items():
        for htrans in roles.get("htrans", []):
            ready_candidates = roles.get("hreadyout", []) + roles.get("hready", [])
            if not ready_candidates:
                continue
            prefixed = [r for r in ready_candidates if _same_prefix(htrans, r, r["role"])]
            ready = (prefixed or ready_candidates)[0]
            clock = _pick_protocol_clock(roles, ("hclk", "clk", "clock"))
            reset = _pick_protocol_clock(roles, ("hresetn", "hreset", "resetn", "rst_n", "reset", "rst"))
            payload = _pick_protocol_payload(roles, htrans, _AHB_PAYLOAD_ROLES, max_payload)
            # HWRITE (qualifier) + HWDATA (data bus) enable the write data-phase
            # HWDATA-hold check, which is a *different* window than payload-hold —
            # so HWDATA is supplied here, not in the address-phase `payload`.
            hwrite_sig = _pick_prefixed_path(roles, "hwrite", htrans)
            hwdata_sig = _pick_prefixed_path(roles, "hwdata", htrans)
            side_paths = [htrans["path"], ready["path"], *payload]
            direction_tag, direction_basis, direction_confidence, warnings = _direction_from_names(side_paths)
            needs = []
            if clock is None:
                needs.append("clock")
            confidence = "high" if clock and payload else "medium" if clock else "low"
            inspect_args = None if clock is None else {
                "clock": clock,
                "valid_htrans": htrans["path"],
                "htrans_rule": "active",
                "ready": ready["path"],
                "payload": payload,
            }
            if inspect_args is not None:
                if hwrite_sig:
                    inspect_args["hwrite"] = hwrite_sig
                if hwdata_sig:
                    inspect_args["write_data"] = hwdata_sig
            bundles.append({
                "protocol": "ahb",
                "scope": scope_name,
                "direction_tag": direction_tag,
                "direction_basis": direction_basis,
                "direction_confidence": direction_confidence,
                "clock": clock,
                "reset": reset,
                "valid_htrans": htrans["path"],
                "htrans_rule": "active",
                "psel": None,
                "penable": None,
                "ready": ready["path"],
                "payload": payload,
                "hwrite": hwrite_sig,
                "write_data": hwdata_sig,
                "inspect_handshake_args": inspect_args,
                "confidence": confidence,
                "rationale": f"AHB htrans/ready pair in scope {scope_name or '(top)'}",
                "needs": needs,
                "warnings": warnings,
            })
    return bundles


def _propose_apb_bundles(records: list[dict], max_payload: int) -> list[dict]:
    grouped = _by_scope_and_role(records, _APB_ROLES)
    bundles: list[dict] = []
    for scope_name, roles in grouped.items():
        for psel in roles.get("psel", []):
            ready_candidates = roles.get("pready", [])
            if not ready_candidates:
                continue
            prefixed = [r for r in ready_candidates if _same_prefix(psel, r, r["role"])]
            ready = (prefixed or ready_candidates)[0]
            penable_candidates = roles.get("penable", [])
            prefixed_penable = [p for p in penable_candidates if _same_prefix(psel, p, p["role"])]
            penable = (prefixed_penable or penable_candidates or [None])[0]
            clock = _pick_protocol_clock(roles, ("pclk", "clk", "clock"))
            reset = _pick_protocol_clock(roles, ("presetn", "preset", "resetn", "rst_n", "reset", "rst"))
            payload = _pick_protocol_payload(roles, psel, _APB_PAYLOAD_ROLES, max_payload)
            side_paths = [psel["path"], ready["path"], *(payload or [])]
            if penable:
                side_paths.append(penable["path"])
            direction_tag, direction_basis, direction_confidence, warnings = _direction_from_names(side_paths)
            needs = ["derived_valid_signal_for_psel_and_penable"]
            if clock is None:
                needs.append("clock")
            if penable is None:
                needs.append("penable")
            confidence = "high" if clock and penable and payload else "medium" if clock else "low"
            bundles.append({
                "protocol": "apb",
                "scope": scope_name,
                "direction_tag": direction_tag,
                "direction_basis": direction_basis,
                "direction_confidence": direction_confidence,
                "clock": clock,
                "reset": reset,
                "valid_htrans": None,
                "htrans_rule": None,
                "psel": psel["path"],
                "penable": penable["path"] if penable else None,
                "ready": ready["path"],
                "payload": payload,
                "inspect_handshake_args": None,
                "confidence": confidence,
                "rationale": (
                    f"APB psel/penable/pready bundle in scope {scope_name or '(top)'}; "
                    "inspect_handshake needs an explicit derived valid signal for psel && penable"
                ),
                "needs": needs,
                "warnings": warnings,
            })
    return bundles


def suggest_protocol_bundles(
    *,
    get_parser: Callable[[str], Any],
    wave_path: str,
    protocol: str,
    scope: str | None = None,
    max_candidates: int = 8,
) -> dict[str, Any]:
    """Scan a waveform for AHB/APB protocol bundles. Reads existing waveforms
    only — does NOT rerun simulation."""
    protocol = protocol.lower()
    if protocol not in _PROTOCOL_GATHER_KEYWORDS:
        raise ValueError("protocol must be 'ahb' or 'apb'")

    parser = get_parser(wave_path)
    sigs: dict[str, dict] = {}

    def _add(results: list[dict]) -> None:
        for r in results or []:
            p = r.get("path")
            if p and p not in sigs:
                sigs[p] = r

    for kw in _PROTOCOL_GATHER_KEYWORDS[protocol]:
        try:
            _add(parser.search_signals(kw).get("results", []))
        except Exception:
            continue

    bundles = propose_protocol_bundles(list(sigs.values()), protocol=protocol, scope=scope)
    candidates = bundles[:max_candidates]
    return {
        "wave_path": wave_path,
        "protocol": protocol,
        "scope": scope,
        "candidate_count": len(bundles),
        "candidates": candidates,
        "reason": None if bundles else (
            f"no {protocol.upper()} bundles found by protocol signal names"
            + (f" under scope {scope}" if scope else "")
        ),
        "next_step": _inspect_handshake_relay(wave_path, candidates),
    }


def _inspect_handshake_relay(wave_path: str, bundles: list[dict]) -> str | None:
    """Build a copy-paste-ready inspect_handshake call for each candidate that
    already carries ``inspect_handshake_args`` (AHB candidates with a clock).
    Discovery only LOCATES the interface; this spells out the analysis step at
    the one point the args exist. APB candidates carry no args (they need a
    derived valid first), so they produce no relay line. Boundary-safe: advances
    the analysis, never asserts a side or a root cause."""
    cmds: list[str] = []
    for b in bundles:
        args = b.get("inspect_handshake_args")
        if not args:
            continue
        payload = args.get("payload") or []
        cmds.append(
            f'inspect_handshake(wave_path="{wave_path}", '
            f'clock="{args.get("clock")}", '
            f'valid_htrans="{args.get("valid_htrans")}", '
            f'htrans_rule="{args.get("htrans_rule", "active")}", '
            f'ready="{args.get("ready")}", '
            f'payload={payload!r})'
        )
    if not cmds:
        return None
    return (
        "Discovery only LOCATED the interface(s) above — this is not yet an "
        "analysis. NEXT, run inspect_handshake on each to get the cycle-by-cycle "
        "handshake classification (stalls, payload-hold during wait states) that "
        "discovery cannot give. Copy-paste:\n" + "\n".join(cmds)
    )
