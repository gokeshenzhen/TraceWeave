"""
connectivity_backend.py
Backend abstraction for driver / load / path (connectivity) queries.

Two backends are wired: ``StaticConnectivityBackend`` (pure-Python
source-regex, always available) and ``VerdiNpiBackend`` (NPI-backed,
selected when a Verdi KDB is detected). They share one Protocol so the
MCP-tool dispatch layer never needs to branch on backend.

Design intent: backend selection happens at the dispatch site (server.py)
based on probe_verdi_backend status — not inside individual scanners.
The NPI backend wraps Static internally and degrades to it on any
per-call failure for driver/load queries; ``find_path`` is NPI-only and
returns a structured ``static_backend_no_path_api`` when no KDB is
present (no honest source-regex equivalent exists).
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from .signal_driver import explain_signal_driver
from .signal_load import find_signal_loads


@runtime_checkable
class ConnectivityBackend(Protocol):
    """Protocol shared by Static and (future) VerdiNpiBackend."""

    name: str

    def find_driver(
        self,
        signal_path: str,
        wave_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        recursive: bool = False,
        max_depth: int = 10,
        simulator: str = "auto",
    ) -> dict[str, Any]: ...

    def find_loads(
        self,
        signal_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        max_depth: int = 1,
        include_expr: bool = True,
        kind_filter: list[str] | None = None,
        simulator: str = "auto",
    ) -> dict[str, Any]: ...

    def find_path(
        self,
        from_signal: str,
        to_signal: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        expand_assigns: bool = False,
        simulator: str = "auto",
    ) -> dict[str, Any]: ...


class StaticConnectivityBackend:
    """Source-regex backend. Always available; never consumes a license."""

    name = "static"

    def find_driver(
        self,
        signal_path: str,
        wave_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        recursive: bool = False,
        max_depth: int = 10,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        return explain_signal_driver(
            signal_path=signal_path,
            wave_path=wave_path,
            compile_log=compile_log,
            top_hint=top_hint,
            recursive=recursive,
            max_depth=max_depth,
            simulator=simulator,
        )

    def find_loads(
        self,
        signal_path: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        max_depth: int = 1,
        include_expr: bool = True,
        kind_filter: list[str] | None = None,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        return find_signal_loads(
            signal_path=signal_path,
            compile_log=compile_log,
            top_hint=top_hint,
            max_depth=max_depth,
            include_expr=include_expr,
            kind_filter=kind_filter,
            simulator=simulator,
        )

    def find_path(
        self,
        from_signal: str,
        to_signal: str,
        compile_log: str,
        *,
        top_hint: str | None = None,
        expand_assigns: bool = False,
        simulator: str = "auto",
    ) -> dict[str, Any]:
        # No honest static equivalent: ``sig_to_sig_conn_list`` walks
        # the elaborated netlist across assigns / interface bindings /
        # generates, which source-regex cannot reproduce reliably.
        # Returning a structured unsupported response lets the caller
        # fall back to explain_signal_driver + find_signal_loads.
        return {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": expand_assigns,
            "unsupported_reason": "static_backend_no_path_api",
        }


def select_backend(backend_status: dict[str, Any]) -> ConnectivityBackend:
    """Pick the active backend based on probe output.

    If a usable KDB is present, return a VerdiNpiBackend wrapping a
    Static fallback. The wrapped backend handles its own fallback on
    NPI failures (import error, license unavailable, load_design
    rejection, query exception) so the dispatch layer never needs to
    distinguish.

    If no KDB is detected, the Static backend is returned directly —
    starting NPI without a design to load would just consume a license
    for nothing.
    """
    if backend_status.get("kdb_flow", "none") != "none" and backend_status.get("kdb_path"):
        # Imported lazily so callers without verdi never trigger the
        # pynpi import path (and the import itself may itself fail).
        from .verdi_npi_backend import VerdiNpiBackend  # noqa: PLC0415
        return VerdiNpiBackend(StaticConnectivityBackend())
    return StaticConnectivityBackend()
