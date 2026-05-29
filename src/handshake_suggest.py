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

Scope note: covers AXI ``*valid``/``*ready``, generic ``x_valid``/``x_ready``
and ``req``/``ack`` handshakes. It does NOT synthesise an AHB "valid" (there is
no literal valid signal — it is ``htrans != IDLE``); for AHB pass valid manually.
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

    # Rank by confidence, then path for stability. (Payload count is NOT a
    # ranking term — more payload often just means more same-scope noise.)
    rank = {"high": 0, "medium": 1, "low": 2}
    bundles.sort(key=lambda b: (rank[b["confidence"]], b["valid"]))
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
    seed = propose_handshake_bundles(list(sigs.values()), scope=scope)
    for b in seed:
        leaf = b["scope"].rsplit(".", 1)[-1] if b["scope"] else ""
        if leaf:
            try:
                _add(parser.search_signals(leaf).get("results", []))
            except Exception:
                pass

    bundles = propose_handshake_bundles(list(sigs.values()), scope=scope)
    return {
        "wave_path": wave_path,
        "scope": scope,
        "candidate_count": len(bundles),
        "candidates": bundles[:max_candidates],
        "reason": None if bundles else (
            "no valid/ready handshake pairs found by name"
            + (f" under scope {scope}" if scope else "")
            + " — for AHB pass valid manually (there is no literal valid signal)."
        ),
    }
