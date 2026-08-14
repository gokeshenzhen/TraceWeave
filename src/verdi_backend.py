"""
verdi_backend.py
Detection of Verdi runtime / KDB artefacts. Pure detection — no NPI
calls, no verdi process spawn, no license consumption.

The module produces a BackendStatus-shape dict surfaced in the result
envelope of every connectivity tool (``find_signal_loads``,
``explain_signal_driver``, ``trace_signal_path``) and consumed
internally by ``build_tb_hierarchy`` to decide whether to overlay
NPI-elaborated ``file:line`` onto the compile-log-derived nodes.
Detection rules:

    VCS:
      1. <case_dir>/simv.daidir/kdb.elab++  → vcs_two_step
      2. <work_lib>/work.lib++ via synopsys_sim.setup → vcs_three_step
    Xcelium:
      1. Skip simv.daidir entirely
      2. <work_lib>/<name>.lib++ → vericom_standalone (if user has run it)

The kdb_hint string is a copy-pasteable command derived from the
parsed compile_command when available.
"""

from __future__ import annotations

import os
import re
from typing import Any


_KDB_DIRNAME = "kdb.elab++"
_KDB_ELAB_ERROR_MARKER = ".hasElabcomError"
_SETUP_FILENAME = "synopsys_sim.setup"
_MAX_KDB_MARKER_BYTES = 64 * 1024
_MAX_KDB_ERROR_LOG_BYTES = 4 * 1024 * 1024
_ELAB_ERROR_COUNT_RE = re.compile(r"Total\s+(\d+)\s+error\(s\)", re.IGNORECASE)


