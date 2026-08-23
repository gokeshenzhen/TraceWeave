#!/usr/bin/env python3
"""Isolated optional-Slang worker for the internal Source Graph runtime."""

from __future__ import annotations

import argparse
import base64
import importlib.metadata
import json
import os
from pathlib import Path
import re
import resource
import stat
import sys
import tempfile
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.connectivity_ir import CoverageStatus, SourceLocation  # noqa: E402
from src.slang_connectivity_projector import (  # noqa: E402
    ProjectionDiagnostic,
    ProjectionExclusion,
    ProjectionOptions,
    SLANG_FRONTEND_NAME,
    project_slang_design,
)
from src.source_graph_contract import (  # noqa: E402
    SOURCE_GRAPH_PROJECTOR_NAME,
    SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION,
    SourceGraphArtifactBuildRequest,
    SourceGraphArtifactScopeReceipt,
    SourceGraphBuildRequest,
)
from src.source_graph_runtime import (  # noqa: E402
    InternalBuildBlocker,
    PrepareStatus,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    WorkerResourceMetrics,
)


_FRONTEND_HDL_SUFFIXES = frozenset({".v", ".sv", ".vh", ".svh"})
_VHDL_SUFFIXES = frozenset({".vhd", ".vhdl"})


def _read_rss_kib() -> tuple[int | None, int | None]:
    current = None
    try:
        with open("/proc/self/status", encoding="utf-8") as stream:
            for line in stream:
                if line.startswith("VmRSS:"):
                    current = int(line.split()[1])
                    break
    except (OSError, ValueError, IndexError):
        pass
    try:
        peak = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    except (ValueError, OSError):
        peak = None
    return current, peak


def _normalize_location(
    item: Mapping[str, Any], source_root: Path
) -> SourceLocation | None:
    file_name = item.get("file")
    line = item.get("line")
    if not file_name or not line:
        return None
    path = Path(str(file_name))
    try:
        normalized = path.resolve().relative_to(source_root.resolve()).as_posix()
    except (OSError, ValueError):
        normalized = path.as_posix()
    return SourceLocation(
        file=normalized,
        line=int(line),
        column=max(int(item.get("column") or 0), 0),
    )


def _projection_diagnostics(
    payload: Mapping[str, Any], source_root: Path
) -> tuple[ProjectionDiagnostic, ...]:
    return tuple(
        ProjectionDiagnostic(
            code=str(item["code"]),
            severity=str(item["severity"]),
            message=str(item["message"]),
            location=_normalize_location(item, source_root),
            constructs=("frontend_diagnostic",),
            scopes=("*",),
        )
        for item in payload.get("items", ())
    )


def _gap_label(value: str) -> str:
    label = re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")
    if not label or not label[0].isalpha():
        label = "gap_" + label
    return label[:120]


def _frontend_args(
    request: SourceGraphBuildRequest | SourceGraphArtifactBuildRequest,
) -> list[str]:
    source_identity = (
        request.identity
        if isinstance(request, SourceGraphBuildRequest)
        else request.source
    )
    manifest = source_identity.compile_inputs
    artifact_identity = (
        request.artifact_identity
        if isinstance(request, SourceGraphBuildRequest)
        else request.identity
    )
    compile_projection = artifact_identity.compile_projection
    ordered_inputs = (
        compile_projection.ordered_inputs
        if compile_projection is not None
        else manifest.ordered_inputs
    )
    result = [
        *manifest.ordered_options,
        *(
            path
            for path in ordered_inputs
            if Path(path).suffix.lower() in _FRONTEND_HDL_SUFFIXES
        ),
    ]
    # A dependency-closure build compiles every proved top/bind definition but
    # elaborates only the hierarchy-selected design top.  Full-manifest calls
    # retain the historical all-top replay for compatibility.
    tops = (
        (request.scope.top,)
        if compile_projection is not None
        else manifest.ordered_tops or (request.scope.top,)
    )
    for top in tops:
        result.extend(["--top", top])
    return result


