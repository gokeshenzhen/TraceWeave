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

import atexit
import contextlib
import ctypes
import logging
import os
import re
import sys
import tempfile
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
_BANNER_SILENCER_INSTALLED = False


def _install_shutdown_banner_silencer() -> None:
    """Hook Python's atexit so Verdi's C-level atexit cannot leak its
    license banner onto fd=1 / fd=2 at process shutdown.

    Verdi's libNPI registers its banner via C's ``atexit()``, which
    runs *after* Python's atexit handlers (Python flushes its own
    cleanup first, then libc handlers fire). By dup'ing fd=1 / fd=2
    onto ``/dev/null`` during our Python atexit handler, the
    subsequent C-level banner write lands on the null device. This is
    the only point in the lifetime where we can shut Verdi up: the
    banner is emitted unconditionally on first init/load_design as a
    pending atexit task, not synchronously during the call.

    Installing the hook is idempotent — call multiple times safely.
    """
    global _BANNER_SILENCER_INSTALLED
    if _BANNER_SILENCER_INSTALLED:
        return

    def _silence_at_shutdown() -> None:
        # Flush any pending Python output *before* swapping fds so the
        # user's last print() / logger output is not lost. After the
        # dup2, the only writers are Verdi's banner and the libc
        # atexit chain, which is exactly what we want to silence.
        try:
            sys.stdout.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            sys.stderr.flush()
        except Exception:  # noqa: BLE001
            pass
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
        except OSError:
            return
        try:
            os.dup2(devnull, 1)
            os.dup2(devnull, 2)
        except OSError:
            pass
        finally:
            try:
                os.close(devnull)
            except OSError:
                pass

    atexit.register(_silence_at_shutdown)
    _BANNER_SILENCER_INSTALLED = True


@contextlib.contextmanager
def _silence_native_stdio():
    """Redirect fd 1 / fd 2 to a temp file for the duration of an NPI call.

    The Verdi NPI runtime writes a license / version banner straight to
    fd=1 the first time ``npisys.init`` or ``load_design`` runs. When
    TraceWeave runs under stdio-based MCP that fd is the JSON-RPC
    channel — any non-JSON byte breaks the protocol and the host
    reports ``Transport closed``. We dup the original fds, swap in a
    temp file, then restore on exit (even on exception).

    Under pytest's default ``fd`` capture mode our dup2 fights with
    pytest's own fd capture and corrupts the captured output stream.
    Skip the swap when pytest is driving so unit tests stay clean —
    the mocked tests never invoke real native code anyway, so there
    is nothing to silence in that environment.
    """
    if "PYTEST_CURRENT_TEST" in os.environ:
        yield None
        return
    saved_out = os.dup(1)
    saved_err = os.dup(2)
    sink = tempfile.TemporaryFile(prefix="traceweave_npi_", suffix=".log")
    try:
        os.dup2(sink.fileno(), 1)
        os.dup2(sink.fileno(), 2)
        try:
            yield sink
        finally:
            os.dup2(saved_out, 1)
            os.dup2(saved_err, 2)
    finally:
        os.close(saved_out)
        os.close(saved_err)
        sink.close()

# Cap how many fan-in boundary points we materialise into a chain. A
# combinational cone of a wide bus can legitimately produce dozens of
# upstream regs; we surface a representative subset so the result stays
# legible without truncating silently for typical signals.
_FAN_IN_MAX_BRANCHES = 32
_FAN_OUT_MAX_BRANCHES = 64


def _module_of(hdl: Any) -> str | None:
    """Best-effort lookup of the module *definition* name owning an NPI handle.

    Walks ``hdl.scope_inst().def_name()`` defensively — NPI handles can
    miss any of those steps depending on the construct. Returns None
    when the chain is unavailable; callers should fall through to other
    sources (e.g. the queried net's parent scope) rather than guess.
    """
    if hdl is None:
        return None
    try:
        scope = hdl.scope_inst() if hasattr(hdl, "scope_inst") else None
    except Exception:
        return None
    if scope is None:
        return None
    try:
        name = scope.def_name() if hasattr(scope, "def_name") else None
    except Exception:
        return None
    return name or None


