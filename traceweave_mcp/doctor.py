"""Read-only diagnostics for the installable TraceWeave command.

The doctor deliberately inspects native prerequisites without loading them.
Loading the FSDB adapter may create repo-local runtime links, which would make
``--doctor`` unsuitable as a read-only installation probe.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
from pathlib import Path
import sys
from typing import Any

from . import __version__


_EXPECTED_PYSLANG_VERSION = "11.0.0"


def _distribution_version(name: str) -> str | None:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return None


def _runtime_config() -> tuple[Any, str]:
    try:
        from ._runtime import config as runtime_config

        return runtime_config, "portable"
    except ModuleNotFoundError as exc:
        if exc.name not in {
            "traceweave_mcp._runtime",
            "traceweave_mcp._runtime.config",
        }:
            raise

    # A source checkout has no physical traceweave_mcp._runtime directory. The
    # private namespace is produced only by setuptools' package-dir mapping.
    import config as runtime_config

    return runtime_config, "repository"


def collect_diagnostics() -> dict[str, Any]:
    """Return a stable, secret-free summary of installation capabilities."""

    runtime_config, profile = _runtime_config()
    runtime_root = Path(runtime_config.__file__).resolve().parent
    wrapper = runtime_root / "libfsdb_wrapper.so"
    runtime_info = runtime_config.get_fsdb_runtime_info()

    dependencies = {
        "mcp": _distribution_version("mcp"),
        "PyYAML": _distribution_version("PyYAML"),
        "pyslang": _distribution_version("pyslang"),
    }
    python_supported = sys.version_info >= (3, 11)
    base_ready = bool(
        python_supported and dependencies["mcp"] and dependencies["PyYAML"]
    )
    source_graph_ready = dependencies["pyslang"] == _EXPECTED_PYSLANG_VERSION

    wrapper_present = wrapper.is_file()
    native_runtime_present = bool(runtime_info.get("enabled"))
    if profile == "portable" and wrapper_present:
        fsdb_status = "unsupported_manual_extension"
        fsdb_ready = False
    elif wrapper_present and native_runtime_present:
        fsdb_status = "candidate_ready_unloaded"
        fsdb_ready = profile == "repository"
    elif not wrapper_present:
        fsdb_status = "wrapper_missing"
        fsdb_ready = False
    else:
        fsdb_status = "native_runtime_missing"
        fsdb_ready = False

    npi_execution = os.environ.get("TRACEWEAVE_NPI_EXECUTION", "local").strip()
    if npi_execution not in {"local", "lsf"}:
        npi_status = "execution_mode_invalid"
    elif npi_execution == "lsf" and not os.environ.get(
        "TRACEWEAVE_NPI_LSF_QUEUE"
    ):
        npi_status = "lsf_queue_missing"
    elif os.environ.get("VERDI_HOME") or os.environ.get("NOVAS_HOME"):
        npi_status = "environment_detected_unverified"
    else:
        npi_status = "environment_not_detected"

    actions: list[str] = []
    if not source_graph_ready:
        actions.append(
            'Install the portable extra: pip install "traceweave-mcp[source-graph]".'
        )
    if profile == "portable" and not wrapper_present:
        actions.append(
            "For FSDB and the full EDA workflow, clone the TraceWeave repository "
            "and run: bash scripts/install.sh."
        )
    elif profile == "portable" and wrapper_present:
        actions.append(
            "This portable installation contains a manually supplied FSDB wrapper; "
            "that mixed layout is not supported. Use the repository-local installer."
        )
    elif not fsdb_ready:
        actions.append("Run: bash scripts/install.sh.")

    return {
        "schema_version": 1,
        "product": "TraceWeave",
        "version": __version__,
        "installation_profile": profile,
        "python": {
            "version": ".".join(str(item) for item in sys.version_info[:3]),
            "supported": python_supported,
        },
        "base_runtime": {
            "ready": base_ready,
            "dependencies": {
                "mcp": dependencies["mcp"],
                "PyYAML": dependencies["PyYAML"],
            },
        },
        "source_graph": {
            "ready": source_graph_ready,
            "expected_pyslang_version": _EXPECTED_PYSLANG_VERSION,
            "installed_pyslang_version": dependencies["pyslang"],
        },
        "fsdb": {
            "ready": fsdb_ready,
            "status": fsdb_status,
            "wrapper_present": wrapper_present,
            "native_runtime_present": native_runtime_present,
            "native_runtime_source": runtime_info.get("source"),
            "missing_runtime_libraries": list(runtime_info.get("missing_libs") or []),
        },
        "verdi_npi": {
            "status": npi_status,
            "execution_mode": npi_execution,
            "queue_configured": bool(
                os.environ.get("TRACEWEAVE_NPI_LSF_QUEUE")
            ),
            "note": (
                "Environment detection is not a KDB, pynpi, scheduler, worker, "
                "or license validation."
            ),
        },
        "recommended_actions": actions,
    }


def _human_report(report: dict[str, Any]) -> str:
    base = report["base_runtime"]
    source_graph = report["source_graph"]
    fsdb = report["fsdb"]
    npi = report["verdi_npi"]

    lines = [
        f"TraceWeave {report['version']} doctor",
        f"installation_profile: {report['installation_profile']}",
        f"base_runtime: {'ready' if base['ready'] else 'not_ready'}",
        "source_graph: "
        + (
            f"ready (pyslang {source_graph['installed_pyslang_version']})"
            if source_graph["ready"]
            else "unavailable"
        ),
        f"fsdb: {fsdb['status']}",
        f"verdi_npi: {npi['status']} (execution_mode={npi['execution_mode']})",
    ]
    if report["recommended_actions"]:
        lines.append("recommended_actions:")
        lines.extend(f"  - {action}" for action in report["recommended_actions"])
    return "\n".join(lines)


def run_doctor(*, json_output: bool = False) -> int:
    report = collect_diagnostics()
    if json_output:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human_report(report))
    return 0 if report["base_runtime"]["ready"] else 1