def _worker_metrics(
    *,
    wall_started: float,
    cpu_started: float,
    rss_start: int | None,
    ir_bytes: int = 0,
) -> WorkerResourceMetrics:
    rss_end, rss_peak = _read_rss_kib()
    return WorkerResourceMetrics(
        wall_time_ms=(time.perf_counter() - wall_started) * 1000,
        cpu_time_ms=(time.process_time() - cpu_started) * 1000,
        rss_start_kib=rss_start,
        rss_peak_kib=rss_peak,
        rss_end_kib=rss_end,
        ir_bytes=ir_bytes,
    )


def _failure_payload(
    status: PrepareStatus,
    blocker: InternalBuildBlocker,
    metrics: WorkerResourceMetrics,
) -> dict[str, Any]:
    return {
        "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        "status": status.value,
        "blocker": blocker.to_dict(include_message=True),
        "metrics": metrics.to_dict(),
        "fallback_used": False,
    }


def execute_build(request: SourceGraphArtifactBuildRequest) -> dict[str, Any]:
    """Build a focused Connectivity IR; imports pyslang only in this process."""

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    rss_start, _ = _read_rss_kib()
    identity = request.source
    if request.identity.worker_protocol_version != SOURCE_GRAPH_WORKER_PROTOCOL_VERSION:
        return _failure_payload(
            PrepareStatus.DEPENDENCY_BLOCKED,
            InternalBuildBlocker(
                code="worker_protocol_identity_mismatch",
                stage="frontend_import",
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )
    if identity.frontend_name != SLANG_FRONTEND_NAME:
        return _failure_payload(
            PrepareStatus.DEPENDENCY_BLOCKED,
            InternalBuildBlocker(
                code="frontend_identity_unsupported",
                stage="frontend_import",
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )
    if (
        identity.projector_name != SOURCE_GRAPH_PROJECTOR_NAME
        or identity.projector_version != SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
        or identity.projector_schema_version != SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
    ):
        return _failure_payload(
            PrepareStatus.DEPENDENCY_BLOCKED,
            InternalBuildBlocker(
                code="projector_version_mismatch",
                stage="frontend_import",
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )
    try:
        from pyslang import driver as driver_module

        version = importlib.metadata.version("pyslang")
    except Exception as exc:
        return _failure_payload(
            PrepareStatus.DEPENDENCY_BLOCKED,
            InternalBuildBlocker(
                code="frontend_unavailable",
                stage="frontend_import",
                message=f"{type(exc).__name__}: {exc}",
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )

    if version != identity.frontend_version:
        return _failure_payload(
            PrepareStatus.DEPENDENCY_BLOCKED,
            InternalBuildBlocker(
                code="frontend_version_mismatch",
                stage="frontend_import",
                message=(f"expected {identity.frontend_version}, found {version}"),
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )

    try:
        # This helper contains the already-validated Phase 0B command-line
        # boundary.  Importing it here keeps both it and pyslang out of the
        # parent runtime module.
        from scripts.spike_source_frontend import (
            _configure_driver,
            _diagnostics_payload,
        )

        driver = _configure_driver(driver_module, _frontend_args(request))
        if not driver.parseAllSources():
            raise RuntimeError("Driver.parseAllSources returned false")
        compilation = driver.createCompilation()
        root = compilation.getRoot()
        diagnostics = list(compilation.getAllDiagnostics())
        diagnostic_payload = _diagnostics_payload(driver, diagnostics)
        source_root = Path.cwd()
        manifest = identity.compile_inputs
        compile_projection = request.identity.compile_projection
        projected_inputs = (
            compile_projection.ordered_inputs
            if compile_projection is not None
            else manifest.ordered_inputs
        )
        frontend_inputs = tuple(
            path
            for path in projected_inputs
            if Path(path).suffix.lower() in _FRONTEND_HDL_SUFFIXES
        )
        vhdl_inputs = tuple(
            path
            for path in manifest.ordered_inputs
            if Path(path).suffix.lower() in _VHDL_SUFFIXES
        )
        exclusions = tuple(
            ProjectionExclusion(
                code=code,
                message=f"objective exclusion retained by build contract: {code}",
                impact=CoverageStatus.INCONCLUSIVE,
                scopes=("*",),
                constructs=(code,),
            )
            for code in request.scope.coverage_boundary.objective_exclusions
        )
        if vhdl_inputs and "opaque_vhdl_boundary" not in {
            item.code for item in exclusions
        }:
            exclusions = (
                *exclusions,
                ProjectionExclusion(
                    code="opaque_vhdl_boundary",
                    message=(
                        "VHDL source is retained in build identity but projected "
                        "as an opaque mixed-language boundary"
                    ),
                    impact=CoverageStatus.INCONCLUSIVE,
                    scopes=("*",),
                    constructs=("vhdl", "mixed_language_boundary"),
                ),
            )
        projection = project_slang_design(
            root=root,
            source_manager=driver.sourceManager,
            frontend_version=version,
            options=ProjectionOptions(
                source_root=source_root,
                files_total=len(manifest.ordered_inputs),
                files_projected=sum(
                    Path(path).expanduser().is_file()
                    for path in frontend_inputs
                ),
                diagnostics=_projection_diagnostics(diagnostic_payload, source_root),
                diagnostic_total=int(diagnostic_payload["total"]),
                blocking_diagnostic_total=int(
                    diagnostic_payload["blocking_error_count"]
                ),
                exclusions=exclusions,
                focus_instance_paths=request.scope.coverage_boundary.instance_paths,
                assignment_instance_paths=request.scope.projection_instance_paths,
                metadata=(
                    ("runtime", "phase3b_bounded_artifact"),
                    ("scope_contract", request.contract_version),
                ),
            ),
        )
        ir = projection.ir
        serialized = ir.to_json_bytes()
        gap_codes = {_gap_label(gap.code) for gap in ir.coverage.gaps}
        if ir.coverage.status is not CoverageStatus.COMPLETE and not gap_codes:
            gap_codes.add("coverage_incomplete_without_detailed_gap")
        scope_receipt = SourceGraphArtifactScopeReceipt(
            scope=request.scope,
            coverage_status=ir.coverage.status,
            gap_codes=tuple(sorted(gap_codes)),
        )
    except Exception as exc:
        return _failure_payload(
            PrepareStatus.BUILD_FAILED,
            InternalBuildBlocker(
                code="frontend_build_failed",
                stage="projection",
                message=f"{type(exc).__name__}: {exc}",
            ),
            _worker_metrics(
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            ),
        )

    metrics = _worker_metrics(
        wall_started=wall_started,
        cpu_started=cpu_started,
        rss_start=rss_start,
        ir_bytes=len(serialized),
    )
    return {
        "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        "status": PrepareStatus.READY.value,
        "ir_json_base64": base64.b64encode(serialized).decode("ascii"),
        "ir_fingerprint_sha256": ir.fingerprint_sha256(),
        "scope_receipt": scope_receipt.to_dict(),
        "projection_receipt": projection.receipt.to_dict(),
        "metrics": metrics.to_dict(),
        "fallback_used": False,
    }


def _read_request(path: Path) -> SourceGraphArtifactBuildRequest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise ValueError("worker request must be an object")
    if payload.get("protocol_version") != SOURCE_GRAPH_WORKER_PROTOCOL_VERSION:
        raise ValueError("worker protocol version mismatch")
    request = payload.get("request")
    if not isinstance(request, Mapping):
        raise ValueError("worker request payload is missing")
    return SourceGraphArtifactBuildRequest.from_dict(request)


def _write_response(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, sort_keys=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary_path, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    try:
        request = _read_request(args.request)
        payload = execute_build(request)
    except Exception as exc:
        payload = _failure_payload(
            PrepareStatus.INVALID_RESPONSE,
            InternalBuildBlocker(
                code="worker_request_invalid",
                stage="worker_request",
                message=f"{type(exc).__name__}: {exc}",
            ),
            WorkerResourceMetrics(),
        )
    _write_response(args.response, payload)
    return 0 if payload["status"] == PrepareStatus.READY.value else 2


if __name__ == "__main__":
    raise SystemExit(main())