def _is_boundary_driver(hdl: Any) -> bool:
    """True when an NPI driver pin is a raw hierarchy port, not a real driver.

    NPI returns a port handle for module inputs because the design net
    itself has no in-scope driver — the value crosses an instance
    boundary. Such pins have no synthesized cell tag (no ``:`` in their
    full name). For these we prefer fan_in_reg_list, which walks through
    the boundary.
    """
    try:
        raw = hdl.full_name() if hasattr(hdl, "full_name") else None
    except Exception:
        return False
    if not raw:
        return False
    return ":" not in raw


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

    # Pre-load librt before libNPI: Synopsys's libNPI.so references
    # `shm_unlink` from librt but does not list it as a DT_NEEDED, so
    # dlopen on glibc 2.34+ (where shm_unlink lives in librt.so.1, not
    # libc) raises ``undefined symbol`` at load time. Preloading librt
    # with RTLD_GLOBAL makes the symbol visible to subsequent dlopens
    # of libNPI and the SWIG `_npisys.so` extension. Without this the
    # MCP server segfaults the first time pynpi is imported.
    for librt_name in ("librt.so.1", "librt.so"):
        try:
            ctypes.CDLL(librt_name, ctypes.RTLD_GLOBAL)
            break
        except OSError:
            continue

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
        try:
            compile_result = parse_compile_log(compile_log, simulator)
            kdb_path = self._kdb_path_from(compile_result, compile_log)
            top = top_hint or self._top_from(compile_result)
            if not kdb_path or not top:
                result = self._fallback.find_driver(
                    signal_path, wave_path, compile_log,
                    top_hint=top_hint, recursive=recursive, max_depth=max_depth,
                    simulator=simulator,
                )
                result.setdefault("_npi_fallback_reason", "kdb_or_top_missing")
                return result
            if not self._ensure_loaded(kdb_path, top):
                result = self._fallback.find_driver(
                    signal_path, wave_path, compile_log,
                    top_hint=top_hint, recursive=recursive, max_depth=max_depth,
                    simulator=simulator,
                )
                result.setdefault("_npi_fallback_reason", "npi_load_failed")
                return result
            return self._npi_find_driver(
                signal_path, wave_path, top, recursive=recursive,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("VerdiNpiBackend.find_driver failed: %s", exc)
            result = self._fallback.find_driver(
                signal_path, wave_path, compile_log,
                top_hint=top_hint, recursive=recursive, max_depth=max_depth,
                simulator=simulator,
            )
            result.setdefault("_npi_fallback_reason", f"exception: {exc}")
            return result

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
        try:
            compile_result = parse_compile_log(compile_log, simulator)
            kdb_path = self._kdb_path_from(compile_result, compile_log)
            top = top_hint or self._top_from(compile_result)
            if not kdb_path or not top:
                return self._path_fallback(
                    from_signal, to_signal, expand_assigns,
                    reason="kdb_or_top_missing",
                )
            if not self._ensure_loaded(kdb_path, top):
                return self._path_fallback(
                    from_signal, to_signal, expand_assigns,
                    reason="npi_load_failed",
                )
            return self._npi_find_path(
                from_signal, to_signal, expand_assigns=expand_assigns,
            )
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("VerdiNpiBackend.find_path failed: %s", exc)
            return self._path_fallback(
                from_signal, to_signal, expand_assigns,
                reason=f"exception: {exc}",
            )

    def _path_fallback(
        self,
        from_signal: str,
        to_signal: str,
        expand_assigns: bool,
        *,
        reason: str,
    ) -> dict[str, Any]:
        """Delegate to the static backend (which always returns
        static_backend_no_path_api) and tag the NPI-level reason so the
        dispatch layer can surface it through backend_status."""
        result = self._fallback.find_path(
            from_signal,
            to_signal,
            compile_log="",  # static backend does not consult it
            expand_assigns=expand_assigns,
        )
        result.setdefault("_npi_fallback_reason", reason)
        return result

    def _npi_find_path(
        self,
        from_signal: str,
        to_signal: str,
        *,
        expand_assigns: bool,
    ) -> dict[str, Any]:
        _, netlist = self._npi_modules  # type: ignore[misc]
        base: dict[str, Any] = {
            "from_signal": from_signal,
            "to_signal": to_signal,
            "found": False,
            "hops": 0,
            "path": [],
            "expand_assigns": expand_assigns,
            "unsupported_reason": None,
        }

        from_hdl = self._resolve_net(netlist, from_signal)
        if from_hdl is None:
            base["unsupported_reason"] = "from_not_found"
            return base
        to_hdl = self._resolve_net(netlist, to_signal)
        if to_hdl is None:
            base["unsupported_reason"] = "to_not_found"
            return base

        # Same-net is structurally valid: the user asked about a path
        # between two names that resolve to the same elaborated net
        # (e.g. via get_actual_net alias). Surface it as a one-hop
        # "found" with hops=0 so the caller does not have to special-case.
        if from_hdl == to_hdl:
            base["found"] = True
            base["path"] = [self._format_path_hop(from_hdl, 0, is_endpoint=True)]
            return base

        try:
            with _silence_native_stdio():
                hdl_list = netlist.sig_to_sig_conn_list(
                    from_hdl, to_hdl, assign_cell=expand_assigns,
                ) or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("sig_to_sig_conn_list failed: %s", exc)
            base["unsupported_reason"] = "npi_call_failed"
            base["_npi_call_error"] = str(exc)
            return base

        if not hdl_list:
            base["unsupported_reason"] = "not_connected"
            return base

        last_idx = len(hdl_list) - 1
        base["path"] = [
            self._format_path_hop(
                h, idx, is_endpoint=(idx == 0 or idx == last_idx),
            )
            for idx, h in enumerate(hdl_list)
        ]
        base["found"] = True
        base["hops"] = max(0, len(hdl_list) - 1)
        return base

    def _format_path_hop(
        self,
        net_hdl: Any,
        index: int,
        *,
        is_endpoint: bool,
    ) -> dict[str, Any]:
        try:
            net_path = net_hdl.full_name() if hasattr(net_hdl, "full_name") else None
        except Exception:  # noqa: BLE001
            net_path = None
        scope_inst = _scope_inst_of(net_hdl)
        scope_path: str | None = None
        if scope_inst is not None:
            try:
                scope_path = (
                    scope_inst.full_name()
                    if hasattr(scope_inst, "full_name") else None
                )
            except Exception:  # noqa: BLE001
                scope_path = None
        file_val, line_val = _inst_src_info(scope_inst)
        return {
            "index": index,
            "net_path": net_path or "",
            "scope_inst": scope_path,
            "source_file": file_val,
            "source_line": line_val,
            "is_endpoint": is_endpoint,
        }

    def collect_instance_src_map(
        self,
        compile_log: str,
        simulator: str = "auto",
    ) -> dict[str, tuple[str | None, int | None]]:
        """Walk the elaborated netlist; return ``full_path -> (file, line)``.

        Used by ``build_tb_hierarchy`` to upgrade compile-log-derived
        source info with NPI's elaborated truth. Returns an empty dict
        on any failure path so the caller can keep going without
        annotation. Never raises.
        """
        try:
            compile_result = parse_compile_log(compile_log, simulator)
            kdb_path = self._kdb_path_from(compile_result, compile_log)
            top = self._top_from(compile_result)
            if not kdb_path or not top:
                return {}
            if not self._ensure_loaded(kdb_path, top):
                return {}
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("collect_instance_src_map setup failed: %s", exc)
            return {}

        _, netlist = self._npi_modules  # type: ignore[misc]
        out: dict[str, tuple[str | None, int | None]] = {}
        try:
            with _silence_native_stdio():
                top_list = netlist.get_top_inst_list() or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("get_top_inst_list failed: %s", exc)
            return {}
        for inst in top_list:
            _walk_inst_src(inst, out)
        return out

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
            with _silence_native_stdio():
                if npisys_id not in _NPI_INITIALIZED_IDS:
                    # init may return 0 if NPI was already initialised by
                    # someone else in this process; trust load_design to
                    # surface real failures.
                    npisys.init(["traceweave_npi"])
                    _NPI_INITIALIZED_IDS.add(npisys_id)
                    # Verdi registers a C atexit() that prints its
                    # license banner during process shutdown. Install
                    # our Python atexit hook *after* init so our
                    # fd-redirect runs before Verdi's banner write.
                    _install_shutdown_banner_silencer()

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
            with _silence_native_stdio():
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

        fan_out_loads = self._fan_out_loads(
            net,
            signal_path,
            top,
            include_expr=include_expr,
            max_branches=_FAN_OUT_MAX_BRANCHES,
        )
        if fan_out_loads is not None:
            for entry in fan_out_loads:
                if keep is not None and entry["kind"] not in keep:
                    continue
                loads.append(entry)

        result["loads"] = _dedup(loads)
        if not result["loads"]:
            result["stopped_at"] = "no_npi_loads"
        return result

    def _fan_out_loads(
        self,
        net: Any,
        signal_path: str,
        top: str,
        *,
        include_expr: bool,
        max_branches: int,
    ) -> list[dict[str, Any]] | None:
        """Walk fan-out cone with NPI and format boundary/register loads.

        ``load_list`` reports direct loads only. For module output ports,
        the useful consumers often live across the parent boundary; Verdi's
        ``fan_out_reg_list`` is the matching cone traversal API.
        """
        if not hasattr(net, "fan_out_reg_list"):
            return None
        bound = signal_path.split(".", 1)[0] if "." in signal_path else top
        try:
            with _silence_native_stdio():
                pins = net.fan_out_reg_list(
                    stop_at_pin=True,
                    report_primary_port=True,
                    top_scope_name=bound,
                ) or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("net.fan_out_reg_list failed for %s: %s", signal_path, exc)
            return None

        loads: list[dict[str, Any]] = []
        for pin in pins[:max_branches]:
            entry = self._format_load(pin, include_expr=include_expr)
            if entry is not None:
                loads.append(entry)
        return loads

    def _npi_find_driver(
        self,
        signal_path: str,
        wave_path: str,
        top: str,
        *,
        recursive: bool = False,
    ) -> dict[str, Any]:
        _, netlist = self._npi_modules  # type: ignore[misc]
        rtl_name = signal_path.split(".")[-1]
        instance_path = signal_path.rsplit(".", 1)[0] if "." in signal_path else top
        net = self._resolve_net(netlist, signal_path)
        # Prefer NPI's own scope_inst().def_name() for the module name —
        # ``top`` is only a last-resort placeholder when the net is
        # unresolvable.
        resolved_module = _module_of(net) or top
        base = {
            "signal_path": signal_path,
            "wave_path": wave_path,
            "resolved_rtl_name": rtl_name,
            "resolved_module": resolved_module,
            "resolved_instance_path": instance_path,
            "driver_status": "unsupported",
            "driver_kind": None,
            "source_file": None,
            "source_line": None,
            "expression_summary": None,
            "upstream_signals": [],
            "instance_port_connections": None,
            "confidence": "exact",
            "unsupported_reason": None,
            "stopped_at": None,
            "recursive": recursive,
            "driver_chain": None,
            "chain_summary": None,
            "backend": "verdi_npi",
        }

        if net is None:
            base["driver_status"] = "unsupported"
            base["unsupported_reason"] = "signal_path_unresolved_in_npi"
            base["stopped_at"] = "signal_path_unresolved_in_npi"
            return base

        try:
            with _silence_native_stdio():
                drivers = net.driver_list() or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("net.driver_list crashed for %s: %s", signal_path, exc)
            base["driver_status"] = "unsupported"
            base["unsupported_reason"] = "npi_driver_list_failed"
            base["stopped_at"] = "npi_driver_list_failed"
            return base

        if not drivers:
            base["driver_status"] = "unsupported"
            base["unsupported_reason"] = "no_npi_drivers"
            base["stopped_at"] = "no_npi_drivers"
            return base

        # The net's own loads, used to detect the misattribution where NPI
        # reports a LOAD (or an interface-slice alias of a load) as the driver:
        # the true driver is testbench/behavioral (procedural drive via virtual
        # interface + clocking block), invisible to NPI's RTL fan-in.
        load_raws = self._net_load_raws(net)

        # Load-alias short-circuit BEFORE fan-in. The judgment must be keyed on
        # NPI's ORIGINAL driver_list (what it *claims* drives the net), not on
        # the fan-in result: when the driver_list head is an interface-slice
        # alias of a load and no genuine RTL register driver remains, the net
        # has no RTL driver regardless of recursive/boundary. Deciding here is
        # what covers recursive=True — otherwise fan-in walks across the
        # boundary to a downstream LOAD register (e.g. an AHB matrix lock_owner
        # that merely READS the net) and the (lock_owner, load_list) comparison
        # misses because lock_owner is in fan-OUT, not the net's load_list.
        # FP-safe: a real self-referential counter's driver_list head is a
        # genuine Reg cell that is NOT in its own load_list (the load is a
        # distinct Add/Assignment), so it never short-circuits.
        driver_pairs = [
            (hdl, fmt)
            for hdl, fmt in ((h, self._format_driver(h)) for h in drivers)
            if fmt is not None
        ]
        if driver_pairs:
            pre = self._loadcheck_head([f for _, f in driver_pairs], load_raws)
            if pre == "testbench":
                return self._apply_testbench_driven(
                    base, _testbench_verdict(driver_pairs[0][1]),
                )
            if isinstance(pre, int):
                # Promote the genuine RTL driver so boundary detection and the
                # downstream formatting use it as head, not the load-alias.
                driver_pairs = [driver_pairs[pre]] + driver_pairs[:pre] + driver_pairs[pre + 1:]
                drivers = [hdl for hdl, _ in driver_pairs]

        # Detect "boundary-only" drivers: every reported driver is a raw
        # hierarchy port (no synthesized cell tag — i.e. no ':' in name).
        # These are NPI's way of saying "the net is a module port; the
        # real driver lives across the boundary". For both recursive and
        # non-recursive callers, walking through with fan_in_reg_list is
        # strictly more useful than reporting the port-as-self.
        boundary_only = all(_is_boundary_driver(d) for d in drivers)
        if recursive or boundary_only:
            chain = self._fan_in_chain(
                net, signal_path, top, max_branches=_FAN_IN_MAX_BRANCHES,
            )
            if chain is not None:
                decision = self._loadcheck_head(chain, load_raws)
                if decision == "testbench":
                    return self._apply_testbench_driven(
                        base, _testbench_verdict(chain[0]),
                    )
                if isinstance(decision, int):
                    chain = [chain[decision]] + chain[:decision] + chain[decision + 1:]
                return self._apply_chain(base, chain, signal_path, recursive)
            # fan_in unavailable / failed — fall through to single-hop
            # formatting so we surface *something* instead of crashing.

        formatted = [d for d in (self._format_driver(h) for h in drivers) if d is not None]
        if not formatted:
            base["driver_status"] = "unsupported"
            base["unsupported_reason"] = "all_drivers_unformattable"
            return base

        # The driver_list load-alias check + genuine-driver promotion already ran
        # above (the single source of truth — that is what covers recursive=True);
        # ``formatted`` here just re-formats the same, possibly-reordered drivers,
        # so its head is already the promoted genuine driver. Only drop the
        # cross-check scratch field before the hops enter the result schema.
        formatted = [_strip_npi_raw(d) for d in formatted]

        head = formatted[0]
        base.update({
            "driver_status": "resolved",
            "driver_kind": head["driver_kind"],
            "source_file": head["source_file"],
            "source_line": head["source_line"],
            "expression_summary": head["expression_summary"],
        })
        if len(formatted) > 1:
            # Multi-driven net (rare but real): expose all candidates as
            # depth-0 chain entries so the caller can see the conflict.
            base["driver_chain"] = [
                {
                    "depth": 0,
                    "signal_path": signal_path,
                    "resolved_module": top,
                    "resolved_instance_path": instance_path,
                    "driver_kind": entry["driver_kind"],
                    "source_file": entry["source_file"],
                    "source_line": entry["source_line"],
                    "source_info_origin": entry.get("source_info_origin"),
                    "expression_summary": entry["expression_summary"],
                    "upstream_signals": [],
                    "instance_port_connections": None,
                    "branch_candidates": None,
                    "stopped_at": None,
                    "backend": "verdi_npi",
                    "backend_confidence": "exact",
                }
                for entry in formatted
            ]
            base["chain_summary"] = (
                f"{len(formatted)} drivers reported by NPI (multi-driven net)"
            )
        return base

    def _fan_in_chain(
        self,
        net: Any,
        signal_path: str,
        top: str,
        *,
        max_branches: int,
    ) -> list[dict[str, Any]] | None:
        """Walk fan-in cone with NPI; return a list of formatted hops.

        Returns None if fan_in_reg_list isn't available on this net or
        raised — caller can then fall back to single-hop formatting.
        Returns [] if fan_in succeeded but reported no boundary points.
        """
        if not hasattr(net, "fan_in_reg_list"):
            return None
        # Bound the traversal at the signal's own top-level scope so
        # fan-in does not wander into unrelated design hierarchies.
        # Falling back to the loaded ``top`` keeps behaviour sensible
        # for single-segment signal paths.
        bound = signal_path.split(".", 1)[0] if "." in signal_path else top
        try:
            with _silence_native_stdio():
                pins = net.fan_in_reg_list(
                    stop_at_pin=True,
                    report_primary_port=True,
                    top_scope_name=bound,
                ) or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("net.fan_in_reg_list failed for %s: %s", signal_path, exc)
            return None
        hops: list[dict[str, Any]] = []
        for pin in pins[:max_branches]:
            entry = self._format_fan_in_pin(pin)
            if entry is not None:
                hops.append(entry)
        return hops

    def _net_load_raws(self, net: Any) -> list[str] | None:
        """Raw NPI full-names of every load of ``net`` (for the driver-vs-load
        cross-check). Returns None when there is no load list or the NPI call
        fails — never raises, so a cross-check failure cannot break driver
        resolution."""
        if not hasattr(net, "load_list"):
            return None
        try:
            with _silence_native_stdio():
                raw_loads = net.load_list() or []
        except Exception as exc:  # noqa: BLE001
            _LOG.warning("cross-check load_list failed: %s", exc)
            return None
        names: list[str] = []
        for hdl in raw_loads:
            try:
                name = hdl.full_name() if hasattr(hdl, "full_name") else None
            except Exception:  # noqa: BLE001
                name = None
            if name:
                names.append(name)
        return names or None

    @staticmethod
    def _loadcheck_head(
        candidates: list[dict[str, Any]],
        load_raws: list[str] | None,
    ) -> int | str | None:
        """Decide what to do with the reported head driver given the net's loads.

        - ``None``  → head is a genuine driver (or no loads to compare); proceed.
        - ``int``   → head is a load-alias but candidate[int] is a genuine RTL
                      driver; promote it to head.
        - ``"testbench"`` → head is a load-alias and no genuine RTL driver
                      remains; the real driver is testbench/behavioral.
        """
        if not candidates or not load_raws:
            return None
        head = candidates[0]
        if not driver_is_load_alias(head.get("_npi_raw"), load_raws):
            return None
        for idx, cand in enumerate(candidates[1:], start=1):
            raw = cand.get("_npi_raw")
            if driver_is_load_alias(raw, load_raws):
                continue
            if _is_genuine_runtime_driver(raw, cand.get("driver_kind")):
                return idx
        return "testbench"

    @staticmethod
    def _apply_testbench_driven(
        base: dict[str, Any],
        verdict: dict[str, Any],
    ) -> dict[str, Any]:
        """Rewrite the result as an honest no-op: the net has no RTL driver
        (the driver NPI reported is actually a load-alias of the same net), so
        the real driver is testbench/behavioral. Do NOT surface the load as a
        driver/exact."""
        base.update({
            "driver_status": "testbench_driven",
            "driver_kind": None,
            "source_file": None,
            "source_line": None,
            "expression_summary": verdict["note"],
            "upstream_signals": [],
            "instance_port_connections": None,
            "confidence": None,
            "unsupported_reason": "driver_is_load_real_driver_is_testbench",
            "stopped_at": "testbench_driven",
            "driver_chain": None,
            "chain_summary": None,
            "cross_check": verdict,
        })
        return base

    @staticmethod
    def _apply_chain(
        base: dict[str, Any],
        hops: list[dict[str, Any]],
        signal_path: str,
        recursive: bool,
    ) -> dict[str, Any]:
        if not hops:
            base["driver_status"] = "unsupported"
            base["unsupported_reason"] = "no_npi_fan_in"
            base["stopped_at"] = "no_npi_fan_in"
            return base
        # Drop the cross-check-only raw name before hops enter driver_chain
        # (the schema forbids extra fields).
        hops = [_strip_npi_raw(hop) for hop in hops]
        head = hops[0]
        base.update({
            "driver_status": "resolved",
            "driver_kind": head["driver_kind"],
            "source_file": head["source_file"],
            "source_line": head["source_line"],
            "expression_summary": head["expression_summary"],
        })
        if recursive:
            # depth-0 entry represents the queried net itself; fan-in
            # boundary points are depth-1 branches. We deliberately do
            # not synthesise depth-2+ entries: fan_in_reg_list already
            # collapses the entire combinational cone into a single
            # boundary set, so deeper synthetic depth would be noise.
            chain: list[dict[str, Any]] = [{
                "depth": 0,
                "signal_path": signal_path,
                "resolved_module": None,
                "resolved_instance_path": signal_path.rsplit(".", 1)[0]
                    if "." in signal_path else None,
                "driver_kind": None,
                "source_file": None,
                "source_line": None,
                "expression_summary": f"queried net {signal_path}",
                "upstream_signals": [],
                "instance_port_connections": None,
                "branch_candidates": None,
                "stopped_at": None,
                "backend": "verdi_npi",
                "backend_confidence": "exact",
            }]
            chain.extend({**hop, "depth": 1} for hop in hops)
            base["driver_chain"] = chain
            base["chain_summary"] = (
                f"NPI fan-in: queried -> {len(hops)} boundary point(s)"
            )
        elif len(hops) > 1:
            base["driver_chain"] = hops
            base["chain_summary"] = (
                f"{len(hops)} fan-in points reported by NPI"
            )
        return base

    def _format_fan_in_pin(self, hdl: Any) -> dict[str, Any] | None:
        try:
            raw = hdl.full_name() if hasattr(hdl, "full_name") else None
            t = hdl.type() if hasattr(hdl, "type") else None
        except Exception:
            return None
        if not raw:
            return None
        kind = _classify_fan_in_kind(raw, t)
        scope = _scope_from_synthesized(raw)
        inst_file, inst_line = _inst_src_info(_scope_inst_of(hdl))
        line = inst_line if inst_line is not None else _line_from_synthesized(raw)
        file_val = inst_file
        origin = "npi" if (file_val is not None or line is not None) else None
        return {
            "depth": 1,
            "signal_path": scope,
            "resolved_module": _module_of(hdl),
            "resolved_instance_path": scope,
            "driver_kind": kind,
            "source_file": file_val,
            "source_line": line,
            "source_info_origin": origin,
            "expression_summary": _fan_in_summary(raw, kind),
            "upstream_signals": [],
            "instance_port_connections": None,
            "branch_candidates": None,
            "stopped_at": None,
            "backend": "verdi_npi",
            "backend_confidence": "exact",
            # For driver-vs-load cross-check only; stripped before return.
            "_npi_raw": raw,
        }

    def _format_driver(self, hdl: Any) -> dict[str, Any] | None:
        try:
            raw = hdl.full_name() if hasattr(hdl, "full_name") else None
        except Exception:
            return None
        if not raw:
            return None
        kind = _classify_driver_kind(raw)
        inst_file, inst_line = _inst_src_info(_scope_inst_of(hdl))
        line = inst_line if inst_line is not None else _line_from_synthesized(raw)
        return {
            "driver_kind": kind,
            "source_file": inst_file,
            "source_line": line,
            "source_info_origin": (
                "npi" if (inst_file is not None or line is not None) else None
            ),
            "expression_summary": _driver_summary(raw, kind),
            # For driver-vs-load cross-check only; stripped before return.
            "_npi_raw": raw,
        }

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
        # Pin-level src_info first (closest to the actual load site);
        # fall back to the enclosing instance's src_info when NPI did
        # not record anything for synthesized pins. Synthesized names
        # still provide a usable line as a last resort.
        src = _safe_src_info(hdl)
        file_val = src.get("file")
        line_val = src.get("line")
        if file_val is None or line_val is None:
            inst_file, inst_line = _inst_src_info(_scope_inst_of(hdl))
            if file_val is None:
                file_val = inst_file
            if line_val is None:
                line_val = inst_line
        if line_val is None:
            line_val = _line_from_synthesized(raw)
        origin = "npi" if (file_val is not None or line_val is not None) else None
        return {
            "load_path": scope,
            "kind": kind,
            "expr": raw if include_expr else None,
            "source_file": file_val,
            "source_line": line_val,
            "source_info_origin": origin,
            "backend": "verdi_npi",
            "confidence": "exact",
            # ``_npi_raw`` is for dedup only — stripped before schema
            # validation in the dispatch layer.
            "_npi_raw": raw,
        }

    def _resolve_net(self, netlist: Any, signal_path: str) -> Any:
        try:
            with _silence_native_stdio():
                net = netlist.get_net(signal_path)
            if net is not None:
                return net
        except Exception:
            pass
        if "[" in signal_path:
            try:
                with _silence_native_stdio():
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
        # Tag the reason; the dispatch layer surfaces it through
        # backend_status.fallback_reason / actual_backend.
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
    idx = _first_colon_outside_brackets(npi_path)
    return npi_path[:idx] if idx is not None else npi_path


def _line_from_synthesized(npi_path: str) -> int | None:
    """Parse the start line out of a synthesized PinHdl name.

    Format: ``<scope>:<construct><idx>#<inner><idx>:<line_start>:<line_end>:<cell>.<port>``
    The two integer fields after the construct identifier are start and
    end line; we surface start line only.
    """
    if _first_colon_outside_brackets(npi_path) is None:
        return None
    parts = npi_path.split(":")
    for tok in parts:
        if tok.isdigit():
            return int(tok)
    return None


def _first_colon_outside_brackets(text: str) -> int | None:
    depth = 0
    for idx, ch in enumerate(text):
        if ch == "[":
            depth += 1
        elif ch == "]" and depth > 0:
            depth -= 1
        elif ch == ":" and depth == 0:
            return idx
    return None


def _classify_driver_kind(npi_path: str) -> str:
    """Map a synthesized driver PinHdl to one of the existing driver_kind
    enum values used by Static.

    Synthesized cell tag is the segment immediately before the final
    `.<port>`. Common cell types observed in cc20:
      Init  → ``initial`` block (initial-value driver) → driver_kind=initial
      Reg   → ``always_ff`` register → driver_kind=always_ff
      Mux / Or / And / Buf → combinational logic → driver_kind=always_comb
      Assignment → continuous ``assign`` → driver_kind=assign
    Falls back to ``unknown`` for cell types we have not seen.
    """
    if ":" not in npi_path:
        # Non-synthesized: top-level decl-net or instance port
        return "instance_port"
    # Bit-range suffixes like ``[4:0]`` contain a ':' that would steal
    # the rsplit and leave us with garbage ("0]"). Strip the trailing
    # range/index before extracting the cell name.
    stripped = re.sub(r"\[[^\]]*\]$", "", npi_path)
    last_segment = stripped.rsplit(":", 1)[-1]
    cell = last_segment.split(".", 1)[0]
    cell_lower = cell.lower()
    if cell_lower == "init":
        return "initial"
    if cell_lower in ("reg", "ff", "dff"):
        return "always_ff"
    if cell_lower == "assignment":
        return "assign"
    if cell_lower in ("mux", "or", "and", "xor", "not", "buf", "notredu",
                      "andredu", "orredu", "selop", "sigtap", "sigop"):
        return "always_comb"
    return "unknown"


def _driver_summary(raw: str, kind: str) -> str:
    """Build a short human-readable summary for a driver PinHdl."""
    line = _line_from_synthesized(raw)
    line_part = f" at line {line}" if line is not None else ""
    if kind == "unknown":
        return f"NPI driver {raw}{line_part}"
    return f"{kind} driver via {raw.rsplit(':', 1)[-1]}{line_part}"


def _classify_fan_in_kind(npi_path: str, hdl_type: str | None) -> str:
    """Classify a pin returned by ``fan_in_reg_list``.

    fan_in collapses combinational logic and stops at registers or
    primary ports — so the answer is always one of:
      - a synthesized cell pin (Reg / Init / Assignment / Mux-family)
      - a top-level port handle (``npiNlPort``) when the fan-in walked
        out to a primary input boundary
    """
    if ":" not in npi_path:
        # No synthesized tag → fan-in terminated at a primary port.
        if hdl_type == "npiNlPort":
            return "primary_input_port"
        return "instance_port"
    return _classify_driver_kind(npi_path)


def _fan_in_summary(raw: str, kind: str) -> str:
    if kind == "primary_input_port":
        return f"fan-in stops at primary port {raw}"
    return _driver_summary(raw, kind)


def _strip_npi_raw(entry: dict[str, Any]) -> dict[str, Any]:
    """Return ``entry`` without the cross-check-only ``_npi_raw`` field, so it
    can pass the ``extra='forbid'`` result schema."""
    if "_npi_raw" not in entry:
        return entry
    return {k: v for k, v in entry.items() if k != "_npi_raw"}


def _norm_raw(raw: str | None) -> str | None:
    """Normalize an NPI full-name to a stable identity for driver-vs-load
    comparison: drop every bit-range / bit-index ``[...]`` segment so that, e.g.,
    ``tb.m_if.ahb_intf#[3:4]`` and ``tb.m_if.ahb_intf#[3:4]`` (or per-bit slices)
    collapse to one identity. Returns None for a falsy input."""
    if not raw:
        return None
    return re.sub(r"\[[^\]]*\]", "", raw)


def driver_is_load_alias(head_raw: str | None, load_raws: list[str] | None) -> bool:
    """True when the reported driver is byte-identical (modulo bit-indexing) to a
    LOAD of the same net.

    A net cannot be both driven by and a load of the same elaborated pin/alias —
    when NPI reports the same identity on both sides it is an aliasing artifact
    (an interface slice or a boundary alias of the net's own consumer), so that
    "driver" is really a load and cannot be the source. This is the FP-safe
    discriminator: a legitimate self-referential counter (``q <= q + 1``) drives
    net ``q`` from a ``Reg`` cell while net ``q`` loads into a distinct ``Add`` /
    ``Assignment`` cell — different identities, so it never matches here.

    Pure and string-keyed → fully unit-testable without a live KDB.
    """
    if not head_raw or not load_raws:
        return False
    norm = _norm_raw(head_raw)
    return any(_norm_raw(lr) == norm for lr in load_raws if lr)


def _is_genuine_runtime_driver(raw: str | None, kind: str | None) -> bool:
    """True when a non-alias driver candidate is a real runtime logic/register
    driver — i.e. it carries a synthesized logic-cell tag and is not an
    initial-value block or a bare hierarchy/primary port. Used to decide whether,
    after discarding a load-alias head, any genuine RTL driver remains (else the
    real driver is testbench/behavioral)."""
    if not raw:
        return False
    if _first_colon_outside_brackets(raw) is None:
        return False  # bare hierarchy port / primary-input alias, not a logic driver
    return kind not in ("initial", "primary_input_port", "instance_port", None)


def _testbench_verdict(head: dict[str, Any]) -> dict[str, Any]:
    """Build the ``cross_check`` receipt for a testbench_driven no-op from the
    load-alias head hop that triggered it."""
    scope = (
        head.get("resolved_instance_path")
        or head.get("load_path")
        or head.get("signal_path")
    )
    line = head.get("source_line")
    where = scope if scope else "the reported driver"
    note = (
        f"NPI resolved this net's driver to {where}"
        + (f" (line {line})" if line is not None else "")
        + ", but that same construct is also a LOAD of this net (it reads the net "
        "as an input / is an interface-slice alias of the net's own consumer). A net "
        "cannot be both driven by and read into the same pin, so this is a load, not "
        "the driver: NPI found no RTL register/logic driver for this net. The real "
        "driver is testbench/behavioral (e.g. a UVM driver writing through a virtual "
        "interface + clocking block), which NPI's RTL fan-in cannot see. Do NOT treat "
        f"{where} as the driver, and do NOT read this as a mis-wire pointing at that "
        "module."
    )
    return {
        "performed": True,
        "conflict": True,
        "matched_scope": str(scope) if scope else None,
        "matched_line": int(line) if isinstance(line, int) else None,
        "note": note,
    }


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


def _walk_inst_src(
    inst: Any,
    accumulator: dict[str, tuple[str | None, int | None]],
    _depth: int = 0,
) -> None:
    """Recursively populate ``full_name() -> (file, line)`` for an instance tree.

    Bounded by design size. Skips silently on any per-node failure so a
    pathological instance never breaks the whole annotation pass.
    """
    if inst is None:
        return
    # Guard runaway hierarchies (synthesized loops should not happen but
    # libNPI has surprised us before).
    if _depth > 256:
        return
    try:
        path = inst.full_name() if hasattr(inst, "full_name") else None
    except Exception:  # noqa: BLE001
        path = None
    if path:
        file_val, line_val = _inst_src_info(inst)
        if file_val is not None or line_val is not None:
            accumulator[path] = (file_val, line_val)
    try:
        children = inst.inst_list() if hasattr(inst, "inst_list") else []
    except Exception:  # noqa: BLE001
        children = []
    for child in children or []:
        _walk_inst_src(child, accumulator, _depth + 1)


def _scope_inst_of(hdl: Any) -> Any:
    """Defensive ``hdl.scope_inst()``. Returns None on any failure."""
    if hdl is None or not hasattr(hdl, "scope_inst"):
        return None
    try:
        return hdl.scope_inst()
    except Exception:  # noqa: BLE001
        return None


def _inst_src_info(inst_hdl: Any) -> tuple[str | None, int | None]:
    """Best-effort ``(file, begin_line)`` from an ``InstHdl``.

    NPI exposes elaborated-instance source info via ``file()`` and
    ``begin_line_no()``; some backends populate ``src_info()`` instead.
    We try the explicit accessors first because their semantics are
    unambiguous, then fall back to ``_safe_src_info`` for the
    dict/tuple-shaped variant. Never raises; returns ``(None, None)``
    when NPI did not record source info for this instance (common for
    library cells and synthesized helpers).
    """
    if inst_hdl is None:
        return (None, None)

    file_val: str | None = None
    line_val: int | None = None

    if hasattr(inst_hdl, "file"):
        try:
            raw = inst_hdl.file()
            if raw:
                file_val = str(raw)
        except Exception:  # noqa: BLE001
            pass
    if hasattr(inst_hdl, "begin_line_no"):
        try:
            raw = inst_hdl.begin_line_no()
            if raw is not None:
                line_val = int(raw)
        except Exception:  # noqa: BLE001
            pass

    if file_val is not None or line_val is not None:
        return (file_val, line_val)

    src = _safe_src_info(inst_hdl)
    fallback_file = src.get("file") if isinstance(src, dict) else None
    fallback_line = src.get("line") if isinstance(src, dict) else None
    return (fallback_file, fallback_line)


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
