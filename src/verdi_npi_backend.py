"""
verdi_npi_backend.py
NPI-backed connectivity backend. Wraps a fallback (typically Static) and
returns to it on any NPI failure. Never crashes the MCP server.

Design:
- Lazy: pynpi imported on first call requiring NPI; npisys.init / load_design
  triggered on first call with a valid kdb_path.
- Reentrant: load_design can be re-called for a different kdb_path; failure
  to load a new design tries to restore the previous one.
- Defensive: every NPI call site wrapped in try/except. Any failure flips
  state to "failed" for the current request and delegates to fallback.
- Path-normalized: synthesized PinHdl paths truncated at first ':' so the
  scope returned to LLMs is FSDB-compatible. Raw form preserved in expr.
"""

from __future__ import annotations

import ctypes
import logging
import os
import sys
from typing import Any

from .compile_log_parser import parse_compile_log
from .connectivity_backend import StaticConnectivityBackend
from .verdi_backend import probe_verdi_backend


_LOG = logging.getLogger(__name__)

# Process-level guard: NPI's npisys.init is non-reentrant. The native
# library prints "Repeated npi_init ... ignored until npi_end" and
# returns 0 on the second call even though the previously loaded
# design is still queryable. We key the guard on ``id(npisys)`` so
# unit tests that swap in a mock npisys do not leak init-state into
# subsequent integration tests with the real native module.
_NPI_INITIALIZED_IDS: set[int] = set()


