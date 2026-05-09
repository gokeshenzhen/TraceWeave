"""
verdi_backend.py
Detection of Verdi runtime / KDB artefacts. Pure detection — no NPI
calls, no verdi process spawn, no license consumption.

The module produces a BackendStatus-shape dict consumed by tools that
expose a backend_status field (find_signal_loads today; explain_signal_driver
in a future session). Detection follows docs/design_verdi_backend_integration.md
§10.2:

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
_SETUP_FILENAME = "synopsys_sim.setup"


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
        # Best-effort: still look for KDB anywhere obvious.
        kdb_path, kdb_flow = _probe_vcs_kdb(case_dir)
        if kdb_path is None:
            kdb_path, kdb_flow = _probe_vericom_kdb(case_dir)

    verdi_home = os.environ.get("VERDI_HOME")
    license_env = (
        os.environ.get("SNPSLMD_LICENSE_FILE")
        or os.environ.get("LM_LICENSE_FILE")
    )

    if kdb_path is not None:
        kdb_hint = (
            f"Verdi KDB found at {kdb_path}; backend ready once NPI integration lands."
        )
    else:
        kdb_hint = _build_kdb_hint(simulator, compile_result, verdi_home, license_env)

    return {
        "simulator": simulator,
        "backend": "static",
        "parser_match": "approximate",
        "kdb_path": kdb_path,
        "kdb_flow": kdb_flow,
        "kdb_hint": kdb_hint,
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
    if os.path.isdir(two_step):
        return two_step, "vcs_two_step"

    setup_path = os.path.join(case_dir, _SETUP_FILENAME)
    work_dir = _read_synopsys_sim_setup(setup_path, case_dir)
    if work_dir:
        candidate = _find_libpp_under(work_dir)
        if candidate:
            return candidate, "vcs_three_step"
    return None, "none"


def _probe_vericom_kdb(case_dir: str | None) -> tuple[str | None, str]:
    if case_dir is None:
        return None, "none"
    setup_path = os.path.join(case_dir, _SETUP_FILENAME)
    work_dir = _read_synopsys_sim_setup(setup_path, case_dir)
    if work_dir:
        candidate = _find_libpp_under(work_dir)
        if candidate:
            return candidate, "vericom_standalone"
    candidate = _find_libpp_under(case_dir)
    if candidate:
        return candidate, "vericom_standalone"
    return None, "none"


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
        files_hint = "<source files>"
        user = (compile_result.get("files") or {}).get("user") or []
        rtl_files = [f["path"] for f in user if f.get("category") in (None, "rtl")]
        if rtl_files:
            files_hint = " ".join(rtl_files[:8]) + (" ..." if len(rtl_files) > 8 else "")
        top_hint = f" -top {top}" if top else ""
        return (
            f"xrun does not generate Verdi KDB. Run vericom standalone over the "
            f"same sources to build a KDB:\n  vericom -kdb {files_hint}{top_hint}\n"
            f"Note: vericom is a different parser from xrun; results are approximate."
            + env_prefix
        )

    return (
        "Connectivity backend requires a Verdi KDB. Either add `-kdb=only` to "
        "your VCS compile or run `vericom -kdb` over the design sources."
        + env_prefix
    )
