#!/usr/bin/env python3
"""Long-lived, single-design Slang worker for bounded semantic contexts."""

from __future__ import annotations

import argparse
import base64
from dataclasses import dataclass
import importlib.metadata
import json
from pathlib import Path
import re
import sys
import time
from typing import Any, Mapping, Sequence


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.slang_connectivity_projector import SLANG_FRONTEND_NAME  # noqa: E402
from src.source_graph_contract import (  # noqa: E402
    SOURCE_GRAPH_PROJECTOR_NAME,
    SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION,
    SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
    SourceGraphArtifactBuildRequest,
    compute_source_graph_semantic_context_key,
)
from src.source_graph_runtime import (  # noqa: E402
    InternalBuildBlocker,
    PrepareStatus,
)
from src.source_graph_worker import (  # noqa: E402
    SemanticFrontendSession,
    _failure_payload,
    _read_request,
    _read_rss_kib,
    _worker_metrics,
    _write_response,
    create_semantic_frontend_session,
    project_semantic_frontend_session,
)


_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")


@dataclass
class _WorkerState:
    context_digest: str | None = None
    session: SemanticFrontendSession | None = None


def _dependency_failure(
    *,
    code: str,
    wall_started: float,
    cpu_started: float,
    rss_start: int | None,
    message: str | None = None,
    frontend_launch_count: int = 1,
    semantic_session_hit_count: int = 0,
    semantic_session_miss_count: int = 1,
) -> dict[str, Any]:
    return _failure_payload(
        PrepareStatus.DEPENDENCY_BLOCKED,
        InternalBuildBlocker(
            code=code,
            stage="frontend_import",
            message=message,
        ),
        _worker_metrics(
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
            frontend_launch_count=frontend_launch_count,
            semantic_session_hit_count=semantic_session_hit_count,
            semantic_session_miss_count=semantic_session_miss_count,
        ),
    )


def _load_frontend(
    request: SourceGraphArtifactBuildRequest,
    *,
    wall_started: float,
    cpu_started: float,
    rss_start: int | None,
) -> tuple[Any, str] | dict[str, Any]:
    identity = request.source
    if request.identity.worker_protocol_version != SOURCE_GRAPH_WORKER_PROTOCOL_VERSION:
        return _dependency_failure(
            code="worker_protocol_identity_mismatch",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
        )
    if identity.frontend_name != SLANG_FRONTEND_NAME:
        return _dependency_failure(
            code="frontend_identity_unsupported",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
        )
    if (
        identity.projector_name != SOURCE_GRAPH_PROJECTOR_NAME
        or identity.projector_version != SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
        or identity.projector_schema_version
        != SOURCE_GRAPH_PROJECTOR_SCHEMA_VERSION
    ):
        return _dependency_failure(
            code="projector_version_mismatch",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
        )
    try:
        from pyslang import driver as driver_module

        version = importlib.metadata.version("pyslang")
    except Exception as exc:
        return _dependency_failure(
            code="frontend_unavailable",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
            message=f"{type(exc).__name__}: {exc}",
        )
    if version != identity.frontend_version:
        return _dependency_failure(
            code="frontend_version_mismatch",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
            message=f"expected {identity.frontend_version}, found {version}",
        )
    return driver_module, version


def execute_session_build(
    state: _WorkerState,
    request: SourceGraphArtifactBuildRequest,
) -> dict[str, Any]:
    """Create/reuse one exact semantic root and project a narrow artifact."""

    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    rss_start, _ = _read_rss_kib()
    context_key = compute_source_graph_semantic_context_key(request.identity)
    if not context_key.cross_request_reusable:
        return _dependency_failure(
            code="semantic_context_identity_incomplete",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
        )
    session_started = state.session is None
    if state.context_digest not in {None, context_key.digest}:
        return _dependency_failure(
            code="semantic_context_identity_mismatch",
            wall_started=wall_started,
            cpu_started=cpu_started,
            rss_start=rss_start,
        )

    try:
        if state.session is None:
            loaded = _load_frontend(
                request,
                wall_started=wall_started,
                cpu_started=cpu_started,
                rss_start=rss_start,
            )
            if isinstance(loaded, dict):
                return loaded
            driver_module, version = loaded
            state.session = create_semantic_frontend_session(
                request,
                driver_module=driver_module,
                frontend_version=version,
            )
            state.context_digest = context_key.digest
        (
            serialized,
            ir_fingerprint_sha256,
            scope_receipt,
            projection_receipt,
        ) = project_semantic_frontend_session(state.session, request)
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
                frontend_launch_count=1 if session_started else 0,
                semantic_session_hit_count=0 if session_started else 1,
                semantic_session_miss_count=1 if session_started else 0,
            ),
        )

    metrics = _worker_metrics(
        wall_started=wall_started,
        cpu_started=cpu_started,
        rss_start=rss_start,
        ir_bytes=len(serialized),
        frontend_launch_count=1 if session_started else 0,
        semantic_session_hit_count=0 if session_started else 1,
        semantic_session_miss_count=1 if session_started else 0,
    )
    return {
        "protocol_version": SOURCE_GRAPH_WORKER_PROTOCOL_VERSION,
        "status": PrepareStatus.READY.value,
        "ir_json_base64": base64.b64encode(serialized).decode("ascii"),
        "ir_fingerprint_sha256": ir_fingerprint_sha256,
        "scope_receipt": scope_receipt.to_dict(),
        "projection_receipt": {
            **projection_receipt,
            "semantic_session": {
                "disposition": "started" if session_started else "reused",
            },
        },
        "metrics": metrics.to_dict(),
        "fallback_used": False,
    }


def _request_paths(directory: Path, token: str) -> tuple[Path, Path]:
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("session worker token is invalid")
    return (
        directory / f"{token}.request.json",
        directory / f"{token}.response.json",
    )


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--directory", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_argument_parser().parse_args(argv)
    directory = args.directory.resolve(strict=True)
    if not directory.is_dir():
        raise SystemExit("session worker directory is unavailable")
    state = _WorkerState()
    for line in sys.stdin:
        try:
            command = json.loads(line)
            if not isinstance(command, Mapping) or command.get("command") != "build":
                raise ValueError("session worker command is invalid")
            token = str(command.get("token") or "")
            request_path, response_path = _request_paths(directory, token)
            request = _read_request(request_path)
            payload = execute_session_build(state, request)
        except Exception as exc:
            payload = _failure_payload(
                PrepareStatus.INVALID_RESPONSE,
                InternalBuildBlocker(
                    code="worker_request_invalid",
                    stage="worker_request",
                    message=f"{type(exc).__name__}: {exc}",
                ),
                _worker_metrics(
                    wall_started=time.perf_counter(),
                    cpu_started=time.process_time(),
                    rss_start=_read_rss_kib()[0],
                ),
            )
            try:
                token = str(command.get("token") or "")
                _, response_path = _request_paths(directory, token)
            except Exception:
                return 2
        _write_response(response_path, payload)
        print(token, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