def probe_verdi_backend(
    compile_result: dict[str, Any],
    compile_log_path: str | None = None,
) -> dict[str, Any]:
    """Detect KDB availability for the case described by compile_result.

    compile_result : output of parse_compile_log (must carry simulator
                     and ideally compile_command and user file list).
    compile_log_path : used to anchor relative searches (case_dir =
                       directory of the compile log).

    Returns a BackendStatus-shape dict suitable for direct injection
    into the tool response.
    """
    simulator_raw = (compile_result.get("simulator") or "unknown").lower()
    simulator = simulator_raw if simulator_raw in ("vcs", "xcelium") else "unknown"

    case_dir = _resolve_case_dir(compile_log_path, compile_result)
    kdb_path: str | None = None
    kdb_flow: str = "none"

    if simulator == "vcs":
        kdb_path, kdb_flow = _probe_vcs_kdb(case_dir)
    elif simulator == "xcelium":
        kdb_path, kdb_flow = _probe_vericom_kdb(case_dir)
    else:
        # Best-effort: still look for KDB anywhere obvious, preferring a clean
        # candidate across both layouts before retaining a degraded one.
        vcs_path, vcs_flow = _probe_vcs_kdb(case_dir)
        vericom_path, vericom_flow = _probe_vericom_kdb(case_dir)
        if vcs_path is not None and _elab_kdb_is_usable(vcs_path):
            kdb_path, kdb_flow = vcs_path, vcs_flow
        elif vericom_path is not None and _elab_kdb_is_usable(vericom_path):
            kdb_path, kdb_flow = vericom_path, vericom_flow
        elif vcs_path is not None:
            kdb_path, kdb_flow = vcs_path, vcs_flow
        else:
            kdb_path, kdb_flow = vericom_path, vericom_flow

    # TraceWeave-managed cache: if the user has previously run the
    # ``build_kdb`` tool, a cached elaborated KDB lives under
    # ``$TRACEWEAVE_CACHE/kdb/<hash>/kdb.elab++``. Pick it up so NPI
    # finds it transparently on subsequent driver/load queries.
    if kdb_path is None or _elab_kdb_has_errors(kdb_path):
        cached = _probe_traceweave_cached_kdb(compile_result, compile_log_path)
        if cached and (
            kdb_path is None
            or _elab_kdb_is_usable(cached)
        ):
            kdb_path, kdb_flow = cached, "traceweave_cached"

    degraded_candidate = (
        kdb_path
        if kdb_path is not None and _elab_kdb_has_errors(kdb_path)
        else None
    )
    kdb_validation_status = (
        "elaboration_error"
        if degraded_candidate is not None
        else (
            "usable"
            if kdb_path is not None
            else (
                "elaboration_error"
                if _failed_elab_kdb_under(case_dir)
                else "unavailable"
            )
        )
    )
    kdb_error_count: int | None = None
    kdb_error_log: str | None = None
    if degraded_candidate is not None:
        kdb_error_count, kdb_error_log = read_kdb_elab_error_metadata(
            degraded_candidate
        )

    # Preserve the artifact facts while retaining the old all-or-nothing
    # selection policy when the escape hatch is explicitly disabled.
    npi_selection_reason: str | None = None
    if degraded_candidate is not None:
        from config import NPI_ALLOW_DEGRADED_KDB  # noqa: PLC0415

        if not NPI_ALLOW_DEGRADED_KDB:
            kdb_path = None
            kdb_flow = "none"
            npi_selection_reason = "npi_degraded_kdb_disabled"

    verdi_home = os.environ.get("VERDI_HOME")
    license_env = (
        os.environ.get("SNPSLMD_LICENSE_FILE")
        or os.environ.get("LM_LICENSE_FILE")
    )

    if kdb_path is not None and kdb_validation_status == "elaboration_error":
        count_note = (
            f" ({kdb_error_count} elaboration error(s))"
            if kdb_error_count is not None
            else ""
        )
        kdb_hint = (
            f"Verdi KDB found at {kdb_path}{count_note}; NPI will attempt a "
            "degraded partial-netlist load. Positive driver/load/path facts remain "
            "usable, while incomplete or negative queries fall through to Source "
            "Graph and Legacy Static."
        )
    elif kdb_path is not None:
        kdb_hint = (
            f"Verdi KDB found at {kdb_path}; NPI backend active — preferred for "
            f"cross-hierarchy driver/load tracing (uses fan-in on the elaborated "
            f"netlist). Static source-trace serves as fallback when NPI cannot load."
        )
    elif kdb_validation_status == "elaboration_error":
        kdb_hint = (
            "Verdi KDB has an elaboration-error marker, but degraded-KDB NPI use "
            "is disabled by TRACEWEAVE_NPI_ALLOW_DEGRADED_KDB. TraceWeave will "
            "use Source Graph meanwhile."
        )
    else:
        kdb_hint = _build_kdb_hint(simulator, compile_result, verdi_home, license_env)

    return {
        "simulator": simulator,
        "backend": "static",
        "parser_match": "approximate",
        "kdb_path": kdb_path,
        "kdb_flow": kdb_flow,
        "kdb_validation_status": kdb_validation_status,
        "kdb_degraded": False,
        "kdb_error_count": kdb_error_count,
        "kdb_error_log": kdb_error_log,
        "kdb_hint": kdb_hint,
        **(
            {"_npi_selection_reason": npi_selection_reason}
            if npi_selection_reason is not None
            else {}
        ),
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _resolve_case_dir(
    compile_log_path: str | None,
    compile_result: dict[str, Any],
) -> str | None:
    if compile_log_path and os.path.exists(compile_log_path):
        return os.path.dirname(os.path.abspath(compile_log_path))
    user_files = (compile_result.get("files") or {}).get("user") or []
    if user_files:
        return os.path.dirname(os.path.abspath(user_files[0]["path"]))
    return None


def _probe_vcs_kdb(case_dir: str | None) -> tuple[str | None, str]:
    if case_dir is None:
        return None, "none"
    two_step = os.path.join(case_dir, "simv.daidir", _KDB_DIRNAME)
    if os.path.isdir(two_step) and _elab_kdb_is_usable(two_step):
        return two_step, "vcs_two_step"

    setup_path = os.path.join(case_dir, _SETUP_FILENAME)
    work_dir = _read_synopsys_sim_setup(setup_path, case_dir)
    if work_dir:
        candidate = _find_libpp_under(work_dir)
        if candidate:
            return candidate, "vcs_three_step"
    if os.path.isdir(two_step):
        return two_step, "vcs_two_step"
    return None, "none"


def _probe_vericom_kdb(case_dir: str | None) -> tuple[str | None, str]:
    """Prefer an *elaborated* KDB (``kdb.elab++``) over source-only
    ``*.lib++``. NPI needs the elaborated DB to answer driver/load queries;
    passing a plain ``work.lib++`` loads without error but every ``get_net``
    resolves to None.  The probe reports the artifact path; the NPI loader
    converts a trailing ``kdb.elab++`` to its containing simflow ``-dbdir``.
    """
    if case_dir is None:
        return None, "none"
    setup_path = os.path.join(case_dir, _SETUP_FILENAME)
    work_dir = _read_synopsys_sim_setup(setup_path, case_dir)
    for root in (work_dir, case_dir):
        if not root:
            continue
        elab = _find_elab_kdb_under(root)
        if elab:
            return elab, "vericom_standalone"
    # A partial elaborated netlist is more useful to NPI than a source-only
    # lib++ database, so retain it as the second choice after a clean KDB.
    for root in (work_dir, case_dir):
        if not root:
            continue
        elab = _find_degraded_elab_kdb_under(root)
        if elab:
            return elab, "vericom_standalone"
    for root in (work_dir, case_dir):
        if not root:
            continue
        candidate = _find_libpp_under(root)
        if candidate:
            return candidate, "vericom_standalone"
    return None, "none"


def _probe_traceweave_cached_kdb(
    compile_result: dict[str, Any],
    compile_log_path: str | None,
) -> str | None:
    """Check whether the ``build_kdb`` tool has already produced a KDB
    matching the current compile inputs. Returns the kdb.elab++ path on
    cache hit, else None. Imports lazily to keep verdi_backend free of
    a dependency on kdb_builder."""
    try:
        # Lazy imports — verdi_backend is intentionally light.
        from src.kdb_builder import _extract_build_inputs  # noqa: PLC0415
        from config import (  # noqa: PLC0415
            KDB_CACHE_SUBDIR,
            TRACEWEAVE_CACHE_ROOT,
        )
    except Exception:
        return None
    try:
        inputs = _extract_build_inputs(compile_result, top_hint=None)
    except Exception:
        return None
    if "error" in inputs:
        return None
    candidate = (
        TRACEWEAVE_CACHE_ROOT
        / KDB_CACHE_SUBDIR
        / inputs["hash"]
        / _KDB_DIRNAME
    )
    return str(candidate) if candidate.is_dir() else None


def _elab_kdb_is_usable(path: str) -> bool:
    return not _elab_kdb_has_errors(path)


def _elab_kdb_has_errors(path: str) -> bool:
    return os.path.isfile(os.path.join(path, _KDB_ELAB_ERROR_MARKER))


def kdb_has_elaboration_errors(path: str) -> bool:
    """Public artifact-fact helper shared by local and LSF NPI paths."""

    return _elab_kdb_has_errors(path)


def _elab_kdb_candidates(directory: str | None) -> list[str]:
    """Return only the bounded direct/one-level elaborated-KDB candidates."""

    if not directory or not os.path.isdir(directory):
        return []
    candidates = [os.path.join(directory, _KDB_DIRNAME)]
    try:
        entries = os.listdir(directory)
    except OSError:
        return candidates
    candidates.extend(
        os.path.join(directory, entry, _KDB_DIRNAME) for entry in entries
    )
    return candidates


def _failed_elab_kdb_under(directory: str | None) -> bool:
    return any(
        os.path.isdir(candidate) and not _elab_kdb_is_usable(candidate)
        for candidate in _elab_kdb_candidates(directory)
    )


def _find_elab_kdb_under(directory: str) -> str | None:
    """Locate an ``kdb.elab++`` directory under ``directory``.

    Checks the directory itself and one level of children so flows
    that emit the elab DB next to (or inside) the case dir are both
    picked up.
    """
    for candidate in _elab_kdb_candidates(directory):
        if os.path.isdir(candidate) and _elab_kdb_is_usable(candidate):
            return candidate
    return None


def _find_degraded_elab_kdb_under(directory: str) -> str | None:
    """Locate the first bounded elaborated KDB carrying an error marker."""

    for candidate in _elab_kdb_candidates(directory):
        if os.path.isdir(candidate) and _elab_kdb_has_errors(candidate):
            return candidate
    return None


def read_kdb_elab_error_metadata(
    kdb_path: str,
) -> tuple[int | None, str | None]:
    """Return bounded diagnostic metadata for an error-marked KDB.

    The marker/log format is owned by Verdi and has varied across releases.
    This helper therefore never raises and never participates in KDB
    admission: malformed, missing, or oversized diagnostics simply return
    ``(None, None)``.
    """

    marker_path = os.path.join(kdb_path, _KDB_ELAB_ERROR_MARKER)
    try:
        marker_text = _read_bounded_text(marker_path, _MAX_KDB_MARKER_BYTES)
        marker_line = next(
            (line.strip() for line in marker_text.splitlines() if line.strip()),
            "",
        )
        if not marker_line or "\x00" in marker_line:
            return None, None
        marker_line = marker_line.strip('"\'')
        log_path = (
            marker_line
            if os.path.isabs(marker_line)
            else os.path.normpath(os.path.join(kdb_path, marker_line))
        )
        log_text = _read_bounded_text(
            log_path,
            _MAX_KDB_ERROR_LOG_BYTES,
            tail=True,
        )
        matches = list(_ELAB_ERROR_COUNT_RE.finditer(log_text))
        count = int(matches[-1].group(1)) if matches else None
        return count, os.path.abspath(log_path)
    except (OSError, UnicodeError, ValueError):
        return None, None


def _read_bounded_text(path: str, max_bytes: int, *, tail: bool = False) -> str:
    size = os.path.getsize(path)
    if size < 0:
        raise OSError("invalid file size")
    with open(path, "rb") as stream:
        if tail and size > max_bytes:
            stream.seek(size - max_bytes)
        data = stream.read(max_bytes + 1)
    if len(data) > max_bytes:
        data = data[-max_bytes:] if tail else data[:max_bytes]
    return data.decode("utf-8", errors="replace")


def _read_synopsys_sim_setup(path: str, case_dir: str) -> str | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", errors="replace") as f:
            for line in f:
                line = line.split("--", 1)[0].split("//", 1)[0].strip()
                if not line or ":" not in line:
                    continue
                lib, target = (item.strip() for item in line.split(":", 1))
                if lib.upper() != "WORK":
                    continue
                target = target.strip().strip('"')
                if not target:
                    continue
                if not os.path.isabs(target):
                    target = os.path.normpath(os.path.join(case_dir, target))
                if os.path.isdir(target):
                    return target
    except OSError:
        return None
    return None


def _find_libpp_under(directory: str) -> str | None:
    if not os.path.isdir(directory):
        return None
    for entry in os.listdir(directory):
        if entry.endswith(".lib++"):
            full = os.path.join(directory, entry)
            if os.path.isdir(full):
                return full
    return None


def _build_kdb_hint(
    simulator: str,
    compile_result: dict[str, Any],
    verdi_home: str | None,
    license_env: str | None,
) -> str:
    cmd = compile_result.get("compile_command")
    top = (compile_result.get("top_modules") or [None])[0]

    env_note = []
    if not verdi_home:
        env_note.append("set VERDI_HOME")
    if not license_env:
        env_note.append("ensure SNPSLMD_LICENSE_FILE / LM_LICENSE_FILE")
    env_prefix = (" " + ", ".join(env_note) + " before running.") if env_note else ""

    if simulator == "vcs":
        if cmd and "-kdb" not in cmd:
            top_hint = f" {top}" if top else ""
            return (
                f"Verdi KDB not found. Re-run with `-kdb=only` to generate KDB "
                f"without rebuilding simv:\n  {cmd} -kdb=only{top_hint}"
                f"{env_prefix}"
            )
        if cmd and "-kdb" in cmd:
            return (
                "Verdi KDB not found despite `-kdb` in compile command. "
                "Check that compile completed; expected "
                "`<case_dir>/simv.daidir/kdb.elab++`." + env_prefix
            )
        return (
            "Verdi KDB not found. Add `-kdb=only` to the next vcs compile "
            "to generate KDB without rebuilding simv." + env_prefix
        )

    if simulator == "xcelium":
        from config import AUTO_KDB_BUILD  # noqa: PLC0415
        if AUTO_KDB_BUILD:
            return (
                "xrun does not generate Verdi KDB. Call the `build_kdb` tool to "
                "auto-generate one from this compile log (vericom + elabcom, "
                "cached under TRACEWEAVE_CACHE_DIR)." + env_prefix
            )
        files_hint = "<source files>"
        user = (compile_result.get("files") or {}).get("user") or []
        rtl_files = [f["path"] for f in user if f.get("category") in (None, "rtl")]
        if rtl_files:
            files_hint = " ".join(rtl_files[:8]) + (" ..." if len(rtl_files) > 8 else "")
        top_hint = f" -top {top}" if top else ""
        return (
            f"xrun does not generate Verdi KDB. Run vericom standalone over the "
            f"same sources to build a KDB:\n  vericom -kdb {files_hint}{top_hint}\n"
            f"Or set TRACEWEAVE_AUTO_KDB=1 to enable the `build_kdb` MCP tool."
            + env_prefix
        )

    return (
        "Connectivity backend requires a Verdi KDB. Either add `-kdb=only` to "
        "your VCS compile or run `vericom -kdb` over the design sources."
        + env_prefix
    )
