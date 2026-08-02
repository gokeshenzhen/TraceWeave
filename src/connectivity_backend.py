"""
connectivity_backend.py
Backend abstraction for driver / load / path (connectivity) queries.

Three execution shapes are wired: ``StaticConnectivityBackend`` (pure-Python
source-regex, always available), local ``VerdiNpiBackend``, and the optional
LSF-backed NPI wrapper selected by environment policy when a Verdi KDB is
detected. They share one Protocol so MCP tool arguments never branch on
execution placement.

Design intent: backend selection happens at the dispatch site (server.py)
based on probe_verdi_backend status — not inside individual scanners.
The NPI backend normally wraps Static internally and degrades to it on any
per-call failure for driver/load queries.  Production driver/load routing may
instead inject :class:`DeferredConnectivityFallbackBackend` so Source Graph gets
the first fallback opportunity; ``find_path`` is NPI-only and
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


class DeferredConnectivityFallbackBackend:
    """Internal no-I/O fallback used by the public driver/load router.

    Verdi backends historically own their Static fallback.  Injecting this
    placeholder lets them retain that control flow and attach their normal NPI
    failure receipt without running Legacy Static before Source Graph.  These
    placeholder shapes are discarded by the router and never cross MCP schema
    validation.
    """

    name = "source_graph_deferred"

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
        del compile_log, top_hint, max_depth, simulator
        return {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "driver_status": "deferred",
            "recursive": recursive,
            "backend": self.name,
            "_connectivity_fallback_deferred": True,
        }

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
        del (
            compile_log,
            top_hint,
            max_depth,
            include_expr,
            kind_filter,
            simulator,
        )
        return {
            "signal_path": signal_path,
            "resolved_rtl_name": signal_path.rsplit(".", 1)[-1],
            "loads": [],
            "completeness": "deferred",
            "_connectivity_fallback_deferred": True,
        }

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
        del compile_log, top_hint, simulator
        return {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": expand_assigns,
            "unsupported_reason": "connectivity_fallback_deferred",
            "_connectivity_fallback_deferred": True,
        }


def select_backend(
    backend_status: dict[str, Any],
    *,
    fallback: ConnectivityBackend | None = None,
) -> ConnectivityBackend:
    """Pick the active backend based on probe output.

    If a usable KDB is present, return a local VerdiNpiBackend or the opt-in
    LSF wrapper.  The default fallback remains Static for backward-compatible
    path/X-trace callers; driver/load production routing injects a deferred
    fallback so it can attempt Source Graph first.

    If no KDB is detected, the configured fallback is returned directly —
    starting NPI without a design to load would just consume a license
    for nothing.
    """
    fallback_backend = fallback or StaticConnectivityBackend()
    if backend_status.get("kdb_flow", "none") != "none" and backend_status.get(
        "kdb_path"
    ):
        from config import get_npi_execution_config  # noqa: PLC0415

        execution = get_npi_execution_config()
        if execution.mode != "local" or not execution.valid:
            from .npi_lsf import LsfConnectivityBackend  # noqa: PLC0415

            return LsfConnectivityBackend(
                execution,
                fallback=fallback_backend,
            )
        # Imported lazily so callers without verdi never trigger the
        # pynpi import path (and the import itself may itself fail).
        from .verdi_npi_backend import VerdiNpiBackend  # noqa: PLC0415

        return VerdiNpiBackend(fallback_backend)
    return fallback_backend
