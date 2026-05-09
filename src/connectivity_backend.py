"""
connectivity_backend.py
Backend abstraction for driver / load (connectivity) queries.

Today only the Static backend is wired. Session 3 will introduce a
VerdiNpiBackend that consumes the same protocol so the MCP-tool layer
does not need to change shape when the new backend lands.

Design intent: backend selection happens at the dispatch site (server.py)
based on probe_verdi_backend status — not inside individual scanners.
StaticBackend is pure-Python source-regex; VerdiNpiBackend will hold a
loaded NPI design and a path-normalization layer.
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


def select_backend(backend_status: dict[str, Any]) -> ConnectivityBackend:
    """Pick the active backend based on probe output.

    Today only StaticConnectivityBackend exists, so this always returns
    Static. The seam is here so session 3 can plug VerdiNpiBackend in
    without touching the MCP dispatch layer.
    """
    # NOTE: VerdiNpiBackend will be selected here when:
    #   backend_status['kdb_flow'] != 'none'
    #   and (npi import succeeds, license available)
    return StaticConnectivityBackend()