def _import_pynpi() -> tuple[Any, Any] | None:
    """Locate and import pynpi using ``$VERDI_HOME``.

    Mirrors the `fsdb_parser._load_wrapper` discipline: derive every
    Verdi-specific path from ``VERDI_HOME`` so the codebase has zero
    hardcoded installation prefixes. Returns ``None`` (without raising)
    when VERDI_HOME is unset, the pynpi tree is missing, or the import
    itself fails — caller is expected to fall back to the static
    backend in those cases.
    """
    verdi_home = os.environ.get("VERDI_HOME")
    if not verdi_home:
        _LOG.info("VERDI_HOME unset; skipping NPI backend.")
        return None
    pynpi_dir = os.path.join(verdi_home, "share", "NPI", "python")
    if not os.path.isdir(pynpi_dir):
        _LOG.info("NPI tree absent at %s; skipping NPI backend.", pynpi_dir)
        return None
    if pynpi_dir not in sys.path:
        sys.path.insert(0, pynpi_dir)

    # Pre-load NPI shared libs with RTLD_GLOBAL so the SWIG-wrapped
    # `_*.so` extensions resolve their dependencies even when the user
    # has not exported LD_LIBRARY_PATH.
    npi_lib_dir = os.path.join(verdi_home, "share", "NPI", "lib", "LINUX64")
    if os.path.isdir(npi_lib_dir):
        for lib in ("libNPI.so", "libnpiL1.so"):
            lib_path = os.path.join(npi_lib_dir, lib)
            if not os.path.exists(lib_path):
                continue
            try:
                ctypes.CDLL(lib_path, ctypes.RTLD_GLOBAL)
            except OSError as exc:
                _LOG.info("Failed to preload %s: %s", lib_path, exc)

    try:
        from pynpi import npisys, netlist  # type: ignore
    except ImportError as exc:
        _LOG.info("pynpi import failed: %s", exc)
        return None
    return (npisys, netlist)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class VerdiNpiBackend:
    """NPI backend with internal Static fallback."""

    name = "verdi_npi"

    def __init__(self, fallback: StaticConnectivityBackend | None = None):
        self._fallback = fallback or StaticConnectivityBackend()
        self._state: str = "uninit"  # uninit | ready | failed
        self._loaded_kdb: str | None = None
        self._loaded_top: str | None = None
        self._npi_modules: tuple[Any, Any] | None = None  # (npisys, netlist)

    # ── public API matching ConnectivityBackend ────────────────────────

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
        # Driver-side NPI integration deferred to a follow-up session;
        # the path-normalization rules differ subtly from the load side.
        return self._fallback.find_driver(
            signal_path,
            wave_path,
            compile_log,
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
        try:
            compile_result = parse_compile_log(compile_log, simulator)
            kdb_path = self._kdb_path_from(compile_result, compile_log)
            top = top_hint or self._top_from(compile_result)
            if not kdb_path or not top:
                return self._fallback_with_reason(
                    signal_path, compile_log, top_hint, max_depth,
                    include_expr, kind_filter, simulator, "kdb_or_top_missing"
                )
            if not self._ensure_loaded(kdb_path, top):
                return self._fallback_with_reason(
                    signal_path, compile_log, top_hint, max_depth,
                    include_expr, kind_filter, simulator, "npi_load_failed"
                )
            return self._npi_find_loads(
                signal_path, compile_result, kdb_path, top,
                include_expr, kind_filter,
            )
        except Exception as exc:  # noqa: BLE001 - never crash the MCP server
            _LOG.warning("VerdiNpiBackend.find_loads failed: %s", exc)
            return self._fallback_with_reason(
                signal_path, compile_log, top_hint, max_depth,
                include_expr, kind_filter, simulator, f"exception: {exc}"
            )

    # ── lifecycle ─────────────────────────────────────────────────────

    def _ensure_loaded(self, kdb_path: str, top: str) -> bool:
        if self._state == "ready" and self._loaded_kdb == kdb_path and self._loaded_top == top:
            return True
        if self._state == "failed":
            return False
        if self._npi_modules is None:
            modules = _import_pynpi()
            if modules is None:
                self._state = "failed"
                return False
            self._npi_modules = modules

        npisys, _ = self._npi_modules
        try:
            npisys_id = id(npisys)
            if npisys_id not in _NPI_INITIALIZED_IDS:
                # init may return 0 if NPI was already initialised by
                # someone else in this process; trust load_design to
                # surface real failures.
                npisys.init(["traceweave_npi"])
                _NPI_INITIALIZED_IDS.add(npisys_id)

            old_kdb, old_top = self._loaded_kdb, self._loaded_top
            rc = npisys.load_design([
                "traceweave_npi",
                "-simflow", "-dbdir", kdb_path,
                "-top", top,
            ])
            if rc != 1:
                # Failed load wipes the previously loaded case in NPI.
                # Best-effort restore so subsequent calls can still hit cache.
                if old_kdb and old_top:
                    npisys.load_design([
                        "traceweave_npi",
                        "-simflow", "-dbdir", old_kdb,
                        "-top", old_top,
                    ])
                return False
            self._state = "ready"
            self._loaded_kdb = kdb_path
            self._loaded_top = top
            return True
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("npisys.load_design crashed: %s", exc)
            self._state = "failed"
            return False

    # ── querying ──────────────────────────────────────────────────────

    def _npi_find_loads(
        self,
        signal_path: str,
        compile_result: dict[str, Any],
        kdb_path: str,
        top: str,
        include_expr: bool,
        kind_filter: list[str] | None,
    ) -> dict[str, Any]:
        _, netlist = self._npi_modules  # type: ignore[misc]
        net = self._resolve_net(netlist, signal_path)
        rtl_name = signal_path.split(".")[-1]
        instance_path = signal_path.rsplit(".", 1)[0] if "." in signal_path else signal_path
        result: dict[str, Any] = {
            "signal_path": signal_path,
            "resolved_rtl_name": rtl_name,
            "resolved_module": top,
            "resolved_instance_path": instance_path,
            "loads": [],
            "completeness": "exact",
            "stopped_at": None,
            "unsupported_reason": None,
        }
        if net is None:
            result["stopped_at"] = "signal_path_unresolved_in_npi"
            return result

        try:
            raw_loads = net.load_list() or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("net.load_list crashed for %s: %s", signal_path, exc)
            result["stopped_at"] = "npi_load_list_failed"
            return result

        keep = set(kind_filter) if kind_filter else None
        loads: list[dict[str, Any]] = []
        for hdl in raw_loads:
            entry = self._format_load(hdl, include_expr=include_expr)
            if entry is None:
                continue
            if keep is not None and entry["kind"] not in keep:
                continue
            loads.append(entry)
        result["loads"] = _dedup(loads)
        if not loads:
            result["stopped_at"] = "no_npi_loads"
        return result

    def _format_load(self, hdl: Any, include_expr: bool) -> dict[str, Any] | None:
        try:
            raw = hdl.full_name() if hasattr(hdl, "full_name") else None
            t = hdl.type() if hasattr(hdl, "type") else None
        except Exception:
            return None
        if not raw:
            return None
        scope = _scope_from_synthesized(raw)
        kind = _classify_load_kind(raw, t)
        src = _safe_src_info(hdl)
        line = src.get("line")
        if line is None:
            line = _line_from_synthesized(raw)
        return {
            "load_path": scope,
            "kind": kind,
            "expr": raw if include_expr else None,
            # NPI does not currently surface a file path for synthesized
            # constructs; line is recoverable from the synthesized name.
            "source_file": src.get("file"),
            "source_line": line,
            "backend": "verdi_npi",
            "confidence": "exact",
            # ``_npi_raw`` is for dedup only — stripped before schema
            # validation in the dispatch layer.
            "_npi_raw": raw,
        }

    def _resolve_net(self, netlist: Any, signal_path: str) -> Any:
        try:
            net = netlist.get_net(signal_path)
            if net is not None:
                return net
        except Exception:
            pass
        if "[" in signal_path:
            try:
                return netlist.get_actual_net(signal_path)
            except Exception:
                return None
        return None

    # ── fallback / helpers ────────────────────────────────────────────

    def _fallback_with_reason(
        self,
        signal_path: str,
        compile_log: str,
        top_hint: str | None,
        max_depth: int,
        include_expr: bool,
        kind_filter: list[str] | None,
        simulator: str,
        reason: str,
    ) -> dict[str, Any]:
        result = self._fallback.find_loads(
            signal_path,
            compile_log,
            top_hint=top_hint,
            max_depth=max_depth,
            include_expr=include_expr,
            kind_filter=kind_filter,
            simulator=simulator,
        )
        # Tag the reason so the dispatch layer can surface it through
        # backend_status if desired (not surfaced today; informational).
        result.setdefault("_npi_fallback_reason", reason)
        return result

    @staticmethod
    def _kdb_path_from(compile_result: dict[str, Any], compile_log: str) -> str | None:
        status = probe_verdi_backend(compile_result, compile_log_path=compile_log)
        return status.get("kdb_path")

    @staticmethod
    def _top_from(compile_result: dict[str, Any]) -> str | None:
        tops = compile_result.get("top_modules") or []
        return tops[0] if tops else None


# ---------------------------------------------------------------------------
# Path normalization helpers
# ---------------------------------------------------------------------------


def _scope_from_synthesized(npi_path: str) -> str:
    """Truncate at first ':' to get the user-visible scope.

    Synthesized PinHdl names are `<scope>:<construct>...:<cell>.<port>`
    where `<scope>` is the FSDB-visible module instance path.
    """
    return npi_path.split(":", 1)[0]


def _line_from_synthesized(npi_path: str) -> int | None:
    """Parse the start line out of a synthesized PinHdl name.

    Format: ``<scope>:<construct><idx>#<inner><idx>:<line_start>:<line_end>:<cell>.<port>``
    The two integer fields after the construct identifier are start and
    end line; we surface start line only.
    """
    if ":" not in npi_path:
        return None
    parts = npi_path.split(":")
    for tok in parts:
        if tok.isdigit():
            return int(tok)
    return None


def _classify_load_kind(npi_path: str, hdl_type: str | None) -> str:
    """Best-effort classification mapping NPI to the existing kind enum.

    Synthesized cell names (containing ':') are RHS-expression consumers
    in the elaborated netlist. Sensitivity and rhs_expr are
    indistinguishable in NPI without further introspection; default to
    'rhs_expr' as the closer match. Module-level instance ports map to
    'module_input'.
    """
    if ":" in npi_path:
        return "rhs_expr"
    return "module_input"


def _safe_src_info(hdl: Any) -> dict[str, Any]:
    if not hasattr(hdl, "src_info"):
        return {}
    try:
        info = hdl.src_info()
    except Exception:
        return {}
    if info is None:
        return {}
    if isinstance(info, dict):
        return info
    # src_info may return a tuple/list (file, begin_line, end_line)
    if isinstance(info, (list, tuple)) and info:
        out: dict[str, Any] = {}
        if len(info) >= 1:
            out["file"] = str(info[0]) if info[0] else None
        if len(info) >= 2:
            try:
                out["line"] = int(info[1])
            except (TypeError, ValueError):
                out["line"] = None
        return out
    return {}


def _dedup(loads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Dedup using the raw synthesized name when available.

    Two NPI loads share the same user-visible scope but live on
    different cells (e.g. multiple muxes in the same module reading the
    same signal). Use the raw NPI path as the disambiguator and drop it
    from the entry before returning.
    """
    seen: set[tuple[str, str, str]] = set()
    out: list[dict[str, Any]] = []
    for entry in loads:
        raw = entry.pop("_npi_raw", entry.get("expr") or "")
        key = (entry["load_path"], entry["kind"], raw)
        if key in seen:
            continue
        seen.add(key)
        out.append(entry)
    return out
